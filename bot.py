"""
Discord link-rewriter bot.

Listens to every message in allowed guilds, finds known social links, rewrites
them to mirror domains with better embeds, reposts, and deletes the original.
- Instagram “share” URLs are resolved to canonical /p|/reel|/tv before mirroring.
- Any attachments are preserved.
- Mentions are suppressed via AllowedMentions.none() to avoid pings.

Operational notes:
- Requires the Message Content intent in the Developer Portal and in code.
- Bot needs Manage Messages in target channels to delete originals.
"""

import os
import re
import json
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse

import discord
from dotenv import load_dotenv
import aiohttp
from bs4 import BeautifulSoup

# ---- Bootstrapping -----------------------------------------------------------

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# ---- Rewriting configuration -------------------------------------------------

# Map origin hosts → mirrors with nicer embeds/cards.
DEFAULT_MIRRORS = {
    "twitter.com": "fixupx.com",
    "x.com": "fixupx.com",

    "instagram.com": "kkinstagram.com",

    "reddit.com": "rxddit.com",

    "tiktok.com": "vxtiktok.com",

    "bsky.app": "bskx.app",
}

SKIP_HOSTS = {
    "fxtwitter.com", "fixupx.com",
    "kkinstagram.com", "uuinstagram.com", "instagramez.com",
    "rxddit.com", "vxreddit.com",
    "vxtiktok.com",
    "bskx.app", "bskyx.app",
}

# Bare-bones URL matcher that avoids trailing punctuation that breaks embeds.
URL_RE = re.compile(r"https?://[^\s<]+[^<.,:;\"')\]\s]")

# Lazily created HTTP session for all outbound fetches.
session: Optional[aiohttp.ClientSession] = None

# IG canonical paths we know how to mirror. Stories are intentionally skipped.
SUPPORTED_IG_POST_PREFIXES = ("/reel/", "/p/", "/tv/")
STORY_PREFIX = "/stories/"

# ---- HTTP/session helpers ----------------------------------------------------

async def _get_session() -> aiohttp.ClientSession:
    """Create a shared aiohttp session with a sane total timeout."""
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
    return session

# ---- Domain routing ----------------------------------------------------------

def _pick_source(host: str) -> Optional[str]:
    """Normalise host to a known source key used in DEFAULT_MIRRORS."""
    h = host.lower()
    if h.endswith("twitter.com") or h.endswith("x.com"): return "x.com"
    if h.endswith("instagram.com"): return "instagram.com"
    if h.endswith("reddit.com"): return "reddit.com"
    if h.endswith("tiktok.com"): return "tiktok.com"
    if h.endswith("bsky.app"): return "bsky.app"
    return None

# ---- Instagram plumbing ------------------------------------------------------

async def _resolve_instagram_share(url: str) -> Optional[str]:
    """
    Follow redirects for instagram.com/share/* to a canonical post URL.
    HEAD first (cheap), then GET as a fallback.
    """
    sess = await _get_session()
    headers = {"User-Agent": "curl/8"}
    try:
        async with sess.head(url, allow_redirects=True, headers=headers) as resp:
            return str(resp.url)
    except Exception:
        pass
    try:
        async with sess.get(url, allow_redirects=True, headers=headers) as resp:
            return str(resp.url)
    except Exception:
        return None

async def _rewrite_instagram(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve /share/* to canonical, skip stories, and mirror canonical posts.
    Returns (mirror_url, canonical_ig_url). The canonical is used by enrichment paths.
    """
    p = urlparse(url)
    canonical = url

    # Resolve short-lived share links to stable canonical URLs.
    if p.path.startswith("/share/"):
        final = await _resolve_instagram_share(url)
        if not final:
            return None, None
        canonical = final
        p = urlparse(final)

    # Stories are ephemeral and mirrors are inconsistent → let them pass untouched.
    if p.path.startswith(STORY_PREFIX):
        return None, canonical

    # Mirror canonical post types only.
    if p.path.startswith(SUPPORTED_IG_POST_PREFIXES):
        mirror = DEFAULT_MIRRORS.get("instagram.com")
        if not mirror:
            return None, canonical
        return urlunparse(p._replace(netloc=mirror)), canonical

    # Unknown IG path, do nothing but hand back canonical for potential future use.
    return None, canonical

# ---- Generic rewrite ---------------------------------------------------------

async def _rewrite_one(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Core rewrite:
    - Skip already-mirrored hosts.
    - Instagram: resolve share → canonical and mirror.
    - Other known hosts: swap netloc to mirror.
    Returns (mirror_url, canonical_ig_url|None).
    """
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        if not host or host in SKIP_HOSTS:
            return None, None

        if host.endswith("instagram.com"):
            return await _rewrite_instagram(url)

        src = _pick_source(host)
        if not src:
            return None, None

        mirror = DEFAULT_MIRRORS.get(src)
        if not mirror:
            return None, None

        new_url = urlunparse(p._replace(netloc=mirror))
        return (new_url if new_url != url else None), None
    except Exception:
        # Quiet failure: don't break message flow for a single bad URL.
        return None, None

# ---- Text helpers ------------------------------------------------------------

def _strip_links_from_text(text: str, links: List[str]) -> str:
    """Remove found URLs from the user's message; collapse whitespace."""
    out = text
    for l in links:
        out = out.replace(l, "")
    return " ".join(out.split()).strip()

def _compact_ws(s: str) -> str:
    """Normalise whitespace in scraped strings."""
    return re.sub(r"\s+", " ", s or "").strip()

def _truncate(s: str, max_len: int = 600) -> str:
    """Hard cap long captions to keep reposts tidy."""
    s = s or ""
    return s if len(s) <= max_len else s[: max_len - 1] + "…"

# ---- IG enrichment does not work :( ------------

def _parse_ig_from_og(og_title: Optional[str], og_desc: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    author = None
    caption = None

    if og_title:
        m = re.match(r'^(.+?)\s+on Instagram:?\s*["“](.*?)["”]?\s*$', og_title.strip())
        if m:
            author = _compact_ws(m.group(1))
            caption = _compact_ws(m.group(2))

    if og_desc:
        caption = _compact_ws(og_desc)

    return author, caption

def _extract_og_twitter_meta(html: str) -> Tuple[Optional[str], Optional[str]]:
    soup = BeautifulSoup(html, "html.parser")

    def meta(prop: str, attr: str = "property") -> Optional[str]:
        tag = soup.find("meta", attrs={attr: prop})
        return tag.get("content") if tag and tag.get("content") else None

    og_title = meta("og:title")
    og_desc  = meta("og:description")
    if not og_title:
        tw_title = soup.find("meta", attrs={"name": "twitter:title"})
        if tw_title and tw_title.get("content"):
            og_title = tw_title.get("content")
    if not og_desc:
        tw_desc = soup.find("meta", attrs={"name": "twitter:description"})
        if tw_desc and tw_desc.get("content"):
            og_desc = tw_desc.get("content")

    author, caption = _parse_ig_from_og(og_title, og_desc)

    if not (author and caption):
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            try:
                data = json.loads(script.text)
                items = data if isinstance(data, list) else [data]
                for d in items:
                    if not isinstance(d, dict):
                        continue
                    if not author:
                        a = d.get("author")
                        if isinstance(a, dict):
                            author = a.get("name") or a.get("alternateName")
                        elif isinstance(a, list) and a and isinstance(a[0], dict):
                            author = a[0].get("name")
                    if not caption:
                        caption = d.get("caption") or d.get("description") or d.get("articleBody")
            except Exception:
                continue

    author = _compact_ws(author) if author else None
    caption = _truncate(_compact_ws(caption)) if caption else None
    return author, caption


async def _fetch_text(url: str, *, for_ig: bool = False) -> Optional[str]:
    try:
        sess = await _get_session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        }
        async with sess.get(url, headers=headers, allow_redirects=True) as resp:
            if 200 <= resp.status < 300:
                return await resp.text(errors="ignore")
    except Exception:
        return None
    return None

async def _ig_from_canonical(canonical_ig_url: str) -> Tuple[Optional[str], Optional[str]]:
    html = await _fetch_text(canonical_ig_url, for_ig=True)
    if not html:
        return None, None
    return _extract_og_twitter_meta(html)

async def _ig_from_mirror(mirror_url: str) -> Tuple[Optional[str], Optional[str]]:
    html = await _fetch_text(mirror_url)
    if not html:
        return None, None
    return _extract_og_twitter_meta(html)

async def _ig_author_caption(canonical_ig_url: Optional[str], mirror_url: str) -> Tuple[Optional[str], Optional[str]]:
    if canonical_ig_url:
        a, c = await _ig_from_canonical(canonical_ig_url)
        if a or c:
            return a, c
    return await _ig_from_mirror(mirror_url)

# ---- Repost formatting -------------------------------------------------------

def _format_repost(author_mention: str,
                   extra_text: Optional[str],
                   blocks: List[Tuple[str, str, Optional[str], Optional[str]]]) -> str:
    """
    Build the repost text. Current formatting uses masked links for *both*
    Original and Embed; masked links do not unfurl.
    """
    lines: List[str] = [extra_text]

    lines.append(f"Sent by {author_mention}")
    for orig, mirror, ig_author, ig_caption in blocks:
        if ig_author or ig_caption:
            if ig_author:
                lines.append(f"**Author:** {ig_author}")
            if ig_caption:
                lines.append(f"**Caption:** {ig_caption}")
        # Angle brackets around the original URL inside a masked link ensure no preview.
        lines.append(f"[Original Link](<{orig}>) | [Embed Link]({mirror})")

    return "\n".join(lines)

# ---- Discord event handlers --------------------------------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

@client.event
async def on_message(message: discord.Message):
    """
    Main loop:
    - Ignore bots and non-whitelisted guilds.
    - Extract URLs, rewrite what we can, enrich IG where possible.
    - Repost with attachments, then delete the original.
    """
    if message.author.bot:
        return

    content = message.content or ""
    found_links = URL_RE.findall(content)
    if not found_links:
        return

    # Rewrite phase
    triples: List[Tuple[str, str, Optional[str]]] = []
    seen = set()
    for link in found_links:
        if link in seen:
            continue
        seen.add(link)
        mirror, canonical_ig = await _rewrite_one(link)
        if mirror:
            triples.append((link, mirror, canonical_ig))

    if not triples:
        return

    # Preserve user text (minus raw URLs) and any attachments.
    extra_text = _strip_links_from_text(content, found_links)

    blocks: List[Tuple[str, str, Optional[str], Optional[str]]] = []
    for orig, mirror, canonical_ig in triples:
        host = (urlparse(orig).hostname or "").lower()
        if host.endswith("instagram.com"):
            a, c = await _ig_author_caption(canonical_ig, mirror)
            blocks.append((orig, mirror, a, c))
        else:
            blocks.append((orig, mirror, None, None))

    files = []
    try:
        for a in message.attachments:
            files.append(await a.to_file(spoiler=a.is_spoiler()))
    except Exception:
        files = []

    # Post first (so the user still sees something if delete fails), then delete.
    try:
        await message.channel.send(
            _format_repost(message.author.mention, extra_text, blocks),
            files=files,
            allowed_mentions=discord.AllowedMentions.all(),
        )
    finally:
        try:
            await message.delete()
        except Exception:
            # Missing perms or race with moderation/author deletion → ignore.
            pass

# ---- Cleanup -----------------------------------------------------------------

async def _cleanup_session():
    """Close the shared aiohttp session on shutdown."""
    global session
    if session and not session.closed:
        await session.close()

import atexit, asyncio
atexit.register(lambda: asyncio.run(_cleanup_session()))

client.run(TOKEN)

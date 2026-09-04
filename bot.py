"""
Discord link-rewriter bot.

Watches guild messages, rewrites known social URLs to embed-friendly mirrors,
and re-posts the same text via a webhook so it still looks like the original
author. Discord cannot edit another user's message; webhook impersonation is
the supported substitute.

Requires the Message Content intent, plus Manage Messages and Manage Webhooks
in target channels.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp
import discord
from dotenv import load_dotenv

log = logging.getLogger("embedder")

# ---- Rewriting configuration -------------------------------------------------

DEFAULT_MIRRORS: Dict[str, str] = {
    "twitter.com": "fixupx.com",
    "x.com": "fixupx.com",
    "instagram.com": "oginstagram.com",
    "reddit.com": "vxreddit.com",
    "tiktok.com": "vxtiktok.com",
    "bsky.app": "bskx.app",
}

SKIP_HOSTS = {
    "fxtwitter.com",
    "fixupx.com",
    "kkinstagram.com",
    "uuinstagram.com",
    "instagramez.com",
    "oginstagram.com",
    "vxreddit.com",
    "rxeddit.com",
    "vxtiktok.com",
    "bskx.app",
    "bskyx.app",
}

TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}

# Bare-bones URL matcher that avoids trailing punctuation that breaks embeds.
URL_RE = re.compile(r"https?://[^\s<]+[^<.,:;\"')\]\s]")

# /p, /reel, /reels, /tv (optionally under a username) and /stories/.
# /share/* is excluded — those must be HTTP-resolved first.
IG_EMBEDDABLE_PATH = re.compile(
    r"^/(?!share/)(?:(?:[^/]+/)?(?:p|reel|reels|tv)/|stories/)",
    re.IGNORECASE,
)

TRACKING_QUERY_EXACT = {
    "igsh",
    "igshid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "si",
    "feature",
    "refsrc",
    "ref_src",
    "ref",
    "mbid",
    "src",
    "s",
    "t",
}

WEBHOOK_NAME = "Embedder"
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=8)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MAX_SEEN = 4000
MAX_FILES = 10


# ---- Pure rewrite helpers (unit-tested) --------------------------------------

def normalize_host(host: Optional[str]) -> str:
    """Lowercase hostname without a leading www."""
    h = (host or "").lower()
    if h.startswith("www."):
        h = h[4:]
    return h


def is_skipped_host(host: Optional[str]) -> bool:
    h = normalize_host(host)
    if not h:
        return True
    if h in SKIP_HOSTS:
        return True
    return any(h.endswith("." + skipped) for skipped in SKIP_HOSTS)


def pick_source(host: Optional[str]) -> Optional[str]:
    """Map a hostname to a DEFAULT_MIRRORS key, or None if unknown."""
    h = normalize_host(host)
    if not h or is_skipped_host(h):
        return None
    if h == "twitter.com" or h.endswith(".twitter.com"):
        return "twitter.com"
    if h == "x.com" or h.endswith(".x.com"):
        return "x.com"
    if h == "instagram.com" or h.endswith(".instagram.com"):
        return "instagram.com"
    if h == "reddit.com" or h.endswith(".reddit.com"):
        return "reddit.com"
    if h == "tiktok.com" or h.endswith(".tiktok.com"):
        return "tiktok.com"
    if h == "bsky.app" or h.endswith(".bsky.app"):
        return "bsky.app"
    return None


def is_instagram_embeddable(path: str) -> bool:
    return bool(IG_EMBEDDABLE_PATH.match(path or ""))


def needs_resolve(url: str) -> bool:
    """True when the URL must be followed before a mirror host can be chosen."""
    parsed = urlparse(url)
    host = normalize_host(parsed.hostname)
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return parsed.path.startswith("/share/")
    return host in TIKTOK_SHORT_HOSTS


def strip_tracking(url: str) -> str:
    """Drop common share/analytics query params and fragments."""
    parsed = urlparse(url)
    if not parsed.query:
        return urlunparse(parsed._replace(fragment=""))
    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_EXACT
        and not key.lower().startswith("utm_")
    ]
    return urlunparse(parsed._replace(query=urlencode(kept), fragment=""))


def rewrite_resolved(url: str) -> Optional[str]:
    """
    Swap a fully-resolved social URL onto its mirror host.
    Returns None when the URL should be left alone.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if is_skipped_host(host):
        return None

    source = pick_source(host)
    if not source:
        return None

    if source == "instagram.com" and not is_instagram_embeddable(parsed.path):
        return None

    mirror = DEFAULT_MIRRORS.get(source)
    if not mirror:
        return None

    cleaned = urlparse(strip_tracking(url))
    new_url = urlunparse(
        cleaned._replace(scheme="https", netloc=mirror)
    )
    return new_url if new_url != url else None


def apply_rewrites(text: str, replacements: Sequence[Tuple[str, str]]) -> str:
    """Replace original URLs with mirrors, longest match first."""
    out = text
    for original, mirror in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        out = out.replace(original, mirror)
    return out


def parse_allowed_guilds(raw: Optional[str]) -> Optional[set[int]]:
    text = (raw or "").strip()
    if not text:
        return None
    ids: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids or None


# ---- HTTP resolve ------------------------------------------------------------

async def resolve_redirect(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    headers = {"User-Agent": BROWSER_UA}
    try:
        async with session.head(url, allow_redirects=True, headers=headers) as resp:
            final = str(resp.url)
            if final:
                return final
    except Exception:
        log.debug("HEAD resolve failed for %s", url, exc_info=True)
    try:
        async with session.get(url, allow_redirects=True, headers=headers) as resp:
            return str(resp.url)
    except Exception:
        log.warning("GET resolve failed for %s", url, exc_info=True)
        return None


async def rewrite_one(url: str, session: aiohttp.ClientSession) -> Optional[str]:
    try:
        current = url
        if needs_resolve(url):
            resolved = await resolve_redirect(session, url)
            if not resolved:
                return None
            current = resolved
        return rewrite_resolved(current)
    except Exception:
        log.exception("Rewrite failed for %s", url)
        return None


# ---- Discord client ----------------------------------------------------------

class Embedder(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.allowed_guilds = parse_allowed_guilds(os.getenv("ALLOWED_GUILD_IDS"))
        self._webhooks: Dict[int, discord.Webhook] = {}
        self._seen: OrderedDict[int, None] = OrderedDict()

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)

    async def close(self) -> None:
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    def _mark_seen(self, message_id: int) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)
        return True

    def _guild_allowed(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if self.allowed_guilds is None:
            return True
        return message.guild.id in self.allowed_guilds

    async def on_ready(self) -> None:
        guilds = ", ".join(f"{g.name} ({g.id})" for g in self.guilds) or "(none)"
        allow = (
            "all guilds"
            if self.allowed_guilds is None
            else f"allowlist {sorted(self.allowed_guilds)}"
        )
        log.info("Logged in as %s — %s — %s", self.user, allow, guilds)

    async def on_message(self, message: discord.Message) -> None:
        await self._process_message(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content:
            return
        await self._process_message(after)

    async def _process_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id:
            return
        if not self._guild_allowed(message):
            return
        if message.id in self._seen:
            return

        content = message.content or ""
        found = URL_RE.findall(content)
        if not found:
            return

        session = self.http_session
        if session is None or session.closed:
            log.error("HTTP session is not available")
            return

        unique: List[str] = list(dict.fromkeys(found))
        mirrors = await asyncio.gather(*(rewrite_one(url, session) for url in unique))
        replacements = [
            (original, mirror)
            for original, mirror in zip(unique, mirrors)
            if mirror
        ]
        if not replacements:
            return

        rewritten = apply_rewrites(content, replacements)
        if rewritten == content:
            return

        if not self._mark_seen(message.id):
            return

        try:
            posted = await self._repost(message, rewritten)
        except Exception:
            self._seen.pop(message.id, None)
            log.exception("Failed to repost message %s", message.id)
            return

        if posted is None:
            self._seen.pop(message.id, None)
            return

        await self._delete_original(message)

    async def _collect_files(self, message: discord.Message) -> List[discord.File]:
        files: List[discord.File] = []
        for attachment in message.attachments[:MAX_FILES]:
            try:
                files.append(await attachment.to_file(spoiler=attachment.is_spoiler()))
            except Exception:
                log.warning(
                    "Could not copy attachment %s on message %s",
                    attachment.filename,
                    message.id,
                    exc_info=True,
                )
        return files

    async def _get_webhook(self, channel: discord.abc.GuildChannel) -> discord.Webhook:
        cached = self._webhooks.get(channel.id)
        if cached:
            return cached

        existing = await channel.webhooks()
        # User-created incoming webhooks usually omit Discord's APP badge.
        # Bot-created ones always show it. discord.py's Webhook may not
        # expose application_id, so ownership is used instead.
        chosen = None
        bot_owned = None
        me = self.user
        for hook in existing:
            if hook.token is None:
                continue
            owner_id = hook.user.id if hook.user is not None else None
            is_ours = me is not None and owner_id == me.id
            if not is_ours:
                chosen = hook
                break
            if bot_owned is None:
                bot_owned = hook

        if chosen is None:
            chosen = bot_owned
        if chosen is None:
            chosen = await channel.create_webhook(
                name=WEBHOOK_NAME,
                reason="Repost rewritten social embeds as the original author",
            )
        self._webhooks[channel.id] = chosen
        return chosen

    async def _repost(self, message: discord.Message, content: str) -> Optional[discord.Message]:
        channel = message.channel
        thread: Optional[discord.Thread] = None
        hook_channel: Optional[discord.abc.GuildChannel] = None

        if isinstance(channel, discord.Thread):
            thread = channel
            hook_channel = channel.parent
        elif isinstance(channel, discord.abc.GuildChannel):
            hook_channel = channel

        if hook_channel is None or not hasattr(hook_channel, "webhooks"):
            log.warning("No webhook channel for message %s; leaving original", message.id)
            return None

        for _attempt in range(2):
            files = await self._collect_files(message)
            try:
                webhook = await self._get_webhook(hook_channel)
                sent = await self._webhook_send(
                    webhook,
                    content=content,
                    username=message.author.display_name,
                    avatar_url=message.author.display_avatar.url,
                    files=files,
                    thread=thread,
                )
                if sent is not None:
                    return sent
                self._webhooks.pop(hook_channel.id, None)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Webhook send failed (%s); leaving original message", exc)
                self._webhooks.pop(hook_channel.id, None)
                return None

        return None

    async def _webhook_send(
        self,
        webhook: discord.Webhook,
        *,
        content: str,
        username: str,
        avatar_url: str,
        files: List[discord.File],
        thread: Optional[discord.Thread],
    ) -> Optional[discord.Message]:
        kwargs: dict = {
            "content": content,
            "username": username,
            "avatar_url": avatar_url,
            "allowed_mentions": discord.AllowedMentions.none(),
            "wait": True,
        }
        if files:
            kwargs["files"] = files
        if thread is not None:
            kwargs["thread"] = thread

        try:
            return await webhook.send(**kwargs)
        except discord.NotFound:
            channel_id = webhook.channel_id
            if channel_id:
                self._webhooks.pop(channel_id, None)
            log.info("Cached webhook was deleted; recreating")
            return None

    async def _delete_original(self, message: discord.Message) -> None:
        try:
            await message.delete()
        except discord.HTTPException:
            log.warning(
                "Could not delete original message %s (missing Manage Messages?)",
                message.id,
            )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    load_dotenv()
    token = (os.getenv("DISCORD_TOKEN") or "").strip().strip("\"'")
    if not token:
        log.error("DISCORD_TOKEN is not set")
        raise SystemExit(1)

    try:
        parse_allowed_guilds(os.getenv("ALLOWED_GUILD_IDS"))
    except ValueError:
        log.error("ALLOWED_GUILD_IDS must be a comma-separated list of integers")
        raise SystemExit(1)

    client = Embedder()
    try:
        # log_handler=None: use the root handler above so journald gets a single stream.
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error(
            "Discord rejected the bot token. Open the Developer Portal, "
            "Bot tab, reset the token, and paste the full value into .env "
            "as DISCORD_TOKEN=... with no quotes or spaces."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()

"""
Discord link-rewriter bot.

Watches guild messages, rewrites known social URLs to embed-friendly mirrors,
and re-posts the same text via a webhook so it still looks like the original
author. Discord cannot edit another user's message; webhook impersonation is
the supported substitute.

In one server, edits and deletes are also posted to a log channel. The text
comes from a short in-memory cache. Deletes this bot makes while rewriting a
link are not logged.

Requires the Message Content intent, plus Manage Messages and Manage Webhooks
in target channels. The log channel needs Send Messages and Embed Links.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
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

# Edit/delete reports for a single server. Rewrites still follow ALLOWED_GUILD_IDS.
AUDIT_GUILD_ID = 763081865455206450
AUDIT_CHANNEL_ID = 1557109450861453322
MAX_SNAPSHOTS = 5000
MAX_BOT_DELETES = 2000
AUDIT_FIELD_LIMIT = 1000
BULK_LINE_LIMIT = 300
MISSING_EDIT_TEXT = "(previous text was not available)"
MISSING_DELETE_TEXT = "(text was not available)"


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


# ---- Edit / delete audit (unit-tested) --------------------------------------

@dataclass(frozen=True)
class MessageSnapshot:
    message_id: int
    channel_id: int
    author_id: int
    author_name: str
    content: str
    attachments: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuditMessage:
    message_id: int
    channel_id: int
    author_id: Optional[int]
    author_name: str
    content: Optional[str]
    attachments: tuple[str, ...] = ()


class SnapshotCache:
    """Bounded memory of recent messages so deletes can quote text."""

    def __init__(self, max_size: int = MAX_SNAPSHOTS) -> None:
        self.max_size = max_size
        self._items: OrderedDict[int, MessageSnapshot] = OrderedDict()

    def remember(self, snapshot: MessageSnapshot) -> None:
        self._items[snapshot.message_id] = snapshot
        self._items.move_to_end(snapshot.message_id)
        while len(self._items) > self.max_size:
            self._items.popitem(last=False)

    def get(self, message_id: int) -> Optional[MessageSnapshot]:
        snapshot = self._items.get(message_id)
        if snapshot is not None:
            self._items.move_to_end(message_id)
        return snapshot

    def pop(self, message_id: int) -> Optional[MessageSnapshot]:
        return self._items.pop(message_id, None)


def mark_bot_delete(
    deleted: OrderedDict[int, None],
    message_id: int,
    *,
    limit: int = MAX_BOT_DELETES,
) -> None:
    """Remember a message id this bot is about to delete."""
    deleted[message_id] = None
    deleted.move_to_end(message_id)
    while len(deleted) > limit:
        deleted.popitem(last=False)


def consume_bot_delete(deleted: OrderedDict[int, None], message_id: int) -> bool:
    """True when this delete was the bot's own rewrite and should not be logged."""
    if message_id not in deleted:
        return False
    del deleted[message_id]
    return True


def filter_bot_deletes(
    message_ids: Iterable[int],
    deleted: OrderedDict[int, None],
) -> List[int]:
    """Drop ids this bot deleted, consuming them so they are not logged later."""
    kept: List[int] = []
    for message_id in message_ids:
        if consume_bot_delete(deleted, message_id):
            continue
        kept.append(message_id)
    return kept


def is_real_edit(edited_timestamp: Optional[str], before: Optional[str], after: str) -> bool:
    """True for a user edit. Embed unfurls leave edited_timestamp empty.

    When the previous text is known, it has to differ. When it is not, any
    edit timestamp is enough to report the new text.
    """
    if not edited_timestamp:
        return False
    if before is None:
        return True
    return before != after


def truncate_audit_text(text: str, limit: int = AUDIT_FIELD_LIMIT) -> str:
    if not text:
        return "(no text)"
    if len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1] + "…"


def format_author(author_id: Optional[int], author_name: str) -> str:
    name = author_name or "Unknown"
    if author_id is None:
        return name
    return f"{name} (`{author_id}`)"


def channel_place(channel_name: Optional[str], channel_id: int) -> str:
    if channel_name:
        return f"#{channel_name}"
    return f"channel `{channel_id}`"


def message_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def audit_from_snapshot(snapshot: MessageSnapshot) -> AuditMessage:
    return AuditMessage(
        message_id=snapshot.message_id,
        channel_id=snapshot.channel_id,
        author_id=snapshot.author_id,
        author_name=snapshot.author_name,
        content=snapshot.content,
        attachments=snapshot.attachments,
    )


def format_edit_embed(
    *,
    guild_id: int,
    channel_id: int,
    channel_name: Optional[str],
    message_id: int,
    author_id: Optional[int],
    author_name: str,
    before: Optional[str],
    after: str,
) -> discord.Embed:
    jump = message_jump_url(guild_id, channel_id, message_id)
    embed = discord.Embed(
        title="Message edited",
        description=f"In {channel_place(channel_name, channel_id)}\n[Jump to message]({jump})",
        colour=discord.Color.orange(),
    )
    embed.add_field(
        name="Author",
        value=truncate_audit_text(format_author(author_id, author_name)),
        inline=False,
    )
    before_text = MISSING_EDIT_TEXT if before is None else truncate_audit_text(before)
    embed.add_field(name="Before", value=before_text, inline=False)
    embed.add_field(name="After", value=truncate_audit_text(after), inline=False)
    return embed


def format_delete_embed(
    *,
    channel_name: Optional[str],
    entry: AuditMessage,
) -> discord.Embed:
    embed = discord.Embed(
        title="Message deleted",
        description=f"In {channel_place(channel_name, entry.channel_id)}",
        colour=discord.Color.red(),
    )
    embed.add_field(
        name="Author",
        value=truncate_audit_text(format_author(entry.author_id, entry.author_name)),
        inline=False,
    )
    content = MISSING_DELETE_TEXT if entry.content is None else truncate_audit_text(entry.content)
    embed.add_field(name="Content", value=content, inline=False)
    if entry.attachments:
        embed.add_field(
            name="Attachments",
            value=truncate_audit_text(", ".join(entry.attachments)),
            inline=False,
        )
    embed.add_field(name="Message ID", value=f"`{entry.message_id}`", inline=False)
    return embed


def _bulk_line(entry: AuditMessage) -> str:
    author = format_author(entry.author_id, entry.author_name)
    if entry.content is None:
        body = MISSING_DELETE_TEXT
    else:
        body = truncate_audit_text(entry.content.replace("\n", " "), BULK_LINE_LIMIT)
    files = ""
    if entry.attachments:
        files = " (files: " + ", ".join(entry.attachments) + ")"
    return f"• {author}: {body}{files} (`{entry.message_id}`)"


def chunk_lines(lines: Sequence[str], limit: int) -> List[List[str]]:
    chunks: List[List[str]] = []
    current: List[str] = []
    size = 0
    for raw in lines:
        line = raw if len(raw) <= limit else truncate_audit_text(raw, limit)
        separator = 1 if current else 0
        if current and size + separator + len(line) > limit:
            chunks.append(current)
            current = [line]
            size = len(line)
            continue
        current.append(line)
        size += separator + len(line)
    if current:
        chunks.append(current)
    return chunks


def format_bulk_delete_embeds(
    *,
    channel_id: int,
    channel_name: Optional[str],
    entries: Sequence[AuditMessage],
    description_limit: int = 4000,
) -> List[discord.Embed]:
    if not entries:
        return []
    noun = "message" if len(entries) == 1 else "messages"
    header = f"{len(entries)} {noun} removed in {channel_place(channel_name, channel_id)}"
    lines = [header, *(_bulk_line(entry) for entry in entries)]
    embeds: List[discord.Embed] = []
    for index, group in enumerate(chunk_lines(lines, description_limit)):
        title = "Messages deleted" if index == 0 else "Messages deleted (continued)"
        embeds.append(
            discord.Embed(
                title=title,
                description="\n".join(group),
                colour=discord.Color.red(),
            )
        )
    return embeds


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
        self._snapshots = SnapshotCache()
        self._bot_deleted: OrderedDict[int, None] = OrderedDict()
        self._audit_channel_warned = False

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
        log.info(
            "Logged in as %s — %s — audit guild %s -> channel %s — %s",
            self.user,
            allow,
            AUDIT_GUILD_ID,
            AUDIT_CHANNEL_ID,
            guilds,
        )

    async def on_message(self, message: discord.Message) -> None:
        self._remember_message(message)
        await self._process_message(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        # Capture the before/after text before the first await. discord.py
        # schedules this before on_message_edit, and the rewrite path may
        # delete the message once that handler runs.
        guild_id = payload.guild_id
        if guild_id is None:
            raw_guild = payload.data.get("guild_id")
            guild_id = int(raw_guild) if raw_guild is not None else None
        if guild_id != AUDIT_GUILD_ID or "content" not in payload.data:
            return

        after = payload.data.get("content") or ""
        previous = self._snapshots.get(payload.message_id)
        if previous is not None:
            before: Optional[str] = previous.content
        elif payload.cached_message is not None:
            before = payload.cached_message.content or ""
        else:
            before = None

        self._store_edit_snapshot(payload, previous, after)
        if not is_real_edit(payload.data.get("edited_timestamp"), before, after):
            return

        author_id, author_name = self._audit_author(previous, payload)
        embed = format_edit_embed(
            guild_id=AUDIT_GUILD_ID,
            channel_id=payload.channel_id,
            channel_name=self._audit_channel_name(payload.channel_id),
            message_id=payload.message_id,
            author_id=author_id,
            author_name=author_name,
            before=before,
            after=after,
        )
        await self._post_audit([embed])

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id != AUDIT_GUILD_ID:
            return
        if consume_bot_delete(self._bot_deleted, payload.message_id):
            self._snapshots.pop(payload.message_id)
            return
        entry = self._take_audit_message(
            payload.message_id,
            payload.channel_id,
            payload.cached_message,
        )
        embed = format_delete_embed(
            channel_name=self._audit_channel_name(payload.channel_id),
            entry=entry,
        )
        await self._post_audit([embed])

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        if payload.guild_id != AUDIT_GUILD_ID:
            return
        cached = {message.id: message for message in payload.cached_messages}
        kept = filter_bot_deletes(sorted(payload.message_ids), self._bot_deleted)
        for message_id in payload.message_ids:
            if message_id not in kept:
                self._snapshots.pop(message_id)
        if not kept:
            return
        entries = [
            self._take_audit_message(message_id, payload.channel_id, cached.get(message_id))
            for message_id in kept
        ]
        embeds = format_bulk_delete_embeds(
            channel_id=payload.channel_id,
            channel_name=self._audit_channel_name(payload.channel_id),
            entries=entries,
        )
        await self._post_audit(embeds)

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

    def _remember_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != AUDIT_GUILD_ID:
            return
        author = message.author
        author_name = getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
        self._snapshots.remember(
            MessageSnapshot(
                message_id=message.id,
                channel_id=message.channel.id,
                author_id=author.id,
                author_name=author_name,
                content=message.content or "",
                attachments=tuple(attachment.filename for attachment in message.attachments),
            )
        )

    def _audit_channel_name(self, channel_id: int) -> Optional[str]:
        channel = self.get_channel(channel_id)
        if channel is None:
            return None
        name = getattr(channel, "name", None)
        return name if isinstance(name, str) else None

    def _audit_author(
        self,
        previous: Optional[MessageSnapshot],
        payload: discord.RawMessageUpdateEvent,
    ) -> Tuple[Optional[int], str]:
        if previous is not None:
            return previous.author_id, previous.author_name
        for message in (payload.cached_message, payload.message):
            if message is None:
                continue
            author = getattr(message, "author", None)
            if author is None or not hasattr(author, "id"):
                continue
            name = getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
            return author.id, name
        return None, "Unknown"

    def _store_edit_snapshot(
        self,
        payload: discord.RawMessageUpdateEvent,
        previous: Optional[MessageSnapshot],
        after: str,
    ) -> None:
        if previous is not None:
            self._snapshots.remember(replace(previous, content=after))
            return

        cached = payload.cached_message
        if cached is not None and getattr(cached, "author", None) is not None:
            author = cached.author
            author_name = getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
            self._snapshots.remember(
                MessageSnapshot(
                    message_id=payload.message_id,
                    channel_id=payload.channel_id,
                    author_id=author.id,
                    author_name=author_name,
                    content=after,
                    attachments=tuple(attachment.filename for attachment in cached.attachments),
                )
            )
            return

        message = payload.message
        author = getattr(message, "author", None)
        if author is None or not hasattr(author, "id"):
            return
        author_name = getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
        attachments = tuple(
            attachment.filename for attachment in getattr(message, "attachments", []) or []
        )
        self._snapshots.remember(
            MessageSnapshot(
                message_id=payload.message_id,
                channel_id=payload.channel_id,
                author_id=author.id,
                author_name=author_name,
                content=after,
                attachments=attachments,
            )
        )

    def _take_audit_message(
        self,
        message_id: int,
        channel_id: int,
        cached: Optional[discord.Message],
    ) -> AuditMessage:
        previous = self._snapshots.pop(message_id)
        if previous is not None:
            return audit_from_snapshot(previous)
        if cached is not None and getattr(cached, "author", None) is not None:
            author = cached.author
            author_name = getattr(author, "display_name", None) or getattr(author, "name", None) or "Unknown"
            return AuditMessage(
                message_id=message_id,
                channel_id=channel_id,
                author_id=author.id,
                author_name=author_name,
                content=cached.content or "",
                attachments=tuple(attachment.filename for attachment in cached.attachments),
            )
        return AuditMessage(
            message_id=message_id,
            channel_id=channel_id,
            author_id=None,
            author_name="Unknown",
            content=None,
        )

    async def _post_audit(self, embeds: Sequence[discord.Embed]) -> None:
        if not embeds:
            return
        channel = self.get_channel(AUDIT_CHANNEL_ID)
        if channel is None:
            try:
                channel = await self.fetch_channel(AUDIT_CHANNEL_ID)
            except discord.HTTPException:
                if not self._audit_channel_warned:
                    log.warning("Audit channel %s is not available", AUDIT_CHANNEL_ID)
                    self._audit_channel_warned = True
                return
        if not hasattr(channel, "send"):
            log.warning("Audit channel %s cannot receive messages", AUDIT_CHANNEL_ID)
            return
        try:
            for start in range(0, len(embeds), 10):
                await channel.send(
                    embeds=list(embeds[start : start + 10]),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            log.warning(
                "Could not post message audit to channel %s",
                AUDIT_CHANNEL_ID,
                exc_info=True,
            )

    async def _delete_original(self, message: discord.Message) -> None:
        track = message.guild is not None and message.guild.id == AUDIT_GUILD_ID
        if track:
            mark_bot_delete(self._bot_deleted, message.id)
        try:
            await message.delete()
        except discord.HTTPException:
            if track:
                self._bot_deleted.pop(message.id, None)
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

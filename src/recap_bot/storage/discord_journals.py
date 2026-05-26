"""Journals stored in the Discord channel itself.

The channel where the bot runs is assumed to contain ONLY journal entries —
one journal per message, chronological. We don't filter by author or
attachment type: every regular message in the channel is a journal entry.

Content source for each message, in priority order:
  1. A `.md` attachment, if present (preferred — the bot's own posts use this)
  2. The message body (plain-text journals posted by players)

Messages with neither (e.g. image-only posts or system events like pins) are
skipped. Session numbers are assigned 1..N based on chronological position.

If the message body matches `**Session N · date · style**`, those metadata
fields are extracted for display; otherwise we fall back to the message's
Discord timestamp for `date` and "unknown" for `style`. Either way, the
authoritative `session` number is the chronological index.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import discord

from recap_bot.storage import files as channel_files

logger = logging.getLogger(__name__)

# Headers used by the bot's own posts. The new format is just the VOD title
# (bold). Style + date are in the attached journal.md content itself.
#
# Legacy formats (still parsed by list_for_channel so old posts are recognized):
#   **Recap · 2024-05-17 · chapters**
#   **Session 0042 · 2024-05-17 · chapters**
_LEGACY_DATED_HEADER_RE = re.compile(r"\*\*Recap\s*·\s*(?P<date>.+?)\s*·\s*(?P<style>\w+)\*\*", re.MULTILINE)
_LEGACY_SESSION_HEADER_RE = re.compile(r"\*\*Session\s+\d+\s*·\s*(?P<date>.+?)\s*·\s*(?P<style>\w+)\*\*", re.MULTILINE)


@dataclass
class JournalEntry:
    session: int                       # chronological position in the channel (1-based, for our cache only — NOT a session number)
    date: str                          # from header, else message timestamp
    style: str                         # from header, else "unknown"
    message_id: int
    attachment_filename: Optional[str] = None
    attachment: Optional[discord.Attachment] = field(default=None, repr=False)
    body: str = ""                     # plain-text fallback when no attachment
    content: Optional[str] = None      # resolved content (filled by fetch_content)


def format_header(title: str) -> str:
    """Header for bot-posted journals — just the VOD title (bold).

    Bot scrubs Discord markdown chars so the title renders cleanly.
    """
    # Escape Discord's bold/italic markers in the title so the surrounding
    # ** doesn't break.
    cleaned = title.replace("**", "").replace("__", "").strip()
    return f"**{cleaned}**" if cleaned else "**Recap**"


# Discord MessageType values that represent real user/bot content (not system
# notifications like joins, pins, thread-created, etc.)
_USER_CONTENT_TYPES = {discord.MessageType.default, discord.MessageType.reply}


async def list_for_channel(bot: discord.Client, channel_id: int, limit: int = 1000) -> list[JournalEntry]:
    """Treat every regular message in the channel as a journal entry.

    Skips: system messages, and messages with neither a `.md` attachment nor a
    non-empty body. Returns entries with session numbers 1..N in chronological
    order (oldest first).
    """
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            logger.warning("Channel %s not accessible", channel_id)
            return []

    entries: list[JournalEntry] = []
    session_num = 0

    async for msg in channel.history(limit=limit, oldest_first=True):
        if msg.type not in _USER_CONTENT_TYPES:
            continue

        md_attachment = next(
            (a for a in msg.attachments if a.filename.lower().endswith(".md")),
            None,
        )
        body = (msg.content or "").strip()

        # Need at least one source of content
        if md_attachment is None and not body:
            continue

        # Pull optional header metadata for nicer display. The new format
        # has just the VOD title in bold and no parseable date/style, so we
        # fall back to message timestamp and "unknown" style in that case.
        content = msg.content or ""
        match = _LEGACY_DATED_HEADER_RE.search(content) or _LEGACY_SESSION_HEADER_RE.search(content)
        if match:
            date = match.group("date").strip()
            style = match.group("style").strip().lower()
        else:
            date = msg.created_at.strftime("%Y-%m-%d")
            style = "unknown"

        session_num += 1
        entries.append(JournalEntry(
            session=session_num,
            date=date,
            style=style,
            message_id=msg.id,
            attachment_filename=md_attachment.filename if md_attachment else None,
            attachment=md_attachment,
            body=body,
        ))

    return entries


async def fetch_content(category_id: int, entry: JournalEntry) -> str:
    """Return the journal body for this entry, caching on disk under the
    CATEGORY (journals are scanned from the journal channel but cached per
    category, the storage scope).

    Preference: existing disk cache → `.md` attachment → message body.
    """
    cached = await channel_files.read_journal_cache(category_id, entry.session)
    if cached is not None:
        return cached

    if entry.attachment is not None:
        raw = await entry.attachment.read()
        text = raw.decode("utf-8", errors="replace")
    else:
        text = entry.body

    if text:
        await channel_files.write_journal_cache(category_id, entry.session, text)
    entry.content = text
    return text


async def count_for_channel(bot: discord.Client, channel_id: int) -> int:
    """Total count of journal-bearing messages in the channel.

    Used by /recap's sanity check (`journals_synced` vs current count) to detect
    when new entries have been added since the last init/recap.
    """
    entries = await list_for_channel(bot, channel_id)
    return len(entries)


async def post_journal(
    bot: discord.Client,
    channel_id: int,
    journal_md: str,
    *,
    vod_id: str,
    title: str,
    date: str,
) -> int:
    """Post a journal message titled by the Twitch VOD title + .md attachment.

    Returns the Discord message id. The attachment filename embeds the date
    and VOD id so the source is identifiable from the filename alone.
    """
    from io import BytesIO

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    safe_date = re.sub(r"[^\w\-]", "_", date)
    safe_vod = re.sub(r"[^\w\-]", "_", vod_id)
    filename = f"recap-{safe_date}-vod{safe_vod}.md"

    header = format_header(title)
    file = discord.File(BytesIO(journal_md.encode("utf-8")), filename=filename)
    # Persistent "✏️ Edit" button so anyone with Manage Channels can re-run
    # /recap_edit for this specific recap.
    from recap_bot.commands._edit_button import make_edit_view
    view = make_edit_view("journal", vod_id)
    msg = await channel.send(content=header, file=file, view=view)
    return msg.id


async def edit_journal_message(
    bot: discord.Client,
    channel_id: int,
    message_id: int,
    new_md_bytes: bytes,
) -> Optional[int]:
    """Edit a previously-posted recap message in place: swap the `.md` attachment.

    Preserves the original message content (bold title header), the existing
    attachment filename, and the persistent "✏️ Edit" view. Returns the
    message id on success. Returns `None` if the original message was deleted
    (in which case the caller should not write to disk, so on-disk and
    in-channel stay in sync).
    """
    from io import BytesIO

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    try:
        msg = await channel.fetch_message(message_id)
    except discord.NotFound:
        return None

    existing_md = next(
        (a for a in msg.attachments if a.filename.lower().endswith(".md")),
        None,
    )
    filename = existing_md.filename if existing_md else "recap.md"

    file = discord.File(BytesIO(new_md_bytes), filename=filename)
    # Omitting content= and view= keeps them unchanged; attachments=[file]
    # replaces the entire attachment list with just our new file.
    await msg.edit(attachments=[file])
    return msg.id


async def find_recap_message_id(
    bot: discord.Client, channel_id: int, vod_id: str, scan_limit: int = 500,
) -> Optional[int]:
    """Scan recent channel history for the bot's recap post for this VOD.

    Backfill path for `/recap_edit` when `discord_msg_id.txt` is missing
    (e.g. recap was posted before this feature shipped). Matches by `.md`
    attachment filename containing `vod<vod_id>` — that's the pattern
    `post_journal()` writes.
    """
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None

    needle = f"vod{vod_id}"
    bot_id = bot.user.id if bot.user else None
    async for msg in channel.history(limit=scan_limit, oldest_first=False):
        if bot_id is not None and msg.author.id != bot_id:
            continue
        for a in msg.attachments:
            name = a.filename.lower()
            if name.endswith(".md") and needle in name:
                return msg.id
    return None

"""Journals stored in the Discord channel itself.

The channel where the bot runs is assumed to contain ONLY journal entries —
one journal per message, chronological. We don't filter by author or
attachment type: every regular message in the channel is a journal entry.

Content source for each message, in priority order:
  1. A `.md` attachment, if present (legacy — the bot used to attach one)
  2. An embed description (current — the bot posts the journal as an embed)
  3. The message body (plain-text journals posted by players)

Messages with none of these (e.g. image-only posts or system events like pins)
are skipped. Session numbers are assigned 1..N based on chronological position.

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
    embed_body: str = ""               # journal text reconstructed from an embed post
    content: Optional[str] = None      # resolved content (filled by fetch_content)


def format_header(title: str) -> str:
    """Header for bot-posted journals — just the VOD title (bold).

    Bot scrubs Discord markdown chars so the title renders cleanly.
    """
    # Escape Discord's bold/italic markers in the title so the surrounding
    # ** doesn't break.
    cleaned = title.replace("**", "").replace("__", "").strip()
    return f"**{cleaned}**" if cleaned else "**Recap**"


# --- Embed-based display -------------------------------------------------
# The journal is posted as a single embed (no attachment):
#   - Standard recaps render the markdown (pretty, reads inline).
#   - Silent recaps wrap it in a ```md code block (copy-paste keeps the syntax).
# The full journal.md stays on disk (data/categories/<id>/recaps/...) as the
# durable source of truth; the channel post is display-only and journals are
# not editable in place. Re-ingestion (list_for_channel / fetch_content) reads
# the journal back from the embed body (see _extract_embed_body).
# The 4000-char journal cap (see pipeline.summarize.MAX_JOURNAL_CHARS) keeps the
# body within Discord's 4096-char embed-description limit, so one embed suffices.

EMBED_COLOR = 0x9B59B6  # purple — magical without being garish
_EMBED_DESC_LIMIT = 4096


def _body_for_embed(journal_md: str) -> str:
    """Strip a leading `# Title` heading from the journal body.

    The title is shown in the message content header (`**Title**`) above the
    embed, so repeating it as an `# H1` inside the embed would be redundant.
    Everything else (including the `## Session Date` line and all scenes) stays.
    """
    text = journal_md.lstrip()
    if text.startswith("# "):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) > 1 else ""
    return text.strip()


def render_journal_embed(journal_md: str, *, date: str = "") -> "discord.Embed":
    """Rendered embed (markdown interpreted) — used for STANDARD channel posts."""
    body = _body_for_embed(journal_md)
    embed = discord.Embed(description=body[:_EMBED_DESC_LIMIT], color=EMBED_COLOR)
    if date:
        embed.set_footer(text=date)
    return embed


def codeblock_journal_embed(journal_md: str, *, date: str = "") -> "discord.Embed":
    """Code-block embed (raw markdown shown) — used for SILENT DM delivery.

    Wrapping the body in a ```md fenced block means Discord shows the literal
    `##`, `**`, `-` syntax instead of rendering it. Selecting + copying then
    preserves the markdown, so the user can paste it into notes/wiki/etc. and
    have it re-render.
    """
    body = _body_for_embed(journal_md)
    # Reserve room for the fences within the 4096 cap. "```md\n" = 6 chars and
    # "\n```" = 4 chars = 10; reserve 12 for a small safety margin.
    fence_overhead = 12
    inner = body[: _EMBED_DESC_LIMIT - fence_overhead]
    embed = discord.Embed(description=f"```md\n{inner}\n```", color=EMBED_COLOR)
    footer = "Copy the text above to reuse it with Markdown formatting"
    if date:
        footer = f"{date} · {footer}"
    embed.set_footer(text=footer)
    return embed


def _extract_embed_body(msg: "discord.Message") -> str:
    """Pull journal text from a bot recap embed.

    The journal body lives in the first embed's `description`. Strips a leading
    ```md code-block fence if present (silent-mode embeds use one; channel posts
    don't, but be robust in case one ever lands here). Returns "" if there's no
    embed or no description.
    """
    if not msg.embeds:
        return ""
    desc = (msg.embeds[0].description or "").strip()
    if not desc:
        return ""
    if desc.startswith("```"):
        nl = desc.find("\n")
        if nl != -1:
            desc = desc[nl + 1:]
        if desc.rstrip().endswith("```"):
            desc = desc.rstrip()[:-3]
    return desc.strip()


# Discord MessageType values that represent real user/bot content (not system
# notifications like joins, pins, thread-created, etc.)
_USER_CONTENT_TYPES = {discord.MessageType.default, discord.MessageType.reply}


async def list_for_channel(bot: discord.Client, channel_id: int, limit: int = 1000) -> list[JournalEntry]:
    """Treat every regular message in the channel as a journal entry.

    Skips: system messages, and messages with no content source (no `.md`
    attachment, no embed body, no plain-text body). Returns entries with
    session numbers 1..N in chronological order (oldest first).
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

        # The bot's recap posts carry the journal in an embed, with just a bold
        # title in the message content. Reconstruct the full journal text from
        # the embed (de-bolded title header + embed body) so re-ingestion sees
        # the same content the old `.md` attachment used to provide.
        embed_raw = _extract_embed_body(msg)
        embed_body = ""
        if embed_raw:
            title = body.strip("*").strip()
            embed_body = f"# {title}\n\n{embed_raw}" if title else embed_raw

        # Need at least one source of content
        if md_attachment is None and not embed_body and not body:
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
            embed_body=embed_body,
        ))

    return entries


async def fetch_content(category_id: int, entry: JournalEntry) -> str:
    """Return the journal body for this entry, caching on disk under the
    CATEGORY (journals are scanned from the journal channel but cached per
    category, the storage scope).

    Preference: existing disk cache → `.md` attachment → embed body →
    message body.
    """
    cached = await channel_files.read_journal_cache(category_id, entry.session)
    if cached is not None:
        return cached

    if entry.attachment is not None:
        raw = await entry.attachment.read()
        text = raw.decode("utf-8", errors="replace")
    elif entry.embed_body:
        text = entry.embed_body
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
    """Post a journal as a bold title header + rendered embed (no attachment).

    The journal.md stays on disk (the durable source of truth); the channel
    post is display-only. Returns the Discord message id.

    No `.md` attachment and no Edit button: journals are no longer editable in
    place (only roster/scratchpad are). Re-ingestion (list_for_channel /
    fetch_content) reads the journal back from the embed body.
    """
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)

    header = format_header(title)
    # Rendered embed for in-channel readability (markdown rendered, no download).
    embed = render_journal_embed(journal_md, date=date)
    msg = await channel.send(content=header, embed=embed)
    return msg.id


async def dm_journal(
    bot: discord.Client,
    user_id: int,
    journal_md: str,
    *,
    vod_id: str,
    title: str,
    date: str,
) -> None:
    """DM the journal privately to a user (for a `silent` recap) as a code-block
    embed — no attachment.

    The code block lets the user select + copy the raw markdown and paste it
    elsewhere with formatting intact. The journal.md stays on disk. Raises if
    the user can't be resolved or DMs are closed.
    """
    user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    if user is None:
        raise RuntimeError(f"Could not resolve user {user_id} to DM the silent recap")

    header = format_header(title)
    embed = codeblock_journal_embed(journal_md, date=date)
    await user.send(
        content=f"🤫 **Silent recap** (not posted in the channel):\n{header}",
        embed=embed,
    )

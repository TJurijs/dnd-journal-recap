"""Per-channel filesystem storage.

Layout under settings.data_dir:

    channels/{channel_id}/
        meta.yaml                       # name, premise, style, guild_id, journals_synced
        journals_cache/{N:04d}.md       # mirror of journals fetched from Discord
        initialize/
            roster.md                   # written by /initialize
            scratchpad.md
        recaps/
            {timestamp}_{vod_id}/       # one folder per /recap run, e.g. 20260518-002530_1234567890
                source.mp4              # downloaded VOD (kept for audit/cache)
                audio.mp3               # converted mono 16kHz MP3
                chunks/chunk_000.mp3 …
                transcript.txt          # raw transcript
                journal.md              # the recap journal
                roster.md               # full roster snapshot after this recap
                scratchpad.md           # full scratchpad snapshot after this recap

/recap reads the *current* roster/scratchpad from the latest recap folder if
any exists, falling back to `initialize/`. It writes its outputs into a fresh
recap folder.

Writes are atomic (tempfile + os.replace). Concurrent writes to the same
channel are serialized via a per-channel asyncio.Lock.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Optional

import yaml

from recap_bot.config import settings

_channel_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _channel_root(channel_id: int) -> Path:
    return settings.data_dir / "channels" / str(channel_id)


def initialize_dir(channel_id: int) -> Path:
    """Directory where /initialize writes roster.md and scratchpad.md."""
    return _channel_root(channel_id) / "initialize"


def recaps_root(channel_id: int) -> Path:
    return _channel_root(channel_id) / "recaps"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write content to path atomically via a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def channel_lock(channel_id: int) -> asyncio.Lock:
    return _channel_locks[channel_id]


# --- Meta (name, premise, style, guild_id) ---

DEFAULT_STYLE = "chapters"


async def read_meta(channel_id: int) -> Optional[dict]:
    path = _channel_root(channel_id) / "meta.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["channel_id"] = channel_id
        return data
    except Exception:
        return None


async def write_meta(channel_id: int, **fields) -> dict:
    """Merge fields into meta.yaml. Returns the merged meta."""
    async with channel_lock(channel_id):
        current = (await read_meta(channel_id)) or {}
        current.pop("channel_id", None)  # not stored on disk; derived from path
        merged = {**current, **fields}
        path = _channel_root(channel_id) / "meta.yaml"
        _atomic_write_text(path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))
        merged["channel_id"] = channel_id
        return merged


async def get_style(channel_id: int) -> str:
    meta = await read_meta(channel_id)
    return (meta or {}).get("style") or DEFAULT_STYLE


# --- Recap directories ---

# Recap folder names look like: 0001_1234567890 (seq + vod_id).
# Seq increments on each NEW recap; re-recapping an existing VOD reuses the
# same folder (and its seq), so chronological order = first-recap order.
_RECAP_DIR_RE = re.compile(r"^(?P<seq>\d{4,})_(?P<vod_id>[\w-]+)$")


def list_recap_dirs(channel_id: int) -> list[Path]:
    """All recap directories for this channel, sorted oldest → newest by seq."""
    root = recaps_root(channel_id)
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and _RECAP_DIR_RE.match(p.name)),
        key=lambda p: p.name,
    )


def find_recap_dir_for_vod(channel_id: int, vod_id: str) -> Optional[Path]:
    """Return the existing recap folder for this VOD, or None if no recap yet."""
    for d in list_recap_dirs(channel_id):
        m = _RECAP_DIR_RE.match(d.name)
        if m and m.group("vod_id") == vod_id:
            return d
    return None


def make_or_reuse_recap_dir(channel_id: int, vod_id: str) -> Path:
    """Return the recap folder for `vod_id`. Re-recap reuses the existing folder
    (preserving its seq number); new VOD gets the next available seq."""
    existing = find_recap_dir_for_vod(channel_id, vod_id)
    if existing is not None:
        (existing / "chunks").mkdir(parents=True, exist_ok=True)
        return existing

    # New recap: find next sequence number
    next_seq = 1
    for d in list_recap_dirs(channel_id):
        m = _RECAP_DIR_RE.match(d.name)
        if m:
            try:
                next_seq = max(next_seq, int(m.group("seq")) + 1)
            except ValueError:
                pass
    path = recaps_root(channel_id) / f"{next_seq:04d}_{vod_id}"
    (path / "chunks").mkdir(parents=True, exist_ok=True)
    return path


# --- Discord message id for the recap's posted journal ---
#
# Stored as a tiny text file alongside journal.md so /recap_edit can edit the
# original Discord post in-place (swap its .md attachment) rather than letting
# the visible post drift out of sync with the on-disk journal.

_RECAP_MSG_ID_FILE = "discord_msg_id.txt"


def read_recap_message_id(recap_dir: Path) -> Optional[int]:
    """Return the Discord message id of the journal post for this recap.

    None if the file is missing (e.g. recap predates this feature) or malformed.
    """
    path = recap_dir / _RECAP_MSG_ID_FILE
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def write_recap_message_id(recap_dir: Path, message_id: int) -> None:
    """Persist the Discord message id of the journal post for this recap."""
    write_text_atomic(recap_dir / _RECAP_MSG_ID_FILE, str(message_id))


# --- Current roster/scratchpad (latest recap → initialize → legacy channel root) ---

def canonical_roster_path(channel_id: int) -> Path:
    """The one true roster.md for this channel.

    Both /initialize and /recap write here; /roster and the recap-context
    reader read from here. This is the single source of truth — older
    per-recap and initialize/ snapshots are still readable for backward
    compat but the bot stops creating new ones.
    """
    return _channel_root(channel_id) / "roster.md"


def canonical_scratchpad_path(channel_id: int) -> Path:
    return _channel_root(channel_id) / "scratchpad.md"


def _roster_source_path(channel_id: int) -> Optional[Path]:
    """Locate the roster.md to display.

    Order:
      1. Canonical channel-root roster.md — what current /initialize and
         /recap write to. This is the source of truth.
      2. Legacy: walk recap dirs newest→oldest. Older bot versions wrote
         per-recap roster snapshots; reading them keeps existing campaigns
         working without manual migration. The next /recap will materialize
         the canonical file from this content.
      3. Legacy: initialize/roster.md. Same story for the older initialize
         subdirectory layout.
    """
    canonical = canonical_roster_path(channel_id)
    if canonical.exists():
        return canonical
    for recap_dir in reversed(list_recap_dirs(channel_id)):
        p = recap_dir / "roster.md"
        if p.exists():
            return p
    init_path = initialize_dir(channel_id) / "roster.md"
    if init_path.exists():
        return init_path
    return None


def _scratchpad_source_path(channel_id: int) -> Optional[Path]:
    """Symmetric with `_roster_source_path`."""
    canonical = canonical_scratchpad_path(channel_id)
    if canonical.exists():
        return canonical
    for recap_dir in reversed(list_recap_dirs(channel_id)):
        p = recap_dir / "scratchpad.md"
        if p.exists():
            return p
    init_path = initialize_dir(channel_id) / "scratchpad.md"
    if init_path.exists():
        return init_path
    return None


async def read_roster(channel_id: int) -> Optional[str]:
    """Read the *current* roster as users see it (latest recap → initialize → legacy)."""
    path = _roster_source_path(channel_id)
    return path.read_text(encoding="utf-8") if path else None


async def read_scratchpad(channel_id: int) -> Optional[str]:
    path = _scratchpad_source_path(channel_id)
    return path.read_text(encoding="utf-8") if path else None


async def read_context_for_recap(channel_id: int) -> tuple[str, str]:
    """Roster + scratchpad to seed THIS recap with.

    Reads the canonical channel-wide roster/scratchpad. Re-recaps no longer
    chain off a per-recap snapshot — there's a single roster that
    accumulates, and the LLM's update step is designed to merge new
    information into existing entries (PREFER UPDATING OVER ADDING).
    """
    roster = await read_roster(channel_id)
    scratchpad = await read_scratchpad(channel_id)
    return roster or "", scratchpad or ""


# --- /initialize writes ---

async def write_roster(channel_id: int, text: str) -> Path:
    """Write the canonical roster.md at the channel root. Used by /initialize
    AND every /recap — there's no per-recap or per-initialize copy anymore."""
    async with channel_lock(channel_id):
        path = canonical_roster_path(channel_id)
        _atomic_write_text(path, text)
        return path


async def write_scratchpad(channel_id: int, text: str) -> Path:
    """Write the canonical scratchpad.md at the channel root."""
    async with channel_lock(channel_id):
        path = canonical_scratchpad_path(channel_id)
        _atomic_write_text(path, text)
        return path


# Back-compat aliases — older code (and tests written against the previous
# layout) called these. New callers should use write_roster / write_scratchpad.
write_initialize_roster = write_roster
write_initialize_scratchpad = write_scratchpad


# --- Generic write to any directory (used by /recap to write into recap dir) ---

def write_text_atomic(path: Path, text: str) -> None:
    _atomic_write_text(path, text)


# --- Context presence ---

async def has_context(channel_id: int) -> bool:
    """True iff we have both roster and scratchpad somewhere (initialize/, latest recap, or legacy)."""
    return _roster_source_path(channel_id) is not None and _scratchpad_source_path(channel_id) is not None


async def clear_context(channel_id: int) -> None:
    """Delete both canonical roster.md and scratchpad.md.

    Used by /initialize when it builds a fresh roster/scratchpad and wants a
    clean slate. Legacy per-recap and initialize/ snapshots are NOT touched —
    they're orphaned remnants from before the single-canonical refactor and
    not in the writer path anymore.
    """
    async with channel_lock(channel_id):
        for path in (
            canonical_roster_path(channel_id),
            canonical_scratchpad_path(channel_id),
        ):
            if path.exists():
                path.unlink()


async def clear_roster(channel_id: int) -> Optional[Path]:
    """Delete the roster.md that `/roster` currently displays.

    The displayed roster comes from `_roster_source_path`'s priority order
    (latest recap → initialize/ → legacy), so this deletes whichever of those
    is actually serving the user. Subsequent `/roster` calls fall through to
    the next file in the priority chain (next-newest recap snapshot →
    initialize/ → legacy → empty).

    Returns the deleted path, or None if there was nothing to delete.

    Note: deleting a recap-snapshot roster also affects the chain seeded into
    the *next* `/recap` on a new VOD (it'll start from an empty roster if all
    snapshots have been wiped). That's the intended behavior for an explicit
    delete — surgical, predictable, and matches what the user sees.
    """
    async with channel_lock(channel_id):
        path = _roster_source_path(channel_id)
        if path is None or not path.exists():
            return None
        path.unlink()
        return path


async def clear_scratchpad(channel_id: int) -> Optional[Path]:
    """Delete the scratchpad.md that `/scratchpad` (or `/pad`) currently displays.

    Symmetric with `clear_roster`: uses `_scratchpad_source_path`'s priority
    order so we always delete what the user actually sees. Returns the deleted
    path, or None if nothing was displayed.
    """
    async with channel_lock(channel_id):
        path = _scratchpad_source_path(channel_id)
        if path is None or not path.exists():
            return None
        path.unlink()
        return path


# --- Journal cache (mirror of Discord-as-truth) ---

def journal_cache_path(channel_id: int, index: int) -> Path:
    return _channel_root(channel_id) / "journals_cache" / f"{index:04d}.md"


async def write_journal_cache(channel_id: int, index: int, journal_md: str) -> Path:
    async with channel_lock(channel_id):
        path = journal_cache_path(channel_id, index)
        _atomic_write_text(path, journal_md)
        return path


async def read_journal_cache(channel_id: int, index: int) -> Optional[str]:
    path = journal_cache_path(channel_id, index)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")

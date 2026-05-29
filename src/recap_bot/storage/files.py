"""Per-CATEGORY filesystem storage.

Data is scoped to a Discord **category** (not a single channel), so every
channel inside a category shares one roster, one scratchpad, and one recap
history. `/initialize` (run in the journal channel) and `/recap` (run in any
channel of the category) both read/write the same category-keyed store; the
recap journal is posted to whichever channel `/recap` was invoked in.

Layout under settings.data_dir:

    categories/{category_id}/
        meta.yaml                       # name, premise, style, guild_id, journals_synced
        roster.md                       # canonical, shared across the category
        scratchpad.md                   # canonical, shared across the category
        journals_cache/{N:04d}.md       # mirror of journals fetched from Discord
        recaps/
            {seq}_{vod_id}/             # one folder per /recap run, e.g. 0001_1234567890
                source.*                # downloaded VOD audio (cache)
                audio.mp3               # converted mono 16kHz MP3
                chunks/chunk_000.mp3 …
                transcript.txt          # raw transcript
                journal.md              # the recap journal (per-recap)
                discord_msg_id.txt      # id of the posted Discord message

roster.md / scratchpad.md are single canonical files per category. Older
layouts (per-recap snapshots, an initialize/ subdir, channel-root files) are
still readable as fallbacks for migrated data.

Writes are atomic (tempfile + os.replace). Concurrent writes to the same
category are serialized via a per-category asyncio.Lock.
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

_category_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _category_root(category_id: int) -> Path:
    return settings.data_dir / "categories" / str(category_id)


def initialize_dir(category_id: int) -> Path:
    """Legacy initialize/ subdir (older layout). Read-only fallback now."""
    return _category_root(category_id) / "initialize"


def recaps_root(category_id: int) -> Path:
    return _category_root(category_id) / "recaps"


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


def category_lock(category_id: int) -> asyncio.Lock:
    return _category_locks[category_id]


# --- Meta (name, premise, style, guild_id, journals_synced) ---

DEFAULT_STYLE = "chapters"


async def read_meta(category_id: int) -> Optional[dict]:
    path = _category_root(category_id) / "meta.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        data["category_id"] = category_id
        return data
    except Exception:
        return None


async def write_meta(category_id: int, **fields) -> dict:
    """Merge fields into meta.yaml. Returns the merged meta."""
    async with category_lock(category_id):
        current = (await read_meta(category_id)) or {}
        current.pop("category_id", None)  # not stored on disk; derived from path
        merged = {**current, **fields}
        path = _category_root(category_id) / "meta.yaml"
        _atomic_write_text(path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True))
        merged["category_id"] = category_id
        return merged


async def get_style(category_id: int) -> str:
    meta = await read_meta(category_id)
    return (meta or {}).get("style") or DEFAULT_STYLE


# --- Recap directories ---

# Recap folder names look like: 0001_1234567890 (seq + vod_id).
# Seq increments on each NEW recap; re-recapping an existing VOD reuses the
# same folder (and its seq), so chronological order = first-recap order.
_RECAP_DIR_RE = re.compile(r"^(?P<seq>\d{4,})_(?P<vod_id>[\w-]+)$")


def list_recap_dirs(category_id: int) -> list[Path]:
    """All recap directories for this category, sorted oldest → newest by seq."""
    root = recaps_root(category_id)
    if not root.exists():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and _RECAP_DIR_RE.match(p.name)),
        key=lambda p: p.name,
    )


def find_recap_dir_for_vod(category_id: int, vod_id: str) -> Optional[Path]:
    """Return the existing recap folder for this VOD, or None if no recap yet."""
    for d in list_recap_dirs(category_id):
        m = _RECAP_DIR_RE.match(d.name)
        if m and m.group("vod_id") == vod_id:
            return d
    return None


def make_or_reuse_recap_dir(category_id: int, vod_id: str) -> Path:
    """Return the recap folder for `vod_id`. Re-recap reuses the existing folder
    (preserving its seq number); new VOD gets the next available seq."""
    existing = find_recap_dir_for_vod(category_id, vod_id)
    if existing is not None:
        (existing / "chunks").mkdir(parents=True, exist_ok=True)
        return existing

    # New recap: find next sequence number
    next_seq = 1
    for d in list_recap_dirs(category_id):
        m = _RECAP_DIR_RE.match(d.name)
        if m:
            try:
                next_seq = max(next_seq, int(m.group("seq")) + 1)
            except ValueError:
                pass
    path = recaps_root(category_id) / f"{next_seq:04d}_{vod_id}"
    (path / "chunks").mkdir(parents=True, exist_ok=True)
    return path


# --- Discord message id for the recap's posted journal ---
#
# Stored as a tiny text file alongside journal.md. Generic helper retained for
# possible future use; recap posts are embed-only and no longer edited in place,
# so the orchestrator no longer writes this during a normal recap.

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


# --- Current roster/scratchpad (canonical → recap snapshot → initialize/) ---

def canonical_roster_path(category_id: int) -> Path:
    """The one true roster.md for this category.

    Both /initialize and /recap write here; /roster and the recap-context
    reader read from here. Older per-recap and initialize/ snapshots are still
    readable for backward compat but the bot stops creating new ones.
    """
    return _category_root(category_id) / "roster.md"


def canonical_scratchpad_path(category_id: int) -> Path:
    return _category_root(category_id) / "scratchpad.md"


def _roster_source_path(category_id: int) -> Optional[Path]:
    """Locate the roster.md to display.

    Order:
      1. Canonical category-root roster.md — what current /initialize and
         /recap write to. The source of truth.
      2. Legacy: walk recap dirs newest→oldest for a roster.md snapshot.
      3. Legacy: initialize/roster.md.
    """
    canonical = canonical_roster_path(category_id)
    if canonical.exists():
        return canonical
    for recap_dir in reversed(list_recap_dirs(category_id)):
        p = recap_dir / "roster.md"
        if p.exists():
            return p
    init_path = initialize_dir(category_id) / "roster.md"
    if init_path.exists():
        return init_path
    return None


def _scratchpad_source_path(category_id: int) -> Optional[Path]:
    """Symmetric with `_roster_source_path`."""
    canonical = canonical_scratchpad_path(category_id)
    if canonical.exists():
        return canonical
    for recap_dir in reversed(list_recap_dirs(category_id)):
        p = recap_dir / "scratchpad.md"
        if p.exists():
            return p
    init_path = initialize_dir(category_id) / "scratchpad.md"
    if init_path.exists():
        return init_path
    return None


async def read_roster(category_id: int) -> Optional[str]:
    """Read the *current* roster as users see it (canonical → snapshot → legacy)."""
    path = _roster_source_path(category_id)
    return path.read_text(encoding="utf-8") if path else None


async def read_scratchpad(category_id: int) -> Optional[str]:
    path = _scratchpad_source_path(category_id)
    return path.read_text(encoding="utf-8") if path else None


async def read_context_for_recap(category_id: int) -> tuple[str, str]:
    """Roster + scratchpad to seed THIS recap with (empty strings if none yet)."""
    roster = await read_roster(category_id)
    scratchpad = await read_scratchpad(category_id)
    return roster or "", scratchpad or ""


# --- Canonical writes ---

async def write_roster(category_id: int, text: str) -> Path:
    """Write the canonical roster.md at the category root (used by /initialize
    AND every /recap — one shared copy per category)."""
    async with category_lock(category_id):
        path = canonical_roster_path(category_id)
        _atomic_write_text(path, text)
        return path


async def write_scratchpad(category_id: int, text: str) -> Path:
    """Write the canonical scratchpad.md at the category root."""
    async with category_lock(category_id):
        path = canonical_scratchpad_path(category_id)
        _atomic_write_text(path, text)
        return path


# Back-compat aliases (older call sites / tests).
write_initialize_roster = write_roster
write_initialize_scratchpad = write_scratchpad


# --- Generic write to any directory (used by /recap to write into recap dir) ---

def write_text_atomic(path: Path, text: str) -> None:
    _atomic_write_text(path, text)


# --- Context presence ---

async def has_context(category_id: int) -> bool:
    """True iff we have both roster and scratchpad somewhere for this category."""
    return _roster_source_path(category_id) is not None and _scratchpad_source_path(category_id) is not None


async def clear_context(category_id: int) -> None:
    """Delete both canonical roster.md and scratchpad.md for this category."""
    async with category_lock(category_id):
        for path in (
            canonical_roster_path(category_id),
            canonical_scratchpad_path(category_id),
        ):
            if path.exists():
                path.unlink()


async def clear_roster(category_id: int) -> Optional[Path]:
    """Delete the roster.md that `/roster` currently displays. Returns the
    deleted path, or None if there was nothing to delete. Repeated calls peel
    back through the priority chain (canonical → snapshot → initialize/)."""
    async with category_lock(category_id):
        path = _roster_source_path(category_id)
        if path is None or not path.exists():
            return None
        path.unlink()
        return path


async def clear_scratchpad(category_id: int) -> Optional[Path]:
    """Delete the scratchpad.md that `/scratchpad` (or `/pad`) currently displays."""
    async with category_lock(category_id):
        path = _scratchpad_source_path(category_id)
        if path is None or not path.exists():
            return None
        path.unlink()
        return path


# --- Journal cache (mirror of Discord-as-truth) ---

def journal_cache_path(category_id: int, index: int) -> Path:
    return _category_root(category_id) / "journals_cache" / f"{index:04d}.md"


async def write_journal_cache(category_id: int, index: int, journal_md: str) -> Path:
    async with category_lock(category_id):
        path = journal_cache_path(category_id, index)
        _atomic_write_text(path, journal_md)
        return path


async def read_journal_cache(category_id: int, index: int) -> Optional[str]:
    path = journal_cache_path(category_id, index)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")

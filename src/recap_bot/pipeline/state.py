"""In-memory active-job state, replacing the old `jobs` SQLite table.

The bot is single-process, single-worker (one job at a time via JobQueue).
At most one active job per channel, so channel_id is the key. On bot restart
all in-flight jobs are lost — same effective behavior as the old
`mark_stalled_jobs_failed()`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class ActiveJob:
    channel_id: int
    guild_id: int
    requested_by: int
    source_type: str
    source_ref: str
    style: str
    status: str = "queued"             # queued | running | done | failed | cancelled
    progress_note: str = ""
    error: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    cancelled: bool = False
    vod_id: str = ""                   # Twitch VOD id, parsed from source_ref
    title: str = ""                    # VOD title, for status display
    channel_label: str = ""            # "Category / channel-name" — captured at invocation
    force: bool = False                # wipe cached audio/chunks before running


_active: dict[int, ActiveJob] = {}


def claim(
    channel_id: int,
    *,
    guild_id: int,
    requested_by: int,
    source_type: str,
    source_ref: str,
    style: str,
    channel_label: str = "",
    force: bool = False,
) -> Optional[ActiveJob]:
    """Atomically reserve the channel for a new job. Returns None if a job already exists."""
    if channel_id in _active:
        return None
    job = ActiveJob(
        channel_id=channel_id,
        guild_id=guild_id,
        requested_by=requested_by,
        source_type=source_type,
        source_ref=source_ref,
        style=style,
        channel_label=channel_label,
        force=force,
    )
    _active[channel_id] = job
    return job


def get(channel_id: int) -> Optional[ActiveJob]:
    return _active.get(channel_id)


def release(channel_id: int) -> None:
    _active.pop(channel_id, None)


def cancel(channel_id: int) -> bool:
    job = _active.get(channel_id)
    if job is None:
        return False
    job.cancelled = True
    return True


def all_active() -> list[ActiveJob]:
    return list(_active.values())

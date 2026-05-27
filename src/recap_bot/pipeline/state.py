"""In-memory active-job state.

Keyed by CATEGORY id (one active recap per category — channels in a category
share a roster/scratchpad, so concurrent recaps would race). The job also
carries `channel_id`, the channel /recap was invoked in, which is where the
recap journal gets posted. On bot restart all in-flight jobs are lost.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ActiveJob:
    category_id: int                   # storage + state key (the campaign's category)
    channel_id: int                    # POST target: the channel /recap was invoked in
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
    vod_id: str = ""                   # VOD id (Twitch numeric or YouTube alphanum), parsed from source_ref
    title: str = ""                    # VOD title, for status display
    channel_label: str = ""            # "Category / channel-name" of the post channel
    force: bool = False                # wipe cached audio/chunks before running
    profile: str = "default"           # models.yaml profile this job uses
    silent: bool = False               # deliver journal via DM to requester, don't post in channel


_active: dict[int, ActiveJob] = {}   # keyed by category_id


def claim(
    category_id: int,
    *,
    channel_id: int,
    guild_id: int,
    requested_by: int,
    source_type: str,
    source_ref: str,
    style: str,
    channel_label: str = "",
    force: bool = False,
    profile: str = "default",
    silent: bool = False,
) -> Optional[ActiveJob]:
    """Atomically reserve the CATEGORY for a new job. Returns None if a job
    already exists for this category."""
    if category_id in _active:
        return None
    job = ActiveJob(
        category_id=category_id,
        channel_id=channel_id,
        guild_id=guild_id,
        requested_by=requested_by,
        source_type=source_type,
        source_ref=source_ref,
        style=style,
        channel_label=channel_label,
        force=force,
        profile=profile,
        silent=silent,
    )
    _active[category_id] = job
    return job


def get(category_id: int) -> Optional[ActiveJob]:
    return _active.get(category_id)


def release(category_id: int) -> None:
    _active.pop(category_id, None)


def cancel(category_id: int) -> bool:
    job = _active.get(category_id)
    if job is None:
        return False
    job.cancelled = True
    return True


def all_active() -> list[ActiveJob]:
    return list(_active.values())

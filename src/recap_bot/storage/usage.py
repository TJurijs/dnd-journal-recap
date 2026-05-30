"""Append-only usage log for the bot owner's /admin log view.

One JSON object per line in data/usage_log.jsonl, recording each cost-bearing
operation (recap / initialize): when, which server, which category+channel,
which command, which user, which model profile, and what it cost.

Logging is best-effort — a failure here must never break the pipeline.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from recap_bot.config import settings

logger = logging.getLogger(__name__)

_LOG_FILENAME = "usage_log.jsonl"


def _log_path() -> Path:
    return settings.data_dir / _LOG_FILENAME


def log_path() -> Path:
    """Public path to the usage log (for /admin settings display)."""
    return _log_path()


def event_count() -> int:
    """Total events recorded so far (-1 if the file can't be read)."""
    path = _log_path()
    if not path.exists():
        return 0
    try:
        with path.open(encoding="utf-8") as fh:
            return sum(1 for line in fh if line.strip())
    except Exception:
        return -1


def log_event(
    *,
    event: str,                 # "recap" | "initialize"
    status: str,                # "done" | "failed" | "cancelled"
    guild_id: Optional[int] = None,
    guild_name: str = "",
    category_id: Optional[int] = None,
    location: str = "",         # "Category / channel-name"
    user_id: Optional[int] = None,
    user_name: str = "",
    profile: str = "",
    vod_id: str = "",
    vod_title: str = "",        # VOD title (the video name)
    source_url: str = "",       # the URL the user pasted (Twitch/YouTube link)
    cost_usd: float = 0.0,
) -> None:
    """Append one usage event. Never raises."""
    try:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            "status": status,
            "guild_id": guild_id,
            "guild_name": guild_name,
            "category_id": category_id,
            "location": location,
            "user_id": user_id,
            "user_name": user_name,
            "profile": profile,
            "vod_id": vod_id,
            "vod_title": vod_title,
            "source_url": source_url,
            "cost_usd": round(float(cost_usd), 6),
        }
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("Failed to write usage event")


def read_recent(limit: int = 15) -> list[dict]:
    """Return up to `limit` most-recent events, newest first."""
    path = _log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        logger.exception("Failed to read usage log")
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out

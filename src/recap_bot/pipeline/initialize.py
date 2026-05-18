"""Bootstrap a campaign's roster and scratchpad from the channel's journal history.

The journals live in the Discord channel (source of truth). We scan the
channel once, fetch each journal's content, then run TWO LLM calls in parallel
— one per artifact — each with all journals in context. The strong model can
then dedupe spelling variants, fix Player/NPC tags, and find characters a
weaker batch extractor would miss.
"""

import asyncio
import logging
import time

import discord

from recap_bot.config import model_config
from recap_bot.pipeline.context import (
    build_roster_from_journals,
    build_scratchpad_from_journals,
)
from recap_bot.pipeline.cost import CostTracker, UsageInfo
from recap_bot.pipeline.step_log import StepLog
from recap_bot.storage import discord_journals, files as channel_files

logger = logging.getLogger(__name__)

# Channels currently being initialized. Used as a soft lock so /recap and a
# second /initialize fail fast instead of racing.
_initializing_channels: set[int] = set()
# Channels where the user clicked Cancel mid-initialization. Read at
# checkpoints; once seen the entry is consumed.
_init_cancelled: set[int] = set()


def is_initializing(channel_id: int) -> bool:
    return channel_id in _initializing_channels


def cancel_initialization(channel_id: int) -> bool:
    """Request cancellation of a running /initialize. Returns True if there
    was something to cancel. Caveat: an in-flight LLM call cannot be aborted
    mid-request — the bot's task is cancelled but the API call still completes
    server-side (you'll still be billed)."""
    if channel_id in _initializing_channels:
        _init_cancelled.add(channel_id)
        return True
    return False


def _check_init_cancelled(channel_id: int) -> None:
    if channel_id in _init_cancelled:
        _init_cancelled.discard(channel_id)
        raise asyncio.CancelledError(f"/initialize on channel {channel_id} cancelled by user")


class InitResult:
    def __init__(self, roster_chars: int, scratchpad_chars: int, journal_count: int, cost: str):
        self.roster_chars = roster_chars
        self.scratchpad_chars = scratchpad_chars
        self.journal_count = journal_count
        self.cost = cost


async def run_initialization(
    bot: discord.Client,
    progress_msg: discord.WebhookMessage | discord.Message,
    channel_id: int,
    guild_id: int,
    *,
    channel_label: str = "",
) -> InitResult:
    """Build roster + scratchpad from existing Discord journals. Edits `progress_msg` in place.

    Caller is responsible for the `_initializing_channels` lock — acquire before
    calling, release in finally.
    """
    cost = CostTracker()
    step_log = StepLog(context=f"init#{channel_id}", cost_tracker=cost)
    header = f"**{channel_label}**\n\n" if channel_label else ""

    # Ensure meta.yaml exists with guild_id (so /recap can find it)
    await channel_files.write_meta(channel_id, guild_id=guild_id)

    _check_init_cancelled(channel_id)

    # Discover journals in Discord channel history
    entries = await discord_journals.list_for_channel(bot, channel_id)
    step_log.step("scan", tool="discord", progress="done", note=f"{len(entries)} journal(s)")
    _check_init_cancelled(channel_id)

    if not entries:
        await channel_files.write_initialize_roster(channel_id, "")
        await channel_files.write_initialize_scratchpad(channel_id, "")
        await channel_files.write_meta(channel_id, journals_synced=0)
        step_log.total(status="empty")
        try:
            await progress_msg.edit(content=f"{header}📭 No prior journals found.\n\n✅ Job completed")
        except Exception:
            logger.exception("Failed to edit init progress message")
        return InitResult(0, 0, 0, cost.format_total())

    # Fetch journal bodies (cached on disk)
    await progress_msg.edit(content=f"{header}📥 Fetching {len(entries)} journal(s) from channel history...")
    journals_md: list[str] = []
    for i, entry in enumerate(entries, 1):
        _check_init_cancelled(channel_id)
        try:
            text = await discord_journals.fetch_content(channel_id, entry)
        except Exception:
            logger.exception("Failed to fetch journal #%s in channel %s", entry.session, channel_id)
            continue
        if text:
            journals_md.append(text)
        if i % 5 == 0 or i == len(entries):
            await progress_msg.edit(content=f"{header}📥 Fetched {i}/{len(entries)} journal(s)...")
    step_log.step("fetch", tool="discord", progress="done", note=f"{len(journals_md)} body/ies loaded")
    _check_init_cancelled(channel_id)

    # Per-step state for the live DM. With single-call builds there's no
    # granular per-batch progress, so we show elapsed time instead and flip the
    # icon to ✅ when the call returns.
    progress_state: dict[str, dict] = {
        "roster":     {"status": "running", "started": time.monotonic(), "elapsed": 0.0, "cost": UsageInfo()},
        "scratchpad": {"status": "running", "started": time.monotonic(), "elapsed": 0.0, "cost": UsageInfo()},
    }

    def _step_line(label: str, model_key: str, key: str) -> str:
        s = progress_state[key]
        if s["status"] == "running":
            icon = "⏳"
            elapsed = time.monotonic() - s["started"]
            extra = f"running… ({elapsed:.0f}s elapsed)"
        elif s["status"] == "done":
            icon = "✅"
            extra = f"done ({s['elapsed']:.0f}s) — {s['cost'].format_cost() if s['cost'].total_tokens else '—'}"
        elif s["status"] == "failed":
            icon = "⚠️"
            extra = f"failed after {s['elapsed']:.0f}s"
        else:
            icon = "⬜"
            extra = "pending"
        return f"{icon} {label} ({model_config.get(model_key)}): {extra}"

    async def _render(footer: str = ""):
        text = (
            f"{header}🛠️ **Initializing from {len(journals_md)} journal(s)**\n\n"
            f"{_step_line('📚 Roster', 'roster_build', 'roster')}\n"
            f"{_step_line('📖 Scratchpad', 'scratchpad_build', 'scratchpad')}\n\n"
            f"💰 Total: {cost.format_total()}"
        )
        if footer:
            text += f"\n\n{footer}"
        try:
            await progress_msg.edit(content=text)
        except Exception:
            logger.exception("Failed to edit init progress message")

    await _render()

    # Build tasks are created below; the heartbeat polls them so it can
    # cancel them if the user clicked Cancel on /jobs.
    build_tasks: list[asyncio.Task] = []

    # Heartbeat task: re-render every 2s so the elapsed-time counter updates
    # AND poll the cancellation flag so we can abort the in-flight build tasks.
    async def _heartbeat():
        try:
            while True:
                await asyncio.sleep(2)
                if channel_id in _init_cancelled:
                    for t in build_tasks:
                        if not t.done():
                            t.cancel()
                    # leave _init_cancelled set so the next checkpoint raises
                    return
                await _render()
        except asyncio.CancelledError:
            pass

    heartbeat = asyncio.create_task(_heartbeat())

    async def _build_roster():
        start = time.monotonic()
        try:
            text, usage = await build_roster_from_journals(journals_md)
            progress_state["roster"]["status"] = "done"
            progress_state["roster"]["elapsed"] = time.monotonic() - start
            if usage:
                progress_state["roster"]["cost"] = progress_state["roster"]["cost"] + usage
            step_log.step("roster_build", model=model_config.get("roster_build"),
                          progress="done", usage=usage, note=f"{len(text)} chars")
            return text
        except Exception:
            logger.exception("Roster build failed")
            progress_state["roster"]["status"] = "failed"
            progress_state["roster"]["elapsed"] = time.monotonic() - start
            step_log.step("roster_build", model=model_config.get("roster_build"),
                          progress="failed")
            return ""

    async def _build_scratchpad():
        start = time.monotonic()
        try:
            text, usage = await build_scratchpad_from_journals(journals_md)
            progress_state["scratchpad"]["status"] = "done"
            progress_state["scratchpad"]["elapsed"] = time.monotonic() - start
            if usage:
                progress_state["scratchpad"]["cost"] = progress_state["scratchpad"]["cost"] + usage
            step_log.step("scratchpad_build", model=model_config.get("scratchpad_build"),
                          progress="done", usage=usage, note=f"{len(text)} chars")
            return text
        except Exception:
            logger.exception("Scratchpad build failed")
            progress_state["scratchpad"]["status"] = "failed"
            progress_state["scratchpad"]["elapsed"] = time.monotonic() - start
            step_log.step("scratchpad_build", model=model_config.get("scratchpad_build"),
                          progress="failed")
            return ""

    roster_task = asyncio.create_task(_build_roster())
    scratchpad_task = asyncio.create_task(_build_scratchpad())
    build_tasks.extend([roster_task, scratchpad_task])

    try:
        roster_text, scratchpad_text = await asyncio.gather(roster_task, scratchpad_task)
    except asyncio.CancelledError:
        # Drain both tasks before propagating. The underlying API call may
        # keep running server-side until it returns — we just stop waiting.
        for t in (roster_task, scratchpad_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(roster_task, scratchpad_task, return_exceptions=True)
        _init_cancelled.discard(channel_id)
        progress_state["roster"]["status"] = "failed"
        progress_state["scratchpad"]["status"] = "failed"
        heartbeat.cancel()
        await _render(footer="⏹️ Cancelled by user — in-flight API call may still complete server-side and be billed")
        raise
    finally:
        heartbeat.cancel()

    await channel_files.write_initialize_roster(channel_id, roster_text)
    await channel_files.write_initialize_scratchpad(channel_id, scratchpad_text)
    await channel_files.write_meta(channel_id, journals_synced=len(entries))
    step_log.total(status="done")

    await _render(footer="✅ Job completed")

    return InitResult(
        roster_chars=len(roster_text),
        scratchpad_chars=len(scratchpad_text),
        journal_count=len(journals_md),
        cost=cost.format_total(),
    )

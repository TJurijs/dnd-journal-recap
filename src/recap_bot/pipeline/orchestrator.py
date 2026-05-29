"""Recap pipeline: download VOD → transcribe → summarize → update context → post.

Keyed by category_id (one active job per channel). State lives in
`pipeline.state` (in-memory). Persistent data lives in `storage.files`
(roster/scratchpad/transcripts/journals cache) and Discord channel history
(authoritative journals).
"""

import asyncio
import logging
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

import discord

from recap_bot.config import settings, model_config
from recap_bot.pipeline import state
from recap_bot.pipeline.audio import extract_audio
from recap_bot.pipeline.chunk_audio import audio_duration, chunk_audio
from recap_bot.pipeline.context import update_roster, update_scratchpad
from recap_bot.pipeline.cost import CostTracker, UsageInfo
from recap_bot.pipeline.download import download_vod, get_vod_info
from recap_bot.pipeline.initialize import is_initializing
from recap_bot.pipeline.step_log import StepLog
from recap_bot.pipeline.summarize import summarize_session
from recap_bot.pipeline.transcribe import transcribe_chunk
from recap_bot.storage import discord_journals, files as channel_files
# Aliased: `usage` is used as a local UsageInfo variable inside run_job, which
# would shadow the module import and break usage_log.log_event() in the finally.
from recap_bot.storage import usage as usage_log

logger = logging.getLogger(__name__)

# category_id -> discord.Message (live DM status message)
_status_msgs: dict[int, discord.Message] = {}

# category_id -> dict of per-step UI status
_step_ui: dict[int, dict] = {}

# Lock for Discord status message edits
_status_lock = asyncio.Lock()

# category_id -> monotonic timestamp of last DM edit (for throttling).
# Discord rate-limits to ~5 edits/5s; we cap to one every 2s and let the
# next progress event coalesce the latest UI state.
_last_status_edit: dict[int, float] = {}
_STATUS_EDIT_INTERVAL = 2.0  # seconds between non-forced edits

# category_id -> fingerprint of step UI states at last render. Used to bypass
# the throttle when *any* step status changes (e.g. summarize transitions
# pending→current→done) so the user actually sees the ⏳ between steps.
_last_status_fingerprint: dict[int, str] = {}


def _ui_fingerprint(category_id: int) -> str:
    ui = _step_ui.get(category_id, {})
    return "|".join(f"{k}:{e.get('status', '?')}" for k, e in ui.items())

# Matches the "## Session Date: YYYY-MM-DD" line in a generated journal
_INGAME_DATE_RE = re.compile(r"##\s*Session Date:\s*(.+?)(?:\n|$)", re.IGNORECASE)

STEPS = [
    ("download", "⬇️ Download VOD"),
    ("extract", "🎙️ Convert to MP3 (mono 16kHz)"),
    ("cleanup", "🗑️ Delete VOD source"),
    ("chunk", "✂️ Split MP3 into 20 chunks"),
    ("transcribe", "📝 Transcribe (20 parallel chunks)"),
    ("summarize", "🧠 Summarize into journal"),
    ("update_roster", "👥 Update roster"),
    ("update_scratchpad", "📋 Update scratchpad"),
    ("post", "📤 Post journal to channel"),
]


def _init_ui(category_id: int) -> None:
    _step_ui[category_id] = {
        key: {"status": "pending", "note": "", "pct": 0, "tool": "", "cost": UsageInfo()}
        for key, _ in STEPS
    }


def _mark_ui(
    category_id: int, key: str, status: str,
    note: str = "", pct: int = 0,
    tool: str = "", cost_delta: UsageInfo | None = None,
) -> None:
    log = _step_ui.get(category_id)
    if log is None:
        return
    entry = log.setdefault(key, {"status": "pending", "note": "", "pct": 0, "tool": "", "cost": UsageInfo()})
    entry["status"] = status
    entry["note"] = note
    entry["pct"] = pct
    if tool:
        entry["tool"] = tool
    if cost_delta:
        entry["cost"] = entry["cost"] + cost_delta


def _build_status_text(category_id: int, total_cost: str = "", header: str = "") -> str:
    job = state.get(category_id)
    title = (job.title if job else "") or f"Recap (channel {category_id})"
    lines = []
    if header:
        lines.append(header)
        lines.append("")
    lines.append(f"**{title}**")
    if job and job.channel_label:
        lines.append(f"_Source: {job.channel_label}_")
    if job:
        lines.append(f"_Profile: {job.profile}_")
    lines.append("")
    for key, label in STEPS:
        entry = _step_ui.get(category_id, {}).get(key, {"status": "pending", "note": "", "pct": 0, "tool": "", "cost": UsageInfo()})
        if entry["status"] == "done":
            icon = "✅"
            pct_text = ""
        elif entry["status"] == "current":
            icon = "⏳"
            pct = entry.get("pct", 0)
            pct_text = f" ({pct}%)" if pct > 0 else ""
        elif entry["status"] == "skipped":
            icon = "⏭️"
            pct_text = ""
        else:
            icon = "⬜"
            pct_text = ""
        tool = entry.get("tool", "")
        tool_str = f" `{tool}`" if tool else ""
        note = f" — {entry['note']}" if entry["note"] else ""
        cost_obj = entry.get("cost", UsageInfo())
        cost_str = f" — {cost_obj.format_cost()}" if cost_obj.total_tokens else ""
        lines.append(f"{icon} {label}{tool_str}{pct_text}{note}{cost_str}")
    if total_cost:
        lines.append("")
        lines.append(f"💰 Total: {total_cost}")
    return "\n".join(lines)


async def _send_status(
    bot, user_id: int, category_id: int,
    total_cost: str = "", header: str = "", *, force: bool = False,
) -> None:
    """Push the current step UI to the user's DM.

    Always renders when a step's *status* changes (so the user sees ⏳
    transitions). Throttled to once per 2s for within-step progress ticks so
    we don't trip Discord's edit-rate limit.
    """
    fingerprint = _ui_fingerprint(category_id)
    state_changed = fingerprint != _last_status_fingerprint.get(category_id)

    if not force and not state_changed:
        now = time.monotonic()
        last = _last_status_edit.get(category_id, 0.0)
        if now - last < _STATUS_EDIT_INTERVAL:
            return
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        if not user:
            return
        text = _build_status_text(category_id, total_cost=total_cost, header=header)
        async with _status_lock:
            msg = _status_msgs.get(category_id)
            if msg:
                try:
                    await msg.edit(content=text)
                    _last_status_edit[category_id] = time.monotonic()
                    _last_status_fingerprint[category_id] = fingerprint
                    return
                except Exception:
                    pass
            msg = await user.send(text)
            _status_msgs[category_id] = msg
            _last_status_edit[category_id] = time.monotonic()
            _last_status_fingerprint[category_id] = fingerprint
    except Exception:
        logger.exception("Failed to send status DM for channel %s", category_id)


def _check_cancelled(category_id: int) -> None:
    job = state.get(category_id)
    if job is not None and job.cancelled:
        raise asyncio.CancelledError(f"Recap on channel {category_id} stopped by user")


def _extract_ingame_date(journal_md: str) -> str | None:
    match = _INGAME_DATE_RE.search(journal_md)
    return match.group(1).strip() if match else None


_ROSTER_NAME_RE = re.compile(r"^-\s+(.+?)\s*\((Player|NPC)\)", re.MULTILINE)


def _roster_names(roster: str) -> dict[str, str]:
    """Map lowercase name → original line, for diffing."""
    out: dict[str, str] = {}
    for line in roster.splitlines():
        m = _ROSTER_NAME_RE.match(line.strip())
        if m:
            out[m.group(1).strip().lower()] = line.strip()
    return out


def _summarize_context_diff(
    *, old_roster: str, new_roster: str, old_scratchpad: str, new_scratchpad: str,
) -> str:
    """Human-readable summary of what changed in roster + scratchpad.

    For roster: new character lines that didn't exist before (by name).
    For scratchpad: the appended portion (whatever's in new but not in old).
    """
    out_lines: list[str] = []

    old_names = _roster_names(old_roster)
    new_names = _roster_names(new_roster)
    added = [new_names[k] for k in new_names if k not in old_names]
    removed = [old_names[k] for k in old_names if k not in new_names]

    if added:
        out_lines.append(f"**Roster additions ({len(added)}):**")
        for entry in added[:20]:
            out_lines.append(entry)
        if len(added) > 20:
            out_lines.append(f"_…and {len(added) - 20} more_")
    if removed:
        out_lines.append(f"**Roster removed ({len(removed)}):**")
        for entry in removed[:10]:
            out_lines.append(entry)

    # Scratchpad: assume the new content is appended after the old.
    if new_scratchpad.startswith(old_scratchpad):
        appended = new_scratchpad[len(old_scratchpad):].strip()
    else:
        # The model rewrote rather than appended; show the last 1-2 non-empty
        # lines as the "new session" entry approximation.
        new_lines = [ln for ln in new_scratchpad.splitlines() if ln.strip()]
        appended = "\n".join(new_lines[-2:]) if new_lines else ""

    if appended:
        out_lines.append("**Scratchpad addition:**")
        out_lines.append(appended[:600])

    if not out_lines:
        return "_(no roster/scratchpad changes detected)_"
    return "\n".join(out_lines)


async def run_job(bot, category_id: int) -> None:
    """Run the recap pipeline for the active job on `category_id`."""
    job = state.get(category_id)
    if job is None:
        logger.error("No active job for channel %s", category_id)
        return

    job.status = "running"
    job.started_at = datetime.utcnow()

    user_id = job.requested_by
    style = job.style
    profile = job.profile  # which models.yaml profile this job uses

    # Probe VOD title and duration
    vod_duration = 0
    try:
        info = await get_vod_info(job.source_ref)
        job.title = info.get("title") or f"Recap (channel {category_id})"
        vod_duration = info.get("duration", 0)
    except Exception:
        logger.exception("Failed to probe VOD info for %s", job.source_ref)
        job.title = f"Recap (channel {category_id})"

    # Parse the Twitch VOD ID — used as the canonical reference for this recap.
    from recap_bot.pipeline.download import get_vod_id
    vod_id = get_vod_id(job.source_ref)
    job.vod_id = vod_id

    # One folder per VOD: new recap gets next seq, re-recap reuses the same
    # folder (overwriting outputs but reusing audio/chunks unless --force).
    recap_dir = channel_files.make_or_reuse_recap_dir(category_id, vod_id)
    chunk_cache_dir = recap_dir / "chunks"
    cached_audio = recap_dir / "audio.mp3"

    cost_tracker = CostTracker()
    step_log = StepLog(context=f"recap#{category_id}-vod{vod_id}", cost_tracker=cost_tracker)
    _init_ui(category_id)

    try:
        _check_cancelled(category_id)

        # --- Precondition: don't run while /initialize is rebuilding context ---
        if is_initializing(category_id):
            raise RuntimeError("This category is currently being initialized. Try again when /initialize finishes.")

        # Read the category's current roster + scratchpad to seed this recap.
        # If the category has none yet (no /initialize run), these come back as
        # empty strings and the LLM builds the first roster/scratchpad from this
        # recap's content.
        roster_text, scratchpad_text = await channel_files.read_context_for_recap(category_id)

        _check_cancelled(category_id)

        # --force: wipe any cached audio/chunks in this folder so we re-download.
        if job.force:
            if cached_audio.exists():
                cached_audio.unlink()
            if chunk_cache_dir.exists():
                shutil.rmtree(chunk_cache_dir, ignore_errors=True)
            step_log.step("cache_wipe", tool="fs", progress="done", note=f"vod={vod_id}")

        # chunk_duration_sec: per-chunk length in seconds. Used to compute
        # absolute timestamp ranges for any chunk that fails transcription.
        # Populated by either the cache path (ffprobe audio.mp3) or the
        # fresh chunking step (returned by chunk_audio).
        chunk_duration_sec: float = 0.0

        skip_pipeline = False
        if chunk_cache_dir.exists():
            cached_chunks = sorted(chunk_cache_dir.glob("chunk_*.mp3"))
            if len(cached_chunks) == 20:
                step_log.step("chunk", tool="cache", progress="skipped", note=f"{len(cached_chunks)} cached chunks vod={vod_id}")
                for k in ("download", "extract", "cleanup"):
                    _mark_ui(category_id, k, "skipped", "cached", tool="cache")
                _mark_ui(category_id, "chunk", "skipped", "20 chunks cached", tool="cache")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
                chunk_paths = cached_chunks
                # Cached path: ffprobe audio.mp3 to recover chunk_duration so
                # failure timestamps still work on re-runs.
                if cached_audio.exists():
                    try:
                        chunk_duration_sec = (await audio_duration(cached_audio)) / 20
                    except Exception:
                        logger.exception("Failed to probe cached audio duration; failure timestamps will be unavailable")
                skip_pipeline = True

        if not skip_pipeline:
            if cached_audio.exists():
                step_log.step("download", tool="cache", progress="skipped", note=f"cached audio vod={vod_id}")
                for k in ("download", "extract", "cleanup"):
                    _mark_ui(category_id, k, "skipped", "cached", tool="cache")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
                audio_path = cached_audio
            else:
                _mark_ui(category_id, "download", "current", tool="yt-dlp")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())

                progress_queue: asyncio.Queue = asyncio.Queue()
                download_task = asyncio.create_task(
                    download_vod(job.source_ref, recap_dir, category_id, progress_queue=progress_queue)
                )

                last_pct = 0
                while not download_task.done():
                    try:
                        pct = await asyncio.wait_for(progress_queue.get(), timeout=2.0)
                        if pct >= last_pct:
                            last_pct = pct
                            _mark_ui(category_id, "download", "current", f"{pct}%", pct=pct, tool="yt-dlp")
                            await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
                            step_log.step("download", tool="yt-dlp", progress=f"{pct}%")
                    except asyncio.TimeoutError:
                        pass
                    # Poll cancel between progress ticks so we can abort a
                    # multi-minute download within a couple seconds of /stop.
                    job_state = state.get(category_id)
                    if job_state is not None and job_state.cancelled:
                        download_task.cancel()
                        try:
                            await download_task
                        except BaseException:
                            pass
                        raise asyncio.CancelledError(f"Cancelled during download on channel {category_id}")

                _check_cancelled(category_id)
                source_path = await download_task
                _mark_ui(category_id, "download", "done", note=source_path.name, tool="yt-dlp")
                step_log.step("download", tool="yt-dlp", progress="done", note=source_path.name)

                _mark_ui(category_id, "extract", "current", tool="ffmpeg")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())

                async def _extract_progress(pct: int):
                    _mark_ui(category_id, "extract", "current", f"{pct}%", pct=pct, tool="ffmpeg")
                    await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())

                audio_path = await extract_audio(source_path, recap_dir, duration=vod_duration, progress_cb=_extract_progress)
                _mark_ui(category_id, "extract", "done", tool="ffmpeg")
                step_log.step("extract", tool="ffmpeg", progress="done")

                # extract_audio writes audio.mp3 directly to recap_dir; rename
                # to the canonical name we use elsewhere.
                if audio_path != cached_audio:
                    audio_path.replace(cached_audio)
                    audio_path = cached_audio

                _mark_ui(category_id, "cleanup", "current", tool="fs")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
                if source_path.exists():
                    size_mb = source_path.stat().st_size / (1024 * 1024)
                    source_path.unlink(missing_ok=True)
                    _mark_ui(category_id, "cleanup", "done", note=f"freed {size_mb:.1f} MB", tool="fs")
                    step_log.step("cleanup", tool="fs", progress="done", note=f"freed {size_mb:.1f} MB")
                else:
                    _mark_ui(category_id, "cleanup", "done", note="already removed", tool="fs")
                    step_log.step("cleanup", tool="fs", progress="done", note="already removed")

            async def _chunk_progress(current: int, total: int):
                pct = int(current / total * 100)
                _mark_ui(category_id, "chunk", "current", f"chunk {current}/{total}", pct=pct, tool="ffmpeg")
                await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())

            _mark_ui(category_id, "chunk", "current", tool="ffmpeg")
            await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
            # chunk_audio writes chunk_*.mp3 directly into chunk_cache_dir, which
            # is `recap_dir/chunks/` — no separate copy step needed.
            chunk_paths, chunk_duration_sec = await chunk_audio(
                audio_path, chunk_cache_dir, num_chunks=20, progress_cb=_chunk_progress,
            )
            _mark_ui(category_id, "chunk", "done", f"{len(chunk_paths)} chunks", pct=100, tool="ffmpeg")
            step_log.step("chunk", tool="ffmpeg", progress="done", note=f"{len(chunk_paths)} chunks")

        _check_cancelled(category_id)

        # --- Step 5: Transcribe (parallel with semaphore) ---
        transcribe_model = model_config.get("transcribe", profile)
        _mark_ui(category_id, "transcribe", "current", tool=transcribe_model)
        await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())

        total_chunks = len(chunk_paths)
        transcript_parts: list[str | None] = [None] * total_chunks
        # Failures (post all recovery attempts) surfaced as gaps in the DM
        # finish status. Each entry: (idx, start_sec, end_sec, reason).
        chunk_failures: list[tuple[int, float, float, str]] = []
        # Successful recoveries — chunks that needed a fallback path to land
        # cleanly. Surfaced in the DM finish status under "✨ Recovered" so
        # the user can see what actually happened on tricky chunks.
        chunk_recoveries: list[tuple[int, float, float, str]] = []
        completed_count = 0

        # All 20 chunks fire in parallel (one Gemini call per chunk).
        semaphore = asyncio.Semaphore(20)

        async def _bounded_transcribe(idx: int, chunk_path: Path):
            async with semaphore:
                part, usage, failure, recovery = await transcribe_chunk(chunk_path, profile)
                return idx, part, usage, failure, recovery

        tasks = [asyncio.create_task(_bounded_transcribe(i, cp)) for i, cp in enumerate(chunk_paths)]

        for coro in asyncio.as_completed(tasks):
            idx, part, usage, failure, recovery = await coro
            transcript_parts[idx] = part
            start = idx * chunk_duration_sec
            end = (idx + 1) * chunk_duration_sec
            if failure is not None:
                chunk_failures.append((idx, start, end, failure))
                logger.warning(
                    "Transcribe gap on chunk %d (%.1f-%.1fs): %s",
                    idx, start, end, failure,
                )
            if recovery is not None:
                chunk_recoveries.append((idx, start, end, recovery))
                logger.info(
                    "Transcribe recovery on chunk %d (%.1f-%.1fs): %s",
                    idx, start, end, recovery,
                )
            completed_count += 1
            pct = int(completed_count / total_chunks * 100)
            note = f"{completed_count}/{total_chunks} chunks"
            if chunk_recoveries:
                note += f", {len(chunk_recoveries)} recovered"
            if chunk_failures:
                note += f", {len(chunk_failures)} gap(s)"
            _mark_ui(
                category_id, "transcribe", "current",
                note=note,
                pct=pct, tool=transcribe_model, cost_delta=usage,
            )
            await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
            step_log.step("transcribe", model=transcribe_model, progress=f"{completed_count}/{total_chunks} ({pct}%)", usage=usage)

        transcript = "\n\n".join(part for part in transcript_parts if part)
        done_note = f"{total_chunks} chunks"
        if chunk_recoveries:
            done_note += f", {len(chunk_recoveries)} recovered"
        if chunk_failures:
            done_note += f", {len(chunk_failures)} gap(s)"
        _mark_ui(
            category_id, "transcribe", "done",
            note=done_note,
            pct=100, tool=transcribe_model,
        )
        step_log.step("transcribe", model=transcribe_model, progress="done", note=done_note)

        # Persist transcript inside this run's recap folder.
        channel_files.write_text_atomic(recap_dir / "transcript.txt", transcript)

        _check_cancelled(category_id)

        # --- Step 6: Summarize ---
        summarize_model = model_config.get("summarize", profile)
        _mark_ui(category_id, "summarize", "current", tool=summarize_model)
        await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
        journal_md, usage = await summarize_session(category_id, transcript, style=style, profile=profile)
        # Prepend the VOD title so the journal is self-identifying (visible in the file,
        # the Discord attachment, and any future re-use of the journal text).
        if job.title:
            journal_md = f"# {job.title}\n\n{journal_md}"
        _mark_ui(category_id, "summarize", "done", tool=summarize_model, cost_delta=usage)
        step_log.step("summarize", model=summarize_model, progress="done", usage=usage)

        _check_cancelled(category_id)

        # --- Step 7: Update roster & scratchpad (parallel) ---
        roster_model = model_config.get("update_roster", profile)
        scratch_model = model_config.get("update_scratchpad", profile)
        _mark_ui(category_id, "update_roster", "current", tool=roster_model)
        _mark_ui(category_id, "update_scratchpad", "current", tool=scratch_model)
        await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
        (new_roster, roster_usage), (new_scratchpad, scratch_usage) = await asyncio.gather(
            update_roster(roster_text, journal_md, profile),
            update_scratchpad(scratchpad_text, journal_md, profile),
        )
        _mark_ui(
            category_id, "update_roster", "done",
            note=f"{len(new_roster):,} chars",
            tool=roster_model, cost_delta=roster_usage,
        )
        _mark_ui(
            category_id, "update_scratchpad", "done",
            note=f"{len(new_scratchpad):,} chars",
            tool=scratch_model, cost_delta=scratch_usage,
        )
        step_log.step("update_roster", model=roster_model, progress="done", usage=roster_usage, note=f"{len(new_roster)} chars")
        step_log.step("update_scratchpad", model=scratch_model, progress="done", usage=scratch_usage, note=f"{len(new_scratchpad)} chars")

        # journal.md stays per-recap (one per session). roster + scratchpad are
        # CATEGORY-wide — one canonical pair, shared by every channel in the
        # category and accumulated across all recaps.
        channel_files.write_text_atomic(recap_dir / "journal.md", journal_md)
        await channel_files.write_roster(category_id, new_roster)
        await channel_files.write_scratchpad(category_id, new_scratchpad)

        # --- Step 8: Deliver the journal ---
        _mark_ui(category_id, "post", "current", tool="discord")
        await _send_status(bot, user_id, category_id, total_cost=cost_tracker.format_total())
        in_game_date = _extract_ingame_date(journal_md) or datetime.utcnow().strftime("%Y-%m-%d")
        if job.silent:
            # Silent: DM the journal privately to the requester instead of
            # posting in the channel. (A real ephemeral post is impossible — the
            # /recap interaction token expired long before the pipeline finished.)
            await discord_journals.dm_journal(
                bot, job.requested_by, journal_md,
                vod_id=vod_id, title=job.title, date=in_game_date,
            )
            _mark_ui(category_id, "post", "done", note=f"VOD {vod_id} (silent → DM)", tool="dm")
        else:
            # Post to the channel /recap was invoked in (job.channel_id), which
            # may differ from the journal channel — recap output and journals
            # can live in different channels of the same category.
            posted_msg_id = await discord_journals.post_journal(
                bot, job.channel_id, journal_md,
                vod_id=vod_id, title=job.title, date=in_game_date,
            )
            # Remember the message id so /recap_edit can edit this post in-place
            # later (swap the attachment) instead of leaving the visible post
            # stale relative to journal.md on disk.
            try:
                channel_files.write_recap_message_id(recap_dir, posted_msg_id)
            except Exception:
                logger.exception(
                    "Failed to persist discord_msg_id for category %s vod %s",
                    category_id, vod_id,
                )
            _mark_ui(category_id, "post", "done", note=f"VOD {vod_id}", tool="discord")
        step_log.step("post", tool="discord", progress="done", note=f"VOD {vod_id}")

        job.status = "done"
        job.completed_at = datetime.utcnow()
        step_log.total(status="done")

        # Diff old vs new roster/scratchpad so the user can see what changed.
        changes = _summarize_context_diff(
            old_roster=roster_text,
            new_roster=new_roster,
            old_scratchpad=scratchpad_text,
            new_scratchpad=new_scratchpad,
        )
        await _finish_status(
            bot, user_id, category_id,
            success=True,
            total_cost=cost_tracker.format_total(),
            changes=changes,
            chunk_failures=chunk_failures,
            chunk_recoveries=chunk_recoveries,
        )

    except asyncio.CancelledError:
        job.status = "cancelled"
        job.completed_at = datetime.utcnow()
        step_log.total(status="cancelled")
        await _finish_status(bot, user_id, category_id, success=False, error="Stopped by user", total_cost=cost_tracker.format_total())
    except Exception as exc:
        logger.exception("Recap failed on channel %s", category_id)
        job.status = "failed"
        job.error = str(exc)
        job.completed_at = datetime.utcnow()
        step_log.total(status="failed")
        await _finish_status(bot, user_id, category_id, success=False, error=str(exc), total_cost=cost_tracker.format_total())
        await _try_notify_failure(bot, job, str(exc))
    finally:
        _status_msgs.pop(category_id, None)
        _step_ui.pop(category_id, None)
        _last_status_edit.pop(category_id, None)
        _last_status_fingerprint.pop(category_id, None)
        # Usage log (best-effort) — record what this recap cost, for /admin log.
        try:
            guild = bot.get_guild(job.guild_id) if job.guild_id else None
            user = bot.get_user(job.requested_by)
            usage_log.log_event(
                event="recap",
                status=job.status,
                guild_id=job.guild_id,
                guild_name=guild.name if guild else "",
                category_id=job.category_id,
                location=job.channel_label,
                user_id=job.requested_by,
                user_name=str(user) if user else "",
                profile=job.profile,
                vod_id=job.vod_id,
                cost_usd=cost_tracker.total_cost_usd,
            )
        except Exception:
            logger.exception("Failed to log recap usage event")
        # recap_dir is PERSISTENT (audit + cache); nothing to clean up here.
        # Release the channel slot so the next /recap can run.
        state.release(category_id)


_FAILURE_LABELS = {
    # By the time a failure surfaces here, the transcribe layer has already
    # done a repetition check + (if not already on high) retried on the high
    # profile model + (for safety) attempted a localized re-chunking rescue.
    # So these labels mean "all recovery attempts failed."
    "safety":     "blocked by content filter (sub-chunk rescue also blocked)",
    "max_tokens": "output looped, retry also looped",
    "empty":      "empty response, retry also empty",
}


def _format_ts(sec: float) -> str:
    """Format seconds as MM:SS (under an hour) or H:MM:SS (an hour+).

    Returns "??:??" when chunk_duration was unavailable (e.g. ffprobe failed on
    the cached audio) so the user sees gaps were detected but no timestamp.
    """
    if sec <= 0:
        return "??:??"
    total = int(sec)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_chunk_failures(failures: list[tuple[int, float, float, str]], total: int = 20) -> str:
    """Render the failed-chunk list as a Discord message section.

    Empty string when there were no failures — caller just doesn't append.
    """
    if not failures:
        return ""
    lines = [f"⚠️ **Transcription gaps ({len(failures)}/{total} chunks):**"]
    for idx, start, end, reason in sorted(failures):
        label = _FAILURE_LABELS.get(reason, reason)
        lines.append(f"• `{_format_ts(start)} – {_format_ts(end)}` — {label}")
    return "\n".join(lines)


def _format_recovery_label(action: str) -> str:
    """Map a recovery_action tag to a user-readable label.

    `subchunk_rescue:N/M` is parsed and re-rendered with explicit counts so
    the user can see whether the rescue was full (N==M) or partial (N<M).
    """
    if action == "truncated_kept":
        return "kept truncated text (output cap reached, content non-repetitive)"
    if action == "retry_high":
        return "succeeded on retry with `high` model (default profile looped or returned empty)"
    if action.startswith("subchunk_rescue:"):
        try:
            n_ok_s, n_total_s = action.split(":", 1)[1].split("/", 1)
            n_ok, n_total = int(n_ok_s), int(n_total_s)
            if n_ok == n_total:
                return f"safety-blocked → re-split into {n_total} sub-windows, **all recovered**"
            blocked = n_total - n_ok
            return (
                f"safety-blocked → re-split into {n_total} sub-windows, "
                f"**{n_ok} recovered** ({blocked} still blocked)"
            )
        except Exception:
            return action
    return action


def _format_chunk_recoveries(recoveries: list[tuple[int, float, float, str]], total: int = 20) -> str:
    """Render the recovered-chunk list as a Discord message section.

    Empty string when nothing was recovered — clean recaps don't show this.
    """
    if not recoveries:
        return ""
    lines = [f"✨ **Recovered ({len(recoveries)}/{total} chunks):**"]
    for idx, start, end, action in sorted(recoveries):
        lines.append(f"• `{_format_ts(start)} – {_format_ts(end)}` — {_format_recovery_label(action)}")
    return "\n".join(lines)


async def _finish_status(
    bot, user_id: int, category_id: int,
    *,
    success: bool,
    error: str = "",
    total_cost: str = "",
    changes: str = "",
    chunk_failures: list[tuple[int, float, float, str]] | None = None,
    chunk_recoveries: list[tuple[int, float, float, str]] | None = None,
) -> None:
    msg = _status_msgs.pop(category_id, None)
    if not msg:
        return
    job = state.get(category_id)
    title = (job.title if job else "") or f"Recap (channel {category_id})"
    failures_text = _format_chunk_failures(chunk_failures or [])
    recoveries_text = _format_chunk_recoveries(chunk_recoveries or [])
    try:
        if success:
            where = (
                "Journal DM'd to you above (silent — not posted in the channel)."
                if (job and job.silent)
                else "Check the channel for the journal."
            )
            header = f"✅ **{title} complete!**\n{where}"
        else:
            header = f"❌ **{title} failed:**\n{error[:1000]}"
        text = _build_status_text(category_id, total_cost=total_cost, header=header)
        # Recoveries above gaps — wins first, then anything we couldn't save.
        if recoveries_text:
            text += f"\n\n{recoveries_text}"
        if failures_text:
            text += f"\n\n{failures_text}"
        if changes:
            text += f"\n\n{changes}"
        # Discord caps message content at 2000 chars; truncate defensively.
        if len(text) > 1990:
            text = text[:1980] + "\n… (truncated)"
        await msg.edit(content=text)
    except Exception:
        logger.exception("Failed to finish status for channel %s", category_id)
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            if user:
                if success:
                    _where = "DM'd above (silent)" if (job and job.silent) else "in the channel"
                    content = f"✅ {title} complete! Journal {_where}."
                else:
                    content = f"❌ {title} failed: {error[:1000]}"
                if total_cost:
                    content += f"\n💰 Total API cost: {total_cost}"
                if recoveries_text:
                    content += f"\n\n{recoveries_text}"
                if failures_text:
                    content += f"\n\n{failures_text}"
                if changes and success:
                    content += f"\n\n{changes[:1000]}"
                await user.send(content[:1990])
        except Exception:
            logger.exception("Failed to send fallback DM for channel %s", category_id)


async def _try_notify_failure(bot, job: state.ActiveJob, error: str) -> None:
    try:
        user = bot.get_user(job.requested_by) or await bot.fetch_user(job.requested_by)
        if user:
            await user.send(f"❌ Recap failed: {error[:500]}")
    except Exception:
        logger.exception(
            "Failed to send failure DM (category=%s, channel=%s, guild=%s, user=%s)",
            job.category_id, job.channel_id, job.guild_id, job.requested_by,
        )


def cancel_job(category_id: int) -> bool:
    return state.cancel(category_id)

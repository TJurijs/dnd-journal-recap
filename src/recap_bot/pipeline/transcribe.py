import asyncio
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from google import genai
from google.genai import types

from recap_bot.config import settings, model_config
from recap_bot.pipeline import llm
from recap_bot.pipeline.cost import extract_usage, UsageInfo
from recap_bot.prompts.transcribe import TRANSCRIPTION_PROMPT

logger = logging.getLogger(__name__)

# Hard cap on per-chunk transcript output. A normal ~9-min chunk produces
# 5K-12K characters ≈ 2K-4K output tokens. The longest legit chunk we've ever
# observed (gemini-3.1-flash-lite on a verbose chunk) is ~22K chars ≈ ~7K
# tokens. We cap at 12K tokens (~36K chars) — generous headroom for legit
# content, while still stopping real runaways (which would otherwise run to
# the model's hard ceiling of ~262K chars).
MAX_OUTPUT_TOKENS = 12000

# Profile we fall back to when a chunk fails on the user's selected profile.
# `gemini-3.1-flash-lite` is empirically more stable on edge content (0/20
# MAX_TOKENS events in our probe vs 2.5-lite's 1-2/20 per run on the same
# VOD). If the user is *already* on this profile, we skip the retry.
RETRY_PROFILE = "high"

# When a chunk fails with PROHIBITED_CONTENT (safety), split it into this
# many sub-chunks and retry each one. The content classifier scores audio
# across the whole window; smaller sub-chunks individually score below the
# block threshold even when their parent didn't. Empirically validated on
# Splitlanders S238 chunk_005: 8/8 sub-chunks transcribed cleanly on
# gemini-2.5-flash-lite where the parent was hard-blocked.
SAFETY_RESCUE_SUBCHUNKS = 8

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _finish_reason(response) -> str:
    """Best-effort extraction of the candidate's finish_reason as uppercase."""
    try:
        cand = (response.candidates or [None])[0]
        raw = getattr(cand, "finish_reason", "") or ""
        return str(raw).rsplit(".", 1)[-1].upper()
    except Exception:
        return ""


def _prompt_block_reason(response) -> str:
    """Best-effort extraction of prompt_feedback.block_reason as uppercase.

    Separate channel from candidate finish_reason. When Gemini blocks the
    *input* outright (e.g. PROHIBITED_CONTENT), `candidates` is empty and the
    block info lives on `prompt_feedback` instead.
    """
    try:
        pf = getattr(response, "prompt_feedback", None)
        if pf is None:
            return ""
        raw = getattr(pf, "block_reason", "") or ""
        return str(raw).rsplit(".", 1)[-1].upper()
    except Exception:
        return ""


def _classify(transcript: str, finish: str, block: str) -> str | None:
    """Map raw API signals to a stable failure code.

    Returns None on success, or one of: "safety", "max_tokens", "empty".
    Does NOT inspect text for repetition — that's a separate step the caller
    runs after MAX_TOKENS to decide whether to keep the truncated text or
    treat it as a runaway.
    """
    if block in ("PROHIBITED_CONTENT", "SAFETY", "BLOCKLIST"):
        return "safety"
    if finish in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION"):
        return "safety"
    if finish == "MAX_TOKENS":
        return "max_tokens"
    if not transcript:
        return "empty"
    return None


# Match `[HH:MM:SS]` or `[MM:SS]` (with any surrounding whitespace) so we can
# strip them before repetition matching. Loop iterations carry unique
# timestamps but identical content — keeping the timestamps would make every
# tail window byte-different from its head copy and defeat the heuristic.
_TIMESTAMP_RE = re.compile(r"\s*\[\d{1,2}:\d{2}(?::\d{2})?\]\s*")


def _normalize_for_repetition(text: str) -> str:
    """Strip timestamp prefixes so repetition matching compares content only."""
    return _TIMESTAMP_RE.sub(" ", text)


def _looks_repetitive(
    text: str,
    *,
    min_text_len: int = 2000,
    tail_frac: float = 0.30,
    window: int = 60,
    step: int = 30,
    threshold: float = 0.50,
) -> bool:
    """Heuristic: True if `text` is degenerating (re-emitting earlier content).

    Strips timestamps first (they're unique per iteration even when content is
    identical), then walks the last `tail_frac` of the text in overlapping
    `window`-char chunks and checks how many also appear in the first
    `1 - tail_frac` portion. If a majority of tail windows are repeats, the
    model is looping.

    Designed to catch loops that line-level repetition checks miss — chunk_008
    on gemini-2.5-flash-lite emitted all output as a single line with
    `[HH:MM:SS] Speaker A:` interleaved sentences, so line-counting found
    nothing. Sliding-window substring matching on normalized (timestamp-free)
    text catches it.

    Returns False for short text where the heuristic is unreliable.
    """
    norm = _normalize_for_repetition(text)
    if len(norm) < min_text_len:
        return False
    cutoff = int(len(norm) * (1 - tail_frac))
    head = norm[:cutoff]
    tail = norm[cutoff:]
    if len(tail) < window * 2:
        return False
    repeats = 0
    total = 0
    for i in range(0, len(tail) - window + 1, step):
        if tail[i:i + window] in head:
            repeats += 1
        total += 1
    return total > 0 and (repeats / total) > threshold


async def _generate(
    client, model: str, file_ref,
) -> tuple[str, UsageInfo | None, str, str]:
    """Call generate_content for one model against an already-uploaded file.

    Returns (text, usage, finish_reason, block_reason). The retry path reuses
    the same uploaded file (the file URI stays valid for both calls), so we
    don't pay for a second upload + poll roundtrip.
    """
    response = await llm.generate_content(
        client,
        model=model,
        contents=[
            types.Part.from_text(text=TRANSCRIPTION_PROMPT),
            types.Part.from_uri(file_uri=file_ref.uri, mime_type=file_ref.mime_type),
        ],
        config=types.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS),
    )
    return (
        response.text or "",
        extract_usage(response, model),
        _finish_reason(response),
        _prompt_block_reason(response),
    )


def _ffprobe_duration(path: Path) -> float:
    """Sync ffprobe (called from inside asyncio.to_thread). Returns duration in s."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def _ffmpeg_split(source: Path, dest_dir: Path, num_subchunks: int) -> list[Path]:
    """Sync ffmpeg split (called from inside asyncio.to_thread).

    Splits `source` into `num_subchunks` equal pieces written to dest_dir.
    Re-encodes to the same mono/16kHz/24kbps mp3 the main chunker uses, so
    the sub-chunks are byte-compatible with the rest of the pipeline.
    """
    duration = _ffprobe_duration(source)
    sub_dur = duration / num_subchunks
    paths: list[Path] = []
    for i in range(num_subchunks):
        start = i * sub_dur
        out_path = dest_dir / f"sub_{i:02d}.mp3"
        subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{sub_dur:.3f}",
             "-i", str(source),
             "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "libmp3lame", "-b:a", "24k",
             str(out_path)],
            capture_output=True, check=True,
        )
        paths.append(out_path)
    return paths


async def _rescue_via_subchunks(
    chunk_path: Path,
    profile: str | None,
) -> tuple[str, UsageInfo | None, int, int]:
    """Recover a safety-blocked chunk by re-splitting it into sub-chunks.

    The PROHIBITED_CONTENT classifier scores audio over the whole window. By
    cutting the chunk into ~65-second pieces, each piece individually scores
    below the threshold even when the parent didn't. Validated empirically
    on Splitlanders S238 chunk_005: 8/8 sub-chunks transcribed cleanly on
    gemini-2.5-flash-lite where the parent was hard-blocked.

    Returns (combined_transcript, combined_usage, n_succeeded, n_total).
    Sub-chunks that themselves fail are inlined as placeholder markers in
    the combined transcript — we never recurse further (no infinite split).
    """
    with tempfile.TemporaryDirectory(prefix="rescue_") as tmpdir:
        tmpdir_path = Path(tmpdir)
        try:
            sub_paths = await asyncio.to_thread(
                _ffmpeg_split, chunk_path, tmpdir_path, SAFETY_RESCUE_SUBCHUNKS,
            )
        except Exception:
            logger.exception("ffmpeg split failed for rescue of %s", chunk_path.name)
            return "", None, 0, 0

        # Transcribe sub-chunks in parallel. _allow_safety_rescue=False prevents
        # infinite recursion if a sub-chunk also fails safety.
        sem = asyncio.Semaphore(min(SAFETY_RESCUE_SUBCHUNKS, 8))

        async def _one(sp: Path):
            async with sem:
                return await transcribe_chunk(sp, profile, _allow_safety_rescue=False)

        results = await asyncio.gather(
            *[_one(p) for p in sub_paths], return_exceptions=True,
        )

    # Combine results in order. Successes get their transcripts; sub-chunks
    # that still failed get a per-sub-chunk placeholder. (recovery_action
    # from sub-chunks is discarded — only the chunk-level summary matters.)
    parts: list[str] = []
    total_usage: UsageInfo | None = None
    n_ok = 0
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Sub-chunk transcribe raised: %s", r)
            parts.append("[transcription unavailable for this segment]")
            continue
        sub_text, sub_usage, sub_failure, _sub_recovery = r
        if sub_usage is not None:
            total_usage = (total_usage + sub_usage) if total_usage is not None else sub_usage
        if sub_failure is None:
            n_ok += 1
        parts.append(sub_text)

    combined = "\n\n".join(parts)
    return combined, total_usage, n_ok, len(results)


async def transcribe_chunk(
    chunk_path: Path,
    profile: str | None = None,
    *,
    _allow_safety_rescue: bool = True,
) -> tuple[str, UsageInfo | None, str | None, str | None]:
    """Transcribe a single audio chunk.

    Returns (transcript, usage, failure_reason, recovery_action):
      - transcript: real text (possibly truncated but valid) OR a placeholder
        if the chunk failed and all recovery paths also failed.
      - usage: combined token/cost across the primary call + any retries/rescues.
      - failure_reason: None on success, else one of "safety", "max_tokens",
        "empty".
      - recovery_action: None if the primary call succeeded cleanly. Else a
        short tag describing what saved this chunk:
          * "truncated_kept"            — MAX_TOKENS but not repetitive; text kept
          * "retry_high"                — recovered by re-running on the high model
          * "subchunk_rescue:N/M"       — recovered by re-splitting into M sub-chunks
                                          (N of M transcribed cleanly; partial when N<M)
        The orchestrator surfaces these in the DM finish status alongside
        gaps so the user can see what actually happened on tricky chunks.

    Recovery strategies in order:

    1. **MAX_TOKENS + not repetitive** → keep the truncated text as success.
       Models sometimes emit verbose-but-legit chunks that brush the cap.

    2. **MAX_TOKENS + repetitive, or empty** → retry the same chunk on the
       `high` profile model (gemini-3.1-flash-lite), which is empirically
       more stable than 2.5-lite on the same content. Skipped when we're
       already on the high model.

    3. **PROHIBITED_CONTENT (safety)** → split the chunk into 8 sub-chunks
       and transcribe each independently. The content classifier scores
       audio across the whole window; smaller pieces individually score
       below the block threshold even when the parent didn't. Empirically
       100% recovery on the one known-blocked chunk we've measured. Skipped
       on recursive sub-chunk calls (`_allow_safety_rescue=False`) to
       prevent infinite splitting if a sub-chunk also blocks.

    Failures returned by this function mean every applicable recovery has
    already been attempted.
    """
    primary_model = model_config.get("transcribe", profile)
    high_model = model_config.get("transcribe", RETRY_PROFILE)

    logger.info("Using model '%s' for transcribe_chunk (profile=%s)", primary_model, profile)
    client = _get_client()
    file = await asyncio.to_thread(client.files.upload, file=str(chunk_path))
    logger.info("Uploaded chunk %s as %s", chunk_path, file.name)

    # Poll until active
    for _ in range(30):
        await asyncio.sleep(2)
        refreshed = await asyncio.to_thread(client.files.get, name=file.name)
        if refreshed.state.name == "ACTIVE":
            break
        logger.debug("File %s state: %s", file.name, refreshed.state.name)
    else:
        raise RuntimeError("Gemini file did not become ACTIVE in time")

    # --- First attempt on the user's selected profile model ---
    transcript, usage, finish, block = await _generate(client, primary_model, refreshed)
    failure = _classify(transcript, finish, block)
    recovery_action: str | None = None

    # MAX_TOKENS: distinguish real runaway from legit-long content.
    if failure == "max_tokens":
        if _looks_repetitive(transcript):
            logger.warning(
                "Chunk %s MAX_TOKENS + repetitive on %s (%d chars) — will retry on %s",
                chunk_path.name, primary_model, len(transcript), high_model,
            )
            # leave failure as "max_tokens"; retry block below handles it
        else:
            logger.info(
                "Chunk %s MAX_TOKENS but NOT repetitive on %s (%d chars) — keeping truncated text",
                chunk_path.name, primary_model, len(transcript),
            )
            failure = None  # soft-truncated success
            recovery_action = "truncated_kept"

    # --- Retry path: re-run this chunk on the high model ---
    # Skipped when we're already on the high model (no upgrade available) OR
    # when the failure is "safety" (input gate is shared across models — the
    # rescue-via-subchunks path below handles that case instead).
    if failure in ("max_tokens", "empty") and primary_model != high_model:
        logger.warning(
            "Chunk %s failed on %s (failure=%s) — retrying on %s",
            chunk_path.name, primary_model, failure, high_model,
        )
        try:
            t2, u2, f2_finish, f2_block = await _generate(client, high_model, refreshed)
        except Exception:
            logger.exception("Retry of chunk %s on %s raised", chunk_path.name, high_model)
            t2, u2, f2_finish, f2_block = "", None, "", ""

        # Accumulate billed usage from both calls — we paid for both.
        if u2 is not None:
            usage = (usage + u2) if usage is not None else u2

        failure_retry = _classify(t2, f2_finish, f2_block)
        if failure_retry == "max_tokens" and not _looks_repetitive(t2):
            logger.info(
                "Chunk %s retry on %s MAX_TOKENS but not repetitive — keeping truncated retry text",
                chunk_path.name, high_model,
            )
            failure_retry = None

        if failure_retry is None:
            logger.info(
                "Chunk %s retry on %s succeeded (%d chars)",
                chunk_path.name, high_model, len(t2),
            )
            transcript = t2
            failure = None
            recovery_action = "retry_high"
        else:
            logger.warning(
                "Chunk %s retry on %s also failed (failure=%s, finish=%r, block=%r)",
                chunk_path.name, high_model, failure_retry, f2_finish, f2_block,
            )
            failure = failure_retry  # retry's failure dominates

    # Clean up the (parent) uploaded file before potentially launching the
    # rescue path — the rescue uploads its own sub-chunk files and we don't
    # want to leak the parent's quota.
    try:
        await asyncio.to_thread(client.files.delete, name=file.name)
    except Exception:
        logger.warning("Failed to delete Gemini file %s", file.name)

    # --- Safety rescue: split into sub-chunks and re-transcribe each ---
    # Triggered when the parent chunk was hard-blocked on content but we're
    # NOT inside a recursive sub-chunk call (the _allow_safety_rescue guard
    # prevents infinite re-splitting). Empirically the classifier passes
    # smaller pieces of the same audio even when the parent fails.
    if failure == "safety" and _allow_safety_rescue:
        logger.warning(
            "Chunk %s safety-blocked — attempting localized rescue (%d sub-chunks)",
            chunk_path.name, SAFETY_RESCUE_SUBCHUNKS,
        )
        rescue_text, rescue_usage, n_ok, n_total = await _rescue_via_subchunks(
            chunk_path, profile,
        )
        if rescue_usage is not None:
            usage = (usage + rescue_usage) if usage is not None else rescue_usage

        if n_ok > 0:
            logger.info(
                "Chunk %s safety rescue: %d/%d sub-chunks transcribed (recovered %d%%)",
                chunk_path.name, n_ok, n_total, int(100 * n_ok / max(1, n_total)),
            )
            transcript = rescue_text
            failure = None  # rescue produced usable content
            recovery_action = f"subchunk_rescue:{n_ok}/{n_total}"
        else:
            logger.warning(
                "Chunk %s safety rescue: ALL %d sub-chunks also blocked — keeping placeholder",
                chunk_path.name, n_total,
            )
            # leave failure = "safety"; falls through to placeholder below

    if failure is not None:
        logger.warning(
            "Chunk %s final failure=%s (primary=%s, finish=%r, block=%r, text_len=%d) — placeholder",
            chunk_path.name, failure, primary_model, finish, block, len(transcript),
        )
        transcript = "[transcription unavailable for this segment]"

    return transcript, usage, failure, recovery_action


async def transcribe_audio(
    audio_path: Path, profile: str | None = None,
) -> tuple[str, UsageInfo | None, str | None, str | None]:
    """Transcribe a full audio file (single chunk)."""
    return await transcribe_chunk(audio_path, profile)

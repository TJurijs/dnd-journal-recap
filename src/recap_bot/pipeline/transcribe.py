import asyncio
import logging
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

    Walks the last `tail_frac` of the text in overlapping `window`-char chunks
    and checks how many also appear in the first `1 - tail_frac` portion. If
    a majority of tail windows are repeats, the model is looping.

    Designed to catch loops that line-level repetition checks miss — chunk_008
    on gemini-2.5-flash-lite emitted all output as a single line with
    `[HH:MM:SS] Speaker A:` interleaved sentences, so line-counting found
    nothing. Sliding-window substring matching catches it.

    Returns False for short text where the heuristic is unreliable.
    """
    if len(text) < min_text_len:
        return False
    cutoff = int(len(text) * (1 - tail_frac))
    head = text[:cutoff]
    tail = text[cutoff:]
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


async def transcribe_chunk(
    chunk_path: Path, profile: str | None = None,
) -> tuple[str, UsageInfo | None, str | None]:
    """Transcribe a single audio chunk.

    Returns (transcript, usage, failure_reason):
      - transcript: real text (possibly truncated but valid) OR a placeholder
        if the chunk failed and any retry also failed.
      - usage: combined token/cost across original call + any retry.
      - failure_reason: None on success, else one of "safety", "max_tokens",
        "empty".

    Recovery strategy when the primary model returns MAX_TOKENS:
      1. Run a repetition heuristic on the text.
      2. If NOT repetitive: it's just legit-long content that hit the cap.
         Keep the truncated text and return success — better a slightly cut
         transcript than no transcript at all.
      3. If REPETITIVE: it's a real runaway. If we're not already on the
         `high` profile model, retry that single chunk on it — sampling
         variance + a larger, better-aligned model usually finishes cleanly.

    Same retry logic applies to "empty" results, which can come from a
    smaller-model hiccup where the larger model produces real text.

    Does NOT retry "safety" — the PROHIBITED_CONTENT / safety gate is shared
    across the flash-lite family, so retrying on a different model in the
    family won't help (empirically confirmed).
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

    # --- Retry path: re-run this chunk on the high model ---
    # Skipped when we're already on the high model (no upgrade available) OR
    # when the failure is "safety" (input gate is shared across models).
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
        else:
            logger.warning(
                "Chunk %s retry on %s also failed (failure=%s, finish=%r, block=%r)",
                chunk_path.name, high_model, failure_retry, f2_finish, f2_block,
            )
            failure = failure_retry  # retry's failure dominates

    if failure is not None:
        logger.warning(
            "Chunk %s final failure=%s (primary=%s, finish=%r, block=%r, text_len=%d) — placeholder",
            chunk_path.name, failure, primary_model, finish, block, len(transcript),
        )
        transcript = "[transcription unavailable for this segment]"

    # Clean up uploaded file
    try:
        await asyncio.to_thread(client.files.delete, name=file.name)
    except Exception:
        logger.warning("Failed to delete Gemini file %s", file.name)

    return transcript, usage, failure


async def transcribe_audio(
    audio_path: Path, profile: str | None = None,
) -> tuple[str, UsageInfo | None, str | None]:
    """Transcribe a full audio file (single chunk)."""
    return await transcribe_chunk(audio_path, profile)

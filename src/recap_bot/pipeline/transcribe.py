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
# 5K–12K characters ≈ 2K–4K output tokens; anything past 8K tokens means the
# model is looping/degenerate. Capping early prevents the 257K-char runaway
# we observed on gemini-2.5-flash-lite (which would otherwise generate to the
# model's hard ~262K ceiling and waste tokens + poison the summarize stage
# with repeated garbage).
MAX_OUTPUT_TOKENS = 8000

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

    This is a SEPARATE channel from candidate finish_reason. When Gemini blocks
    the *input* (the audio) outright — e.g. PROHIBITED_CONTENT — `candidates`
    is empty and the block info lives on `prompt_feedback` instead. That's
    exactly the failure mode we saw on the Splitlanders chunk_005.
    """
    try:
        pf = getattr(response, "prompt_feedback", None)
        if pf is None:
            return ""
        raw = getattr(pf, "block_reason", "") or ""
        return str(raw).rsplit(".", 1)[-1].upper()
    except Exception:
        return ""


def _classify_failure(transcript: str, finish: str, block: str) -> str | None:
    """Map raw API signals to a stable, human-meaningful failure code.

    Returns None when the chunk is fine. Otherwise one of:
      - "safety"      : input or output blocked by content classifier
      - "max_tokens"  : model ran past our output cap (almost certainly looping)
      - "empty"       : empty text, no clear reason (rare; model hiccup)
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


async def transcribe_chunk(
    chunk_path: Path, profile: str | None = None,
) -> tuple[str, UsageInfo | None, str | None]:
    """Transcribe a single audio chunk. Uploads, polls, generates, deletes.

    Returns (transcript, usage, failure_reason):
      - transcript is either real text OR a placeholder when failure_reason is set
      - usage is the token/cost record (still meaningful even on failure — the
        call was billed)
      - failure_reason is None on success, else one of: "safety", "max_tokens",
        "empty"

    Never raises on per-chunk content issues — those are surfaced via
    failure_reason so the recap can complete with placeholders instead of
    dying on a single bad chunk. Genuine errors (upload failure, transient
    network) still raise.
    """
    model = model_config.get("transcribe", profile)
    logger.info("Using model '%s' for transcribe_chunk", model)
    client = _get_client()
    file = await asyncio.to_thread(client.files.upload, file=str(chunk_path))
    logger.info("Uploaded chunk %s as %s", chunk_path, file.name)

    # Poll until active
    for attempt in range(30):
        await asyncio.sleep(2)
        refreshed = await asyncio.to_thread(client.files.get, name=file.name)
        if refreshed.state.name == "ACTIVE":
            break
        logger.debug("File %s state: %s", file.name, refreshed.state.name)
    else:
        raise RuntimeError("Gemini file did not become ACTIVE in time")

    response = await llm.generate_content(
        client,
        model=model,
        contents=[
            types.Part.from_text(text=TRANSCRIPTION_PROMPT),
            types.Part.from_uri(file_uri=refreshed.uri, mime_type=refreshed.mime_type),
        ],
        config=types.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS),
    )

    transcript = response.text or ""
    finish = _finish_reason(response)
    block = _prompt_block_reason(response)
    failure = _classify_failure(transcript, finish, block)

    if failure is not None:
        # Don't kill the whole recap on one bad chunk. Substitute a placeholder
        # so the other 19 still produce a usable journal — the orchestrator
        # collects these failures and surfaces them in the DM finish status
        # with timestamp ranges so the user knows which segments are missing.
        logger.warning(
            "Chunk %s failed (model=%s, failure=%s, finish_reason=%r, "
            "block_reason=%r, text_len=%d) — substituting placeholder",
            chunk_path.name, model, failure, finish, block, len(transcript),
        )
        transcript = "[transcription unavailable for this segment]"

    usage = extract_usage(response, model)

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

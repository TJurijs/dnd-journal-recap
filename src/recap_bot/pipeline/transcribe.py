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

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


async def transcribe_chunk(chunk_path: Path, profile: str | None = None) -> tuple[str, UsageInfo | None]:
    """Transcribe a single audio chunk. Uploads, polls, generates, deletes."""
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
    )

    transcript = response.text or ""
    if not transcript:
        # Don't kill the whole recap on one bad chunk. An empty response can
        # come from real silence in the chunk, a safety-filter trip, or a flaky
        # moment from the smaller transcribe model (the cheaper `default`
        # profile uses gemini-2.5-flash-lite, which is more prone to this than
        # `high`). Log the finish_reason and substitute a placeholder so 19/20
        # good chunks still produce a recap.
        reason = ""
        try:
            cand = (response.candidates or [None])[0]
            reason = str(getattr(cand, "finish_reason", "") or "")
        except Exception:
            pass
        logger.warning(
            "Empty transcript for %s (model=%s, finish_reason=%r) — substituting placeholder",
            chunk_path.name, model, reason,
        )
        note = f" (finish_reason={reason})" if reason and reason.lower() not in ("stop", "finish_reason_unspecified") else ""
        transcript = f"[transcription unavailable for this segment{note}]"

    usage = extract_usage(response, model)

    # Clean up uploaded file
    try:
        await asyncio.to_thread(client.files.delete, name=file.name)
    except Exception:
        logger.warning("Failed to delete Gemini file %s", file.name)

    return transcript, usage


async def transcribe_audio(audio_path: Path, profile: str | None = None) -> tuple[str, UsageInfo | None]:
    """Transcribe a full audio file (single chunk)."""
    return await transcribe_chunk(audio_path, profile)

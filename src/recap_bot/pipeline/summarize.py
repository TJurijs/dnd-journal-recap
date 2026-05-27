import asyncio
import logging

from google import genai

from recap_bot.config import settings, model_config
from recap_bot.pipeline import llm
from recap_bot.pipeline.cost import extract_usage, UsageInfo
from recap_bot.storage import files as channel_files
from recap_bot.prompts.summarize import build_summarize_prompt

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


async def summarize_session(category_id: int, transcript: str, style: str | None = None) -> tuple[str, UsageInfo | None]:
    model = model_config.get("summarize")
    meta = await channel_files.read_meta(category_id) or {"category_id": category_id}
    effective_style = style or meta.get("style") or settings.default_style

    roster_text = await channel_files.read_roster(category_id)
    scratchpad_text = await channel_files.read_scratchpad(category_id)

    prompt = build_summarize_prompt(
        campaign=meta,
        roster=roster_text,
        scratchpad=scratchpad_text,
        style=effective_style,
        transcript=transcript,
    )

    client = _get_client()
    response = await llm.generate_content(client, model=model, contents=prompt)

    journal = response.text or ""
    if not journal:
        raise RuntimeError("Gemini returned empty journal")

    usage = extract_usage(response, model)
    return journal, usage

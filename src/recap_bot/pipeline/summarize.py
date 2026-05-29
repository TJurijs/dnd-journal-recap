import asyncio
import logging

from google import genai

from recap_bot.config import settings, model_config
from recap_bot.pipeline import llm
from recap_bot.pipeline.cost import extract_usage, UsageInfo
from recap_bot.storage import files as channel_files
from recap_bot.prompts.summarize import build_summarize_prompt

logger = logging.getLogger(__name__)

# Hard cap on journal length so the recap fits in a single Discord embed
# (Discord's embed description limit is 4096 chars; we leave headroom for the
# code-block fences used in silent-mode delivery + a safety margin).
MAX_JOURNAL_CHARS = 4000

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


def _trim_to_section_boundary(text: str, max_chars: int) -> str:
    """Trim `text` to <= max_chars, cutting at a `## ` section boundary when
    possible so we never truncate mid-sentence. Appends a small marker.

    Keeps the leading content (title line, `## Session Date`, first scenes) and
    drops trailing scenes. If even the first section exceeds max_chars, hard-cut
    at max_chars as a last resort.
    """
    marker = "\n\n_…(truncated to fit a single Discord post)_"
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(marker)
    if budget <= 0:
        return text[:max_chars]

    # Split keeping the `## ` delimiters attached to each following section.
    import re
    sections = re.split(r"(?=\n##\s)", text)
    kept = ""
    for s in sections:
        if len(kept) + len(s) <= budget:
            kept += s
        else:
            break
    kept = kept.rstrip()
    if not kept:
        # First section alone is over budget — hard cut.
        return text[:budget].rstrip() + marker
    return kept + marker


async def _generate_journal(client, model: str, prompt: str) -> tuple[str, UsageInfo | None]:
    response = await llm.generate_content(client, model=model, contents=prompt)
    return (response.text or ""), extract_usage(response, model)


async def summarize_session(category_id: int, transcript: str, style: str | None = None, profile: str | None = None) -> tuple[str, UsageInfo | None]:
    model = model_config.get("summarize", profile)
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
        max_chars=MAX_JOURNAL_CHARS,
    )

    client = _get_client()
    journal, usage = await _generate_journal(client, model, prompt)
    if not journal:
        raise RuntimeError("Gemini returned empty journal")

    # --- Enforce the hard length cap ---
    # The model usually obeys the prompt's length instruction, but not always.
    # If it overruns, retry once with an explicit "you went over" nudge, then
    # fall back to a section-boundary trim. Both calls use the same summarize
    # model, so summing usage via __add__ is safe (model tag preserved).
    if len(journal) > MAX_JOURNAL_CHARS:
        logger.warning(
            "Journal over cap (%d > %d chars) — retrying once with stronger constraint",
            len(journal), MAX_JOURNAL_CHARS,
        )
        retry_prompt = (
            prompt
            + f"\n\nYOUR PREVIOUS DRAFT WAS {len(journal)} CHARACTERS — TOO LONG. "
            f"Rewrite the ENTIRE journal so it is strictly under {MAX_JOURNAL_CHARS} "
            f"characters. Cut the least-important scenes and tighten every bullet. "
            f"Keep the most pivotal story beats."
        )
        journal2, usage2 = await _generate_journal(client, model, retry_prompt)
        if usage2 is not None:
            usage = (usage + usage2) if usage is not None else usage2
        if journal2 and len(journal2) <= MAX_JOURNAL_CHARS:
            journal = journal2
        elif journal2 and len(journal2) < len(journal):
            # Retry still over but shorter — keep the shorter one to trim.
            journal = journal2

    if len(journal) > MAX_JOURNAL_CHARS:
        logger.warning(
            "Journal still over cap after retry (%d chars) — trimming at section boundary",
            len(journal),
        )
        journal = _trim_to_section_boundary(journal, MAX_JOURNAL_CHARS)

    return journal, usage

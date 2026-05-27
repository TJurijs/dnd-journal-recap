"""Shared Gemini call helper with retry on transient (429 / 5xx) errors.

Google's API intermittently returns 500 INTERNAL / 503 UNAVAILABLE or 429 rate
limits. Without a retry, a single transient blip on any one of a recap's ~22
LLM calls (20 transcribe chunks + summarize + roster/scratchpad updates) kills
the entire run — which the user has already partially paid for. We retry a few
times with exponential backoff + jitter before giving up. Non-transient errors
(e.g. 400 bad request) are re-raised immediately — retrying them is pointless.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying: rate limit + server-side errors.
_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_CODE_RE = re.compile(r"\b(429|500|502|503|504)\b")


def _transient_code(exc: BaseException) -> int | None:
    """Return the HTTP status if `exc` looks like a transient API error, else None.

    Prefers the SDK's `.code` attribute (google.genai APIError exposes it),
    falling back to scanning the message for a known transient code.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code in _TRANSIENT_CODES:
        return code
    m = _CODE_RE.search(str(exc))
    return int(m.group(1)) if m else None


async def generate_content(
    client,
    *,
    model,
    contents,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    **kwargs,
):
    """Run `client.models.generate_content(...)` off the event loop, retrying on
    transient errors with exponential backoff + jitter.

    Re-raises non-transient errors immediately, and the last transient error
    once `max_attempts` is exhausted.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return await asyncio.to_thread(
                client.models.generate_content, model=model, contents=contents, **kwargs
            )
        except Exception as exc:
            code = _transient_code(exc)
            if code is None or attempt == max_attempts:
                raise
            delay = min(base_delay * (2 ** (attempt - 1)) + random.uniform(0, base_delay), 30.0)
            logger.warning(
                "Gemini transient error %s (attempt %d/%d, model %s) — retrying in %.1fs",
                code, attempt, max_attempts, model, delay,
            )
            await asyncio.sleep(delay)

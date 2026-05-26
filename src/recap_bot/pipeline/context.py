"""Roster + scratchpad building, and incremental updates.

The full-history build for /initialize uses ONE LLM call per artifact with all
journals concatenated in context. This gives the strong model the source of
truth so it can:
  - merge spelling variants (Caladin/Kaladin, Sana/Xana)
  - correctly tag Player vs NPC by who appears in every session
  - find named characters the batch extractor would miss
  - preserve relationships (e.g. "Kaladin's pet Ignatius")

The incremental /recap path still uses small per-journal update functions.
"""

from __future__ import annotations

import asyncio
import logging
import re

from google import genai

from recap_bot.config import model_config, settings
from recap_bot.pipeline.cost import UsageInfo, extract_usage

logger = logging.getLogger(__name__)

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.gemini_api_key)
    return _client


_NAME_RE = re.compile(r"^-\s+(.+?)\s*\((Player|NPC)\)")


def _concat_journals(journals_md: list[str]) -> str:
    """Concatenate journal records with clear separators and 1-based indices."""
    blocks = []
    for idx, text in enumerate(journals_md, 1):
        blocks.append(f"=== RECORD {idx} ===\n{text}")
    return "\n\n---\n\n".join(blocks)


# ----- Full-history builds (used by /initialize) ----------------------------

async def build_roster_from_journals(journals_md: list[str], progress_cb=None) -> tuple[str, UsageInfo | None]:
    """Build a canonical roster from ALL journals in a single LLM call.

    `progress_cb` (if given) is called with (0, total, None) at the start and
    (total, total, usage) at the end — the underlying call has no granular
    progress, so the UI gets begin/end signals only.
    """
    if not journals_md:
        return "", None

    model = model_config.get("roster_build")
    total = len(journals_md)

    if progress_cb:
        await progress_cb(0, total, None)

    journals_block = _concat_journals(journals_md)
    prompt = f"""You are the campaign chronicler. Below are ALL journals from a D&D campaign in chronological order. Produce the canonical roster of every named character.

Rules:
1. Include every named character: PCs, important NPCs (allies, enemies, shopkeepers), named monsters, named pets, named gods/factions when they appear as characters.
2. "Player" = a member of the adventuring party (recurs across sessions, fights alongside the others). "NPC" = anyone else.
3. Merge spelling variants caused by Speech-to-Text errors:
   - Typos: "Caladin", "Kalidin", "Kaladins" → "Kaladin"
   - Phonetic similarities: "Sana"/"Xana" → pick the canonical form
   - Spacing/casing: "kal ruin" → "Kalruin", "black water" → "Blackwater"
   - Short/long forms: "Rob" → "Robert", "Patch" → "Patchwork"
4. Use 1-2 sentence descriptions focused on **who the character IS, not what they did this week**:
   - Class / role / type / faction (wizard, ranger, captain of the Iron Tide, queen of X, ancient dragon, etc.)
   - Key relationships ("Kaladin's pet", "rival of Schoks", "consort of Queen Dinara")
   - Long-term motivation or arc ("seeking a trade alliance with Demmi", "hunting the cult of Avagaru", "in exile from her homeland")
   - Persistent state if notable (dead, departed, transformed, imprisoned, ruling X, secretly cursed)

   DO NOT include single-session events. Bad: "teleported the party back to Issic and used a teddy bear to pacify a cloaked guardian." Good: "Tactical magic-user pursuing a trade alliance with Demmi to clear Issic of monsters." Single-session events belong in the scratchpad, not the roster. The roster is reference material that stays stable across sessions.
5. Do NOT skip named characters just because they only appear in one session — include them.
6. Exclude clearly generic unnamed characters ("a guard", "the bartender", "townsfolk").
7. Order: ALL Player characters first (in order of introduction), then ALL NPCs (in order of introduction).
8. Each line MUST be exactly: `- Name (Player): description` or `- Name (NPC): description`

Output the roster ONLY — no preamble, no explanations, no markdown headers.

Journals:
{journals_block}
"""

    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
    )
    text = (response.text or "").strip()
    usage = extract_usage(response)

    # Keep only lines that match the strict format; LLMs sometimes wrap with prose.
    lines = [ln for ln in text.splitlines() if _NAME_RE.match(ln.strip())]
    roster = "\n".join(lines) if lines else text

    if progress_cb:
        await progress_cb(total, total, usage)

    logger.debug("Built roster: %d lines from %d journals (%s)", len(lines), total, usage.format_cost() if usage else "?")
    return roster, usage


async def build_scratchpad_from_journals(journals_md: list[str], progress_cb=None) -> tuple[str, UsageInfo | None]:
    """Build a canonical scratchpad from ALL journal records in a single LLM call.

    One line per journal record (these are NOT 1:1 with sessions — some sessions
    have multiple records). Chronological, consistent character spellings.
    """
    if not journals_md:
        return "", None

    model = model_config.get("scratchpad_build")
    total = len(journals_md)

    if progress_cb:
        await progress_cb(0, total, None)

    journals_block = _concat_journals(journals_md)
    prompt = f"""You are the campaign chronicler. Below are ALL journal records from a D&D campaign in chronological order ({total} records). Produce a canonical scratchpad — a compact one-line summary per record.

Important: these are *journal records*, not game sessions. Some sessions span multiple records; some records cover a partial session. Do NOT call them "sessions" or assign session numbers.

Rules:
1. ONE line per record, in this format: `Entry N (Date if known): 1-2 sentence summary.`
   - N is the record's position (1 to {total})
2. Maintain chronological order from Entry 1 to Entry {total}.
3. Focus on the single most important development of each record. If the record is a continuation of a prior one, mention that briefly.
4. Use consistent character name spellings across all entries (fix STT variants like Caladin → Kaladin, Sana → Xana).
5. Do NOT skip records, merge records, or invent records.

Output the scratchpad ONLY — no preamble, no explanations, no markdown headers.

Journal records:
{journals_block}
"""

    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
    )
    text = (response.text or "").strip()
    usage = extract_usage(response)

    if progress_cb:
        await progress_cb(total, total, usage)

    logger.debug("Built scratchpad: %d chars from %d journals (%s)", len(text), total, usage.format_cost() if usage else "?")
    return text, usage


# ----- Incremental updates (used by /recap) --------------------------------

async def update_scratchpad(existing_scratchpad: str, new_journal: str) -> tuple[str, UsageInfo | None]:
    """Append a new entry to the scratchpad from this recap. Returns (text, usage)."""
    model = model_config.get("update_scratchpad")
    prompt = f"""Given the existing campaign scratchpad and a new recap journal, add ONE new entry to the end.

Important: scratchpad entries are *journal records*, not game sessions. Don't call this entry a "session" or give it a session number. Use the next "Entry N" number that continues the existing sequence (e.g. if the last entry is "Entry 117", this one is "Entry 118"). If the existing scratchpad uses a different format, match it.

Rules:
- Write 1-2 sentences summarizing the single most important event of this recap.
- Include the in-game date if mentioned.
- Match the formatting of the existing entries.
- Append the new entry to the end. Do NOT modify or remove any existing entries.

Existing scratchpad:
{existing_scratchpad}

New recap journal:
{new_journal[:2000]}

Return ONLY the updated scratchpad with the new entry appended."""

    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
    )

    text = response.text or ""
    usage = extract_usage(response)
    return (text.strip() or existing_scratchpad), usage


async def update_roster(existing_roster: str, new_journal: str) -> tuple[str, UsageInfo | None]:
    """Incrementally update roster from a single new journal entry. Returns (text, usage)."""
    model = model_config.get("update_roster")
    prompt = f"""You are updating a campaign roster with information from a new session journal. The roster is a PERMANENT campaign log.

CRITICAL RULES:

1. NEVER REMOVE EXISTING ENTRIES. Keep every character that was in the roster, including those who died, departed, or aren't in this session. Update their description to reflect the new state (e.g. "killed by the party in this session", "departed for Chaur"). Dead and gone characters stay on the roster forever.

2. PREFER UPDATING EXISTING ENTRIES OVER ADDING NEW ONES. Before adding a new character, scan the existing roster for spelling variants or short/long forms of the same name. Treat these as the SAME character and UPDATE the existing entry instead of creating a duplicate:
   - Typos: "Evany" → existing "Ebony", "Caladin" → existing "Kaladin", "Kalidin" → existing "Kaladin"
   - Phonetic: "Sana" → existing "Xana"
   - Short forms: "Rob" → existing "Robert", "Patch" → existing "Patchwork"
   - Spacing/casing: "kal ruin" → existing "Kalruin"

3. ONLY ADD GENUINELY NEW NAMED CHARACTERS — i.e. someone who appears in this journal and has no spelling-variant match in the existing roster.

4. Descriptions focus on **who the character IS** — class/role/faction, key relationships, long-term motivation, persistent state. DO NOT mention what happened *this session*: those events belong in the scratchpad. Bad: "teleported the party back to Issic this session." Good: "Tactical magic-user pursuing trade alliance with Demmi." The roster is reference material that should stay roughly stable across sessions; only update when an enduring fact actually changes (someone dies, transforms, switches sides, gains a permanent title, etc.).

5. Exclude clearly generic unnamed characters ("a guard", "townsfolk").

6. PRESERVE existing (Player) and (NPC) tags. Only assign tags to genuinely new entries.

7. ORDER: ALL Player characters FIRST in their existing order, then ALL NPCs in their existing order. Append any newly-introduced characters at the end of their respective group.

Current roster:
{existing_roster}

New journal:
{new_journal[:5000]}

Output format:
<roster>
- Name (Player): one or two sentence description
- Name (NPC): one or two sentence description
...
</roster>

Return ONLY the updated roster inside the tags."""

    client = _get_client()
    response = await asyncio.to_thread(
        client.models.generate_content,
        model=model,
        contents=prompt,
    )

    text = response.text or ""
    roster = existing_roster
    if "<roster>" in text and "</roster>" in text:
        s = text.index("<roster>") + len("<roster>")
        e = text.index("</roster>")
        roster = text[s:e].strip()
    elif text.strip():
        roster = text.strip()

    usage = extract_usage(response)
    return roster, usage

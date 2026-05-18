from typing import Optional

from recap_bot.prompts.styles import get_style_prompt


def build_summarize_prompt(
    campaign: Optional[dict],
    roster: Optional[str],
    scratchpad: Optional[str],
    style: str,
    transcript: str,
) -> str:
    lines = [
        "You are a chronicler producing a session journal for an ongoing D&D campaign.",
    ]

    if campaign:
        lines.append(f"Campaign: {campaign.get('name', 'Unnamed')}")
        if campaign.get("premise"):
            lines.append(f"Premise: {campaign['premise']}")

    if roster:
        lines.append("")
        lines.append("Characters & Players:")
        lines.append(roster)

    if scratchpad:
        lines.append("")
        lines.append("Session History:")
        lines.append(scratchpad)

    lines.append("")
    lines.append(get_style_prompt(style))

    lines.append("")
    lines.append(
        "Instructions:\n"
        "- Refer to characters and players by name (not 'Speaker A').\n"
        "- If a speaker's name is unclear, use a placeholder like [Player 1] or [DM].\n"
        "- Capture: key story beats, combat highlights, NPCs introduced, loot, decisions made, cliffhangers.\n"
        "- Omit out-of-character chatter, rules debates, and breaks.\n"
        "- Output Markdown."
    )

    lines.append("")
    lines.append("<transcript>")
    lines.append(transcript)
    lines.append("</transcript>")

    lines.append("")
    lines.append("")
    lines.append(
        "At the very top of your response, if you can identify an in-game date from the transcript "
        "(e.g. 'the 74th of Fall', '4/6/26', 'session 76'), include it on its own line as:\n"
        "## Session Date: [date]\n"
        "If no date is mentioned, omit this line."
    )

    lines.append("Write the journal for this session.")

    return "\n".join(lines)

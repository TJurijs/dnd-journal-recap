STYLES = {
    "chapters": """Identify the 3-8 key events of this session and write each as its own chapter.

Format:
## <Short, descriptive chapter title>
- 2-6 bullet points capturing what happened in that event

Chapter titles should be specific and evocative — describe what HAPPENED in that scene, in 3-6 words.

Good examples:
  ## Goblin Ambush at Dawn
  ## Solving the Crypt Puzzle
  ## Negotiating with the Iron Tide
  ## Scouting the Forest
  ## The Cliffside Chase
  ## Rescuing Xu Tan
  ## The Vault of Verisi

Bad examples (too generic — DON'T do these):
  ## Combat
  ## Encounter
  ## Roleplay
  ## Exploration

Within each chapter, the bullets should cover what's relevant to THAT event: key actions, decisions, NPCs introduced, combat highlights, loot found, cliffhangers raised. Use specific character names from the roster — not "the party" if one PC did something.

Order chapters chronologically as events happened in the session.""",
    "bullets": """Write the journal as nested bullets organized by scene. Use bold scene headers. Each bullet should be at most 2 lines. Capture key story beats, combat highlights, NPCs introduced, loot, decisions made, and cliffhangers.""",
    "narrative": """Write the journal as prose chapters, past tense, third-person omniscient. Use two to four paragraphs per major scene. Capture key story beats, combat highlights, NPCs introduced, loot, decisions made, and cliffhangers.""",
    "structured": """Write the journal using Markdown sections: ## Combat, ## Roleplay & Social, ## Exploration & Discovery, ## Loot & Rewards, ## Cliffhangers & Open Threads. Use bullets within each section. Capture key story beats, combat highlights, NPCs introduced, loot, decisions made, and cliffhangers.""",
    "terse": """Write the journal as 10-20 bullets total, no embellishment, fact-only. Suitable for a 'Previously on...' recap. Capture key story beats, combat highlights, NPCs introduced, loot, decisions made, and cliffhangers.""",
}


def get_style_prompt(style: str) -> str:
    return STYLES.get(style, STYLES["bullets"])

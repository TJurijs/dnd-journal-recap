from recap_bot.prompts.styles import get_style_prompt, STYLES
from recap_bot.prompts.summarize import build_summarize_prompt


def test_styles_exist():
    for key in ("chapters", "bullets", "narrative", "structured", "terse"):
        assert key in STYLES
        assert len(get_style_prompt(key)) > 20


def test_summarize_prompt_with_context():
    prompt = build_summarize_prompt(
        campaign={"name": "Test", "premise": "A test premise"},
        roster="- Thorne (Player): Ranger\n- Nil (Player): Wizard",
        scratchpad="1. Session 1: Party met in tavern and fought goblins.",
        style="bullets",
        transcript="Speaker A: Hello\nSpeaker B: Hi",
    )
    assert "Test" in prompt
    assert "Thorne" in prompt
    assert "Session 1" in prompt
    assert "Speaker A" in prompt
    assert "bullets" in prompt.lower()


def test_summarize_prompt_first_session():
    prompt = build_summarize_prompt(
        campaign=None,
        roster=None,
        scratchpad=None,
        style="terse",
        transcript="...",
    )
    assert "10-20 bullets" in prompt.lower()

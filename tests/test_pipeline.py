import tempfile
from pathlib import Path

import pytest

from recap_bot.pipeline.download import (
    _TWITCH_RE,
    _YOUTUBE_RE,
    _is_youtube_bot_block,
    _youtube_cookies_path,
    _youtube_yt_dlp_opts,
    detect_source,
    get_vod_id,
)


# ----- Twitch URL regex -----

@pytest.mark.parametrize("url", [
    "https://www.twitch.tv/videos/123456789",
    "https://twitch.tv/somechannel/videos/987654321",
    "http://twitch.tv/videos/1",
])
def test_twitch_regex_matches(url):
    assert _TWITCH_RE.match(url)


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=123",   # 3-char id — too short for YouTube format
    "not a url",
    "https://twitch.tv/somechannel",     # no /videos/<id>
])
def test_twitch_regex_no_match(url):
    assert not _TWITCH_RE.match(url)


# ----- YouTube URL regex -----

@pytest.mark.parametrize("url,expected_id", [
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/watch?feature=player_embedded&v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/abcdefghijk", "abcdefghijk"),
    ("https://www.youtube.com/embed/_-ABcdEFghi", "_-ABcdEFghi"),
    ("https://www.youtube.com/shorts/ABC123_-xyz", "ABC123_-xyz"),
])
def test_youtube_regex_extracts_id(url, expected_id):
    m = _YOUTUBE_RE.match(url)
    assert m is not None
    assert m.group("id") == expected_id


@pytest.mark.parametrize("url", [
    "https://twitch.tv/videos/12345",   # Twitch URL — should not match YouTube
    "https://youtube.com/watch?v=tooShort",  # 8-char id, YouTube requires 11
    "https://youtu.be/short",
    "not a url",
])
def test_youtube_regex_no_match(url):
    assert not _YOUTUBE_RE.match(url)


# ----- detect_source dispatch -----

@pytest.mark.parametrize("url,expected", [
    ("https://www.twitch.tv/videos/2455113742", ("twitch", "2455113742")),
    ("https://twitch.tv/somechannel/videos/1", ("twitch", "1")),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
    ("https://youtu.be/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
])
def test_detect_source_known(url, expected):
    assert detect_source(url) == expected


@pytest.mark.parametrize("url", [
    "https://example.com/video/42",
    "https://twitch.tv/channel",  # missing /videos/<id>
    "ftp://twitch.tv/videos/1",   # wrong scheme
    "",
])
def test_detect_source_unknown(url):
    assert detect_source(url) is None


def test_get_vod_id_returns_id():
    assert get_vod_id("https://www.twitch.tv/videos/2455113742") == "2455113742"
    assert get_vod_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"


def test_get_vod_id_raises_for_unsupported():
    with pytest.raises(ValueError):
        get_vod_id("https://example.com/video/42")


# ----- YouTube auth (anti-bot) helpers -----

def test_youtube_yt_dlp_opts_includes_player_clients():
    """Less-gated player_clients listed in preference order so yt-dlp falls
    through them on datacenter IPs where the default 'web' client gets
    blocked."""
    opts = _youtube_yt_dlp_opts()
    clients = opts["extractor_args"]["youtube"]["player_client"]
    assert "mweb" in clients
    assert "web" in clients
    # 'mweb' should come before 'web' — it's historically less gated.
    assert clients.index("mweb") < clients.index("web")


def test_youtube_yt_dlp_opts_omits_cookies_when_file_absent(monkeypatch):
    from recap_bot.config import settings as live_settings
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(live_settings, "data_dir", Path(td))
        opts = _youtube_yt_dlp_opts()
        assert "cookiefile" not in opts


def test_youtube_yt_dlp_opts_attaches_cookies_when_file_present(monkeypatch):
    from recap_bot.config import settings as live_settings
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(live_settings, "data_dir", Path(td))
        cookies_path = _youtube_cookies_path()
        cookies_path.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        opts = _youtube_yt_dlp_opts()
        assert opts["cookiefile"] == str(cookies_path)


@pytest.mark.parametrize("message", [
    "ERROR: [youtube] xyz: Sign in to confirm you're not a bot. Use --cookies-from-browser",
    "ERROR: Please sign in to confirm you are not a bot.",
    "confirm you're not a bot",
])
def test_is_youtube_bot_block_detects_canonical_messages(message):
    assert _is_youtube_bot_block(Exception(message))


@pytest.mark.parametrize("message", [
    "ERROR: HTTP 404 Not Found",
    "Connection refused",
    "Video is private",
    "",
])
def test_is_youtube_bot_block_ignores_unrelated_errors(message):
    assert not _is_youtube_bot_block(Exception(message))


# ----- channel permission preflight -----

def test_preflight_flags_missing_attach_files():
    import discord
    from recap_bot.commands._helpers import _missing_perms, RECAP_REQUIRED_PERMS
    # journal-tribe case: can view/send/read but Attach Files denied
    perms = discord.Permissions(
        view_channel=True, read_message_history=True,
        send_messages=True, attach_files=False,
    )
    assert _missing_perms(perms, RECAP_REQUIRED_PERMS) == ["Attach Files"]


def test_preflight_passes_when_all_present():
    import discord
    from recap_bot.commands._helpers import _missing_perms, RECAP_REQUIRED_PERMS
    perms = discord.Permissions(
        view_channel=True, read_message_history=True,
        send_messages=True, attach_files=True,
    )
    assert _missing_perms(perms, RECAP_REQUIRED_PERMS) == []


def test_preflight_reports_multiple_missing_in_order():
    import discord
    from recap_bot.commands._helpers import _missing_perms, RECAP_REQUIRED_PERMS
    perms = discord.Permissions.none()  # nothing granted
    missing = _missing_perms(perms, RECAP_REQUIRED_PERMS)
    assert missing == ["View Channel", "Read Message History", "Send Messages", "Attach Files"]


def test_preflight_initialize_needs_only_view_and_history():
    import discord
    from recap_bot.commands._helpers import _missing_perms, INITIALIZE_REQUIRED_PERMS
    # No send/attach is fine for initialize (it only reads + DMs)
    perms = discord.Permissions(view_channel=True, read_message_history=True)
    assert _missing_perms(perms, INITIALIZE_REQUIRED_PERMS) == []
    # Missing history is flagged
    perms2 = discord.Permissions(view_channel=True)
    assert _missing_perms(perms2, INITIALIZE_REQUIRED_PERMS) == ["Read Message History"]


# ----- model-aware cost tracking -----

def test_price_table_loads_per_model_and_falls_back(tmp_path):
    from recap_bot.pipeline.cost import PriceTable
    p = tmp_path / "prices.yaml"
    p.write_text(
        "prices:\n"
        "  pro:\n    input: 2.0\n    output: 10.0\n"
        "  flash:\n    input: 0.1\n    output: 0.4\n"
        "default:\n  input: 0.15\n  output: 0.6\n",
        encoding="utf-8",
    )
    tbl = PriceTable(path=p)
    assert tbl.rates("pro") == (2.0, 10.0)
    assert tbl.rates("flash") == (0.1, 0.4)
    assert tbl.rates("some-unknown-model") == (0.15, 0.6)  # fallback


def test_price_table_tiered_above_threshold(tmp_path):
    """Gemini Pro charges more for prompts over 200k tokens — the tier must
    kick in based on a call's input token count."""
    from recap_bot.pipeline.cost import PriceTable
    p = tmp_path / "prices.yaml"
    p.write_text(
        "prices:\n"
        "  pro:\n"
        "    input: 2.0\n    output: 12.0\n"
        "    long_threshold: 200000\n    input_long: 4.0\n    output_long: 18.0\n",
        encoding="utf-8",
    )
    tbl = PriceTable(path=p)
    assert tbl.rates("pro", input_tokens=50_000) == (2.0, 12.0)    # base tier
    assert tbl.rates("pro", input_tokens=200_000) == (2.0, 12.0)   # at threshold → base
    assert tbl.rates("pro", input_tokens=250_000) == (4.0, 18.0)   # over → long tier


def test_cost_is_model_aware_and_totals_correctly(monkeypatch):
    from recap_bot.pipeline import cost

    # Install a known table: Pro is 20x Flash on input, 25x on output.
    tbl = cost.PriceTable.__new__(cost.PriceTable)
    tbl._prices = {
        "pro": {"input": 2.0, "output": 10.0},
        "flash": {"input": 0.1, "output": 0.4},
    }
    tbl._default = (0.15, 0.6)
    tbl._warned = set()
    monkeypatch.setattr(cost, "price_table", tbl)

    pro = cost.UsageInfo(1_000_000, 1_000_000, "pro")
    flash = cost.UsageInfo(1_000_000, 1_000_000, "flash")
    # Per-call cost uses each call's own model rate
    assert pro.cost_usd == 12.0          # 2.0 + 10.0
    assert abs(flash.cost_usd - 0.5) < 1e-9  # 0.1 + 0.4

    # The whole point: a mixed-model total sums each at its OWN rate, not a
    # single flat rate (which was the under-counting bug).
    t = cost.CostTracker()
    t.add(pro)
    t.add(flash)
    assert abs(t.total_cost_usd - 12.5) < 1e-9


# ----- model profiles (models.yaml) -----

def test_model_config_profiles(tmp_path):
    from recap_bot.config import ModelConfig
    p = tmp_path / "models.yaml"
    p.write_text(
        "profiles:\n"
        "  default:\n    summarize: pro\n    transcribe: flash-lite\n"
        "  lite:\n    summarize: flash\n",
        encoding="utf-8",
    )
    mc = ModelConfig(path=p)
    assert mc.profile_names()[0] == "default"          # default always first
    assert "lite" in mc.profile_names()
    assert mc.get("summarize") == "pro"                # default profile
    assert mc.get("summarize", "lite") == "flash"      # lite overrides
    assert mc.get("transcribe", "lite") == "flash-lite"  # missing key → default profile
    assert mc.has_profile("lite")
    assert not mc.has_profile("nope")


def test_model_config_unknown_profile_falls_back_to_default(tmp_path):
    from recap_bot.config import ModelConfig
    p = tmp_path / "models.yaml"
    p.write_text("profiles:\n  default:\n    summarize: pro\n", encoding="utf-8")
    mc = ModelConfig(path=p)
    assert mc.get("summarize", "does-not-exist") == "pro"


def test_model_config_backcompat_old_models_block(tmp_path):
    from recap_bot.config import ModelConfig
    p = tmp_path / "models.yaml"
    p.write_text("models:\n  summarize: oldpro\n", encoding="utf-8")
    mc = ModelConfig(path=p)
    assert mc.profile_names() == ["default"]
    assert mc.get("summarize") == "oldpro"  # old top-level `models:` → default profile


# ----- transient-error retry (llm.generate_content) -----

class _FakeGenAIClient:
    """Stands in for genai.Client: .models.generate_content raises `code` the
    first `fail_times` calls, then returns a sentinel."""

    def __init__(self, fail_times: int, code: int):
        self._fail = fail_times
        self._code = code
        self.calls = 0
        self.models = self

    def generate_content(self, *, model, contents, **kwargs):
        self.calls += 1
        if self.calls <= self._fail:
            exc = Exception(f"{self._code} ERROR")
            exc.code = self._code
            raise exc
        return "OK-RESPONSE"


@pytest.mark.asyncio
async def test_llm_retries_then_succeeds():
    from recap_bot.pipeline import llm
    c = _FakeGenAIClient(fail_times=2, code=500)
    r = await llm.generate_content(c, model="m", contents="x", max_attempts=4, base_delay=0)
    assert r == "OK-RESPONSE"
    assert c.calls == 3  # failed twice, succeeded on the third


@pytest.mark.asyncio
async def test_llm_no_retry_on_client_error():
    from recap_bot.pipeline import llm
    c = _FakeGenAIClient(fail_times=5, code=400)  # 400 is not transient
    with pytest.raises(Exception):
        await llm.generate_content(c, model="m", contents="x", max_attempts=4, base_delay=0)
    assert c.calls == 1  # raised immediately, no retry


@pytest.mark.asyncio
async def test_llm_gives_up_after_max_attempts():
    from recap_bot.pipeline import llm
    c = _FakeGenAIClient(fail_times=99, code=503)
    with pytest.raises(Exception):
        await llm.generate_content(c, model="m", contents="x", max_attempts=3, base_delay=0)
    assert c.calls == 3  # tried exactly max_attempts times


def test_resolve_category_returns_id_and_name():
    from recap_bot.commands._helpers import resolve_category

    class _Cat:
        id = 555
        name = "Starry Knights"

    class _Channel:
        category = _Cat()

    class _Interaction:
        channel = _Channel()

    assert resolve_category(_Interaction()) == (555, "Starry Knights")


def test_resolve_category_none_when_uncategorized():
    from recap_bot.commands._helpers import resolve_category

    class _Channel:
        category = None

    class _Interaction:
        channel = _Channel()

    assert resolve_category(_Interaction()) is None


def test_resolve_category_none_for_dm():
    from recap_bot.commands._helpers import resolve_category

    # A DM channel has no `category` attribute at all → treated as uncategorized.
    class _DMChannel:
        pass

    class _Interaction:
        channel = _DMChannel()

    assert resolve_category(_Interaction()) is None


def test_extract_usage_tags_model():
    from recap_bot.pipeline.cost import extract_usage

    class _Meta:
        prompt_token_count = 100
        candidates_token_count = 50

    class _Resp:
        usage_metadata = _Meta()

    u = extract_usage(_Resp(), "gemini-3.1-pro-preview")
    assert u is not None
    assert u.input_tokens == 100
    assert u.output_tokens == 50
    assert u.model == "gemini-3.1-pro-preview"


def test_cost_tracker_add_list_prices_each_call_at_its_own_model():
    """Mixed-model recoveries (e.g. retry on `high`) must price each call at
    its OWN model's rate. Summing UsageInfos via __add__ first drops the
    per-call model tag and under-counts the more-expensive call's tokens.

    Regression test for the under-counting bug found while reviewing recovery
    cost accounting: transcribe_chunk was returning `usage_a + usage_b` for
    a chunk that retried on a different model; the combined UsageInfo kept
    only the first model's tag and CostTracker priced ALL tokens at that
    (cheaper) model's rate.
    """
    from recap_bot.pipeline.cost import CostTracker, UsageInfo

    # Two calls on two different models — typical mixed-model retry case.
    default_call = UsageInfo(input_tokens=10_000, output_tokens=5_000,
                             model="gemini-2.5-flash-lite")
    high_call = UsageInfo(input_tokens=10_000, output_tokens=5_000,
                          model="gemini-3.1-flash-lite")

    # Buggy behaviour we're guarding against: summing first, then adding the
    # combined object — would price all 20k input + 10k output at 2.5-lite
    # rates because __add__ keeps the first model's tag.
    buggy = default_call + high_call
    tracker_buggy = CostTracker()
    tracker_buggy.add(buggy)

    # Correct behaviour: add the list, CostTracker unpacks and prices each.
    tracker_correct = CostTracker()
    tracker_correct.add([default_call, high_call])

    # Each call separately, for the ground-truth reference.
    tracker_ref = CostTracker()
    tracker_ref.add(default_call)
    tracker_ref.add(high_call)

    # Correct path matches the reference exactly.
    assert tracker_correct.total_cost_usd == pytest.approx(tracker_ref.total_cost_usd)

    # And the correct path costs MORE than the buggy path — high-lite is
    # priced higher than 2.5-lite, so giving it its proper rate increases
    # the total. (If somebody re-introduces the bug, this assertion fires.)
    assert tracker_correct.total_cost_usd > tracker_buggy.total_cost_usd


def test_cost_tracker_add_handles_none_and_empty_list():
    """Edge cases — None and empty-list inputs shouldn't error or change total."""
    from recap_bot.pipeline.cost import CostTracker
    t = CostTracker()
    t.add(None)
    t.add([])
    t.add([None, None])
    assert t.total_cost_usd == 0.0


# ----- Journal length cap: _trim_to_section_boundary -----

def _make_long_journal(n_scenes: int = 8, words_per_bullet: int = 80) -> str:
    title = "# Splitlanders, Session 238\n\n"
    sdate = "## Session Date: 12th of Winter\n\n"
    body = " ".join(f"word{j}" for j in range(words_per_bullet))
    scenes = [f"## Scene {i}\n- {body}\n- {body}\n" for i in range(1, n_scenes + 1)]
    return title + sdate + "\n".join(scenes)


def test_trim_to_section_boundary_caps_length():
    from recap_bot.pipeline.summarize import _trim_to_section_boundary
    journal = _make_long_journal()
    assert len(journal) > 4000  # sanity
    trimmed = _trim_to_section_boundary(journal, 4000)
    assert len(trimmed) <= 4000
    # Cut at a `## ` boundary → no half-scene left dangling; marker appended.
    assert trimmed.rstrip().endswith("post)_")
    assert "## Session Date" in trimmed  # leading context preserved
    assert trimmed.startswith("# Splitlanders")


def test_trim_to_section_boundary_under_limit_is_identity():
    from recap_bot.pipeline.summarize import _trim_to_section_boundary
    short = "# T\n\n## Scene 1\n- short content here"
    assert _trim_to_section_boundary(short, 4000) == short


def test_trim_to_section_boundary_hard_cuts_oversized_first_section():
    from recap_bot.pipeline.summarize import _trim_to_section_boundary
    # First (only) section alone exceeds the budget → fall back to hard cut.
    huge = "## Scene 1\n" + ("x" * 5000)
    trimmed = _trim_to_section_boundary(huge, 4000)
    assert len(trimmed) <= 4000


# ----- Embed body extraction + builders -----

def test_body_for_embed_strips_leading_title():
    from recap_bot.storage.discord_journals import _body_for_embed
    journal = "# Session 238\n\n## Session Date: Winter\n\n## Scene 1\n- thing happened"
    body = _body_for_embed(journal)
    assert not body.startswith("# Session 238")
    assert body.startswith("## Session Date")


def test_body_for_embed_no_title_unchanged():
    from recap_bot.storage.discord_journals import _body_for_embed
    journal = "## Session Date: Winter\n\n## Scene 1\n- thing"
    assert _body_for_embed(journal).startswith("## Session Date")


def test_render_embed_description_within_discord_limit():
    from recap_bot.storage.discord_journals import render_journal_embed
    journal = "# T\n\n" + ("## Scene\n- " + "x" * 200 + "\n") * 30
    embed = render_journal_embed(journal, date="Winter")
    assert len(embed.description) <= 4096
    assert embed.footer.text == "Winter"


def test_codeblock_embed_wraps_in_md_fence_and_fits():
    from recap_bot.storage.discord_journals import codeblock_journal_embed
    journal = "# T\n\n## Scene 1\n- thing happened in the tavern"
    embed = codeblock_journal_embed(journal)
    assert embed.description.startswith("```md\n")
    assert embed.description.rstrip().endswith("```")
    assert len(embed.description) <= 4096
    # The raw markdown syntax must survive inside the code block.
    assert "## Scene 1" in embed.description


def test_codeblock_embed_respects_description_limit_on_huge_input():
    from recap_bot.storage.discord_journals import codeblock_journal_embed
    journal = "# T\n\n" + ("## Scene\n- " + "x" * 200 + "\n") * 40  # way over 4096
    embed = codeblock_journal_embed(journal)
    assert len(embed.description) <= 4096
    assert embed.description.startswith("```md\n")
    assert embed.description.rstrip().endswith("```")


# ----- Re-ingestion: reading the journal back out of an embed -----

class _FakeEmbed:
    def __init__(self, description):
        self.description = description


class _FakeMsg:
    def __init__(self, embeds):
        self.embeds = embeds


def test_extract_embed_body_rendered():
    """A standard (rendered) embed's description is returned verbatim."""
    from recap_bot.storage.discord_journals import _extract_embed_body
    body = "## Session Date: Winter\n\n## Scene 1\n- thing happened"
    msg = _FakeMsg([_FakeEmbed(body)])
    assert _extract_embed_body(msg) == body


def test_extract_embed_body_strips_codeblock_fence():
    """A silent (code-block) embed's ```md fence is stripped on read-back."""
    from recap_bot.storage.discord_journals import _extract_embed_body
    body = "## Scene 1\n- thing happened\n- and another"
    msg = _FakeMsg([_FakeEmbed(f"```md\n{body}\n```")])
    assert _extract_embed_body(msg) == body


def test_extract_embed_body_no_embed_or_empty():
    from recap_bot.storage.discord_journals import _extract_embed_body
    assert _extract_embed_body(_FakeMsg([])) == ""
    assert _extract_embed_body(_FakeMsg([_FakeEmbed("")])) == ""
    assert _extract_embed_body(_FakeMsg([_FakeEmbed(None)])) == ""


def test_roundtrip_embed_body_recovers_journal():
    """Build a rendered embed from a journal, then read it back — the body
    (minus the title, which lives in the message content header) survives."""
    from recap_bot.storage.discord_journals import (
        render_journal_embed,
        _extract_embed_body,
        _body_for_embed,
    )
    journal = "# Session 99\n\n## Session Date: Winter\n\n## Scene 1\n- the party fought a dragon"
    embed = render_journal_embed(journal)
    msg = _FakeMsg([_FakeEmbed(embed.description)])
    recovered = _extract_embed_body(msg)
    assert recovered == _body_for_embed(journal)
    assert "## Scene 1" in recovered
    assert "the party fought a dragon" in recovered


# ----- _looks_repetitive: tells runaway loops from legit-long content -----

def _make_clean_transcript(n_lines: int = 60) -> str:
    """Synthesize a transcript with no repetition — every line unique."""
    return "\n".join(
        f"[00:{i // 60:02d}:{i % 60:02d}] Speaker {chr(65 + (i % 4))}: "
        f"Discussing topic {i} which involves character {chr(65 + (i % 26))} "
        f"taking action {i * 7 % 100} in the {['tavern', 'dungeon', 'forest', 'castle'][i % 4]}."
        for i in range(n_lines)
    )


def test_looks_repetitive_clean_long_transcript_returns_false():
    """A normal long transcript with varied content should NOT register as a loop."""
    from recap_bot.pipeline.transcribe import _looks_repetitive
    text = _make_clean_transcript(100)
    assert len(text) > 5000  # sanity: it's long enough to be interesting
    assert _looks_repetitive(text) is False


def test_looks_repetitive_detects_sentence_level_loop():
    """The chunk_008 failure mode: same sentence emitted repeatedly with
    different timestamp prefixes, all on one line (no newlines).
    """
    from recap_bot.pipeline.transcribe import _looks_repetitive
    # Realistic varied lead-in, then a 50× repeat of the same sentence.
    head = " ".join(
        f"[00:00:{i:02d}] Speaker A: Different content at time {i} about topic {i}."
        for i in range(30)
    )
    loop_sentence = "I was short. I didn't bring enough gold to buy what I wanted. "
    loop = " ".join(f"[00:09:{i:02d}] Speaker A: {loop_sentence}" for i in range(50))
    text = head + " " + loop
    assert _looks_repetitive(text) is True


def test_looks_repetitive_short_text_returns_false():
    """Heuristic should bail (False) for too-short input — too few windows
    to be reliable."""
    from recap_bot.pipeline.transcribe import _looks_repetitive
    assert _looks_repetitive("hello world") is False
    assert _looks_repetitive("") is False
    assert _looks_repetitive("a" * 100) is False  # under min_text_len


def test_looks_repetitive_mostly_unique_with_occasional_repetition():
    """Mild natural repetition (common phrases, e.g. 'Yeah.') should NOT
    flag as a loop — threshold is 50% of tail windows."""
    from recap_bot.pipeline.transcribe import _looks_repetitive
    base = _make_clean_transcript(80)
    # Sprinkle a few "Yeah." lines among the unique content — these would
    # match each other but only contribute a tiny fraction of windows.
    extra = "\n".join("[00:30:00] Speaker B: Yeah." for _ in range(5))
    assert _looks_repetitive(base + "\n" + extra) is False

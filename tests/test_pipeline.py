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

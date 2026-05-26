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

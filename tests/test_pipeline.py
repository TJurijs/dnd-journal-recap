import pytest

from recap_bot.pipeline.download import _TWITCH_RE


@pytest.mark.parametrize("url", [
    "https://www.twitch.tv/videos/123456789",
    "https://twitch.tv/somechannel/videos/987654321",
    "http://twitch.tv/videos/1",
])
def test_twitch_regex_matches(url):
    assert _TWITCH_RE.match(url)


@pytest.mark.parametrize("url", [
    "https://youtube.com/watch?v=123",
    "not a url",
    "https://twitch.tv/somechannel",
])
def test_twitch_regex_no_match(url):
    assert not _TWITCH_RE.match(url)

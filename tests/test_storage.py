"""Tests for the file-based storage and in-memory job state."""

import tempfile
from pathlib import Path

import pytest

from recap_bot.pipeline import state
from recap_bot.storage import files as channel_files


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr(channel_files.settings, "data_dir", Path(tmpdir))
    # Clear in-memory job state between tests
    state._active.clear()
    channel_files._channel_locks.clear()
    yield Path(tmpdir)


# ----- meta.yaml -----

@pytest.mark.asyncio
async def test_meta_round_trip(isolated_data_dir):
    assert await channel_files.read_meta(42) is None
    merged = await channel_files.write_meta(42, guild_id=999, name="Strahd", style="narrative")
    assert merged["channel_id"] == 42
    assert merged["guild_id"] == 999

    read = await channel_files.read_meta(42)
    assert read["guild_id"] == 999
    assert read["name"] == "Strahd"
    assert read["style"] == "narrative"
    assert read["channel_id"] == 42  # derived, not stored


@pytest.mark.asyncio
async def test_meta_merge_preserves_existing(isolated_data_dir):
    await channel_files.write_meta(42, guild_id=999, name="Strahd")
    await channel_files.write_meta(42, style="terse")
    meta = await channel_files.read_meta(42)
    assert meta["name"] == "Strahd"
    assert meta["style"] == "terse"


@pytest.mark.asyncio
async def test_get_style_default(isolated_data_dir):
    assert await channel_files.get_style(42) == channel_files.DEFAULT_STYLE
    await channel_files.write_meta(42, style="narrative")
    assert await channel_files.get_style(42) == "narrative"


# ----- /initialize writes -----

@pytest.mark.asyncio
async def test_initialize_writes_to_initialize_subfolder(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "- Alice (Player)")
    await channel_files.write_initialize_scratchpad(42, "Session 1")
    assert (isolated_data_dir / "channels" / "42" / "initialize" / "roster.md").exists()
    assert (isolated_data_dir / "channels" / "42" / "initialize" / "scratchpad.md").exists()


# ----- current roster/scratchpad chain (latest recap → initialize → legacy) -----

@pytest.mark.asyncio
async def test_read_falls_back_to_initialize_when_no_recaps(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "init roster")
    await channel_files.write_initialize_scratchpad(42, "init scratchpad")
    assert (await channel_files.read_roster(42)) == "init roster"
    assert (await channel_files.read_scratchpad(42)) == "init scratchpad"


@pytest.mark.asyncio
async def test_read_prefers_latest_recap(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "init roster")
    await channel_files.write_initialize_scratchpad(42, "init scratchpad")

    # Make a recap dir manually with the new seq-prefixed naming
    recap_dir = isolated_data_dir / "channels" / "42" / "recaps" / "0001_111"
    (recap_dir / "chunks").mkdir(parents=True)
    (recap_dir / "roster.md").write_text("recap1 roster", encoding="utf-8")
    (recap_dir / "scratchpad.md").write_text("recap1 scratchpad", encoding="utf-8")

    assert (await channel_files.read_roster(42)) == "recap1 roster"
    assert (await channel_files.read_scratchpad(42)) == "recap1 scratchpad"

    # A later recap takes over
    later = isolated_data_dir / "channels" / "42" / "recaps" / "0002_222"
    (later / "chunks").mkdir(parents=True)
    (later / "roster.md").write_text("recap2 roster", encoding="utf-8")
    (later / "scratchpad.md").write_text("recap2 scratchpad", encoding="utf-8")

    assert (await channel_files.read_roster(42)) == "recap2 roster"


@pytest.mark.asyncio
async def test_read_falls_back_to_legacy_root(isolated_data_dir):
    """Pre-restructure layout: roster.md at channel root."""
    root = isolated_data_dir / "channels" / "42"
    root.mkdir(parents=True)
    (root / "roster.md").write_text("legacy roster", encoding="utf-8")
    (root / "scratchpad.md").write_text("legacy scratchpad", encoding="utf-8")
    assert (await channel_files.read_roster(42)) == "legacy roster"
    assert (await channel_files.read_scratchpad(42)) == "legacy scratchpad"


# ----- has_context / clear -----

@pytest.mark.asyncio
async def test_has_context_requires_both_via_initialize(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "x")
    assert not await channel_files.has_context(42)
    await channel_files.write_initialize_scratchpad(42, "y")
    assert await channel_files.has_context(42)


@pytest.mark.asyncio
async def test_empty_strings_count_as_context(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "")
    await channel_files.write_initialize_scratchpad(42, "")
    assert await channel_files.has_context(42)


@pytest.mark.asyncio
async def test_clear_context_removes_initialize_files(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "stuff")
    await channel_files.write_initialize_scratchpad(42, "stuff")
    await channel_files.clear_context(42)
    assert not await channel_files.has_context(42)


# ----- recap dirs + cache -----

def test_make_or_reuse_recap_dir_new(isolated_data_dir):
    d = channel_files.make_or_reuse_recap_dir(42, "12345")
    assert d.exists()
    assert (d / "chunks").exists()
    assert d.name == "0001_12345"  # seq 1, vod 12345


def test_make_or_reuse_recap_dir_re_recap_reuses_folder(isolated_data_dir):
    """Re-recap of the same VOD reuses the original folder + seq."""
    a = channel_files.make_or_reuse_recap_dir(42, "111")
    b = channel_files.make_or_reuse_recap_dir(42, "222")
    assert a.name == "0001_111"
    assert b.name == "0002_222"

    # Re-recap of vod 111 should return the same folder, not a new seq
    a_again = channel_files.make_or_reuse_recap_dir(42, "111")
    assert a_again == a
    assert a_again.name == "0001_111"


def test_list_recap_dirs_sorts_by_seq(isolated_data_dir):
    a = isolated_data_dir / "channels" / "42" / "recaps" / "0001_111"
    b = isolated_data_dir / "channels" / "42" / "recaps" / "0002_222"
    for d in (a, b):
        (d / "chunks").mkdir(parents=True)
    dirs = channel_files.list_recap_dirs(42)
    assert [p.name for p in dirs] == [a.name, b.name]
    assert channel_files.latest_recap_dir(42) == b


def test_prior_recap_dir_for_new_vod_returns_latest(isolated_data_dir):
    """A brand-new VOD chains from the latest existing recap."""
    a = channel_files.make_or_reuse_recap_dir(42, "111")
    b = channel_files.make_or_reuse_recap_dir(42, "222")
    # Asking about a NEW vod returns the latest existing
    assert channel_files.prior_recap_dir(42, "999") == b


def test_prior_recap_dir_for_re_recap_returns_one_before(isolated_data_dir):
    """Re-recapping an existing VOD chains from the recap before it."""
    a = channel_files.make_or_reuse_recap_dir(42, "111")
    b = channel_files.make_or_reuse_recap_dir(42, "222")
    c = channel_files.make_or_reuse_recap_dir(42, "333")
    # Re-recap of 222: prior should be 111
    assert channel_files.prior_recap_dir(42, "222") == a
    # Re-recap of the FIRST recap: no prior (caller falls back to initialize/)
    assert channel_files.prior_recap_dir(42, "111") is None


@pytest.mark.asyncio
async def test_read_context_for_recap_chains_from_prior(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "init roster")
    await channel_files.write_initialize_scratchpad(42, "init scratchpad")

    a = channel_files.make_or_reuse_recap_dir(42, "111")
    (a / "roster.md").write_text("game 51 roster", encoding="utf-8")
    (a / "scratchpad.md").write_text("game 51 scratchpad", encoding="utf-8")

    b = channel_files.make_or_reuse_recap_dir(42, "222")
    (b / "roster.md").write_text("game 52 roster", encoding="utf-8")
    (b / "scratchpad.md").write_text("game 52 scratchpad", encoding="utf-8")

    # New VOD chains from latest (game 52)
    r, s = await channel_files.read_context_for_recap(42, "999")
    assert r == "game 52 roster"
    assert s == "game 52 scratchpad"

    # Re-recap of game 52 chains from game 51 (the prior)
    r, s = await channel_files.read_context_for_recap(42, "222")
    assert r == "game 51 roster"
    assert s == "game 51 scratchpad"

    # Re-recap of the FIRST game chains from initialize/
    r, s = await channel_files.read_context_for_recap(42, "111")
    assert r == "init roster"
    assert s == "init scratchpad"


# ----- journal cache -----

@pytest.mark.asyncio
async def test_journal_cache_round_trip(isolated_data_dir):
    await channel_files.write_journal_cache(42, 7, "# Session 7\nbody")
    assert (await channel_files.read_journal_cache(42, 7)).startswith("# Session 7")


# ----- recap message id (for /recap_edit in-place attachment swap) -----

def test_recap_message_id_round_trip(isolated_data_dir):
    recap_dir = channel_files.make_or_reuse_recap_dir(42, "vod1")
    assert channel_files.read_recap_message_id(recap_dir) is None
    channel_files.write_recap_message_id(recap_dir, 123456789012345678)
    assert channel_files.read_recap_message_id(recap_dir) == 123456789012345678


def test_recap_message_id_overwrites(isolated_data_dir):
    """Re-recap (or any rewrite) should replace the stored id, not append."""
    recap_dir = channel_files.make_or_reuse_recap_dir(42, "vod1")
    channel_files.write_recap_message_id(recap_dir, 111)
    channel_files.write_recap_message_id(recap_dir, 222)
    assert channel_files.read_recap_message_id(recap_dir) == 222


def test_recap_message_id_returns_none_for_garbage(isolated_data_dir):
    """Malformed file shouldn't crash the caller — degrade to 'no id known'."""
    recap_dir = channel_files.make_or_reuse_recap_dir(42, "vod1")
    (recap_dir / "discord_msg_id.txt").write_text("not-an-int", encoding="utf-8")
    assert channel_files.read_recap_message_id(recap_dir) is None


# ----- atomic write -----

@pytest.mark.asyncio
async def test_atomic_write_no_temp_leftover(isolated_data_dir):
    await channel_files.write_initialize_roster(42, "first")
    await channel_files.write_initialize_roster(42, "second")
    init_dir = isolated_data_dir / "channels" / "42" / "initialize"
    tmp_files = list(init_dir.glob(".tmp-*"))
    assert tmp_files == []
    assert (await channel_files.read_roster(42)) == "second"


# ----- in-memory job state -----

def test_claim_release_lifecycle():
    job = state.claim(
        channel_id=42, guild_id=1, requested_by=100,
        source_type="twitch", source_ref="https://twitch.tv/videos/1", style="bullets",
    )
    assert job is not None
    assert state.get(42) is job

    # Second claim on the same channel is rejected
    second = state.claim(
        channel_id=42, guild_id=1, requested_by=100,
        source_type="twitch", source_ref="https://twitch.tv/videos/1", style="bullets",
    )
    assert second is None

    state.release(42)
    assert state.get(42) is None


def test_cancel_marks_flag():
    state.claim(
        channel_id=42, guild_id=1, requested_by=100,
        source_type="twitch", source_ref="x", style="bullets",
    )
    assert state.cancel(42) is True
    assert state.get(42).cancelled is True
    state.release(42)


def test_cancel_nonexistent_returns_false():
    assert state.cancel(9999) is False


# ----- /settings helpers -----

def test_settings_mask_short():
    from recap_bot.commands.settings import _mask
    assert _mask("") == "(unset)"
    assert _mask("short") == "*****"
    assert _mask("abcdefgh") == "********"  # exactly 8 → full mask


def test_settings_mask_long():
    from recap_bot.commands.settings import _mask
    assert _mask("AIzaSyABC1234567890DEF") == "AIza…0DEF"


def test_write_override_roundtrip(isolated_data_dir, monkeypatch):
    from recap_bot.commands.settings import _write_override
    from recap_bot import config

    # _write_override uses runtime_config_path() which uses settings.data_dir;
    # the isolated_data_dir fixture already patches that.
    _write_override("gemini_api_key", "new-test-key")
    _write_override("gemini_model", "gemini-3.1-pro-preview")

    path = config.runtime_config_path()
    assert path.exists()

    import yaml
    saved = yaml.safe_load(path.read_text())
    assert saved == {"gemini_api_key": "new-test-key", "gemini_model": "gemini-3.1-pro-preview"}


def test_write_override_rejects_non_overridable(isolated_data_dir):
    from recap_bot.commands.settings import _write_override
    import pytest
    with pytest.raises(ValueError):
        _write_override("discord_bot_token", "secret")  # not in RUNTIME_OVERRIDABLE

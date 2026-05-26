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
async def test_write_roster_writes_canonical_channel_root(isolated_data_dir):
    """Single-canonical layout: roster + scratchpad live at the channel root,
    not in initialize/ or per-recap subdirs."""
    await channel_files.write_roster(42, "- Alice (Player)")
    await channel_files.write_scratchpad(42, "Session 1")
    assert (isolated_data_dir / "channels" / "42" / "roster.md").exists()
    assert (isolated_data_dir / "channels" / "42" / "scratchpad.md").exists()
    # And initialize/ subdir is NOT created — that path is legacy-only.
    assert not (isolated_data_dir / "channels" / "42" / "initialize" / "roster.md").exists()


# ----- current roster/scratchpad chain (canonical → recap snapshot → initialize/) -----

@pytest.mark.asyncio
async def test_read_returns_canonical_when_present(isolated_data_dir):
    await channel_files.write_roster(42, "canonical roster")
    await channel_files.write_scratchpad(42, "canonical scratchpad")
    assert (await channel_files.read_roster(42)) == "canonical roster"
    assert (await channel_files.read_scratchpad(42)) == "canonical scratchpad"


@pytest.mark.asyncio
async def test_read_falls_back_to_recap_snapshot_when_no_canonical(isolated_data_dir):
    """Pre-refactor channels stored roster.md inside each per-recap dir.
    Reading them keeps existing campaigns working without manual migration —
    the next /recap will materialize the canonical file."""
    recap_dir = isolated_data_dir / "channels" / "42" / "recaps" / "0001_111"
    (recap_dir / "chunks").mkdir(parents=True)
    (recap_dir / "roster.md").write_text("recap1 roster", encoding="utf-8")
    (recap_dir / "scratchpad.md").write_text("recap1 scratchpad", encoding="utf-8")

    assert (await channel_files.read_roster(42)) == "recap1 roster"
    assert (await channel_files.read_scratchpad(42)) == "recap1 scratchpad"

    # A later recap takes over (newest-first walk)
    later = isolated_data_dir / "channels" / "42" / "recaps" / "0002_222"
    (later / "chunks").mkdir(parents=True)
    (later / "roster.md").write_text("recap2 roster", encoding="utf-8")
    (later / "scratchpad.md").write_text("recap2 scratchpad", encoding="utf-8")

    assert (await channel_files.read_roster(42)) == "recap2 roster"


@pytest.mark.asyncio
async def test_canonical_beats_recap_snapshot_in_priority(isolated_data_dir):
    """If both exist (e.g. mid-migration), canonical wins."""
    recap_dir = isolated_data_dir / "channels" / "42" / "recaps" / "0001_111"
    (recap_dir / "chunks").mkdir(parents=True)
    (recap_dir / "roster.md").write_text("stale recap snapshot", encoding="utf-8")
    await channel_files.write_roster(42, "canonical wins")
    assert (await channel_files.read_roster(42)) == "canonical wins"


@pytest.mark.asyncio
async def test_read_falls_back_to_initialize_subdir_legacy(isolated_data_dir):
    """Pre-refactor /initialize wrote into initialize/ subdir. Still readable."""
    init_dir = isolated_data_dir / "channels" / "42" / "initialize"
    init_dir.mkdir(parents=True)
    (init_dir / "roster.md").write_text("legacy init roster", encoding="utf-8")
    (init_dir / "scratchpad.md").write_text("legacy init scratchpad", encoding="utf-8")
    assert (await channel_files.read_roster(42)) == "legacy init roster"
    assert (await channel_files.read_scratchpad(42)) == "legacy init scratchpad"


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


# ----- /roster action:delete / /scratchpad action:delete (clear_*) -----

@pytest.mark.asyncio
async def test_clear_roster_deletes_canonical(isolated_data_dir):
    await channel_files.write_roster(42, "canonical roster")
    deleted = await channel_files.clear_roster(42)
    assert deleted is not None
    assert deleted.name == "roster.md"
    # Canonical lives at the channel root, not in any subdir.
    assert deleted.parent.name == "42"
    assert (await channel_files.read_roster(42)) is None


@pytest.mark.asyncio
async def test_clear_roster_deletes_recap_snapshot_when_present(isolated_data_dir):
    """Regression: previously /roster delete only touched initialize/, so a
    user looking at a recap-snapshot roster saw 'No roster to delete' even
    though /roster show clearly returned content. Now delete removes what
    /roster show actually displays."""
    recap_dir = channel_files.make_or_reuse_recap_dir(42, "vodA")
    (recap_dir / "roster.md").write_text("from recap snapshot", encoding="utf-8")
    # initialize/ may or may not exist; the recap snapshot wins via priority
    deleted = await channel_files.clear_roster(42)
    assert deleted is not None
    assert "vodA" in str(deleted)
    assert (await channel_files.read_roster(42)) is None


@pytest.mark.asyncio
async def test_clear_roster_peels_through_snapshots_iteratively(isolated_data_dir):
    """Multiple recap snapshots: each delete removes the latest, the next
    show falls through to the previous one. This is the intentional
    iterative-cleanup workflow."""
    for i, content in enumerate(["first", "second", "third"], 1):
        recap_dir = channel_files.make_or_reuse_recap_dir(42, f"vod{i}")
        (recap_dir / "roster.md").write_text(content, encoding="utf-8")
    assert (await channel_files.read_roster(42)) == "third"
    await channel_files.clear_roster(42)
    assert (await channel_files.read_roster(42)) == "second"
    await channel_files.clear_roster(42)
    assert (await channel_files.read_roster(42)) == "first"
    await channel_files.clear_roster(42)
    assert (await channel_files.read_roster(42)) is None
    # And once everything's gone, returns None instead of looping forever.
    assert (await channel_files.clear_roster(42)) is None


@pytest.mark.asyncio
async def test_clear_roster_returns_none_when_nothing_anywhere(isolated_data_dir):
    deleted = await channel_files.clear_roster(42)
    assert deleted is None


@pytest.mark.asyncio
async def test_clear_scratchpad_deletes_displayed_source(isolated_data_dir):
    """Mirror of clear_roster — same semantic for the scratchpad."""
    recap_dir = channel_files.make_or_reuse_recap_dir(42, "vodA")
    (recap_dir / "scratchpad.md").write_text("from recap", encoding="utf-8")
    deleted = await channel_files.clear_scratchpad(42)
    assert deleted is not None
    assert "vodA" in str(deleted)
    assert (await channel_files.read_scratchpad(42)) is None


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
async def test_read_context_for_recap_returns_canonical(isolated_data_dir):
    """With the single-canonical layout, every /recap reads the same channel-
    wide roster + scratchpad. No per-recap-snapshot chaining: the LLM's
    update step is what merges the new session's information into the
    accumulating roster."""
    await channel_files.write_roster(42, "canonical roster")
    await channel_files.write_scratchpad(42, "canonical scratchpad")
    r, s = await channel_files.read_context_for_recap(42)
    assert r == "canonical roster"
    assert s == "canonical scratchpad"


@pytest.mark.asyncio
async def test_read_context_for_recap_empty_when_nothing(isolated_data_dir):
    """Empty channel + no prior state: pipeline gets ("", "") so the LLM
    starts from scratch."""
    r, s = await channel_files.read_context_for_recap(42)
    assert r == ""
    assert s == ""


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
    await channel_files.write_roster(42, "first")
    await channel_files.write_roster(42, "second")
    channel_root = isolated_data_dir / "channels" / "42"
    tmp_files = list(channel_root.glob(".tmp-*"))
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


# ----- multi-guild DISCORD_GUILD_ID parsing -----

def test_guild_ids_empty():
    from recap_bot.config import Settings
    s = Settings(discord_bot_token="x", gemini_api_key="y", discord_guild_id="")
    assert s.guild_ids == []
    assert s.guild_id_as_int is None


def test_guild_ids_single():
    from recap_bot.config import Settings
    s = Settings(discord_bot_token="x", gemini_api_key="y", discord_guild_id="12345")
    assert s.guild_ids == [12345]
    assert s.guild_id_as_int == 12345  # back-compat: first id


def test_guild_ids_comma_separated():
    from recap_bot.config import Settings
    s = Settings(discord_bot_token="x", gemini_api_key="y", discord_guild_id="111, 222,333")
    assert s.guild_ids == [111, 222, 333]
    assert s.guild_id_as_int == 111  # back-compat: first id


def test_guild_ids_ignores_garbage_parts():
    from recap_bot.config import Settings
    s = Settings(discord_bot_token="x", gemini_api_key="y", discord_guild_id="111,notanid,222")
    assert s.guild_ids == [111, 222]


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

"""DM-only /check command: sanity-check that everything is set up.

Replaces the old /ping and /ping_llm. Tests:
  - Discord connection (the bot is responding to the command at all = pass)
  - Configured guild is reachable
  - Data directory exists and is writable
  - Gemini API key works (one short call)
  - All configured models load without error (no API call per model — just config)
  - ffmpeg + yt-dlp binaries are on PATH
"""

import asyncio
import logging
import shutil
import tempfile
import time

import discord
from discord import app_commands
from google import genai

from recap_bot.bot import bot
from recap_bot.commands._helpers import (
    MANAGE_CHANNELS_REQUIRED_MSG,
    user_has_manage_channels_anywhere,
)
from recap_bot.config import model_config, settings
from recap_bot.pipeline.download import _youtube_cookies_path

logger = logging.getLogger(__name__)


def _check_data_dir() -> tuple[bool, str]:
    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=settings.data_dir, prefix=".check-", delete=True) as fh:
            fh.write(b"ok")
        return True, f"`{settings.data_dir}` writable"
    except Exception as exc:
        return False, f"`{settings.data_dir}` — {exc}"


def _check_binary(name: str) -> tuple[bool, str]:
    path = shutil.which(name)
    if path:
        return True, f"`{name}` at `{path}`"
    return False, f"`{name}` not on PATH"


def _check_youtube_cookies() -> tuple[str, str]:
    """Not a pass/fail — YouTube cookies are optional, but their presence
    determines whether `/recap` on YouTube URLs will work from a datacenter IP.
    Returns (icon, message)."""
    path = _youtube_cookies_path()
    if path.exists():
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        return "✅", f"YouTube cookies present at `{path}` ({size:,} bytes)"
    return (
        "⚠️",
        f"No YouTube cookies at `{path}` — YouTube URLs may fail with "
        f"\"Sign in to confirm\" from this server's datacenter IP. "
        f"Twitch is unaffected.",
    )


def _check_guild() -> tuple[bool, str]:
    """Report on the configured home guild + enumerate every guild the bot has joined.

    The enumeration matters during multi-server rollouts: after changing
    DISCORD_GUILD_ID or sending a new invite link, `/check` is how you confirm
    the bot actually landed in the right place.
    """
    gid = settings.guild_id_as_int
    member_guilds = list(bot.guilds)
    member_lines = (
        "\n".join(f"   • `{g.name}` ({g.id})" for g in member_guilds)
        if member_guilds
        else "   (not in any guilds yet)"
    )

    if not gid:
        return True, (
            f"no `DISCORD_GUILD_ID` set (global command sync). "
            f"Member of {len(member_guilds)} guild(s):\n{member_lines}"
        )

    home = bot.get_guild(gid)
    if home is None:
        return False, (
            f"home guild `{gid}` NOT visible — has the bot been invited there?\n"
            f"   Currently member of {len(member_guilds)} guild(s):\n{member_lines}"
        )
    return True, (
        f"home `{home.name}` ({gid}). Member of {len(member_guilds)} guild(s):\n{member_lines}"
    )


async def _check_llm() -> tuple[bool, str, float]:
    """One short generation against the default model. Returns (ok, msg, elapsed_s)."""
    start = time.monotonic()
    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=settings.gemini_model,
            contents="Reply with exactly: pong",
        )
        elapsed = time.monotonic() - start
        text = (response.text or "").strip()
        return True, f"`{settings.gemini_model}` replied {text!r}", elapsed
    except Exception as exc:
        elapsed = time.monotonic() - start
        return False, f"{exc.__class__.__name__}: {exc}", elapsed


def _models_summary() -> str:
    keys = [
        "transcribe", "summarize",
        "roster_build", "scratchpad_build",
        "roster_canonicalize", "scratchpad_canonicalize",
        "update_roster", "update_scratchpad",
    ]
    return "\n".join(f"  • {k}: `{model_config.get(k)}`" for k in keys)


@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
@app_commands.allowed_installs(guilds=True, users=False)
@bot.tree.command(name="check", description="Run basic setup checks (DM only, Manage Channels)")
async def check(interaction: discord.Interaction):
    if not await user_has_manage_channels_anywhere(bot, interaction.user.id):
        await interaction.response.send_message(MANAGE_CHANNELS_REQUIRED_MSG, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    results: list[str] = []

    # 1. Discord
    if bot.user:
        results.append(f"✅ Discord: logged in as `{bot.user}` (id `{bot.user.id}`)")
    else:
        results.append("⚠️ Discord: not yet logged in?")

    # 2. Guild
    ok, msg = _check_guild()
    results.append(f"{'✅' if ok else '❌'} Guild: {msg}")

    # 3. Data dir
    ok, msg = _check_data_dir()
    results.append(f"{'✅' if ok else '❌'} Data dir: {msg}")

    # 4. Binaries
    for binary in (settings.ffmpeg_bin, "yt-dlp"):
        ok, msg = _check_binary(binary)
        results.append(f"{'✅' if ok else '❌'} Binary: {msg}")

    # 4b. YouTube cookies (optional — only needed if /recap is used on YouTube)
    icon, msg = _check_youtube_cookies()
    results.append(f"{icon} YouTube auth: {msg}")

    # 5. LLM call (the slow one)
    ok, msg, elapsed = await _check_llm()
    results.append(f"{'✅' if ok else '❌'} Gemini ({elapsed:.1f}s): {msg}")

    # 6. Models config (no API call)
    results.append("ℹ️ Models configured:\n" + _models_summary())

    await interaction.followup.send("\n".join(results), ephemeral=True)

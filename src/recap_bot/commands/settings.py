"""/settings — DM-only, owner-only runtime configuration.

Writes overrides to `data/runtime_config.yaml` (gitignored, persists across
deploys). The container is restarted to apply, which takes ~30 seconds.

Currently exposes: `set_gemini_key`. More overrides can be added by extending
RUNTIME_OVERRIDABLE in config.py and adding a subcommand here.
"""

import asyncio
import logging
import os

import discord
import yaml
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.config import RUNTIME_OVERRIDABLE, runtime_config_path, settings

logger = logging.getLogger(__name__)


def _mask(value: str) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def _write_override(key: str, value: str) -> None:
    """Merge a single key into runtime_config.yaml atomically."""
    if key not in RUNTIME_OVERRIDABLE:
        raise ValueError(f"{key!r} is not a runtime-overridable setting")
    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        try:
            existing = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    existing[key] = value
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


async def _restart_container_after(delay: float) -> None:
    """Sleep, then hard-exit the process. Docker's `restart: unless-stopped`
    brings the container back with the updated config loaded at startup."""
    await asyncio.sleep(delay)
    logger.warning("Restarting process via os._exit(0) to apply runtime config change")
    os._exit(0)


settings_group = app_commands.Group(
    name="settings",
    description="📜 Recap: Bot owner settings (DM only)",
    # DM-only at the group level so Discord hides it from guild slash menus.
    # Per-subcommand allowed_contexts is ignored by Discord for Groups.
    allowed_contexts=app_commands.AppCommandContext(
        guild=False, dm_channel=True, private_channel=False,
    ),
    allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
)


@settings_group.command(
    name="show",
    description="Show current bot settings (secret values are masked)",
)
async def settings_show(interaction: discord.Interaction):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/settings`.", ephemeral=True,
        )
        return

    runtime_path = runtime_config_path()
    has_runtime = runtime_path.exists()
    guild_ids = settings.guild_ids
    guilds_str = ", ".join(f"`{g}`" for g in guild_ids) if guild_ids else "(none — global sync only)"
    lines = [
        "**Current bot settings** (overrides applied at startup):",
        f"• `gemini_api_key`: `{_mask(settings.gemini_api_key)}`",
        f"• `gemini_model`: `{settings.gemini_model}`",
        f"• `default_style`: `{settings.default_style}`",
        f"• `max_vod_hours`: `{settings.max_vod_hours}`",
        f"• `log_level`: `{settings.log_level}`",
        f"• `data_dir`: `{settings.data_dir}`",
        f"• guild ids (instant-sync servers): {guilds_str}",
        "",
        f"Runtime override file: `{runtime_path}` ({'present' if has_runtime else 'not created yet'})",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@settings_group.command(
    name="set_gemini_key",
    description="Replace the Gemini API key and restart the bot to apply",
)
@app_commands.describe(key="The new Gemini API key (BYO from aistudio.google.com)")
async def settings_set_gemini_key(interaction: discord.Interaction, key: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/settings`.", ephemeral=True,
        )
        return

    key = key.strip()
    if len(key) < 20:
        await interaction.response.send_message(
            "❌ That key looks too short to be valid (< 20 chars). Aborting.",
            ephemeral=True,
        )
        return

    try:
        _write_override("gemini_api_key", key)
    except Exception as exc:
        logger.exception("Failed to write runtime override")
        await interaction.response.send_message(
            f"❌ Failed to write override: `{exc}`",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"✅ Saved new Gemini API key to `{runtime_config_path()}` "
        f"(masked: `{_mask(key)}`).\n"
        f"Restarting the bot in 3 seconds to apply. Discord will reconnect "
        f"automatically; expect ~20–30s of unavailability.",
        ephemeral=True,
    )
    asyncio.create_task(_restart_container_after(3.0))


def _parse_guild_id(raw: str) -> int | None:
    """Parse a Discord guild-id string. Returns the int, or None if invalid.
    Snowflakes are ~17-20 digit integers."""
    raw = raw.strip()
    if not raw.isdigit():
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@settings_group.command(
    name="add_guild",
    description="Add a server (guild id) to the instant-sync list and restart",
)
@app_commands.describe(guild_id="The server's guild id (right-click server → Copy Server ID)")
async def settings_add_guild(interaction: discord.Interaction, guild_id: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/settings`.", ephemeral=True,
        )
        return

    new_id = _parse_guild_id(guild_id)
    if new_id is None:
        await interaction.response.send_message(
            "❌ That doesn't look like a guild id (expected a number, "
            "e.g. `1505296799982682262`).",
            ephemeral=True,
        )
        return

    current = settings.guild_ids
    if new_id in current:
        await interaction.response.send_message(
            f"⏭️ Guild `{new_id}` is already in the list: "
            f"{', '.join(f'`{g}`' for g in current)}. Nothing to do.",
            ephemeral=True,
        )
        return

    updated = current + [new_id]
    in_guild = bot.get_guild(new_id) is not None
    try:
        _write_override("discord_guild_id", ",".join(str(g) for g in updated))
    except Exception as exc:
        logger.exception("Failed to write guild override")
        await interaction.response.send_message(f"❌ Failed to write override: `{exc}`", ephemeral=True)
        return

    warn = "" if in_guild else (
        "\n⚠️ Heads up: the bot is **not yet a member** of that server. Make sure "
        "you've invited it with the OAuth link — otherwise command sync to it will "
        "fail (the bot's other servers are unaffected)."
    )
    await interaction.response.send_message(
        f"✅ Added guild `{new_id}`. New list: {', '.join(f'`{g}`' for g in updated)}.{warn}\n"
        f"Restarting in 3 seconds to sync commands to it. ~20–30s of unavailability.",
        ephemeral=True,
    )
    asyncio.create_task(_restart_container_after(3.0))


@settings_group.command(
    name="remove_guild",
    description="Remove a server (guild id) from the instant-sync list and restart",
)
@app_commands.describe(guild_id="The server's guild id to stop syncing channel commands to")
async def settings_remove_guild(interaction: discord.Interaction, guild_id: str):
    if not await bot.is_owner(interaction.user):
        await interaction.response.send_message(
            "Only the bot owner can use `/settings`.", ephemeral=True,
        )
        return

    target = _parse_guild_id(guild_id)
    if target is None:
        await interaction.response.send_message(
            "❌ That doesn't look like a guild id (expected a number).",
            ephemeral=True,
        )
        return

    current = settings.guild_ids
    if target not in current:
        await interaction.response.send_message(
            f"⏭️ Guild `{target}` isn't in the list: "
            f"{', '.join(f'`{g}`' for g in current) or '(empty)'}. Nothing to do.",
            ephemeral=True,
        )
        return

    updated = [g for g in current if g != target]
    try:
        _write_override("discord_guild_id", ",".join(str(g) for g in updated))
    except Exception as exc:
        logger.exception("Failed to write guild override")
        await interaction.response.send_message(f"❌ Failed to write override: `{exc}`", ephemeral=True)
        return

    remaining = ", ".join(f"`{g}`" for g in updated) if updated else "(none — global sync only)"
    await interaction.response.send_message(
        f"✅ Removed guild `{target}`. Remaining: {remaining}.\n"
        f"Note: the old guild's channel commands won't be cleared automatically — "
        f"they linger in Discord until you kick the bot from that server or they "
        f"expire. Restarting in 3 seconds.",
        ephemeral=True,
    )
    asyncio.create_task(_restart_container_after(3.0))


bot.tree.add_command(settings_group)

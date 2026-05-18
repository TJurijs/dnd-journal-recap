import logging

import discord
from discord.ext import commands

from recap_bot.config import settings

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Attach job queue to bot for access from commands
bot._job_queue = None


def _is_dm_only(cmd) -> bool:
    """True if the command is restricted to DMs via @app_commands.allowed_contexts(guilds=False, ...)."""
    contexts = getattr(cmd, "allowed_contexts", None)
    if contexts is None:
        return False
    return contexts.guild is False and contexts.dm_channel is True


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    # Log the guild membership so a multi-server operator can confirm at a glance
    # which servers the bot joined (esp. useful after DISCORD_GUILD_ID swaps or
    # new invite links).
    if bot.guilds:
        guilds_summary = ", ".join(f"{g.name}({g.id})" for g in bot.guilds)
        logger.info("Member of %d guild(s): %s", len(bot.guilds), guilds_summary)
    else:
        logger.info("Not a member of any guilds yet (waiting for invite)")
    # Register the persistent "Edit" dynamic button so old buttons keep working
    # across restarts.
    try:
        from recap_bot.commands._edit_button import EditHintButton
        bot.add_dynamic_items(EditHintButton)
    except Exception:
        logger.exception("Failed to register EditHintButton dynamic item")
    try:
        all_globals = list(bot.tree.get_commands(guild=None))
        dm_only = [c for c in all_globals if _is_dm_only(c)]
        channel_cmds = [c for c in all_globals if not _is_dm_only(c)]

        if settings.guild_id_as_int:
            guild = discord.Object(id=settings.guild_id_as_int)

            # Rebuild the local tree:
            #   - DM-only commands stay global (must be global to appear in DM autocomplete)
            #   - Channel commands get guild-scoped for instant sync
            bot.tree.clear_commands(guild=None)
            for cmd in dm_only:
                bot.tree.add_command(cmd)
            for cmd in channel_cmds:
                bot.tree.add_command(cmd, guild=guild)

            synced_global = await bot.tree.sync(guild=None)
            synced_guild = await bot.tree.sync(guild=guild)
            logger.info(
                "Synced %d global (DM-only) command(s) and %d guild command(s) to guild %s",
                len(synced_global), len(synced_guild), settings.guild_id_as_int,
            )
        else:
            synced = await bot.tree.sync()
            logger.info("Synced %d commands globally", len(synced))
    except Exception:
        logger.exception("Failed to sync commands")


async def run_bot() -> None:
    from recap_bot.queue import JobQueue
    bot._job_queue = JobQueue(bot)
    bot._job_queue.start()
    await bot.start(settings.discord_bot_token)


# Import command modules so they register with the tree
from recap_bot.commands import check, initialize, jobs, recap, recap_edit, roster, scratchpad, settings as _settings_cmd  # noqa: E402,F401

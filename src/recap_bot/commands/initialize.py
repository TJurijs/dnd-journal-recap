import asyncio
import logging

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._helpers import format_channel_label
from recap_bot.pipeline import state
from recap_bot.pipeline.initialize import (
    _initializing_channels,
    is_initializing,
    run_initialization,
)
from recap_bot.storage import files as channel_files

logger = logging.getLogger(__name__)


class _ConfirmRebuildView(discord.ui.View):
    """Confirm/cancel buttons shown when /initialize would overwrite existing context."""

    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.confirmed: bool | None = None

    async def _gate(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the original requester can answer this.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Rebuild", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._gate(interaction):
            return
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._gate(interaction):
            return
        self.confirmed = False
        await interaction.response.defer()
        self.stop()


@bot.tree.command(
    name="initialize",
    description="📜 Recap: Build roster + scratchpad from this channel's existing journals",
)
@app_commands.default_permissions(manage_channels=True)
async def initialize(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    guild_id = interaction.guild_id

    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Run `/initialize` in the campaign channel, not a DM.", ephemeral=True,
        )
        return

    if is_initializing(channel_id):
        await interaction.response.send_message(
            "Initialization is already running in this channel.", ephemeral=True,
        )
        return

    if state.get(channel_id) is not None:
        await interaction.response.send_message(
            "There's an active recap job. Wait for it to finish or `/stop` it first.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    channel_label = format_channel_label(interaction.channel)
    already_initialized = await channel_files.has_context(channel_id)

    if already_initialized:
        view = _ConfirmRebuildView(interaction.user.id)
        prompt_msg = await interaction.followup.send(
            f"⚠️ **{channel_label}** already has a roster/scratchpad. Rebuild from scratch?",
            view=view,
            ephemeral=True,
        )
        timed_out = await view.wait()
        if timed_out or view.confirmed is None:
            await prompt_msg.edit(content="⏱️ Confirmation timed out. No changes made.", view=None)
            return
        if not view.confirmed:
            await prompt_msg.edit(content="❎ Cancelled. No changes made.", view=None)
            return
        await prompt_msg.edit(
            content=f"🛠️ Initialization started for **{channel_label}** — DMing you progress.",
            view=None,
        )
    else:
        await interaction.followup.send(
            f"🛠️ Initialization started for **{channel_label}** — DMing you progress.",
            ephemeral=True,
        )

    # Open a DM with the requester and send the initial progress message there.
    try:
        user = interaction.user
        dm_msg = await user.send(f"🛠️ **Initializing** for **{channel_label}**\nScanning channel history...")
    except discord.Forbidden:
        await interaction.followup.send(
            "I couldn't DM you — please enable DMs from server members and try again.",
            ephemeral=True,
        )
        return

    _initializing_channels.add(channel_id)
    try:
        # run_initialization owns the final render — it keeps all per-step
        # progress visible and appends the summary, so we don't overwrite it.
        await run_initialization(bot, dm_msg, channel_id, guild_id or 0, channel_label=channel_label)
    except asyncio.CancelledError:
        # run_initialization already rendered the cancellation footer.
        logger.info("Initialization cancelled for channel %s", channel_id)
    except Exception as exc:
        logger.exception("Initialization failed for channel %s", channel_id)
        await dm_msg.edit(content=f"❌ Initialization failed for **{channel_label}**:\n{str(exc)[:1500]}")
    finally:
        _initializing_channels.discard(channel_id)

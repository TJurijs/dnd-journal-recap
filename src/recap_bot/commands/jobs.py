"""DM-only /jobs command: list all active recap + init jobs with cancel buttons."""

import logging

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._helpers import (
    MANAGE_CHANNELS_REQUIRED_MSG,
    user_has_manage_channels_anywhere,
)
from recap_bot.pipeline import state
from recap_bot.pipeline.initialize import (
    _initializing_channels,
    cancel_initialization,
)
from recap_bot.pipeline.orchestrator import cancel_job

logger = logging.getLogger(__name__)


class _CancelRecapButton(discord.ui.Button):
    """Cancel an active /recap job for a given channel."""

    def __init__(self, channel_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Cancel · {label}",
            custom_id=f"cancel_recap_{channel_id}",
        )
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        job = state.get(self.channel_id)
        if job is None:
            await interaction.response.send_message(
                f"⏭️ Recap on channel `{self.channel_id}` already completed.",
                ephemeral=True,
            )
            return
        name = job.title or f"queued recap of {job.source_ref}"
        # Just set the flag — the orchestrator's _check_cancelled reads
        # job.cancelled via state.get(). If we released here, the flag would
        # be invisible to the orchestrator and the job would run to completion.
        cancel_job(self.channel_id)
        await interaction.response.send_message(
            f"⏹️ Cancelling recap: **{name}** ({job.channel_label or self.channel_id}).\n"
            f"It will stop at the next safe checkpoint.",
            ephemeral=True,
        )


class _CancelInitButton(discord.ui.Button):
    """Cancel a running /initialize on a given channel.

    Note: the in-flight LLM call cannot be aborted mid-request — our task is
    cancelled but the API call still runs server-side (and is billed).
    """

    def __init__(self, channel_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Cancel init · {label}",
            custom_id=f"cancel_init_{channel_id}",
        )
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        ok = cancel_initialization(self.channel_id)
        if not ok:
            await interaction.response.send_message(
                f"⏭️ Initialization on channel `{self.channel_id}` already finished.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"⏹️ Cancelling initialization on channel `{self.channel_id}`. "
            f"The bot will stop at the next checkpoint. Any in-flight LLM call may still complete "
            f"server-side and be billed.",
            ephemeral=True,
        )


def _build_jobs_view() -> tuple[str, discord.ui.View | None]:
    """Render the jobs listing + a view with one cancel button per job."""
    active = state.all_active()
    initializing = sorted(_initializing_channels)

    if not active and not initializing:
        return "No active jobs.", None

    lines: list[str] = []
    view = discord.ui.View(timeout=300)
    button_count = 0
    MAX_BUTTONS = 25

    for job in active:
        name = job.title or f"queued recap of {job.source_ref}"
        source = job.channel_label or f"channel {job.channel_id}"
        vod_ref = f"VOD {job.vod_id}" if job.vod_id else ""
        meta_bits = [bit for bit in (job.status, vod_ref) if bit]
        meta = f" ({' · '.join(meta_bits)})" if meta_bits else ""
        lines.append(f"🎬 **{name}**{meta}\n   _Source: {source}_")
        if button_count < MAX_BUTTONS:
            view.add_item(_CancelRecapButton(job.channel_id, name[:30]))
            button_count += 1

    for channel_id in initializing:
        if state.get(channel_id) is not None:
            continue  # already rendered above (init+recap on same channel shouldn't happen, but defensive)
        lines.append(f"🛠️ **Initialization** on channel `{channel_id}`")
        if button_count < MAX_BUTTONS:
            view.add_item(_CancelInitButton(channel_id, str(channel_id)[:20]))
            button_count += 1

    return "**Active jobs:**\n\n" + "\n\n".join(lines), view


@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
@app_commands.allowed_installs(guilds=True, users=False)
@bot.tree.command(name="jobs", description="📜 Recap: List all active jobs (DM only, Manage Channels)")
async def jobs(interaction: discord.Interaction):
    if not await user_has_manage_channels_anywhere(bot, interaction.user.id):
        await interaction.response.send_message(MANAGE_CHANNELS_REQUIRED_MSG, ephemeral=True)
        return
    content, view = _build_jobs_view()
    await interaction.response.send_message(content, view=view, ephemeral=True)

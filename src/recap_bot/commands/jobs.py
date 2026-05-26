"""DM-only /jobs command: list all active recap + init jobs with cancel buttons.

Jobs are keyed by CATEGORY (storage scope). A job's `channel_id` is the channel
its recap will post to; `category_id` is what cancellation/state use.
"""

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
    """Cancel an active /recap job for a given category."""

    def __init__(self, category_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Cancel · {label}",
            custom_id=f"cancel_recap_{category_id}",
        )
        self.category_id = category_id

    async def callback(self, interaction: discord.Interaction):
        job = state.get(self.category_id)
        if job is None:
            await interaction.response.send_message(
                f"⏭️ Recap for category `{self.category_id}` already completed.",
                ephemeral=True,
            )
            return
        name = job.title or f"queued recap of {job.source_ref}"
        # Just set the flag — the orchestrator's _check_cancelled reads
        # job.cancelled via state.get(). If we released here, the flag would
        # be invisible to the orchestrator and the job would run to completion.
        cancel_job(self.category_id)
        await interaction.response.send_message(
            f"⏹️ Cancelling recap: **{name}** ({job.channel_label or self.category_id}).\n"
            f"It will stop at the next safe checkpoint.",
            ephemeral=True,
        )


class _CancelInitButton(discord.ui.Button):
    """Cancel a running /initialize for a given category.

    Note: the in-flight LLM call cannot be aborted mid-request — our task is
    cancelled but the API call still runs server-side (and is billed).
    """

    def __init__(self, category_id: int, label: str):
        super().__init__(
            style=discord.ButtonStyle.danger,
            label=f"Cancel init · {label}",
            custom_id=f"cancel_init_{category_id}",
        )
        self.category_id = category_id

    async def callback(self, interaction: discord.Interaction):
        ok = cancel_initialization(self.category_id)
        if not ok:
            await interaction.response.send_message(
                f"⏭️ Initialization for category `{self.category_id}` already finished.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"⏹️ Cancelling initialization for category `{self.category_id}`. "
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
        lines.append(f"🎬 **{name}**{meta}\n   _Posting to: {source}_")
        if button_count < MAX_BUTTONS:
            view.add_item(_CancelRecapButton(job.category_id, name[:30]))
            button_count += 1

    for category_id in initializing:
        if state.get(category_id) is not None:
            continue  # already rendered above (init+recap on same category shouldn't happen, but defensive)
        lines.append(f"🛠️ **Initialization** for category `{category_id}`")
        if button_count < MAX_BUTTONS:
            view.add_item(_CancelInitButton(category_id, str(category_id)[:20]))
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

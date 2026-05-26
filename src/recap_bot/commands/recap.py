import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._helpers import format_channel_label
from recap_bot.config import settings
from recap_bot.pipeline import state
from recap_bot.pipeline.download import detect_source
from recap_bot.pipeline.initialize import is_initializing
from recap_bot.queue import JobQueue
from recap_bot.storage import discord_journals, files as channel_files


@bot.tree.command(
    name="recap",
    description="Generate a session recap from a Twitch or YouTube VOD",
)
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(
    url="Public Twitch VOD URL or YouTube video URL (max 6h duration)",
    style="Override default journal style (optional)",
    force="Delete cached audio/chunks for this VOD and re-download",
)
@app_commands.choices(
    style=[app_commands.Choice(name=s.title(), value=s) for s in ("chapters", "bullets", "narrative", "structured", "terse")]
)
async def recap(
    interaction: discord.Interaction,
    url: str,
    style: app_commands.Choice[str] = None,
    force: bool = False,
):
    await interaction.response.defer(ephemeral=True)

    detected = detect_source(url)
    if detected is None:
        await interaction.followup.send(
            "Invalid VOD URL — must be a Twitch VOD (`twitch.tv/.../videos/<id>`) "
            "or YouTube link (`youtube.com/watch?v=<id>` or `youtu.be/<id>`).",
            ephemeral=True,
        )
        return
    source_type, _vod_id = detected

    channel_id = interaction.channel_id
    guild_id = interaction.guild_id

    if state.get(channel_id) is not None:
        await interaction.followup.send(
            "There's already an active job for this channel. Check your DMs for progress, or use `/stop` to cancel.",
            ephemeral=True,
        )
        return

    if is_initializing(channel_id):
        await interaction.followup.send(
            "Initialization is in progress. Wait for `/initialize` to finish.",
            ephemeral=True,
        )
        return

    if not await channel_files.has_context(channel_id):
        await interaction.followup.send(
            "This channel hasn't been initialized yet. Run `/initialize` first to build the roster and scratchpad.",
            ephemeral=True,
        )
        return

    # Sanity check: if journals have been added to the channel since the last
    # /initialize (or /recap), the on-disk roster/scratchpad is out of sync and
    # a new init is needed before this recap can correctly incorporate them.
    meta = await channel_files.read_meta(channel_id) or {}
    journals_synced = int(meta.get("journals_synced", 0))
    try:
        current_entries = await discord_journals.list_for_channel(bot, channel_id)
        current_count = len(current_entries)
    except Exception:
        current_count = journals_synced  # fail-open if Discord scan fails

    if current_count > journals_synced:
        new_count = current_count - journals_synced
        await interaction.followup.send(
            f"⚠️ This channel has **{current_count}** journal entries but only **{journals_synced}** "
            f"have been incorporated into the roster/scratchpad. {new_count} new entr(y/ies) need to be "
            f"synced first — run `/initialize` to rebuild context, then re-run `/recap`.",
            ephemeral=True,
        )
        return

    style_value = style.value if style else (meta.get("style") or settings.default_style)

    job = state.claim(
        channel_id=channel_id,
        guild_id=guild_id or 0,
        requested_by=interaction.user.id,
        source_type=source_type,
        source_ref=url,
        style=style_value,
        channel_label=format_channel_label(interaction.channel),
        force=force,
    )
    if job is None:
        # Race: another /recap claimed the slot between our check and claim
        await interaction.followup.send(
            "There's already an active job for this channel. Check your DMs for progress, or use `/stop` to cancel.",
            ephemeral=True,
        )
        return

    # If anything fails between claim and enqueue, release the slot so we don't
    # leave orphan state stuck for the rest of the bot's lifetime.
    try:
        queue: JobQueue = bot._job_queue
        await queue.enqueue(channel_id)

        msg = "📜 Queued. I'll post the recap in this channel when ready and DM you live progress."
        if style:
            msg += f" Style override: **{style.value}**."
        if force:
            msg += " **Force re-download:** cached audio and chunks will be wiped."
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        state.release(channel_id)
        raise

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._helpers import (
    RECAP_REQUIRED_PERMS,
    bot_missing_channel_perms,
    format_channel_label,
)
from recap_bot.config import settings
from recap_bot.pipeline import state
from recap_bot.pipeline.download import detect_source
from recap_bot.pipeline.initialize import is_initializing
from recap_bot.queue import JobQueue
from recap_bot.storage import discord_journals, files as channel_files


@bot.tree.command(
    name="recap",
    description="📜 Recap: Generate a session recap from a Twitch or YouTube VOD",
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

    # Permission preflight — the recap pipeline is expensive (download +
    # transcribe + summarize, all billable) and only posts the result at the
    # very end. Bail BEFORE any of that if the bot can't post here, so a
    # locked-down channel never costs you a doomed API run.
    missing = bot_missing_channel_perms(interaction, RECAP_REQUIRED_PERMS)
    if missing:
        await interaction.followup.send(
            f"🔒 I can't run a recap in this channel — I'm missing: "
            f"**{', '.join(missing)}**.\n"
            f"Ask a server admin to grant these to me (or my role) in this "
            f"channel's permission settings, then try again. "
            f"(No API cost incurred — I check before starting.)",
            ephemeral=True,
        )
        return

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

    # Snapshot channel state ONCE — we need the journal count for two checks
    # below (empty-channel fast-path AND journals-synced drift check).
    try:
        current_entries = await discord_journals.list_for_channel(bot, channel_id)
        current_count = len(current_entries)
        scan_failed = False
    except Exception:
        current_count = 0
        scan_failed = True

    meta = await channel_files.read_meta(channel_id) or {}
    has_ctx = await channel_files.has_context(channel_id)

    if not has_ctx:
        # No prior /initialize, no recap snapshots on disk.
        if scan_failed:
            await interaction.followup.send(
                "Couldn't read channel history to determine state. Run `/initialize` "
                "first to seed the roster and scratchpad.",
                ephemeral=True,
            )
            return
        if current_count > 0:
            # Channel has organic journals but the bot has no state for them.
            # Forcing /initialize first ensures the new recap chains correctly
            # off the existing campaign content instead of overwriting it.
            await interaction.followup.send(
                f"This channel has **{current_count}** journal entr(y/ies) but no "
                f"`/initialize` has been run. Run `/initialize` first to seed the "
                f"roster and scratchpad from those entries — otherwise this recap "
                f"would lose continuity with them.",
                ephemeral=True,
            )
            return
        # Empty channel + no prior state: skip the /initialize requirement.
        # The orchestrator's read_context_for_recap returns ("", "") for this
        # case and the LLM's roster/scratchpad steps will populate from the
        # first recap's content. Seed meta.yaml with guild_id so subsequent
        # reads have it (normally /initialize does this).
        await channel_files.write_meta(channel_id, guild_id=guild_id or 0)
    else:
        # Prior state exists. Enforce that journals_synced is current so the
        # new recap incorporates everything added to the channel since the
        # last /initialize or /recap.
        journals_synced = int(meta.get("journals_synced", 0))
        if not scan_failed and current_count > journals_synced:
            new_count = current_count - journals_synced
            await interaction.followup.send(
                f"⚠️ This channel has **{current_count}** journal entries but only "
                f"**{journals_synced}** have been incorporated into the roster/scratchpad. "
                f"{new_count} new entr(y/ies) need to be synced first — run "
                f"`/initialize` to rebuild context, then re-run `/recap`.",
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

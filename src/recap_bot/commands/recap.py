import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._helpers import (
    NOT_IN_CATEGORY_MSG,
    RECAP_REQUIRED_PERMS,
    bot_missing_channel_perms,
    format_channel_label,
    resolve_category,
)
from recap_bot.config import model_config, settings
from recap_bot.pipeline import state
from recap_bot.pipeline.download import detect_source
from recap_bot.pipeline.initialize import is_initializing
from recap_bot.queue import JobQueue
from recap_bot.storage import files as channel_files


@bot.tree.command(
    name="recap",
    description="📜 Recap: Generate a session recap from a Twitch or YouTube VOD",
)
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(
    url="Public Twitch VOD URL or YouTube video URL (max 6h duration)",
    style="Override default journal style (optional)",
    force="Delete cached audio/chunks for this VOD and re-download",
    profile="Model profile (default = cheaper/faster, high = best quality). For A/B testing.",
    silent="DM the finished recap privately to you instead of posting it in the channel",
)
@app_commands.choices(
    style=[app_commands.Choice(name=s.title(), value=s) for s in ("chapters", "bullets", "narrative", "structured", "terse")],
    profile=[app_commands.Choice(name=p, value=p) for p in model_config.profile_names()],
)
async def recap(
    interaction: discord.Interaction,
    url: str,
    style: app_commands.Choice[str] = None,
    force: bool = False,
    profile: app_commands.Choice[str] = None,
    silent: bool = False,
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

    # Data is scoped to the channel's CATEGORY. No category → no scoping → refuse.
    cat = resolve_category(interaction)
    if cat is None:
        await interaction.followup.send(NOT_IN_CATEGORY_MSG, ephemeral=True)
        return
    category_id, category_name = cat

    # Permission preflight on the POST channel (where this recap will be posted).
    # The pipeline is expensive (download + transcribe + summarize, all billable)
    # and only posts at the very end — bail for free if we can't post here.
    # Skipped for silent recaps: they're DM'd to the requester, never posted to
    # the channel, so no channel post permissions are needed.
    if not silent:
        missing = bot_missing_channel_perms(interaction, RECAP_REQUIRED_PERMS)
        if missing:
            await interaction.followup.send(
                f"🔒 I can't run a recap in this channel — I'm missing: "
                f"**{', '.join(missing)}**.\n"
                f"Ask a server admin to grant these to me (or my role) in this "
                f"channel's permission settings, then try again. "
                f"(No API cost incurred — I check before starting. Tip: `silent:true` "
                f"DMs the recap to you and needs no channel permissions.)",
                ephemeral=True,
            )
            return

    post_channel_id = interaction.channel_id
    guild_id = interaction.guild_id

    if state.get(category_id) is not None:
        await interaction.followup.send(
            f"There's already an active recap job for the **{category_name}** category. "
            "Check your DMs for progress, or use `/stop` to cancel.",
            ephemeral=True,
        )
        return

    if is_initializing(category_id):
        await interaction.followup.send(
            f"`/initialize` is running for the **{category_name}** category. "
            "Wait for it to finish.",
            ephemeral=True,
        )
        return

    # If the category has no roster/scratchpad yet (no /initialize), that's fine
    # — the pipeline seeds from empty context and the LLM builds the first
    # roster/scratchpad from this recap. If it does have context, we read +
    # update it. Either way, seed meta with guild_id so it's recorded.
    meta = await channel_files.read_meta(category_id) or {}
    await channel_files.write_meta(category_id, guild_id=guild_id or 0)
    style_value = style.value if style else (meta.get("style") or settings.default_style)
    profile_value = profile.value if profile else "default"

    job = state.claim(
        category_id=category_id,
        channel_id=post_channel_id,
        guild_id=guild_id or 0,
        requested_by=interaction.user.id,
        source_type=source_type,
        source_ref=url,
        style=style_value,
        channel_label=format_channel_label(interaction.channel),
        force=force,
        profile=profile_value,
        silent=silent,
    )
    if job is None:
        # Race: another /recap claimed this category between our check and claim
        await interaction.followup.send(
            f"There's already an active recap job for the **{category_name}** category. "
            "Check your DMs for progress, or use `/stop` to cancel.",
            ephemeral=True,
        )
        return

    # If anything fails between claim and enqueue, release the slot so we don't
    # leave orphan state stuck for the rest of the bot's lifetime.
    try:
        queue: JobQueue = bot._job_queue
        await queue.enqueue(category_id)

        where = "DM you the recap privately" if silent else "post the recap in this channel"
        msg = (
            f"📜 Queued for **{category_name}**. I'll {where} when ready and DM you live progress."
        )
        if silent:
            msg += " 🤫 **Silent:** nothing will be posted in the channel."
        if profile_value != "default":
            msg += f" Profile: **{profile_value}**."
        if style:
            msg += f" Style override: **{style.value}**."
        if force:
            msg += " **Force re-download:** cached audio and chunks will be wiped."
        await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        state.release(category_id)
        raise

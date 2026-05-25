"""/recap_edit — replace a specific recap's journal.md with an uploaded file,
and edit the original Discord post in-place to swap its attachment.

The Discord post and the on-disk `journal.md` are kept in sync: the post is
edited *before* the disk write, so a partial failure (e.g. the original message
was deleted, or the bot lacks Manage Messages perms) leaves disk untouched. This
way humans reading the channel never see a stale `.md` attachment while disk
holds the "real" content.
"""

import logging
from typing import Optional

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.storage import discord_journals, files as channel_files

logger = logging.getLogger(__name__)


@bot.tree.command(
    name="recap_edit",
    description="Replace a recap's journal.md and update the Discord post in place",
)
@app_commands.describe(
    vod_id="Twitch VOD ID of the recap to edit (e.g. 2455113742)",
    file="The new journal.md to replace the existing one",
)
@app_commands.default_permissions(manage_channels=True)
async def recap_edit(
    interaction: discord.Interaction,
    vod_id: str,
    file: discord.Attachment,
):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use `/recap_edit` in the campaign channel.", ephemeral=True
        )
        return

    perms = interaction.user.guild_permissions if interaction.user else None
    if not (perms and perms.manage_channels):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to edit a recap journal.",
            ephemeral=True,
        )
        return

    recap_dir = channel_files.find_recap_dir_for_vod(interaction.channel_id, vod_id)
    if recap_dir is None:
        await interaction.response.send_message(
            f"No recap folder found for VOD `{vod_id}` in this channel.",
            ephemeral=True,
        )
        return

    raw = await file.read()
    try:
        new_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        await interaction.response.send_message(
            "❌ Uploaded file must be UTF-8 text.", ephemeral=True
        )
        return

    # Discord message edits + history scans can take a beat; defer so we don't
    # hit the 3-second response window.
    await interaction.response.defer(ephemeral=True)

    # Resolve the Discord message id for this recap. Prefer the stored value;
    # for old recaps (posted before this feature shipped) fall back to scanning
    # channel history for the matching `vod<id>.md` attachment.
    msg_id: Optional[int] = channel_files.read_recap_message_id(recap_dir)
    backfilled = False
    if msg_id is None:
        try:
            msg_id = await discord_journals.find_recap_message_id(
                bot, interaction.channel_id, vod_id,
            )
        except Exception:
            logger.exception("find_recap_message_id failed for vod %s", vod_id)
            msg_id = None
        if msg_id is not None:
            # Opportunistically persist so we don't have to scan next time.
            try:
                channel_files.write_recap_message_id(recap_dir, msg_id)
                backfilled = True
            except Exception:
                logger.exception("Failed to backfill discord_msg_id for vod %s", vod_id)

    if msg_id is None:
        await interaction.followup.send(
            f"⚠️ Could not locate the original Discord post for VOD `{vod_id}` "
            f"in this channel (it may have been deleted, or scrolled past the "
            f"500-message scan window). On-disk `journal.md` was **not** changed "
            f"so the channel and disk stay in sync. Re-run `/recap` on this VOD "
            f"to post a fresh recap message.",
            ephemeral=True,
        )
        return

    # Edit the Discord post first; only touch disk if that succeeded.
    try:
        edited_msg_id = await discord_journals.edit_journal_message(
            bot, interaction.channel_id, msg_id, raw,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I lack permission to edit my own recap post in this channel "
            "(check that I have Send Messages + Attach Files). On-disk file "
            "NOT changed.",
            ephemeral=True,
        )
        return
    except Exception as exc:
        logger.exception("edit_journal_message failed")
        await interaction.followup.send(
            f"❌ Failed to edit the Discord post: `{exc.__class__.__name__}: {exc}`. "
            f"On-disk file NOT changed (kept in sync with channel).",
            ephemeral=True,
        )
        return

    if edited_msg_id is None:
        # Original message is gone (deleted by a mod, channel cleared, etc.).
        # Leaving disk untouched keeps the two in sync; the user can re-`/recap`
        # to post fresh.
        await interaction.followup.send(
            f"⚠️ The original Discord post for VOD `{vod_id}` was deleted, so I "
            f"can't edit it in place. On-disk file was **not** changed. "
            f"Re-run `/recap` on this VOD to post a fresh message.",
            ephemeral=True,
        )
        return

    # Discord edit succeeded — safe to update on-disk journal.md
    target = recap_dir / "journal.md"
    channel_files.write_text_atomic(target, new_text)
    rel_path = target.relative_to(target.parents[3])

    extra = " (message id backfilled from history)" if backfilled else ""
    await interaction.followup.send(
        f"✅ Replaced `{rel_path}` ({len(new_text):,} chars) and swapped the "
        f"attachment on the original Discord post in this channel{extra}.\n"
        f"Note: the recap folder's `roster.md` / `scratchpad.md` snapshots are "
        f"unchanged — edit separately with "
        f"`/roster action:edit vod_id:{vod_id}` / "
        f"`/scratchpad action:edit vod_id:{vod_id}` if needed.",
        ephemeral=True,
    )

"""/recap_edit — replace a specific recap's journal.md with an uploaded file."""

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.storage import files as channel_files


@bot.tree.command(
    name="recap_edit",
    description="Replace a recap's journal.md with an uploaded file",
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

    target = recap_dir / "journal.md"
    channel_files.write_text_atomic(target, new_text)
    rel_path = target.relative_to(target.parents[3])

    await interaction.response.send_message(
        f"✅ Replaced `{rel_path}` ({len(new_text):,} chars).\n"
        f"Note: the recap folder's `roster.md` and `scratchpad.md` snapshots are unchanged — "
        f"edit them separately with `/roster action:edit vod_id:{vod_id}` / "
        f"`/scratchpad action:edit vod_id:{vod_id}` if needed.",
        ephemeral=True,
    )

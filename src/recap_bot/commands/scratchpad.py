from io import BytesIO
from typing import Literal, Optional

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._edit_button import make_edit_view
from recap_bot.storage import files as channel_files


async def _do_show(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use this in the channel where your campaign is tracked.", ephemeral=True
        )
        return

    scratchpad_text = await channel_files.read_scratchpad(interaction.channel_id)
    if not scratchpad_text:
        await interaction.response.send_message(
            "📝 Scratchpad is empty. Run `/initialize` (if the channel has prior journals) "
            "or just `/recap` (if it's a fresh channel) to populate it.",
            ephemeral=True,
        )
        return

    edit_view = make_edit_view("scratchpad")
    if len(scratchpad_text) <= 4000:
        embed = discord.Embed(title="Session Scratchpad", description=scratchpad_text)
        await interaction.response.send_message(embed=embed, view=edit_view, ephemeral=True)
    else:
        file = discord.File(BytesIO(scratchpad_text.encode("utf-8")), filename="scratchpad.md")
        await interaction.response.send_message("📝 Campaign scratchpad:", file=file, view=edit_view, ephemeral=True)


async def _do_delete(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use this in the campaign channel.", ephemeral=True
        )
        return

    perms = interaction.user.guild_permissions if interaction.user else None
    if not (perms and perms.manage_channels):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to delete the scratchpad.",
            ephemeral=True,
        )
        return

    deleted = await channel_files.clear_scratchpad(interaction.channel_id)
    if deleted is not None:
        try:
            rel = deleted.relative_to(deleted.parents[3])
        except (ValueError, IndexError):
            rel = deleted
        await interaction.response.send_message(
            f"🗑️ Deleted `{rel}`. The next `/scratchpad` will fall back to any "
            f"legacy snapshot, then empty. Run `/initialize` or `/recap` to rebuild.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "No scratchpad to delete — this channel has no scratchpad anywhere.",
            ephemeral=True,
        )


async def _do_edit(
    interaction: discord.Interaction,
    file: Optional[discord.Attachment],
):
    """Edit the channel's canonical scratchpad. Same single-file model as
    `/roster action:edit`: no per-recap target, one canonical file at the
    channel root."""
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use this in the campaign channel.", ephemeral=True
        )
        return

    perms = interaction.user.guild_permissions if interaction.user else None
    if not (perms and perms.manage_channels):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to edit the scratchpad.",
            ephemeral=True,
        )
        return

    if file is None:
        text = await channel_files.read_scratchpad(interaction.channel_id)
        if not text:
            await interaction.response.send_message(
                "No scratchpad to download yet. Run `/initialize` or `/recap` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "📥 Current scratchpad. Edit locally and re-run "
            "`/scratchpad action:edit file:<your edited file>` to replace it.",
            file=discord.File(BytesIO(text.encode("utf-8")), filename="scratchpad.md"),
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

    await channel_files.write_scratchpad(interaction.channel_id, new_text)
    await interaction.response.send_message(
        f"✅ Replaced the channel scratchpad ({len(new_text):,} chars).",
        ephemeral=True,
    )


def _register(name: str, description: str) -> None:
    """Register `/name` with show/delete/edit behavior. Used for both
    `/scratchpad` and its `/pad` alias."""

    @bot.tree.command(name=name, description=description)
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        action="show (default), delete, or edit",
        file="For action=edit: a scratchpad.md file to replace the current scratchpad with",
    )
    async def _cmd(
        interaction: discord.Interaction,
        action: Optional[Literal["show", "delete", "edit"]] = None,
        file: Optional[discord.Attachment] = None,
    ):
        if action == "delete":
            await _do_delete(interaction)
        elif action == "edit":
            await _do_edit(interaction, file)
        else:
            await _do_show(interaction)


_register("scratchpad", "Show or manage the campaign scratchpad")
_register("pad", "Show or manage the campaign scratchpad (alias for /scratchpad)")

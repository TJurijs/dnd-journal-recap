from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._edit_button import make_edit_view
from recap_bot.storage import files as channel_files


def _resolve_scratchpad_path(channel_id: int, vod_id: Optional[str]) -> Optional[Path]:
    if vod_id:
        recap = channel_files.find_recap_dir_for_vod(channel_id, vod_id)
        if recap is None:
            return None
        return recap / "scratchpad.md"
    latest = channel_files.latest_recap_dir(channel_id)
    if latest is not None:
        return latest / "scratchpad.md"
    init_path = channel_files.initialize_dir(channel_id) / "scratchpad.md"
    return init_path if init_path.exists() else None


async def _do_show(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use this in the channel where your campaign is tracked.", ephemeral=True
        )
        return

    scratchpad_text = await channel_files.read_scratchpad(interaction.channel_id)
    if not scratchpad_text:
        await interaction.response.send_message(
            "📝 Scratchpad is empty. Run `/initialize` to build it from your journal history.", ephemeral=True
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

    existed = await channel_files.clear_scratchpad(interaction.channel_id)
    if existed:
        await interaction.response.send_message(
            "🗑️ Scratchpad deleted from `initialize/`. Recap snapshots still hold their own copies.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("No scratchpad to delete.", ephemeral=True)


async def _do_edit(
    interaction: discord.Interaction,
    file: Optional[discord.Attachment],
    vod_id: Optional[str],
):
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

    target = _resolve_scratchpad_path(interaction.channel_id, vod_id)
    if target is None:
        await interaction.response.send_message(
            f"No scratchpad found for {('VOD ' + vod_id) if vod_id else 'this channel'}. "
            "Run `/initialize` (or `/recap`) first.",
            ephemeral=True,
        )
        return

    if file is None:
        if not target.exists():
            await interaction.response.send_message(
                f"The target scratchpad doesn't exist yet at `{target.relative_to(target.parents[3])}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"📥 Current scratchpad from `{target.parent.name}`. Edit and re-run "
            f"`/scratchpad action:edit file:<your edited file>` to replace it.",
            file=discord.File(str(target), filename="scratchpad.md"),
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

    channel_files.write_text_atomic(target, new_text)
    rel_path = target.relative_to(target.parents[3])
    await interaction.response.send_message(
        f"✅ Replaced `{rel_path}` ({len(new_text):,} chars).",
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
        vod_id="For action=edit: a specific recap's VOD ID (default = latest)",
    )
    async def _cmd(
        interaction: discord.Interaction,
        action: Optional[Literal["show", "delete", "edit"]] = None,
        file: Optional[discord.Attachment] = None,
        vod_id: Optional[str] = None,
    ):
        if action == "delete":
            await _do_delete(interaction)
        elif action == "edit":
            await _do_edit(interaction, file, vod_id)
        else:
            await _do_show(interaction)


_register("scratchpad", "Show or manage the campaign scratchpad")
_register("pad", "Show or manage the campaign scratchpad (alias for /scratchpad)")

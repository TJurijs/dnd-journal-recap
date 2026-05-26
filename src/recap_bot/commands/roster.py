from io import BytesIO
from typing import Literal, Optional

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._edit_button import make_edit_view
from recap_bot.commands._helpers import NOT_IN_CATEGORY_MSG, resolve_category
from recap_bot.storage import files as channel_files


def _format_roster(roster_text: str) -> str:
    """Group lines by (Player) vs (NPC) with visible headers."""
    lines = [ln.strip() for ln in roster_text.splitlines() if ln.strip()]
    players = [ln for ln in lines if "(Player)" in ln]
    npcs = [ln for ln in lines if "(NPC)" in ln]
    other = [ln for ln in lines if ln not in players and ln not in npcs]

    sections: list[str] = []
    if players:
        sections.append("**Player Characters**\n" + "\n".join(players))
    if npcs:
        sections.append("**NPCs**\n" + "\n".join(npcs))
    if other:
        sections.append("**Other**\n" + "\n".join(other))
    return "\n\n".join(sections) or roster_text


def _require_manage(interaction: discord.Interaction) -> bool:
    perms = interaction.user.guild_permissions if interaction.user else None
    return bool(perms and perms.manage_channels)


async def _do_show(interaction: discord.Interaction):
    cat = resolve_category(interaction)
    if cat is None:
        await interaction.response.send_message(NOT_IN_CATEGORY_MSG, ephemeral=True)
        return
    category_id, _ = cat

    roster_text = await channel_files.read_roster(category_id)
    if not roster_text:
        await interaction.response.send_message(
            "📝 Roster is empty for this category. Run `/initialize` in the journal "
            "channel, or just `/recap`, to populate it.",
            ephemeral=True,
        )
        return

    formatted = _format_roster(roster_text)
    edit_view = make_edit_view("roster")
    if len(formatted) <= 4000:
        embed = discord.Embed(title="Campaign Roster", description=formatted)
        await interaction.response.send_message(embed=embed, view=edit_view, ephemeral=True)
    else:
        file = discord.File(BytesIO(formatted.encode("utf-8")), filename="roster.md")
        await interaction.response.send_message("📝 Campaign roster:", file=file, view=edit_view, ephemeral=True)


async def _do_delete(interaction: discord.Interaction):
    cat = resolve_category(interaction)
    if cat is None:
        await interaction.response.send_message(NOT_IN_CATEGORY_MSG, ephemeral=True)
        return
    category_id, _ = cat

    if not _require_manage(interaction):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to delete the roster.",
            ephemeral=True,
        )
        return

    deleted = await channel_files.clear_roster(category_id)
    if deleted is not None:
        try:
            rel = deleted.relative_to(deleted.parents[3])
        except (ValueError, IndexError):
            rel = deleted
        await interaction.response.send_message(
            f"🗑️ Deleted `{rel}`. The next `/roster` falls back to any legacy "
            f"snapshot, then empty. Run `/initialize` or `/recap` to rebuild.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "No roster to delete — this category has no roster anywhere yet.",
            ephemeral=True,
        )


async def _do_edit(interaction: discord.Interaction, file: Optional[discord.Attachment]):
    """Edit the category's canonical roster. `file` omitted → download current;
    `file` present → replace."""
    cat = resolve_category(interaction)
    if cat is None:
        await interaction.response.send_message(NOT_IN_CATEGORY_MSG, ephemeral=True)
        return
    category_id, _ = cat

    if not _require_manage(interaction):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to edit the roster.",
            ephemeral=True,
        )
        return

    if file is None:
        text = await channel_files.read_roster(category_id)
        if not text:
            await interaction.response.send_message(
                "No roster to download yet. Run `/initialize` or `/recap` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "📥 Current roster. Edit locally and re-run "
            "`/roster action:edit file:<your edited file>` to replace it.",
            file=discord.File(BytesIO(text.encode("utf-8")), filename="roster.md"),
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

    await channel_files.write_roster(category_id, new_text)
    await interaction.response.send_message(
        f"✅ Replaced the category roster ({len(new_text):,} chars).",
        ephemeral=True,
    )


@bot.tree.command(name="roster", description="📜 Recap: Show or manage the campaign roster")
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(
    action="show (default), delete, or edit",
    file="For action=edit: a roster.md file to replace the current roster with",
)
async def roster(
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

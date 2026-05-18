from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.commands._edit_button import make_edit_view
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


def _resolve_roster_path(channel_id: int, vod_id: Optional[str]) -> Optional[Path]:
    """Which roster.md file does `action:edit` touch?

    - vod_id given → that specific recap's roster.md (must exist)
    - else → latest recap's roster.md
    - else → initialize/roster.md
    - else → None (nothing to edit yet)
    """
    if vod_id:
        recap = channel_files.find_recap_dir_for_vod(channel_id, vod_id)
        if recap is None:
            return None
        return recap / "roster.md"
    latest = channel_files.latest_recap_dir(channel_id)
    if latest is not None:
        return latest / "roster.md"
    init_path = channel_files.initialize_dir(channel_id) / "roster.md"
    return init_path if init_path.exists() else None


async def _do_show(interaction: discord.Interaction):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use `/roster` in the channel where your campaign is tracked.", ephemeral=True
        )
        return

    roster_text = await channel_files.read_roster(interaction.channel_id)
    if not roster_text:
        await interaction.response.send_message(
            "📝 Roster is empty. Run `/initialize` to build it from your journal history.", ephemeral=True
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
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use `/roster action:delete` in the campaign channel.", ephemeral=True
        )
        return

    perms = interaction.user.guild_permissions if interaction.user else None
    if not (perms and perms.manage_channels):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to delete the roster.",
            ephemeral=True,
        )
        return

    existed = await channel_files.clear_roster(interaction.channel_id)
    if existed:
        await interaction.response.send_message(
            "🗑️ Roster deleted from `initialize/`. Recap snapshots still hold their own copies — edit them individually if needed.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("No roster to delete.", ephemeral=True)


async def _do_edit(
    interaction: discord.Interaction,
    file: Optional[discord.Attachment],
    vod_id: Optional[str],
):
    if isinstance(interaction.channel, discord.DMChannel):
        await interaction.response.send_message(
            "Use `/roster action:edit` in the campaign channel.", ephemeral=True
        )
        return

    perms = interaction.user.guild_permissions if interaction.user else None
    if not (perms and perms.manage_channels):
        await interaction.response.send_message(
            "You need the **Manage Channels** permission to edit the roster.",
            ephemeral=True,
        )
        return

    target = _resolve_roster_path(interaction.channel_id, vod_id)
    if target is None:
        await interaction.response.send_message(
            f"No roster found for {('VOD ' + vod_id) if vod_id else 'this channel'}. "
            "Run `/initialize` (or `/recap`) first.",
            ephemeral=True,
        )
        return

    if file is None:
        # No file uploaded — return the current roster so the user can download,
        # edit locally, and re-run the command with the file attached.
        if not target.exists():
            await interaction.response.send_message(
                f"The target roster doesn't exist yet at `{target.relative_to(target.parents[3])}`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"📥 Current roster from `{target.parent.name}`. Edit and re-run "
            f"`/roster action:edit file:<your edited file>` to replace it.",
            file=discord.File(str(target), filename="roster.md"),
            ephemeral=True,
        )
        return

    # File uploaded — read, decode, replace target.
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


@bot.tree.command(name="roster", description="Show or manage the campaign roster")
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(
    action="show (default), delete, or edit",
    file="For action=edit: a roster.md file to replace the current roster with",
    vod_id="For action=edit: a specific recap's VOD ID (default = latest)",
)
async def roster(
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

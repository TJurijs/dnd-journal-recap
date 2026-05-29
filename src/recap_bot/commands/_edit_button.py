"""Persistent "✏️ Edit" button attached to roster/scratchpad/recap messages.

Clicking the button shows an ephemeral reminder of the slash command the user
needs to invoke to replace the file. Discord doesn't expose a file picker via
buttons — only slash commands with `file:` parameters do — so this button's
job is purely to surface the right command path.

The button is implemented as a `DynamicItem` so it survives bot restarts: the
custom_id encodes which kind of content is being edited (and a VOD id for
journal edits) and `from_custom_id` reconstructs the handler on demand.
"""

from __future__ import annotations

import re

import discord
from discord import ui


_CUSTOM_ID_TEMPLATE = r"edit:(?P<kind>roster|scratchpad|journal):(?P<vod_id>[\w\-]*)"


class EditHintButton(ui.DynamicItem[ui.Button], template=_CUSTOM_ID_TEMPLATE):
    """Persistent button that tells the user how to replace the content."""

    def __init__(self, kind: str, vod_id: str = ""):
        super().__init__(
            ui.Button(
                style=discord.ButtonStyle.secondary,
                label="✏️ Edit (upload .md)",
                custom_id=f"edit:{kind}:{vod_id}",
            )
        )
        self.kind = kind
        self.vod_id = vod_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match["kind"], match["vod_id"])

    def _format_command(self) -> str:
        vod_param = f" vod_id:{self.vod_id}" if self.vod_id else ""
        if self.kind == "roster":
            return f"/roster action:edit file:<your roster.md>{vod_param}"
        if self.kind == "scratchpad":
            return f"/scratchpad action:edit file:<your scratchpad.md>{vod_param}"
        return f"/<unknown:{self.kind}>"

    async def callback(self, interaction: discord.Interaction):
        # Journal editing was removed (recap posts are now embed-only, no
        # attachment). Old recap posts may still carry a "journal" Edit button;
        # tell the user it's no longer supported instead of pointing at a
        # command that no longer exists.
        if self.kind == "journal":
            await interaction.response.send_message(
                "Editing recap journals in place is no longer supported — recaps "
                "are posted as embeds now. To refresh a recap, re-run `/recap` on "
                "the same VOD. (Roster and scratchpad are still editable.)",
                ephemeral=True,
            )
            return
        cmd = self._format_command()
        await interaction.response.send_message(
            f"To replace this file, run:\n```\n{cmd}\n```\n"
            f"Discord opens a native file picker when you fill in the `file:` parameter. "
            f"Editing requires the **Manage Channels** permission.",
            ephemeral=True,
        )


def make_edit_view(kind: str, vod_id: str = "") -> ui.View:
    """A View containing a single EditHintButton."""
    view = ui.View(timeout=None)
    view.add_item(EditHintButton(kind, vod_id))
    return view

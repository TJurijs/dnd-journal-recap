"""Small Discord helpers shared by slash command modules."""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


def format_channel_label(channel: discord.abc.GuildChannel | discord.Thread | None) -> str:
    """Return a short label for status messages: 'Category / channel-name'.

    Falls back to just the channel name if there's no category, and to the
    string form of the channel id if even the name is unavailable.
    """
    if channel is None:
        return "(unknown channel)"
    name = getattr(channel, "name", "") or str(getattr(channel, "id", ""))
    # Threads have a `parent`; regular text channels have `category`.
    category = getattr(channel, "category", None)
    if category is not None and getattr(category, "name", ""):
        return f"{category.name} / {name}"
    parent = getattr(channel, "parent", None)
    if parent is not None and getattr(parent, "name", ""):
        return f"{parent.name} / {name}"
    return name


async def user_has_manage_channels_anywhere(bot: discord.Client, user_id: int) -> bool:
    """Check whether `user_id` has Manage Channels in ANY guild the bot is in.

    Used to gate DM-only commands (`/jobs`, `/check`) — there's no guild
    context in a DM so we can't use Discord's `default_permissions` decorator.
    Caches member lookups via `guild.get_member`; falls back to API fetch.
    """
    for guild in bot.guilds:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden):
                continue
            except Exception:
                logger.exception("fetch_member failed for guild %s user %s", guild.id, user_id)
                continue
        if member is not None and member.guild_permissions.manage_channels:
            return True
    return False


MANAGE_CHANNELS_REQUIRED_MSG = (
    "🔒 This command requires the **Manage Channels** permission in a server "
    "the bot is in."
)


# --- Channel permission preflight ---------------------------------------------
#
# Run a recap/initialize ONLY after confirming the bot can actually do the work
# in the target channel. The expensive part of /recap (download + transcribe +
# summarize, all billable) happens BEFORE the bot posts the result, so without a
# preflight a missing "Attach Files" channel override burns API spend on a recap
# that can never post. We check up front and bail for free.

# (discord.Permissions attribute, human label) — checked in this order.
# /recap posts the journal as a .md attachment at the end → needs Attach Files.
RECAP_REQUIRED_PERMS = [
    ("view_channel", "View Channel"),
    ("read_message_history", "Read Message History"),
    ("send_messages", "Send Messages"),
    ("attach_files", "Attach Files"),
]
# /initialize only reads channel history and DMs progress → no posting perms.
INITIALIZE_REQUIRED_PERMS = [
    ("view_channel", "View Channel"),
    ("read_message_history", "Read Message History"),
]


def _missing_perms(perms: discord.Permissions, required) -> list[str]:
    """Pure helper: human labels of `required` permissions absent from `perms`."""
    return [label for attr, label in required if not getattr(perms, attr, False)]


def bot_missing_channel_perms(interaction: discord.Interaction, required) -> list[str]:
    """Permissions the bot lacks in the interaction's channel to do `required`.

    Returns [] when all are present — or when there's no guild context to check
    (e.g. a DM, where these commands don't run anyway).
    """
    guild = interaction.guild
    channel = interaction.channel
    me = guild.me if guild else None
    if guild is None or channel is None or me is None:
        return []
    return _missing_perms(channel.permissions_for(me), required)

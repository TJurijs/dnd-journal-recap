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

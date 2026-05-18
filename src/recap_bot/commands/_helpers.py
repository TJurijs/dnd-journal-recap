"""Small Discord helpers shared by slash command modules."""

from __future__ import annotations

import discord


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

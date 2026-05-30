"""/admin — DM-only, bot-owner-only admin console.

  /admin settings  — detailed dump of the bot's *actual* current config
  /admin restart   — restart the process (re-syncs commands + reloads config
                     you've hand-edited on the server)
  /admin log       — recent usage: when, which server, category/channel,
                     command, user, profile, and cost

Settings are edited directly on the server (.env, models.yaml, prices.yaml)
and applied with /admin restart — there's no in-Discord settings editing.
"""

import asyncio
import logging
import os

import discord
from discord import app_commands

from recap_bot.bot import bot
from recap_bot.config import model_config, settings
from recap_bot.pipeline.cost import price_table
from recap_bot.pipeline.download import _youtube_cookies_path
from recap_bot.storage import usage

logger = logging.getLogger(__name__)

_ACTIONS = ["transcribe", "summarize", "roster_build", "scratchpad_build", "update_roster", "update_scratchpad"]
_STATUS_ICON = {"done": "✅", "failed": "⚠️", "cancelled": "⏹️"}


def _mask(value: str) -> str:
    if not value:
        return "(unset)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]}"


async def _is_owner(interaction: discord.Interaction) -> bool:
    if await bot.is_owner(interaction.user):
        return True
    await interaction.response.send_message("Only the bot owner can use `/admin`.", ephemeral=True)
    return False


async def _restart_after(delay: float) -> None:
    """Sleep, then hard-exit. Docker's `restart: unless-stopped` brings the
    container back, re-running on_ready (which re-syncs commands) and reloading
    .env / models.yaml / prices.yaml from disk."""
    await asyncio.sleep(delay)
    logger.warning("Restarting process via os._exit(0) for /admin restart")
    os._exit(0)


admin_group = app_commands.Group(
    name="admin",
    description="Bot owner admin console (DM only)",
    # DM-only at the group level (per-subcommand allowed_contexts is ignored for Groups).
    allowed_contexts=app_commands.AppCommandContext(guild=False, dm_channel=True, private_channel=False),
    allowed_installs=app_commands.AppInstallationType(guild=True, user=False),
)


async def _build_settings_text() -> str:
    lines: list[str] = ["**🤖 Bot**"]
    lines.append(f"• Logged in: `{bot.user}` (id `{getattr(bot.user, 'id', '?')}`)")
    try:
        app = await bot.application_info()
        lines.append(f"• Owner: `{app.owner}` (id `{app.owner.id}`)")
    except Exception:
        pass

    home_ids = settings.guild_ids
    lines += ["", "**🌐 Servers**"]
    lines.append(f"• Configured (DISCORD_GUILD_ID): "
                 f"{', '.join(f'`{g}`' for g in home_ids) or '(none — global sync)'}")
    member = list(bot.guilds)
    if member:
        for g in member:
            tag = " **[home]**" if g.id in home_ids else ""
            lines.append(f"   • `{g.name}` ({g.id}){tag}")
    else:
        lines.append("   • (not a member of any guilds)")

    lines += ["", "**🧠 Gemini & core**"]
    lines.append(f"• Default model (GEMINI_MODEL): `{settings.gemini_model}`")
    lines.append(f"• API key: `{_mask(settings.gemini_api_key)}`")
    lines.append(f"• Data dir: `{settings.data_dir}`")
    lines.append(f"• Log level: `{settings.log_level}`")
    lines.append(f"• Max VOD hours: `{settings.max_vod_hours}`")
    lines.append(f"• Default style: `{settings.default_style}`")
    lines.append(f"• Download rate limit: `{settings.download_rate_limit or '(none)'}`")
    lines.append(f"• ffmpeg bin: `{settings.ffmpeg_bin}`")

    lines += ["", "**🎚️ Model profiles** (models.yaml)"]
    for prof in model_config.profile_names():
        lines.append(f"• **{prof}**")
        for a in _ACTIONS:
            lines.append(f"   • {a}: `{model_config.get(a, prof)}`")

    models_used = sorted({model_config.get(a, p) for p in model_config.profile_names() for a in _ACTIONS})
    lines += ["", "**💵 Prices** (USD per 1M tokens, prices.yaml)"]
    for m in models_used:
        in_r, out_r = price_table.rates(m)
        lines.append(f"• `{m}`: in ${in_r} / out ${out_r}")

    cookies = _youtube_cookies_path()
    lines += ["", "**📺 YouTube**"]
    if cookies.exists():
        lines.append(f"• Cookies present: `{cookies}`")
    else:
        lines.append(f"• No cookies (`{cookies}`) — YouTube may hit bot-detection. Twitch unaffected.")

    lines += ["", "**📊 Usage log**"]
    lines.append(f"• `{usage.log_path()}` ({usage.event_count()} events) — see `/admin log`")

    return "\n".join(lines)


@admin_group.command(name="settings", description="Show the bot's current configuration in detail")
async def admin_settings(interaction: discord.Interaction):
    if not await _is_owner(interaction):
        return
    text = await _build_settings_text()
    if len(text) <= 1990:
        await interaction.response.send_message(text, ephemeral=True)
    else:
        from io import BytesIO
        await interaction.response.send_message(
            "⚙️ Current settings (attached — too long to inline):",
            file=discord.File(BytesIO(text.encode("utf-8")), filename="settings.md"),
            ephemeral=True,
        )


@admin_group.command(name="restart", description="Restart the bot (re-syncs commands, reloads server-edited config)")
async def admin_restart(interaction: discord.Interaction):
    if not await _is_owner(interaction):
        return
    await interaction.response.send_message(
        "🔄 Restarting in 3s — commands will re-sync and any config you edited on "
        "the server (.env / models.yaml / prices.yaml) will be reloaded. Expect "
        "~20–30s of unavailability; Discord reconnects automatically.",
        ephemeral=True,
    )
    asyncio.create_task(_restart_after(3.0))


def _vod_url(e: dict) -> str:
    """Best link to the source video: the stored URL, else reconstruct from the
    VOD id (numeric → Twitch, alphanumeric → YouTube) for entries logged before
    source_url was recorded."""
    url = (e.get("source_url") or "").strip()
    if url:
        return url
    vod_id = (e.get("vod_id") or "").strip()
    if not vod_id:
        return ""
    return (
        f"https://www.twitch.tv/videos/{vod_id}" if vod_id.isdigit()
        else f"https://youtu.be/{vod_id}"
    )


def _resolve_user(e: dict) -> str:
    """Human-readable requester: live cache lookup → stored name → raw id."""
    uid = e.get("user_id")
    if uid is not None and str(uid).isdigit():
        u = bot.get_user(int(uid))
        if u is not None:
            return u.display_name
    return (e.get("user_name") or "").strip() or (str(uid) if uid else "?")


def _md_link_text(title: str) -> str:
    """Sanitize a title for use inside a [text](url) masked link."""
    return title.replace("[", "(").replace("]", ")").replace("\n", " ").strip()


def _fmt_event(e: dict) -> str:
    ts = (e.get("ts") or "")[:16].replace("T", " ")
    icon = _STATUS_ICON.get(e.get("status", ""), "•")
    guild = e.get("guild_name") or e.get("guild_id") or "?"
    loc = e.get("location") or "?"
    cmd = e.get("event") or "?"
    user = _resolve_user(e)
    profile = e.get("profile") or "?"
    cost = e.get("cost_usd")
    cost_str = f"${cost:.4f}" if isinstance(cost, (int, float)) else "$—"
    bf = " _(backfill)_" if e.get("backfilled") else ""
    line1 = (
        f"{icon} `{ts}` · **{guild}** · {loc} · /{cmd} · "
        f"**{user}** · `{profile}` · **{cost_str}**{bf}"
    )
    # Video line (recaps only — initialize has no VOD). Inside an embed, a
    # masked [title](url) link renders as a clickable name with no preview.
    url = _vod_url(e)
    title = _md_link_text(e.get("vod_title") or e.get("vod_id") or "")
    if url and title:
        return f"{line1}\n↳ 📺 [{title}]({url})"
    if url:
        return f"{line1}\n↳ 📺 {url}"
    if title:
        return f"{line1}\n↳ 📺 {title}"
    return line1


@admin_group.command(name="log", description="Recent usage: when, server, channel, user, video, cost")
@app_commands.describe(limit="How many recent events to show (default 12, max 30)")
async def admin_log(interaction: discord.Interaction, limit: int = 12):
    if not await _is_owner(interaction):
        return
    limit = max(1, min(limit, 30))
    events = usage.read_recent(limit)
    if not events:
        await interaction.response.send_message(
            "📊 No usage recorded yet — run a `/recap` or `/initialize` first.",
            ephemeral=True,
        )
        return
    total = sum(e["cost_usd"] for e in events if isinstance(e.get("cost_usd"), (int, float)))
    title = f"📊 Last {len(events)} event(s) — known-cost total ${total:.4f}"
    body = "\n\n".join(_fmt_event(e) for e in events)
    # Embeds allow 4096-char descriptions AND render masked links (so the video
    # name is clickable). Fall back to a plain-text file only if it overflows.
    if len(body) <= 4096:
        embed = discord.Embed(title=title[:256], description=body, color=0x9B59B6)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        from io import BytesIO
        await interaction.response.send_message(
            f"{title} (attached — too many events to render inline; links not clickable in the file):",
            file=discord.File(BytesIO(body.encode("utf-8")), filename="usage_log.txt"),
            ephemeral=True,
        )


bot.tree.add_command(admin_group)

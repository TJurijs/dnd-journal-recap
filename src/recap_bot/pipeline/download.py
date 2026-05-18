import asyncio
import logging
import re
from pathlib import Path

import yt_dlp

from recap_bot.config import settings

logger = logging.getLogger(__name__)

_TWITCH_RE = re.compile(r"https?://(?:www\.)?twitch\.tv/(?:[^/]+/)?videos/(\d+)")


def get_vod_id(url: str) -> str:
    match = _TWITCH_RE.match(url)
    if not match:
        raise ValueError("Invalid Twitch VOD URL")
    return match.group(1)


_OG_TITLE_RE = re.compile(
    r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.IGNORECASE)


async def _fetch_title_from_html(url: str) -> str | None:
    """Fallback when yt-dlp's metadata probe fails: scrape the Twitch VOD page
    for `og:title` or `<title>`. Static HTML so no JS rendering needed."""
    try:
        # aiohttp is already a transitive dep via discord.py — no new requirement.
        import aiohttp
    except ImportError:
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning("HTML title scrape: HTTP %s for %s", resp.status, url)
                    return None
                html = await resp.text(errors="replace")
    except Exception:
        logger.exception("HTML title scrape failed for %s", url)
        return None

    m = _OG_TITLE_RE.search(html)
    if m:
        return m.group(1).strip() or None

    m = _HTML_TITLE_RE.search(html)
    if m:
        title = m.group(1).strip()
        # Twitch's <title> often ends with "- Twitch" — strip it.
        title = re.sub(r"\s*[-|]\s*Twitch\s*$", "", title)
        return title or None

    return None


async def get_vod_info(url: str) -> dict:
    """Probe a VOD for title, duration, uploader without downloading.

    Two-tier:
      1. yt-dlp metadata extraction (preferred — also gives duration + uploader)
      2. HTML scrape of `og:title` / `<title>` (fallback when yt-dlp's API path
         breaks; no duration/uploader available this way)
    """
    if not _TWITCH_RE.match(url):
        raise ValueError("Invalid Twitch VOD URL")

    url = url.split("?t=")[0].split("&t=")[0]

    opts = {"quiet": True, "noprogress": True}

    def _probe():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title") or "",
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", "Unknown"),
            }

    try:
        result = await asyncio.to_thread(_probe)
    except Exception:
        logger.warning("yt-dlp probe failed for %s — trying HTML fallback", url, exc_info=True)
        result = {"title": "", "duration": 0, "uploader": "Unknown"}

    if not result["title"]:
        # Either yt-dlp failed outright or it returned an empty title.
        scraped = await _fetch_title_from_html(url)
        if scraped:
            logger.info("Got title from HTML scrape: %r", scraped)
            result["title"] = scraped
    if not result["title"]:
        result["title"] = "Unknown VOD"
    return result


async def download_vod(url: str, dest_dir: Path, job_id: int, progress_queue: asyncio.Queue | None = None) -> Path:
    if not _TWITCH_RE.match(url):
        raise ValueError("Invalid Twitch VOD URL")

    # Strip timestamps
    url = url.split("?t=")[0].split("&t=")[0]

    # Twitch publishes audio-only as "audio_only" / "Audio_Only" — try those
    # first so we don't waste bandwidth on a video rendition. Fall back chain:
    #   1. audio_only / Audio_Only — Twitch's explicit audio HLS rendition
    #   2. bestaudio — yt-dlp's generic best-audio selector
    #   3. worst — last resort, but still smaller than 1080p
    opts = {
        # Prefer Twitch's audio-only HLS rendition if it exists; otherwise fall
        # back to the smallest combined stream (~160p, much smaller than
        # 1080p). The downstream "Convert to MP3" step handles either case.
        "format": "audio_only/Audio_Only/bestaudio/worst",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        "quiet": True,
        "noprogress": True,
        # HLS fragments are tiny and fetched sequentially by default. 8 in
        # parallel gets us ~5-8x the wall-time of a single-threaded download.
        "concurrent_fragment_downloads": 8,
    }
    if settings.download_rate_limit:
        opts["ratelimit"] = settings.download_rate_limit

    loop = asyncio.get_running_loop()

    def _download():
        if progress_queue:
            def _hook(d):
                if d["status"] == "downloading":
                    # For HLS streams (Twitch), fragment_count/fragment_index
                    # is the reliable progress signal — total_bytes_estimate
                    # only becomes accurate late in the download, so a pure
                    # byte-based bar sits at 0% for ages.
                    frag_index = d.get("fragment_index")
                    frag_count = d.get("fragment_count")
                    if frag_count and frag_index is not None and frag_count > 0:
                        pct = int(min(frag_index, frag_count) / frag_count * 100)
                        loop.call_soon_threadsafe(progress_queue.put_nowait, pct)
                        return
                    # Fallback for non-HLS sources (rare for Twitch).
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    downloaded = d.get("downloaded_bytes", 0)
                    if total and total > 0:
                        pct = int(downloaded / total * 100)
                        loop.call_soon_threadsafe(progress_queue.put_nowait, pct)
                elif d["status"] == "finished":
                    loop.call_soon_threadsafe(progress_queue.put_nowait, 100)

            opts["progress_hooks"] = [_hook]

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get("duration", 0)
            max_seconds = settings.max_vod_hours * 3600
            if duration > max_seconds:
                raise ValueError(
                    f"VOD is {duration//3600}h{duration%3600//60}m, max allowed is {settings.max_vod_hours}h"
                )
            ydl.download([url])
            # Find the downloaded source file
            files = sorted(dest_dir.glob("source.*"))
            if not files:
                raise RuntimeError("Download completed but no file found")
            # Return the largest file (the actual download, not any sidecar)
            return max(files, key=lambda p: p.stat().st_size)

    return await asyncio.to_thread(_download)

import asyncio
import logging
import re
import subprocess
from pathlib import Path

from recap_bot.config import settings

logger = logging.getLogger(__name__)

_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def _tail(text: str, n: int = 800) -> str:
    return text[-n:] if len(text) > n else text


async def extract_audio(source_path: Path, dest_dir: Path, duration: float = 0, progress_cb=None) -> Path:
    output = dest_dir / "audio.mp3"
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "warning",
        "-y",
        "-i", str(source_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "libmp3lame",
        "-b:a", "24k",
        str(output),
    ]

    if duration <= 0 or not progress_cb:
        # Simple path: no progress tracking
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg extract failed: {_tail(stderr.decode(errors='replace'))}")
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("ffmpeg produced no output")
        logger.info("Extracted audio to %s (%d bytes)", output, output.stat().st_size)
        return output

    # Progress tracking path. Note: do NOT use proc.communicate() here — it
    # internally reads stderr too, which races with our manual readline() loop
    # and produces "read() called while another coroutine is already waiting".
    # Instead: read stderr manually (driving the progress bar AND capturing the
    # tail for error reporting), discard stdout, then wait() on the process.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_chunks: list[str] = []

    async def _read_stderr():
        last_pct = 0
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            line_str = line.decode("utf-8", errors="ignore")
            stderr_chunks.append(line_str)
            m = _TIME_RE.search(line_str)
            if m:
                hours = int(m.group(1))
                minutes = int(m.group(2))
                seconds = float(m.group(3))
                current = hours * 3600 + minutes * 60 + seconds
                pct = int(current / duration * 100)
                if pct > last_pct:
                    last_pct = pct
                    await progress_cb(pct)

    await _read_stderr()
    await proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {_tail(''.join(stderr_chunks))}")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("ffmpeg produced no output")
    logger.info("Extracted audio to %s (%d bytes)", output, output.stat().st_size)
    return output

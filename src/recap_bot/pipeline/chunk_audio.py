import asyncio
import logging
from pathlib import Path

from recap_bot.config import settings

logger = logging.getLogger(__name__)


def _tail(text: str, n: int = 800) -> str:
    """Return the last `n` chars of `text` — the real ffmpeg error is at the end."""
    return text[-n:] if len(text) > n else text


async def chunk_audio(audio_path: Path, dest_dir: Path, num_chunks: int = 5, progress_cb=None) -> list[Path]:
    """Split an audio file into N roughly equal chunks using ffmpeg."""
    if num_chunks < 1:
        return [audio_path]

    # Probe duration so we can clamp the final chunk and avoid `-ss past EOF`
    # failures on truncated/imprecise audio.
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {_tail(stderr.decode(errors='replace'))}")

    try:
        duration = float(stdout.decode().strip())
    except ValueError:
        raise RuntimeError("ffprobe returned invalid duration")

    if duration <= 0:
        raise RuntimeError(f"Audio file has zero or negative duration ({duration})")

    chunk_duration = duration / num_chunks
    chunks: list[Path] = []

    for i in range(num_chunks):
        start = i * chunk_duration
        # Clamp the last chunk's `-t` so we never request past EOF. ffmpeg
        # tolerates short reads but `-ss` past EOF on a strict input can error.
        remaining = max(0.0, duration - start)
        chunk_t = min(chunk_duration, remaining)
        if chunk_t <= 0.0:
            raise RuntimeError(
                f"Chunk {i} would have zero duration "
                f"(start={start:.2f}s, file_duration={duration:.2f}s, chunk_duration={chunk_duration:.2f}s)"
            )

        output = dest_dir / f"chunk_{i:03d}.mp3"
        cmd = [
            settings.ffmpeg_bin,
            "-hide_banner",
            "-loglevel", "warning",
            "-y",
            "-ss", f"{start:.3f}",
            "-t", f"{chunk_t:.3f}",
            "-i", str(audio_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "libmp3lame",
            "-b:a", "24k",
            str(output),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = _tail(stderr.decode(errors="replace"))
            raise RuntimeError(
                f"ffmpeg chunk {i+1}/{num_chunks} failed (start={start:.1f}s, t={chunk_t:.1f}s, "
                f"audio_duration={duration:.1f}s): {err}"
            )
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg produced no output for chunk {i+1}")
        chunks.append(output)
        if progress_cb:
            await progress_cb(i + 1, num_chunks)
        logger.info("Created chunk %d/%d: %s (%.1fs)", i + 1, num_chunks, output, chunk_t)

    if progress_cb:
        await progress_cb(num_chunks, num_chunks)
    logger.info("Split %s into %d chunks (duration=%.1fs)", audio_path, len(chunks), duration)
    return chunks

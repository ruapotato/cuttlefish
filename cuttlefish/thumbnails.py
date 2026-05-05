"""Generate poster thumbnails from a video frame when no sidecar exists.

Lazy + cached: the first /poster/{id} request for an item with no real poster
runs ffmpeg once to grab a frame ~5 minutes in (or 10% in for short videos),
saves it to the cache dir, and serves it. Every subsequent request just
serves the file.

Cache location: $XDG_CACHE_HOME/cuttlefish/thumbs/ (defaults to
~/.cache/cuttlefish/thumbs/). Files are named by media id or episode id.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Optional

from cuttlefish.probe import get_duration

log = logging.getLogger(__name__)

# Serialize ffmpeg invocations so a 50-image page load doesn't fork 50
# ffmpeg processes at once.
_GEN_LOCK = threading.Lock()


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "cuttlefish" / "thumbs"


def media_thumb_path(media_id: int) -> Path:
    return cache_dir() / f"media-{media_id}.jpg"


def episode_thumb_path(episode_id: int) -> Path:
    return cache_dir() / f"episode-{episode_id}.jpg"


def generate_thumbnail(
    video_path: Path,
    output_path: Path,
    ffmpeg: str = "ffmpeg",
    timeout: float = 30.0,
) -> Optional[Path]:
    """Extract one frame from `video_path` to `output_path`, ~5 minutes in.

    Returns the output path on success, None on failure (we never raise —
    a missing thumbnail isn't worth crashing the page render).
    """
    if not video_path.is_file():
        return None
    duration = get_duration(video_path)
    if duration is None or duration <= 0:
        timestamp = 60.0  # 1 minute, hopeful default
    else:
        # 5 min in for normal-length content; 10% for shorter; clamped sensibly.
        timestamp = min(5 * 60, max(1.0, duration * 0.1))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        "-ss", f"{timestamp:.2f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", "scale='min(640,iw)':-2",
        "-q:v", "5",
        str(output_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        log.info("thumbnail generation failed for %s: %s", video_path, proc.stderr[-500:] if proc.returncode else "empty output")
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass
        return None
    return output_path


def get_or_generate(
    video_path: Path, output_path: Path, ffmpeg: str = "ffmpeg",
) -> Optional[Path]:
    """Return the cached thumbnail if present, otherwise try to generate it.

    The lock prevents a flood of concurrent requests from spawning N ffmpeg
    processes for the same library on first page load.
    """
    if output_path.is_file() and output_path.stat().st_size > 0:
        return output_path
    with _GEN_LOCK:
        # Re-check under the lock — another request may have just written it
        if output_path.is_file() and output_path.stat().st_size > 0:
            return output_path
        return generate_thumbnail(video_path, output_path, ffmpeg=ffmpeg)

"""Lightweight ffprobe wrapper to read media metadata (just duration for now).

Intentionally narrow: shells out to ffprobe with a very specific arg set,
returns a single float or None. Does not raise on failure — callers treat
None as 'unknown' and move on.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def get_duration(path: Path, ffprobe: str = "ffprobe", timeout: float = 30.0) -> Optional[float]:
    if not path.is_file():
        return None
    try:
        if path.stat().st_size == 0:
            return None
    except OSError:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe, "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return float(out)
    except ValueError:
        return None

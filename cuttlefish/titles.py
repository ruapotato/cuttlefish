"""Filename → display-title cleanup helpers.

Intentionally simple. The real source of truth for canonical titles is TMDb
(added later). These helpers produce a 'best guess' from a filename so the
scanner has something to display before metadata lookup runs.
"""
from __future__ import annotations

import re
from pathlib import Path

# yt-dlp default trailing pattern: "Title-VIDEOID" where VIDEOID is 11 chars
# from YouTube's base64-url alphabet [A-Za-z0-9_-].
_YOUTUBE_ID = re.compile(r"-[A-Za-z0-9_-]{11}$")

# Common scene/release tokens that often trail a title on rips.
_RELEASE_NOISE = re.compile(
    r"\b("
    r"\d{3,4}p"
    r"|x264|x265|h264|h265|hevc|av1|xvid|divx"
    r"|bluray|bdrip|brrip|web[-]?dl|webrip|hdtv|dvdrip|hdrip"
    r"|aac|ac3|dts|flac|opus|mp3"
    r"|repack|proper|extended|remastered|directors[. ]cut|uncut"
    r")\b.*",
    re.IGNORECASE,
)


def title_from_filename(name: str) -> str:
    """Best-effort canonical title from a filename or directory name."""
    p = Path(name)
    stem = p.stem if "." in p.name else name
    stem = _YOUTUBE_ID.sub("", stem).strip()
    stem = _RELEASE_NOISE.sub("", stem).strip()
    stem = re.sub(r"[._]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -")
    return stem

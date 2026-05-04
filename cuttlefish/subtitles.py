"""Subtitle helpers — locate sidecar files and convert SRT to WebVTT.

HTML5 <video> / <audio> only natively renders WebVTT in <track> elements,
not SRT. The two formats are nearly identical; the meaningful differences
are the leading 'WEBVTT' header and '.' (vs ',') as the millisecond
separator. We convert on-the-fly when serving so the user doesn't have to
keep two copies of every subtitle.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

SUBTITLE_EXTS = (".srt", ".vtt", ".ass", ".ssa")


def srt_to_vtt(srt_text: str) -> str:
    out = ["WEBVTT", ""]
    for line in srt_text.splitlines():
        # Cue-timestamp lines look like "00:00:00,000 --> 00:00:01,500".
        # Only the comma-vs-period separator differs from VTT.
        if "-->" in line:
            line = line.replace(",", ".")
        out.append(line)
    return "\n".join(out) + "\n"


def find_sidecar_subtitle(video_path: Path) -> Optional[Path]:
    """Look for a subtitle file with the same stem (or a stem.LANG variant)."""
    if not video_path.is_file():
        return None
    stem = video_path.stem
    parent = video_path.parent
    for ext in SUBTITLE_EXTS:
        cand = parent / f"{stem}{ext}"
        if cand.is_file():
            return cand
    for cand in sorted(parent.glob(f"{stem}.*")):
        if cand.is_file() and cand.suffix.lower() in SUBTITLE_EXTS:
            return cand
    return None


def subtitle_for_media(conn: sqlite3.Connection, media_id: int) -> Optional[Path]:
    """Return a Path to the subtitle for a movie or audiobook media item, or
    None if not available."""
    row = conn.execute(
        "SELECT m.source_path, e.subtitle_path, e.video_path "
        "FROM media m LEFT JOIN encoded_files e ON e.media_id = m.id "
        "WHERE m.id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        return None
    if row["subtitle_path"]:
        p = Path(row["subtitle_path"])
        if p.is_file():
            return p
    # Look next to the encoded video first if we have one
    if row["video_path"]:
        sidecar = find_sidecar_subtitle(Path(row["video_path"]))
        if sidecar:
            return sidecar
    src = Path(row["source_path"])
    if src.is_file():
        return find_sidecar_subtitle(src)
    if src.is_dir():
        # Movies in clean layout: search inside
        for child in src.iterdir():
            if child.is_file() and child.suffix.lower() in SUBTITLE_EXTS:
                return child
    return None


def subtitle_for_episode(conn: sqlite3.Connection, episode_id: int) -> Optional[Path]:
    row = conn.execute(
        "SELECT e.source_path, ee.subtitle_path, ee.video_path "
        "FROM tv_episodes e LEFT JOIN encoded_episodes ee ON ee.episode_id = e.id "
        "WHERE e.id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    if row["subtitle_path"]:
        p = Path(row["subtitle_path"])
        if p.is_file():
            return p
    if row["video_path"]:
        sidecar = find_sidecar_subtitle(Path(row["video_path"]))
        if sidecar:
            return sidecar
    src = Path(row["source_path"])
    if src.is_file():
        return find_sidecar_subtitle(src)
    return None

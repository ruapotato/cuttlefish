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
ASR_INFIX = ".asr"  # ASR-generated SRTs land at <stem>.asr.srt


def srt_to_vtt(srt_text: str) -> str:
    out = ["WEBVTT", ""]
    for line in srt_text.splitlines():
        # Cue-timestamp lines look like "00:00:00,000 --> 00:00:01,500".
        # Only the comma-vs-period separator differs from VTT.
        if "-->" in line:
            line = line.replace(",", ".")
        out.append(line)
    return "\n".join(out) + "\n"


def _is_asr_path(path: Path) -> bool:
    """Detect <stem>.asr.<ext> as our auto-generated variant."""
    return path.suffixes[-2:-1] == [ASR_INFIX] and path.suffix.lower() in SUBTITLE_EXTS


def find_sidecar_subtitle(video_path: Path) -> Optional[Path]:
    """Look for an *original* subtitle file with the same stem (or a
    stem.LANG variant). Does NOT match the ASR-generated <stem>.asr.<ext>
    variant — use find_asr_subtitle for that."""
    if not video_path.is_file():
        return None
    stem = video_path.stem
    parent = video_path.parent
    for ext in SUBTITLE_EXTS:
        cand = parent / f"{stem}{ext}"
        if cand.is_file():
            return cand
    for cand in sorted(parent.glob(f"{stem}.*")):
        if not cand.is_file():
            continue
        if cand.suffix.lower() not in SUBTITLE_EXTS:
            continue
        if _is_asr_path(cand):
            continue  # belongs to the ASR variant lookup
        return cand
    return None


def find_asr_subtitle(video_path: Path) -> Optional[Path]:
    """Look for the ASR-generated <stem>.asr.srt sitting next to the video."""
    if not video_path.is_file():
        return None
    parent = video_path.parent
    stem = video_path.stem
    for ext in SUBTITLE_EXTS:
        cand = parent / f"{stem}{ASR_INFIX}{ext}"
        if cand.is_file():
            return cand
    return None


def asr_output_path_for(video_path: Path) -> Path:
    """Where the ASR worker should write its SRT for this video."""
    return video_path.with_suffix(f"{ASR_INFIX}.srt")


def _resolve_video_for_media(row) -> Optional[Path]:
    """The video file we should look 'next to' when hunting for sidecars."""
    if row["video_path"]:
        v = Path(row["video_path"])
        if v.is_file():
            return v
    src = Path(row["source_path"])
    if src.is_file():
        return src
    if src.is_dir():
        for child in src.iterdir():
            if child.is_file() and child.suffix.lower() in {
                ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv"
            }:
                return child
    return None


def subtitle_variants_for_media(
    conn: sqlite3.Connection, media_id: int,
) -> dict[str, Path]:
    """Return the set of available subtitle tracks for a media item:
        {"original": Path, "asr": Path}
    with each entry only present if the file actually exists on disk.

    The 'original' slot covers anything that isn't ASR-generated — sidecar
    SRTs, OpenSubtitles downloads, encoded-folder sidecars. The 'asr' slot
    is reserved for files we wrote at <stem>.asr.srt.
    """
    row = conn.execute(
        "SELECT m.source_path, e.subtitle_path, e.video_path "
        "FROM media m LEFT JOIN encoded_files e ON e.media_id = m.id "
        "WHERE m.id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, Path] = {}
    video = _resolve_video_for_media(row)
    # Original: prefer DB-tracked sidecar (set during scan), fall back to
    # filesystem search next to the video.
    if row["subtitle_path"]:
        p = Path(row["subtitle_path"])
        if p.is_file() and not _is_asr_path(p):
            out["original"] = p
    if "original" not in out and video is not None:
        sidecar = find_sidecar_subtitle(video)
        if sidecar:
            out["original"] = sidecar
    # ASR: filesystem-only — distinct file, not tracked in encoded_files.
    if video is not None:
        asr = find_asr_subtitle(video)
        if asr:
            out["asr"] = asr
    return out


def subtitle_for_media(
    conn: sqlite3.Connection, media_id: int, variant: Optional[str] = None,
) -> Optional[Path]:
    """Return a single subtitle path. variant=None picks the best available
    (ASR if present, otherwise original)."""
    variants = subtitle_variants_for_media(conn, media_id)
    if not variants:
        return None
    if variant in variants:
        return variants[variant]
    if variant is None:
        return variants.get("asr") or variants.get("original")
    return None


def subtitle_variants_for_episode(
    conn: sqlite3.Connection, episode_id: int,
) -> dict[str, Path]:
    row = conn.execute(
        "SELECT e.source_path, ee.subtitle_path, ee.video_path "
        "FROM tv_episodes e LEFT JOIN encoded_episodes ee ON ee.episode_id = e.id "
        "WHERE e.id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        return {}
    out: dict[str, Path] = {}
    video = None
    if row["video_path"] and Path(row["video_path"]).is_file():
        video = Path(row["video_path"])
    elif row["source_path"] and Path(row["source_path"]).is_file():
        video = Path(row["source_path"])
    if row["subtitle_path"]:
        p = Path(row["subtitle_path"])
        if p.is_file() and not _is_asr_path(p):
            out["original"] = p
    if "original" not in out and video is not None:
        sidecar = find_sidecar_subtitle(video)
        if sidecar:
            out["original"] = sidecar
    if video is not None:
        asr = find_asr_subtitle(video)
        if asr:
            out["asr"] = asr
    return out


def subtitle_for_episode(
    conn: sqlite3.Connection, episode_id: int, variant: Optional[str] = None,
) -> Optional[Path]:
    variants = subtitle_variants_for_episode(conn, episode_id)
    if not variants:
        return None
    if variant in variants:
        return variants[variant]
    if variant is None:
        return variants.get("asr") or variants.get("original")
    return None

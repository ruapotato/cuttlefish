"""Encoding worker — pulls jobs from SQLite, runs ffmpeg, never touches originals.

Encoder output convention (per design memo):

    <library_root>/
      Big Buck Bunny-VID.mp4         ← original, untouched
      Big Buck Bunny/                 ← clean output dir, alongside
        Big Buck Bunny.mp4            ← H.264/AAC/MP4, 1080p max
        Big Buck Bunny.jpg            ← copied sidecar poster, if found
        Big Buck Bunny.srt            ← copied sidecar subtitle, if found

Originals are NEVER deleted automatically. Cleanup is a separate manual flow
behind the admin UI (see admin endpoints in cuttlefish/server/app.py).
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Process-local flag set when an encode-worker thread is spawned in this
# server process. Mirrors the ASR worker indicator so /admin/encode can
# tell the user whether queued jobs will actually be picked up here.
_worker_in_process = False


def mark_worker_started() -> None:
    global _worker_in_process
    _worker_in_process = True


def is_worker_in_process() -> bool:
    return _worker_in_process


VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv")
POSTER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SUBTITLE_EXTS = (".srt", ".vtt", ".ass")

# H.264 High @ Level 4.0 = 1080p30 max — the broadest "modern device" baseline.
# CRF 22 is visually transparent for most sources at this resolution.
FFMPEG_VIDEO_FILTER = (
    "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease,"
    "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # H.264 needs even dimensions
)
FFMPEG_ARGS = (
    "-vf", FFMPEG_VIDEO_FILTER,
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "22",
    "-profile:v", "high",
    "-level", "4.0",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "128k",
    "-ac", "2",
    "-movflags", "+faststart",
    "-map_metadata", "-1",
)


@dataclass
class EncodeResult:
    media_id: int
    clean_dir: Path
    video_path: Path
    poster_path: Optional[Path]
    subtitle_path: Optional[Path]
    size_bytes: int


class EncodeError(Exception):
    pass


# --- helpers --------------------------------------------------------------


def safe_dirname(name: str) -> str:
    """Sanitize a title for use as a directory/filename."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    cleaned = cleaned.strip(". ").rstrip()
    return cleaned or "untitled"


def _resolve_source_video(source_path: Path) -> Path:
    if source_path.is_file():
        return source_path
    if source_path.is_dir():
        for child in sorted(source_path.iterdir()):
            if child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                return child
    raise EncodeError(f"no source video for {source_path}")


def _find_sidecar(source_video: Path, exts: tuple[str, ...]) -> Optional[Path]:
    """Look for a sidecar file with one of the given extensions next to the video."""
    stem = source_video.stem
    parent = source_video.parent
    # Exact stem match: "movie.srt" / "movie.jpg"
    for ext in exts:
        cand = parent / f"{stem}{ext}"
        if cand.is_file():
            return cand
    # Stem + language tag: "movie.en.srt" / "movie.eng.srt"
    if exts == SUBTITLE_EXTS:
        for cand in sorted(parent.glob(f"{stem}.*")):
            if cand.is_file() and cand.suffix.lower() in exts:
                return cand
    return None


# --- main encode entry point ---------------------------------------------


def encode_media(
    conn: sqlite3.Connection,
    media_id: int,
    ffmpeg: str = "ffmpeg",
    overwrite: bool = False,
) -> EncodeResult:
    """Re-encode the media item with id=media_id to the clean Title/ layout.

    Returns the EncodeResult and writes a row into encoded_files. The caller
    is responsible for queuing this from the admin endpoint or a worker loop.
    """
    row = conn.execute(
        "SELECT m.source_path, m.title_guess, l.root_path FROM media m "
        "JOIN libraries l ON l.id = m.library_id WHERE m.id = ?",
        (media_id,),
    ).fetchone()
    if row is None:
        raise EncodeError(f"media {media_id} not found")
    source_path = Path(row["source_path"])
    title = row["title_guess"]
    safe_title = safe_dirname(title)
    source_video = _resolve_source_video(source_path)

    # Output dir lives alongside the source. For a loose file that's the
    # library root; for a directory source it's the directory's parent.
    base_dir = (
        source_path.parent if source_path.is_file() else source_path.parent
    )
    clean_dir = base_dir / safe_title
    out_video = clean_dir / f"{safe_title}.mp4"
    if out_video.exists() and not overwrite:
        raise EncodeError(f"output already exists: {out_video}")
    clean_dir.mkdir(parents=True, exist_ok=True)

    # Build ffmpeg command
    cmd = [ffmpeg, "-nostdin", "-y" if overwrite else "-n", "-i", str(source_video)]
    cmd.extend(FFMPEG_ARGS)
    cmd.append(str(out_video))
    log.info("running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # Don't leave a half-written file
        try:
            out_video.unlink()
        except FileNotFoundError:
            pass
        raise EncodeError(
            f"ffmpeg failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
        )
    if not out_video.is_file() or out_video.stat().st_size == 0:
        raise EncodeError(f"ffmpeg produced no/empty output at {out_video}")

    # Copy any sidecar poster/subtitle, normalized to safe_title.<ext>
    poster_dst: Optional[Path] = None
    subtitle_dst: Optional[Path] = None
    poster_src = _find_sidecar(source_video, POSTER_EXTS)
    if poster_src:
        poster_dst = clean_dir / f"{safe_title}{poster_src.suffix.lower()}"
        if not poster_dst.exists():
            shutil.copy2(poster_src, poster_dst)
    sub_src = _find_sidecar(source_video, SUBTITLE_EXTS)
    if sub_src:
        subtitle_dst = clean_dir / f"{safe_title}{sub_src.suffix.lower()}"
        if not subtitle_dst.exists():
            shutil.copy2(sub_src, subtitle_dst)

    size_bytes = out_video.stat().st_size
    with conn:
        conn.execute(
            """
            INSERT INTO encoded_files (media_id, clean_dir, video_path, subtitle_path, poster_path, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(media_id) DO UPDATE SET
                clean_dir     = excluded.clean_dir,
                video_path    = excluded.video_path,
                subtitle_path = excluded.subtitle_path,
                poster_path   = excluded.poster_path,
                size_bytes    = excluded.size_bytes,
                encoded_at    = CURRENT_TIMESTAMP
            """,
            (
                media_id,
                str(clean_dir),
                str(out_video),
                str(subtitle_dst) if subtitle_dst else None,
                str(poster_dst) if poster_dst else None,
                size_bytes,
            ),
        )
    return EncodeResult(
        media_id=media_id,
        clean_dir=clean_dir,
        video_path=out_video,
        poster_path=poster_dst,
        subtitle_path=subtitle_dst,
        size_bytes=size_bytes,
    )


# --- job queue ------------------------------------------------------------


def enqueue_encode(
    conn: sqlite3.Connection, media_id: int, payload: Optional[dict] = None
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, media_id, payload) VALUES ('encode', ?, ?)",
            (media_id, json.dumps(payload) if payload else None),
        )
    return cur.lastrowid


def enqueue_episode_encode(
    conn: sqlite3.Connection, episode_id: int, payload: Optional[dict] = None
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO jobs (kind, episode_id, payload) VALUES ('encode', ?, ?)",
            (episode_id, json.dumps(payload) if payload else None),
        )
    return cur.lastrowid


def encode_episode(
    conn: sqlite3.Connection,
    episode_id: int,
    ffmpeg: str = "ffmpeg",
    overwrite: bool = False,
) -> EncodeResult:
    """Re-encode a single TV episode into <show_root>/Title/Season XX/SxxExx - <ep>.mp4

    Originals are kept; the encoded file lives alongside in a sibling clean
    folder (one per season).
    """
    row = conn.execute(
        "SELECT e.source_path, e.season, e.episode, e.title_guess AS ep_title, "
        "       m.title_guess AS show_title, m.source_path AS show_path "
        "FROM tv_episodes e JOIN media m ON m.id = e.show_id "
        "WHERE e.id = ?",
        (episode_id,),
    ).fetchone()
    if row is None:
        raise EncodeError(f"episode {episode_id} not found")
    source_video = Path(row["source_path"])
    if not source_video.is_file():
        raise EncodeError(f"episode source missing: {source_video}")
    show_dir = Path(row["show_path"])
    safe_show = safe_dirname(row["show_title"])
    season_dir_name = f"Season {row['season']:02d}"
    ep_label = f"S{row['season']:02d}E{row['episode']:02d}"
    ep_title = (row["ep_title"] or "").strip()
    base = f"{safe_show} - {ep_label}"
    if ep_title and ep_title != row["show_title"]:
        base = f"{safe_show} - {ep_label} - {safe_dirname(ep_title)[:80]}"
    clean_dir = show_dir.parent / safe_show / season_dir_name
    out_video = clean_dir / f"{base}.mp4"
    if out_video.exists() and not overwrite:
        raise EncodeError(f"output already exists: {out_video}")
    clean_dir.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-nostdin", "-y" if overwrite else "-n", "-i", str(source_video)]
    cmd.extend(FFMPEG_ARGS)
    cmd.append(str(out_video))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            out_video.unlink()
        except FileNotFoundError:
            pass
        raise EncodeError(
            f"ffmpeg failed (exit {proc.returncode}): {proc.stderr[-2000:]}"
        )
    if not out_video.is_file() or out_video.stat().st_size == 0:
        raise EncodeError(f"ffmpeg produced no/empty output at {out_video}")

    # Sidecars from source
    poster_dst: Optional[Path] = None
    sub_dst: Optional[Path] = None
    poster_src = _find_sidecar(source_video, POSTER_EXTS)
    if poster_src:
        poster_dst = out_video.with_suffix(poster_src.suffix.lower())
        if not poster_dst.exists():
            shutil.copy2(poster_src, poster_dst)
    sub_src = _find_sidecar(source_video, SUBTITLE_EXTS)
    if sub_src:
        sub_dst = out_video.with_suffix(sub_src.suffix.lower())
        if not sub_dst.exists():
            shutil.copy2(sub_src, sub_dst)

    size_bytes = out_video.stat().st_size
    with conn:
        conn.execute(
            """
            INSERT INTO encoded_episodes
                (episode_id, clean_dir, video_path, subtitle_path, poster_path, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(episode_id) DO UPDATE SET
                clean_dir     = excluded.clean_dir,
                video_path    = excluded.video_path,
                subtitle_path = excluded.subtitle_path,
                poster_path   = excluded.poster_path,
                size_bytes    = excluded.size_bytes,
                encoded_at    = CURRENT_TIMESTAMP
            """,
            (
                episode_id,
                str(clean_dir),
                str(out_video),
                str(sub_dst) if sub_dst else None,
                str(poster_dst) if poster_dst else None,
                size_bytes,
            ),
        )
    return EncodeResult(
        media_id=episode_id,  # repurposed: caller can disambiguate by job kind
        clean_dir=clean_dir,
        video_path=out_video,
        poster_path=poster_dst,
        subtitle_path=sub_dst,
        size_bytes=size_bytes,
    )


def claim_next_job(conn: sqlite3.Connection, kind: str = "encode") -> Optional[dict]:
    with conn:
        row = conn.execute(
            """
            UPDATE jobs SET status = 'running', started_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM jobs
                WHERE kind = ? AND status = 'queued'
                ORDER BY id LIMIT 1
            )
            RETURNING id, kind, media_id, episode_id, payload
            """,
            (kind,),
        ).fetchone()
    return dict(row) if row else None


def mark_done(conn: sqlite3.Connection, job_id: int, result: Optional[dict] = None) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=CURRENT_TIMESTAMP, result=? WHERE id=?",
            (json.dumps(result) if result else None, job_id),
        )


def mark_failed(conn: sqlite3.Connection, job_id: int, error: str) -> None:
    with conn:
        conn.execute(
            "UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, error=? WHERE id=?",
            (error[-2000:], job_id),
        )


def run_worker(
    db_path: Optional[Path | str] = None,
    once: bool = False,
    poll_interval: float = 5.0,
    ffmpeg: str = "ffmpeg",
) -> int:
    """Main worker loop. Returns the count of jobs processed."""
    from cuttlefish import db as _db

    conn = _db.connect(db_path)
    processed = 0
    while True:
        job = claim_next_job(conn, kind="encode")
        if job is None:
            if once:
                return processed
            time.sleep(poll_interval)
            continue
        log.info(
            "processing job %s (media=%s episode=%s)",
            job["id"], job["media_id"], job["episode_id"],
        )
        try:
            if job["episode_id"]:
                result = encode_episode(conn, job["episode_id"], ffmpeg=ffmpeg)
            elif job["media_id"]:
                result = encode_media(conn, job["media_id"], ffmpeg=ffmpeg)
            else:
                raise EncodeError("job has neither media_id nor episode_id")
            mark_done(
                conn,
                job["id"],
                {
                    "video_path": str(result.video_path),
                    "size_bytes": result.size_bytes,
                },
            )
            processed += 1
        except Exception as e:
            log.exception("job %s failed", job["id"])
            mark_failed(conn, job["id"], str(e))
        if once:
            return processed

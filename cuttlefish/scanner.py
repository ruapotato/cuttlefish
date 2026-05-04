"""Library scanner — walks the filesystem and populates the DB.

Detection rules (per design memos):

- Movies library: a top-level entry that is a video file is a loose movie;
  a top-level directory containing video files is a movie folder (clean
  layout). Everything else is skipped.
- TV library: top-level dir = show; inside, season folders match
  S\\d+ / Season \\d+ / Season \\d+; inside seasons, video files are episodes
  with S\\d+E\\d+ marker parsed when present.
- Audiobooks library: recursive — any folder with direct audio file children
  is a *book*; otherwise descend into its subfolders. Branches resolve
  independently, supporting arbitrary depth.

The scanner is read-only against the filesystem.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cuttlefish.titles import title_from_filename

VIDEO_EXTS = frozenset({".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv"})
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav", ".aac"})

_SEASON_DIR = re.compile(r"^(?:s|season)\s*(\d{1,3})$", re.IGNORECASE)
_EP_MARKER = re.compile(r"s(\d{1,3})e(\d{1,3})", re.IGNORECASE)


@dataclass
class ScanResult:
    movies_added: int = 0
    shows_added: int = 0
    episodes_added: int = 0
    audiobooks_added: int = 0
    tracks_added: int = 0
    skipped: int = 0

    def merge(self, other: "ScanResult") -> None:
        self.movies_added += other.movies_added
        self.shows_added += other.shows_added
        self.episodes_added += other.episodes_added
        self.audiobooks_added += other.audiobooks_added
        self.tracks_added += other.tracks_added
        self.skipped += other.skipped


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in VIDEO_EXTS


def is_audio(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in AUDIO_EXTS


def scan_library(
    conn: sqlite3.Connection, library_id: int, root: Path, kind: str
) -> ScanResult:
    if not root.is_dir():
        raise ValueError(f"library root not found or not a directory: {root}")
    if kind == "movies":
        return _scan_movies(conn, library_id, root)
    if kind == "tv":
        return _scan_tv(conn, library_id, root)
    if kind == "audiobooks":
        return _scan_audiobooks(conn, library_id, root)
    raise ValueError(f"unknown library kind: {kind!r}")


def _upsert_media(
    conn: sqlite3.Connection,
    library_id: int,
    kind: str,
    source_path: Path,
    title: str,
) -> int:
    with conn:
        conn.execute(
            """
            INSERT INTO media (library_id, kind, source_path, title_guess)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(library_id, source_path) DO UPDATE SET
                last_seen_at = CURRENT_TIMESTAMP,
                title_guess  = excluded.title_guess
            """,
            (library_id, kind, str(source_path), title),
        )
    row = conn.execute(
        "SELECT id FROM media WHERE library_id = ? AND source_path = ?",
        (library_id, str(source_path)),
    ).fetchone()
    return row["id"]


def _iter_visible(folder: Path):
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        yield entry


def _scan_movies(conn: sqlite3.Connection, library_id: int, root: Path) -> ScanResult:
    result = ScanResult()
    for entry in _iter_visible(root):
        if is_video(entry):
            title = title_from_filename(entry.name)
            _upsert_media(conn, library_id, "movie", entry, title)
            result.movies_added += 1
        elif entry.is_dir():
            videos = [c for c in entry.iterdir() if is_video(c)]
            if videos:
                title = title_from_filename(entry.name)
                _upsert_media(conn, library_id, "movie", entry, title)
                result.movies_added += 1
            else:
                result.skipped += 1
        else:
            result.skipped += 1
    return result


def _scan_tv(conn: sqlite3.Connection, library_id: int, root: Path) -> ScanResult:
    result = ScanResult()
    for show_dir in _iter_visible(root):
        if not show_dir.is_dir():
            result.skipped += 1
            continue
        title = title_from_filename(show_dir.name)
        show_id = _upsert_media(conn, library_id, "tv_show", show_dir, title)
        result.shows_added += 1
        for season_dir in _iter_visible(show_dir):
            if not season_dir.is_dir():
                continue
            m = _SEASON_DIR.match(season_dir.name)
            if not m:
                continue
            season_num = int(m.group(1))
            for ep_file in _iter_visible(season_dir):
                if not is_video(ep_file):
                    continue
                em = _EP_MARKER.search(ep_file.name)
                episode_num = int(em.group(2)) if em else 0
                ep_title = title_from_filename(ep_file.name)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO tv_episodes (show_id, season, episode, source_path, title_guess)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(show_id, source_path) DO UPDATE SET
                            last_seen_at = CURRENT_TIMESTAMP,
                            season       = excluded.season,
                            episode      = excluded.episode,
                            title_guess  = excluded.title_guess
                        """,
                        (show_id, season_num, episode_num, str(ep_file), ep_title),
                    )
                result.episodes_added += 1
    return result


def _scan_audiobooks(
    conn: sqlite3.Connection, library_id: int, root: Path
) -> ScanResult:
    result = ScanResult()

    def walk(folder: Path) -> None:
        if folder.name.startswith("."):
            return
        children = list(folder.iterdir())
        audio_children = sorted(
            (c for c in children if is_audio(c)), key=lambda p: p.name
        )
        if audio_children:
            title = title_from_filename(folder.name)
            book_id = _upsert_media(conn, library_id, "audiobook", folder, title)
            result.audiobooks_added += 1
            for idx, track in enumerate(audio_children):
                with conn:
                    conn.execute(
                        """
                        INSERT INTO audiobook_tracks (book_id, order_index, source_path)
                        VALUES (?, ?, ?)
                        ON CONFLICT(book_id, source_path) DO UPDATE SET
                            order_index  = excluded.order_index,
                            last_seen_at = CURRENT_TIMESTAMP
                        """,
                        (book_id, idx, str(track)),
                    )
                result.tracks_added += 1
            return
        for sub in sorted((c for c in children if c.is_dir()), key=lambda p: p.name):
            walk(sub)

    for top in _iter_visible(root):
        if top.is_dir():
            walk(top)
    return result

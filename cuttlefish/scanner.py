"""Library scanner — walks the filesystem and populates the DB.

A library is just a folder. Cuttlefish decides what each subfolder is by
looking at its contents. There's no per-library "kind" setting; one library
can contain movies and TV shows and audiobooks side by side.

Per-folder classification rules:

  loose video file at the library root        → movie
  folder with audio files in it directly      → audiobook (one book)
  folder with video files in it directly      → movie (extras subdirs OK)
  folder containing only subfolders:
    subfolders contain audio                  → audiobook series; recurse
    otherwise                                 → TV show; subfolders are seasons

Each subfolder is classified independently — a library can mix all kinds.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cuttlefish import thumbnails as _thumbs
from cuttlefish.probe import get_duration
from cuttlefish.titles import title_from_filename

VIDEO_EXTS = frozenset({".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv"})
AUDIO_EXTS = frozenset({".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".wav", ".aac"})
POSTER_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})

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


# --- helpers --------------------------------------------------------------


def _iter_visible(folder: Path):
    if not folder.is_dir():
        return
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        yield entry


def _direct_children(folder: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Return (videos, audios, subdirs) that are direct visible children."""
    videos: list[Path] = []
    audios: list[Path] = []
    subdirs: list[Path] = []
    for c in _iter_visible(folder):
        if c.is_file():
            ext = c.suffix.lower()
            if ext in VIDEO_EXTS:
                videos.append(c)
            elif ext in AUDIO_EXTS:
                audios.append(c)
        elif c.is_dir():
            subdirs.append(c)
    return videos, audios, subdirs


def _find_poster_for(path: Path) -> Path | None:
    """Find a poster image associated with `path`.

    - File: look for sibling with same stem and an image extension.
    - Directory: prefer `<dirname>.<ext>`, then `poster.<ext>`, then
      `cover.<ext>`, then any image whose stem matches a sibling video,
      then the first image found.
    """
    if path.is_file():
        stem = path.stem
        for ext in POSTER_EXTS:
            cand = path.parent / f"{stem}{ext}"
            if cand.is_file():
                return cand
        return None
    if not path.is_dir():
        return None
    children = list(path.iterdir())
    images = sorted(c for c in children if c.is_file() and c.suffix.lower() in POSTER_EXTS)
    if not images:
        return None
    dir_stem = path.name
    for preferred in (dir_stem, "poster", "cover", "folder"):
        for ext in POSTER_EXTS:
            cand = path / f"{preferred}{ext}"
            if cand.is_file():
                return cand
    videos = [c for c in children if c.is_file() and c.suffix.lower() in VIDEO_EXTS]
    if videos:
        v_stem = videos[0].stem
        for img in images:
            if img.stem == v_stem:
                return img
    return images[0]


# --- per-folder classification -------------------------------------------


def classify_folder(folder: Path) -> str | None:
    """Return one of: 'movie', 'audiobook', 'tv_show', 'audiobook_grouping',
    or None if the folder doesn't contain media we can use.

    Each subfolder of a library is classified independently, so one library
    can mix movies, TV shows, and audiobooks freely.

    Rule: video anywhere in the tree wins. A folder is only an audiobook if
    no video file exists below it. This protects against a TV show folder
    that happens to have a theme.mp3 at its root being misclassified as an
    audiobook just because audio sits directly inside it.
    """
    videos, audios, subdirs = _direct_children(folder)

    # Direct video files = movie folder. Subdirs (if any) are extras.
    if videos:
        return "movie"

    # Any video below this folder = it's a TV show (subfolders are seasons),
    # NOT an audiobook — even if audio files (theme.mp3, etc.) sit at the
    # top level alongside the season directories.
    if _tree_has_video(folder):
        return "tv_show"

    # No video anywhere from here down. Now audio decides.
    if audios:
        return "audiobook"
    if subdirs and _tree_has_audio(folder):
        return "audiobook_grouping"
    return None


def _tree_has_audio(folder: Path, max_depth: int = 6) -> bool:
    if max_depth < 0:
        return False
    try:
        children = list(folder.iterdir())
    except OSError:
        return False
    for c in children:
        if c.name.startswith("."):
            continue
        if c.is_file() and c.suffix.lower() in AUDIO_EXTS:
            return True
        if c.is_dir() and _tree_has_audio(c, max_depth - 1):
            return True
    return False


def _tree_has_video(folder: Path, max_depth: int = 6) -> bool:
    if max_depth < 0:
        return False
    try:
        children = list(folder.iterdir())
    except OSError:
        return False
    for c in children:
        if c.name.startswith("."):
            continue
        if c.is_file() and c.suffix.lower() in VIDEO_EXTS:
            return True
        if c.is_dir() and _tree_has_video(c, max_depth - 1):
            return True
    return False


# --- DB writers ----------------------------------------------------------


def _upsert_media(
    conn: sqlite3.Connection, library_id: int, kind: str,
    source_path: Path, title: str, *,
    poster_path: Path | None = None, duration_seconds: float | None = None,
) -> int:
    with conn:
        conn.execute(
            """
            INSERT INTO media (library_id, kind, source_path, title_guess,
                               poster_path, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(library_id, source_path) DO UPDATE SET
                last_seen_at     = CURRENT_TIMESTAMP,
                title_guess      = excluded.title_guess,
                poster_path      = COALESCE(excluded.poster_path, media.poster_path),
                duration_seconds = COALESCE(excluded.duration_seconds, media.duration_seconds)
            """,
            (
                library_id, kind, str(source_path), title,
                str(poster_path) if poster_path else None,
                duration_seconds,
            ),
        )
    return conn.execute(
        "SELECT id FROM media WHERE library_id = ? AND source_path = ?",
        (library_id, str(source_path)),
    ).fetchone()["id"]


def _add_movie(conn: sqlite3.Connection, library_id: int, source: Path,
               result: ScanResult) -> None:
    title = title_from_filename(source.name)
    poster = _find_poster_for(source)
    if source.is_file():
        video_path: Path | None = source
    else:
        videos, _, _ = _direct_children(source)
        video_path = videos[0] if videos else None
    duration = get_duration(video_path) if video_path else None
    media_id = _upsert_media(conn, library_id, "movie", source, title,
                             poster_path=poster, duration_seconds=duration)
    result.movies_added += 1
    # Pre-generate a frame thumbnail when no sidecar poster exists, so the
    # home page poster grid loads without on-demand ffmpeg pauses.
    if poster is None and video_path is not None:
        _thumbs.get_or_generate(video_path, _thumbs.media_thumb_path(media_id))


def _add_audiobook(conn: sqlite3.Connection, library_id: int, folder: Path,
                   result: ScanResult) -> None:
    title = title_from_filename(folder.name)
    poster = _find_poster_for(folder)
    book_id = _upsert_media(conn, library_id, "audiobook", folder, title,
                            poster_path=poster)
    result.audiobooks_added += 1
    _, audios, _ = _direct_children(folder)
    for idx, track in enumerate(sorted(audios, key=lambda p: p.name)):
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


def _add_show(conn: sqlite3.Connection, library_id: int, show_dir: Path,
              result: ScanResult) -> None:
    """A TV show folder contains season subfolders. We support:
       - Season-pattern subdirs ('Season 01', 'S2', 'Season 3')
       - Other subdirs treated as season 1, season 2, ... in iteration order
    """
    title = title_from_filename(show_dir.name)
    poster = _find_poster_for(show_dir)
    show_id = _upsert_media(conn, library_id, "tv_show", show_dir, title,
                            poster_path=poster)
    result.shows_added += 1
    first_episode_video: Path | None = None
    # Discover seasons
    season_entries: list[tuple[int, Path]] = []
    fallback_index = 1
    for sub in _iter_visible(show_dir):
        if not sub.is_dir():
            continue
        m = _SEASON_DIR.match(sub.name)
        if m:
            season_entries.append((int(m.group(1)), sub))
        else:
            season_entries.append((fallback_index, sub))
            fallback_index += 1
    for season_num, season_dir in season_entries:
        for ep_file in _iter_visible(season_dir):
            if not is_video(ep_file):
                continue
            em = _EP_MARKER.search(ep_file.name)
            episode_num = int(em.group(2)) if em else 0
            ep_title = title_from_filename(ep_file.name)
            ep_poster = _find_poster_for(ep_file)
            ep_dur = get_duration(ep_file)
            with conn:
                conn.execute(
                    """
                    INSERT INTO tv_episodes (show_id, season, episode, source_path,
                                             title_guess, poster_path, duration_seconds)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(show_id, source_path) DO UPDATE SET
                        last_seen_at = CURRENT_TIMESTAMP,
                        season       = excluded.season,
                        episode      = excluded.episode,
                        title_guess  = excluded.title_guess,
                        poster_path  = COALESCE(excluded.poster_path, tv_episodes.poster_path),
                        duration_seconds = COALESCE(excluded.duration_seconds, tv_episodes.duration_seconds)
                    """,
                    (
                        show_id, season_num, episode_num, str(ep_file), ep_title,
                        str(ep_poster) if ep_poster else None,
                        ep_dur,
                    ),
                )
            ep_id = conn.execute(
                "SELECT id FROM tv_episodes WHERE show_id = ? AND source_path = ?",
                (show_id, str(ep_file)),
            ).fetchone()["id"]
            result.episodes_added += 1
            # Pre-generate per-episode thumbnail when no sidecar exists
            if ep_poster is None:
                _thumbs.get_or_generate(ep_file, _thumbs.episode_thumb_path(ep_id))
            if first_episode_video is None:
                first_episode_video = ep_file
    # Show-level thumbnail comes from the first episode's video
    if poster is None and first_episode_video is not None:
        _thumbs.get_or_generate(first_episode_video, _thumbs.media_thumb_path(show_id))


def _walk_audiobook_grouping(
    conn: sqlite3.Connection, library_id: int, folder: Path,
    result: ScanResult,
) -> None:
    """Folder of folders containing audio: each subfolder is a book.
    Recurses to handle Author/Series/Book/chapter.mp3 layouts."""
    for sub in _iter_visible(folder):
        if not sub.is_dir():
            continue
        cls = classify_folder(sub)
        if cls == "audiobook":
            _add_audiobook(conn, library_id, sub, result)
        elif cls == "audiobook_grouping":
            _walk_audiobook_grouping(conn, library_id, sub, result)


# --- main entry point ----------------------------------------------------


def scan_library(
    conn: sqlite3.Connection,
    library_id: int,
    root: Path,
    on_progress=None,
) -> ScanResult:
    """Walk the library root, classifying each top-level entry independently.

    on_progress: optional callback(current, total, item_name). Called once
    with (0, total, "") at the start, after every top-level item processed,
    and once with (total, total, "") at the end. Used by the web UI's
    auto-scan progress indicator.
    """
    if not root.is_dir():
        raise ValueError(f"library root not found or not a directory: {root}")
    if on_progress is None:
        on_progress = lambda *a, **kw: None  # noqa: E731
    result = ScanResult()
    items = list(_iter_visible(root))
    total = len(items)
    on_progress(0, total, "")
    for i, entry in enumerate(items):
        on_progress(i, total, entry.name)
        if entry.is_file():
            if is_video(entry):
                _add_movie(conn, library_id, entry, result)
            else:
                result.skipped += 1
        elif entry.is_dir():
            classification = classify_folder(entry)
            if classification == "movie":
                _add_movie(conn, library_id, entry, result)
            elif classification == "audiobook":
                _add_audiobook(conn, library_id, entry, result)
            elif classification == "tv_show":
                _add_show(conn, library_id, entry, result)
            elif classification == "audiobook_grouping":
                _walk_audiobook_grouping(conn, library_id, entry, result)
            else:
                result.skipped += 1
        else:
            result.skipped += 1
    on_progress(total, total, "")
    return result

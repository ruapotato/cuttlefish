"""Find non-media non-sidecar files inside a library — `downloadedfrom.txt`,
NFOs, sample files, system droppings — that the admin may want to clean up.

Hidden files (starting with `.`) and the cuttlefish DB itself are skipped.
A subtitle/poster file is *not* cruft if a sibling video/audio file with the
same stem exists; otherwise it counts as cruft (an orphan sidecar).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from cuttlefish.scanner import AUDIO_EXTS, VIDEO_EXTS

POSTER_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SUBTITLE_EXTS = frozenset({".srt", ".vtt", ".ass", ".ssa"})
SIDECAR_EXTS = POSTER_EXTS | SUBTITLE_EXTS


@dataclass
class CruftEntry:
    path: Path
    size_bytes: int
    reason: str  # "orphan-sidecar" or "non-media"


def list_cruft(conn: sqlite3.Connection, library_id: int) -> list[CruftEntry]:
    lib = conn.execute(
        "SELECT root_path FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if lib is None:
        return []
    root = Path(lib["root_path"])
    if not root.is_dir():
        return []
    # A library can mix all kinds of media. Both audio and video count as
    # "media" — anything outside that and the sidecar set is cruft.
    media_exts: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS

    out: list[CruftEntry] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        # Skip hidden files / dirs anywhere in the path.
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        ext = path.suffix.lower()
        if ext in media_exts:
            continue
        if ext in SIDECAR_EXTS:
            # Sidecar to a media file with the same stem in the same dir?
            siblings = path.parent.iterdir() if path.parent.is_dir() else []
            has_media_sibling = any(
                s.is_file() and s.stem == path.stem and s.suffix.lower() in media_exts
                for s in siblings
            )
            if has_media_sibling:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            out.append(CruftEntry(path=path, size_bytes=size, reason="orphan-sidecar"))
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(CruftEntry(path=path, size_bytes=size, reason="non-media"))
    return sorted(out, key=lambda e: str(e.path))


def is_path_inside_a_library(conn: sqlite3.Connection, path: Path) -> bool:
    """Validate that `path` is inside the root of one of the registered
    libraries. Used as a guard before deletion so users can't pass arbitrary
    filesystem paths to the delete endpoint."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    rows = conn.execute("SELECT root_path FROM libraries").fetchall()
    for r in rows:
        try:
            root = Path(r["root_path"]).resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return True
    return False

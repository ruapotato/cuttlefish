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


def list_cruft(
    conn: sqlite3.Connection, library_id: int,
    include_sidecar_images: bool = False,
) -> list[CruftEntry]:
    """List files in the library that the admin might want to delete.

    Default ('conservative'): non-media files (txt/nfo/zip/etc.) and
    orphan sidecars (subtitle/poster files with no matching media file
    nearby).

    With include_sidecar_images=True ('aggressive'): also include
    sidecar IMAGE files (JPG/PNG/WEBP) that sit next to a media file —
    on the theory that cuttlefish can auto-extract a frame from the
    video so the hand-bundled poster is redundant. Sidecar SUBTITLES
    paired with media are still kept under both modes.
    """
    lib = conn.execute(
        "SELECT root_path FROM libraries WHERE id = ?", (library_id,)
    ).fetchone()
    if lib is None:
        return []
    root = Path(lib["root_path"])
    if not root.is_dir():
        return []
    media_exts: frozenset[str] = VIDEO_EXTS | AUDIO_EXTS

    out: list[CruftEntry] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        ext = path.suffix.lower()
        if ext in media_exts:
            continue
        if ext in SIDECAR_EXTS:
            siblings = path.parent.iterdir() if path.parent.is_dir() else []
            has_media_sibling = any(
                s.is_file() and s.stem == path.stem and s.suffix.lower() in media_exts
                for s in siblings
            )
            if not has_media_sibling:
                # Orphan sidecar — always cruft.
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                out.append(CruftEntry(path=path, size_bytes=size, reason="orphan-sidecar"))
                continue
            # Paired sidecar with a media sibling.
            if ext in POSTER_EXTS and include_sidecar_images:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                out.append(CruftEntry(
                    path=path, size_bytes=size,
                    reason="sidecar-image (we can extract frames)",
                ))
            # Paired subtitles or non-image sidecars with media siblings: keep.
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

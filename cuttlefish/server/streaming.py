"""HTTP range-request streaming for media files.

Browsers (and HTML5 <video>) seek by sending Range headers. We honor them so
playback can jump around without downloading the whole file.
"""
from __future__ import annotations

import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

# mimetypes' built-in db doesn't know about all media containers we accept.
_EXTRA_MIME = {
    ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
    ".webm": "video/webm",
    ".m4b": "audio/mp4",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}

_RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)$")
_CHUNK = 64 * 1024


def guess_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _EXTRA_MIME:
        return _EXTRA_MIME[suffix]
    mt, _ = mimetypes.guess_type(str(path))
    return mt or "application/octet-stream"


def stream_file(path: Path, request: Request):
    if not path.is_file():
        raise HTTPException(404, f"file not found: {path.name}")
    file_size = path.stat().st_size
    media_type = guess_media_type(path)
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(
            path, media_type=media_type, headers={"Accept-Ranges": "bytes"}
        )

    m = _RANGE_RE.match(range_header.strip().lower())
    if not m:
        raise HTTPException(416, "invalid Range header")
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else file_size - 1
    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        raise HTTPException(
            416,
            "range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    length = end - start + 1

    def iter_chunks():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
    }
    return StreamingResponse(
        iter_chunks(), status_code=206, headers=headers, media_type=media_type
    )


def video_path_for_media(source_path: Path) -> Path:
    """Resolve a media row's source_path to a playable file.

    For loose movies, source_path *is* the file. For movies in the clean
    Title/Title.mp4 layout, source_path is the directory; pick the first
    video file inside.
    """
    if source_path.is_file():
        return source_path
    if source_path.is_dir():
        for child in sorted(source_path.iterdir()):
            if child.is_file() and child.suffix.lower() in {
                ".mp4",
                ".mkv",
                ".webm",
                ".avi",
                ".mov",
                ".m4v",
                ".ts",
                ".wmv",
            }:
                return child
    raise HTTPException(404, "no playable file for media")

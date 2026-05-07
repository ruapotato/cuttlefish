"""In-process scan tracker.

Holds a thread-safe map of library_id → progress dict so the
/admin/libraries page can poll and render a live 'Scanning N of M' line
while a background scan thread runs the actual filesystem walk.

This is a per-process tracker; it doesn't survive a restart. That's fine:
when the user starts cuttlefish back up they can either re-trigger a scan
or just wait for the next periodic refresh — scans are idempotent.
"""
from __future__ import annotations

import datetime
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_lock = threading.Lock()
# library_id → {status, current, total, current_item, started_at, finished_at, error, result}
_scans: dict[int, dict] = {}


def _now() -> str:
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def get(library_id: int) -> Optional[dict]:
    with _lock:
        v = _scans.get(library_id)
        return dict(v) if v else None


def all() -> dict[int, dict]:
    with _lock:
        return {k: dict(v) for k, v in _scans.items()}


def is_running(library_id: int) -> bool:
    with _lock:
        v = _scans.get(library_id)
        return bool(v) and v.get("status") == "running"


def start(library_id: int, db_path: Optional[Path | str]) -> bool:
    """Kick off a background scan for `library_id`. Returns False if a scan
    is already in flight for that library; True if a thread was spawned."""
    with _lock:
        existing = _scans.get(library_id)
        if existing and existing.get("status") == "running":
            return False
        _scans[library_id] = {
            "library_id": library_id,
            "status": "running",
            "current": 0,
            "total": 0,
            "current_item": "",
            "started_at": _now(),
            "finished_at": None,
            "error": None,
            "result": None,
        }
    t = threading.Thread(
        target=_run, args=(library_id, db_path), daemon=True,
        name=f"scan-{library_id}",
    )
    t.start()
    return True


def _update(library_id: int, **fields) -> None:
    with _lock:
        if library_id in _scans:
            _scans[library_id].update(fields)


def _run(library_id: int, db_path: Optional[Path | str]) -> None:
    # Imported lazily so circular imports don't bite at app boot
    from cuttlefish import db, scanner

    try:
        conn = db.connect(db_path)
        row = conn.execute(
            "SELECT root_path FROM libraries WHERE id = ?", (library_id,)
        ).fetchone()
        if row is None:
            _update(
                library_id, status="failed", error="library not found",
                finished_at=_now(),
            )
            return
        root = Path(row["root_path"])

        def on_progress(current: int, total: int, name: str) -> None:
            _update(
                library_id,
                current=current,
                total=total,
                current_item=name,
            )

        result = scanner.scan_library(conn, library_id, root, on_progress=on_progress)
        _update(
            library_id,
            status="done",
            finished_at=_now(),
            result={
                "movies_added": result.movies_added,
                "shows_added": result.shows_added,
                "episodes_added": result.episodes_added,
                "audiobooks_added": result.audiobooks_added,
                "tracks_added": result.tracks_added,
                "skipped": result.skipped,
            },
        )
    except Exception as e:
        log.exception("scan for library_id=%s failed", library_id)
        _update(
            library_id, status="failed", error=str(e), finished_at=_now(),
        )


def reset_for_tests() -> None:
    """Tests use this to wipe state between cases."""
    with _lock:
        _scans.clear()

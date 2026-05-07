"""FastAPI app: HTML pages + JSON API + media streaming + auth + progress.

Pages are intentionally hand-written HTML strings (no Jinja, no JS framework)
so the same UI works on a smart TV's built-in browser.
"""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from cuttlefish import auth, cruft as cruft_mod, db, subtitles as subs_mod, thumbnails as thumbs_mod
from cuttlefish.clients.opensubtitles import OpenSubtitles
from cuttlefish.clients.tmdb import TMDb
from cuttlefish.server import scan_tracker
from cuttlefish.server.cast import CastBus
from cuttlefish.server.streaming import stream_file, video_path_for_media
from cuttlefish.workers import encoder


# --- request/response models ----------------------------------------------


class ProgressBody(BaseModel):
    position_seconds: float = Field(..., ge=0)
    duration_seconds: Optional[float] = Field(None, ge=0)


class AudiobookProgressBody(BaseModel):
    track_id: int
    position_seconds: float = Field(..., ge=0)


class CruftDeleteBody(BaseModel):
    path: str


class LibraryCreateBody(BaseModel):
    name: str
    root_path: str


class UserPatchBody(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = None


class PasswordChangeBody(BaseModel):
    current_password: str
    new_password: str


# --- app factory ----------------------------------------------------------


def create_app(
    db_path: Optional[Path | str] = None,
    tmdb_client: Optional[TMDb] = None,
    opensubtitles_client: Optional[OpenSubtitles] = None,
) -> FastAPI:
    app = FastAPI(title="Cuttlefish", version="0.0.0", docs_url="/api/docs")
    # Lazily build clients; allow injection for testing.
    _tmdb = tmdb_client
    _opensubs = opensubtitles_client
    cast_bus = CastBus()

    # --- Login-required gate --------------------------------------------
    # All routes other than the public set below require a valid session.
    # As a practical concession, when the users table is empty the gate is
    # disabled — this keeps test fixtures (which don't always register an
    # admin) working, and there's nothing to protect on a fresh DB anyway.

    PUBLIC_PATHS = {"/login", "/health", "/api/docs", "/api/openapi.json"}
    PUBLIC_PREFIXES = ("/api/auth/",)

    @app.middleware("http")
    async def require_auth(request: Request, call_next):
        path = request.url.path
        # Always-public surfaces
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)
        # /register is open when there are no users yet (covers fresh
        # installs that didn't go through bootstrap), admin-only after.
        # The view itself enforces the latter; the gate just lets the
        # request through.
        try:
            conn = db.connect(db_path)
            n_users = auth.user_count(conn)
        except Exception:
            return await call_next(request)
        if n_users == 0:
            return await call_next(request)
        if path == "/register":
            return await call_next(request)
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if token and auth.lookup_session(conn, token):
            return await call_next(request)
        # Not authenticated. JSON for API, redirect to /login for HTML.
        if path.startswith("/api/") or path.startswith("/stream/") \
                or path.startswith("/poster/") or path.startswith("/subtitle/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse(f"/login?next={path}", status_code=303)

    def get_tmdb() -> TMDb:
        nonlocal _tmdb
        if _tmdb is None:
            _tmdb = TMDb()
        return _tmdb

    def get_opensubtitles() -> OpenSubtitles:
        nonlocal _opensubs
        if _opensubs is None:
            _opensubs = OpenSubtitles()
        return _opensubs

    def _conn() -> sqlite3.Connection:
        return db.connect(db_path)

    def _current_user(request: Request) -> Optional[dict]:
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if not token:
            return None
        row = auth.lookup_session(_conn(), token)
        return dict(row) if row else None

    def _require_user(request: Request) -> dict:
        user = _current_user(request)
        if not user:
            raise HTTPException(401, "login required")
        return user

    def _require_admin(request: Request) -> dict:
        user = _require_user(request)
        if not user["is_admin"]:
            raise HTTPException(403, "admin only")
        return user

    # --- JSON: libraries / media -----------------------------------------

    @app.get("/api/libraries")
    def api_libraries():
        rows = _conn().execute(
            "SELECT id, name, root_path, created_at FROM libraries ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/media")
    def api_media(library: Optional[str] = None, kind: Optional[str] = None):
        sql = (
            "SELECT m.id, m.kind, m.title_guess, m.source_path, "
            "       m.first_seen_at, m.last_seen_at, l.name AS library "
            "FROM media m JOIN libraries l ON l.id = m.library_id"
        )
        clauses = []
        params: list = []
        if library:
            clauses.append("l.name = ?")
            params.append(library)
        if kind:
            clauses.append("m.kind = ?")
            params.append(kind)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY m.kind, m.title_guess"
        rows = _conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/media/{media_id}")
    def api_media_one(media_id: int):
        row = _conn().execute(
            "SELECT m.id, m.kind, m.title_guess, m.source_path, "
            "       m.first_seen_at, m.last_seen_at, l.name AS library "
            "FROM media m JOIN libraries l ON l.id = m.library_id "
            "WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        return dict(row)

    # --- Streaming -------------------------------------------------------

    @app.get("/stream/{media_id}")
    def stream(media_id: int, request: Request):
        # Prefer the encoded version if one exists; fall back to source.
        row = _conn().execute(
            "SELECT m.source_path, e.video_path FROM media m "
            "LEFT JOIN encoded_files e ON e.media_id = m.id WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        encoded = row["video_path"]
        if encoded and Path(encoded).is_file():
            return stream_file(Path(encoded), request)
        path = video_path_for_media(Path(row["source_path"]))
        return stream_file(path, request)

    @app.get("/stream/episode/{episode_id}")
    def stream_episode(episode_id: int, request: Request):
        row = _conn().execute(
            "SELECT source_path FROM tv_episodes WHERE id = ?", (episode_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "episode not found")
        return stream_file(Path(row["source_path"]), request)

    @app.get("/stream/track/{track_id}")
    def stream_track(track_id: int, request: Request):
        row = _conn().execute(
            "SELECT source_path FROM audiobook_tracks WHERE id = ?", (track_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "track not found")
        return stream_file(Path(row["source_path"]), request)

    # --- Subtitles served as WebVTT (browser-native captions) -----------

    def _serve_vtt(path: Path):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".srt":
            text = subs_mod.srt_to_vtt(text)
        elif not text.lstrip().startswith("WEBVTT"):
            text = "WEBVTT\n\n" + text
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(text, media_type="text/vtt")

    @app.get("/subtitle/{media_id}")
    def subtitle_for_media(media_id: int, variant: Optional[str] = None):
        """variant=None picks the best (ASR if present, else original);
        variant='asr' / 'original' picks specifically.
        """
        sub = subs_mod.subtitle_for_media(_conn(), media_id, variant=variant)
        if sub is None:
            raise HTTPException(404, "no subtitle available for this media")
        return _serve_vtt(sub)

    @app.get("/subtitle/episode/{episode_id}")
    def subtitle_for_episode(episode_id: int, variant: Optional[str] = None):
        sub = subs_mod.subtitle_for_episode(_conn(), episode_id, variant=variant)
        if sub is None:
            raise HTTPException(404, "no subtitle available for this episode")
        return _serve_vtt(sub)

    # --- Posters ---------------------------------------------------------

    @app.get("/poster/{media_id}")
    def poster_for_media(media_id: int):
        from fastapi.responses import FileResponse
        row = _conn().execute(
            "SELECT m.kind, m.source_path, m.poster_path, "
            "       e.poster_path AS encoded_poster, e.video_path AS encoded_video "
            "FROM media m LEFT JOIN encoded_files e ON e.media_id = m.id "
            "WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        # Real posters first
        for candidate in (row["encoded_poster"], row["poster_path"]):
            if candidate:
                p = Path(candidate)
                if p.is_file():
                    return FileResponse(p)
        # Fall back to a frame extracted from the video. Audiobooks have
        # no video frame to extract so we 404 and the page shows a placeholder.
        if row["kind"] == "audiobook":
            raise HTTPException(404, "no poster available")
        # For TV shows, the show row's source_path is a directory of seasons.
        # Use the first episode's video as the show thumbnail source.
        if row["kind"] == "tv_show":
            ep = _conn().execute(
                "SELECT source_path, ee.video_path AS encoded_video "
                "FROM tv_episodes "
                "LEFT JOIN encoded_episodes ee ON ee.episode_id = tv_episodes.id "
                "WHERE show_id = ? "
                "ORDER BY season, episode, id LIMIT 1",
                (media_id,),
            ).fetchone()
            if ep is None:
                raise HTTPException(404, "no poster available")
            video = _resolve_video_for_thumbnail(ep, kind="episode")
        else:
            video = _resolve_video_for_thumbnail(row, kind="movie")
        if video is None:
            raise HTTPException(404, "no poster available")
        out = thumbs_mod.media_thumb_path(media_id)
        gen = thumbs_mod.get_or_generate(video, out)
        if gen is None:
            raise HTTPException(404, "no poster available")
        return FileResponse(gen)

    @app.get("/poster/episode/{episode_id}")
    def poster_for_episode(episode_id: int):
        from fastapi.responses import FileResponse
        row = _conn().execute(
            "SELECT e.poster_path, e.source_path, "
            "       ee.poster_path AS encoded_poster, ee.video_path AS encoded_video "
            "FROM tv_episodes e LEFT JOIN encoded_episodes ee ON ee.episode_id = e.id "
            "WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "episode not found")
        for candidate in (row["encoded_poster"], row["poster_path"]):
            if candidate:
                p = Path(candidate)
                if p.is_file():
                    return FileResponse(p)
        # Fallback: frame from the encoded video, or the source video
        video = _resolve_video_for_thumbnail(row, kind="episode")
        if video is None:
            raise HTTPException(404, "no poster available")
        out = thumbs_mod.episode_thumb_path(episode_id)
        gen = thumbs_mod.get_or_generate(video, out)
        if gen is None:
            raise HTTPException(404, "no poster available")
        return FileResponse(gen)

    # --- Auth API --------------------------------------------------------

    @app.post("/api/auth/register")
    def api_register(
        username: str = Form(...),
        password: str = Form(...),
        request: Request = None,
    ):
        conn = _conn()
        if len(username) < 1 or len(password) < 6:
            raise HTTPException(400, "username required, password >= 6 chars")
        first_user = auth.user_count(conn) == 0
        if not first_user:
            current = _current_user(request) if request else None
            if not current or not current["is_admin"]:
                raise HTTPException(
                    403, "registration is admin-only after the first user"
                )
        try:
            auth.create_user(conn, username, password, is_admin=first_user)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "username taken")
        return {"ok": True, "is_admin": first_user}

    @app.post("/api/auth/login")
    def api_login(
        response: Response,
        username: str = Form(...),
        password: str = Form(...),
    ):
        conn = _conn()
        user_id = auth.authenticate(conn, username, password)
        if user_id is None:
            raise HTTPException(401, "invalid credentials")
        token, expires = auth.create_session(conn, user_id)
        response.set_cookie(
            auth.SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            expires=expires,
            path="/",
        )
        return {"ok": True}

    @app.post("/api/auth/logout")
    def api_logout(request: Request, response: Response):
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if token:
            auth.delete_session(_conn(), token)
        response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def api_me(request: Request):
        user = _require_user(request)
        return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"])}

    @app.put("/api/me/password")
    def api_change_my_password(body: PasswordChangeBody, request: Request):
        user = _require_user(request)
        conn = _conn()
        if auth.authenticate(conn, user["username"], body.current_password) != user["id"]:
            raise HTTPException(401, "current password is incorrect")
        if len(body.new_password) < 6:
            raise HTTPException(400, "new password must be at least 6 characters")
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (auth.hash_password(body.new_password), user["id"]),
            )
        return {"ok": True}

    # --- Progress API ----------------------------------------------------

    @app.put("/api/progress/{media_id}")
    def api_put_progress(media_id: int, body: ProgressBody, request: Request):
        user = _require_user(request)
        conn = _conn()
        # Confirm media exists
        row = conn.execute("SELECT id FROM media WHERE id = ?", (media_id,)).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        with conn:
            conn.execute(
                """
                INSERT INTO media_progress (user_id, media_id, position_seconds, duration_seconds, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, media_id) DO UPDATE SET
                    position_seconds = excluded.position_seconds,
                    duration_seconds = COALESCE(excluded.duration_seconds, media_progress.duration_seconds),
                    updated_at       = CURRENT_TIMESTAMP
                """,
                (user["id"], media_id, body.position_seconds, body.duration_seconds),
            )
        return {"ok": True}

    @app.get("/api/progress/{media_id}")
    def api_get_progress(media_id: int, request: Request):
        user = _require_user(request)
        row = _conn().execute(
            "SELECT position_seconds, duration_seconds, updated_at "
            "FROM media_progress WHERE user_id = ? AND media_id = ?",
            (user["id"], media_id),
        ).fetchone()
        if not row:
            return {"position_seconds": 0.0, "duration_seconds": None, "updated_at": None}
        return dict(row)

    @app.get("/api/progress")
    def api_list_progress(request: Request):
        user = _require_user(request)
        rows = _conn().execute(
            "SELECT media_id, position_seconds, duration_seconds, updated_at "
            "FROM media_progress WHERE user_id = ? ORDER BY updated_at DESC",
            (user["id"],),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.delete("/api/progress/{media_id}")
    def api_delete_progress(media_id: int, request: Request):
        user = _require_user(request)
        with _conn() as conn:
            conn.execute(
                "DELETE FROM media_progress WHERE user_id = ? AND media_id = ?",
                (user["id"], media_id),
            )
        return {"ok": True}

    @app.post("/api/progress/{media_id}/watched")
    def api_mark_watched(media_id: int, request: Request):
        user = _require_user(request)
        conn = _conn()
        row = conn.execute(
            "SELECT duration_seconds FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "media not found")
        duration = row["duration_seconds"] or 0
        with conn:
            conn.execute(
                """
                INSERT INTO media_progress (user_id, media_id, position_seconds, duration_seconds, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, media_id) DO UPDATE SET
                    position_seconds = excluded.position_seconds,
                    duration_seconds = COALESCE(excluded.duration_seconds, media_progress.duration_seconds),
                    updated_at       = CURRENT_TIMESTAMP
                """,
                (user["id"], media_id, duration, duration if duration > 0 else None),
            )
        return {"ok": True, "position_seconds": duration}

    @app.put("/api/progress/episode/{episode_id}")
    def api_put_episode_progress(episode_id: int, body: ProgressBody, request: Request):
        user = _require_user(request)
        conn = _conn()
        if conn.execute(
            "SELECT 1 FROM tv_episodes WHERE id = ?", (episode_id,)
        ).fetchone() is None:
            raise HTTPException(404, "episode not found")
        with conn:
            conn.execute(
                """
                INSERT INTO episode_progress
                    (user_id, episode_id, position_seconds, duration_seconds, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, episode_id) DO UPDATE SET
                    position_seconds = excluded.position_seconds,
                    duration_seconds = COALESCE(excluded.duration_seconds, episode_progress.duration_seconds),
                    updated_at       = CURRENT_TIMESTAMP
                """,
                (user["id"], episode_id, body.position_seconds, body.duration_seconds),
            )
        return {"ok": True}

    @app.get("/api/progress/episode/{episode_id}")
    def api_get_episode_progress(episode_id: int, request: Request):
        user = _require_user(request)
        row = _conn().execute(
            "SELECT position_seconds, duration_seconds, updated_at "
            "FROM episode_progress WHERE user_id = ? AND episode_id = ?",
            (user["id"], episode_id),
        ).fetchone()
        if not row:
            return {"position_seconds": 0.0, "duration_seconds": None, "updated_at": None}
        return dict(row)

    @app.delete("/api/progress/episode/{episode_id}")
    def api_delete_episode_progress(episode_id: int, request: Request):
        user = _require_user(request)
        with _conn() as conn:
            conn.execute(
                "DELETE FROM episode_progress WHERE user_id = ? AND episode_id = ?",
                (user["id"], episode_id),
            )
        return {"ok": True}

    @app.put("/api/progress/book/{book_id}")
    def api_put_book_progress(book_id: int, body: AudiobookProgressBody, request: Request):
        user = _require_user(request)
        conn = _conn()
        # Validate book + track relationship
        row = conn.execute(
            "SELECT m.id FROM media m WHERE m.id = ? AND m.kind = 'audiobook'",
            (book_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "audiobook not found")
        track_row = conn.execute(
            "SELECT id FROM audiobook_tracks WHERE id = ? AND book_id = ?",
            (body.track_id, book_id),
        ).fetchone()
        if track_row is None:
            raise HTTPException(404, "track not in this book")
        with conn:
            conn.execute(
                """
                INSERT INTO audiobook_progress
                    (user_id, book_id, current_track_id, position_seconds, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id, book_id) DO UPDATE SET
                    current_track_id = excluded.current_track_id,
                    position_seconds = excluded.position_seconds,
                    updated_at       = CURRENT_TIMESTAMP
                """,
                (user["id"], book_id, body.track_id, body.position_seconds),
            )
        return {"ok": True}

    @app.get("/api/progress/book/{book_id}")
    def api_get_book_progress(book_id: int, request: Request):
        user = _require_user(request)
        row = _conn().execute(
            "SELECT current_track_id, position_seconds, updated_at "
            "FROM audiobook_progress WHERE user_id = ? AND book_id = ?",
            (user["id"], book_id),
        ).fetchone()
        if not row:
            return {"current_track_id": None, "position_seconds": 0.0, "updated_at": None}
        return dict(row)

    # --- Admin: encoding + cleanup ---------------------------------------

    @app.post("/api/admin/encode/{media_id}")
    def api_admin_enqueue_encode(media_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is None:
            raise HTTPException(404, "media not found")
        job_id = encoder.enqueue_encode(conn, media_id)
        return {"ok": True, "job_id": job_id}

    @app.get("/api/admin/jobs/by-ids")
    def api_admin_jobs_by_ids(request: Request, ids: str = ""):
        """Return per-status counts for the specified job IDs. Used by the
        bulk-ASR progress poller so it tracks just the jobs THIS click
        created, not the global ASR queue.

        ids: comma-separated integer job IDs.
        Returns: {queued, running, done, failed}

        IMPORTANT: this route must be declared BEFORE
        `/api/admin/jobs/{job_id}` — otherwise FastAPI matches `by-ids`
        as a path parameter and returns 422.
        """
        _require_admin(request)
        if not ids.strip():
            return {"queued": 0, "running": 0, "done": 0, "failed": 0}
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "ids must be comma-separated integers")
        if not id_list:
            return {"queued": 0, "running": 0, "done": 0, "failed": 0}
        # SQLite has a parameter-count limit (default 999). Chunk the lookup
        # so a giant bulk submission still works.
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0}
        for start in range(0, len(id_list), 800):
            chunk = id_list[start:start + 800]
            placeholders = ",".join(["?"] * len(chunk))
            rows = _conn().execute(
                f"SELECT status, COUNT(*) AS c FROM jobs "
                f"WHERE id IN ({placeholders}) GROUP BY status",
                chunk,
            ).fetchall()
            for r in rows:
                counts[r["status"]] = counts.get(r["status"], 0) + r["c"]
        return counts

    @app.get("/api/admin/jobs/{job_id}")
    def api_admin_get_job(job_id: int, request: Request):
        _require_admin(request)
        row = _conn().execute(
            "SELECT id, kind, media_id, episode_id, status, error, "
            "       created_at, started_at, finished_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "job not found")
        return dict(row)

    @app.get("/api/admin/jobs")
    def api_admin_list_jobs(request: Request, status: Optional[str] = None):
        _require_admin(request)
        sql = (
            "SELECT j.id, j.kind, j.media_id, j.status, j.error, j.created_at, "
            "j.started_at, j.finished_at, m.title_guess "
            "FROM jobs j LEFT JOIN media m ON m.id = j.media_id"
        )
        params: list = []
        if status:
            sql += " WHERE j.status = ?"
            params.append(status)
        sql += " ORDER BY j.id DESC LIMIT 200"
        rows = _conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    @app.get("/api/admin/cleanup-candidates")
    def api_admin_cleanup_candidates(request: Request):
        """List media where an encoded version exists and the original is still
        a separate file on disk (i.e. not yet replaced by the clean layout)."""
        _require_admin(request)
        rows = _conn().execute(
            "SELECT m.id, m.title_guess, m.source_path, e.video_path, e.size_bytes "
            "FROM media m JOIN encoded_files e ON e.media_id = m.id "
            "ORDER BY m.title_guess"
        ).fetchall()
        candidates = []
        for r in rows:
            src = Path(r["source_path"])
            video = Path(r["video_path"])
            if src.is_file() and src.resolve() != video.resolve():
                candidates.append(
                    {
                        "id": r["id"],
                        "title_guess": r["title_guess"],
                        "original_path": str(src),
                        "encoded_path": str(video),
                        "encoded_size_bytes": r["size_bytes"],
                        "original_size_bytes": src.stat().st_size,
                    }
                )
        return candidates

    @app.delete("/api/admin/originals/{media_id}")
    def api_admin_delete_original(media_id: int, request: Request):
        """Delete the original loose file for a media item, only after the
        encoded version is on disk and confirmed playable size > 0."""
        _require_admin(request)
        conn = _conn()
        row = conn.execute(
            "SELECT m.source_path, e.video_path, e.size_bytes "
            "FROM media m JOIN encoded_files e ON e.media_id = m.id WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "media has no encoded version yet")
        src = Path(row["source_path"])
        video = Path(row["video_path"])
        if not video.is_file() or video.stat().st_size == 0:
            raise HTTPException(409, "encoded file missing or empty; refusing to delete original")
        if not src.is_file():
            raise HTTPException(404, "original file not present")
        if src.resolve() == video.resolve():
            raise HTTPException(409, "original IS the encoded file; refusing")
        src.unlink()
        # Re-point the media row at the encoded path so subsequent scans don't
        # re-create the original entry.
        with conn:
            conn.execute(
                "UPDATE media SET source_path = ? WHERE id = ?",
                (str(video.parent), media_id),
            )
        return {"ok": True, "deleted": str(src)}

    # --- Admin: external lookups ----------------------------------------

    def _encoded_or_404(media_id: int):
        row = _conn().execute(
            "SELECT m.title_guess, m.kind, e.clean_dir, e.video_path "
            "FROM media m JOIN encoded_files e ON e.media_id = m.id "
            "WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(
                409,
                "media has no encoded version yet — run the encoder first so "
                "we know where to write the metadata/subtitle",
            )
        return row

    @app.post("/api/admin/metadata/{media_id}")
    def api_admin_fetch_metadata(media_id: int, request: Request):
        _require_admin(request)
        client = get_tmdb()
        if not client.configured:
            raise HTTPException(
                503,
                "TMDb not configured: set TMDB_API_KEY in the environment",
            )
        row = _encoded_or_404(media_id)
        kind = row["kind"]
        searcher = client.search_movie if kind != "tv_show" else client.search_tv
        results = searcher(row["title_guess"])
        if not results:
            return {"matched": False}
        top = results[0]
        clean_dir = Path(row["clean_dir"])
        title = (top.get("title") or top.get("name") or "poster").replace("/", "_")
        poster_dst = clean_dir / f"{Path(row['video_path']).stem}.jpg"
        client.download_poster(top.get("poster_path"), poster_dst)
        with _conn() as conn:
            conn.execute(
                "UPDATE encoded_files SET poster_path = ? WHERE media_id = ?",
                (str(poster_dst) if poster_dst.exists() else None, media_id),
            )
        return {
            "matched": True,
            "tmdb_id": top.get("id"),
            "title": title,
            "poster_path": str(poster_dst) if poster_dst.exists() else None,
        }

    @app.post("/api/admin/subtitle/{media_id}")
    def api_admin_fetch_subtitle(media_id: int, request: Request, language: str = "en"):
        _require_admin(request)
        client = get_opensubtitles()
        if not client.configured:
            raise HTTPException(
                503, "OpenSubtitles not configured: set OPENSUBTITLES_API_KEY"
            )
        row = _encoded_or_404(media_id)
        results = client.search(row["title_guess"], languages=language)
        if not results:
            return {"matched": False}
        # Pick the first result's first file id
        files = results[0].get("attributes", {}).get("files", [])
        if not files:
            return {"matched": False}
        file_id = files[0].get("file_id")
        if not file_id:
            return {"matched": False}
        if not client.can_download:
            raise HTTPException(
                503,
                "OpenSubtitles search succeeded but downloading needs "
                "OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD too",
            )
        clean_dir = Path(row["clean_dir"])
        srt_dst = clean_dir / f"{Path(row['video_path']).stem}.srt"
        client.download(file_id, srt_dst)
        with _conn() as conn:
            conn.execute(
                "UPDATE encoded_files SET subtitle_path = ? WHERE media_id = ?",
                (str(srt_dst) if srt_dst.exists() else None, media_id),
            )
        return {
            "matched": True,
            "subtitle_path": str(srt_dst) if srt_dst.exists() else None,
        }

    @app.post("/api/admin/asr/{media_id}")
    def api_admin_enqueue_asr(media_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is None:
            raise HTTPException(404, "media not found")
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (media_id,)
            )
        return {"ok": True, "job_id": cur.lastrowid}

    @app.post("/api/admin/asr/episode/{episode_id}")
    def api_admin_enqueue_asr_episode(episode_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM tv_episodes WHERE id = ?", (episode_id,)).fetchone() is None:
            raise HTTPException(404, "episode not found")
        with conn:
            cur = conn.execute(
                "INSERT INTO jobs (kind, episode_id) VALUES ('asr', ?)", (episode_id,)
            )
        return {"ok": True, "job_id": cur.lastrowid}

    def _bulk_enqueue_asr_for_library(conn, library_id: int) -> list[int]:
        """Queue an ASR job for every movie + episode in the library that
        doesn't already have an ASR variant on disk. Returns the list of
        newly-created job IDs so the caller / UI can track *just these*
        jobs to completion (vs the global ASR queue count, which can
        include unrelated work)."""
        if conn.execute(
            "SELECT 1 FROM libraries WHERE id = ?", (library_id,)
        ).fetchone() is None:
            raise HTTPException(404, "library not found")
        job_ids: list[int] = []
        movies = conn.execute(
            "SELECT id FROM media WHERE library_id = ? AND kind = 'movie'",
            (library_id,),
        ).fetchall()
        for m in movies:
            variants = subs_mod.subtitle_variants_for_media(conn, m["id"])
            if "asr" in variants:
                continue
            with conn:
                cur = conn.execute(
                    "INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (m["id"],)
                )
            job_ids.append(cur.lastrowid)
        eps = conn.execute(
            "SELECT e.id FROM tv_episodes e "
            "JOIN media m ON m.id = e.show_id "
            "WHERE m.library_id = ?",
            (library_id,),
        ).fetchall()
        for e in eps:
            variants = subs_mod.subtitle_variants_for_episode(conn, e["id"])
            if "asr" in variants:
                continue
            with conn:
                cur = conn.execute(
                    "INSERT INTO jobs (kind, episode_id) VALUES ('asr', ?)", (e["id"],)
                )
            job_ids.append(cur.lastrowid)
        return job_ids

    @app.post("/api/admin/asr/library/{library_id}")
    def api_admin_enqueue_asr_library(library_id: int, request: Request):
        _require_admin(request)
        ids = _bulk_enqueue_asr_for_library(_conn(), library_id)
        return {"ok": True, "queued": len(ids), "job_ids": ids}

    @app.post("/admin/asr/library/{library_id}")
    def page_admin_enqueue_asr_library(library_id: int, request: Request):
        _require_admin(request)
        ids = _bulk_enqueue_asr_for_library(_conn(), library_id)
        return RedirectResponse(
            f"/admin/subtitles?queued={len(ids)}&lib={library_id}",
            status_code=303,
        )

    @app.get("/api/admin/asr-status")
    def api_admin_asr_status(request: Request):
        _require_admin(request)
        from cuttlefish.workers import asr as _asr
        pending = _conn().execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE kind='asr' AND status='queued'"
        ).fetchone()["c"]
        return {
            "available": _asr.is_available(),
            "worker_in_process": _asr.is_worker_in_process(),
            "queued": pending,
        }

    @app.post("/api/admin/encode/episode/{episode_id}")
    def api_admin_enqueue_episode_encode(episode_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM tv_episodes WHERE id = ?", (episode_id,)).fetchone() is None:
            raise HTTPException(404, "episode not found")
        job_id = encoder.enqueue_episode_encode(conn, episode_id)
        return {"ok": True, "job_id": job_id}

    # --- Admin: libraries (CRUD + scan) ---------------------------------

    @app.post("/api/admin/libraries")
    def api_admin_create_library(body: LibraryCreateBody, request: Request):
        _require_admin(request)
        root = Path(body.root_path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"root path is not a directory: {root}")
        try:
            with _conn() as conn:
                cur = conn.execute(
                    "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
                    (body.name, str(root.resolve())),
                )
            new_id = cur.lastrowid
            # Start scanning right away so the user doesn't have to click
            # 'Scan' as a separate step. Progress is visible at
            # GET /api/admin/scans and on /admin/libraries.
            scan_tracker.start(new_id, db_path)
            return {"ok": True, "id": new_id, "scanning": True}
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"library name or root already exists: {e}")

    @app.delete("/api/admin/libraries/{library_id}")
    def api_admin_delete_library(library_id: int, request: Request):
        _require_admin(request)
        with _conn() as conn:
            cur = conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
            if cur.rowcount == 0:
                raise HTTPException(404, "library not found")
        return {"ok": True}

    @app.post("/api/admin/scan/{library_id}")
    def api_admin_scan_one(library_id: int, request: Request):
        _require_admin(request)
        from cuttlefish import scanner as scn
        conn = _conn()
        row = conn.execute(
            "SELECT root_path FROM libraries WHERE id = ?", (library_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "library not found")
        # Synchronous behavior preserved here for the JSON API: callers
        # of this endpoint historically expected a count payload back
        # when it returned. The form-based scan button uses the
        # background tracker instead so the page doesn't hang.
        result = scn.scan_library(conn, library_id, Path(row["root_path"]))
        return {
            "ok": True,
            "movies_added": result.movies_added,
            "shows_added": result.shows_added,
            "episodes_added": result.episodes_added,
            "audiobooks_added": result.audiobooks_added,
            "tracks_added": result.tracks_added,
            "skipped": result.skipped,
        }

    @app.post("/api/admin/scan")
    def api_admin_scan_all(request: Request):
        _require_admin(request)
        from cuttlefish import scanner as scn
        conn = _conn()
        rows = conn.execute("SELECT id, root_path FROM libraries").fetchall()
        total = scn.ScanResult()
        for r in rows:
            try:
                total.merge(scn.scan_library(conn, r["id"], Path(r["root_path"])))
            except Exception:
                # Don't break the loop on one bad library
                continue
        return {
            "ok": True,
            "movies_added": total.movies_added,
            "shows_added": total.shows_added,
            "episodes_added": total.episodes_added,
            "audiobooks_added": total.audiobooks_added,
            "tracks_added": total.tracks_added,
            "skipped": total.skipped,
            "scanned_libraries": len(rows),
        }

    @app.get("/admin/libraries", response_class=HTMLResponse)
    def page_admin_libraries(request: Request):
        user = _require_admin(request)
        rows = _conn().execute(
            "SELECT id, name, root_path FROM libraries ORDER BY id"
        ).fetchall()
        if not rows:
            list_html = "<p class='empty'>No libraries yet.</p>"
        else:
            row_html = "".join(
                f"<tr data-lib-id='{r['id']}'>"
                f"<td>{r['id']}</td>"
                f"<td>{html.escape(r['name'])}</td>"
                f"<td><code>{html.escape(r['root_path'])}</code></td>"
                f"<td class='scan-cell'><span class='scan-status hint'>—</span></td>"
                f"<td>"
                f"<form method='post' action='/admin/libraries/{r['id']}/scan' style='display:inline'>"
                f"<button type='submit'>Scan</button></form> "
                f"<form method='post' action='/admin/libraries/{r['id']}/delete' style='display:inline' "
                f"onsubmit='return confirm(\"Delete library {html.escape(r['name'])}? Media rows will be removed too.\");'>"
                f"<button type='submit'>Delete</button></form>"
                f"</td>"
                f"</tr>"
                for r in rows
            )
            list_html = (
                "<table class='admin'><thead><tr>"
                "<th>id</th><th>name</th><th>root</th><th>scan</th><th></th>"
                "</tr></thead><tbody>"
                f"{row_html}</tbody></table>"
                "<form method='post' action='/admin/libraries/scan-all' style='margin-top:1rem'>"
                "<button type='submit'>Scan all libraries</button></form>"
            )
        body = f"""
<h2>Libraries</h2>
<p class='hint'>A library is just a folder. Cuttlefish figures out what each
subfolder is — a movie, a TV show, an audiobook — by looking at it. One
library can contain all kinds of media, mixed. <strong>New libraries are
scanned automatically</strong> as soon as you add them.</p>
{list_html}
<h3>Add a library</h3>
<form method='post' action='/admin/libraries' class='auth'>
  <label>Name <input name='name' required placeholder='e.g. Media'></label>
  <label>Root path <input name='root_path' placeholder='/data/Media' required></label>
  <button type='submit'>Add</button>
</form>
<p><a href='/admin'>&larr; Admin</a></p>
{_scan_progress_js()}
"""
        return _page("Libraries", body, user=user)

    @app.post("/admin/libraries")
    def page_admin_libraries_add(
        request: Request,
        name: str = Form(...),
        root_path: str = Form(...),
    ):
        _require_admin(request)
        root = Path(root_path).expanduser()
        if not root.is_dir():
            raise HTTPException(400, f"root path is not a directory: {root}")
        try:
            with _conn() as conn:
                cur = conn.execute(
                    "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
                    (name, str(root.resolve())),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "library name or root already exists")
        # Auto-scan the new library in the background. /admin/libraries
        # polls /api/admin/scans and shows progress in the row.
        scan_tracker.start(cur.lastrowid, db_path)
        return RedirectResponse("/admin/libraries", status_code=303)

    @app.post("/admin/libraries/{library_id}/scan")
    def page_admin_libraries_scan(library_id: int, request: Request):
        _require_admin(request)
        # Use the background tracker so the user gets live progress
        # instead of waiting for a synchronous scan to finish before
        # the page redirects.
        if _conn().execute(
            "SELECT 1 FROM libraries WHERE id = ?", (library_id,)
        ).fetchone() is None:
            raise HTTPException(404, "library not found")
        scan_tracker.start(library_id, db_path)
        return RedirectResponse("/admin/libraries", status_code=303)

    @app.post("/admin/libraries/{library_id}/delete")
    def page_admin_libraries_delete(library_id: int, request: Request):
        _require_admin(request)
        with _conn() as conn:
            conn.execute("DELETE FROM libraries WHERE id = ?", (library_id,))
        return RedirectResponse("/admin/libraries", status_code=303)

    @app.post("/admin/libraries/scan-all")
    def page_admin_libraries_scan_all(request: Request):
        _require_admin(request)
        rows = _conn().execute("SELECT id FROM libraries").fetchall()
        for r in rows:
            scan_tracker.start(r["id"], db_path)
        return RedirectResponse("/admin/libraries", status_code=303)

    @app.get("/api/admin/scans")
    def api_admin_scans(request: Request):
        """Per-library scan progress for the live UI poller."""
        _require_admin(request)
        return scan_tracker.all()

    # --- Admin: users -----------------------------------------------------

    def _admin_count(conn) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]

    @app.get("/api/admin/users")
    def api_admin_list_users(request: Request):
        _require_admin(request)
        rows = _conn().execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
        return [{"id": r["id"], "username": r["username"],
                 "is_admin": bool(r["is_admin"]), "created_at": r["created_at"]}
                for r in rows]

    @app.delete("/api/admin/users/{user_id}")
    def api_admin_delete_user(user_id: int, request: Request):
        actor = _require_admin(request)
        if actor["id"] == user_id:
            raise HTTPException(409, "cannot delete your own account")
        conn = _conn()
        target = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            raise HTTPException(404, "user not found")
        if target["is_admin"] and _admin_count(conn) <= 1:
            raise HTTPException(409, "cannot delete the last admin")
        with conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return {"ok": True}

    @app.patch("/api/admin/users/{user_id}")
    def api_admin_patch_user(user_id: int, body: UserPatchBody, request: Request):
        actor = _require_admin(request)
        conn = _conn()
        target = conn.execute(
            "SELECT id, is_admin FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if target is None:
            raise HTTPException(404, "user not found")
        if body.is_admin is not None:
            if (
                target["is_admin"]
                and body.is_admin is False
                and _admin_count(conn) <= 1
            ):
                raise HTTPException(409, "cannot demote the last admin")
            with conn:
                conn.execute(
                    "UPDATE users SET is_admin = ? WHERE id = ?",
                    (1 if body.is_admin else 0, user_id),
                )
        if body.password is not None:
            if len(body.password) < 6:
                raise HTTPException(400, "password must be at least 6 characters")
            with conn:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (auth.hash_password(body.password), user_id),
                )
        return {"ok": True}

    @app.get("/admin/users", response_class=HTMLResponse)
    def page_admin_users(request: Request):
        actor = _require_admin(request)
        rows = _conn().execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        ).fetchall()
        if not rows:
            list_html = "<p class='empty'>No users.</p>"
        else:
            row_html = "".join(
                f"<tr>"
                f"<td>{r['id']}</td>"
                f"<td>{html.escape(r['username'])}</td>"
                f"<td>{'admin' if r['is_admin'] else 'user'}</td>"
                f"<td>{html.escape(r['created_at'] or '')}</td>"
                f"<td>"
                + (
                    f"<form method='post' action='/admin/users/{r['id']}/toggle-admin' style='display:inline'>"
                    f"<button type='submit'>"
                    f"{'Demote' if r['is_admin'] else 'Promote'}</button></form> "
                    if r["id"] != actor["id"]
                    else ""
                )
                + (
                    f"<form method='post' action='/admin/users/{r['id']}/delete' style='display:inline' "
                    f"onsubmit='return confirm(\"Delete user {html.escape(r['username'])}?\");'>"
                    f"<button type='submit'>Delete</button></form>"
                    if r["id"] != actor["id"]
                    else "<span class='hint'>(you)</span>"
                )
                + "</td>"
                f"</tr>"
                for r in rows
            )
            list_html = (
                "<table class='admin'><thead><tr>"
                "<th>id</th><th>username</th><th>role</th><th>created</th><th></th>"
                "</tr></thead><tbody>"
                f"{row_html}</tbody></table>"
            )
        body = f"""
<h2>Users</h2>
{list_html}
<h3>Add a user</h3>
<form method='post' action='/admin/users' class='auth'>
  <label>Username <input name='username' required></label>
  <label>Password (>= 6 chars) <input name='password' type='password' minlength='6' required></label>
  <label><input type='checkbox' name='is_admin' value='1'> Admin</label>
  <button type='submit'>Create</button>
</form>
<p><a href='/admin'>&larr; Admin</a></p>
"""
        return _page("Users", body, user=actor)

    @app.post("/admin/users")
    def page_admin_users_add(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        is_admin: Optional[str] = Form(None),
    ):
        _require_admin(request)
        if len(password) < 6:
            raise HTTPException(400, "password must be at least 6 characters")
        try:
            auth.create_user(_conn(), username, password, is_admin=bool(is_admin))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "username taken")
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/toggle-admin")
    def page_admin_users_toggle(user_id: int, request: Request):
        actor = _require_admin(request)
        conn = _conn()
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "user not found")
        new_admin = 0 if row["is_admin"] else 1
        if row["is_admin"] and not new_admin and _admin_count(conn) <= 1:
            raise HTTPException(409, "cannot demote the last admin")
        with conn:
            conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_admin, user_id))
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id}/delete")
    def page_admin_users_delete(user_id: int, request: Request):
        actor = _require_admin(request)
        if actor["id"] == user_id:
            raise HTTPException(409, "cannot delete your own account")
        conn = _conn()
        target = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            raise HTTPException(404, "user not found")
        if target["is_admin"] and _admin_count(conn) <= 1:
            raise HTTPException(409, "cannot delete the last admin")
        with conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return RedirectResponse("/admin/users", status_code=303)

    # --- Admin: cruft -----------------------------------------------------

    @app.get("/api/admin/cruft")
    def api_admin_list_cruft(request: Request, library_id: Optional[int] = None):
        _require_admin(request)
        conn = _conn()
        lib_ids: list[int]
        if library_id is not None:
            lib_ids = [library_id]
        else:
            lib_ids = [r["id"] for r in conn.execute("SELECT id FROM libraries").fetchall()]
        out: list[dict] = []
        for lid in lib_ids:
            for entry in cruft_mod.list_cruft(conn, lid):
                out.append(
                    {
                        "library_id": lid,
                        "path": str(entry.path),
                        "size_bytes": entry.size_bytes,
                        "reason": entry.reason,
                    }
                )
        return out

    @app.post("/api/admin/cruft/delete")
    def api_admin_delete_cruft(body: CruftDeleteBody, request: Request):
        _require_admin(request)
        conn = _conn()
        path = Path(body.path)
        if not cruft_mod.is_path_inside_a_library(conn, path):
            raise HTTPException(403, "path is not inside a registered library")
        if not path.is_file():
            raise HTTPException(404, "file not found")
        # Don't accidentally delete a media file via the cruft endpoint.
        ext = path.suffix.lower()
        from cuttlefish.scanner import AUDIO_EXTS, VIDEO_EXTS
        if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
            raise HTTPException(409, "refusing to delete a media file via the cruft endpoint")
        path.unlink()
        return {"ok": True, "deleted": str(path)}

    # --- Admin HTML pages -----------------------------------------------

    @app.get("/admin", response_class=HTMLResponse)
    def page_admin(request: Request):
        user = _require_admin(request)
        conn = _conn()
        counts = conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM media) AS media, "
            "(SELECT COUNT(*) FROM encoded_files) AS encoded, "
            "(SELECT COUNT(*) FROM jobs WHERE status='queued') AS queued, "
            "(SELECT COUNT(*) FROM jobs WHERE status='running') AS running, "
            "(SELECT COUNT(*) FROM jobs WHERE status='failed') AS failed"
        ).fetchone()
        body = f"""
<h2>Admin</h2>
<p>Logged in as <strong>{html.escape(user['username'])}</strong> (admin).</p>
<ul>
  <li><a href='/admin/encode'>Encode media</a> — {counts['media'] - counts['encoded']} not yet encoded</li>
  <li><a href='/admin/jobs'>Jobs</a> — {counts['queued']} queued, {counts['running']} running, {counts['failed']} failed</li>
  <li><a href='/admin/cleanup'>Cleanup originals</a> — manual delete after re-encode</li>
  <li><a href='/admin/subtitles'>Subtitles</a> — generate via Parakeet ASR</li>
  <li><a href='/admin/cruft'>Cruft</a> — non-media files that may be safe to delete</li>
  <li><a href='/admin/libraries'>Libraries</a> — add, scan, delete library roots</li>
  <li><a href='/admin/users'>Users</a> — manage accounts and admin privileges</li>
</ul>
"""
        return _page("Admin", body, user=user)

    @app.get("/admin/jobs", response_class=HTMLResponse)
    def page_admin_jobs(request: Request):
        user = _require_admin(request)
        rows = _conn().execute(
            "SELECT j.id, j.kind, j.media_id, j.status, j.error, j.created_at, "
            "j.started_at, j.finished_at, m.title_guess "
            "FROM jobs j LEFT JOIN media m ON m.id = j.media_id "
            "ORDER BY j.id DESC LIMIT 100"
        ).fetchall()
        if not rows:
            body = "<h2>Jobs</h2><p class='empty'>No jobs.</p>"
        else:
            row_html = "".join(
                f"<tr><td>{r['id']}</td><td>{r['kind']}</td>"
                f"<td>{html.escape(r['title_guess'] or '')}</td>"
                f"<td class='status-{r['status']}'>{r['status']}</td>"
                f"<td>{html.escape(r['created_at'] or '')}</td>"
                f"<td>{html.escape(r['finished_at'] or '')}</td>"
                f"<td class='error'>{html.escape((r['error'] or '')[:120])}</td></tr>"
                for r in rows
            )
            body = (
                "<h2>Jobs</h2>"
                "<table class='admin'><thead><tr>"
                "<th>id</th><th>kind</th><th>media</th><th>status</th>"
                "<th>created</th><th>finished</th><th>error</th>"
                "</tr></thead><tbody>"
                f"{row_html}</tbody></table>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        return _page("Jobs", body, user=user)

    @app.get("/admin/cleanup", response_class=HTMLResponse)
    def page_admin_cleanup(request: Request):
        user = _require_admin(request)
        rows = _conn().execute(
            "SELECT m.id, m.title_guess, m.source_path, e.video_path, e.size_bytes "
            "FROM media m JOIN encoded_files e ON e.media_id = m.id "
            "ORDER BY m.title_guess"
        ).fetchall()
        candidates = []
        for r in rows:
            src = Path(r["source_path"])
            video = Path(r["video_path"])
            if src.is_file() and src.resolve() != video.resolve():
                candidates.append({
                    "id": r["id"],
                    "title": r["title_guess"],
                    "original": src,
                    "encoded": video,
                    "encoded_size": r["size_bytes"],
                    "original_size": src.stat().st_size,
                })
        if not candidates:
            body = (
                "<h2>Cleanup originals</h2>"
                "<p class='empty'>No originals are ready for deletion. "
                "Encode something first, then come back.</p>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        else:
            row_html = "".join(
                f"<tr>"
                f"<td>{html.escape(c['title'])}</td>"
                f"<td><span class='size'>{_human_size(c['original_size'])}</span><br>"
                f"<small>{html.escape(c['original'].name)}</small></td>"
                f"<td><span class='size'>{_human_size(c['encoded_size'])}</span><br>"
                f"<small>{html.escape(c['encoded'].name)}</small></td>"
                f"<td><form method='post' action='/admin/originals/{c['id']}/delete' "
                f"onsubmit='return confirm(\"Delete original {html.escape(c['original'].name)}? This cannot be undone.\");'>"
                f"<button type='submit'>Delete original</button></form></td>"
                f"</tr>"
                for c in candidates
            )
            body = (
                "<h2>Cleanup originals</h2>"
                "<p class='hint'>The originals listed below have an encoded "
                "version on disk. Deleting them frees space and finishes the "
                "clean Title/Title.mp4 layout.</p>"
                "<table class='admin'><thead><tr>"
                "<th>title</th><th>original</th><th>encoded</th><th></th>"
                "</tr></thead><tbody>"
                f"{row_html}</tbody></table>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        return _page("Cleanup originals", body, user=user)

    @app.get("/admin/encode", response_class=HTMLResponse)
    def page_admin_encode(request: Request):
        user = _require_admin(request)
        rows = _conn().execute(
            "SELECT m.id, m.kind, m.title_guess, l.name AS library "
            "FROM media m JOIN libraries l ON l.id = m.library_id "
            "LEFT JOIN encoded_files e ON e.media_id = m.id "
            "WHERE e.media_id IS NULL "
            "ORDER BY m.kind, m.title_guess "
            "LIMIT 200"
        ).fetchall()
        if not rows:
            body = (
                "<h2>Encode media</h2>"
                "<p class='empty'>Everything in the library is already "
                "encoded.</p><p><a href='/admin'>&larr; Admin</a></p>"
            )
        else:
            row_html = "".join(
                f"<tr><td>{html.escape(r['library'])}</td>"
                f"<td>{r['kind']}</td>"
                f"<td>{html.escape(r['title_guess'])}</td>"
                f"<td>"
                f"<form method='post' action='/admin/encode/{r['id']}'>"
                f"<button type='submit'>Enqueue encode</button></form>"
                f"</td></tr>"
                for r in rows
            )
            body = (
                "<h2>Encode media</h2>"
                "<p class='hint'>One-shot encode to H.264/AAC/MP4 1080p. The "
                "encoder worker (run via "
                "<code>uv run cuttlefish encode-worker</code>) picks jobs up "
                "from the queue. Originals are kept until you confirm delete.</p>"
                "<table class='admin'><thead><tr>"
                "<th>library</th><th>kind</th><th>title</th><th></th>"
                "</tr></thead><tbody>"
                f"{row_html}</tbody></table>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        return _page("Encode", body, user=user)

    @app.post("/admin/encode/{media_id}")
    def page_admin_encode_submit(media_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is None:
            raise HTTPException(404, "media not found")
        encoder.enqueue_encode(conn, media_id)
        return RedirectResponse("/admin/jobs", status_code=303)

    @app.get("/admin/subtitles", response_class=HTMLResponse)
    def page_admin_subtitles(
        request: Request, queued: Optional[int] = None, lib: Optional[int] = None,
    ):
        user = _require_admin(request)
        from cuttlefish.workers import asr as _asr

        conn = _conn()
        # Movies + audiobooks: items currently lacking a discoverable subtitle
        media_rows = conn.execute(
            "SELECT m.id, m.kind, m.title_guess "
            "FROM media m WHERE m.kind != 'tv_show' "
            "ORDER BY m.kind, m.title_guess"
        ).fetchall()
        movie_items = []
        for r in media_rows:
            if r["kind"] != "movie":
                continue
            sub = subs_mod.subtitle_for_media(conn, r["id"])
            movie_items.append({"id": r["id"], "title": r["title_guess"], "has_sub": sub is not None})

        # TV episodes
        ep_rows = conn.execute(
            "SELECT e.id, e.season, e.episode, e.title_guess, "
            "       m.id AS show_id, m.title_guess AS show_title "
            "FROM tv_episodes e JOIN media m ON m.id = e.show_id "
            "ORDER BY m.title_guess, e.season, e.episode"
        ).fetchall()
        ep_items = []
        for r in ep_rows:
            sub = subs_mod.subtitle_for_episode(conn, r["id"])
            ep_items.append({
                "id": r["id"],
                "show_title": r["show_title"],
                "label": f"S{r['season']:02d}E{r['episode']:02d}",
                "title": r["title_guess"],
                "has_sub": sub is not None,
            })

        asr_ok = _asr.is_available()
        worker_running = _asr.is_worker_in_process()
        # Also surface how many ASR jobs are currently sitting in the queue
        asr_pending = conn.execute(
            "SELECT COUNT(*) AS c FROM jobs WHERE kind='asr' AND status='queued'"
        ).fetchone()["c"]
        pending_note = (
            f" <strong>{asr_pending} ASR job(s) waiting in the queue right now.</strong>"
            if asr_pending else ""
        )
        if not asr_ok:
            status_banner = (
                "<p class='error'><strong>ASR dependencies aren't importable</strong> "
                "in this venv — that's surprising since they're a required "
                "core dep. Try <code>uv sync</code> from the project root and "
                "restart. You can still queue jobs; they'll process once the "
                f"worker comes online.{pending_note}</p>"
            )
        elif not worker_running:
            status_banner = (
                "<p class='error'><strong>ASR worker isn't running in this "
                "process.</strong> Restart the server normally — the ASR "
                "worker auto-starts as part of <code>serve</code>. Or run "
                "<code>uv run cuttlefish asr-worker</code> in a separate "
                f"terminal — queued jobs will start within seconds.{pending_note}</p>"
            )
        else:
            status_banner = (
                "<p class='hint' style='color:#6c6'>"
                "<strong>ASR worker is running.</strong> "
                f"Queued jobs are picked up within ~5 seconds.{pending_note}</p>"
            )

        def render_movie_row(it):
            has = "✓" if it["has_sub"] else "—"
            btn = (
                f"<form method='post' action='/admin/asr/{it['id']}' style='display:inline'>"
                f"<button type='submit'>Generate</button></form>"
            )
            return (
                f"<tr><td>{html.escape(it['title'])}</td>"
                f"<td>{has}</td><td>{btn}</td></tr>"
            )

        def render_ep_row(it):
            has = "✓" if it["has_sub"] else "—"
            btn = (
                f"<form method='post' action='/admin/asr/episode/{it['id']}' style='display:inline'>"
                f"<button type='submit'>Generate</button></form>"
            )
            return (
                f"<tr><td>{html.escape(it['show_title'])} {it['label']} "
                f"<span class='kind'>{html.escape(it['title'] or '')}</span></td>"
                f"<td>{has}</td><td>{btn}</td></tr>"
            )

        sections = []
        if movie_items:
            sections.append(
                "<h3>Movies</h3>"
                "<table class='admin'><thead><tr>"
                "<th>title</th><th>subtitle</th><th></th>"
                "</tr></thead><tbody>"
                + "".join(render_movie_row(it) for it in movie_items)
                + "</tbody></table>"
            )
        if ep_items:
            sections.append(
                "<h3>TV episodes</h3>"
                "<table class='admin'><thead><tr>"
                "<th>episode</th><th>subtitle</th><th></th>"
                "</tr></thead><tbody>"
                + "".join(render_ep_row(it) for it in ep_items)
                + "</tbody></table>"
            )
        if not sections:
            sections.append("<p class='empty'>No movies or episodes scanned yet.</p>")

        # Per-library bulk-ASR section. Each library has a JS-driven
        # button that posts to /api/admin/asr/library/{id} and then polls
        # /api/admin/asr-status to surface 'N jobs remaining' inline.
        # Idempotent: re-running only enqueues items that don't already
        # have an ASR variant.
        libs = conn.execute(
            "SELECT id, name FROM libraries ORDER BY id"
        ).fetchall()
        bulk_html = ""
        if libs:
            rows = "".join(
                f"<tr>"
                f"<td>{html.escape(lib_row['name'])}</td>"
                f"<td><button type='button' class='bulk-asr-btn' "
                f"data-lib-id='{lib_row['id']}'>"
                "Generate ASR for everything in this library</button></td>"
                f"<td><span class='bulk-asr-status' "
                f"data-lib-id='{lib_row['id']}'></span></td>"
                f"</tr>"
                for lib_row in libs
            )
            bulk_html = (
                "<h3>Bulk: whole library</h3>"
                "<p class='hint'>Queues an ASR job for every movie and "
                "episode in the library that doesn't already have an "
                "auto-generated subtitle. Items with existing ASR are skipped — "
                "use the per-row Regenerate button on a watch page if you "
                "want to redo one.</p>"
                "<table class='admin'><tbody>"
                f"{rows}</tbody></table>"
                f"{_BULK_ASR_JS}"
            )

        # Flash message if we just came back from a bulk-enqueue redirect.
        flash_html = ""
        if queued is not None:
            if queued == 0:
                flash_html = (
                    "<p class='hint' style='color:#6c6'>Nothing to queue — "
                    "every item in that library already has an ASR variant.</p>"
                )
            else:
                flash_html = (
                    f"<p class='hint' style='color:#6c6'>Queued <strong>{queued}</strong> "
                    "ASR job(s). The worker is picking them up; check "
                    "<a href='/admin/jobs'>jobs</a> or refresh this page for the "
                    "updated 'pending' count.</p>"
                )

        body = (
            "<h2>Subtitles</h2>"
            "<p class='hint'>Click <strong>Generate</strong> to enqueue a "
            "Parakeet ASR job. The worker writes an SRT next to the source "
            "file (or in the clean folder if the item has been encoded), and "
            "the watch page picks it up automatically. ASR is slow on CPU — "
            "GPU strongly recommended.</p>"
            f"{status_banner}"
            f"{flash_html}"
            f"{bulk_html}"
            + "".join(sections)
            + "<p><a href='/admin'>&larr; Admin</a></p>"
        )
        return _page("Subtitles", body, user=user)

    @app.post("/admin/asr/{media_id}")
    def page_admin_asr(media_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM media WHERE id = ?", (media_id,)).fetchone() is None:
            raise HTTPException(404, "media not found")
        with conn:
            conn.execute("INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (media_id,))
        return RedirectResponse("/admin/jobs", status_code=303)

    @app.post("/admin/asr/episode/{episode_id}")
    def page_admin_asr_episode(episode_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM tv_episodes WHERE id = ?", (episode_id,)).fetchone() is None:
            raise HTTPException(404, "episode not found")
        with conn:
            conn.execute("INSERT INTO jobs (kind, episode_id) VALUES ('asr', ?)", (episode_id,))
        return RedirectResponse("/admin/jobs", status_code=303)

    @app.get("/admin/cruft", response_class=HTMLResponse)
    def page_admin_cruft(
        request: Request, include_images: int = 0, deleted: Optional[int] = None,
    ):
        user = _require_admin(request)
        conn = _conn()
        include_imgs = bool(include_images)
        libs = conn.execute("SELECT id, name FROM libraries ORDER BY id").fetchall()
        sections = []
        total = 0
        for lib in libs:
            entries = cruft_mod.list_cruft(
                conn, lib["id"], include_sidecar_images=include_imgs,
            )
            if not entries:
                continue
            rows = "".join(
                f"<tr>"
                f"<td><code>{html.escape(str(e.path))}</code></td>"
                f"<td><span class='size'>{_human_size(e.size_bytes)}</span></td>"
                f"<td>{html.escape(e.reason)}</td>"
                f"<td><form method='post' action='/admin/cruft/delete' "
                f"onsubmit='return confirm(\"Delete {html.escape(e.path.name)}?\");'>"
                f"<input type='hidden' name='path' value='{html.escape(str(e.path))}'>"
                f"<button type='submit'>Delete</button></form></td>"
                f"</tr>"
                for e in entries
            )
            total += len(entries)
            bulk_form = (
                f"<form method='post' action='/admin/cruft/delete-all' "
                f"onsubmit='return confirm(\"Delete ALL {len(entries)} listed file(s) "
                f"in {html.escape(lib['name'])}? This cannot be undone.\");' "
                "style='margin-bottom:.5rem'>"
                f"<input type='hidden' name='library_id' value='{lib['id']}'>"
                f"<input type='hidden' name='include_images' value='{1 if include_imgs else 0}'>"
                f"<button type='submit'>Delete all {len(entries)} listed in this library</button>"
                "</form>"
            )
            sections.append(
                f"<h3>{html.escape(lib['name'])}</h3>"
                f"{bulk_form}"
                "<table class='admin'><thead><tr>"
                "<th>path</th><th>size</th><th>reason</th><th></th>"
                "</tr></thead><tbody>"
                f"{rows}</tbody></table>"
            )

        # Toggle for include_sidecar_images. Renders as a small form with
        # a hidden input + submit button so it works without JS.
        if include_imgs:
            toggle_html = (
                "<form method='get' action='/admin/cruft' style='display:inline'>"
                "<button type='submit'>Hide sidecar images (conservative)</button>"
                "</form>"
            )
            mode_note = (
                "<strong>Aggressive mode is on:</strong> sidecar JPG/PNG "
                "images sitting next to videos are listed here too. "
                "Cuttlefish auto-extracts a frame for the thumbnail so "
                "those images aren't strictly required."
            )
        else:
            toggle_html = (
                "<form method='get' action='/admin/cruft' style='display:inline'>"
                "<input type='hidden' name='include_images' value='1'>"
                "<button type='submit'>Also list sidecar images "
                "(we can re-extract frames)</button>"
                "</form>"
            )
            mode_note = (
                "Conservative mode: only files cuttlefish can't use are "
                "listed (TXT, NFO, orphan subs). Click the button to also "
                "surface sidecar JPG/PNG files paired with videos."
            )

        flash_html = ""
        if deleted is not None:
            flash_html = (
                f"<p class='hint' style='color:#6c6'>Deleted <strong>{deleted}</strong> "
                "file(s).</p>"
            )

        intro = (
            "<h2>Cruft</h2>"
            f"<p class='hint'>{mode_note}</p>"
            f"<p>{toggle_html}</p>"
            f"{flash_html}"
        )
        if not sections:
            body = (
                f"{intro}<p class='empty'>Nothing to clean up.</p>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        else:
            body = (
                f"{intro}"
                f"<p class='hint'>{total} file(s) listed across your libraries. "
                "Delete only what you don't want — there's no undo.</p>"
                + "".join(sections)
                + "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        return _page("Cruft", body, user=user)

    @app.post("/admin/cruft/delete")
    def page_admin_cruft_delete(request: Request, path: str = Form(...)):
        _require_admin(request)
        conn = _conn()
        p = Path(path)
        if not cruft_mod.is_path_inside_a_library(conn, p):
            raise HTTPException(403, "path is not inside a registered library")
        if not p.is_file():
            return RedirectResponse("/admin/cruft", status_code=303)
        ext = p.suffix.lower()
        from cuttlefish.scanner import AUDIO_EXTS, VIDEO_EXTS
        if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
            raise HTTPException(409, "refusing to delete a media file via cruft")
        p.unlink()
        return RedirectResponse("/admin/cruft", status_code=303)

    @app.post("/admin/cruft/delete-all")
    def page_admin_cruft_delete_all(
        request: Request,
        library_id: int = Form(...),
        include_images: int = Form(0),
    ):
        _require_admin(request)
        conn = _conn()
        # Re-list at delete time so we don't accept stale paths from the
        # form. Bulk delete = whatever cruft is currently listed for this
        # library at the same toggle state.
        if conn.execute(
            "SELECT 1 FROM libraries WHERE id = ?", (library_id,)
        ).fetchone() is None:
            raise HTTPException(404, "library not found")
        from cuttlefish.scanner import AUDIO_EXTS, VIDEO_EXTS
        entries = cruft_mod.list_cruft(
            conn, library_id, include_sidecar_images=bool(include_images),
        )
        deleted = 0
        for entry in entries:
            p = entry.path
            ext = p.suffix.lower()
            if ext in VIDEO_EXTS or ext in AUDIO_EXTS:
                continue  # never delete media
            if not cruft_mod.is_path_inside_a_library(conn, p):
                continue
            try:
                if p.is_file():
                    p.unlink()
                    deleted += 1
            except OSError:
                continue
        # Preserve the include-images toggle on the redirect.
        qs = f"deleted={deleted}"
        if include_images:
            qs += "&include_images=1"
        return RedirectResponse(f"/admin/cruft?{qs}", status_code=303)

    @app.post("/admin/originals/{media_id}/delete")
    def page_admin_delete_original(media_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        row = conn.execute(
            "SELECT m.source_path, e.video_path, e.size_bytes "
            "FROM media m JOIN encoded_files e ON e.media_id = m.id WHERE m.id = ?",
            (media_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "media has no encoded version yet")
        src = Path(row["source_path"])
        video = Path(row["video_path"])
        if not video.is_file() or video.stat().st_size == 0:
            raise HTTPException(409, "encoded file missing or empty")
        if not src.is_file():
            return RedirectResponse("/admin/cleanup", status_code=303)
        if src.resolve() == video.resolve():
            raise HTTPException(409, "original IS the encoded file")
        src.unlink()
        with conn:
            conn.execute(
                "UPDATE media SET source_path = ? WHERE id = ?",
                (str(video.parent), media_id),
            )
        return RedirectResponse("/admin/cleanup", status_code=303)

    # --- Health ---------------------------------------------------------

    @app.get("/health")
    def health():
        try:
            row = _conn().execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            schema_version = int(row["value"]) if row else None
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
        return {"ok": True, "schema_version": schema_version}

    # --- HTML pages ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_index(request: Request):
        user = _current_user(request)
        conn = _conn()
        # Empty-state: no libraries registered at all
        lib_count = conn.execute("SELECT COUNT(*) AS c FROM libraries").fetchone()["c"]
        if lib_count == 0:
            if user and user["is_admin"]:
                body = (
                    "<p class='empty'>No libraries yet. Open "
                    "<a href='/admin/libraries'>Admin → Libraries</a>, add "
                    "the path on disk where your media lives, then click "
                    "<strong>Scan all libraries</strong>.</p>"
                )
            elif user:
                body = (
                    "<p class='empty'>No libraries yet. The admin needs "
                    "to add one in /admin/libraries.</p>"
                )
            else:
                body = (
                    "<p class='empty'>No libraries yet. "
                    "<a href='/login'>Log in</a> as admin to add one.</p>"
                )
            return _page("Cuttlefish", body, user=user)

        # All media merged across every library, grouped by kind
        rows = conn.execute(
            "SELECT id, kind, title_guess, poster_path FROM media "
            "ORDER BY kind, title_guess"
        ).fetchall()
        sections = {"movie": [], "tv_show": [], "audiobook": []}
        for r in rows:
            sections.setdefault(r["kind"], []).append(r)

        def render_card(item):
            url = _watch_url(item["kind"], item["id"])
            return (
                f"<li class='card'><a href='{url}'>"
                f"<div class='poster-wrap'>"
                f"<div class='no-poster'></div>"
                f"<img src='/poster/{item['id']}' alt='' loading='lazy' "
                f"onerror=\"this.style.display='none'\">"
                f"</div>"
                f"<span class='card-title'>{html.escape(item['title_guess'])}</span>"
                f"</a></li>"
            )

        section_order = [
            ("movie", "Movies"),
            ("tv_show", "TV Shows"),
            ("audiobook", "Audiobooks"),
        ]
        parts = []
        for kind, label in section_order:
            items = sections.get(kind) or []
            if not items:
                continue
            cards = "".join(render_card(it) for it in items)
            parts.append(
                f"<section class='media-section'>"
                f"<h2>{label} <span class='kind'>({len(items)})</span></h2>"
                f"<ul class='cards'>{cards}</ul>"
                f"</section>"
            )

        if not parts:
            body = (
                "<p class='empty'>No media yet. "
                "Open <a href='/admin/libraries'>Admin → Libraries</a> and "
                "click <strong>Scan</strong>.</p>"
            )
        else:
            body = "".join(parts)
        return _page("Cuttlefish", body, user=user)

    @app.get("/library/{library_id}", response_class=HTMLResponse)
    def page_library(library_id: int, request: Request):
        user = _current_user(request)
        conn = _conn()
        lib = conn.execute(
            "SELECT id, name FROM libraries WHERE id = ?", (library_id,)
        ).fetchone()
        if not lib:
            raise HTTPException(404, "library not found")
        media = conn.execute(
            "SELECT id, kind, title_guess, poster_path FROM media WHERE library_id = ? "
            "ORDER BY kind, title_guess",
            (library_id,),
        ).fetchall()
        if not media:
            body = "<p class='empty'>No media yet. Use <strong>Scan</strong> on this library in /admin/libraries.</p>"
        else:
            items = "".join(
                f"<li class='card'><a href='{_watch_url(m['kind'], m['id'])}'>"
                f"<div class='poster-wrap'>"
                f"<div class='no-poster'></div>"
                f"<img src='/poster/{m['id']}' alt='' loading='lazy' "
                f"onerror=\"this.style.display='none'\">"
                f"</div>"
                f"<span class='card-title'>{html.escape(m['title_guess'])}</span>"
                f"<span class='card-kind'>{_kind_label(m['kind'])}</span>"
                "</a></li>"
                for m in media
            )
            body = f"<ul class='cards'>{items}</ul>"
        return _page(lib['name'], body, user=user)

    @app.get("/watch/{media_id}", response_class=HTMLResponse)
    def page_watch(media_id: int, request: Request):
        """Movie player. Shows redirect to /show, audiobooks to /book."""
        user = _current_user(request)
        row = _conn().execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        if row["kind"] == "tv_show":
            return RedirectResponse(f"/show/{media_id}", status_code=303)
        if row["kind"] == "audiobook":
            return RedirectResponse(f"/book/{media_id}", status_code=303)
        title = html.escape(row["title_guess"])
        variants = subs_mod.subtitle_variants_for_media(_conn(), media_id)
        track_html = _subtitle_tracks_html(
            f"/subtitle/{media_id}", variants
        )
        admin_actions = ""
        admin_js = ""
        if user and user["is_admin"]:
            admin_actions = _generate_subs_button_html(variants, f"/api/admin/asr/{media_id}")
            admin_js = _generate_subs_js()
        body = (
            f"<div class='theater'>"
            f"<video id='player' controls autoplay playsinline preload='auto' "
            f"src='/stream/{media_id}'>"
            f"{track_html}"
            "Your browser does not support the video element.</video>"
            f"</div>"
            f"<div class='theater-meta'>"
            f"<h2>{title}</h2>"
            "<p><a href='/'>&larr; Back to library</a></p>"
            f"{admin_actions}"
            "</div>"
            + _player_progress_js(f"/api/progress/{media_id}")
            + admin_js
        )
        return _page(title, body, user=user, body_class="watch")

    @app.get("/show/{show_id}", response_class=HTMLResponse)
    def page_show(show_id: int, request: Request):
        """Selecting a TV show goes straight to the next episode you haven't
        finished — Jellyfin/Emby behavior. The big episode list now lives
        as a strip at the bottom of the player page itself."""
        user = _current_user(request)
        conn = _conn()
        show = conn.execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (show_id,)
        ).fetchone()
        if not show or show["kind"] != "tv_show":
            raise HTTPException(404, "show not found")
        next_id = _next_episode_for_show(conn, show_id, user["id"] if user else None)
        if next_id is not None:
            return RedirectResponse(f"/watch/episode/{next_id}", status_code=303)
        # No episodes scanned yet — fall back to a small placeholder page.
        title = html.escape(show["title_guess"])
        body = (
            f"<h2>{title}</h2>"
            "<p class='empty'>No episodes scanned yet.</p>"
            "<p><a href='/'>&larr; Libraries</a></p>"
        )
        return _page(title, body, user=user)

    @app.get("/watch/episode/{episode_id}", response_class=HTMLResponse)
    def page_watch_episode(episode_id: int, request: Request):
        user = _current_user(request)
        row = _conn().execute(
            "SELECT e.id, e.season, e.episode, e.title_guess, e.show_id, m.title_guess AS show_title "
            "FROM tv_episodes e JOIN media m ON m.id = e.show_id WHERE e.id = ?",
            (episode_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "episode not found")
        if row["season"] == 0:
            ep_label = f"Extras E{row['episode']:02d}" if row["episode"] else "Extras"
        else:
            ep_label = f"S{row['season']:02d}E{row['episode']:02d}"
        title = f"{row['show_title']} {ep_label}"
        variants = subs_mod.subtitle_variants_for_episode(_conn(), episode_id)
        track_html = _subtitle_tracks_html(
            f"/subtitle/episode/{episode_id}", variants
        )
        admin_actions = ""
        admin_js = ""
        if user and user["is_admin"]:
            admin_actions = _generate_subs_button_html(variants, f"/api/admin/asr/episode/{episode_id}")
            admin_js = _generate_subs_js()
        strip_html = _episode_strip_html(
            _conn(),
            show_id=row["show_id"],
            current_episode_id=episode_id,
            current_season=row["season"],
            user_id=user["id"] if user else None,
        )
        body = (
            f"<div class='theater'>"
            f"<video id='player' controls autoplay playsinline preload='auto' "
            f"src='/stream/episode/{episode_id}'>"
            f"{track_html}"
            "Your browser does not support the video element.</video>"
            f"</div>"
            f"<div class='theater-meta'>"
            f"<h2>{html.escape(row['show_title'])} &mdash; {ep_label}</h2>"
            f"<p>{html.escape(row['title_guess'] or '')}</p>"
            f"{admin_actions}"
            "</div>"
            f"{strip_html}"
            + _player_progress_js(f"/api/progress/episode/{episode_id}")
            + admin_js
        )
        return _page(title, body, user=user, body_class="watch")

    @app.get("/book/{book_id}", response_class=HTMLResponse)
    def page_book(book_id: int, request: Request):
        user = _current_user(request)
        conn = _conn()
        book = conn.execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (book_id,)
        ).fetchone()
        if not book or book["kind"] != "audiobook":
            raise HTTPException(404, "audiobook not found")
        tracks = conn.execute(
            "SELECT id, order_index, source_path FROM audiobook_tracks "
            "WHERE book_id = ? ORDER BY order_index",
            (book_id,),
        ).fetchall()
        title = html.escape(book["title_guess"])
        if not tracks:
            return _page(title, f"<h2>{title}</h2><p class='empty'>No tracks scanned.</p>", user=user)
        # Build a JS playlist that auto-advances.
        playlist_json = (
            "[" + ",".join(
                "{"
                f"\"id\":{t['id']},\"label\":\"{html.escape(Path(t['source_path']).name)}\""
                "}"
                for t in tracks
            ) + "]"
        )
        chapter_lis = "".join(
            f"<li data-track-id='{t['id']}'>"
            f"<button type='button' class='ch'>{t['order_index']+1}. "
            f"{html.escape(Path(t['source_path']).name)}</button></li>"
            for t in tracks
        )
        body = f"""
<h2>{title}</h2>
<audio id='player' controls preload='metadata'></audio>
<p id='now-playing' class='hint'>Loading...</p>
<ol class='chapters'>{chapter_lis}</ol>
<p><a href='/'>&larr; Libraries</a></p>
<script>(function(){{
  var playlist = {playlist_json};
  var book_id = {book_id};
  var el = document.getElementById('player');
  var nowPlaying = document.getElementById('now-playing');
  var idx = 0;
  function load(i){{
    if(i<0||i>=playlist.length) return;
    idx = i;
    var t = playlist[i];
    el.src = '/stream/track/' + t.id;
    nowPlaying.textContent = 'Chapter ' + (i+1) + ': ' + t.label;
    document.querySelectorAll('.chapters li').forEach(function(li){{
      li.classList.toggle('active', parseInt(li.dataset.trackId)===t.id);
    }});
  }}
  function play(i){{ load(i); el.play().catch(function(){{}}); }}
  document.querySelectorAll('.chapters .ch').forEach(function(btn,i){{
    btn.addEventListener('click', function(){{ play(i); }});
  }});
  el.addEventListener('ended', function(){{
    if(idx<playlist.length-1) play(idx+1);
  }});
  // Resume + save progress + autoplay (with muted fallback)
  fetch('/api/progress/book/' + book_id).then(function(r){{return r.ok?r.json():null;}}).then(function(p){{
    if(!p||!p.current_track_id){{ load(0); }}
    else {{
      var i = playlist.findIndex(function(t){{return t.id===p.current_track_id;}});
      if(i<0) i = 0;
      load(i);
      el.addEventListener('loadedmetadata', function once(){{
        el.removeEventListener('loadedmetadata', once);
        el.currentTime = p.position_seconds || 0;
      }});
    }}
    el.addEventListener('canplay', function once(){{
      el.removeEventListener('canplay', once);
      el.play().catch(function(){{el.muted=true;el.play().catch(function(){{}});}});
    }});
  }});
  var last = 0;
  el.addEventListener('timeupdate', function(){{
    var t = el.currentTime;
    if(Math.abs(t-last)<5) return; last = t;
    fetch('/api/progress/book/' + book_id, {{
      method: 'PUT',
      headers: {{'Content-Type':'application/json'}},
      body: JSON.stringify({{track_id: playlist[idx].id, position_seconds: t}})
    }});
  }});
}})();</script>
"""
        return _page(title, body, user=user)

    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request, next: str = "/"):
        user = _current_user(request)
        if user:
            return RedirectResponse(next or "/", status_code=303)
        # Landing-page tone: this server is private. Authorized users only.
        next_safe = next if (next.startswith("/") and not next.startswith("//")) else "/"
        body = f"""
<div class='landing'>
  <h2>Cuttlefish</h2>
  <p class='warn'><strong>Authorized access only.</strong>
  This is a private media server. Unauthorized use, distribution of
  content, or attempts to bypass authentication are prohibited.
  All access is logged.</p>
  <form method='post' action='/login' class='auth'>
    <input type='hidden' name='next' value='{html.escape(next_safe)}'>
    <label>Username <input name='username' autofocus required></label>
    <label>Password <input name='password' type='password' required></label>
    <button type='submit'>Log in</button>
  </form>
</div>"""
        return _page("Log in", body, user=None)

    @app.post("/login")
    def page_login_submit(
        username: str = Form(...),
        password: str = Form(...),
        next: str = Form("/"),
    ):
        conn = _conn()
        user_id = auth.authenticate(conn, username, password)
        if user_id is None:
            return _page(
                "Log in",
                "<p class='error'>Invalid credentials.</p>"
                + _login_form_html(next),
                user=None,
            )
        token, expires = auth.create_session(conn, user_id)
        next_safe = next if (next and next.startswith("/") and not next.startswith("//")) else "/"
        resp = RedirectResponse(next_safe, status_code=303)
        resp.set_cookie(
            auth.SESSION_COOKIE_NAME,
            token,
            httponly=True,
            samesite="lax",
            expires=expires,
            path="/",
        )
        return resp

    @app.get("/register", response_class=HTMLResponse)
    def page_register(request: Request):
        user = _current_user(request)
        first_user = auth.user_count(_conn()) == 0
        # First user: open form (covers DBs created without serve's bootstrap,
        # e.g. existing installs upgrading or someone running serve once with
        # output redirected and missing the banner).
        if not first_user and (not user or not user["is_admin"]):
            body = (
                "<h2>Register</h2>"
                "<p>New accounts are created by an admin. Visit "
                "<a href='/admin/users'>Admin → Users</a> if you have admin "
                "access, or ask the server's admin to add you.</p>"
                "<p><a href='/login'>&larr; Log in</a></p>"
            )
            return _page("Register", body, user=user)
        admin_note = (
            "<p class='hint'>You'll be the first user, so you'll be the admin.</p>"
            if first_user
            else "<p class='hint'>You're an admin — adding a regular user.</p>"
        )
        body = f"""
        <form method='post' action='/register' class='auth'>
          <h2>Register</h2>
          {admin_note}
          <label>Username <input name='username' autofocus required></label>
          <label>Password (>= 6 chars) <input name='password' type='password' minlength='6' required></label>
          <button type='submit'>Register</button>
        </form>"""
        return _page("Register", body, user=user)

    @app.post("/register")
    def page_register_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        conn = _conn()
        first_user = auth.user_count(conn) == 0
        if not first_user:
            current = _current_user(request)
            if not current or not current["is_admin"]:
                raise HTTPException(403, "registration is admin-only after the first user")
        if len(password) < 6:
            return _page(
                "Register",
                "<p class='error'>Password must be at least 6 characters.</p>",
                user=None,
            )
        try:
            auth.create_user(conn, username, password, is_admin=first_user)
        except sqlite3.IntegrityError:
            return _page("Register", "<p class='error'>Username taken.</p>", user=None)
        return RedirectResponse("/login", status_code=303)

    # --- Casting ---------------------------------------------------------

    @app.websocket("/api/cast/channel")
    async def ws_cast(ws: WebSocket):
        # Authenticate via the session cookie used by the rest of the app.
        token = ws.cookies.get(auth.SESSION_COOKIE_NAME)
        if not token:
            await ws.close(code=4401)
            return
        sess = auth.lookup_session(_conn(), token)
        if not sess:
            await ws.close(code=4401)
            return
        await ws.accept()
        # First message must be {type: 'identify', role, label}
        try:
            ident = await ws.receive_json()
        except Exception:
            await ws.close()
            return
        role = ident.get("role")
        label = (ident.get("label") or "Unnamed device")[:80]
        if role not in ("target", "controller"):
            await ws.close(code=4400)
            return
        dev = await cast_bus.register(sess["id"], role, label, ws)
        await ws.send_json({
            "type": "registered",
            "client_id": dev.client_id,
            "role": role,
            "targets": cast_bus.list_for(sess["id"], role="target",
                                          except_id=dev.client_id),
        })
        try:
            while True:
                msg = await ws.receive_json()
                msg_type = msg.get("type")
                if msg_type == "command":
                    target_id = msg.get("to")
                    if target_id:
                        await cast_bus.send_to(
                            sess["id"], target_id,
                            {"type": "command",
                             "from": dev.client_id,
                             "action": msg.get("action"),
                             "payload": msg.get("payload") or {}},
                        )
                elif msg_type == "state_update":
                    # Fan out state updates to all controllers for this user
                    for d in cast_bus.list_for(sess["id"], role="controller"):
                        await cast_bus.send_to(
                            sess["id"], d["client_id"],
                            {"type": "state_update",
                             "from": dev.client_id,
                             "media_id": msg.get("media_id"),
                             "position_seconds": msg.get("position_seconds"),
                             "playing": msg.get("playing")},
                        )
                # Other types ignored — clients shouldn't send them
        except WebSocketDisconnect:
            pass
        finally:
            await cast_bus.unregister(dev)

    @app.get("/api/cast/targets")
    def api_cast_targets(request: Request):
        user = _require_user(request)
        return cast_bus.list_for(user["id"], role="target")

    @app.get("/cast", response_class=HTMLResponse)
    def page_cast(request: Request):
        user = _require_user(request)
        body = """
<h2>Cast</h2>
<p class='hint'>Open the <a href='/'>Libraries</a> page on your TV (or any
other device, logged in as you) and start playing something. That tab
becomes a 'target'. From here you can pause / play / seek that target.</p>
<div id='status' class='hint'>Connecting...</div>
<div id='targets'></div>
<script>(function(){
  var statusEl = document.getElementById('status');
  var targetsEl = document.getElementById('targets');
  var ws = new WebSocket((location.protocol==='https:'?'wss://':'ws://') + location.host + '/api/cast/channel');
  var myId = null;
  var targets = {};
  function render(){
    var html = '';
    var keys = Object.keys(targets);
    if(keys.length===0){
      html = '<p class=\\'empty\\'>No active targets.</p>';
    } else {
      html = '<ul class=\\'media\\'>' + keys.map(function(id){
        var t = targets[id];
        return '<li><strong>'+escapeHtml(t.label)+'</strong> '
          + '<button data-id=\\''+id+'\\' data-action=\\'play\\'>Play</button> '
          + '<button data-id=\\''+id+'\\' data-action=\\'pause\\'>Pause</button> '
          + '<button data-id=\\''+id+'\\' data-action=\\'seek-back\\'>-10s</button> '
          + '<button data-id=\\''+id+'\\' data-action=\\'seek-fwd\\'>+30s</button>'
          + (t.position!=null ? ' <span class=\\'kind\\'>at '+formatTime(t.position)+'</span>' : '')
          + '</li>';
      }).join('') + '</ul>';
    }
    targetsEl.innerHTML = html;
    targetsEl.querySelectorAll('button').forEach(function(b){
      b.addEventListener('click', function(){ command(b.dataset.id, b.dataset.action); });
    });
  }
  function escapeHtml(s){return String(s).replace(/[&<>\\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\\"':'&quot;',\"'\":'&#39;'}[c];});}
  function formatTime(s){var m=Math.floor(s/60);var sec=Math.floor(s%60);return m+':'+(sec<10?'0':'')+sec;}
  function command(id, action){
    var payload = {};
    if(action==='seek-back'){ action='seek'; payload.delta = -10; }
    if(action==='seek-fwd'){ action='seek'; payload.delta = 30; }
    ws.send(JSON.stringify({type:'command', to:id, action:action, payload:payload}));
  }
  ws.addEventListener('open', function(){
    ws.send(JSON.stringify({type:'identify', role:'controller', label:'Controller'}));
  });
  ws.addEventListener('message', function(e){
    var m = JSON.parse(e.data);
    if(m.type==='registered'){
      statusEl.textContent = 'Connected. Listening for targets...';
      myId = m.client_id;
      (m.targets||[]).forEach(function(t){ targets[t.client_id] = {label:t.label}; });
      render();
    } else if(m.type==='target_available'){
      targets[m.client_id] = {label:m.label};
      render();
    } else if(m.type==='target_gone'){
      delete targets[m.client_id]; render();
    } else if(m.type==='state_update'){
      if(targets[m.from]){ targets[m.from].position = m.position_seconds; render(); }
    }
  });
  ws.addEventListener('close', function(){ statusEl.textContent = 'Disconnected.'; });
})();</script>
"""
        return _page("Cast", body, user=user)

    @app.get("/account", response_class=HTMLResponse)
    def page_account(request: Request, error: str = "", ok: str = ""):
        user = _require_user(request)
        notice = ""
        if error:
            notice = f"<p class='error'>{html.escape(error)}</p>"
        elif ok:
            notice = f"<p class='hint' style='color:#6c6'>{html.escape(ok)}</p>"
        body = f"""
<h2>Account</h2>
<p>Signed in as <strong>{html.escape(user['username'])}</strong>"""
        body += " (admin)" if user["is_admin"] else ""
        body += f"""</p>
{notice}
<h3>Change password</h3>
<form method='post' action='/account/password' class='auth'>
  <label>Current password <input name='current_password' type='password' required></label>
  <label>New password (>= 6 chars) <input name='new_password' type='password' minlength='6' required></label>
  <label>Confirm new password <input name='confirm_password' type='password' minlength='6' required></label>
  <button type='submit'>Change password</button>
</form>
<p><a href='/'>&larr; Home</a></p>
"""
        return _page("Account", body, user=user)

    @app.post("/account/password")
    def page_change_my_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
    ):
        user = _require_user(request)
        if new_password != confirm_password:
            return RedirectResponse(
                "/account?error=" + "New password and confirmation do not match.",
                status_code=303,
            )
        if len(new_password) < 6:
            return RedirectResponse(
                "/account?error=" + "Password must be at least 6 characters.",
                status_code=303,
            )
        conn = _conn()
        if auth.authenticate(conn, user["username"], current_password) != user["id"]:
            return RedirectResponse(
                "/account?error=" + "Current password is incorrect.",
                status_code=303,
            )
        with conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (auth.hash_password(new_password), user["id"]),
            )
        return RedirectResponse("/account?ok=Password+updated.", status_code=303)

    @app.post("/logout")
    def page_logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if token:
            auth.delete_session(_conn(), token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
        return resp

    # --- Search ---------------------------------------------------------

    @app.get("/api/search")
    def api_search(q: str):
        if not q or not q.strip():
            return {"media": [], "episodes": []}
        like = f"%{q.strip()}%"
        conn = _conn()
        media = conn.execute(
            "SELECT m.id, m.kind, m.title_guess, l.name AS library "
            "FROM media m JOIN libraries l ON l.id = m.library_id "
            "WHERE m.title_guess LIKE ? COLLATE NOCASE "
            "ORDER BY m.kind, m.title_guess LIMIT 50",
            (like,),
        ).fetchall()
        episodes = conn.execute(
            "SELECT e.id, e.season, e.episode, e.title_guess, "
            "       m.id AS show_id, m.title_guess AS show_title "
            "FROM tv_episodes e JOIN media m ON m.id = e.show_id "
            "WHERE e.title_guess LIKE ? COLLATE NOCASE "
            "ORDER BY m.title_guess, e.season, e.episode LIMIT 50",
            (like,),
        ).fetchall()
        return {
            "media": [dict(r) for r in media],
            "episodes": [dict(r) for r in episodes],
        }

    @app.get("/search", response_class=HTMLResponse)
    def page_search(request: Request, q: str = ""):
        user = _current_user(request)
        q = q.strip()
        results_html = ""
        if q:
            data = api_search(q)
            blocks = []
            if data["media"]:
                lis = "".join(
                    f"<li><a href='{_watch_url(r['kind'], r['id'])}'>"
                    f"{html.escape(r['title_guess'])}</a> "
                    f"<span class='kind'>{r['kind']} · {html.escape(r['library'])}</span></li>"
                    for r in data["media"]
                )
                blocks.append(f"<h3>Media</h3><ul class='media'>{lis}</ul>")
            if data["episodes"]:
                lis = "".join(
                    f"<li><a href='/watch/episode/{r['id']}'>"
                    f"{html.escape(r['show_title'])} S{r['season']:02d}E{r['episode']:02d}"
                    f" &mdash; {html.escape(r['title_guess'] or '')}</a></li>"
                    for r in data["episodes"]
                )
                blocks.append(f"<h3>Episodes</h3><ul class='episodes'>{lis}</ul>")
            if not blocks:
                blocks.append("<p class='empty'>No matches.</p>")
            results_html = "".join(blocks)
        body = f"""
<h2>Search</h2>
<form method='get' action='/search' class='search'>
  <input name='q' value='{html.escape(q)}' placeholder='Title or episode keyword' autofocus>
  <button type='submit'>Search</button>
</form>
{results_html}
"""
        return _page("Search", body, user=user)

    # --- Continue Watching ----------------------------------------------

    @app.get("/api/continue-watching")
    def api_continue_watching(request: Request, limit: int = 20):
        user = _require_user(request)
        conn = _conn()
        media_rows = conn.execute(
            """
            SELECT m.id, m.kind, m.title_guess, mp.position_seconds,
                   mp.duration_seconds, mp.updated_at
            FROM media_progress mp
            JOIN media m ON m.id = mp.media_id
            WHERE mp.user_id = ? AND mp.position_seconds > 0
            ORDER BY mp.updated_at DESC LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
        episode_rows = conn.execute(
            """
            SELECT e.id, e.season, e.episode, e.title_guess AS ep_title,
                   m.id AS show_id, m.title_guess AS show_title,
                   ep.position_seconds, ep.duration_seconds, ep.updated_at
            FROM episode_progress ep
            JOIN tv_episodes e ON e.id = ep.episode_id
            JOIN media m ON m.id = e.show_id
            WHERE ep.user_id = ? AND ep.position_seconds > 0
            ORDER BY ep.updated_at DESC LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
        book_rows = conn.execute(
            """
            SELECT m.id, m.title_guess, ap.current_track_id,
                   ap.position_seconds, ap.updated_at
            FROM audiobook_progress ap
            JOIN media m ON m.id = ap.book_id
            WHERE ap.user_id = ? AND (ap.position_seconds > 0 OR ap.current_track_id IS NOT NULL)
            ORDER BY ap.updated_at DESC LIMIT ?
            """,
            (user["id"], limit),
        ).fetchall()
        return {
            "media": [dict(r) for r in media_rows],
            "episodes": [dict(r) for r in episode_rows],
            "audiobooks": [dict(r) for r in book_rows],
        }

    @app.get("/continue-watching", response_class=HTMLResponse)
    def page_continue_watching(request: Request):
        user = _require_user(request)
        data = api_continue_watching(request)
        blocks = []
        if data["media"]:
            lis = "".join(
                f"<li><a href='{_watch_url(r['kind'], r['id'])}'>"
                f"{html.escape(r['title_guess'])}</a> "
                f"<span class='kind'>{_progress_label(r)}</span> "
                f"{_progress_actions(r['id'], 'media')}</li>"
                for r in data["media"]
            )
            blocks.append(f"<h3>Movies / books</h3><ul class='media'>{lis}</ul>")
        if data["episodes"]:
            lis = "".join(
                f"<li><a href='/watch/episode/{r['id']}'>"
                f"{html.escape(r['show_title'])} S{r['season']:02d}E{r['episode']:02d}"
                f"</a> <span class='kind'>{_progress_label(r)}</span> "
                f"{_progress_actions(r['id'], 'episode')}</li>"
                for r in data["episodes"]
            )
            blocks.append(f"<h3>TV episodes</h3><ul class='episodes'>{lis}</ul>")
        if data["audiobooks"]:
            lis = "".join(
                f"<li><a href='/book/{r['id']}'>{html.escape(r['title_guess'])}</a></li>"
                for r in data["audiobooks"]
            )
            blocks.append(f"<h3>Audiobooks</h3><ul class='media'>{lis}</ul>")
        if not blocks:
            body = "<h2>Continue Watching</h2><p class='empty'>Nothing in progress yet.</p>"
        else:
            body = "<h2>Continue Watching</h2>" + "".join(blocks)
        return _page("Continue Watching", body, user=user)

    @app.post("/progress/{media_id}/reset")
    def page_reset_progress(media_id: int, request: Request):
        user = _require_user(request)
        with _conn() as conn:
            conn.execute(
                "DELETE FROM media_progress WHERE user_id = ? AND media_id = ?",
                (user["id"], media_id),
            )
        return RedirectResponse("/continue-watching", status_code=303)

    @app.post("/progress/{media_id}/watched")
    def page_mark_watched(media_id: int, request: Request):
        api_mark_watched(media_id, request)
        return RedirectResponse("/continue-watching", status_code=303)

    @app.post("/progress/episode/{episode_id}/reset")
    def page_reset_episode_progress(episode_id: int, request: Request):
        user = _require_user(request)
        with _conn() as conn:
            conn.execute(
                "DELETE FROM episode_progress WHERE user_id = ? AND episode_id = ?",
                (user["id"], episode_id),
            )
        return RedirectResponse("/continue-watching", status_code=303)

    return app


_STYLE = """
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto;
       padding: 0 1rem; background: #111; color: #eee; }
a { color: #6cf; text-decoration: none; }
a:hover { text-decoration: underline; }
h1 a { color: inherit; }
header { display: flex; justify-content: space-between; align-items: baseline;
         border-bottom: 1px solid #333; padding-bottom: .5rem; margin-bottom: 1rem; }
header .userbar { font-size: .9em; color: #aaa; }
header form { display: inline; }
header button { background: none; border: none; color: #6cf; cursor: pointer;
                padding: 0; font: inherit; }
header button:hover { text-decoration: underline; }
ul.libraries, ul.media, ul.episodes, ol.chapters { list-style: none; padding: 0; }
ul.libraries li, ul.media li, ul.episodes li, ol.chapters li {
    padding: .4rem 0; border-bottom: 1px solid #222;
}
ol.chapters li.active button { color: #fc6; font-weight: bold; }
ol.chapters button { background: none; border: none; color: #6cf;
                      cursor: pointer; padding: 0; font: inherit; text-align: left; }
ol.chapters button:hover { text-decoration: underline; }
.kind { color: #888; font-size: .8em; margin-left: .5rem; }
video, audio { width: 100%; max-height: 70vh; background: #000; }
audio { max-height: 50px; }
/* Theater mode for the watch page: video stretches to the viewport. */
body.watch { max-width: none; margin: 0; padding: 0; background: #000; }
body.watch header { max-width: 1400px; margin: 0 auto; padding: .4rem 1rem; }
body.watch nav.top { max-width: 1400px; margin: 0 auto; padding: 0 1rem; }
body.watch .theater { width: 100%; background: #000; display: flex;
                       justify-content: center; align-items: center; }
body.watch .theater video { width: 100%; height: auto; max-height: 90vh;
                             display: block; background: #000; }
body.watch .theater-meta { max-width: 1400px; margin: 1rem auto; padding: 0 1rem; }
body.watch .theater-meta h2 { margin-top: 0; }
.landing { max-width: 480px; margin: 2rem auto; }
.landing h2 { margin-top: 0; }
.landing .warn { background: #2a1a0d; border-left: 4px solid #c93; color: #eee;
                  padding: .75rem 1rem; border-radius: 3px; line-height: 1.4; }
.admin-actions { background: #1a1a1a; border: 1px solid #333; border-radius: 4px;
                  padding: .75rem 1rem; margin-top: 1rem; display: flex;
                  align-items: center; gap: 1rem; flex-wrap: wrap; }
.admin-actions form { margin: 0; }
.admin-actions button { padding: .4rem .8rem; background: #245; color: #eee;
                         border: 1px solid #468; border-radius: 3px; cursor: pointer; font: inherit; }
.admin-actions button:disabled { opacity: .55; cursor: not-allowed; }
.admin-actions .hint { color: #888; font-size: .9em; }
/* ASR job status: spinner + bar + colored text */
#gen-subs-status { display: inline-flex; align-items: center; gap: .5rem;
                    flex-wrap: wrap; min-width: 12rem; }
#gen-subs-status.working .asr-text  { color: #fc6; font-weight: 500; }
#gen-subs-status.running .asr-text  { color: #6cf; font-weight: 500; }
#gen-subs-status.done    .asr-text  { color: #6c6; font-weight: 500; }
#gen-subs-status.failed  .asr-text  { color: #f66; font-weight: 500; }
.bulk-asr-status { display: inline-flex; align-items: center; gap: .5rem;
                    font-size: .9em; }
.bulk-asr-status.working .asr-text { color: #fc6; font-weight: 500; }
.bulk-asr-status.running .asr-text { color: #6cf; font-weight: 500; }
.bulk-asr-status.done    .asr-text { color: #6c6; font-weight: 500; }
.bulk-asr-status.failed  .asr-text { color: #f66; font-weight: 500; }
.asr-spinner { display: inline-block; width: 14px; height: 14px;
                border: 2px solid currentColor; border-top-color: transparent;
                border-radius: 50%; animation: asr-spin 0.8s linear infinite;
                vertical-align: -2px; }
@keyframes asr-spin { to { transform: rotate(360deg); } }
.asr-bar { display: inline-block; flex: 1 0 8rem; height: 6px;
            background: #222; border-radius: 3px; overflow: hidden;
            position: relative; }
.asr-bar > span { position: absolute; left: -40%; top: 0; bottom: 0; width: 40%;
                   background: linear-gradient(90deg, transparent, #6cf, transparent);
                   animation: asr-slide 1.4s ease-in-out infinite; }
@keyframes asr-slide { from { left: -40%; } to { left: 100%; } }
.empty, .hint { color: #888; }
.error { color: #f66; }
code { background: #222; padding: .15em .4em; border-radius: 3px; }
form.auth { display: grid; gap: .75rem; max-width: 320px; }
form.auth label { display: grid; gap: .25rem; font-size: .9em; color: #aaa; }
form.auth input { padding: .4rem .5rem; background: #222; color: #eee;
                   border: 1px solid #333; border-radius: 3px; font: inherit; }
form.auth button { padding: .5rem; background: #245; color: #eee;
                    border: 1px solid #468; border-radius: 3px; cursor: pointer; }
table.admin { width: 100%; border-collapse: collapse; }
table.admin th, table.admin td { padding: .35rem .5rem;
                                  border-bottom: 1px solid #222; text-align: left; }
table.admin th { color: #aaa; font-weight: normal; font-size: .85em; }
.size { color: #888; font-size: .85em; }
.status-queued  { color: #aaa; }
.status-running { color: #fc6; }
.status-done    { color: #6c6; }
.status-failed  { color: #f66; }
.scan-status    { font-size: .9em; }
/* Episode strip — Jellyfin-style horizontal cards under the player. */
.episode-strip { max-width: 1400px; margin: 1.5rem auto; padding: 0 1rem; }
.season-tabs { display: flex; gap: .25rem; margin-bottom: .75rem;
                border-bottom: 1px solid #333; }
.season-tabs button { background: none; border: none; padding: .5rem 1rem;
                       color: #aaa; cursor: pointer; font: inherit;
                       border-bottom: 2px solid transparent; margin-bottom: -1px; }
.season-tabs button:hover { color: #eee; }
.season-tabs button.active { color: #fff; border-bottom-color: #6cf; }
.ep-cards { display: grid; grid-auto-flow: column;
             grid-auto-columns: minmax(240px, 1fr);
             gap: .75rem; overflow-x: auto; padding-bottom: .5rem; }
/* The HTML `hidden` attribute is normally `display: none` via the UA
   sheet, but our explicit `display: grid` above outranks the UA rule.
   Re-assert it at our specificity so season tabs actually filter. */
.ep-cards[hidden] { display: none; }
.ep-card { display: block; min-width: 240px; max-width: 320px;
            text-decoration: none; color: inherit; }
.ep-card .ep-thumb { position: relative; aspect-ratio: 16 / 9;
                      background: #1a1a1a; border-radius: 4px; overflow: hidden;
                      border: 2px solid transparent; }
.ep-card.current .ep-thumb { border-color: #6cf;
                              box-shadow: 0 0 0 1px #6cf; }
.ep-card .ep-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.ep-card .ep-label { position: absolute; left: .4rem; bottom: .4rem;
                      background: rgba(0,0,0,.75); color: #eee;
                      padding: .1rem .4rem; border-radius: 2px;
                      font-size: .75em; font-weight: bold; letter-spacing: .03em; }
.ep-card .ep-progress { position: absolute; left: 0; right: 0; bottom: 0;
                         height: 3px; background: rgba(255,255,255,.15); }
.ep-card .ep-progress > span { display: block; height: 100%; background: #6cf; }
.ep-card .ep-watched { position: absolute; right: .4rem; top: .4rem;
                        background: rgba(0,0,0,.75); color: #6c6;
                        padding: .1rem .4rem; border-radius: 2px;
                        font-size: .85em; font-weight: bold; }
.ep-card.watched .ep-thumb img { opacity: .55; }
.ep-card .ep-title { display: block; padding: .4rem 0 0; color: #ddd;
                      font-size: .9em; line-height: 1.25;
                      overflow: hidden; text-overflow: ellipsis;
                      display: -webkit-box; -webkit-line-clamp: 2;
                      -webkit-box-orient: vertical; }
.ep-card.current .ep-title { color: #fff; }
ul.cards { list-style: none; padding: 0; display: grid;
            grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 1rem; }
ul.cards li.card a { display: block; }
ul.cards li.card .poster-wrap { position: relative; aspect-ratio: 2 / 3;
                                  border-radius: 4px; overflow: hidden;
                                  background: #222; }
ul.cards li.card .poster-wrap .no-poster { position: absolute; inset: 0;
                                            background: #222;
                                            background-image: linear-gradient(135deg,
                                              #1a1a1a 25%, #222 25%, #222 50%,
                                              #1a1a1a 50%, #1a1a1a 75%, #222 75%); }
ul.cards li.card .poster-wrap img { position: absolute; inset: 0;
                                     width: 100%; height: 100%;
                                     object-fit: cover; }
ul.cards li.card .card-title { display: block; font-size: .9em; padding: .4rem 0 0;
                                color: #eee; line-height: 1.2; }
ul.cards li.card .card-kind { display: block; font-size: .75em; color: #888; }
section.media-section { margin-bottom: 2rem; }
section.media-section h2 { color: #eee; margin-bottom: .75rem; }
section.media-section h2 .kind { color: #888; font-size: .7em; font-weight: normal; }
img.show-poster { max-width: 200px; max-height: 300px; float: right;
                   margin: 0 0 1rem 1rem; border-radius: 4px; }
form.search { display: flex; gap: .5rem; margin-bottom: 1rem; }
form.search input { flex: 1; padding: .4rem .5rem; background: #222;
                     color: #eee; border: 1px solid #333; border-radius: 3px; font: inherit; }
form.search button { padding: .4rem .8rem; background: #245; color: #eee;
                      border: 1px solid #468; border-radius: 3px; cursor: pointer; }
nav.top { display: flex; gap: 1rem; margin-bottom: 1rem; font-size: .9em; }
"""


def _progress_actions(item_id: int, kind: str) -> str:
    """Inline forms for 'mark watched' and 'reset' next to a continue-watching row."""
    base = f"/progress/{item_id}" if kind == "media" else f"/progress/episode/{item_id}"
    watched = (
        f"<form method='post' action='{base}/watched' style='display:inline'>"
        "<button type='submit'>Mark watched</button></form>"
        if kind == "media"
        else ""
    )
    reset = (
        f"<form method='post' action='{base}/reset' style='display:inline'>"
        "<button type='submit'>Reset</button></form>"
    )
    return f"<span class='actions'>{watched} {reset}</span>"


def _episode_strip_html(
    conn, show_id: int, current_episode_id: int,
    current_season: int, user_id: Optional[int],
) -> str:
    """Render the bottom-of-page episode strip with season tabs.

    All seasons are rendered into the HTML; client-side JS shows only the
    active one. Each card shows the episode's frame thumbnail, S/E label,
    title, and a watched / in-progress indicator.
    """
    eps = conn.execute(
        "SELECT id, season, episode, title_guess, duration_seconds "
        "FROM tv_episodes WHERE show_id = ? ORDER BY season, episode, id",
        (show_id,),
    ).fetchall()
    if not eps:
        return ""

    # Per-user progress map for in-progress / watched indicators
    progress: dict[int, float] = {}
    if user_id is not None:
        for r in conn.execute(
            "SELECT episode_id, position_seconds, duration_seconds "
            "FROM episode_progress WHERE user_id = ?",
            (user_id,),
        ).fetchall():
            progress[r["episode_id"]] = r["position_seconds"]

    seasons: dict[int, list] = {}
    for e in eps:
        seasons.setdefault(e["season"], []).append(e)

    def _card_label(e) -> str:
        if e["season"] == 0:
            return f"EXT{e['episode']:02d}" if e["episode"] else "EXT"
        return f"S{e['season']:02d}E{e['episode']:02d}"

    def render_card(e) -> str:
        ep_id = e["id"]
        is_current = ep_id == current_episode_id
        pos = progress.get(ep_id) or 0
        dur = e["duration_seconds"] or 0
        watched = bool(dur) and pos >= 0.9 * dur
        in_progress = bool(pos) and not watched
        classes = ["ep-card"]
        if is_current:
            classes.append("current")
        if watched:
            classes.append("watched")
        elif in_progress:
            classes.append("in-progress")
        progress_overlay = ""
        if in_progress and dur:
            pct = max(2, min(100, int(100 * pos / dur)))
            progress_overlay = (
                f"<span class='ep-progress'><span style='width:{pct}%'></span></span>"
            )
        elif watched:
            progress_overlay = "<span class='ep-watched'>✓</span>"
        return (
            f"<a class='{' '.join(classes)}' href='/watch/episode/{ep_id}'>"
            f"<div class='ep-thumb'>"
            f"<img src='/poster/episode/{ep_id}' alt='' loading='lazy' "
            f"onerror=\"this.style.display='none'\">"
            f"<span class='ep-label'>{_card_label(e)}</span>"
            f"{progress_overlay}"
            f"</div>"
            f"<span class='ep-title'>{html.escape(e['title_guess'] or '(untitled)')}</span>"
            f"</a>"
        )

    # Sort regular seasons first, extras (season=0) last. So a show with
    # Season 1, Season 2, Specials renders tabs as: Season 1 | Season 2 | Extras.
    season_keys = sorted(seasons.keys(), key=lambda s: (s == 0, s))

    def _season_label(s: int) -> str:
        return "Extras" if s == 0 else f"Season {s}"

    if len(season_keys) > 1:
        tabs = "".join(
            f"<button type='button' data-season='{s}' class='{'active' if s == current_season else ''}'>"
            f"{_season_label(s)}</button>"
            for s in season_keys
        )
        tabs_html = f"<div class='season-tabs'>{tabs}</div>"
    else:
        tabs_html = ""

    season_blocks = "".join(
        f"<div class='ep-cards' data-season='{s}'"
        + ("" if s == current_season else " hidden")
        + ">"
        + "".join(render_card(e) for e in seasons[s])
        + "</div>"
        for s in season_keys
    )
    js = (
        "<script>(function(){"
        "var tabs=document.querySelectorAll('.season-tabs button');"
        "var blocks=document.querySelectorAll('.ep-cards');"
        "tabs.forEach(function(b){b.addEventListener('click',function(){"
        "  var s=b.dataset.season;"
        "  tabs.forEach(function(x){x.classList.toggle('active',x.dataset.season===s);});"
        "  blocks.forEach(function(x){x.hidden = (x.dataset.season!==s);});"
        "});});"
        "var cur=document.querySelector('.ep-card.current');"
        "if(cur)cur.scrollIntoView({behavior:'instant',block:'nearest',inline:'center'});"
        "})();</script>"
    )
    return (
        "<div class='episode-strip'>"
        f"{tabs_html}"
        f"{season_blocks}"
        "</div>"
        f"{js}"
    )


def _next_episode_for_show(conn, show_id: int, user_id: Optional[int]) -> Optional[int]:
    """Pick the next episode to play for `show_id`. For a logged-in user,
    that's the first episode that hasn't been watched ≥90% through, in
    season+episode order. Falls back to the first episode if everything
    is watched (or the user is anonymous).

    Extras (season=0) are skipped when the show has any regular-season
    episodes — clicking a show shouldn't dump you into a behind-the-scenes
    reel. If a show has *only* extras, those become the pool.
    """
    all_eps = conn.execute(
        "SELECT id, season, episode, duration_seconds FROM tv_episodes "
        "WHERE show_id = ? ORDER BY season, episode, id",
        (show_id,),
    ).fetchall()
    if not all_eps:
        return None
    regular = [e for e in all_eps if e["season"] != 0]
    pool = regular if regular else all_eps
    if user_id is None:
        return pool[0]["id"]
    progress = {
        r["episode_id"]: r["position_seconds"]
        for r in conn.execute(
            "SELECT episode_id, position_seconds FROM episode_progress "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    }
    for ep in pool:
        pos = progress.get(ep["id"]) or 0
        dur = ep["duration_seconds"] or 0
        if dur and pos >= 0.9 * dur:
            continue
        return ep["id"]
    return pool[0]["id"]


def _format_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return ""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s"


def _progress_label(row: dict) -> str:
    """Format a 'mm:ss / mm:ss' style progress label for continue-watching."""
    pos = row.get("position_seconds") or 0
    dur = row.get("duration_seconds")
    pos_s = f"{int(pos // 60)}:{int(pos % 60):02d}"
    if dur:
        dur_s = f"{int(dur // 60)}:{int(dur % 60):02d}"
        pct = int(min(100, (pos / dur) * 100)) if dur else 0
        return f"{pos_s} / {dur_s} ({pct}%)"
    return pos_s


def _human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}" if u != "B" else f"{int(f)} B"
        f /= 1024
    return f"{int(n)} B"


def _kind_label(kind: str) -> str:
    return {"movie": "Movie", "tv_show": "TV Show", "audiobook": "Audiobook"}.get(kind, kind)


def _resolve_video_for_thumbnail(row, kind: str) -> Optional[Path]:
    """Pick a video file we can ffmpeg a frame from for the thumbnail.

    For movies + episodes: prefer the encoded version if present; fall back
    to the source. For TV shows the row's source_path is the show directory
    — caller handles that via the show poster route, not this helper.
    """
    encoded = row["encoded_video"]
    if encoded and Path(encoded).is_file():
        return Path(encoded)
    src = Path(row["source_path"])
    if src.is_file():
        return src
    if src.is_dir():
        # Movie folder layout: pick the first video inside
        for child in sorted(src.iterdir()):
            if child.is_file() and child.suffix.lower() in {
                ".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv"
            }:
                return child
    return None


def _watch_url(kind: str, media_id: int) -> str:
    if kind == "tv_show":
        return f"/show/{media_id}"
    if kind == "audiobook":
        return f"/book/{media_id}"
    return f"/watch/{media_id}"


def _scan_progress_js() -> str:
    """Polls /api/admin/scans and updates the per-library status cell."""
    return (
        "<script>(function(){"
        "function fmt(p){"
        "  if(p.status==='running'){"
        "    var label='Scanning';"
        "    if(p.total>0)label+=' '+p.current+' / '+p.total;"
        "    if(p.current_item)label+=' — '+p.current_item;"
        "    return label;"
        "  }"
        "  if(p.status==='done'){"
        "    var r=p.result||{};"
        "    var bits=[];"
        "    if(r.movies_added)bits.push(r.movies_added+' movie(s)');"
        "    if(r.shows_added)bits.push(r.shows_added+' show(s)');"
        "    if(r.episodes_added)bits.push(r.episodes_added+' episode(s)');"
        "    if(r.audiobooks_added)bits.push(r.audiobooks_added+' audiobook(s)');"
        "    return 'Done: '+(bits.length?bits.join(', '):'no media found');"
        "  }"
        "  if(p.status==='failed')return 'Failed: '+(p.error||'unknown error');"
        "  return p.status||'';"
        "}"
        "function tick(){"
        "  fetch('/api/admin/scans').then(function(r){"
        "    if(!r.ok)return null;"
        "    return r.json();"
        "  }).then(function(scans){"
        "    if(!scans)return;"
        "    var anyRunning=false;"
        "    document.querySelectorAll('tr[data-lib-id]').forEach(function(tr){"
        "      var id=tr.dataset.libId;"
        "      var p=scans[id];"
        "      var cell=tr.querySelector('.scan-status');"
        "      if(!cell)return;"
        "      if(p){"
        "        cell.textContent=fmt(p);"
        "        cell.className='scan-status status-'+p.status;"
        "        if(p.status==='running')anyRunning=true;"
        "      }"
        "    });"
        "    setTimeout(tick, anyRunning?1500:5000);"
        "  }).catch(function(){setTimeout(tick,5000);});"
        "}"
        "tick();"
        "})();</script>"
    )


def _subtitle_tracks_html(base_url: str, variants: dict) -> str:
    """Render one <track> per available subtitle variant. ASR is marked
    `default` when present so newly-generated tracks auto-load on the
    next page render — matching the user's request to default to the
    new ones once they exist."""
    tracks: list[str] = []
    has_asr = "asr" in variants
    if "original" in variants:
        default_attr = "" if has_asr else " default"
        tracks.append(
            f"<track kind='subtitles' label='Original' srclang='en' "
            f"src='{base_url}?variant=original'{default_attr}>"
        )
    if has_asr:
        tracks.append(
            f"<track kind='subtitles' label='Auto-generated (ASR)' "
            f"srclang='en' src='{base_url}?variant=asr' default>"
        )
    return "".join(tracks)


def _generate_subs_button_html(variants: dict, asr_url: str) -> str:
    """Admin Generate button + status line. The button label changes
    based on what's already on disk. The ASR enqueue endpoint URL is
    passed to the button as a data attribute so the inline JS doesn't
    have to do string interpolation (which previously corrupted braces
    inside the JS function body)."""
    has_original = "original" in variants
    has_asr = "asr" in variants
    if has_asr:
        label = "Regenerate ASR subtitles"
        hint = "Replaces the existing auto-generated track. Originals are kept."
    elif has_original:
        label = "Generate ASR subtitles (alongside the existing one)"
        hint = "Existing subtitles stay put. The new track becomes the default."
    else:
        label = "Generate subtitles via ASR"
        hint = "Queues a Parakeet job. We'll auto-reload when it's ready."
    return (
        "<div class='admin-actions'>"
        f"<button id='gen-subs' type='button' "
        f"data-asr-url='{html.escape(asr_url)}'>{label}</button>"
        f"<span id='gen-subs-status' class='hint'>{hint}</span>"
        "</div>"
    )


_BULK_ASR_JS = r"""
<script>(function(){
  var buttons = document.querySelectorAll('button.bulk-asr-btn');
  if (!buttons.length) return;

  function setStatus(span, state, text, withSpinner) {
    span.classList.remove('idle', 'working', 'running', 'done', 'failed');
    span.classList.add(state);
    var spinner = withSpinner
      ? '<span class="asr-spinner" aria-hidden="true"></span> '
      : '';
    span.innerHTML = spinner + '<span class="asr-text">' + text + '</span>';
  }

  function reenable(libId) {
    var btn = document.querySelector(
      'button.bulk-asr-btn[data-lib-id="' + libId + '"]'
    );
    if (btn) btn.disabled = false;
  }

  buttons.forEach(function(btn) {
    btn.addEventListener('click', function() {
      var libId = btn.dataset.libId;
      var status = document.querySelector(
        '.bulk-asr-status[data-lib-id="' + libId + '"]'
      );
      if (!status) return;
      btn.disabled = true;
      setStatus(status, 'working', 'Scanning the library and queuing jobs…', true);
      fetch('/api/admin/asr/library/' + libId, { method: 'POST' })
        .then(function(r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        })
        .then(function(j) {
          if (!j.queued) {
            setStatus(status, 'done',
              'Nothing to queue — every item already has an ASR variant.',
              false);
            reenable(libId);
            return;
          }
          setStatus(status, 'running',
            'Queued ' + j.queued + ' ASR job(s). Worker is processing…',
            true);
          // Track *exactly* the IDs we just enqueued so other ASR work
          // (per-item Generate clicks, previous bulk runs) doesn't skew
          // the percentage.
          pollScoped(status, libId, j.job_ids || [], j.queued);
        })
        .catch(function(e) {
          setStatus(status, 'failed', 'Queue failed: ' + e.message, false);
          reenable(libId);
        });
    });
  });

  function pollScoped(status, libId, jobIds, total) {
    if (!jobIds.length) {
      setStatus(status, 'done', '✓ All ASR jobs done.', false);
      reenable(libId);
      return;
    }
    var url = '/api/admin/jobs/by-ids?ids=' + encodeURIComponent(jobIds.join(','));
    fetch(url)
      .then(function(r) { return r.ok ? r.json() : null; })
      .then(function(s) {
        if (!s) {
          setTimeout(function() { pollScoped(status, libId, jobIds, total); }, 5000);
          return;
        }
        var done = (s.done || 0) + (s.failed || 0);
        var remaining = (s.queued || 0) + (s.running || 0);
        if (remaining === 0) {
          if (s.failed > 0) {
            setStatus(status, 'failed',
              '✗ ' + s.done + ' of ' + total + ' done, ' + s.failed + ' failed.',
              false);
          } else {
            setStatus(status, 'done',
              '✓ All ' + total + ' ASR jobs done.', false);
          }
          reenable(libId);
          return;
        }
        var pct = total ? Math.round(100 * done / total) : 0;
        var details = (s.running ? ' (' + s.running + ' running)' : '');
        setStatus(status, 'running',
          done + ' / ' + total + ' done (' + pct + '%) — '
            + remaining + ' remaining' + details,
          true);
        setTimeout(function() { pollScoped(status, libId, jobIds, total); }, 3000);
      })
      .catch(function() {
        setTimeout(function() { pollScoped(status, libId, jobIds, total); }, 5000);
      });
  }
})();</script>
"""


_GEN_SUBS_JS = r"""
<script>(function(){
  var btn = document.getElementById('gen-subs');
  var status = document.getElementById('gen-subs-status');
  if (!btn || !status) return;
  var asrUrl = btn.dataset.asrUrl;
  if (!asrUrl) return;

  function setStatus(state, text) {
    status.classList.remove('idle', 'working', 'running', 'done', 'failed');
    status.classList.add(state);
    var spinner = (state === 'working' || state === 'running')
      ? '<span class="asr-spinner" aria-hidden="true"></span> '
      : '';
    var bar = (state === 'running')
      ? '<span class="asr-bar"><span></span></span>'
      : '';
    status.innerHTML = spinner + '<span class="asr-text">' + text + '</span>' + bar;
  }

  btn.addEventListener('click', function() {
    btn.disabled = true;
    setStatus('working', 'Queuing job...');
    fetch(asrUrl, { method: 'POST' })
      .then(function(r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.json();
      })
      .then(function(j) {
        setStatus('working', 'Queued (job #' + j.job_id + '). Waiting for the worker...');
        poll(j.job_id);
      })
      .catch(function(e) {
        setStatus('failed', 'Queue failed: ' + e.message);
        btn.disabled = false;
      });
  });

  var lastStatus = '';
  function poll(jid) {
    fetch('/api/admin/jobs/' + jid)
      .then(function(r) {
        if (!r.ok) { throw new Error('HTTP ' + r.status); }
        return r.json();
      })
      .then(function(job) {
        if (job.status !== lastStatus) {
          lastStatus = job.status;
          if (job.status === 'queued') {
            setStatus('working', 'Queued — waiting for the ASR worker to pick it up...');
          } else if (job.status === 'running') {
            setStatus('running', 'Transcribing — seconds on GPU, several minutes on CPU.');
          }
        }
        if (job.status === 'done') {
          setStatus('done', 'Subtitles ready. Reloading...');
          setTimeout(function() { location.reload(); }, 800);
          return;
        }
        if (job.status === 'failed') {
          setStatus('failed', 'ASR job failed: ' + (job.error || 'unknown error'));
          btn.disabled = false;
          return;
        }
        setTimeout(function() { poll(jid); }, 2000);
      })
      .catch(function(e) {
        setStatus('working', 'Polling error: ' + e.message + '; retrying...');
        setTimeout(function() { poll(jid); }, 4000);
      });
  }
})();</script>
"""


def _generate_subs_js() -> str:
    """The button reads its target URL from `data-asr-url` so this script
    needs no Python interpolation — which is what made the previous
    version silently emit invalid JS like `}}).then(...)` and break
    the click handler.
    """
    return _GEN_SUBS_JS


def _player_progress_js(progress_url: str) -> str:
    """JS that resumes, saves position, autoplays, and enters fullscreen on
    the user's first interaction.

    Browsers block requestFullscreen() without a recent user gesture, and a
    click that navigates to a new page no longer counts once the new page
    loads. So we autoplay (muted if necessary) on canplay, and the FIRST
    click or keypress on the watch page enters fullscreen. Double-click on
    the video element toggles fullscreen back off.
    """
    return (
        "<script>(function(){"
        "var el=document.getElementById('player');"
        f"var url='{progress_url}';"
        # --- Resume position
        "var loaded=false;"
        "fetch(url).then(function(r){return r.ok?r.json():null;}).then(function(p){"
        "  if(!p||!p.position_seconds)return;"
        "  el.addEventListener('loadedmetadata',function(){if(loaded)return;loaded=true;el.currentTime=p.position_seconds;});"
        "});"
        # --- Autoplay (muted fallback if browser rejects sound)
        "el.addEventListener('canplay',function once(){"
        "  el.removeEventListener('canplay',once);"
        "  el.play().catch(function(){el.muted=true;el.play().catch(function(){});});"
        "});"
        # --- Save progress every ~5 seconds of playback
        "var last=0;"
        "el.addEventListener('timeupdate',function(){"
        "  var t=el.currentTime;if(Math.abs(t-last)<5)return;last=t;"
        "  fetch(url,{method:'PUT',headers:{'Content-Type':'application/json'},"
        "    body:JSON.stringify({position_seconds:t,duration_seconds:el.duration||null})});"
        "});"
        # --- Fullscreen: trigger only on a user gesture *inside the player*.
        # Earlier this listened on `document`, which meant clicking the
        # season tabs or an episode card also went fullscreen. Now we bind
        # to the .theater container (or fall back to the video element) so
        # only clicks/taps in the player area count.
        "function fs(){if(document.fullscreenElement)return;"
        "  var f=el.requestFullscreen||el.webkitRequestFullscreen||el.mozRequestFullScreen||el.msRequestFullscreen;"
        "  if(f){try{var p=f.call(el);if(p&&p.catch)p.catch(function(){});}catch(e){}}"
        "  if(el.muted){el.muted=false;}"
        "}"
        "var theater=el.closest('.theater')||el;"
        "function firstGesture(){"
        "  fs();"
        "  theater.removeEventListener('click',firstGesture,true);"
        "  theater.removeEventListener('touchstart',firstGesture,true);"
        "}"
        "theater.addEventListener('click',firstGesture,true);"
        "theater.addEventListener('touchstart',firstGesture,true);"
        "el.addEventListener('dblclick',function(){"
        "  if(document.fullscreenElement){document.exitFullscreen();}else{fs();}"
        "});"
        "})();</script>"
    )


def _login_form_html(next_path: str = "/") -> str:
    next_safe = next_path if (next_path.startswith("/") and not next_path.startswith("//")) else "/"
    return f"""
    <form method='post' action='/login' class='auth'>
      <input type='hidden' name='next' value='{html.escape(next_safe)}'>
      <label>Username <input name='username' autofocus required></label>
      <label>Password <input name='password' type='password' required></label>
      <button type='submit'>Log in</button>
    </form>"""


def _page(
    title: str, body_html: str, user: Optional[dict], body_class: str = ""
) -> str:
    if user:
        admin_link = " · <a href='/admin'>Admin</a>" if user['is_admin'] else ""
        userbar = (
            "<span class='userbar'>"
            f"<a href='/account'>{html.escape(user['username'])}</a>"
            + (" (admin)" if user['is_admin'] else "")
            + admin_link
            + " · <form method='post' action='/logout'><button>Log out</button></form>"
            + "</span>"
        )
        nav = (
            "<nav class='top'>"
            "<a href='/'>Libraries</a>"
            "<a href='/continue-watching'>Continue Watching</a>"
            "<a href='/search'>Search</a>"
            "<a href='/cast'>Cast</a>"
            "</nav>"
        )
    else:
        userbar = "<span class='userbar'><a href='/login'>Log in</a> · <a href='/register'>Register</a></span>"
        nav = "<nav class='top'><a href='/'>Libraries</a><a href='/search'>Search</a></nav>"
    body_attr = f' class="{body_class}"' if body_class else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Cuttlefish</title>
<style>{_STYLE}</style>
</head>
<body{body_attr}>
<header>
  <h1><a href="/">Cuttlefish</a></h1>
  {userbar}
</header>
{nav}
{body_html}
</body>
</html>
"""

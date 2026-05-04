"""FastAPI app: HTML pages + JSON API + media streaming + auth + progress.

Pages are intentionally hand-written HTML strings (no Jinja, no JS framework)
so the same UI works on a smart TV's built-in browser.
"""
from __future__ import annotations

import html
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from cuttlefish import auth, cruft as cruft_mod, db
from cuttlefish.clients.opensubtitles import OpenSubtitles
from cuttlefish.clients.tmdb import TMDb
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
            "SELECT id, name, kind, root_path, created_at FROM libraries ORDER BY id"
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
        _encoded_or_404(media_id)
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (media_id,)
            )
        return {"ok": True, "job_id": cur.lastrowid}

    @app.post("/api/admin/encode/episode/{episode_id}")
    def api_admin_enqueue_episode_encode(episode_id: int, request: Request):
        _require_admin(request)
        conn = _conn()
        if conn.execute("SELECT 1 FROM tv_episodes WHERE id = ?", (episode_id,)).fetchone() is None:
            raise HTTPException(404, "episode not found")
        job_id = encoder.enqueue_episode_encode(conn, episode_id)
        return {"ok": True, "job_id": job_id}

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
  <li><a href='/admin/cruft'>Cruft</a> — non-media files that may be safe to delete</li>
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

    @app.get("/admin/cruft", response_class=HTMLResponse)
    def page_admin_cruft(request: Request):
        user = _require_admin(request)
        conn = _conn()
        libs = conn.execute("SELECT id, name FROM libraries ORDER BY id").fetchall()
        sections = []
        total = 0
        for lib in libs:
            entries = cruft_mod.list_cruft(conn, lib["id"])
            if not entries:
                continue
            rows = "".join(
                f"<tr>"
                f"<td><code>{html.escape(str(e.path))}</code></td>"
                f"<td><span class='size'>{_human_size(e.size_bytes)}</span></td>"
                f"<td>{e.reason}</td>"
                f"<td><form method='post' action='/admin/cruft/delete' "
                f"onsubmit='return confirm(\"Delete {html.escape(e.path.name)}?\");'>"
                f"<input type='hidden' name='path' value='{html.escape(str(e.path))}'>"
                f"<button type='submit'>Delete</button></form></td>"
                f"</tr>"
                for e in entries
            )
            total += len(entries)
            sections.append(
                f"<h3>{html.escape(lib['name'])}</h3>"
                "<table class='admin'><thead><tr>"
                "<th>path</th><th>size</th><th>reason</th><th></th>"
                "</tr></thead><tbody>"
                f"{rows}</tbody></table>"
            )
        if not sections:
            body = (
                "<h2>Cruft</h2><p class='empty'>No cruft found in any library.</p>"
                "<p><a href='/admin'>&larr; Admin</a></p>"
            )
        else:
            body = (
                "<h2>Cruft</h2>"
                f"<p class='hint'>{total} non-media file(s) found across your libraries. "
                "These are files cuttlefish does not know what to do with — typically "
                "<code>downloadedfrom.txt</code>, NFOs, sample files, or orphan "
                "subtitles whose video disappeared. Delete only what you don't want.</p>"
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
        rows = _conn().execute(
            "SELECT id, name, kind, root_path FROM libraries ORDER BY id"
        ).fetchall()
        if not rows:
            body = (
                "<p class='empty'>No libraries yet. Add one with "
                "<code>uv run cuttlefish add-library &lt;name&gt; &lt;path&gt; "
                "--kind movies|tv|audiobooks</code> and then "
                "<code>uv run cuttlefish scan</code>.</p>"
            )
        else:
            items = "".join(
                f"<li><a href='/library/{r['id']}'>{html.escape(r['name'])}</a> "
                f"<span class='kind'>{r['kind']}</span></li>"
                for r in rows
            )
            body = f"<ul class='libraries'>{items}</ul>"
        return _page("Libraries", body, user=user)

    @app.get("/library/{library_id}", response_class=HTMLResponse)
    def page_library(library_id: int, request: Request):
        user = _current_user(request)
        conn = _conn()
        lib = conn.execute(
            "SELECT id, name, kind FROM libraries WHERE id = ?", (library_id,)
        ).fetchone()
        if not lib:
            raise HTTPException(404, "library not found")
        media = conn.execute(
            "SELECT id, kind, title_guess FROM media WHERE library_id = ? "
            "ORDER BY title_guess",
            (library_id,),
        ).fetchall()
        if not media:
            body = "<p class='empty'>No media yet. Run <code>uv run cuttlefish scan</code>.</p>"
        else:
            items = "".join(
                f"<li><a href='{_watch_url(m['kind'], m['id'])}'>{html.escape(m['title_guess'])}</a></li>"
                for m in media
            )
            body = f"<ul class='media'>{items}</ul>"
        return _page(f"{lib['name']} ({lib['kind']})", body, user=user)

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
        body = (
            f"<h2>{title}</h2>"
            f"<video id='player' controls preload='metadata' src='/stream/{media_id}'>"
            "Your browser does not support the video element.</video>"
            "<p><a href='/'>&larr; Libraries</a></p>"
            + _player_progress_js(f"/api/progress/{media_id}")
        )
        return _page(title, body, user=user)

    @app.get("/show/{show_id}", response_class=HTMLResponse)
    def page_show(show_id: int, request: Request):
        user = _current_user(request)
        conn = _conn()
        show = conn.execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (show_id,)
        ).fetchone()
        if not show or show["kind"] != "tv_show":
            raise HTTPException(404, "show not found")
        eps = conn.execute(
            "SELECT id, season, episode, title_guess FROM tv_episodes "
            "WHERE show_id = ? ORDER BY season, episode, id",
            (show_id,),
        ).fetchall()
        title = html.escape(show["title_guess"])
        if not eps:
            body = f"<h2>{title}</h2><p class='empty'>No episodes scanned yet.</p>"
        else:
            seasons: dict[int, list] = {}
            for e in eps:
                seasons.setdefault(e["season"], []).append(e)
            sections = []
            for season, items in sorted(seasons.items()):
                lis = "".join(
                    f"<li><a href='/watch/episode/{e['id']}'>"
                    f"S{e['season']:02d}E{e['episode']:02d} &mdash; {html.escape(e['title_guess'] or '(untitled)')}"
                    "</a></li>"
                    for e in items
                )
                sections.append(f"<h3>Season {season}</h3><ul class='episodes'>{lis}</ul>")
            body = f"<h2>{title}</h2>" + "".join(sections) + "<p><a href='/'>&larr; Libraries</a></p>"
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
        ep_label = f"S{row['season']:02d}E{row['episode']:02d}"
        title = f"{row['show_title']} {ep_label}"
        body = (
            f"<h2>{html.escape(row['show_title'])} &mdash; {ep_label}</h2>"
            f"<p>{html.escape(row['title_guess'] or '')}</p>"
            f"<video id='player' controls preload='metadata' src='/stream/episode/{episode_id}'>"
            "Your browser does not support the video element.</video>"
            f"<p><a href='/show/{row['show_id']}'>&larr; All episodes</a></p>"
            + _player_progress_js(f"/api/progress/episode/{episode_id}")
        )
        return _page(title, body, user=user)

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
  // Resume + save progress
  fetch('/api/progress/book/' + book_id).then(function(r){{return r.ok?r.json():null;}}).then(function(p){{
    if(!p||!p.current_track_id){{ load(0); return; }}
    var i = playlist.findIndex(function(t){{return t.id===p.current_track_id;}});
    if(i<0) i = 0;
    load(i);
    el.addEventListener('loadedmetadata', function once(){{
      el.removeEventListener('loadedmetadata', once);
      el.currentTime = p.position_seconds || 0;
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
    def page_login(request: Request):
        user = _current_user(request)
        if user:
            return RedirectResponse("/", status_code=303)
        body = """
        <form method='post' action='/login' class='auth'>
          <h2>Log in</h2>
          <label>Username <input name='username' autofocus required></label>
          <label>Password <input name='password' type='password' required></label>
          <button type='submit'>Log in</button>
        </form>"""
        return _page("Log in", body, user=None)

    @app.post("/login")
    def page_login_submit(
        username: str = Form(...), password: str = Form(...)
    ):
        conn = _conn()
        user_id = auth.authenticate(conn, username, password)
        if user_id is None:
            return _page(
                "Log in",
                "<p class='error'>Invalid credentials.</p>"
                + _login_form_html(),
                user=None,
            )
        token, expires = auth.create_session(conn, user_id)
        resp = RedirectResponse("/", status_code=303)
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
        if not first_user and (not user or not user["is_admin"]):
            raise HTTPException(403, "registration is admin-only after the first user")
        admin_note = (
            "<p class='hint'>You'll be the first user, so you'll be the admin.</p>"
            if first_user
            else ""
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

    @app.post("/logout")
    def page_logout(request: Request):
        token = request.cookies.get(auth.SESSION_COOKIE_NAME)
        if token:
            auth.delete_session(_conn(), token)
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
        return resp

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
"""


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


def _watch_url(kind: str, media_id: int) -> str:
    if kind == "tv_show":
        return f"/show/{media_id}"
    if kind == "audiobook":
        return f"/book/{media_id}"
    return f"/watch/{media_id}"


def _player_progress_js(progress_url: str) -> str:
    """JS that resumes + saves position via the given progress endpoint."""
    return (
        "<script>(function(){"
        "var el=document.getElementById('player');"
        f"var url='{progress_url}';"
        "var loaded=false;"
        "fetch(url).then(function(r){return r.ok?r.json():null;}).then(function(p){"
        "  if(!p||!p.position_seconds)return;"
        "  el.addEventListener('loadedmetadata',function(){if(loaded)return;loaded=true;el.currentTime=p.position_seconds;});"
        "});"
        "var last=0;"
        "el.addEventListener('timeupdate',function(){"
        "  var t=el.currentTime;if(Math.abs(t-last)<5)return;last=t;"
        "  fetch(url,{method:'PUT',headers:{'Content-Type':'application/json'},"
        "    body:JSON.stringify({position_seconds:t,duration_seconds:el.duration||null})});"
        "});})();</script>"
    )


def _login_form_html() -> str:
    return """
    <form method='post' action='/login' class='auth'>
      <label>Username <input name='username' autofocus required></label>
      <label>Password <input name='password' type='password' required></label>
      <button type='submit'>Log in</button>
    </form>"""


def _page(title: str, body_html: str, user: Optional[dict]) -> str:
    if user:
        admin_link = " · <a href='/admin'>Admin</a>" if user['is_admin'] else ""
        userbar = (
            f"<span class='userbar'>{html.escape(user['username'])}"
            + (" (admin)" if user['is_admin'] else "")
            + admin_link
            + " · <form method='post' action='/logout'><button>Log out</button></form>"
            + "</span>"
        )
    else:
        userbar = "<span class='userbar'><a href='/login'>Log in</a> · <a href='/register'>Register</a></span>"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Cuttlefish</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
  <h1><a href="/">Cuttlefish</a></h1>
  {userbar}
</header>
{body_html}
</body>
</html>
"""

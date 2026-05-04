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

from cuttlefish import auth, db
from cuttlefish.server.streaming import stream_file, video_path_for_media


# --- request/response models ----------------------------------------------


class ProgressBody(BaseModel):
    position_seconds: float = Field(..., ge=0)
    duration_seconds: Optional[float] = Field(None, ge=0)


# --- app factory ----------------------------------------------------------


def create_app(db_path: Optional[Path | str] = None) -> FastAPI:
    app = FastAPI(title="Cuttlefish", version="0.0.0", docs_url="/api/docs")

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
        row = _conn().execute(
            "SELECT source_path FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        path = video_path_for_media(Path(row["source_path"]))
        return stream_file(path, request)

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
            "SELECT id, title_guess FROM media WHERE library_id = ? "
            "ORDER BY title_guess",
            (library_id,),
        ).fetchall()
        if not media:
            body = "<p class='empty'>No media yet. Run <code>uv run cuttlefish scan</code>.</p>"
        else:
            items = "".join(
                f"<li><a href='/watch/{m['id']}'>{html.escape(m['title_guess'])}</a></li>"
                for m in media
            )
            body = f"<ul class='media'>{items}</ul>"
        return _page(f"{lib['name']} ({lib['kind']})", body, user=user)

    @app.get("/watch/{media_id}", response_class=HTMLResponse)
    def page_watch(media_id: int, request: Request):
        user = _current_user(request)
        row = _conn().execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        title = html.escape(row["title_guess"])
        tag = "audio" if row["kind"] == "audiobook" else "video"
        # Embed a tiny progress-tracker that resumes + saves position when the
        # viewer is logged in. If not logged in, the JS below silently no-ops.
        progress_js = (
            "<script>(function(){"
            f"var el=document.getElementById('player');var mid={media_id};"
            "var loaded=false;"
            "fetch('/api/progress/'+mid).then(function(r){if(!r.ok)return null;return r.json();}).then(function(p){"
            "  if(!p||!p.position_seconds)return;"
            "  el.addEventListener('loadedmetadata',function(){if(loaded)return;loaded=true;el.currentTime=p.position_seconds;});"
            "});"
            "var last=0;"
            "el.addEventListener('timeupdate',function(){"
            "  var t=el.currentTime;if(Math.abs(t-last)<5)return;last=t;"
            "  fetch('/api/progress/'+mid,{method:'PUT',headers:{'Content-Type':'application/json'},"
            "    body:JSON.stringify({position_seconds:t,duration_seconds:el.duration||null})});"
            "});})();</script>"
        )
        body = (
            f"<h2>{title}</h2>"
            f"<{tag} id='player' controls preload='metadata' src='/stream/{media_id}'>"
            f"Your browser does not support the {tag} element.</{tag}>"
            "<p><a href='/'>&larr; Libraries</a></p>"
            + progress_js
        )
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
ul.libraries, ul.media { list-style: none; padding: 0; }
ul.libraries li, ul.media li { padding: .5rem 0; border-bottom: 1px solid #333; }
.kind { color: #888; font-size: .8em; margin-left: .5rem; }
video, audio { width: 100%; max-height: 70vh; background: #000; }
.empty, .hint { color: #888; }
.error { color: #f66; }
code { background: #222; padding: .15em .4em; border-radius: 3px; }
form.auth { display: grid; gap: .75rem; max-width: 320px; }
form.auth label { display: grid; gap: .25rem; font-size: .9em; color: #aaa; }
form.auth input { padding: .4rem .5rem; background: #222; color: #eee;
                   border: 1px solid #333; border-radius: 3px; font: inherit; }
form.auth button { padding: .5rem; background: #245; color: #eee;
                    border: 1px solid #468; border-radius: 3px; cursor: pointer; }
"""


def _login_form_html() -> str:
    return """
    <form method='post' action='/login' class='auth'>
      <label>Username <input name='username' autofocus required></label>
      <label>Password <input name='password' type='password' required></label>
      <button type='submit'>Log in</button>
    </form>"""


def _page(title: str, body_html: str, user: Optional[dict]) -> str:
    if user:
        userbar = (
            f"<span class='userbar'>{html.escape(user['username'])}"
            + (" (admin)" if user['is_admin'] else "")
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

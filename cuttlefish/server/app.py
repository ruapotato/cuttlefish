"""FastAPI app: HTML pages + JSON API + media streaming.

Pages are intentionally hand-written HTML strings (no Jinja, no JS framework)
so the same UI works on a smart TV's built-in browser.
"""
from __future__ import annotations

import html
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from cuttlefish import db
from cuttlefish.server.streaming import stream_file, video_path_for_media


def create_app(db_path: Optional[Path | str] = None) -> FastAPI:
    app = FastAPI(title="Cuttlefish", version="0.0.0", docs_url="/api/docs")

    def _conn():
        return db.connect(db_path)

    # --- JSON API ---------------------------------------------------------

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

    # --- Streaming --------------------------------------------------------

    @app.get("/stream/{media_id}")
    def stream(media_id: int, request: Request):
        row = _conn().execute(
            "SELECT source_path FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        path = video_path_for_media(Path(row["source_path"]))
        return stream_file(path, request)

    # --- HTML pages -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_index():
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
        return _page("Libraries", body)

    @app.get("/library/{library_id}", response_class=HTMLResponse)
    def page_library(library_id: int):
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
        return _page(f"{lib['name']} ({lib['kind']})", body)

    @app.get("/watch/{media_id}", response_class=HTMLResponse)
    def page_watch(media_id: int):
        row = _conn().execute(
            "SELECT id, title_guess, kind FROM media WHERE id = ?", (media_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "media not found")
        title = html.escape(row["title_guess"])
        if row["kind"] == "audiobook":
            player = (
                f"<audio controls preload='metadata' src='/stream/{media_id}'>"
                "Your browser does not support the audio element.</audio>"
            )
        else:
            player = (
                f"<video controls preload='metadata' src='/stream/{media_id}'>"
                "Your browser does not support the video element.</video>"
            )
        body = f"<h2>{title}</h2>{player}<p><a href='/'>&larr; Libraries</a></p>"
        return _page(title, body)

    return app


_STYLE = """
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto;
       padding: 0 1rem; background: #111; color: #eee; }
a { color: #6cf; text-decoration: none; }
a:hover { text-decoration: underline; }
h1 a { color: inherit; }
ul.libraries, ul.media { list-style: none; padding: 0; }
ul.libraries li, ul.media li { padding: .5rem 0; border-bottom: 1px solid #333; }
.kind { color: #888; font-size: .8em; margin-left: .5rem; }
video, audio { width: 100%; max-height: 70vh; background: #000; }
.empty { color: #888; }
code { background: #222; padding: .15em .4em; border-radius: 3px; }
"""


def _page(title: str, body_html: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Cuttlefish</title>
<style>{_STYLE}</style>
</head>
<body>
<h1><a href="/">Cuttlefish</a></h1>
{body_html}
</body>
</html>
"""

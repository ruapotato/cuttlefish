"""External-API client tests using httpx.MockTransport — no network."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cuttlefish.clients.opensubtitles import OpenSubtitles
from cuttlefish.clients.tmdb import TMDb
from cuttlefish.workers import asr


# --- TMDb ----------------------------------------------------------------


def test_tmdb_unconfigured_returns_empty(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    client = TMDb()
    assert client.configured is False
    assert client.search_movie("Big Buck Bunny") == []
    assert client.search_tv("Show") == []
    assert client.poster_url(None) is None


def test_tmdb_search_movie(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/3/search/movie"
        assert request.url.params["query"] == "Big Buck Bunny"
        assert request.headers["authorization"] == "Bearer test-key"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"id": 10378, "title": "Big Buck Bunny", "poster_path": "/abc.jpg"}
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = TMDb(api_key="test-key", client=httpx.Client(transport=transport))
    results = client.search_movie("Big Buck Bunny")
    assert len(results) == 1
    assert results[0]["id"] == 10378


def test_tmdb_download_poster(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if "image.tmdb.org" in str(request.url):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0fakejpg")
        raise AssertionError(f"unexpected {request.url}")

    client = TMDb(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    dest = tmp_path / "out" / "poster.jpg"
    result = client.download_poster("/abc.jpg", dest)
    assert result == dest
    assert dest.read_bytes().startswith(b"\xff\xd8\xff\xe0")


def test_tmdb_poster_url_short_circuits_on_none():
    client = TMDb(api_key="x")
    assert client.poster_url(None) is None
    assert client.poster_url("/x.jpg") == "https://image.tmdb.org/t/p/w500/x.jpg"


# --- OpenSubtitles -------------------------------------------------------


def test_opensubtitles_unconfigured(monkeypatch):
    monkeypatch.delenv("OPENSUBTITLES_API_KEY", raising=False)
    client = OpenSubtitles()
    assert client.configured is False
    assert client.can_download is False
    assert client.search("anything") == []


def test_opensubtitles_search_with_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/subtitles")
        assert request.headers["api-key"] == "key"
        return httpx.Response(
            200,
            json={"data": [{"id": "1", "attributes": {"files": [{"file_id": 42}]}}]},
        )

    client = OpenSubtitles(
        api_key="key",
        client=httpx.Client(
            transport=httpx.MockTransport(handler), base_url="https://api.opensubtitles.com/api/v1"
        ),
    )
    results = client.search("Big Buck Bunny")
    assert results[0]["attributes"]["files"][0]["file_id"] == 42


def test_opensubtitles_can_download_requires_creds():
    a = OpenSubtitles(api_key="k")
    assert a.can_download is False
    b = OpenSubtitles(api_key="k", username="u", password="p")
    assert b.can_download is True


# --- ASR helpers ----------------------------------------------------------


def test_asr_format_ts():
    assert asr._format_ts(0) == "00:00:00,000"
    assert asr._format_ts(3661.5) == "01:01:01,500"
    assert asr._format_ts(-5) == "00:00:00,000"


def test_asr_words_to_cues_breaks_on_long_pause():
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.5, "end": 1.0},
        {"word": "next", "start": 5.0, "end": 5.5},  # > 1.0s gap
    ]
    cues = asr.words_to_cues(words, max_gap=1.0, max_chars=200)
    assert len(cues) == 2
    assert cues[0].text == "Hello world"
    assert cues[1].text == "next"


def test_asr_words_to_cues_breaks_on_long_line():
    words = [{"word": "a" * 50, "start": 0.0, "end": 0.5} for _ in range(3)]
    cues = asr.words_to_cues(words, max_chars=80)
    assert len(cues) >= 2  # split happened


def test_asr_cues_to_srt_format():
    cues = [asr.SrtCue(1, 0.0, 1.5, "Hello"), asr.SrtCue(2, 2.0, 3.0, "World")]
    out = asr.cues_to_srt(cues)
    assert out.startswith("1\n00:00:00,000 --> 00:00:01,500\nHello\n\n2\n")


def test_asr_transcribe_raises_when_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(asr, "is_available", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        asr.transcribe_to_srt(tmp_path / "nope.mp4", tmp_path / "out.srt")


# --- admin endpoint integration ------------------------------------------


@pytest.fixture
def encoded_db_with_admin(tmp_path: Path):
    """Build a small library, register an admin, encode the one item, return
    (TestClient, db_path, media_id)."""
    import shutil
    import subprocess

    from fastapi.testclient import TestClient

    from cuttlefish import db, scanner
    from cuttlefish.server import create_app
    from cuttlefish.workers import encoder

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")

    root = tmp_path / "movies"
    root.mkdir()
    src = root / "Tiny.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES (?, ?, ?)",
            ("movies", "movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root, "movies")
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    encoder.encode_media(conn, media_id, ffmpeg=ffmpeg)
    return db_path, media_id


def _admin_client(db_path, tmdb=None, opensubs=None):
    from fastapi.testclient import TestClient

    from cuttlefish.server import create_app

    app = create_app(db_path=db_path, tmdb_client=tmdb, opensubtitles_client=opensubs)
    client = TestClient(app)
    client.post("/api/auth/register", data={"username": "admin", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "admin", "password": "secret123"})
    return client


def test_admin_metadata_503_when_tmdb_unconfigured(encoded_db_with_admin):
    db_path, media_id = encoded_db_with_admin
    client = _admin_client(db_path, tmdb=TMDb(api_key=None))
    r = client.post(f"/api/admin/metadata/{media_id}")
    assert r.status_code == 503


def test_admin_metadata_downloads_poster(encoded_db_with_admin):
    db_path, media_id = encoded_db_with_admin

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/movie"):
            return httpx.Response(
                200,
                json={"results": [{"id": 1, "title": "Tiny", "poster_path": "/p.jpg"}]},
            )
        if "image.tmdb.org" in str(request.url):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0test")
        raise AssertionError(f"unexpected {request.url}")

    tmdb = TMDb(
        api_key="key",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    client = _admin_client(db_path, tmdb=tmdb)
    r = client.post(f"/api/admin/metadata/{media_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is True
    assert body["poster_path"] is not None
    assert Path(body["poster_path"]).is_file()


def test_admin_subtitle_503_when_unconfigured(encoded_db_with_admin):
    db_path, media_id = encoded_db_with_admin
    client = _admin_client(db_path, opensubs=OpenSubtitles(api_key=None))
    r = client.post(f"/api/admin/subtitle/{media_id}")
    assert r.status_code == 503


def test_admin_subtitle_503_when_no_creds_for_download(encoded_db_with_admin):
    db_path, media_id = encoded_db_with_admin

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/subtitles"):
            return httpx.Response(
                200,
                json={"data": [{"attributes": {"files": [{"file_id": 99}]}}]},
            )
        raise AssertionError(f"unexpected {request.url}")

    opensubs = OpenSubtitles(
        api_key="k",
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.opensubtitles.com/api/v1",
        ),
    )
    client = _admin_client(db_path, opensubs=opensubs)
    r = client.post(f"/api/admin/subtitle/{media_id}")
    # Search succeeded, but download requires creds we don't have.
    assert r.status_code == 503


def test_admin_asr_enqueues_job(encoded_db_with_admin):
    from cuttlefish import db

    db_path, media_id = encoded_db_with_admin
    client = _admin_client(db_path)
    r = client.post(f"/api/admin/asr/{media_id}")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    conn = db.connect(db_path)
    job = conn.execute(
        "SELECT kind, status FROM jobs WHERE media_id = ? ORDER BY id DESC LIMIT 1",
        (media_id,),
    ).fetchone()
    assert job["kind"] == "asr"
    assert job["status"] == "queued"


def test_admin_metadata_409_without_encoded(tmp_path: Path):
    """Without an encoded version, metadata fetch refuses with 409."""
    from cuttlefish import db, scanner

    root = tmp_path / "movies"
    root.mkdir()
    (root / "Foo.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, kind, root_path) VALUES (?, ?, ?)",
            ("m", "movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root, "movies")
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]

    def handler(req):
        return httpx.Response(200, json={"results": []})

    tmdb = TMDb(api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client = _admin_client(db_path, tmdb=tmdb)
    r = client.post(f"/api/admin/metadata/{media_id}")
    assert r.status_code == 409

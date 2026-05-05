"""Server integration tests using FastAPI's TestClient."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner
from cuttlefish.server import create_app


@pytest.fixture
def populated_db(tmp_path: Path):
    """A DB with one movies library and two synthetic 'video' files in it."""
    root = tmp_path / "movies"
    root.mkdir()
    # Use small distinct contents so we can verify range slicing.
    (root / "alpha.mp4").write_bytes(b"A" * 1000)
    (root / "beta.mp4").write_bytes(b"B" * 500)
    db_path = tmp_path / "test.db"
    conn = db.connect(db_path)
    db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            ("movies", str(root)),
        )
        lib_id = cur.lastrowid
    scanner.scan_library(conn, lib_id, root)
    return db_path, root


@pytest.fixture
def client(populated_db):
    db_path, _ = populated_db
    app = create_app(db_path=db_path)
    return TestClient(app)


def test_api_libraries(client):
    r = client.get("/api/libraries")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["name"] == "movies"
    assert "kind" not in data[0]
    assert "root_path" in data[0]


def test_api_media_lists_all(client):
    r = client.get("/api/media")
    assert r.status_code == 200
    titles = sorted(m["title_guess"] for m in r.json())
    assert titles == ["alpha", "beta"]


def test_api_media_filters_by_library(client):
    assert client.get("/api/media?library=movies").status_code == 200
    assert client.get("/api/media?library=nope").json() == []


def test_api_media_one(client):
    r = client.get("/api/media/1")
    assert r.status_code == 200
    assert r.json()["title_guess"] in ("alpha", "beta")


def test_api_media_one_404(client):
    assert client.get("/api/media/999").status_code == 404


def test_html_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Cuttlefish" in r.text
    assert "movies" in r.text


def test_html_library(client):
    r = client.get("/library/1")
    assert r.status_code == 200
    assert "alpha" in r.text and "beta" in r.text


def test_html_watch_video(client):
    r = client.get("/watch/1")
    assert r.status_code == 200
    assert "<video" in r.text
    assert "/stream/1" in r.text


def test_stream_full_file(client):
    r = client.get("/stream/1")
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    # Body is one of our synthetic files.
    assert r.content in (b"A" * 1000, b"B" * 500)


def test_stream_range_returns_206_with_correct_slice(client):
    # alpha.mp4 = b"A" * 1000 (assuming it sorts first by id; check both).
    # Just request bytes 100-199 of media id 1 and verify length + slice.
    r = client.get("/stream/1", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-length"] == "100"
    assert "/100" in r.headers["content-range"] or "/500" in r.headers["content-range"]
    assert len(r.content) == 100
    # Body should be 100 copies of either A or B (whichever id 1 ended up being).
    assert r.content == b"A" * 100 or r.content == b"B" * 100


def test_stream_open_ended_range(client):
    # "bytes=0-" should return the whole file with 206.
    r = client.get("/stream/1", headers={"Range": "bytes=0-"})
    assert r.status_code == 206
    # Length matches one of our two files.
    assert int(r.headers["content-length"]) in (1000, 500)


def test_stream_range_past_end_returns_416(client):
    r = client.get("/stream/1", headers={"Range": "bytes=99999-"})
    assert r.status_code == 416


def test_stream_404(client):
    assert client.get("/stream/999").status_code == 404

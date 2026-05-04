"""Tests for the TOML config loader and the web-based library management
endpoints + admin page."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, config
from cuttlefish.server import create_app


# --- Config.load --------------------------------------------------------


def _write_toml(p: Path, text: str) -> Path:
    p.write_text(text)
    return p


def test_load_minimal_config(tmp_path: Path):
    c = _write_toml(tmp_path / "c.toml", 'db = "/tmp/cf.db"\n')
    cfg = config.Config.load(c)
    assert cfg.db == Path("/tmp/cf.db")
    assert cfg.libraries == []
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8000


def test_load_server_overrides(tmp_path: Path):
    c = _write_toml(tmp_path / "c.toml", """
[server]
host = "0.0.0.0"
port = 9000
with_worker = true
""")
    cfg = config.Config.load(c)
    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9000
    assert cfg.server.with_worker is True


def test_load_libraries(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    tv = tmp_path / "TV"; tv.mkdir()
    c = _write_toml(tmp_path / "c.toml", f"""
[[library]]
name = "Movies"
kind = "movies"
root = "{movies}"

[[library]]
name = "TV"
kind = "tv"
root = "{tv}"
""")
    cfg = config.Config.load(c)
    assert len(cfg.libraries) == 2
    assert cfg.libraries[0].name == "Movies"
    assert cfg.libraries[1].kind == "tv"


def test_load_rejects_invalid_kind(tmp_path: Path):
    c = _write_toml(tmp_path / "c.toml", """
[[library]]
name = "Foo"
kind = "bogus"
root = "/tmp"
""")
    with pytest.raises(ValueError, match="invalid library kind"):
        config.Config.load(c)


def test_load_rejects_missing_fields(tmp_path: Path):
    c = _write_toml(tmp_path / "c.toml", """
[[library]]
name = "Foo"
""")
    with pytest.raises(ValueError, match="missing"):
        config.Config.load(c)


def test_load_or_die_on_missing_file(tmp_path: Path):
    with pytest.raises(SystemExit) as excinfo:
        config.load_or_die(tmp_path / "nope.toml")
    assert excinfo.value.code == 2


def test_apply_libraries_inserts_and_updates(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    cfg = config.Config(
        libraries=[
            config.LibraryEntry(name="Movies", kind="movies", root=movies),
        ]
    )
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    added, updated = cfg.apply_libraries(conn)
    assert (added, updated) == (1, 0)
    # Re-apply: should update, not add
    added, updated = cfg.apply_libraries(conn)
    assert (added, updated) == (0, 1)


# --- web library management --------------------------------------------


def _admin_client(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client, db_path


def test_api_create_library(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, db_path = _admin_client(tmp_path)
    r = client.post(
        "/api/admin/libraries",
        json={"name": "Movies", "kind": "movies", "root_path": str(movies)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] > 0
    rows = db.connect(db_path).execute("SELECT name, kind FROM libraries").fetchall()
    assert rows[0]["name"] == "Movies"


def test_api_create_library_rejects_invalid_kind(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, _ = _admin_client(tmp_path)
    r = client.post(
        "/api/admin/libraries",
        json={"name": "X", "kind": "bogus", "root_path": str(movies)},
    )
    assert r.status_code == 400


def test_api_create_library_rejects_missing_root(tmp_path: Path):
    client, _ = _admin_client(tmp_path)
    r = client.post(
        "/api/admin/libraries",
        json={"name": "X", "kind": "movies", "root_path": str(tmp_path / "nope")},
    )
    assert r.status_code == 400


def test_api_create_library_rejects_dup_name(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, _ = _admin_client(tmp_path)
    client.post("/api/admin/libraries",
                json={"name": "M", "kind": "movies", "root_path": str(movies)})
    other = tmp_path / "Other"; other.mkdir()
    r = client.post("/api/admin/libraries",
                    json={"name": "M", "kind": "movies", "root_path": str(other)})
    assert r.status_code == 409


def test_api_delete_library(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, db_path = _admin_client(tmp_path)
    r = client.post("/api/admin/libraries",
                    json={"name": "M", "kind": "movies", "root_path": str(movies)})
    lib_id = r.json()["id"]
    assert client.delete(f"/api/admin/libraries/{lib_id}").status_code == 200
    assert client.delete(f"/api/admin/libraries/{lib_id}").status_code == 404


def test_api_scan_one_and_all(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    (movies / "A.mp4").write_bytes(b"")
    client, db_path = _admin_client(tmp_path)
    r = client.post("/api/admin/libraries",
                    json={"name": "M", "kind": "movies", "root_path": str(movies)})
    lib_id = r.json()["id"]
    r = client.post(f"/api/admin/scan/{lib_id}")
    assert r.status_code == 200
    assert r.json()["movies_added"] == 1
    r = client.post("/api/admin/scan")
    assert r.status_code == 200
    assert r.json()["scanned_libraries"] == 1


def test_admin_libraries_html_page(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, _ = _admin_client(tmp_path)
    client.post("/api/admin/libraries",
                json={"name": "M", "kind": "movies", "root_path": str(movies)})
    r = client.get("/admin/libraries")
    assert r.status_code == 200
    assert "Add a library" in r.text
    assert "Scan all libraries" in r.text


def test_admin_libraries_form_add(tmp_path: Path):
    movies = tmp_path / "Movies"; movies.mkdir()
    client, db_path = _admin_client(tmp_path)
    r = client.post(
        "/admin/libraries",
        data={"name": "M", "kind": "movies", "root_path": str(movies)},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/libraries"
    assert db.connect(db_path).execute("SELECT COUNT(*) FROM libraries").fetchone()[0] == 1


def test_admin_libraries_endpoints_require_admin(tmp_path: Path):
    db_path = tmp_path / "t.db"
    db.init_schema(db.connect(db_path))
    client = TestClient(create_app(db_path=db_path))
    assert client.get("/admin/libraries").status_code == 401
    assert client.post("/api/admin/libraries", json={"name":"x","kind":"movies","root_path":"/"}).status_code == 401

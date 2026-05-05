"""Scanner tests build a synthetic filesystem under tmp_path."""
from __future__ import annotations

from pathlib import Path

import pytest

from cuttlefish import db, scanner


def _new_db(tmp_path: Path):
    conn = db.connect(tmp_path / "test.db")
    db.init_schema(conn)
    return conn


def _add_library(conn, name: str, root: Path) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES (?, ?)",
            (name, str(root)),
        )
    return cur.lastrowid


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")


def test_scan_movies_loose_files(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    _touch(root / "Big Buck Bunny-YE7VzlLtp-4.mp4")
    _touch(root / "Sintel-eRsGyueVLvQ.webm")
    _touch(root / "downloadedfrom.txt")  # cruft, must be ignored
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.movies_added == 2
    assert result.skipped == 1  # the cruft
    titles = sorted(
        r["title_guess"] for r in conn.execute("SELECT title_guess FROM media").fetchall()
    )
    assert titles == ["Big Buck Bunny", "Sintel"]


def test_scan_movies_clean_layout(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    bbb = root / "Big Buck Bunny"
    bbb.mkdir()
    _touch(bbb / "Big Buck Bunny.mp4")
    _touch(bbb / "Big Buck Bunny.jpg")
    _touch(bbb / "Big Buck Bunny.srt")
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.movies_added == 1
    row = conn.execute("SELECT title_guess, source_path FROM media").fetchone()
    assert row["title_guess"] == "Big Buck Bunny"
    assert row["source_path"] == str(bbb)


def test_scan_tv_supports_multiple_season_formats(tmp_path: Path):
    root = tmp_path / "tv"
    root.mkdir()
    show = root / "My Show"
    s01 = show / "Season 01"
    s2 = show / "S2"
    s3 = show / "Season 3"
    _touch(s01 / "My Show - S01E01 - Pilot.mp4")
    _touch(s01 / "My Show - S01E02 - Second.mp4")
    _touch(s2 / "My Show - S02E01 - Return.mp4")
    _touch(s3 / "ep one.mkv")  # no SxxExx marker — episode_num falls back to 0
    _touch(show / "specials" / "behind the scenes.mp4")  # treated as another season
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.shows_added == 1
    # Every subfolder of a TV show is a season — Season 01 / S2 / Season 3 /
    # specials. Specials gets a fallback season number.
    assert result.episodes_added == 5
    seasons = sorted(
        r["season"] for r in conn.execute("SELECT season FROM tv_episodes").fetchall()
    )
    assert seasons == [1, 1, 1, 2, 3]  # 'specials' falls back to season 1


def test_scan_audiobooks_arbitrary_depth(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    _touch(root / "Some Book" / "01.mp3")
    _touch(root / "Some Book" / "02.mp3")
    _touch(root / "Some Series" / "Book 1" / "01.mp3")
    _touch(root / "Some Series" / "Book 2" / "01.mp3")
    _touch(root / "Some Series" / "Book 2" / "02.mp3")
    _touch(root / "Some Author" / "Trilogy" / "Book A" / "01.m4a")
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.audiobooks_added == 4  # Some Book, Book 1, Book 2, Book A
    assert result.tracks_added == 6
    titles = sorted(
        r["title_guess"] for r in conn.execute("SELECT title_guess FROM media").fetchall()
    )
    assert titles == ["Book 1", "Book 2", "Book A", "Some Book"]


def test_track_order_is_preserved_lexicographically(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    _touch(root / "Book" / "03.mp3")
    _touch(root / "Book" / "01.mp3")
    _touch(root / "Book" / "02.mp3")
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    scanner.scan_library(conn, lib_id, root)
    rows = conn.execute(
        "SELECT order_index, source_path FROM audiobook_tracks ORDER BY order_index"
    ).fetchall()
    assert [Path(r["source_path"]).name for r in rows] == ["01.mp3", "02.mp3", "03.mp3"]


def test_idempotent_rescan(tmp_path: Path):
    root = tmp_path / "movies"
    root.mkdir()
    _touch(root / "Movie One.mp4")
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    scanner.scan_library(conn, lib_id, root)
    scanner.scan_library(conn, lib_id, root)
    count = conn.execute("SELECT COUNT(*) AS c FROM media").fetchone()["c"]
    assert count == 1


def test_missing_root_raises(tmp_path: Path):
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", tmp_path)
    with pytest.raises(ValueError):
        scanner.scan_library(conn, lib_id, tmp_path / "nope")


def test_mixed_library_classifies_per_subfolder(tmp_path: Path):
    """A single library can hold movies, TV shows, and audiobooks side by side."""
    root = tmp_path / "media"
    root.mkdir()
    # Loose movie at root
    _touch(root / "Loose Movie.mp4")
    # Movie folder
    _touch(root / "Some Movie" / "Some Movie.mp4")
    # TV show
    _touch(root / "My Show" / "Season 01" / "My Show - S01E01.mp4")
    _touch(root / "My Show" / "Season 01" / "My Show - S01E02.mp4")
    # Audiobook
    _touch(root / "An Audiobook" / "01.mp3")
    _touch(root / "An Audiobook" / "02.mp3")
    # Audiobook series
    _touch(root / "A Series" / "Book 1" / "01.mp3")
    _touch(root / "A Series" / "Book 2" / "01.mp3")

    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "media", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.movies_added == 2  # loose + folder
    assert result.shows_added == 1
    assert result.episodes_added == 2
    assert result.audiobooks_added == 3  # An Audiobook + Book 1 + Book 2

    rows = conn.execute("SELECT kind, title_guess FROM media ORDER BY kind, title_guess").fetchall()
    pairs = [(r["kind"], r["title_guess"]) for r in rows]
    assert pairs == [
        ("audiobook", "An Audiobook"),
        ("audiobook", "Book 1"),
        ("audiobook", "Book 2"),
        ("movie", "Loose Movie"),
        ("movie", "Some Movie"),
        ("tv_show", "My Show"),
    ]


def test_classify_folder_basics(tmp_path: Path):
    """Direct unit test of the classifier."""
    movie = tmp_path / "movie"
    movie.mkdir(); _touch(movie / "x.mp4")
    book = tmp_path / "book"
    book.mkdir(); _touch(book / "01.mp3")
    show = tmp_path / "show"
    (show / "Season 01").mkdir(parents=True)
    _touch(show / "Season 01" / "ep.mp4")
    series = tmp_path / "series"
    (series / "Book 1").mkdir(parents=True)
    _touch(series / "Book 1" / "01.mp3")
    empty = tmp_path / "empty"
    empty.mkdir()

    assert scanner.classify_folder(movie) == "movie"
    assert scanner.classify_folder(book) == "audiobook"
    assert scanner.classify_folder(show) == "tv_show"
    assert scanner.classify_folder(series) == "audiobook_grouping"
    assert scanner.classify_folder(empty) is None

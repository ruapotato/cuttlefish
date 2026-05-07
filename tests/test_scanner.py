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
    _touch(show / "specials" / "behind the scenes.mp4")  # → season 0 (Extras)
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "test", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.shows_added == 1
    # Season 01 (2 eps) / S2 (1 ep) / Season 3 (1 ep) / specials (1 ep, season=0).
    assert result.episodes_added == 5
    seasons = sorted(
        r["season"] for r in conn.execute("SELECT season FROM tv_episodes").fetchall()
    )
    assert seasons == [0, 1, 1, 2, 3]  # specials → 0 (Extras), not season 1


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


def test_classify_tv_show_with_theme_mp3_at_root(tmp_path: Path):
    """A TV show folder with a theme.mp3 sitting alongside season dirs is
    a TV show, not an audiobook. Audio at the top doesn't override video
    that exists in the tree below."""
    show = tmp_path / "Some Show"
    (show / "Season 01").mkdir(parents=True)
    (show / "Season 02").mkdir(parents=True)
    _touch(show / "theme.mp3")  # the bait
    _touch(show / "Season 01" / "Some Show - S01E01.mp4")
    _touch(show / "Season 01" / "Some Show - S01E02.mp4")
    _touch(show / "Season 02" / "Some Show - S02E01.mp4")
    assert scanner.classify_folder(show) == "tv_show"


def test_classify_movie_folder_with_theme_mp3(tmp_path: Path):
    """Direct video files still win — a movie folder with both an mp4 and
    a theme.mp3 at the same level is a movie."""
    movie = tmp_path / "Some Movie"
    movie.mkdir()
    _touch(movie / "Some Movie.mp4")
    _touch(movie / "theme.mp3")
    assert scanner.classify_folder(movie) == "movie"


def test_classify_pure_audio_folder_still_audiobook(tmp_path: Path):
    """No video anywhere + audio = audiobook. Regression check."""
    book = tmp_path / "Book"
    book.mkdir()
    _touch(book / "01.mp3")
    _touch(book / "02.mp3")
    _touch(book / "cover.jpg")
    assert scanner.classify_folder(book) == "audiobook"


def test_rescan_after_classifier_change_updates_kind(tmp_path: Path):
    """If a previous (buggy) scan classified a folder as 'audiobook' and the
    next scan classifies it as 'tv_show', the media row's kind must be
    updated and any orphan audiobook_tracks rows must be deleted."""
    root = tmp_path / "media"
    root.mkdir()
    show = root / "Show"
    show.mkdir()
    # Plant an old audiobook-style row + tracks for what's now a TV show.
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "media", root)
    with conn:
        conn.execute(
            "INSERT INTO media (library_id, kind, source_path, title_guess) "
            "VALUES (?, 'audiobook', ?, 'Show')",
            (lib_id, str(show)),
        )
        media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
        conn.execute(
            "INSERT INTO audiobook_tracks (book_id, order_index, source_path) "
            "VALUES (?, 0, ?)",
            (media_id, str(show / "theme.mp3")),
        )
    # Now make the folder structure a TV show with the theme on top.
    (show / "Season 01").mkdir()
    _touch(show / "theme.mp3")
    _touch(show / "Season 01" / "Show - S01E01.mp4")

    scanner.scan_library(conn, lib_id, root)
    row = conn.execute("SELECT kind FROM media WHERE id = ?", (media_id,)).fetchone()
    assert row["kind"] == "tv_show"
    # The orphan audiobook track is gone.
    tracks = conn.execute(
        "SELECT COUNT(*) AS c FROM audiobook_tracks WHERE book_id = ?", (media_id,)
    ).fetchone()
    assert tracks["c"] == 0
    # And tv_episodes was populated by the rescan.
    eps = conn.execute(
        "SELECT COUNT(*) AS c FROM tv_episodes WHERE show_id = ?", (media_id,)
    ).fetchone()
    assert eps["c"] == 1


def test_rescan_movie_to_tv_show_drops_movie_only_state(tmp_path: Path):
    """A folder that used to be a single-file movie that's now a TV show
    (someone added Season subfolders) should reclassify cleanly."""
    root = tmp_path / "media"
    root.mkdir()
    item = root / "Item"
    item.mkdir()
    _touch(item / "Item.mp4")
    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "media", root)
    scanner.scan_library(conn, lib_id, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    assert conn.execute(
        "SELECT kind FROM media WHERE id = ?", (media_id,)
    ).fetchone()["kind"] == "movie"

    # User reorganizes: removes the loose mp4, adds a Season folder.
    (item / "Item.mp4").unlink()
    (item / "Season 01").mkdir()
    _touch(item / "Season 01" / "Item - S01E01.mp4")
    scanner.scan_library(conn, lib_id, root)
    assert conn.execute(
        "SELECT kind FROM media WHERE id = ?", (media_id,)
    ).fetchone()["kind"] == "tv_show"


def test_scan_tv_with_theme_mp3_doesnt_create_audiobook(tmp_path: Path):
    """End-to-end: a library with one TV show that has a theme.mp3 should
    produce a single tv_show row in the media table, not an audiobook."""
    root = tmp_path / "media"
    root.mkdir()
    show = root / "My Show"
    (show / "Season 01").mkdir(parents=True)
    _touch(show / "theme.mp3")
    _touch(show / "Season 01" / "My Show - S01E01.mp4")
    _touch(show / "Season 01" / "My Show - S01E02.mp4")

    conn = _new_db(tmp_path)
    lib_id = _add_library(conn, "media", root)
    result = scanner.scan_library(conn, lib_id, root)
    assert result.shows_added == 1
    assert result.audiobooks_added == 0
    kinds = sorted(r["kind"] for r in conn.execute("SELECT kind FROM media").fetchall())
    assert kinds == ["tv_show"]

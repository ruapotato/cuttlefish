"""Multiple-subtitle-track support: ASR writes alongside originals,
both selectable on the watch page."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cuttlefish import db, scanner, subtitles


def _setup_movie_with_originals(tmp_path: Path):
    """A movies library with one .mp4 + a sidecar .srt (the 'original')."""
    root = tmp_path / "movies"
    root.mkdir()
    video = root / "Movie.mp4"
    video.write_bytes(b"")
    (root / "Movie.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    return db_path, media_id, video


# --- subtitles module helpers -----------------------------------------


def test_asr_output_path_is_dot_asr_dot_srt(tmp_path):
    p = subtitles.asr_output_path_for(tmp_path / "Some Show - S01E01.mkv")
    assert p.name == "Some Show - S01E01.asr.srt"


def test_find_sidecar_excludes_asr_variant(tmp_path):
    video = tmp_path / "Movie.mp4"
    video.write_bytes(b"")
    sidecar = tmp_path / "Movie.srt"
    sidecar.write_text("o")
    asr = tmp_path / "Movie.asr.srt"
    asr.write_text("a")
    # Sidecar finder returns the original, NOT the ASR file.
    found = subtitles.find_sidecar_subtitle(video)
    assert found == sidecar
    # ASR finder returns the ASR file specifically.
    assert subtitles.find_asr_subtitle(video) == asr


def test_find_sidecar_lang_variant_still_works(tmp_path):
    video = tmp_path / "Movie.mp4"
    video.write_bytes(b"")
    (tmp_path / "Movie.en.srt").write_text("o")
    found = subtitles.find_sidecar_subtitle(video)
    assert found is not None and found.name == "Movie.en.srt"


def test_find_sidecar_when_only_asr_exists_returns_none(tmp_path):
    """If the only subtitle on disk is the ASR variant, it shouldn't be
    misreported as an 'original' sidecar."""
    video = tmp_path / "Movie.mp4"
    video.write_bytes(b"")
    (tmp_path / "Movie.asr.srt").write_text("a")
    assert subtitles.find_sidecar_subtitle(video) is None
    assert subtitles.find_asr_subtitle(video) is not None


# --- variants_for_media: original + asr both surface -----------------


def test_variants_returns_only_original_when_asr_missing(tmp_path):
    db_path, media_id, _ = _setup_movie_with_originals(tmp_path)
    variants = subtitles.subtitle_variants_for_media(db.connect(db_path), media_id)
    assert set(variants.keys()) == {"original"}


def test_variants_returns_both_when_asr_present(tmp_path):
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    asr_path = video.parent / "Movie.asr.srt"
    asr_path.write_text("WEBVTT-ish")
    variants = subtitles.subtitle_variants_for_media(db.connect(db_path), media_id)
    assert set(variants.keys()) == {"original", "asr"}
    assert variants["asr"] == asr_path


def test_subtitle_for_media_default_picks_asr_over_original(tmp_path):
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    asr_path = video.parent / "Movie.asr.srt"
    asr_path.write_text("a")
    # Default (no variant arg): ASR wins.
    chosen = subtitles.subtitle_for_media(db.connect(db_path), media_id)
    assert chosen == asr_path
    # Explicit variant=original: returns the sidecar.
    chosen = subtitles.subtitle_for_media(db.connect(db_path), media_id, variant="original")
    assert chosen.name == "Movie.srt"


# --- ASR worker writes to .asr.srt, doesn't clobber original --------


def test_asr_worker_writes_dot_asr_path(tmp_path, monkeypatch):
    """Without running real NeMo: stub transcribe_to_srt to write a fake
    SRT and verify the worker hands it the .asr.srt path."""
    from cuttlefish.workers import asr, encoder

    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    conn = db.connect(db_path)
    encoder.enqueue_encode(conn, media_id)  # any queued job we'll discard
    # Replace the real transcription with a stub that writes a marker file
    # at whatever path the worker chose.
    paths_written: list[Path] = []

    def fake_transcribe(video_path, output_srt, ffmpeg="ffmpeg"):
        paths_written.append(output_srt)
        output_srt.parent.mkdir(parents=True, exist_ok=True)
        output_srt.write_text("WEBVTT\nasr\n")
        return output_srt

    monkeypatch.setattr(asr, "transcribe_to_srt", fake_transcribe)

    # Now enqueue an ASR job and run the worker once.
    encoder.enqueue_encode(conn, media_id)  # cleanup placeholder (will be claimed by encode worker if any)
    with conn:
        cur = conn.execute("INSERT INTO jobs (kind, media_id) VALUES ('asr', ?)", (media_id,))
    job_id = cur.lastrowid

    # Drain just the asr-kind queue
    n = asr.run_worker(db_path=db_path, once=True)
    assert n == 1
    assert len(paths_written) == 1
    assert paths_written[0].name == "Movie.asr.srt"
    # Original sidecar is untouched.
    assert (video.parent / "Movie.srt").read_text().startswith("1\n")
    # Job is marked done.
    job = db.connect(db_path).execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert job["status"] == "done"


# --- /subtitle endpoint variant routing -----------------------------


def _client(db_path):
    from cuttlefish.server import create_app
    return TestClient(create_app(db_path=db_path))


def test_subtitle_endpoint_variant_original(tmp_path):
    db_path, media_id, _ = _setup_movie_with_originals(tmp_path)
    r = _client(db_path).get(f"/subtitle/{media_id}?variant=original")
    assert r.status_code == 200
    assert r.text.startswith("WEBVTT")


def test_subtitle_endpoint_variant_asr(tmp_path):
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    (video.parent / "Movie.asr.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nfrom-asr\n"
    )
    r = _client(db_path).get(f"/subtitle/{media_id}?variant=asr")
    assert r.status_code == 200
    assert "from-asr" in r.text


def test_subtitle_endpoint_default_prefers_asr(tmp_path):
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    (video.parent / "Movie.asr.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,000\nfrom-asr\n"
    )
    # No variant param → ASR wins
    r = _client(db_path).get(f"/subtitle/{media_id}")
    assert "from-asr" in r.text


def test_subtitle_endpoint_unknown_variant_404(tmp_path):
    db_path, media_id, _ = _setup_movie_with_originals(tmp_path)
    r = _client(db_path).get(f"/subtitle/{media_id}?variant=bogus")
    assert r.status_code == 404


# --- watch page renders multiple <track> elements -----------------


def test_watch_page_one_track_when_only_original(tmp_path):
    db_path, media_id, _ = _setup_movie_with_originals(tmp_path)
    r = _client(db_path).get(f"/watch/{media_id}")
    assert r.status_code == 200
    # One <track>, label='Original', and it's the default since no ASR.
    assert "label='Original'" in r.text
    assert "default" in r.text  # the original is default when no ASR
    assert "Auto-generated" not in r.text


def test_watch_page_two_tracks_with_asr_default(tmp_path):
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    (video.parent / "Movie.asr.srt").write_text("WEBVTT\n")
    r = _client(db_path).get(f"/watch/{media_id}")
    assert "label='Original'" in r.text
    assert "label='Auto-generated (ASR)'" in r.text
    # ASR <track> carries default; original does not.
    asr_track_idx = r.text.find("label='Auto-generated (ASR)'")
    orig_track_idx = r.text.find("label='Original'")
    assert asr_track_idx > 0 and orig_track_idx > 0
    # Slice each <track> tag and confirm only the ASR one has 'default'.
    asr_tag_end = r.text.find(">", asr_track_idx)
    orig_tag_end = r.text.find(">", orig_track_idx)
    asr_tag = r.text[asr_track_idx:asr_tag_end]
    orig_tag = r.text[orig_track_idx:orig_tag_end]
    assert " default" in asr_tag
    assert " default" not in orig_tag


# --- admin Generate button label changes by state ----------------


def _admin_client(tmp_path: Path):
    from cuttlefish.server import create_app
    db_path, media_id, video = _setup_movie_with_originals(tmp_path)
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    return client, db_path, media_id, video


def test_button_label_when_only_original(tmp_path):
    client, _, media_id, _ = _admin_client(tmp_path)
    r = client.get(f"/watch/{media_id}")
    assert "Generate ASR subtitles (alongside" in r.text


def test_button_label_when_asr_already_exists(tmp_path):
    client, _, media_id, video = _admin_client(tmp_path)
    (video.parent / "Movie.asr.srt").write_text("WEBVTT\n")
    r = client.get(f"/watch/{media_id}")
    assert "Regenerate ASR subtitles" in r.text


def test_button_label_when_no_subtitles(tmp_path):
    """Movie with no sidecar at all → original generate-button copy."""
    from cuttlefish.server import create_app
    root = tmp_path / "movies"; root.mkdir()
    (root / "Movie.mp4").write_bytes(b"")
    db_path = tmp_path / "t.db"
    conn = db.connect(db_path); db.init_schema(conn)
    with conn:
        cur = conn.execute(
            "INSERT INTO libraries (name, root_path) VALUES ('m', ?)",
            (str(root),),
        )
    scanner.scan_library(conn, cur.lastrowid, root)
    media_id = conn.execute("SELECT id FROM media").fetchone()["id"]
    client = TestClient(create_app(db_path=db_path))
    client.post("/api/auth/register", data={"username": "a", "password": "secret123"})
    client.post("/api/auth/login", data={"username": "a", "password": "secret123"})
    r = client.get(f"/watch/{media_id}")
    assert "Generate subtitles via ASR" in r.text
    assert "Regenerate" not in r.text
    assert "alongside" not in r.text

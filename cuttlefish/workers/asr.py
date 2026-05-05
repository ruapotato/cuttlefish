"""ASR worker — generates SRT subtitles from a video using Parakeet-TDT.

Heavy ML deps (torch, nemo_toolkit) are imported lazily inside the
transcription function so the rest of cuttlefish can be installed and run
without them. Install via `uv sync --extra asr`.

Reference implementation: ~/Voice-Command/speech/whisper_processor.py uses
the same `nvidia/parakeet-tdt-0.6b-v2` model.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "nvidia/parakeet-tdt-0.6b-v2"


def is_available() -> bool:
    """True if NeMo + torch are importable on this machine."""
    try:
        import nemo.collections.asr  # noqa: F401
        return True
    except Exception:
        return False


# Process-local flag set when an ASR worker thread is spawned in this server
# process. The /admin/subtitles page checks this so users see whether queued
# jobs will actually be picked up here, vs sitting forever in the queue.
_worker_in_process = False


def mark_worker_started() -> None:
    global _worker_in_process
    _worker_in_process = True


def is_worker_in_process() -> bool:
    return _worker_in_process


@dataclass
class SrtCue:
    index: int
    start: float
    end: float
    text: str


def _format_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace(".", ",")


def cues_to_srt(cues: Iterable[SrtCue]) -> str:
    out = []
    for c in cues:
        out.append(str(c.index))
        out.append(f"{_format_ts(c.start)} --> {_format_ts(c.end)}")
        out.append(c.text)
        out.append("")
    return "\n".join(out)


def words_to_cues(
    words: list[dict],
    max_chars: int = 84,
    max_gap: float = 1.0,
) -> list[SrtCue]:
    """Group word-level timestamps into reasonable subtitle cues.

    Each `word` dict is expected to have keys: word/text, start, end.
    Cues break when a line gets too long OR there's a long pause.
    """
    cues: list[SrtCue] = []
    current_words: list[str] = []
    current_start: Optional[float] = None
    current_end: Optional[float] = None
    last_end: Optional[float] = None

    for w in words:
        text = (w.get("word") or w.get("text") or "").strip()
        if not text:
            continue
        start = float(w.get("start", w.get("start_time", 0.0)))
        end = float(w.get("end", w.get("end_time", start)))

        gap_too_big = last_end is not None and (start - last_end) > max_gap
        line_too_long = (
            current_words and len(" ".join(current_words + [text])) > max_chars
        )
        if (gap_too_big or line_too_long) and current_words:
            cues.append(
                SrtCue(
                    index=len(cues) + 1,
                    start=current_start or 0.0,
                    end=current_end or start,
                    text=" ".join(current_words),
                )
            )
            current_words = []
            current_start = None
            current_end = None

        if current_start is None:
            current_start = start
        current_end = end
        current_words.append(text)
        last_end = end

    if current_words:
        cues.append(
            SrtCue(
                index=len(cues) + 1,
                start=current_start or 0.0,
                end=current_end or 0.0,
                text=" ".join(current_words),
            )
        )
    return cues


def extract_audio_for_asr(video_path: Path, wav_path: Path, ffmpeg: str = "ffmpeg") -> None:
    """Extract 16kHz mono PCM wav, which is what Parakeet expects."""
    cmd = [
        ffmpeg, "-nostdin", "-y", "-loglevel", "error",
        "-i", str(video_path),
        "-ac", "1", "-ar", "16000", "-vn", str(wav_path),
    ]
    subprocess.run(cmd, check=True)


def transcribe_to_srt(
    video_path: Path,
    output_srt: Path,
    model_id: str = DEFAULT_MODEL_ID,
    ffmpeg: str = "ffmpeg",
) -> Path:
    """Run Parakeet ASR on `video_path` and write an SRT to `output_srt`.

    Raises RuntimeError if NeMo isn't installed (i.e. user didn't install
    the [asr] extra).
    """
    if not is_available():
        raise RuntimeError(
            "ASR dependencies (torch + nemo_toolkit) not installed. "
            "Install with: uv sync --extra asr"
        )
    import nemo.collections.asr as nemo_asr  # type: ignore

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / "audio.wav"
        extract_audio_for_asr(video_path, wav, ffmpeg=ffmpeg)
        log.info("loading Parakeet model %s", model_id)
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=model_id)
        log.info("transcribing %s", video_path)
        # NeMo's transcribe API varies by version. We try the newer kw, then
        # fall back to the older positional form.
        try:
            results = model.transcribe([str(wav)], timestamps=True)
        except TypeError:
            results = model.transcribe([str(wav)])
        words = _extract_words_from_nemo_result(results)
        cues = words_to_cues(words)
        output_srt.parent.mkdir(parents=True, exist_ok=True)
        output_srt.write_text(cues_to_srt(cues), encoding="utf-8")
    return output_srt


def _extract_words_from_nemo_result(results) -> list[dict]:
    """NeMo returns nested result objects whose exact shape varies between
    versions. We dig out a list of {word, start, end} dicts as best we can,
    and degrade to a single whole-utterance cue if no per-word data is found.
    """
    words: list[dict] = []
    if not results:
        return words
    first = results[0]
    # 0.x style: list of strings
    if isinstance(first, str):
        return [{"word": first, "start": 0.0, "end": 0.0}]
    # 1.x style: hypothesis object with .timestamp containing word-level info
    ts = getattr(first, "timestamp", None) or (
        first.get("timestamp") if isinstance(first, dict) else None
    )
    if ts:
        for w in ts.get("word", []) if isinstance(ts, dict) else []:
            words.append({
                "word": w.get("word") or w.get("text") or "",
                "start": w.get("start", 0.0),
                "end": w.get("end", 0.0),
            })
    if not words:
        # Fallback: just use the whole transcript as one cue
        text = getattr(first, "text", None) or (first.get("text", "") if isinstance(first, dict) else str(first))
        if text:
            words.append({"word": text, "start": 0.0, "end": 0.0})
    return words


VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".wmv"}


def _resolve_asr_target(conn, job) -> tuple[Path, Path, str]:
    """Pick the video to transcribe and where to write the SRT for a job.

    Prefers the encoded version when available (writes SRT alongside it in
    the clean folder). Otherwise transcribes the source and drops the SRT
    next to it as a sidecar — which the scanner / subtitle resolver picks
    up automatically.

    Returns (video_path, srt_output_path, kind) where kind is 'media' or
    'episode'. Raises RuntimeError on unresolvable jobs.
    """
    if job["episode_id"]:
        row = conn.execute(
            "SELECT e.source_path, ee.video_path AS encoded_video, "
            "       ee.clean_dir AS encoded_dir "
            "FROM tv_episodes e LEFT JOIN encoded_episodes ee "
            "ON ee.episode_id = e.id WHERE e.id = ?",
            (job["episode_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"episode {job['episode_id']} not found")
        if row["encoded_video"] and Path(row["encoded_video"]).is_file():
            video = Path(row["encoded_video"])
            srt = Path(row["encoded_dir"]) / (video.stem + ".srt")
        else:
            video = Path(row["source_path"])
            srt = video.with_suffix(".srt")
        return video, srt, "episode"
    if job["media_id"]:
        row = conn.execute(
            "SELECT m.source_path, e.video_path AS encoded_video, "
            "       e.clean_dir AS encoded_dir "
            "FROM media m LEFT JOIN encoded_files e ON e.media_id = m.id "
            "WHERE m.id = ?",
            (job["media_id"],),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"media {job['media_id']} not found")
        if row["encoded_video"] and Path(row["encoded_video"]).is_file():
            video = Path(row["encoded_video"])
            srt = Path(row["encoded_dir"]) / (video.stem + ".srt")
            return video, srt, "media"
        src = Path(row["source_path"])
        if src.is_file():
            return src, src.with_suffix(".srt"), "media"
        if src.is_dir():
            for child in sorted(src.iterdir()):
                if child.is_file() and child.suffix.lower() in VIDEO_EXTS:
                    return child, child.with_suffix(".srt"), "media"
            raise RuntimeError(f"no video file inside {src}")
        raise RuntimeError(f"source {src} is neither file nor dir")
    raise RuntimeError("ASR job has neither media_id nor episode_id")


def run_worker(
    db_path=None,
    once: bool = False,
    poll_interval: float = 5.0,
    ffmpeg: str = "ffmpeg",
) -> int:
    """Main loop for the ASR worker. Pulls 'asr' jobs from the queue."""
    from cuttlefish import db as _db
    from cuttlefish.workers import encoder

    conn = _db.connect(db_path)
    processed = 0
    while True:
        job = encoder.claim_next_job(conn, kind="asr")
        if job is None:
            if once:
                return processed
            time.sleep(poll_interval)
            continue
        log.info(
            "ASR job %s (media=%s episode=%s)",
            job["id"], job["media_id"], job["episode_id"],
        )
        try:
            video, srt, kind = _resolve_asr_target(conn, job)
            transcribe_to_srt(video, srt, ffmpeg=ffmpeg)
            # Reflect the new sidecar in the encoded_* tables when present
            # so subtitle_for_media() / subtitle_for_episode() find it via
            # the column lookup as well as the disk fallback.
            with conn:
                if kind == "episode":
                    conn.execute(
                        "UPDATE encoded_episodes SET subtitle_path = ? "
                        "WHERE episode_id = ?",
                        (str(srt), job["episode_id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE encoded_files SET subtitle_path = ? "
                        "WHERE media_id = ?",
                        (str(srt), job["media_id"]),
                    )
            encoder.mark_done(conn, job["id"], {"srt": str(srt)})
            processed += 1
        except Exception as e:
            log.exception("ASR job %s failed", job["id"])
            encoder.mark_failed(conn, job["id"], str(e))
        if once:
            return processed

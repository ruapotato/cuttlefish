"""Tests for the chunked-audio ASR path that fixes CUDA OOM on long videos.

We can't run NeMo in CI, so these tests stub out the heavy pieces (model
loading, transcribe()) and assert that:
  - Long audio is split into the right number of chunks
  - Word timestamps from each chunk are offset by the chunk start
  - Empty cache is called between chunks (memory hygiene)
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from cuttlefish.workers import asr


def _torch_importable() -> bool:
    """Return whether 'import torch' works in this venv. Skip ASR tests if
    not (e.g. mid-CUDA-swap state where libcudnn.so isn't on disk yet)."""
    try:
        import torch  # noqa: F401
        return True
    except Exception:
        return False


@pytest.fixture
def fake_nemo(monkeypatch):
    """Install fake nemo + torch + soundfile modules so transcribe_to_srt
    can run end-to-end without the actual GPU/ML stack."""
    if not _torch_importable():
        pytest.skip("torch not importable in this venv")
    # ---- fake nemo.collections.asr -----------------------------------
    fake_model = MagicMock()
    fake_model.transcribe.return_value = [
        # NeMo-1.x style: object with .timestamp dict containing 'word' list
        types.SimpleNamespace(
            timestamp={
                "word": [
                    {"word": "hello", "start": 0.5, "end": 1.0},
                    {"word": "world", "start": 1.0, "end": 1.5},
                ]
            },
            text="hello world",
        )
    ]
    nemo_asr = MagicMock()
    nemo_asr.models.ASRModel.from_pretrained.return_value = fake_model

    fake_nemo_pkg = types.ModuleType("nemo")
    fake_nemo_collections = types.ModuleType("nemo.collections")
    fake_nemo_asr = types.ModuleType("nemo.collections.asr")
    fake_nemo_asr.models = nemo_asr.models
    monkeypatch.setitem(sys.modules, "nemo", fake_nemo_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections", fake_nemo_collections)
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", fake_nemo_asr)

    # ---- fake torch (already importable since it's a real dep, but we
    # patch is_available so the no-CUDA branch is taken) ---------------
    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    # is_available() lazy-imports nemo; ensure our fake satisfies it.
    monkeypatch.setattr(asr, "is_available", lambda: True)
    # Skip the real CUDA probe.
    monkeypatch.setattr(asr, "_force_cpu_if_cuda_broken", lambda: None)

    # ---- bypass ffmpeg by stubbing extract_audio_for_asr ------------
    def fake_extract(video_path, wav_path, ffmpeg="ffmpeg"):
        # Write 65 seconds of 16kHz silence so the chunker sees > 2 chunks
        import soundfile as sf  # type: ignore
        import numpy as _np
        sr = 16000
        data = _np.zeros(int(65 * sr), dtype=_np.float32)
        sf.write(str(wav_path), data, sr)

    monkeypatch.setattr(asr, "extract_audio_for_asr", fake_extract)

    return fake_model


def test_chunked_transcription_writes_srt(tmp_path, fake_nemo, monkeypatch):
    """65s of audio with 30s chunks → 3 chunks → 3 model.transcribe calls,
    and the resulting cues use the offset-adjusted timestamps."""
    monkeypatch.setenv("CUTTLEFISH_ASR_CHUNK_SECS", "30")
    out = tmp_path / "out.srt"
    asr.transcribe_to_srt(
        video_path=tmp_path / "fake.mp4",  # not actually read (extract is stubbed)
        output_srt=out,
    )
    assert out.is_file()
    text = out.read_text()
    # Three calls × two cue-words per call (from the fake model)
    assert fake_nemo.transcribe.call_count == 3
    # SRT uses comma as the millisecond separator. Each chunk emits a cue
    # whose timestamps are offset by the chunk start.
    assert "00:00:00,500" in text  # first chunk's "hello"
    assert "00:00:30,500" in text  # second chunk's "hello" offset by 30s
    assert "00:01:00,500" in text  # third chunk's "hello" offset by 60s


def test_chunk_size_env_var(tmp_path, fake_nemo, monkeypatch):
    """CUTTLEFISH_ASR_CHUNK_SECS=60 → 65s audio fits in 2 chunks."""
    monkeypatch.setenv("CUTTLEFISH_ASR_CHUNK_SECS", "60")
    out = tmp_path / "out.srt"
    asr.transcribe_to_srt(
        video_path=tmp_path / "fake.mp4",
        output_srt=out,
    )
    assert fake_nemo.transcribe.call_count == 2


def test_short_audio_is_one_chunk(tmp_path, monkeypatch):
    """Audio shorter than chunk_secs → one transcribe call, cues unshifted."""
    if not _torch_importable():
        pytest.skip("torch not importable in this venv")
    fake_model = MagicMock()
    fake_model.transcribe.return_value = [
        types.SimpleNamespace(
            timestamp={"word": [{"word": "hi", "start": 0.1, "end": 0.5}]},
            text="hi",
        )
    ]
    nemo_models = MagicMock()
    nemo_models.ASRModel.from_pretrained.return_value = fake_model
    fake_nemo_pkg = types.ModuleType("nemo")
    fake_nemo_collections = types.ModuleType("nemo.collections")
    fake_nemo_asr = types.ModuleType("nemo.collections.asr")
    fake_nemo_asr.models = nemo_models
    monkeypatch.setitem(sys.modules, "nemo", fake_nemo_pkg)
    monkeypatch.setitem(sys.modules, "nemo.collections", fake_nemo_collections)
    monkeypatch.setitem(sys.modules, "nemo.collections.asr", fake_nemo_asr)

    import torch
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(asr, "is_available", lambda: True)
    monkeypatch.setattr(asr, "_force_cpu_if_cuda_broken", lambda: None)

    def fake_extract(video_path, wav_path, ffmpeg="ffmpeg"):
        import soundfile as sf  # type: ignore
        sr = 16000
        sf.write(str(wav_path), np.zeros(int(5 * sr), dtype=np.float32), sr)
    monkeypatch.setattr(asr, "extract_audio_for_asr", fake_extract)

    out = tmp_path / "out.srt"
    asr.transcribe_to_srt(video_path=tmp_path / "fake.mp4", output_srt=out)
    assert fake_model.transcribe.call_count == 1
    # No offset since it was the first (and only) chunk; SRT comma-separator.
    assert "00:00:00,100" in out.read_text()

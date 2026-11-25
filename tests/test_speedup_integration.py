"""Integration-style checks of the speed-up paths, with Whisper and yt-dlp replaced only at their
outer boundary: batched decoding → refill of an unheard region → natural segments → cache,
and a link whose video download breaks mid-way while its audio is already transcribed."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import faster_whisper
import faster_whisper.audio
import faster_whisper.vad
import transcription
import utils
from backend import db, queue_runner, server
from state import app_state


def _seg(words):
    ws = [SimpleNamespace(start=s, end=e, word=" " + w) for s, e, w in words]
    return SimpleNamespace(start=ws[0].start, end=ws[-1].end, text=" ".join(w for _, _, w in words), words=ws)


def _info(duration=30.0, language="uk"):
    return SimpleNamespace(language=language, language_probability=0.99, duration=duration)


# 0–10 s: one long batched chunk of speech (no full stops, so it must be split by pause/length);
# 10–16 s: speech the batched decoder skipped; 16–30 s: more speech.
BATCHED_SEGMENTS = [
    _seg([(0.2 + i * 0.45, 0.6 + i * 0.45, f"раз{i}") for i in range(10)]          # 0.2–4.65
         + [(5.5 + i * 0.4, 5.85 + i * 0.4, f"два{i}") for i in range(11)]),        # pause, then 5.5–9.85
    _seg([(16.2 + i * 0.5, 16.6 + i * 0.5, f"три{i}") for i in range(27)]),         # 16.2–29.6, no pauses
]
REFILL = [_seg([(0.7, 1.2, "Привіт,"), (1.3, 1.6, "я"), (1.7, 2.3, "зараз"), (2.4, 3.0, "граю."),
                (3.4, 3.9, "Ось"), (4.0, 4.8, "дивіться.")])]
VAD = [{"start": 0, "end": 10 * 16000}, {"start": 10 * 16000, "end": 16 * 16000},
       {"start": 16 * 16000, "end": 30 * 16000}]


@pytest.fixture
def gpu_whisper(monkeypatch):
    """A fake GPU model: the batched pipeline misses 10–16 s, the sequential decoder hears it."""
    calls = {"batched": [], "sequential": []}

    class FakePipeline:
        def __init__(self, model):
            self.model = model

        def transcribe(self, audio, batch_size=8, **kw):
            calls["batched"].append({"batch_size": batch_size, **kw})
            return iter(BATCHED_SEGMENTS), _info()

    model = SimpleNamespace()

    def sequential(audio, **kw):
        calls["sequential"].append({"seconds": len(audio) / 16000, **kw})
        return iter(REFILL), _info(duration=len(audio) / 16000)

    model.transcribe = sequential
    monkeypatch.setattr(faster_whisper, "BatchedInferencePipeline", FakePipeline)
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **k: np.zeros(30 * 16000, np.float32))
    monkeypatch.setattr(faster_whisper.vad, "get_speech_timestamps", lambda audio, opts: list(VAD))
    monkeypatch.setattr(transcription, "get_whisper", lambda: model)
    monkeypatch.setattr(transcription, "extract_audio", lambda src, dst: dst)
    monkeypatch.setattr(app_state, "_whisper_engine", "faster_whisper")
    monkeypatch.setattr(app_state, "whisper_device", "cuda")
    monkeypatch.setitem(transcription.CONFIG["whisper"], "batch_size", 8)
    monkeypatch.setitem(transcription.CONFIG["cache"], "enabled", True)
    return calls


def test_batched_pipeline_end_to_end_into_the_cache(gpu_whisper, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not really a video" * 1000)
    progress = []

    segments, words, lang, cached = transcription.transcribe_video(
        str(video), status_cb=lambda *a: progress.append(a), language="uk")

    assert cached is False and lang == "uk"
    # batched decoding ran once, with the studio's settings
    assert len(gpu_whisper["batched"]) == 1
    b = gpu_whisper["batched"][0]
    assert b["batch_size"] == 8 and b["language"] == "uk" and b["word_timestamps"] is True
    # the skipped 10–16 s region was decoded again sequentially, without VAD
    assert len(gpu_whisper["sequential"]) == 1
    seq = gpu_whisper["sequential"][0]
    assert seq["seconds"] == pytest.approx(6.0) and seq["vad_filter"] is False
    # its words are back, shifted to the video's timeline, in time order
    texts = [w["word"] for w in words]
    assert texts[texts.index("Привіт,"):texts.index("Привіт,") + 6] == ["Привіт,", "я", "зараз", "граю.", "Ось", "дивіться."]
    hello = next(w for w in words if w["word"] == "Привіт,")
    assert hello["start"] == pytest.approx(10.7)
    assert [w["start"] for w in words] == sorted(w["start"] for w in words)
    assert len(words) == 10 + 11 + 6 + 27
    # segments look like the sequential mode's again: none longer than 12 s, split at the pause
    # and at full stops; every word ends up in exactly one segment
    assert all(s["end"] - s["start"] <= 12.0 for s in segments)
    assert any(s["text"].startswith("два0") for s in segments)
    assert "Привіт, я зараз граю." in [s["text"] for s in segments]
    assert sum(len(s["text"].split()) for s in segments) == len(words)
    assert any(isinstance(a[0], (int, float)) for a in progress)  # progress percentages reported

    # saved to the (isolated) cache: the next analysis skips Whisper entirely
    again = transcription.transcribe_video(str(video), language="uk")
    assert again == (segments, words, "uk", True)
    assert len(gpu_whisper["batched"]) == 1


def test_video_breaking_mid_download_fails_the_item_honestly(monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    audio_dirs = []

    def audio_download(url, output_dir, **kw):
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        audio_dirs.append(d)
        f = d / "abc.audio.m4a"
        f.write_bytes(b"audio")
        return str(f)

    def broken_video(url, progress_cb=None, space_checked=False):
        progress_cb(0.42, "Завантаження: 42%")
        raise subprocess.CalledProcessError(
            1, ["yt-dlp"], "", "[download] 42.0%\nERROR: unable to download video data: HTTP Error 403: Forbidden\n")

    transcribed = []
    monkeypatch.setattr(utils, "download_audio_from_url", audio_download)
    monkeypatch.setattr(server, "ingest_url", broken_video)
    monkeypatch.setattr(transcription, "transcribe_media",
                        lambda path, lang, cb=None: transcribed.append(path) or ([{"start": 0, "end": 1, "text": "так"}],
                                                                                 [{"start": 0, "end": 1, "word": "так"}], "uk"))
    projects_before = len(db.list_projects()) if hasattr(db, "list_projects") else None

    client.post("/api/queue", json={"urls": ["https://youtube.com/watch?v=abc"]})
    item = queue_runner.process_next()

    assert transcribed, "Whisper should have started on the audio while the video was downloading"
    assert item["status"] == "failed"
    assert item["error"] == "Не вдалося завантажити відео: unable to download video data: HTTP Error 403: Forbidden"
    assert not item["project_id"]
    assert audio_dirs and not audio_dirs[0].exists()        # temporary audio removed
    assert not list(server.UPLOADS_DIR.glob("temp_audio_*"))
    if projects_before is not None:
        assert len(db.list_projects()) == projects_before   # no half-made project left behind
    assert not any(transcription.transcript_cache_dir().glob("*.json"))  # nothing cached for a video that never came

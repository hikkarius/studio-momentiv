"""Links in the queue: audio first, Whisper on it while the video downloads in parallel."""
from __future__ import annotations

import threading

import pytest

import transcription
import utils
from backend import db, queue_runner, server

SEGS = [{"start": 0.0, "end": 2.0, "text": "Привіт усім"}]
WORDS = [{"start": 0.0, "end": 0.8, "word": "Привіт"}, {"start": 0.9, "end": 2.0, "word": "усім"}]


@pytest.fixture
def fake_audio(monkeypatch):
    made = []

    def download(url, output_dir, **kw):
        from pathlib import Path
        p = Path(output_dir)
        p.mkdir(parents=True, exist_ok=True)
        f = p / "abc.audio.m4a"
        f.write_bytes(b"audio")
        made.append(p)
        return str(f)

    monkeypatch.setattr(utils, "download_audio_from_url", download)
    return made


def _fake_ingest(started: threading.Event | None = None, wait_for: threading.Event | None = None, fail=None):
    calls = []

    def ingest(url, progress_cb=None, space_checked=False):
        calls.append({"space_checked": space_checked})
        if started:
            started.set()
        if progress_cb:
            progress_cb(0.5, "Завантаження: 50%")
        if wait_for:
            wait_for.wait(timeout=5)
        if fail:
            raise fail
        video = server.UPLOADS_DIR / "p1_video.mp4"
        video.write_bytes(b"video-bytes" * 100)
        proj = db.create_project(project_id="p1", title="Відео", source_path=str(video), duration=60.0)
        return proj, False

    ingest.calls = calls
    return ingest


def test_whisper_runs_on_audio_while_the_video_downloads(monkeypatch, fake_audio):
    video_started = threading.Event()
    whisper_done = threading.Event()
    ingest = _fake_ingest(started=video_started, wait_for=whisper_done)
    monkeypatch.setattr(server, "ingest_url", ingest)
    seen = {}

    def fake_transcribe(path, cfg_lang, status_cb=None):
        seen["path"], seen["lang"] = path, cfg_lang
        seen["parallel"] = video_started.wait(timeout=5)  # the video download is already running
        status_cb(0.5, "Транскрипція аудіо (CUDA): 50%")
        whisper_done.set()
        return SEGS, WORDS, "uk"

    monkeypatch.setattr(transcription, "transcribe_media", fake_transcribe)
    msgs = []
    project, reused = queue_runner.download_audio_first("https://youtube.com/watch?v=abc",
                                                        lambda f, m: msgs.append(m), "uk")
    assert seen["parallel"] is True
    assert seen["path"].endswith("abc.audio.m4a") and seen["lang"] == "uk"
    assert ingest.calls == [{"space_checked": True}]  # the size was probed once, up front
    # the transcript waits in the cache under the final video, so the analysis skips Whisper
    assert transcription.load_transcript_cache_for(project["source_path"], "uk") == (SEGS, WORDS, "uk")
    assert any("розпізнавання мови: 50%" in m for m in msgs)
    assert any("мову вже розпізнано" in m for m in msgs)
    assert not fake_audio[0].exists()  # the temporary audio is gone


def test_failed_video_download_fails_honestly(monkeypatch, fake_audio):
    monkeypatch.setattr(server, "ingest_url", _fake_ingest(fail=RuntimeError("Video unavailable")))
    monkeypatch.setattr(transcription, "transcribe_media", lambda *a, **k: (SEGS, WORDS, "uk"))
    with pytest.raises(RuntimeError, match="Video unavailable"):
        queue_runner.download_audio_first("https://youtube.com/watch?v=abc", lambda f, m: None, "uk")
    assert not fake_audio[0].exists()


def test_no_separate_audio_falls_back_to_the_old_way(monkeypatch):
    # conftest makes the audio download fail
    ingest = _fake_ingest()
    monkeypatch.setattr(server, "ingest_url", ingest)
    called = []
    monkeypatch.setattr(transcription, "transcribe_media", lambda *a, **k: called.append(1))
    project, _ = queue_runner.download_audio_first("https://youtube.com/watch?v=abc", lambda f, m: None)
    assert project["id"] == "p1"
    assert called == []
    assert ingest.calls == [{"space_checked": False}]
    assert transcription.load_transcript_cache_for(project["source_path"], "uk") is None


def test_audio_transcription_error_still_downloads_the_video(monkeypatch, fake_audio):
    monkeypatch.setattr(server, "ingest_url", _fake_ingest())

    def broken(*a, **k):
        raise RuntimeError("Whisper crashed")

    monkeypatch.setattr(transcription, "transcribe_media", broken)
    project, _ = queue_runner.download_audio_first("https://youtube.com/watch?v=abc", lambda f, m: None, "uk")
    assert project["id"] == "p1"
    # nothing cached: the analysis will transcribe the video itself
    assert transcription.load_transcript_cache_for(project["source_path"], "uk") is None


def test_queue_item_uses_the_audio_first_path(monkeypatch, fake_audio):
    from fastapi.testclient import TestClient
    client = TestClient(server.app)
    monkeypatch.setattr(server, "ingest_url", _fake_ingest())
    monkeypatch.setattr(transcription, "transcribe_media", lambda *a, **k: (SEGS, WORDS, "uk"))
    analysed = []
    from backend import job_runner

    def fake_analyze(job_id, project_id, clips_count, filter_mode, language=None, search_mode=None):
        analysed.append(transcription.load_transcript_cache_for(db.get_project(project_id)["source_path"], "uk"))
        db.update_job(job_id, status="completed", progress=1.0, message="Готово")

    monkeypatch.setattr(job_runner, "_run_analyze_worker", fake_analyze)
    client.post("/api/queue", json={"urls": ["https://youtube.com/watch?v=abc"], "language": "uk"})
    queue_runner.process_next()
    assert analysed and analysed[0] == (SEGS, WORDS, "uk")

"""«Скасувати» on a running video, and the honest percent after the video is downloaded."""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

import transcription
import utils
from backend import db, job_runner, queue_runner, server

from tests.test_audio_first import SEGS, WORDS, _fake_ingest, fake_audio  # noqa: F401


@pytest.fixture
def client():
    return TestClient(server.app)


def _add(client) -> str:
    r = client.post("/api/queue", json={"urls": ["https://youtube.com/watch?v=abc"], "language": "uk"})
    return r.json()["added"][0]["id"]


def test_time_left_text():
    assert queue_runner.whisper_time_left(None, 100) == ""
    assert queue_runner.whisper_time_left(0.01, 100) == ""   # too early to judge
    assert queue_runner.whisper_time_left(0.5, 10) == ""
    assert queue_runner.whisper_time_left(0.25, 600) == "≈ 30 хв"
    assert queue_runner.whisper_time_left(0.1, 600) == "≈ 1 год 30 хв"
    assert queue_runner.whisper_time_left(0.5, 3600) == "≈ 1 год"


def test_after_the_download_the_percent_is_whispers(monkeypatch, fake_audio):  # noqa: F811
    monkeypatch.setattr(server, "ingest_url", _fake_ingest())
    video_done = threading.Event()
    real_ingest = server.ingest_url

    def ingest(url, progress_cb=None, space_checked=False):
        out = real_ingest(url, progress_cb, space_checked)
        progress_cb(1.0, "Завантаження: 100%")
        video_done.set()
        return out

    monkeypatch.setattr(server, "ingest_url", ingest)

    def slow_whisper(path, cfg_lang, status_cb=None):
        video_done.wait(timeout=5)
        status_cb(0.23, "Транскрипція аудіо (CUDA): 23%")
        return SEGS, WORDS, "uk"

    monkeypatch.setattr(transcription, "transcribe_media", slow_whisper)
    seen = []
    queue_runner.download_audio_first("https://youtube.com/watch?v=abc", lambda f, m: seen.append((f, m)), "uk")
    after = [(f, m) for f, m in seen if m.startswith("Відео скачано")]
    # not «Завантаження відео: 100%» with Whisper's percent hidden (looked like a hang to a friend)
    assert (0.23, "Відео скачано · розпізнавання мови: 23%") in [(round(f, 2), m.split(" · лишилось")[0]) for f, m in after]


def test_cancel_stops_whisper_and_marks_the_item(monkeypatch, client, fake_audio):  # noqa: F811
    monkeypatch.setattr(server, "ingest_url", _fake_ingest())
    item_id = _add(client)
    started, stopped = threading.Event(), []

    def endless_whisper(path, cfg_lang, status_cb=None):
        started.set()
        for i in range(500):  # a long video: one report per segment
            try:
                status_cb(i / 1000, "Транскрипція аудіо (CUDA)")
            except job_runner.Cancelled:
                stopped.append(i)
                raise
            threading.Event().wait(0.01)
        return SEGS, WORDS, "uk"

    monkeypatch.setattr(transcription, "transcribe_media", endless_whisper)
    analysed = []
    monkeypatch.setattr(job_runner, "_run_analyze_worker", lambda *a, **k: analysed.append(1))

    def press_cancel():
        started.wait(timeout=5)
        r = client.post(f"/api/queue/{item_id}/cancel")
        assert r.json() == {"status": "cancelling"}

    t = threading.Thread(target=press_cancel)
    t.start()
    item = queue_runner.process_next()
    t.join()
    assert stopped and stopped[0] < 499
    assert item["status"] == "cancelled" and item["message"] == "Скасовано"
    assert analysed == []
    assert not fake_audio[0].exists()  # the temporary audio is cleaned up
    # and it can be started again
    r = client.post(f"/api/queue/{item_id}/retry")
    assert r.status_code == 200 and r.json()["item"]["status"] == "waiting"


def test_cancel_during_the_analysis(monkeypatch, client):
    proj = db.create_project(project_id="p2", title="Відео", source_path=str(server.UPLOADS_DIR / "v.mp4"), duration=60.0)
    r = client.post("/api/queue", json={"project_ids": [proj["id"]]})
    item_id = r.json()["added"][0]["id"]
    started = threading.Event()

    def fake_transcribe(video_path, status_cb=None, language=None):
        started.set()
        for i in range(500):
            status_cb(i / 1000, "Транскрипція аудіо (CUDA)")
            threading.Event().wait(0.01)
        return SEGS, WORDS, "uk", False

    monkeypatch.setattr(job_runner, "transcribe_video", fake_transcribe)

    def press_cancel():
        started.wait(timeout=5)
        assert client.post(f"/api/queue/{item_id}/cancel").json() == {"status": "cancelling"}

    t = threading.Thread(target=press_cancel)
    t.start()
    item = queue_runner.process_next()
    t.join()
    assert item["status"] == "cancelled"
    assert db.get_project("p2")["status"] == "uploaded"  # not «analyzing» or «failed»


def test_cancel_a_waiting_item_removes_it(client):
    item_id = _add(client)
    assert client.post(f"/api/queue/{item_id}/cancel").json() == {"status": "removed"}
    assert db.get_queue_item(item_id) is None


def test_cancel_unknown_or_finished(client):
    assert client.post("/api/queue/q_nope/cancel").status_code == 404
    item_id = _add(client)
    db.update_queue_item(item_id, status="done")
    assert client.post(f"/api/queue/{item_id}/cancel").status_code == 409


def test_restart_while_cancelling_keeps_it_cancelled(client):
    item_id = _add(client)
    db.update_queue_item(item_id, status="downloading", message=db.CANCEL_MSG)
    other = db.add_queue_item(queue_runner.new_item_id(), source_url="https://youtube.com/watch?v=x", title="x")
    db.update_queue_item(other["id"], status="analyzing", message="Аналіз")
    db.requeue_interrupted_items()
    assert db.get_queue_item(item_id)["status"] == "cancelled"
    assert db.get_queue_item(other["id"])["status"] == "waiting"


def test_cancel_kills_ytdlp(monkeypatch, tmp_path):
    """The download subprocess is killed when the progress callback raises Cancelled."""
    import sys
    script = tmp_path / "fake.py"
    script.write_text("import time\nfor i in range(1000):\n    print(f'[download]  {i/10:.1f}%', flush=True)\n    time.sleep(0.01)\n")
    calls = []

    def cb(frac, msg):
        calls.append(frac)
        if len(calls) == 3:
            raise job_runner.Cancelled()

    with pytest.raises(job_runner.Cancelled):
        utils._run_ytdlp_with_progress([sys.executable, str(script), "--"], None, cb)
    assert len(calls) == 3

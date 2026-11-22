"""
tests/test_job_runner.py — Unit tests for backend/job_runner.py.
Tests job scheduling, SSE pub/sub, audio windowing, safe scoring, and fallback flows.
"""
import asyncio
import os
import shutil
from unittest.mock import MagicMock, patch

import pytest

from backend import db
from backend.job_runner import (
    _run_analyze_worker,
    broadcast_update,
    enqueue_analyze_job,
    register_subscriber,
    set_event_loop,
    unregister_subscriber,
)


@pytest.fixture(autouse=True)
def setup_tmp_db(tmp_path, monkeypatch):
    test_db = str(tmp_path / "test_runner.db")
    monkeypatch.setattr(db, "DB_PATH", test_db)
    db.init_db()


def test_subscriber_registration_and_unregistration():
    job_id = "test_job_sub_1"
    q = register_subscriber(job_id)
    assert q is not None

    # Unregister
    unregister_subscriber(job_id, q)
    # Double unregister should be safe
    unregister_subscriber(job_id, q)


def test_broadcast_update_with_loop():
    loop = asyncio.new_event_loop()
    set_event_loop(loop)

    job_id = "test_job_bcast"
    proj_id = "test_proj_bcast"
    db.create_project(proj_id, "test.mp4", "test.mp4", 60.0)
    db.create_job(job_id, proj_id, "analyze")

    q = register_subscriber(job_id)

    # Broadcast update
    broadcast_update(job_id, {"status": "running", "progress": 0.5, "message": "Analyzing..."})

    # Run loop briefly to process call_soon_threadsafe callbacks
    loop.stop()
    loop.run_forever()

    assert not q.empty()
    item = q.get_nowait()
    assert item["status"] == "running"
    assert item["progress"] == 0.5

    unregister_subscriber(job_id, q)
    set_event_loop(None)
    loop.close()


def test_run_analyze_worker_project_not_found():
    job_id = "job_missing_proj"
    proj_id = "non_existent_proj_id"
    # Create parent project or dummy project so foreign key won't fail
    db.create_project("dummy_parent", "dummy.mp4", "dummy.mp4", 10.0)
    db.create_job(job_id, "dummy_parent", "analyze")

    _run_analyze_worker(job_id, proj_id, 3, "Viral Hooks")
    job = db.get_job(job_id)
    assert job is not None
    assert job["status"] == "failed"
    assert "не знайдено" in (job["error"] or "")


def test_run_analyze_worker_whisper_local_success(tmp_path, monkeypatch):
    proj_id = "test_proj_worker_whisper"
    video_file = tmp_path / "test_vid.mp4"
    video_file.write_bytes(b"dummy_mp4_bytes")

    db.create_project(proj_id, str(video_file), "test_vid.mp4", 120.0)
    job_id = "job_worker_whisper_1"
    db.create_job(job_id, proj_id, "analyze")

    mock_segments = [{"start": 10.0, "end": 40.0, "text": "Вірусний хук і порада"}]
    mock_words = []
    mock_clips = [
        {
            "start": 10.0,
            "end": 40.0,
            "title": "Секрет успіху",
            "score": 92,
            "hook": "Почніть з цього",
            "reason": "Високий інтерес",
            "caption": "Повний опис",
            "hashtags": ["#успіх", "#шортс"],
        }
    ]

    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: (mock_segments, mock_words, "uk", False))
    monkeypatch.setattr("backend.job_runner.analyze_and_find_clips", lambda **kwargs: mock_clips)

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks", engine="whisper_local")

    job = db.get_job(job_id)
    assert job["status"] == "completed"
    assert job["progress"] == 1.0

    proj = db.get_project(proj_id)
    assert proj["status"] == "ready"

    clips = db.get_candidate_clips(proj_id)
    assert len(clips) == 1
    assert clips[0]["title"] == "Секрет успіху"
    assert clips[0]["virality_score"] == 92

    # Clean up test output directory
    out_dir = os.path.join("web_output", proj_id)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)


def test_run_analyze_worker_legacy_engine_standardizes_to_whisper(tmp_path, monkeypatch):
    """Verifies that passing legacy engine='gemini_cloud' seamlessly executes Faster-Whisper pipeline."""
    proj_id = "test_proj_fallback"
    video_file = tmp_path / "test_vid2.mp4"
    video_file.write_bytes(b"dummy_mp4")

    db.create_project(proj_id, str(video_file), "test_vid2.mp4", 80.0)
    job_id = "job_worker_fallback_1"
    db.create_job(job_id, proj_id, "analyze")

    mock_segments = [{"start": 0.0, "end": 30.0, "text": "Звичайний текст"}]
    mock_clips = [{"start": 0.0, "end": 30.0, "title": "Фрагмент 1", "score": "88/100"}]

    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: (mock_segments, [], "uk", False))
    monkeypatch.setattr("backend.job_runner.analyze_and_find_clips", lambda **kwargs: mock_clips)

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks", engine="gemini_cloud")

    job = db.get_job(job_id)
    assert job["status"] == "completed"
    clips = db.get_candidate_clips(proj_id)
    assert len(clips) == 1
    assert clips[0]["title"] == "Фрагмент 1"

    out_dir = os.path.join("web_output", proj_id)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)


def test_enqueue_analyze_job(monkeypatch):
    proj_id = "2026-11-17_proj_enqueue"
    db.create_project(proj_id, "source.mp4", "source.mp4", 60.0)

    submitted = []
    monkeypatch.setattr("backend.job_runner._executor.submit", lambda fn, *args: submitted.append(args))

    job_id = enqueue_analyze_job(proj_id, clips_count=5, filter_mode="Viral Hooks")
    assert job_id.startswith("job_analyze_")
    assert len(submitted) == 1
    assert submitted[0][0] == job_id
    assert submitted[0][1] == proj_id


def test_run_analyze_worker_empty_segments_fails_job(tmp_path, monkeypatch):
    """When transcribe_video returns no segments, worker must mark job as failed."""
    proj_id = "test_proj_empty_segs"
    video_file = tmp_path / "silent.mp4"
    video_file.write_bytes(b"dummy_silent_video")

    db.create_project(proj_id, str(video_file), "silent.mp4", 60.0)
    job_id = "job_silent_1"
    db.create_job(job_id, proj_id, "analyze")

    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: ([], [], "uk", False))

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks")

    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "не виявлено мовлення" in job["error"]


def test_run_analyze_worker_no_valid_clips_fails_without_fake_highlight(tmp_path, monkeypatch):
    """When all AI candidates are invalid, the job must fail honestly (no fake 'Головний хайлайт')."""
    proj_id = "test_proj_empty_valid"
    video_file = tmp_path / "short_vid.mp4"
    video_file.write_bytes(b"dummy_mp4")

    db.create_project(proj_id, "short_vid.mp4", str(video_file), 45.0)
    job_id = "job_empty_valid_1"
    db.create_job(job_id, proj_id, "analyze")

    too_short_clips = [{"start": 1.0, "end": 3.0, "title": "Too short"}]
    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: ([{"start": 0.0, "end": 45.0, "text": "hello"}], [], "uk", False))
    monkeypatch.setattr("backend.job_runner.analyze_and_find_clips", lambda **kwargs: too_short_clips)

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Storytelling", engine="whisper_local")

    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "жодного коректного кліпу" in job["error"]
    assert db.get_candidate_clips(proj_id) == []
    assert db.get_project(proj_id)["status"] == "failed"


def test_run_analyze_worker_gemini_unavailable_reaches_job_error(tmp_path, monkeypatch):
    from ai_selector import GEMINI_UNAVAILABLE_MESSAGE, GeminiUnavailableError

    proj_id = "test_proj_gemini_down"
    db.create_project(proj_id, "vid.mp4", str(tmp_path / "vid.mp4"), 300.0)
    job_id = "job_gemini_down_1"
    db.create_job(job_id, proj_id, "analyze")

    def gemini_down(**kwargs):
        raise GeminiUnavailableError(GEMINI_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: ([{"start": 0.0, "end": 60.0, "text": "x"}], [], "uk", True))
    monkeypatch.setattr("backend.job_runner.analyze_and_find_clips", gemini_down)

    _run_analyze_worker(job_id, proj_id, clips_count=3, filter_mode="Viral Hooks")

    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert job["error"] == GEMINI_UNAVAILABLE_MESSAGE
    assert GEMINI_UNAVAILABLE_MESSAGE in job["message"]
    assert db.get_candidate_clips(proj_id) == []


def test_run_analyze_worker_uses_config_clip_bounds(tmp_path, monkeypatch):
    from config_loader import app_config

    proj_id = "test_proj_bounds"
    db.create_project(proj_id, "vid.mp4", str(tmp_path / "vid.mp4"), 600.0)
    job_id = "job_bounds_1"
    db.create_job(job_id, proj_id, "analyze")

    captured = {}

    def fake_analyze(**kwargs):
        captured.update(kwargs)
        return [
            {"start": 10.0, "end": 40.0, "title": "OK", "score": 90},
            {"start": 100.0, "end": 200.0, "title": "LONG", "score": 85},
        ]

    monkeypatch.setattr("backend.job_runner.transcribe_video", lambda p, status_cb=None, language=None: ([{"start": 0.0, "end": 60.0, "text": "x"}], [], "uk", False))
    monkeypatch.setattr("backend.job_runner.analyze_and_find_clips", fake_analyze)

    _run_analyze_worker(job_id, proj_id, clips_count=2, filter_mode="Viral Hooks")

    assert captured["min_len"] == app_config.clips.default_min_len
    assert captured["max_len"] == app_config.clips.default_max_len
    saved = db.get_candidate_clips(proj_id)
    assert len(saved) == 2
    for c in saved:
        assert c["end_sec"] - c["start_sec"] <= app_config.clips.default_max_len + 3


def test_config_yaml_clip_bounds_are_15_to_60():
    from config_loader import app_config
    assert app_config.clips.default_min_len == 15
    assert app_config.clips.default_max_len == 60


def test_whisper_cb_progress_mapping(tmp_path, monkeypatch):
    """BUG-05: whisper_cb must accept (pct, msg) and advance job progress smoothly from 0.15 to 0.55."""
    proj_id = "test_proj_whisper_pct"
    video_file = tmp_path / "pct_vid.mp4"
    video_file.write_bytes(b"dummy_mp4")

    db.create_project(proj_id, "pct_vid.mp4", str(video_file), 60.0)
    job_id = "job_whisper_pct_1"
    db.create_job(job_id, proj_id, "analyze")

    recorded_progress = []

    def fake_transcribe(p, status_cb=None, language=None):
        if status_cb:
            status_cb(0.5, "Транскрипція аудіо (CUDA): 50%")
            recorded_progress.append(db.get_job(job_id)["progress"])
            status_cb(1.0, "Транскрипція аудіо (CUDA): 100%")
            recorded_progress.append(db.get_job(job_id)["progress"])
        return ([{"start": 0.0, "end": 40.0, "text": "test"}], [], "uk", False)

    monkeypatch.setattr("backend.job_runner.transcribe_video", fake_transcribe)
    monkeypatch.setattr(
        "backend.job_runner.analyze_and_find_clips",
        lambda **kwargs: [{"start": 0.0, "end": 30.0, "title": "Clip", "score": 90}],
    )

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks", engine="whisper_local")

    assert recorded_progress == [0.35, 0.55]
    assert db.get_job(job_id)["status"] == "completed"


def test_run_analyze_worker_transcribe_exception_marks_failed(tmp_path, monkeypatch):
    """When an unexpected exception occurs in worker, status must transition to failed with error message."""
    proj_id = "test_proj_exc"
    video_file = tmp_path / "corrupt.mp4"
    video_file.write_bytes(b"corrupt_mp4")

    db.create_project(proj_id, "corrupt.mp4", str(video_file), 60.0)
    job_id = "job_corrupt_1"
    db.create_job(job_id, proj_id, "analyze")

    def mock_fail(*args, **kwargs):
        raise ValueError("FFmpeg audio decode failure")

    monkeypatch.setattr("backend.job_runner.transcribe_video", mock_fail)

    _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks")

    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "FFmpeg audio decode failure" in job["error"]

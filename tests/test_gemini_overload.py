"""Google overloaded: windows are retried, never silently lost; finished windows are kept;
the queue pauses with an honest message; parallel windows back off together; thinking level;
Whisper warms up when a new queue item starts."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import ai_selector
from ai_selector import (GEMINI_BAD_KEY_MESSAGE, GeminiIncompleteError, GeminiUnavailableError,
                         analyze_and_find_clips)
from backend import db, job_runner, queue_runner, server
from config_loader import app_config

client = TestClient(server.app)


def _segments(n=300):
    return [{"start": i * 2.0, "end": i * 2.0 + 1.8, "text": f"речення {i}."} for i in range(n)]


def _clips_for(segs, k=15):
    return [{"start": segs[(j * len(segs)) // k]["start"], "end": segs[(j * len(segs)) // k]["start"] + 20,
             "title": f"{segs[(j * len(segs)) // k]['start']}", "score": 50 + j} for j in range(k)]


@pytest.fixture
def sleeps(monkeypatch):
    rec: list[float] = []
    monkeypatch.setattr("ai_selector.time.sleep", lambda s: rec.append(s))
    return rec


def _flaky(fail_first: dict):
    """Fake window: windows whose first segment start is in fail_first fail that many times with 503."""
    calls: dict[float, int] = {}
    lock = threading.Lock()

    def window(**kw):
        first = kw["segments"][0]["start"]
        with lock:
            calls[first] = calls.get(first, 0) + 1
            n = calls[first]
        if n <= fail_first.get(first, 0):
            raise GeminiUnavailableError(ai_selector.GEMINI_UNAVAILABLE_MESSAGE)
        return _clips_for(kw["segments"])

    window.calls = calls
    return window


# ─── 1. Lost windows: second round, then an honest error with the finished ones ─────────────

def test_second_round_recovers_an_unanswered_window(monkeypatch, sleeps):
    monkeypatch.setattr(ai_selector, "_SECOND_ROUND_WAIT", 90.0)
    fake = _flaky({120.0: 1})  # window 2 (starts at 120 s) is not answered the first time
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake)
    clips = analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert 90.0 in sleeps                      # waited before the second round
    assert fake.calls[120.0] == 2 and sorted(fake.calls.values()) == [1, 1, 1, 1, 2]
    assert any(120 <= c["start"] < 240 for c in clips)  # its clips made it into the result


def test_still_missing_window_raises_with_the_finished_ones(monkeypatch, sleeps):
    fake = _flaky({120.0: 99, 240.0: 99})
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake)
    with pytest.raises(GeminiIncompleteError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    e = exc.value
    assert e.overloaded and not e.daily
    assert e.total == 5 and len(e.done) == 3
    assert "проаналізовано 3 з 5 частин" in str(e)
    assert fake.calls[120.0] == 2 and fake.calls[0.0] == 1  # only the missing ones were asked again


def test_resume_asks_only_the_missing_windows_and_matches_a_full_run(monkeypatch, sleeps):
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", _flaky({120.0: 99}))
    with pytest.raises(GeminiIncompleteError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    saved = exc.value.done

    later = _flaky({})
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", later)
    resumed = analyze_and_find_clips(client=object(), segments=_segments(), count=75, done_windows=saved)
    assert list(later.calls) == [120.0]  # one request, for the window that was missing

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", _flaky({}))
    full = analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert resumed == full


def test_saved_windows_of_another_request_are_not_reused(monkeypatch, sleeps):
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", _flaky({120.0: 99}))
    with pytest.raises(GeminiIncompleteError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    later = _flaky({})
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", later)
    from constants import FilterMode
    analyze_and_find_clips(client=object(), segments=_segments(), count=75, filter_mode=FilterMode.STORYTELLING,
                           done_windows=exc.value.done)
    assert len(later.calls) == 5  # another filter = other prompts: everything is asked again


def test_wrong_key_is_not_retried_in_a_second_round(monkeypatch, sleeps):
    calls = []

    def bad(**kw):
        calls.append(1)
        raise GeminiUnavailableError(GEMINI_BAD_KEY_MESSAGE)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", bad)
    monkeypatch.setattr(ai_selector, "_SECOND_ROUND_WAIT", 90.0)
    with pytest.raises(GeminiUnavailableError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert str(exc.value) == GEMINI_BAD_KEY_MESSAGE
    assert not isinstance(exc.value, GeminiIncompleteError)
    assert len(calls) == 5 and 90.0 not in sleeps


def test_short_video_overload_is_also_reported_as_incomplete(monkeypatch, sleeps):
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", _flaky({0.0: 99}))
    with pytest.raises(GeminiIncompleteError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(20), count=5)
    assert exc.value.overloaded and exc.value.total == 1 and exc.value.done == {}
    assert "перевантажений" in str(exc.value)


# ─── job runner: finished windows are kept on disk, the job fails honestly ──────────────

def _project(pid="p_over", duration=600.0):
    src = server.UPLOADS_DIR / f"{pid}.mp4"
    src.write_bytes(b"\0" * 2048)
    return db.create_project(pid, f"{pid}.mp4", str(src), duration)


@pytest.fixture
def analysis_fakes(monkeypatch):
    segs = _segments()
    words = [{"start": s["start"] + k * 0.4, "end": s["start"] + k * 0.4 + 0.35, "word": w}
             for s in segs for k, w in enumerate(s["text"].split())]
    monkeypatch.setattr(job_runner, "transcribe_video", lambda *a, **k: (segs, words, "uk", True))
    monkeypatch.setattr(job_runner, "rerank_clips", lambda clips, **k: clips)
    return segs


def test_incomplete_analysis_keeps_windows_and_fails_honestly_then_resumes(monkeypatch, analysis_fakes):
    proj = _project()
    seen = []

    def first(**kw):
        seen.append(dict(kw.get("done_windows") or {}))
        raise GeminiIncompleteError("Сервери Google перевантажені: проаналізовано 3 з 5 частин відео.",
                                    {"k1": [{"start": 1, "end": 20}]}, 5, GeminiUnavailableError("503"))

    monkeypatch.setattr(job_runner, "analyze_and_find_clips", first)
    job_id = job_runner._new_job_id(proj["id"])
    db.create_job(job_id, proj["id"], "analyze")
    job_runner._run_analyze_worker(job_id, proj["id"], 10, "Viral Hooks")

    job = db.get_job(job_id)
    assert job["status"] == "failed"
    assert "проаналізовано 3 з 5" in job["error"] and "доробимо лише те, чого бракує" in job["error"]
    hit = job_runner.pop_quota_hit(job_id)
    assert isinstance(hit, GeminiIncompleteError) and hit.overloaded
    assert job_runner.load_done_windows(proj["id"]) == {"k1": [{"start": 1, "end": 20}]}

    def second(**kw):
        seen.append(dict(kw.get("done_windows") or {}))
        return [{"start": 10.0, "end": 40.0, "title": "ОК", "score": 90}]

    monkeypatch.setattr(job_runner, "analyze_and_find_clips", second)
    job_id2 = job_runner._new_job_id(proj["id"]) + "b"
    db.create_job(job_id2, proj["id"], "analyze")
    job_runner._run_analyze_worker(job_id2, proj["id"], 10, "Viral Hooks")
    assert seen[1] == {"k1": [{"start": 1, "end": 20}]}  # the second run got the finished windows
    assert db.get_job(job_id2)["status"] == "completed"
    assert job_runner.load_done_windows(proj["id"]) == {}  # cleaned up after success


# ─── queue: overload pauses the video with its own message ─────────────────────────────

def test_overload_pauses_the_queue_item_with_an_honest_message(monkeypatch):
    proj = _project("p_q")
    r = client.post("/api/queue", json={"project_ids": [proj["id"]]})
    assert r.status_code == 200

    def run(job_id, project_id, clips_count, filter_mode, engine="whisper_local", language=None):
        with job_runner._quota_lock:
            job_runner._quota_hits[job_id] = GeminiIncompleteError(
                "x", {"a": [], "b": []}, 5, GeminiUnavailableError("503"))
        job_runner.broadcast_update(job_id, {"status": "failed", "progress": 1.0, "error": "x", "message": "x"})

    monkeypatch.setattr(job_runner, "_run_analyze_worker", run)
    before = time.time()
    item = queue_runner.process_next()
    assert item["status"] == "limit"
    assert item["message"].startswith("Сервери Google перевантажені. Проаналізовано 2 з 5 частин відео")
    assert "Доробимо решту автоматично" in item["message"]
    assert 170 <= item["resume_at"] - before <= 200  # a few minutes, not until tomorrow
    q = client.get("/api/queue").json()
    assert q["limit_reason"] == "overload" and q["limit_daily"] is False
    # «Спробувати зараз» works for it
    assert client.post(f"/api/queue/{item['id']}/resume").status_code == 200


def test_overload_pause_respects_turning_waiting_off(monkeypatch):
    monkeypatch.setattr(app_config.studio, "wait_for_limit", False)
    err = GeminiIncompleteError("x", {}, 3, GeminiUnavailableError("503"))
    assert queue_runner._pause_on_limit({"id": "q_x", "limit_hits": 0}, err) is False


# ─── 2. Shared back-off between parallel windows ─────────────────────────────────────────

def test_a_503_in_one_window_makes_the_others_wait(sleeps):
    gate = ai_selector._SharedBackoff()
    gate.hit(3.0)
    t = threading.Thread(target=gate.wait)  # another window, about to send its request
    t.start()
    t.join(timeout=5)
    assert len(sleeps) == 1 and 2.5 < sleeps[0] <= 3.0


def test_window_call_waits_at_the_gate_after_a_503(monkeypatch, sleeps):
    monkeypatch.setattr(ai_selector, "_GATE_PAUSE", (2.0, 2.0))
    fake_client = MagicMock()
    ok = MagicMock()
    ok.text = '[{"start": 1, "end": 30, "title": "ОК"}]'
    fake_client.models.generate_content.side_effect = [Exception("503 UNAVAILABLE high demand"), ok]
    gate = ai_selector._SharedBackoff()
    clips = ai_selector._call_gemini_single_window(
        client=fake_client, segments=_segments(10), count=1, min_len=15, max_len=60,
        filter_mode="Viral Hooks", detected_lang="uk", gate=gate)
    assert clips[0]["title"] == "ОК"
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 2.0  # held off before asking the next model


def test_no_gate_pauses_for_a_single_window(monkeypatch, sleeps):
    fake_client = MagicMock()
    ok = MagicMock()
    ok.text = '[{"start": 1, "end": 30, "title": "ОК"}]'
    fake_client.models.generate_content.side_effect = [Exception("503 UNAVAILABLE"), ok]
    analyze_and_find_clips(client=fake_client, segments=_segments(10), count=1)
    assert sleeps == []


# ─── 3. Thinking level ─────────────────────────────────────────────────────────────────

def test_thinking_level_is_sent_and_dropped_for_a_model_that_refuses_it(monkeypatch):
    monkeypatch.setattr(app_config.gemini, "thinking_level", "minimal")
    monkeypatch.setattr(ai_selector, "_NO_THINKING_LEVEL", set())
    configs = []
    ok = MagicMock()
    ok.text = "[]"

    def gen(model, contents, config):
        configs.append((model, config.thinking_config))
        if model == "old-model" and config.thinking_config is not None:
            raise Exception("400 INVALID_ARGUMENT: thinking_level is not supported for this model")
        return ok

    fake = MagicMock()
    fake.models.generate_content.side_effect = gen
    ai_selector._generate(fake, "new-model", "p", {}, 0.3)
    ai_selector._generate(fake, "old-model", "p", {}, 0.3)
    ai_selector._generate(fake, "old-model", "p", {}, 0.3)  # remembered: asked without it at once
    levels = [(m, tc.thinking_level.value if tc else None) for m, tc in configs]
    assert levels == [("new-model", "MINIMAL"), ("old-model", "MINIMAL"), ("old-model", None), ("old-model", None)]


def test_empty_thinking_level_leaves_the_model_default(monkeypatch):
    monkeypatch.setattr(app_config.gemini, "thinking_level", "")
    assert ai_selector._json_config({}, 0.3, ai_selector._thinking_level_for("m")).thinking_config is None


def test_rerank_uses_the_thinking_level_too(monkeypatch):
    monkeypatch.setattr(app_config.gemini, "thinking_level", "low")
    monkeypatch.setattr(ai_selector, "_NO_THINKING_LEVEL", set())
    resp = MagicMock()
    resp.text = '[{"n": 1, "score": 90}, {"n": 2, "score": 40}]'
    fake = MagicMock()
    fake.models.generate_content.return_value = resp
    ai_selector.rerank_clips([{"start": 0, "end": 20, "score": 60}, {"start": 30, "end": 50, "score": 60}], client=fake)
    cfg = fake.models.generate_content.call_args.kwargs["config"]
    assert cfg.thinking_config.thinking_level.value == "LOW"


# ─── 4. Whisper warms up when a new video starts in the queue ──────────────────────────

def test_new_queue_item_warms_whisper_but_a_resumed_one_does_not(monkeypatch):
    from state import app_state
    warmed = []
    monkeypatch.setattr(app_state, "_whisper", None)
    monkeypatch.setattr(app_state, "preload_whisper_async", lambda: warmed.append(1))

    def ok(job_id, project_id, clips_count, filter_mode, engine="whisper_local", language=None):
        job_runner.broadcast_update(job_id, {"status": "completed", "progress": 1.0, "message": "Готово"})

    monkeypatch.setattr(job_runner, "_run_analyze_worker", ok)
    proj = _project("p_warm")
    client.post("/api/queue", json={"project_ids": [proj["id"]]})
    queue_runner.process_next()
    assert warmed == [1]

    # a paused item coming back only needs Gemini: no warm-up
    proj2 = _project("p_warm2")
    client.post("/api/queue", json={"project_ids": [proj2["id"]]})
    item = next(i for i in db.list_queue_items() if i["project_id"] == proj2["id"])
    db.pause_queue_item(item["id"], time.time() - 1, False, "x", reason="overload")
    queue_runner.process_next()
    assert warmed == [1]


def test_already_loaded_whisper_is_not_loaded_again(monkeypatch):
    from state import app_state
    warmed = []
    monkeypatch.setattr(app_state, "_whisper", object())
    monkeypatch.setattr(app_state, "preload_whisper_async", lambda: warmed.append(1))
    queue_runner._warm_whisper()
    assert warmed == []

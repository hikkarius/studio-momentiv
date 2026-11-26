"""
tests/test_reliability.py — Gemini limit pause in the queue, daily database copies, keep-awake.
"""
import datetime
import os
import sqlite3
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import ai_selector
import config_loader
import transcription
from backend import backup, db, job_runner, keep_awake, queue_runner, server
from backend.server import app

client = TestClient(app)

KYIV = datetime.timezone(datetime.timedelta(hours=2))  # winter time


def _project(pid, duration=600.0):
    src = server.UPLOADS_DIR / f"{pid}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\0" * 2048)
    return db.create_project(pid, f"{pid}.mp4", str(src), duration)


def _limit_then_ok(limit_runs=1, daily=True):
    """Fake analysis: the first `limit_runs` calls stop on the Gemini limit, then it works."""
    def run(job_id, project_id, clips_count, filter_mode, engine="whisper_local", language=None, search_mode=None):
        run.calls.append(project_id)
        if len(run.calls) <= limit_runs:
            with job_runner._quota_lock:
                job_runner._quota_hits[job_id] = ai_selector.GeminiQuotaError(
                    ai_selector.GEMINI_QUOTA_MESSAGE, daily=daily, retry_after=30)
            job_runner.broadcast_update(job_id, {"status": "failed", "progress": 1.0,
                                                 "error": ai_selector.GEMINI_QUOTA_MESSAGE, "message": "x"})
        else:
            db.save_candidate_clips(project_id, [{"start": 1, "end": 20, "title": "t", "hook": "h", "reason": "r"}])
            db.update_project_status(project_id, "ready")
            job_runner.broadcast_update(job_id, {"status": "completed", "progress": 1.0, "message": "Готово!"})
    run.calls = []
    return run


# ─── Gemini limit errors ──────────────────────────────────────────────────────

_RAW_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota. "
            "Please retry in 37.5s.', 'details': [{'violations': [{'quotaId': '%s'}]}, "
            "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '37s'}]}}")


def test_quota_error_tells_daily_from_per_minute():
    daily = ai_selector.quota_error_from([_RAW_429 % "GenerateRequestsPerDayPerProjectPerModel-FreeTier"])
    minute = ai_selector.quota_error_from([_RAW_429 % "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"])
    bare = ai_selector.quota_error_from(["429 quota exceeded"])
    assert (daily.daily, daily.retry_after) == (True, 37.0)
    assert (minute.daily, minute.retry_after) == (False, 37.0)
    assert (bare.daily, bare.retry_after) == (False, None)
    assert isinstance(daily, ai_selector.GeminiUnavailableError)  # old handlers still catch it
    assert str(daily) == ai_selector.GEMINI_QUOTA_MESSAGE


def test_all_models_on_limit_raise_quota_error(monkeypatch):
    class Models:
        def generate_content(self, **kw):
            raise RuntimeError(_RAW_429 % "GenerateRequestsPerDayPerProjectPerModel-FreeTier")

    class Client:
        models = Models()

    monkeypatch.setattr(ai_selector, "_RETRY_PASS_DELAYS", (0.0, 0.0))
    segs = [{"start": 0.0, "end": 30.0, "text": "привіт"}]
    with pytest.raises(ai_selector.GeminiQuotaError) as exc:
        ai_selector._call_gemini_single_window(Client(), segs, 3, 15, 60, "Viral Hooks", "uk", 30.0)
    assert exc.value.daily is True


def test_daily_limit_resets_at_midnight_pacific():
    # Winter: midnight PST = 08:00 UTC = 10:00 Kyiv
    now = datetime.datetime(2026, 11, 24, 14, 35, tzinfo=KYIV).timestamp()
    reset = datetime.datetime.fromtimestamp(queue_runner.next_daily_reset(now), tz=KYIV)
    assert (reset.date(), reset.hour, reset.minute) == (datetime.date(2026, 11, 25), 10, 0)
    # Just after the reset the next one is a day later
    now = datetime.datetime(2026, 11, 25, 10, 1, tzinfo=KYIV).timestamp()
    reset = datetime.datetime.fromtimestamp(queue_runner.next_daily_reset(now), tz=KYIV)
    assert reset.date() == datetime.date(2026, 11, 26)
    # Summer: midnight PDT = 07:00 UTC
    now = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    reset = datetime.datetime.fromtimestamp(queue_runner.next_daily_reset(now), tz=datetime.timezone.utc)
    assert (reset.day, reset.hour) == (2, 7)


def test_short_limit_backs_off_when_it_repeats():
    now = 1_000_000.0
    assert queue_runner.limit_resume_at(False, None, 0, now) == now + 60
    assert queue_runner.limit_resume_at(False, 90, 0, now) == now + 95
    assert queue_runner.limit_resume_at(False, None, 2, now) == now + 15 * 60
    assert queue_runner.limit_resume_at(False, None, 9, now) == now + 3600
    assert queue_runner.limit_resume_at(True, None, 0, now) > now + 60


# ─── Queue pause ──────────────────────────────────────────────────────────────

def test_limit_pauses_queue_instead_of_failing_then_resumes():
    _project("proj_a")
    db.add_queue_item("q_a", project_id="proj_a", title="A")
    fake = _limit_then_ok(limit_runs=1, daily=True)
    with patch.object(job_runner, "_run_analyze_worker", fake):
        item = queue_runner.process_next()
        assert item["status"] == "limit"
        assert item["limit_daily"] == 1
        assert item["resume_at"] > time.time() + 60
        assert "ліміт" in item["message"].lower()

        body = client.get("/api/queue").json()
        assert body["active"] is True
        assert body["limit_daily"] is True
        assert body["paused_until"] == pytest.approx(item["resume_at"])
        assert "proj_a" in db.busy_project_ids()  # stays off the ready-videos list

        assert queue_runner.process_next() is None  # nothing to do until the limit resets

        r = client.post("/api/queue/q_a/resume")  # «Спробувати зараз»
        assert r.status_code == 200
        item = queue_runner.process_next()
    assert item["status"] == "done"
    assert fake.calls == ["proj_a", "proj_a"]


def test_limit_pause_can_be_turned_off():
    config_loader.save_user_settings({"studio": {"wait_for_limit": False}})
    _project("proj_b")
    db.add_queue_item("q_b", project_id="proj_b", title="B")
    with patch.object(job_runner, "_run_analyze_worker", _limit_then_ok(limit_runs=1)):
        item = queue_runner.process_next()
    assert item["status"] == "failed"
    assert item["error"] == ai_selector.GEMINI_QUOTA_MESSAGE


def test_next_videos_are_transcribed_while_paused():
    _project("proj_c1")
    _project("proj_c2")
    db.add_queue_item("q_c1", project_id="proj_c1", title="C1")
    db.add_queue_item("q_c2", project_id="proj_c2", title="C2")
    fake = _limit_then_ok(limit_runs=1)
    transcribed = []
    with patch.object(job_runner, "_run_analyze_worker", fake), \
         patch.object(job_runner, "transcribe_only", lambda pid, language=None, progress_cb=None: transcribed.append(pid)):
        first = queue_runner.process_next()
        second = queue_runner.process_next()
        assert first["status"] == "limit"
        assert second["status"] == "limit"
        assert transcribed == ["proj_c2"]
        assert fake.calls == ["proj_c1"]  # Gemini was not asked again during the pause
        assert second["resume_at"] == pytest.approx(first["resume_at"])
        assert "розпізнано" in second["message"]

        # The limit resets: both go on in their order, the first one first
        with db.get_db() as conn:
            conn.execute("UPDATE queue_items SET resume_at = 0 WHERE status = 'limit'")
        assert queue_runner.process_next()["id"] == "q_c1"
        assert queue_runner.process_next()["id"] == "q_c2"
    assert fake.calls == ["proj_c1", "proj_c1", "proj_c2"]


def test_resume_endpoint_only_for_paused_items():
    _project("proj_d")
    db.add_queue_item("q_d", project_id="proj_d", title="D")
    assert client.post("/api/queue/q_d/resume").status_code == 409


def test_paused_item_can_be_removed():
    _project("proj_e")
    db.add_queue_item("q_e", project_id="proj_e", title="E")
    db.pause_queue_item("q_e", time.time() + 3600, True, "ліміт")
    assert client.delete("/api/queue/q_e").status_code == 200
    assert db.get_queue_item("q_e") is None


# ─── Keep awake ───────────────────────────────────────────────────────────────

def test_keep_awake_follows_the_queue(monkeypatch):
    calls = []
    monkeypatch.setattr(keep_awake, "_active", False)
    monkeypatch.setattr(keep_awake.sys, "platform", "win32")

    class K32:
        def SetThreadExecutionState(self, flags):
            calls.append(flags)
            return 1

    class Windll:
        kernel32 = K32()

    import ctypes
    monkeypatch.setattr(ctypes, "windll", Windll(), raising=False)

    queue_runner._update_keep_awake()
    assert keep_awake.is_active() is False and calls == []

    _project("proj_f")
    db.add_queue_item("q_f", project_id="proj_f", title="F")
    queue_runner._update_keep_awake()
    assert keep_awake.is_active() is True
    assert calls == [0x80000001]

    config_loader.save_user_settings({"studio": {"keep_awake": False}})
    queue_runner._update_keep_awake()
    assert keep_awake.is_active() is False
    assert calls == [0x80000001, 0x80000000]


# ─── Daily database copy ──────────────────────────────────────────────────────

def _cache() -> Path:
    d = transcription.transcript_cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_backup_has_database_and_transcripts():
    _project("proj_g")
    db.save_candidate_clips("proj_g", [{"start": 1, "end": 20, "title": "t"}])
    (_cache() / "abc.json").write_text('{"segments": []}', encoding="utf-8")
    item = backup.create_backup("manual")
    assert (item["videos"], item["clips"]) == (1, 1)
    path = backup.BACKUP_DIR / item["name"]
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
    assert {"studio_data.db", "meta.json", "transcripts/abc.json"} <= names
    assert backup.list_backups()[0]["name"] == item["name"]
    assert str(backup.BACKUP_DIR).startswith(str(Path(db.DB_PATH).parent))  # tests stay in tmp


def test_daily_backup_once_a_day_and_only_when_on():
    _project("proj_h")
    assert backup.maybe_daily_backup()["kind"] == "daily"
    assert backup.maybe_daily_backup() is None  # already done today
    config_loader.save_user_settings({"studio": {"daily_backup": False}})
    with patch.object(backup, "newest_backup_time", lambda: 0.0):
        assert backup.maybe_daily_backup() is None


def test_old_copies_are_pruned(monkeypatch):
    backup.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for day in range(1, 10):
        (backup.BACKUP_DIR / f"studio-202611{day:02d}-090000-daily.zip").write_bytes(b"x")
    for day in range(1, 4):
        (backup.BACKUP_DIR / f"studio-202611{day:02d}-100000-before-restore.zip").write_bytes(b"x")
    backup.create_backup("daily")
    names = sorted(p.name for p in backup.BACKUP_DIR.glob("*.zip"))
    assert len([n for n in names if "before-restore" not in n]) == backup.KEEP
    assert len([n for n in names if "before-restore" in n]) == backup.KEEP_BEFORE_RESTORE
    assert "studio-20261101-090000-daily.zip" not in names


def test_restore_brings_statuses_back_and_keeps_a_safety_copy():
    _project("proj_i")
    db.save_candidate_clips("proj_i", [{"start": 1, "end": 20, "title": "t"}])
    (_cache() / "t1.json").write_text("{}", encoding="utf-8")
    saved = backup.create_backup("manual")

    # Disaster: the clip status changes, the project and transcript are gone
    db.update_clip_fields("proj_i", 0, review_status="drop")
    _project("proj_new")
    with db.get_db() as conn:
        conn.execute("DELETE FROM candidate_clips WHERE project_id = 'proj_i'")
    (_cache() / "t1.json").unlink()

    r = client.post(f"/api/backups/{saved['name']}/restore")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcripts"] == 1
    assert (_cache() / "t1.json").exists()
    assert len(db.get_candidate_clips("proj_i")) == 1
    assert db.get_project("proj_new") is None
    kinds = [b["kind"] for b in client.get("/api/backups").json()["items"]]
    assert "before-restore" in kinds

    # …and the restore itself can be undone
    r = client.post(f"/api/backups/{body['safety']}/restore")
    assert r.status_code == 200
    assert db.get_project("proj_new") is not None


def test_restore_refuses_bad_names_and_running_analysis():
    for bad in ["../studio_data.db", "studio-1-daily.zip", "nope.zip"]:
        assert client.post(f"/api/backups/{bad}/restore").status_code in (404, 409)
    item = backup.create_backup("manual")
    _project("proj_j")
    db.create_job("job_j", "proj_j", "analyze")
    db.update_job("job_j", status="running")
    r = client.post(f"/api/backups/{item['name']}/restore")
    assert r.status_code == 409
    assert "обробляється" in r.json()["detail"]


def test_backup_endpoint_and_settings():
    r = client.post("/api/backups")
    assert r.status_code == 200 and r.json()["item"]["kind"] == "manual"
    body = client.get("/api/backups").json()
    assert body["keep"] == 7 and body["total_size"] > 0
    r = client.post("/api/settings", json={"studio": {"daily_backup": False, "keep_awake": False, "wait_for_limit": False}})
    assert r.status_code == 200
    vals = r.json()["values"]["studio"]
    assert (vals["daily_backup"], vals["keep_awake"], vals["wait_for_limit"]) == (False, False, False)

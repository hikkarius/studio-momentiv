"""
tests/test_queue_settings_storage.py — processing queue, «Налаштування» screen and disk cleanup.
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import config_loader
from backend import db, job_runner, queue_runner, server
from backend.server import app

client = TestClient(app)


def _project(pid="proj_q1", duration=600.0, source=None):
    src = source or str(server.UPLOADS_DIR / f"{pid}.mp4")
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(src):
        Path(src).write_bytes(b"\0" * 2048)
    return db.create_project(pid, f"{pid}.mp4", src, duration)


def _fake_analyze(ok=True):
    def run(job_id, project_id, clips_count, filter_mode, engine="whisper_local", language=None):
        run.calls.append((project_id, clips_count, filter_mode))
        run.languages.append(language)
        if ok:
            db.save_candidate_clips(project_id, [{"start": 1, "end": 20, "title": "t", "hook": "h", "reason": "r"}])
            db.update_project_status(project_id, "ready")
            job_runner.broadcast_update(job_id, {"status": "completed", "progress": 1.0, "message": "Готово! 1"})
        else:
            job_runner.broadcast_update(job_id, {"status": "failed", "progress": 1.0, "error": "Gemini впав", "message": "x"})
    run.calls = []
    run.languages = []
    return run


# ─── Queue ────────────────────────────────────────────────────────────────────

def test_queue_runs_items_in_order_with_auto_count():
    _project("proj_a", duration=500)
    _project("proj_b", duration=100)
    r = client.post("/api/queue", json={"project_ids": ["proj_a", "proj_b"], "filter_mode": "Storytelling"})
    assert r.status_code == 200 and len(r.json()["added"]) == 2

    fake = _fake_analyze()
    with patch.object(job_runner, "_run_analyze_worker", fake):
        first = queue_runner.process_next()
        second = queue_runner.process_next()
        assert queue_runner.process_next() is None

    assert [c[0] for c in fake.calls] == ["proj_a", "proj_b"]
    assert fake.calls[0][1] == 20 and fake.calls[1][1] == 5      # auto: 500/25 = 20, at least 5
    assert "Storytelling" in fake.calls[0][2]
    assert first["status"] == "done" and second["status"] == "done"
    assert client.get("/api/queue").json()["active"] is False


def test_queue_downloads_links_then_analyzes():
    def fake_ingest(url, progress_cb=None):
        progress_cb(0.5, "Завантаження: 50%")
        return _project("proj_url", duration=250), False

    client.post("/api/queue", json={"urls": ["https://youtu.be/abc", "https://youtu.be/abc"], "clips_count": 7})
    items = client.get("/api/queue").json()["items"]
    assert len(items) == 1                                         # same link twice → once

    fake = _fake_analyze()
    with patch.object(server, "ingest_url", fake_ingest), patch.object(job_runner, "_run_analyze_worker", fake):
        done = queue_runner.process_next()
    assert done["status"] == "done" and done["project_id"] == "proj_url"
    assert done["title"] == "proj_url.mp4"
    assert fake.calls == [("proj_url", 7, "Viral Hooks")]


def test_queue_skips_analysis_for_already_processed_video():
    proj = _project("proj_ready")
    db.save_candidate_clips("proj_ready", [{"start": 1, "end": 20, "title": "t", "hook": "h", "reason": "r"}])
    db.update_project_status("proj_ready", "ready")
    client.post("/api/queue", json={"urls": ["https://youtu.be/again"]})
    fake = _fake_analyze()
    with patch.object(server, "ingest_url", lambda url, progress_cb=None: (db.get_project("proj_ready"), True)), \
         patch.object(job_runner, "_run_analyze_worker", fake):
        done = queue_runner.process_next()
    assert done["status"] == "done" and "вже оброблене" in done["message"]
    assert fake.calls == []


def test_queue_failure_is_reported_and_can_be_retried():
    _project("proj_f")
    client.post("/api/queue", json={"project_ids": ["proj_f"]})
    with patch.object(job_runner, "_run_analyze_worker", _fake_analyze(ok=False)):
        failed = queue_runner.process_next()
    assert failed["status"] == "failed" and "Gemini впав" in failed["error"]

    r = client.post(f"/api/queue/{failed['id']}/retry")
    assert r.status_code == 200
    items = client.get("/api/queue").json()["items"]
    assert [i["status"] for i in items] == ["waiting"]


def test_queue_download_error_is_short_and_clear():
    client.post("/api/queue", json={"urls": ["https://youtu.be/broken"]})

    def boom(url, progress_cb=None):
        import subprocess
        raise subprocess.CalledProcessError(1, ["yt-dlp"], "", "WARNING: x\nERROR: [youtube] Video unavailable\n")

    with patch.object(server, "ingest_url", boom):
        failed = queue_runner.process_next()
    assert failed["status"] == "failed"
    assert failed["error"] == "Не вдалося завантажити відео: [youtube] Video unavailable"


def test_queue_remove_move_and_clear():
    for p in ("proj_1", "proj_2", "proj_3"):
        _project(p)
    client.post("/api/queue", json={"project_ids": ["proj_1", "proj_2", "proj_3"]})
    ids = [i["id"] for i in client.get("/api/queue").json()["items"]]
    assert client.post(f"/api/queue/{ids[2]}/move", json={"direction": -1}).status_code == 200
    order = [i["project_id"] for i in client.get("/api/queue").json()["items"]]
    assert order == ["proj_1", "proj_3", "proj_2"]

    assert client.delete(f"/api/queue/{ids[0]}").status_code == 200
    db.update_queue_item(ids[1], status="analyzing")
    assert client.delete(f"/api/queue/{ids[1]}").status_code == 409   # can't stop a running one
    db.update_queue_item(ids[1], status="done")
    assert client.post("/api/queue/clear-finished").json()["removed"] == 1
    assert len(client.get("/api/queue").json()["items"]) == 1


def test_queue_does_not_add_same_video_twice_while_waiting():
    _project("proj_dup")
    client.post("/api/queue", json={"project_ids": ["proj_dup"]})
    r = client.post("/api/queue", json={"project_ids": ["proj_dup"]})
    assert r.json()["added"] == []


def test_queue_rejects_bad_input():
    assert client.post("/api/queue", json={}).status_code == 400
    assert client.post("/api/queue", json={"urls": ["ftp://x"]}).status_code == 400
    assert client.post("/api/queue", json={"project_ids": ["nope"]}).status_code == 404


def test_interrupted_items_resume_first():
    _project("proj_r1")
    _project("proj_r2")
    client.post("/api/queue", json={"project_ids": ["proj_r1", "proj_r2"]})
    items = client.get("/api/queue").json()["items"]
    db.update_queue_item(items[1]["id"], status="analyzing")
    assert db.requeue_interrupted_items() == 1
    assert db.claim_next_queue_item()["project_id"] == "proj_r2"


def test_analyses_never_run_in_parallel():
    """The old «Знайти моменти» button and the queue share one lock: one video at a time on the GPU."""
    import threading
    inside = []
    peak = []

    def slow(*a, **k):
        inside.append(1)
        peak.append(len(inside))
        time.sleep(0.05)
        inside.pop()

    _project("proj_l")
    with patch.object(job_runner, "_run_analyze_worker_locked", slow):
        ts = [threading.Thread(target=job_runner._run_analyze_worker, args=(f"j{i}", "proj_l", 5, "Viral Hooks"))
              for i in range(3)]
        for i in range(3):
            db.create_job(f"j{i}", "proj_l", "analyze")
        [t.start() for t in ts]
        [t.join() for t in ts]
    assert max(peak) == 1


# ─── Settings ─────────────────────────────────────────────────────────────────

def test_settings_roundtrip_changes_running_config():
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["values"]["clips"]["default_max_len"] == config_loader.app_config.clips.default_max_len

    r = client.post("/api/settings", json={"clips": {"default_max_len": 45}, "studio": {"default_clips": 12, "cleanup_days": 30}})
    assert r.status_code == 200
    assert config_loader.app_config.clips.default_max_len == 45
    assert config_loader.CONFIG["clips"]["default_max_len"] == 45            # dict view refreshed too
    assert job_runner.app_config.clips.default_max_len == 45                 # same object everywhere
    saved = json.loads(config_loader.USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    assert saved == {"clips": {"default_max_len": 45}, "studio": {"default_clips": 12, "cleanup_days": 30}}

    # survives a reload (studio restart)
    assert config_loader._load_app_config().clips.default_max_len == 45

    r = client.post("/api/settings/reset")
    assert r.status_code == 200
    assert not config_loader.USER_SETTINGS_PATH.exists()
    assert config_loader.app_config.studio.default_clips == 0


def test_settings_validation_errors_are_friendly():
    r = client.post("/api/settings", json={"clips": {"default_min_len": 50, "default_max_len": 40}})
    assert r.status_code == 400 and "меншою за максимальну" in r.json()["detail"]
    assert client.post("/api/settings", json={"logging": {"level": "DEBUG"}}).status_code == 400
    assert client.post("/api/settings", json={"clips": {"max_count": 5}}).status_code == 400
    assert client.post("/api/settings", json={"studio": {"default_filter": "Хаос"}}).status_code == 400
    assert not config_loader.USER_SETTINGS_PATH.exists()


def test_settings_whisper_model_change_reloads_model():
    with patch("state.app_state.reset_whisper") as reset:
        client.post("/api/settings", json={"whisper": {"model": "medium"}})
        assert reset.called
    assert config_loader.CONFIG["whisper"]["model"] == "medium"


def test_settings_gemini_key_written_to_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "old")
    config_loader.ENV_PATH.write_text("OTHER=1\nGEMINI_API_KEY=old\n", encoding="utf-8")
    assert client.post("/api/settings", json={"gemini_api_key": "short"}).status_code == 400
    r = client.post("/api/settings", json={"gemini_api_key": "AIzaSyTESTKEY1234567890abcd"})
    assert r.status_code == 200
    assert r.json()["gemini_key"] == {"set": True, "hint": "…abcd"}
    assert config_loader.ENV_PATH.read_text(encoding="utf-8") == "OTHER=1\nGEMINI_API_KEY=AIzaSyTESTKEY1234567890abcd\n"
    assert os.environ["GEMINI_API_KEY"] == "AIzaSyTESTKEY1234567890abcd"


def test_broken_user_settings_file_does_not_stop_studio():
    config_loader.USER_SETTINGS_PATH.write_text('{"clips": {"default_min_len": 999}}', encoding="utf-8")
    cfg = config_loader._load_app_config()
    assert cfg.clips.default_min_len < cfg.clips.default_max_len


# ─── Disk space ───────────────────────────────────────────────────────────────

def _old(path: Path, days=40):
    t = time.time() - days * 86400
    for root, dirs, files in os.walk(path) if path.is_dir() else [(str(path.parent), [], [path.name])]:
        for f in files:
            os.utime(os.path.join(root, f), (t, t))
    os.utime(path, (t, t))


def test_storage_scan_and_cleanup(monkeypatch, tmp_path):
    monkeypatch.setitem(config_loader.CONFIG["cache"], "dir", str(tmp_path / "tcache"))
    proj = _project("proj_disk")
    out = server.OUTPUT_DIR / "proj_disk"
    (out / "slices").mkdir(parents=True)
    (out / "slices" / "clip_1.mp4").write_bytes(b"x" * 5000)
    (out / "vertical").mkdir()
    (out / "vertical" / "v.mp4").write_bytes(b"x" * 3000)
    (out / "thumbnail.jpg").write_bytes(b"x" * 10)
    # junk: folder of a deleted project, an old half-finished download, a fresh one (kept)
    (server.OUTPUT_DIR / "deleted_proj").mkdir()
    (server.OUTPUT_DIR / "deleted_proj" / "a.mp4").write_bytes(b"x" * 700)
    old_tmp = server.UPLOADS_DIR / "temp_url_dead"
    old_tmp.mkdir()
    (old_tmp / "part.mp4").write_bytes(b"x" * 400)
    _old(old_tmp)
    fresh_tmp = server.UPLOADS_DIR / "temp_url_live"
    fresh_tmp.mkdir()
    (fresh_tmp / "part.mp4").write_bytes(b"x" * 100)

    r = client.get("/api/storage").json()
    assert r["totals"]["clips"] == 5000 and r["totals"]["vertical"] == 3000
    assert r["totals"]["sources"] == 2048
    assert r["totals"]["junk"] == 1100
    assert r["projects"][0]["id"] == "proj_disk" and r["projects"][0]["clips_bytes"] == 8000

    r = client.post("/api/storage/cleanup", json={"junk": True}).json()
    assert r["freed_bytes"] == 1100
    assert fresh_tmp.exists() and not old_tmp.exists() and not (server.OUTPUT_DIR / "deleted_proj").exists()
    assert (out / "slices" / "clip_1.mp4").exists()

    # clips younger than 30 days stay, older go; the project itself is untouched
    r = client.post("/api/storage/cleanup", json={"clips": True, "older_than_days": 30}).json()
    assert r["freed_bytes"] == 0
    _old(out / "slices")
    _old(out / "vertical")
    r = client.post("/api/storage/cleanup", json={"clips": True, "older_than_days": 30}).json()
    assert r["freed_bytes"] == 8000
    assert (out / "thumbnail.jpg").exists() and os.path.exists(proj["source_path"])
    assert r["storage"]["totals"]["clips"] == 0


def test_storage_cleanup_single_project_and_validation():
    _project("proj_one")
    _project("proj_two")
    for pid in ("proj_one", "proj_two"):
        d = server.OUTPUT_DIR / pid / "slices"
        d.mkdir(parents=True)
        (d / "c.mp4").write_bytes(b"x" * 100)
    r = client.post("/api/storage/cleanup", json={"clips": True, "project_ids": ["proj_one"]}).json()
    assert r["freed_bytes"] == 100
    assert (server.OUTPUT_DIR / "proj_two" / "slices" / "c.mp4").exists()
    assert client.post("/api/storage/cleanup", json={}).status_code == 400
    assert client.post("/api/storage/cleanup", json={"clips": True, "project_ids": ["../x"]}).status_code == 400


def test_orphan_transcripts_are_junk(monkeypatch, tmp_path):
    cache = tmp_path / "tcache"
    monkeypatch.setitem(config_loader.CONFIG["cache"], "dir", str(cache))
    proj = _project("proj_tr")
    from transcription import _cache_path
    live = _cache_path(proj["source_path"])
    live.write_text("{}")
    dead = cache / ("f" * 64 + "_large-v3-turbo.json")
    dead.write_text("{}" * 50)
    r = client.get("/api/storage").json()
    assert r["totals"]["junk"] == dead.stat().st_size
    assert r["totals"]["transcripts"] == live.stat().st_size
    client.post("/api/storage/cleanup", json={"junk": True})
    assert live.exists() and not dead.exists()


def test_auto_cleanup_follows_setting(monkeypatch):
    from backend import storage
    assert storage.auto_cleanup(server.UPLOADS_DIR, server.OUTPUT_DIR) is None   # off by default
    client.post("/api/settings", json={"studio": {"cleanup_days": 7}})
    _project("proj_auto")
    d = server.OUTPUT_DIR / "proj_auto" / "slices"
    d.mkdir(parents=True)
    (d / "c.mp4").write_bytes(b"x" * 10)
    _old(d, days=10)
    res = storage.auto_cleanup(server.UPLOADS_DIR, server.OUTPUT_DIR)
    assert res["freed_bytes"] >= 10 and not d.exists()


def test_tests_never_touch_the_real_transcripts(tmp_path):
    """The disk cleanup deletes transcripts the database does not know; in tests both must be temporary,
    also after a settings save rebuilds CONFIG from config.yaml."""
    import transcription
    from backend import storage
    client.post("/api/settings", json={"studio": {"cleanup_days": 7}})
    for d in (transcription.transcript_cache_dir(), storage._transcript_dir(), storage._RENDER_TMP):
        assert d.resolve().is_relative_to(tmp_path.resolve()), d

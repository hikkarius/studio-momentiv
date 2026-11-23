"""«Прибрати відео, з якими закінчив»: only the long video goes; clips, marks, texts and transcripts stay."""
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

import transcription
from backend import db, server, storage
from backend.server import app

client = TestClient(app)


def _make(pid, statuses, source=None, duration=300.0):
    """A 5-min project (best 5 clips in «Нові», the rest in «Запас») with a source file and a clip file."""
    src = source or str(server.UPLOADS_DIR / f"{pid}.mp4")
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    if not os.path.exists(src):
        Path(src).write_bytes(os.urandom(4096))
    db.create_project(pid, f"{pid}.mp4", src, duration)
    db.save_candidate_clips(pid, [{"start": i * 30.0, "end": i * 30.0 + 20, "title": f"t{i}", "score": 90 - i}
                                  for i in range(len(statuses))])
    for i, s in enumerate(statuses, 1):
        if s:
            db.update_clip_fields(pid, i, review_status=s)
    d = server.OUTPUT_DIR / pid / "slices"
    d.mkdir(parents=True, exist_ok=True)
    (d / "c.mp4").write_bytes(b"x" * 500)
    return src


def _transcript(src):
    path = transcription._cache_path(src)
    path.write_text(json.dumps({"segments": [], "words": [{"start": 1.0, "end": 1.5, "word": "привіт"}], "lang": "uk"}),
                    encoding="utf-8")
    return path


def test_finished_rule():
    assert storage.is_finished({"clips_count": 5, "new_count": 0, "kept_count": 0})
    assert not storage.is_finished({"clips_count": 5, "new_count": 1, "kept_count": 0})
    assert not storage.is_finished({"clips_count": 5, "new_count": 0, "kept_count": 2})
    assert not storage.is_finished({"clips_count": 0, "new_count": 0, "kept_count": 0})


def test_scan_marks_finished_videos():
    _make("rm_done", ["done", "drop", "done", "drop", "done", "", ""])   # 2 clips left in «Запас»: still finished
    _make("rm_busy", ["done", "keep", "drop", "drop", "drop"])
    _make("rm_new", ["done", "", "drop", "drop", "drop"])
    scan = client.get("/api/storage").json()
    by = {p["id"]: p for p in scan["projects"]}
    assert by["rm_done"]["finished"] and by["rm_done"]["removable"] and by["rm_done"]["reserve_count"] == 2
    assert not by["rm_busy"]["finished"] and not by["rm_new"]["finished"]
    assert by["rm_new"]["new_count"] == 1 and by["rm_busy"]["kept_count"] == 1
    assert scan["finished_count"] == 1 and scan["finished_bytes"] >= 4096 + 500


def test_remove_keeps_marks_texts_and_transcript():
    src = _make("rm_one", ["done", "drop", "done", "drop", "done"])
    tr = _transcript(src)
    res = client.post("/api/storage/remove-sources", json={"project_ids": ["rm_one"]})
    assert res.status_code == 200, res.text
    assert res.json()["removed"] == 1 and res.json()["freed_bytes"] >= 4096 + 500
    assert not os.path.exists(src) and not (server.OUTPUT_DIR / "rm_one" / "slices").exists()
    assert db.get_project("rm_one")["source_removed"] == 1
    assert [c["review_status"] for c in db.get_candidate_clips("rm_one")] == ["done", "drop", "done", "drop", "done"]
    # the transcript stays and is still found for the removed video, also after «Прибрати сміття»
    client.post("/api/storage/cleanup", json={"junk": True})
    assert tr.exists() and transcription.load_transcript_cache(src)[1][0]["word"] == "привіт"
    # the project stays in the list, marked
    entry = next(p for p in client.get("/api/storage").json()["projects"] if p["id"] == "rm_one")
    assert entry["source_removed"] and not entry["removable"]
    # downloads say why they cannot work
    for url in ("/api/projects/rm_one/clips/1/download", "/api/projects/rm_one/clips/1/vertical"):
        r = client.get(url)
        assert r.status_code == 410 and "прибрано" in r.json()["detail"]


def test_remove_refuses_unfinished_busy_outside_and_shared(tmp_path):
    _make("rf_new", ["", "done", "drop", "drop", "drop"])
    r = client.post("/api/storage/remove-sources", json={"project_ids": ["rf_new"]})
    assert r.status_code == 400 and "Нових" in r.json()["detail"]

    outside = tmp_path / "elsewhere.mp4"
    _make("rf_out", ["done"] * 5, source=str(outside))
    assert client.post("/api/storage/remove-sources", json={"project_ids": ["rf_out"]}).status_code == 400
    assert outside.exists()

    shared = _make("rf_a", ["done"] * 5)
    _make("rf_b", ["keep", "", "", "", ""], source=shared)
    assert client.post("/api/storage/remove-sources", json={"project_ids": ["rf_a"]}).status_code == 400
    assert os.path.exists(shared)

    # nothing is removed when one video of the batch may not be
    good = _make("rf_good", ["done"] * 5)
    assert client.post("/api/storage/remove-sources", json={"project_ids": ["rf_good", "rf_new"]}).status_code == 400
    assert os.path.exists(good)

    assert client.post("/api/storage/remove-sources", json={"project_ids": ["../etc"]}).status_code in (400, 422)
    assert client.post("/api/storage/remove-sources", json={"project_ids": []}).status_code == 422


def test_remove_refuses_a_video_in_the_queue(monkeypatch):
    src = _make("rf_busy", ["done"] * 5)
    monkeypatch.setattr(db, "busy_project_ids", lambda: ["rf_busy"])
    r = client.post("/api/storage/remove-sources", json={"project_ids": ["rf_busy"]})
    assert r.status_code == 400 and "черзі" in r.json()["detail"]
    assert os.path.exists(src)


def test_upload_again_clears_the_mark():
    src = _make("rm_back", ["done"] * 5)
    client.post("/api/storage/remove-sources", json={"project_ids": ["rm_back"]})
    Path(src).write_bytes(b"\0" * 2048)
    db.create_project("rm_back", "rm_back.mp4", src, 300.0)
    assert db.get_project("rm_back")["source_removed"] == 0

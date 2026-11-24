"""Step 2 «Усі разом»: named templates, which 9:16 files are ready, «Зробити незроблені» in the background."""
import time
import uuid

from fastapi.testclient import TestClient

import vertical_render
from backend import db, server
from backend.server import app

client = TestClient(app)


def _project(n=3):
    pid = f"test_vlist_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "Подкаст", "/tmp/none.mp4", 600)
    db.save_candidate_clips(pid, [{"start": i * 40.0, "end": i * 40.0 + 25, "title": f"К{i}", "score": 90 - i} for i in range(n)])
    for i in range(1, n + 1):
        db.update_clip_fields(pid, i, review_status="keep")
    return pid


def test_template_keeps_clip_own_words_and_title():
    own = {"style": "kar", "hook": True, "hook_text": "Мій гачок", "edits": {"12.30": {"w": "слово"}}, "size": 1.2}
    tpl = vertical_render.template_of({"style": "one", "color": "FF4D4D", "hook": False, "hook_text": "чужий", "edits": {"1.00": {"w": "x"}}})
    assert "hook_text" not in tpl and "edits" not in tpl
    out = vertical_render.apply_template(own, tpl)
    assert out["style"] == "one" and out["color"] == "FF4D4D" and out["hook"] is False and out["size"] == 1.0
    assert out["hook_text"] == "Мій гачок" and out["edits"] == {"12.30": {"w": "слово"}}


def test_templates_save_overwrite_delete():
    r = client.put("/api/vertical-templates", json={"name": "  Подкаст  ", "opts": {"style": "hl", "color": "FF4D4D", "edits": {"1.00": {"w": "x"}}}})
    assert r.status_code == 200
    t = r.json()["templates"]
    assert [x["name"] for x in t] == ["Подкаст"] and t[0]["opts"]["color"] == "FF4D4D" and "edits" not in t[0]["opts"]
    client.put("/api/vertical-templates", json={"name": "Подкаст", "opts": {"style": "min"}})
    assert client.get("/api/vertical-templates").json()["templates"][0]["opts"]["style"] == "min"
    assert client.put("/api/vertical-templates", json={"name": "a/b", "opts": {}}).status_code == 422
    assert client.delete("/api/vertical-templates/Подкаст").json()["templates"] == []
    assert client.delete("/api/vertical-templates/Подкаст").status_code == 404


def test_apply_template_to_all_kept_and_status_shows_it():
    pid = _project()
    db.update_clip_fields(pid, 2, vertical_opts=vertical_render.normalize_opts({"hook": True, "hook_text": "Гачок 2"}))
    db.update_clip_fields(pid, 3, review_status="drop")
    client.put("/api/vertical-templates", json={"name": "Стрім", "opts": {"style": "one", "hook": True}})
    r = client.post(f"/api/projects/{pid}/apply-template", json={"name": "Стрім", "clips": []})
    assert r.status_code == 200 and len(r.json()["clips"]) == 2  # the dropped clip is left alone
    clips = {c["clip_index"]: c for c in db.get_candidate_clips(pid)}
    assert clips[1]["vertical_opts"]["style"] == "one" and clips[2]["vertical_opts"]["hook_text"] == "Гачок 2"
    assert clips[3]["vertical_opts"] == {}
    st = client.get(f"/api/projects/{pid}/vertical-status").json()
    assert [(c["clip_index"], c["ready"], c["template"]) for c in st["clips"]] == [(1, False, "Стрім"), (2, False, "Стрім")]
    assert st["job"] is None
    assert client.post(f"/api/projects/{pid}/apply-template", json={"name": "Нема", "clips": []}).status_code == 404


def test_batch_renders_only_missing_and_status_turns_ready(monkeypatch):
    pid = _project()
    made = []

    def fake_ensure(project_id, proj, clip, subs):
        path, name = server._vertical_target(project_id, clip, subs)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"mp4")
        made.append(clip["clip_index"])
        return str(path), name
    monkeypatch.setattr(server, "_ensure_vertical", fake_ensure)
    # clip 1 is already made with its current settings
    c1 = next(c for c in db.get_candidate_clips(pid) if c["clip_index"] == 1)
    fake_ensure(pid, None, c1, True)
    made.clear()
    job = client.post(f"/api/projects/{pid}/render-vertical", json={"clips": []}).json()
    assert job["total"] == 2 and job["kind"] == "batch"
    for _ in range(50):
        if server._vjobs[job["id"]]["status"] != "running":
            break
        time.sleep(0.05)
    assert server._vjobs[job["id"]]["status"] == "completed" and sorted(made) == [2, 3]
    st = client.get(f"/api/projects/{pid}/vertical-status").json()
    assert all(c["ready"] for c in st["clips"])
    r = client.post(f"/api/projects/{pid}/render-vertical", json={"clips": []})
    assert r.status_code == 400 and "вже зроблені" in r.json()["detail"]
    # a changed look makes the clip «Не зроблено» again
    db.update_clip_fields(pid, 2, vertical_opts=vertical_render.normalize_opts({"style": "min"}))
    st = {c["clip_index"]: c["ready"] for c in client.get(f"/api/projects/{pid}/vertical-status").json()["clips"]}
    assert st == {1: True, 2: False, 3: True}

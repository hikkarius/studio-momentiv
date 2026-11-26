"""
tests/test_top3.py — «🏆 Топ-3»: short clips glued into one countdown video (#3 → #1).
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import db, server, top3
from backend.server import app

client = TestClient(app)
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def test_overlay_has_the_number_and_the_title_only_where_asked():
    first = top3.overlay_ass(1080, 1920, 3, "Топ-3 моменти")
    assert "#3" in first and "ТОП-3 МОМЕНТИ" in first and "PlayResY: 1920" in first
    plain = top3.overlay_ass(640, 360, None, "")
    assert "Dialogue" not in plain


def _video(path: Path, sec: float, audio: bool = True):
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10:duration={sec}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=300:duration={sec}", "-c:a", "aac"]
    subprocess.run(cmd + ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True, timeout=60)
    return str(path)


@needs_ffmpeg
def test_build_joins_files_and_source_cuts_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(top3.vertical_render, "RENDER_TMP", tmp_path / "work")
    a = _video(tmp_path / "a.mp4", 3)
    b = _video(tmp_path / "b.mp4", 2, audio=False)  # a silent clip still joins
    src = _video(tmp_path / "src.mp4", 10)
    out = top3.build([a, b, (src, 4.0, 2.5)], str(tmp_path / "top.mp4"), title="Топ", numbers=True)
    w, h, dur = top3.probe(out)
    assert (w, h) == (320, 180)
    assert abs(dur - 7.5) < 0.4
    assert not list((tmp_path / "work").glob("*"))  # temp files cleaned


def _project(tmp_path):
    src = server.UPLOADS_DIR / "t.mp4"
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("ffmpeg"):
        _video(Path(src), 120)
    else:
        Path(src).write_bytes(b"\0" * 2048)
    db.create_project("p_top", "Відео", str(src), 120.0)
    db.save_candidate_clips("p_top", [
        {"start": 5.0, "end": 15.0, "score": 80, "title": "Перший"},
        {"start": 20.0, "end": 32.0, "score": 90, "title": "Другий"},
        {"start": 40.0, "end": 50.0, "score": 70, "title": "Третій"},
        {"start": 0.0, "end": 110.0, "score": 95, "title": "Історія", "kind": "story", "split": 55.0},
    ])
    db.update_project_status("p_top", "ready")


def test_top_request_is_checked(tmp_path):
    _project(tmp_path)
    assert client.post("/api/projects/p_top/top3", json={"clips": [1]}).status_code == 422
    assert client.post("/api/projects/p_top/top3", json={"clips": [1, 1]}).status_code == 400
    assert client.post("/api/projects/p_top/top3", json={"clips": [1, 99]}).status_code == 404
    r = client.post("/api/projects/p_top/top3", json={"clips": [1, 4]})
    assert r.status_code == 400 and "історія" in r.json()["detail"].lower()


def test_top_longer_than_three_minutes_is_refused(tmp_path):
    _project(tmp_path)
    db.update_clip_bounds("p_top", 1, 0.0, 100.0)
    db.update_clip_bounds("p_top", 2, 0.0, 100.0)
    r = client.post("/api/projects/p_top/top3", json={"clips": [1, 2]})
    assert r.status_code == 400 and "3 хв" in r.json()["detail"]


@needs_ffmpeg
def test_top_is_rendered_in_the_background_and_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr(top3.vertical_render, "RENDER_TMP", tmp_path / "work")
    _project(tmp_path)
    r = client.post("/api/projects/p_top/top3", json={"clips": [3, 1, 2], "title": "Мій топ", "vertical": False})
    assert r.status_code == 200
    job = r.json()
    for _ in range(300):
        job = client.get(f"/api/vertical-jobs/{job['id']}").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "completed", job
    f = client.get(f"/api/vertical-jobs/{job['id']}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "video/mp4"
    assert "16x9" in f.headers["content-disposition"]
    out = tmp_path / "got.mp4"
    out.write_bytes(f.content)
    assert abs(top3.probe(str(out))[2] - 32.0) < 0.5

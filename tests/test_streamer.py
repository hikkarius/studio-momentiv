"""
tests/test_streamer.py — «Стрім: вебка + гра»: finding the webcam rectangle, the three layouts,
the ffmpeg graph (a real tiny render of each layout) and the /stream API.
"""
import shutil
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

import streamer
import vertical_render as vr
from backend import db, server
from backend.server import app

client = TestClient(app)
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")

CAM = [0.02, 0.70, 0.24, 0.26]   # bottom-left webcam
FACE = [0.14, 0.82]


# ─── Finding the webcam ──────────────────────────────────────────────────────

def _stream_frames(n=16, H=180, W=320, cam=(10, 126, 87, 172), seed=1):
    """Grey frames: a changing «game» everywhere, a calm «room» inside the webcam box with a framed border."""
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = cam
    frames = []
    for i in range(n):
        f = rng.uniform(0, 255, (H, W)).astype(np.float32)          # game: different every frame
        f = (f + np.roll(f, 1, 0) + np.roll(f, 1, 1)) / 3
        room = np.full((y1 - y0, x1 - x0), 90.0, dtype=np.float32)
        room[10:30, 30 + i % 3: 55 + i % 3] = 170                  # the face moves a little
        f[y0:y1, x0:x1] = room
        f[y0:y1, x0] = f[y0:y1, x1 - 1] = 250                       # the overlay border
        f[y0, x0:x1] = f[y1 - 1, x0:x1] = 250
        frames.append(f)
    return np.stack(frames)


def _faces_at(cx, cy, n, w=0.04, h=0.1):
    return [(cx + 0.002 * (i % 2), cy, w, h, i) for i in range(n)]


def test_find_cam_box_finds_the_framed_webcam():
    frames = _stream_frames()
    W, H = 320, 180
    faces = _faces_at((10 + 43) / W, (126 + 20) / H, len(frames))
    out = streamer.find_cam_box(faces, frames)
    x, y, w, h = out["cam"]
    assert abs(x * W - 10) <= 2 and abs(y * H - 126) <= 2
    assert abs((x + w) * W - 87) <= 2 and abs((y + h) * H - 172) <= 2
    assert out["face"] is not None and out["note"] == ""


def test_find_cam_box_needs_a_face_in_one_place():
    frames = _stream_frames()
    wandering = [(0.1 + 0.08 * i, 0.5, 0.05, 0.1, i) for i in range(len(frames))]
    out = streamer.find_cam_box(wandering, frames)
    assert out["cam"] is None and "не знайдено" in out["note"]
    assert streamer.find_cam_box([], frames)["cam"] is None


def test_find_cam_box_skips_ordinary_videos_with_a_big_face():
    frames = _stream_frames()
    out = streamer.find_cam_box(_faces_at(0.5, 0.45, len(frames), w=0.2, h=0.45), frames)
    assert out["cam"] is None and "звичайне відео" in out["note"]


def test_clean_cam():
    assert streamer.clean_cam([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]
    assert streamer.clean_cam([0.9, 0.9, 0.5, 0.5]) == [0.9, 0.9, 0.1, 0.1]  # kept inside the frame
    assert streamer.clean_cam(None) is None and streamer.clean_cam([1, 2]) is None and streamer.clean_cam(["a", 0, 0, 0]) is None


# ─── Layouts ─────────────────────────────────────────────────────────────────

def _inside(src):
    x, y, w, h = src
    return x >= -1e-6 and y >= -1e-6 and x + w <= 1 + 1e-4 and y + h <= 1 + 1e-4


def test_plan_top_fills_the_phone_and_puts_subtitles_on_the_seam():
    sp = streamer.plan(CAM, FACE, 1920, 1080, "top")
    cam, game = sp["parts"]
    assert cam["role"] == "cam" and game["role"] == "game"
    assert cam["dst"][3] + game["dst"][3] == 1920 and game["dst"][1] == cam["dst"][3]
    assert sp["pos"] == pytest.approx(100 * cam["dst"][3] / 1920, abs=0.1)
    for p in sp["parts"]:
        assert _inside(p["src"])
        # the piece keeps the phone part's proportions (nothing stretched)
        src_aspect = p["src"][2] * 1920 / (p["src"][3] * 1080)
        assert src_aspect == pytest.approx(p["dst"][2] / p["dst"][3], rel=0.01)
    # the game crop is centred on the game (middle of the frame), the webcam crop stays inside the webcam
    assert game["src"][0] + game["src"][2] / 2 == pytest.approx(0.5, abs=0.01)
    assert cam["src"][0] >= CAM[0] and cam["src"][0] + cam["src"][2] <= CAM[0] + CAM[2] + 1e-4
    assert sp["blur"][0] <= CAM[0] and sp["blur"][2] >= CAM[2]


def test_plan_share_game_x_and_other_layouts():
    big = streamer.plan(CAM, FACE, 1920, 1080, "top", share=0.45)
    assert big["parts"][0]["dst"][3] == 864
    moved = streamer.plan(CAM, FACE, 1920, 1080, "top", game_x=0.7)
    g = moved["parts"][1]["src"]
    assert g[0] + g[2] / 2 == pytest.approx(0.7, abs=0.01)
    edge = streamer.plan(CAM, FACE, 1920, 1080, "top", game_x=1.0)
    assert _inside(edge["parts"][1]["src"])

    full = streamer.plan(CAM, FACE, 1920, 1080, "full")
    assert full["bg"] and full["parts"][1]["src"] == [0, 0, 1, 1] and full["parts"][1]["dst"][2:] == [1080, 608]
    assert full["pos"] > 100 * (full["parts"][0]["dst"][3] + 608) / 1920

    pip = streamer.plan(CAM, FACE, 1920, 1080, "pip")
    game, cam = pip["parts"]
    assert game["dst"] == [0, 0, 1080, 1920] and cam["border"] > 0
    assert pip["hook_pos"] > 100 * (cam["dst"][1] + cam["dst"][3]) / 1920  # the title goes under the window

    none = streamer.plan(None, None, 1920, 1080, "top")
    assert none["layout"] == "game" and len(none["parts"]) == 1 and none["blur"] is None


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["top", "full", "pip", None])
def test_render_each_layout_real(tmp_path, layout):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True, timeout=60)
    sp = streamer.plan(CAM if layout else None, FACE, 640, 360, layout or "top")
    out = tmp_path / "v.mp4"
    vr.render_vertical(str(src), 0, 2, str(out), vr.Layout("stream", stream=sp), None)
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(out)], capture_output=True, text=True, timeout=30).stdout.strip()
    assert info == "1080,1920"


def test_normalize_stream_opts():
    o = vr.normalize_opts({"frame": "stream", "stream_layout": "pip", "cam_share": 0.9})
    assert o["frame"] == "stream" and o["stream_layout"] == "pip" and o["cam_share"] == 0.5
    o = vr.normalize_opts({"stream_layout": "weird", "cam_share": "x"})
    assert o["stream_layout"] == "top" and o["cam_share"] is None


# ─── API ─────────────────────────────────────────────────────────────────────

def _project(tmp_path):
    pid = f"strm_{uuid.uuid4().hex[:8]}"
    src = tmp_path / "stream.mp4"
    src.write_bytes(b"x" * 2000)
    db.create_project(pid, "Стрім", str(src), 60.0)
    db.save_candidate_clips(pid, [{"start": 10.0, "end": 15.0, "title": "1 проти 3", "hook": "h", "reason": "r", "score": 90}])
    return pid, db.get_candidate_clips(pid)[0]["clip_index"]


def test_stream_api_detects_once_moves_frame_and_renders(tmp_path, monkeypatch):
    pid, idx = _project(tmp_path)
    calls = {"detect": 0, "render": []}

    def fake_detect(source, **k):
        calls["detect"] += 1
        return {"cam": CAM, "face": FACE, "w": 1920, "h": 1080, "note": ""}

    def fake_render(source, start, end, output_path, layout, ass_text, has_nvenc=False, timeout=900, cuts_rel=None):
        calls["render"].append(layout)
        Path(output_path).write_bytes(b"mp4" * 1000)
        return output_path

    monkeypatch.setattr(streamer, "detect_webcam", fake_detect)
    monkeypatch.setattr(vr, "detect_faces", lambda *a, **k: pytest.fail("faces are not needed for a stream"))
    monkeypatch.setattr(vr, "render_vertical", fake_render)
    monkeypatch.setattr(server, "load_transcript_cache", lambda *a, **k: ([], [], "uk"))

    info = client.get(f"/api/projects/{pid}/stream").json()
    assert info["cam"] == CAM and info["auto"] is True and info["game_x"] == 0.5
    client.get(f"/api/projects/{pid}/stream")
    assert calls["detect"] == 1  # kept in vertical/stream.json

    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan?frame=stream&stream_layout=full").json()
    assert plan["layout"]["kind"] == "stream" and plan["layout"]["stream"]["layout"] == "full"
    assert plan["src"] == {"w": 1920, "h": 1080}

    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"frame": "stream", "stream_layout": "top"}})
    assert client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false").status_code == 200
    assert calls["render"][-1].kind == "stream" and calls["render"][-1].stream["layout"] == "top"

    # moving the frame: saved, marked as the user's, and a new render (the old file no longer matches)
    r = client.put(f"/api/projects/{pid}/stream", json={"cam": [0.7, 0.7, 0.28, 0.28], "game_x": 0.4})
    assert r.status_code == 200 and r.json()["auto"] is False and r.json()["face"] is None
    client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false")
    assert len(calls["render"]) == 2
    r = client.put(f"/api/projects/{pid}/stream", json={"keep_cam": True, "game_x": 0.6})
    assert r.json()["cam"] == [0.7, 0.7, 0.28, 0.28] and r.json()["game_x"] == 0.6

    # no webcam: the game only, and a clear note
    client.put(f"/api/projects/{pid}/stream", json={"cam": None})
    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan?frame=stream").json()
    assert plan["layout"]["stream"]["layout"] == "game" and "лише гру" in plan["layout"]["note"]

    client.post(f"/api/projects/{pid}/stream/redetect")
    assert calls["detect"] == 2
    assert client.get(f"/api/projects/{pid}/stream").json()["game_x"] == 0.6  # kept across a new search


def test_stream_api_unknown_project():
    assert client.get("/api/projects/nope_123/stream").status_code == 404

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
    sp = streamer.plan(CAM, FACE, 1920, 1080)
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
    big = streamer.plan(CAM, FACE, 1920, 1080, {"share": 0.45})
    assert big["parts"][0]["dst"][3] == 864
    moved = streamer.plan(CAM, FACE, 1920, 1080, {"game_x": 0.7})
    g = moved["parts"][1]["src"]
    assert g[0] + g[2] / 2 == pytest.approx(0.7, abs=0.01)
    edge = streamer.plan(CAM, FACE, 1920, 1080, {"game_x": 1.0})
    assert _inside(edge["parts"][1]["src"])

    full = streamer.plan(CAM, FACE, 1920, 1080, {"layout": "full"})
    assert full["bg"] and full["parts"][1]["src"] == [0, 0, 1, 1] and full["parts"][1]["dst"][2:] == [1080, 608]
    assert full["pos"] > 100 * (full["parts"][0]["dst"][3] + 608) / 1920

    pip = streamer.plan(CAM, FACE, 1920, 1080, {"layout": "pip"})
    game, cam = pip["parts"]
    assert game["dst"] == [0, 0, 1080, 1920] and cam["border"] > 0
    assert pip["hook_pos"] > 100 * (cam["dst"][1] + cam["dst"][3]) / 1920  # the title goes under the window

    none = streamer.plan(None, None, 1920, 1080)
    assert none["layout"] == "game" and len(none["parts"]) == 1 and none["blur"] is None


def test_clean_look_ranges():
    lk = streamer.clean_look({"layout": "weird", "share": 5, "cam_zoom": 0, "game_zoom": "x", "cam_fx": None,
                              "pip_w": 0.1, "frame": "pink", "nick": "  {\\b1}Лебіга\n  " + "x" * 40, "round": 1})
    assert lk["layout"] == "top" and lk["share"] == 0.6 and lk["cam_zoom"] == 1.0 and lk["game_zoom"] == 1.0
    assert lk["cam_fx"] is None and lk["pip_w"] == 0.25 and lk["frame"] == "white" and lk["round"] is True
    assert "{" not in lk["nick"] and "\\" not in lk["nick"] and len(lk["nick"]) == 30
    # a partial change keeps the rest of the base
    assert streamer.clean_look({"cam_zoom": 2}, {"layout": "pip", "pip_w": 0.6}) == {
        **streamer.LOOK_DEFAULT, "layout": "pip", "pip_w": 0.6, "cam_zoom": 2.0}


def test_plan_zoom_pan_window_and_styling():
    base = streamer.plan(CAM, FACE, 1920, 1080)
    z = streamer.plan(CAM, FACE, 1920, 1080, {"cam_zoom": 2, "game_zoom": 2})
    for i in (0, 1):  # twice closer = half the piece, same place on the phone
        assert z["parts"][i]["src"][2] == pytest.approx(base["parts"][i]["src"][2] / 2, rel=0.01)
        assert z["parts"][i]["dst"] == base["parts"][i]["dst"]
    # panning inside the webcam: the crop follows cam_fx/cam_fy and stays inside the webcam
    left = streamer.plan(CAM, FACE, 1920, 1080, {"cam_zoom": 2, "cam_fx": 0.0, "cam_fy": 0.0})["parts"][0]["src"]
    right = streamer.plan(CAM, FACE, 1920, 1080, {"cam_zoom": 2, "cam_fx": 1.0, "cam_fy": 1.0})["parts"][0]["src"]
    assert left[0] < right[0] and left[1] < right[1]
    for c in (left, right):
        assert c[0] >= CAM[0] and c[0] + c[2] <= CAM[0] + CAM[2] + 1e-4
    low = streamer.plan(CAM, FACE, 1920, 1080, {"game_zoom": 2, "game_y": 1.0})["parts"][1]["src"]
    assert low[1] + low[3] == pytest.approx(1.0, abs=1e-3)

    pip = streamer.plan(CAM, FACE, 1920, 1080, {"layout": "pip", "pip_w": 0.6, "pip_x": 1, "pip_y": 1,
                                                 "round": True, "frame": "gold", "nick": "lebiga"})
    cam = pip["parts"][1]
    assert cam["dst"][2] == 648 + 12 and cam["dst"][0] + cam["dst"][2] == 1080 and cam["dst"][1] + cam["dst"][3] == 1920
    assert cam["radius"] > 0 and cam["color"] == "E6C068" and pip["hook_pos"] is None
    assert pip["nick"]["text"] == "lebiga" and pip["nick"]["an"] == 1 and pip["nick"]["y"] < cam["dst"][1]  # above a bottom window
    up = streamer.plan(CAM, FACE, 1920, 1080, {"layout": "pip", "nick": "x"})
    assert up["nick"]["an"] == 7 and up["nick"]["y"] > up["parts"][1]["dst"][1] + up["parts"][1]["dst"][3]
    plain = streamer.plan(CAM, FACE, 1920, 1080, {"layout": "pip", "frame": "none"})
    assert plain["parts"][1]["border"] == 0 and plain["parts"][1]["color"] is None

    top = streamer.plan(CAM, FACE, 1920, 1080, {"divider": True, "frame": "cam", "nick": "lebiga"})
    ch = top["parts"][0]["dst"][3]
    assert top["divider"] == [0, ch - 3, 1080, 6] and top["divider_color"] == "9146FF"
    assert top["nick"] == {"text": "lebiga", "x": 36, "y": 48, "an": 7}
    assert base["divider"] is None and base["nick"] is None


def test_nick_ass_adds_to_subtitles_or_makes_its_own():
    sp = {"nick": {"text": "lebiga", "x": 36, "y": 600, "an": 1}}
    own = streamer.nick_ass(None, sp)
    assert "Style: Nick" in own and "lebiga" in own and "[Events]" in own
    subs = vr.build_ass([{"word": "привіт", "start": 0, "end": 1}], 2, "hl", 70)
    both = streamer.nick_ass(subs, sp)
    assert "Style: Nick" in both and both.count("[Events]") == 1 and "ПРИВІТ" in both
    assert streamer.nick_ass(subs, {"nick": None}) == subs


@needs_ffmpeg
@pytest.mark.parametrize("look", [{"layout": "top", "divider": True, "nick": "lebiga", "cam_zoom": 1.5},
                                  {"layout": "full", "game_zoom": 1.4},
                                  {"layout": "pip", "round": True, "frame": "gold", "pip_x": 1, "pip_y": 1},
                                  {"layout": "pip", "frame": "none"}, None])
def test_render_each_layout_real(tmp_path, look):
    src = tmp_path / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=2",
                    "-f", "lavfi", "-i", "sine=d=2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(src)], check=True, timeout=60)
    sp = streamer.plan(CAM if look else None, FACE, 640, 360, look)
    out = tmp_path / "v.mp4"
    vr.render_vertical(str(src), 0, 2, str(out), vr.Layout("stream", stream=sp), streamer.nick_ass(None, sp))
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(out)], capture_output=True, text=True, timeout=30).stdout.strip()
    assert info == "1080,1920"


def test_normalize_stream_opts():
    o = vr.normalize_opts({"frame": "stream", "stream_look": {"layout": "pip", "share": 0.9}})
    assert o["frame"] == "stream" and o["stream_look"]["layout"] == "pip" and o["stream_look"]["share"] == 0.6
    assert vr.normalize_opts({"stream_look": "x"})["stream_look"] is None
    assert vr.normalize_opts({})["stream_look"] is None


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
    calls = {"detect": 0, "render": [], "ass": []}

    def fake_detect(source, **k):
        calls["detect"] += 1
        return {"cam": CAM, "face": FACE, "w": 1920, "h": 1080, "note": ""}

    def fake_render(source, start, end, output_path, layout, ass_text, has_nvenc=False, timeout=900, cuts_rel=None):
        calls["render"].append(layout)
        calls["ass"].append(ass_text)
        Path(output_path).write_bytes(b"mp4" * 1000)
        return output_path

    monkeypatch.setattr(streamer, "detect_webcam", fake_detect)
    monkeypatch.setattr(vr, "detect_faces", lambda *a, **k: pytest.fail("faces are not needed for a stream"))
    monkeypatch.setattr(vr, "render_vertical", fake_render)
    monkeypatch.setattr(server, "load_transcript_cache", lambda *a, **k: ([], [], "uk"))

    info = client.get(f"/api/projects/{pid}/stream").json()
    assert info["cam"] == CAM and info["auto"] is True and info["look"] == streamer.LOOK_DEFAULT
    client.get(f"/api/projects/{pid}/stream")
    assert calls["detect"] == 1  # kept in stream.json

    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan?frame=stream").json()
    assert plan["layout"]["kind"] == "stream" and plan["layout"]["stream"]["layout"] == "top"
    assert plan["src"] == {"w": 1920, "h": 1080}

    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"frame": "stream"}})
    assert client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false").status_code == 200
    assert calls["render"][-1].kind == "stream" and calls["render"][-1].stream["layout"] == "top"

    # the video's look: partial changes are merged, every clip without its own look follows, a new render
    r = client.put(f"/api/projects/{pid}/stream", json={"keep_cam": True, "look": {"layout": "full", "game_zoom": 1.5}})
    assert r.json()["look"]["layout"] == "full" and r.json()["look"]["game_zoom"] == 1.5 and r.json()["cam"] == CAM
    r = client.put(f"/api/projects/{pid}/stream", json={"keep_cam": True, "look": {"nick": "lebiga"}})
    assert r.json()["look"]["layout"] == "full" and r.json()["look"]["nick"] == "lebiga"
    client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false")
    assert len(calls["render"]) == 2 and calls["render"][-1].stream["layout"] == "full"
    assert calls["render"][-1].stream["nick"]["text"] == "lebiga"
    assert calls["ass"][-1] and "lebiga" in calls["ass"][-1]  # the nick even without subtitles

    # one clip with its own look
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"frame": "stream", "stream_look": {"layout": "pip"}}})
    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan").json()
    assert plan["layout"]["stream"]["layout"] == "pip"
    client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false")
    assert calls["render"][-1].stream["layout"] == "pip"
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"frame": "stream"}})

    # moving the frame: saved, marked as the user's, the look stays, a new render
    r = client.put(f"/api/projects/{pid}/stream", json={"cam": [0.7, 0.7, 0.28, 0.28]})
    assert r.status_code == 200 and r.json()["auto"] is False and r.json()["face"] is None
    assert r.json()["look"]["layout"] == "full"
    n = len(calls["render"])
    client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false")
    assert len(calls["render"]) == n + 1

    # no webcam: the game only, and a clear note
    client.put(f"/api/projects/{pid}/stream", json={"cam": None})
    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan?frame=stream").json()
    assert plan["layout"]["stream"]["layout"] == "game" and "лише гру" in plan["layout"]["note"]

    client.post(f"/api/projects/{pid}/stream/redetect")
    assert calls["detect"] == 2
    assert client.get(f"/api/projects/{pid}/stream").json()["look"]["nick"] == "lebiga"  # kept across a new search


def test_stream_old_file_with_game_x_still_works(tmp_path):
    pid, _ = _project(tmp_path)
    path = server._stream_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cam": [0.02, 0.7, 0.24, 0.26], "face": null, "w": 1920, "h": 1080, "note": "", "auto": true, "game_x": 0.3}',
                    encoding="utf-8")
    info = client.get(f"/api/projects/{pid}/stream").json()
    assert info["look"]["game_x"] == 0.3 and "game_x" not in info
    r = client.put(f"/api/projects/{pid}/stream", json={"keep_cam": True, "look": {"cam_zoom": 2}})
    assert r.json()["look"]["game_x"] == 0.3 and r.json()["look"]["cam_zoom"] == 2 and "game_x" not in r.json()


def test_stream_api_unknown_project():
    assert client.get("/api/projects/nope_123/stream").status_code == 404


def test_webcam_search_after_analysis_switches_stream_on(tmp_path, monkeypatch):
    from backend import job_runner
    pid, idx = _project(tmp_path)
    src = str(tmp_path / "stream.mp4")
    monkeypatch.setattr(job_runner, "OUTPUT_DIR", str(server.OUTPUT_DIR))
    monkeypatch.setattr(streamer, "detect_webcam", lambda s, **k: {"cam": CAM, "face": FACE, "w": 1920, "h": 1080, "note": ""})
    job_runner._find_webcam_once(pid, src)
    info = client.get(f"/api/projects/{pid}/stream?detect=false").json()
    assert info["known"] and info["use"] is True and info["cam"] == CAM
    # a clip nobody touched renders as «Стрім»; a clip with its own frame keeps it
    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan").json()
    assert plan["opts"]["frame"] == "stream" and plan["layout"]["kind"] == "stream"
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"frame": "blur"}})
    monkeypatch.setattr(server, "_clip_faces", lambda *a, **k: {"w": 1920, "h": 1080, "faces": [], "duration": 5})
    monkeypatch.setattr(vr, "plan_layout", lambda *a, **k: vr.Layout("blur"))
    assert client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan").json()["opts"]["frame"] == "blur"
    # switched off for the video: untouched clips are ordinary again; a second search does not run
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {}})
    client.put(f"/api/projects/{pid}/stream", json={"keep_cam": True, "use": False})
    assert client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan").json()["opts"]["frame"] == "auto"
    monkeypatch.setattr(streamer, "detect_webcam", lambda *a, **k: pytest.fail("searched twice"))
    job_runner._find_webcam_once(pid, src)


def test_webcam_search_after_analysis_never_fails_the_job(tmp_path, monkeypatch):
    from backend import job_runner
    pid, _ = _project(tmp_path)
    monkeypatch.setattr(job_runner, "OUTPUT_DIR", str(server.OUTPUT_DIR))
    monkeypatch.setattr(streamer, "detect_webcam", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad video")))
    job_runner._find_webcam_once(pid, str(tmp_path / "stream.mp4"))
    job_runner._find_webcam_once(pid, str(tmp_path / "missing.mp4"))
    assert client.get(f"/api/projects/{pid}/stream?detect=false").json() == {"known": False}

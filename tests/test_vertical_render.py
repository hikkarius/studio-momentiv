"""
tests/test_vertical_render.py — vertical 9:16 clips: captions (.ass), face-based framing plan,
ffmpeg filter graph, option cleaning, a real (tiny) render, and the API endpoints.
"""
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import vertical_render as vr
from backend import db, server
from backend.server import app

client = TestClient(app)

WORDS = [
    {"start": 10.0, "end": 10.3, "word": "Чи"},
    {"start": 10.3, "end": 10.6, "word": "можна"},
    {"start": 10.6, "end": 11.0, "word": "стати"},
    {"start": 11.0, "end": 11.8, "word": "мільйонером"},
    {"start": 11.8, "end": 12.3, "word": "чесно?"},
    {"start": 13.5, "end": 13.9, "word": "90"},
    {"start": 13.9, "end": 14.4, "word": "відсотків"},
]


# ─── Captions ────────────────────────────────────────────────────────────────

def test_clip_words_relative_times_and_edits():
    words = vr.clip_words(WORDS, 10.0, 14.0, edits={"10.30": {"w": "МОЖНА"}, "10.60": {"w": ""}, "10.00": {"k": True}})
    texts = [w["word"] for w in words]
    assert texts == ["Чи", "МОЖНА", "мільйонером", "чесно?", "90"]  # "стати" deleted, "відсотків" outside
    assert words[0]["start"] == 0.0 and words[0]["key"] is True
    assert words[0]["id"] == "10.00"
    assert next(w for w in words if w["word"] == "90")["key"] is True  # numbers are key words
    assert next(w for w in words if w["word"] == "мільйонером")["key"] is True  # long word


def test_chunk_words_breaks_on_pause_and_sentence_end():
    words = vr.clip_words(WORDS, 10.0, 15.0)
    chunks = vr.chunk_words(words, 3, 30)
    assert [len(c) for c in chunks] == [3, 2, 2]  # 3 max, then "чесно?" ends sentence, pause before "90"


@pytest.mark.parametrize("style", list(vr.CAPTION_STYLES))
def test_build_ass_every_style(style):
    words = vr.clip_words(WORDS, 10.0, 15.0)
    ass = vr.build_ass(words, 5.0, style, None, hook="Чесний бізнес")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert vr.CAPTION_STYLES[style].font in ass
    assert ass.count("Dialogue:") >= 3
    assert "ЧЕСНИЙ БІЗНЕС" in ass  # hook title on top
    if vr.CAPTION_STYLES[style].upper:
        assert "МІЛЬЙОНЕРОМ" in ass


def test_build_ass_highlight_has_plate_layer_and_position():
    words = vr.clip_words(WORDS, 10.0, 12.5)
    ass = vr.build_ass(words, 2.5, "hl", pos_pct=50)
    assert "Style: Shape" in ass
    assert "\\pos(540,960)" in ass
    # one text event + one drawn plate per word
    assert sum(1 for l in ass.splitlines() if l.startswith("Dialogue: 2,")) == 5
    assert sum(1 for l in ass.splitlines() if l.startswith("Dialogue: 1,") and "\\p1" in l) == 5


def test_text_width_matches_libass_measurement():
    # measured with ffmpeg/libass: «ЧЕСНИЙ БІЗНЕС ЙОГО» at 100 → 788 px Montserrat Black, 736 px Rubik Bold
    assert abs(vr.text_width("Montserrat Black", False, "ЧЕСНИЙ БІЗНЕС ЙОГО", 100) - 788) < 3
    assert abs(vr.text_width("Rubik", True, "ЧЕСНИЙ БІЗНЕС ЙОГО", 100) - 736) < 3
    assert vr.text_width("Nope", False, "abc", 100) == 180  # unknown font → rough estimate


def test_wrap_lines_breaks_long_chunks():
    assert vr.wrap_lines([400, 400, 400], 20, 900) == [[0, 1], [2]]
    assert vr.wrap_lines([1000], 20, 900) == [[0]]


def test_rounded_plates_and_edges():
    words = vr.clip_words(WORDS, 10.0, 12.5)
    square = vr.build_ass(words, 2.5, "hl")
    rnd = vr.build_ass(words, 2.5, "hl", round_plates=True, stroke="FFFFFF")
    assert " b " not in square.split("[Events]")[1] and " b " in rnd  # bezier corners only when rounded
    assert "\\bord" in rnd and "\\3c&H00FFFFFF" in rnd  # white plate edge
    t = vr.build_ass(words, 2.5, "kar", stroke="FFFFFF", text_color="FFE14D")
    assert ",&H00000000,&H80000000," in vr.build_ass(words, 2.5, "kar", stroke="FFFFFF").split("Style: Txt")[1].split("\n")[0]
    assert "Style: Txt,Rubik Black" in t and ",&H00FFFFFF,&H80000000," in t.split("Style: Txt")[1].split("\n")[0]
    h = vr.build_ass(words, 2.5, "none", hook="Дуже довгий заголовок який точно не влізе в один рядок на екрані",
                     hook_round=True, hook_stroke="000000", hook_style="white")
    assert "\\N" in h.split("Hook,,")[-1]  # wrapped by measured width
    assert h.count("\\p1") >= 4  # 2 lines × (edge + fill)


def test_build_ass_escapes_braces():
    ass = vr.build_ass([{"start": 0, "end": 1, "word": "{\\b1}x", "key": False}], 1.0, "box")
    assert "{\\b1}x" not in ass and "(/b1)x" in ass


# ─── Framing plan ────────────────────────────────────────────────────────────

def _faces(samples, w=1920, h=1080):
    return {"w": w, "h": h, "samples": samples}


def test_plan_track_follows_face_and_cuts_on_speaker_change():
    s = [[i * 0.25, [[0.7, 0.3, 0.1, 0.2, 0.9]]] for i in range(20)]
    s += [[5 + i * 0.25, [[0.25, 0.3, 0.1, 0.2, 0.9]]] for i in range(20)]
    lay = vr.plan_layout(_faces(s), "auto", 10)
    assert lay.kind == "track"
    assert abs(lay.keyframes[0][1] - 0.7) < 0.01
    assert abs(lay.keyframes[-1][1] - 0.25) < 0.01
    t_cut = [k for k in lay.keyframes if abs(k[1] - 0.25) < 0.01][0][0]
    assert 4.9 <= t_cut <= 5.2  # hard cut, not a slow pan


def test_plan_switches_exactly_on_camera_cuts_and_glides_inside_a_shot():
    s = [[i * 0.25, [[0.7, 0.3, 0.1, 0.2, 0.9]]] for i in range(20)]           # shot 1: person right
    s += [[5 + i * 0.25, [[0.25, 0.3, 0.1, 0.2, 0.9]]] for i in range(8)]      # shot 2 from 5.13 s: person left
    s += [[7 + i * 0.25, [[0.45, 0.3, 0.1, 0.2, 0.9]]] for i in range(12)]     # same shot, person moves right
    lay = vr.plan_layout({"w": 1920, "h": 1080, "samples": s, "cuts": [5.13]}, "track", 10)
    keys = lay.keyframes
    def x_at(t):  # same easing as the render and the preview
        for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
            if t0 <= t < t1:
                u = (t - t0) / (t1 - t0)
                return x0 + (x1 - x0) * u * u * (3 - 2 * u)
        return keys[-1][1] if t >= keys[-1][0] else keys[0][1]
    assert abs(x_at(5.13) - 0.25) < 0.005          # the cut frame already shows the new person
    assert abs(x_at(5.13 - 1 / 30) - 0.7) < 0.005  # the frame before still shows the old one
    glide = [(t0, t1) for (t0, x0), (t1, x1) in zip(keys, keys[1:]) if abs(x0 - 0.25) < 0.01 and abs(x1 - 0.45) < 0.01]
    assert glide and glide[0][1] - glide[0][0] >= 0.79                           # slow, eased move


def test_split_ignores_background_people_and_edited_multicam():
    # Wide shot: main person + small background person; close-up of another person alone.
    wide = [[0.29, 0.4, 0.06, 0.15, 0.9], [0.84, 0.4, 0.035, 0.09, 0.9]]
    s = [[i * 0.25, wide] for i in range(20)] + [[5 + i * 0.25, [[0.66, 0.35, 0.09, 0.3, 0.9]]] for i in range(20)]
    lay = vr.plan_layout({"w": 1920, "h": 1080, "samples": s, "cuts": [5.0]}, "split", 10)
    assert lay.kind == "track" and lay.note


def test_plan_ignores_small_background_faces_and_jitter():
    s = []
    for i in range(40):
        jitter = 0.02 if i % 2 else -0.02
        s.append([i * 0.25, [[0.6 + jitter, 0.3, 0.1, 0.2, 0.9], [0.05, 0.6, 0.03, 0.05, 0.8]]])
    lay = vr.plan_layout(_faces(s), "auto", 10)
    assert lay.kind == "track"
    assert len(lay.keyframes) == 1  # camera holds still


def test_plan_split_for_two_people():
    s = [[i * 0.25, [[0.25, 0.35, 0.09, 0.18, 0.9], [0.75, 0.35, 0.09, 0.18, 0.9]]] for i in range(20)]
    lay = vr.plan_layout(_faces(s), "auto", 5)
    assert lay.kind == "split"
    assert lay.split[0][0] < lay.split[1][0]


def test_plan_blur_when_no_faces_and_explicit_modes():
    s = [[i * 0.25, []] for i in range(20)]
    assert vr.plan_layout(_faces(s), "auto", 5).kind == "blur"
    assert vr.plan_layout(_faces(s), "track", 5).kind == "center"
    one = [[i * 0.25, [[0.7, 0.3, 0.1, 0.2, 0.9]]] for i in range(20)]
    lay = vr.plan_layout(_faces(one), "split", 5)
    assert lay.kind == "track" and lay.note
    assert vr.plan_layout(_faces(one), "blur", 5).kind == "blur"


def test_plan_vertical_source_needs_no_crop():
    lay = vr.plan_layout(_faces([[0, []]], w=1080, h=1920), "auto", 5)
    assert lay.kind == "center"


def test_x_expr_is_flat_piecewise_linear():
    expr = vr._x_expr([(0.0, 0.5), (2.0, 0.5), (2.6, 0.7)], 1920, 606)
    assert expr.startswith("657.0")
    assert "(clip((t-2.000)/0.600,0,1))*(clip((t-2.000)/0.600,0,1))*(3-2*" in expr  # eased
    assert "if(" not in expr


def test_video_filter_variants():
    track = vr.video_filter(vr.Layout("track", [(0, 0.5)]), 1920, 1080, "subs.ass", "../../frontend/fonts")
    assert "crop=606:1080" in track and "ass=subs.ass:fontsdir=../../frontend/fonts[v]" in track
    blur = vr.video_filter(vr.Layout("blur"), 1920, 1080, None, "f")
    assert "boxblur" in blur and blur.endswith("null[v]")
    split = vr.video_filter(vr.Layout("split", split=[(0.25, 0.3, 0.62), (0.75, 0.3, 0.62)]), 1920, 1080, None, "f")
    assert "vstack" in split


def test_normalize_opts():
    o = vr.normalize_opts({"style": "bad", "frame": "zzz", "pos": "200", "hook": 1,
                           "edits": {"12.30": {"w": "слово", "k": True}, "oops": {"w": "x"}, "1.5": {"w": "y"}}})
    assert o["style"] == vr.DEFAULT_STYLE and o["frame"] == "auto" and o["pos"] == 85.0 and o["hook"] is True
    assert o["edits"] == {"12.30": {"w": "слово", "k": True}}
    assert vr.normalize_opts({"style": "none"})["style"] == "none"


def test_cache_key_changes_with_settings():
    a = vr.cache_key(1, 20, vr.normalize_opts({}), True)
    assert a == vr.cache_key(1, 20, vr.normalize_opts({}), True)
    assert a != vr.cache_key(1, 21, vr.normalize_opts({}), True)
    assert a != vr.cache_key(1, 20, vr.normalize_opts({"style": "one"}), True)
    assert a != vr.cache_key(1, 20, vr.normalize_opts({}), False)


# ─── Real render (needs ffmpeg with libass) ──────────────────────────────────

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _make_video(path: Path, seconds: int = 2):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


@needs_ffmpeg
@pytest.mark.parametrize("layout", [vr.Layout("track", [(0, 0.3), (1.0, 0.3), (1.5, 0.7)]), vr.Layout("blur"),
                                    vr.Layout("split", split=[(0.3, 0.4, 0.62), (0.7, 0.4, 0.62)])])
def test_render_vertical_real(tmp_path, layout):
    src = tmp_path / "src.mp4"
    _make_video(src)
    words = vr.clip_words([{"start": 0.1, "end": 0.6, "word": "Привіт"}, {"start": 0.6, "end": 1.2, "word": "світ"}], 0, 2)
    out = tmp_path / "out" / "v.mp4"
    vr.render_vertical(str(src), 0, 2, str(out), layout, vr.build_ass(words, 2, "hl"))
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    assert info == "1080,1920"
    assert not list(out.parent.glob("tmp_*"))


# ─── API ─────────────────────────────────────────────────────────────────────

def _project_with_clip(tmp_path):
    pid = f"vert_{uuid.uuid4().hex[:8]}"
    src = tmp_path / "source.mp4"
    src.write_bytes(b"x" * 2000)
    db.create_project(pid, "Тест", str(src), 60.0)
    db.save_candidate_clips(pid, [{"start": 10.0, "end": 15.0, "title": "ЧЕСНИЙ БІЗНЕС", "hook": "h", "reason": "r", "score": 90}])
    clip = db.get_candidate_clips(pid)[0]
    return pid, clip["clip_index"], src


def test_caption_styles_endpoint():
    r = client.get("/api/caption-styles")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["styles"]]
    assert ids[:3] == ["hl", "kar", "one"] and r.json()["default"] == "hl"


def test_patch_vertical_opts_is_saved_cleaned(tmp_path):
    pid, idx, _ = _project_with_clip(tmp_path)
    assert db.get_candidate_clips(pid)[0]["vertical_opts"] == {}
    r = client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"style": "one", "frame": "track", "pos": 60,
                                                                                  "edits": {"10.30": {"w": "МОЖНА"}}}})
    assert r.status_code == 200
    vo = db.get_candidate_clips(pid)[0]["vertical_opts"]
    assert vo["style"] == "one" and vo["frame"] == "track" and vo["pos"] == 60.0 and vo["edits"]["10.30"]["w"] == "МОЖНА"


def test_vertical_plan_and_render_endpoints(tmp_path, monkeypatch):
    pid, idx, src = _project_with_clip(tmp_path)
    faces = {"w": 1920, "h": 1080, "samples": [[i * 0.25, [[0.7, 0.3, 0.1, 0.2, 0.9]]] for i in range(20)]}
    calls = {"detect": 0, "render": 0}

    def fake_detect(*a, **k):
        calls["detect"] += 1
        return faces

    def fake_render(source, start, end, output_path, layout, ass_text, has_nvenc=False, timeout=900):
        calls["render"] += 1
        assert layout.kind == "track" and (ass_text is None or "МОЖНА" in ass_text)
        Path(output_path).write_bytes(b"mp4" * 1000)
        return output_path

    monkeypatch.setattr(vr, "detect_faces", fake_detect)
    monkeypatch.setattr(vr, "render_vertical", fake_render)
    monkeypatch.setattr(server, "load_transcript_cache", lambda *a, **k: ([], WORDS, "uk"))
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"vertical_opts": {"edits": {"10.30": {"w": "можна!"}}}})

    plan = client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan").json()
    assert plan["layout"]["kind"] == "track"
    assert [w["word"] for w in plan["words"]][:2] == ["Чи", "можна!"]
    client.get(f"/api/projects/{pid}/clips/{idx}/vertical-plan")
    assert calls["detect"] == 1  # faces cached on disk

    r = client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=true")
    assert r.status_code == 200 and r.json()["filename"].endswith("_9x16_subs.mp4")
    d = client.get(f"/api/projects/{pid}/clips/{idx}/vertical?subs=true")
    assert d.status_code == 200 and d.headers["content-type"] == "video/mp4"
    assert calls["render"] == 1  # second request served from cache
    client.post(f"/api/projects/{pid}/clips/{idx}/vertical?subs=false")
    assert calls["render"] == 2

    # ZIP of kept clips in the background
    client.patch(f"/api/projects/{pid}/clips/{idx}", json={"review_status": "keep"})
    job = client.post(f"/api/projects/{pid}/export-vertical-zip", json={"subs": True}).json()
    import time
    for _ in range(50):
        st = client.get(f"/api/vertical-jobs/{job['id']}").json()
        if st["status"] != "running":
            break
        time.sleep(0.05)
    assert st["status"] == "completed" and st["done"] == 1 and st["total"] == 1
    z = client.get(f"/api/vertical-jobs/{job['id']}/file")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"


def test_vertical_zip_without_kept_clips(tmp_path):
    pid, _, _ = _project_with_clip(tmp_path)
    r = client.post(f"/api/projects/{pid}/export-vertical-zip", json={})
    assert r.status_code == 400


def test_build_ass_size_colours_and_title_variants():
    words = vr.clip_words(WORDS, 10.0, 12.5)
    base = vr.build_ass(words, 2.5, "hl")
    big = vr.build_ass(words, 2.5, "hl", size=1.5, accent="FF4D4D", text_color="FFE14D")
    assert "Montserrat Black,100," in base and "Montserrat Black,150," in big
    assert "&H004D4DFF" in big                       # red plate behind the active word (ASS is BGR)
    assert "Style: Txt,Montserrat Black,150,&H004DE1FF" in big  # yellow text
    for hs in vr.HOOK_STYLES:
        ass = vr.build_ass(words, 2.5, "box", hook="Мій заголовок", hook_style=hs, hook_size=1.5, hook_sec=None)
        line = next(l for l in ass.splitlines() if ",Hook," in l and l.startswith("Dialogue"))
        assert "0:00:02.50" in line and "МІЙ ЗАГОЛОВОК" in line and "\\fad" not in line  # whole clip
        assert ("Montserrat Black,90," in ass) or ("Montserrat Black,108," in ass)


def test_normalize_new_opts():
    o = vr.normalize_opts({"size": 9, "hook_size": "1.4", "color": "#ff4d4d", "text": "123456", "hook_text": "  Привіт ",
                           "hook_style": "black", "hook_full": 1})
    assert o["size"] == 1.6 and o["hook_size"] == 1.4 and o["color"] == "FF4D4D" and o["text"] is None
    assert o["hook_text"] == "Привіт" and o["hook_style"] == "black" and o["hook_full"] is True
    d = vr.normalize_opts({})
    assert d["hook_style"] == "white" and d["size"] == 1.0 and d["color"] is None
    assert vr.cache_key(0, 5, vr.normalize_opts({"hook": True}), True, "A") != vr.cache_key(0, 5, vr.normalize_opts({"hook": True, "hook_text": "B"}), True, "A")


def test_normalize_plate_opts():
    o = vr.normalize_opts({"round": 1, "stroke": "#ffffff", "hook_round": True, "hook_stroke": "123456"})
    assert o["round"] is True and o["stroke"] == "FFFFFF" and o["hook_round"] is True and o["hook_stroke"] is None
    d = vr.normalize_opts({})
    assert d["round"] is False and d["stroke"] is None


def test_hook_position():
    a = vr.build_ass([], 2.0, "none", hook="Заголовок")
    b = vr.build_ass([], 2.0, "none", hook="Заголовок", hook_pos=50)
    assert "\\pos(540,190)" in a and "\\pos(540,960)" in b
    assert vr.normalize_opts({"hook_pos": 120})["hook_pos"] == 80.0 and vr.normalize_opts({})["hook_pos"] is None

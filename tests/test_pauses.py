"""«Прибрати паузи»: finding pauses, the cut timeline, saving cuts, cut renders and subtitles."""
import shutil
import subprocess
import uuid

import pytest
from fastapi.testclient import TestClient

import pauses
from backend import db, server
from backend.server import app

client = TestClient(app)
R = pauses.LEVEL_RATE


def _levels(*parts):
    """parts: (seconds, dB) runs → level list."""
    out = []
    for sec, db_ in parts:
        out += [float(db_)] * int(round(sec * R))
    return out


def test_find_pauses_by_audio():
    # 10 s clip: speech 3 s, silence 1.5 s, speech 2 s, silence 0.4 s (short), speech 3.1 s
    lv = _levels((3, -20), (1.5, -60), (2, -18), (0.4, -60), (3.1, -22))
    cuts = pauses.find_pauses(100.0, 110.0, levels=lv, words=[])
    assert cuts == [(103.1, 104.4)]  # 1.5 s pause minus 0.1 s kept on each side
    assert pauses.find_pauses(100.0, 110.0, levels=lv, words=[], min_gap=0.3)[1][0] == pytest.approx(106.6, abs=0.01)


def test_quiet_speech_is_not_a_pause():
    lv = _levels((3, -20), (1.5, -60), (5.5, -20))
    words = [{"start": 103.6, "end": 103.9, "word": "тихо"}]
    assert pauses.find_pauses(100.0, 110.0, levels=lv, words=words) == []


def test_find_pauses_by_words_without_audio():
    words = [{"start": 0.5, "end": 1.0, "word": "а"}, {"start": 2.5, "end": 3.0, "word": "б"}, {"start": 3.2, "end": 9.0, "word": "в"}]
    assert pauses.find_pauses(0.0, 10.0, levels=None, words=words) == [(1.1, 2.4)]


def test_cut_timeline_helpers():
    assert pauses.normalize_cuts([[5, 6], [5.5, 7], [1, 1.05], ["x"], [-3, 0.5]], 0.0, 10.0) == [(0.05, 0.5), (5.0, 7.0)]
    cuts = [(2.0, 3.0), (5.0, 5.5)]
    assert pauses.kept_segments(0.0, 8.0, cuts) == [(0.0, 2.0), (3.0, 5.0), (5.5, 8.0)]
    assert pauses.map_time(1.0, cuts) == 1.0 and pauses.map_time(2.5, cuts) is None and pauses.map_time(6.0, cuts) == 4.5
    assert pauses.map_time_clamped(2.5, cuts) == 2.0
    words = [{"start": 1.5, "end": 2.4, "word": "а"}, {"start": 2.2, "end": 2.6, "word": "в паузі"}, {"start": 4.0, "end": 4.5, "word": "б"}]
    assert [(w["word"], w["start"], w["end"]) for w in pauses.remap_words(words, cuts)] == [("а", 1.5, 2.0), ("б", 3.0, 3.5)]
    assert pauses.removed(cuts) == 1.5
    assert pauses.cuts_key([]) == "" and len(pauses.cuts_key(cuts)) == 8


def _project(tmp_path):
    pid = f"test_pauses_{uuid.uuid4().hex[:8]}"
    src = tmp_path / "source.mp4"
    src.write_bytes(b"dummy")
    db.create_project(pid, "Подкаст", str(src), 120.0)
    db.save_candidate_clips(pid, [{"start": 10.0, "end": 30.0, "title": "Перший", "score": 70}])
    return pid


def test_pauses_endpoint_saves_cuts_and_srt_follows(tmp_path, monkeypatch):
    pid = _project(tmp_path)
    words = [{"start": 10.2 + i * 0.5, "end": 10.6 + i * 0.5, "word": f"с{i}"} for i in range(10)]  # 10.2 … 15.1
    words += [{"start": 18.0 + i * 0.5, "end": 18.4 + i * 0.5, "word": f"д{i}"} for i in range(20)]  # pause 15.1 → 18.0
    monkeypatch.setattr("backend.server.load_transcript_cache", lambda p, **kw: ([], words, "uk"))
    monkeypatch.setattr("subtitles_generator.load_transcript_cache", lambda p, **kw: ([], words, "uk"))
    monkeypatch.setattr(pauses, "audio_levels", lambda *a, **k: _levels((5.1, -20), (2.9, -60), (12, -20)))

    data = client.post(f"/api/projects/{pid}/clips/1/pauses", json={"min_gap": 0.6}).json()
    assert data["count"] == 1 and data["by"] == "audio"
    assert data["cuts"] == [[15.2, 17.9]] and data["removed_sec"] == 2.7
    assert db.get_candidate_clips(pid)[0]["cuts"] == [[15.2, 17.9]]

    # the SRT runs on the cut timeline: the first word after the pause comes right after the last one before it
    srt = client.get(f"/api/projects/{pid}/clips/1/export-srt").text
    # «д1» ends at 18.9 s in the video = 8.9 s into the clip, minus the 2.7 s pause = 6.2 s
    assert "00:00:04,200 --> 00:00:06,200\nс8 с9 д0 д1" in srt
    assert srt.rstrip().endswith("00:00:14,300 --> 00:00:15,200\nд18 д19")

    # restoring one pause = saving the list without it; junk is cleaned
    res = client.patch(f"/api/projects/{pid}/clips/1", json={"cuts": [[1, 2], [25.0, 26.0]]})
    assert res.status_code == 200 and res.json()["clip"]["cuts"] == [[25.0, 26.0]]
    assert client.patch(f"/api/projects/{pid}/clips/1", json={"cuts": []}).json()["clip"]["cuts"] == []
    assert client.post(f"/api/projects/{pid}/clips/1/pauses", json={"min_gap": 9}).status_code == 422


def test_levels_endpoint(tmp_path, monkeypatch):
    pid = _project(tmp_path)
    monkeypatch.setattr(pauses, "audio_levels", lambda src, lo, hi, **k: [-30.0] * int((hi - lo) * R))
    data = client.get(f"/api/projects/{pid}/clips/1/levels?pad=4").json()
    assert data["start"] == 6.0 and data["rate"] == R and len(data["db"]) == 28 * R


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_render_with_cuts_is_shorter(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10",
                    "-f", "lavfi", "-i", "sine=frequency=300", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True, timeout=60)
    out = tmp_path / "cut.mp4"
    pauses.render_with_cuts(str(src), 1.0, 5.0, [(2.0, 3.0)], str(out))
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True, timeout=20).stdout)
    assert 2.8 < dur < 3.3
    levels = pauses.audio_levels(str(src), 0.0, 2.0)
    assert len(levels) == 2 * R and max(levels) > -40

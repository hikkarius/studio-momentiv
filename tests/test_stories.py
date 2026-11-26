"""
tests/test_stories.py — «Довгі історії»: whole stories of 1–3 min, shown «Ціла» or «Частина 1/2».
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

import ai_selector
import vertical_render
from ai_selector import GeminiUnavailableError, _call_gemini_single_window, find_stories
from backend import db, job_runner, queue_runner, server
from backend.server import app
from clip_refiner import clamp_split, story_split

client = TestClient(app)


def _resp(clips):
    r = MagicMock()
    r.text = json.dumps(clips)
    return r


def _segments(minutes: float):
    return [{"start": i * 10.0, "end": i * 10.0 + 10.0, "text": "слово " * 20} for i in range(int(minutes * 6))]


def _story(start, end, **kw):
    return {"start": start, "end": end, "title": "ІСТОРІЯ", "category": "content",
            "hook": 7, "emotion": 8, "standalone": 8, "value": 6, **kw}


def _words(t0: float, t1: float, sentence_every: float = 8.0, pause_at: float | None = None):
    """A word every 0.5 s; every `sentence_every` s a sentence ends with «.»; a 1.2 s pause before `pause_at`."""
    out, t, n = [], t0, 0
    while t < t1 - 0.5:
        if pause_at is not None and abs(t - pause_at) < 0.25:
            t += 1.2
        end_of_sentence = (n + 1) % int(sentence_every * 2) == 0
        out.append({"start": round(t, 2), "end": round(t + 0.4, 2), "word": "слово." if end_of_sentence else "слово"})
        t += 0.5
        n += 1
    return out


# ─── Where a story is cut in two ─────────────────────────────────────────────

def test_cut_is_a_sentence_end_near_the_middle_preferring_a_real_pause():
    words = _words(100.0, 220.0, pause_at=171.0)
    cut = story_split(100.0, 220.0, words)
    assert 100 + 120 * 0.35 <= cut <= 100 + 120 * 0.65
    assert 170.0 < cut < 172.5  # the long pause wins over sentence ends closer to the middle
    assert not any(w["start"] < cut < w["end"] for w in words)  # never inside a word


def test_cut_without_words_is_the_middle_and_bad_saved_cuts_are_rejected():
    assert story_split(10.0, 130.0, []) == 70.0
    assert clamp_split(70.0, 10.0, 130.0) == 70.0
    assert clamp_split(12.0, 10.0, 130.0) is None  # «Частина 1» would be 2 s
    assert clamp_split(None, 10.0, 130.0) is None
    assert clamp_split("x", 10.0, 130.0) is None


# ─── Gemini prompt and search ────────────────────────────────────────────────

def test_story_prompt_asks_for_whole_stories_and_allows_none():
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([_story(60, 170)])
    _call_gemini_single_window(mc, _segments(10), 3, 60, 180, "Viral Hooks", "uk", experimental=True,
                               max_chars=ai_selector.EXPERIMENTAL_MAX_CHARS, story=True)
    prompt = mc.models.generate_content.call_args.kwargs["contents"]
    assert "ДО 3 ЦІЛИХ ІСТОРІЙ" in prompt and "порожній масив []" in prompt
    assert "від 60 до 180 секунд" in prompt
    assert "ДОВГІ ІСТОРІЇ" in prompt and "НАЙВІРУСНІШІ моменти" not in prompt


def test_find_stories_reads_the_whole_video_once_and_scores_by_the_rubric():
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([_story(60, 170), _story(300, 400, hook=9)])
    found = find_stories(client=mc, segments=_segments(20), count=3)
    assert mc.models.generate_content.call_count == 1
    assert len(found) == 2 and all(isinstance(c["score"], int) for c in found)


def test_find_stories_returns_nothing_rather_than_inventing():
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([])
    assert find_stories(client=mc, segments=_segments(20), count=3) == []


def test_story_search_failure_keeps_the_short_clips(monkeypatch):
    def down(**kw):
        raise GeminiUnavailableError("503")
    monkeypatch.setattr(job_runner, "find_stories", down)
    proj = {"id": "p", "duration": 1200.0}
    assert job_runner.find_stories_for(proj, _segments(20), _segments(20), [], "uk", "Viral Hooks", False) == []


def test_found_stories_are_snapped_marked_and_get_a_cut(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return [_story(100.0, 220.0), _story(400.0, 430.0)]  # the second is too short for a story

    monkeypatch.setattr(job_runner, "find_stories", fake)
    words = _words(0.0, 600.0)
    proj = {"id": "p", "duration": 600.0}
    out = job_runner.find_stories_for(proj, _segments(10), _segments(10), words, "uk", "Viral Hooks", False)
    assert seen["min_len"] == 60 and seen["max_len"] == 180 and seen["count"] == 3 and seen["strong"] is None
    assert len(out) == 1
    s = out[0]
    assert s["kind"] == "story" and 60 <= s["end"] - s["start"] <= 180
    assert s["start"] + 15 <= s["split"] <= s["end"] - 15


def _run_worker(monkeypatch, stories):
    db.create_project("p_story", "v.mp4", "v.mp4", 900.0)
    db.create_job("job_story", "p_story", "analyze")
    segs = _segments(15)
    monkeypatch.setattr(job_runner, "transcribe_video", lambda p, status_cb=None, language=None: (segs, _words(0, 900), "uk", False))
    monkeypatch.setattr(job_runner, "analyze_and_find_clips", lambda **kw: [_story(10, 40), _story(500, 530)])
    monkeypatch.setattr(job_runner, "rerank_clips", lambda clips, **kw: clips)
    calls = []
    monkeypatch.setattr(job_runner, "find_stories", lambda **kw: calls.append(kw) or [_story(100.0, 220.0)])
    job_runner._run_analyze_worker("job_story", "p_story", 2, "Viral Hooks", stories=stories)
    return calls


def test_analysis_adds_stories_on_top_of_the_short_clips(monkeypatch):
    calls = _run_worker(monkeypatch, True)
    assert len(calls) == 1 and db.get_job("job_story")["status"] == "completed"
    clips = db.get_candidate_clips("p_story")
    stories = [c for c in clips if c["kind"] == "story"]
    assert len(stories) == 1 and len(clips) == 3
    assert stories[0]["story_view"] == "whole" and stories[0]["split_sec"] is not None
    assert stories[0]["reserve"] == 0  # a story never goes to «Запас»


def test_analysis_without_stories_does_not_ask_for_them(monkeypatch):
    assert _run_worker(monkeypatch, False) == []
    assert all(c["kind"] == "" for c in db.get_candidate_clips("p_story"))


def test_queue_and_settings_carry_the_story_choice(monkeypatch):
    assert client.get("/api/settings").json()["values"]["studio"]["find_stories"] is True
    assert client.post("/api/settings", json={"studio": {"find_stories": False}}).status_code == 200
    assert client.get("/api/settings").json()["values"]["studio"]["find_stories"] is False

    src = server.UPLOADS_DIR / "p_q.mp4"
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    Path(src).write_bytes(b"\0" * 2048)
    db.create_project("p_q", "p_q.mp4", str(src), 600.0)
    seen = []
    monkeypatch.setattr(job_runner, "_run_analyze_worker", lambda job_id, pid, n, f, **kw: seen.append(kw.get("stories", "default")))
    assert client.post("/api/queue", json={"project_ids": ["p_q"], "stories": "on"}).status_code == 200
    queue_runner.process_next()
    assert client.post("/api/queue", json={"project_ids": ["p_q"]}).status_code == 200
    queue_runner.process_next()
    assert client.post("/api/queue", json={"project_ids": ["p_q"], "stories": "maybe"}).status_code == 422
    assert seen == [True, "default"]


# ─── Database ────────────────────────────────────────────────────────────────

def test_repeated_search_keeps_a_new_story_over_a_kept_short_clip():
    db.create_project("p_rep", "v.mp4", "v.mp4", 600.0)
    db.save_candidate_clips("p_rep", [_story(100.0, 130.0, title="short")])
    db.update_clip_fields("p_rep", 1, review_status="keep")
    db.save_candidate_clips("p_rep", [_story(90.0, 200.0, title="story", kind="story", split=145.0),
                                      _story(100.0, 128.0, title="dup")], keep_reviewed=True)
    titles = {c["title"]: c for c in db.get_candidate_clips("p_rep")}
    assert set(titles) == {"short", "story"}
    assert titles["story"]["split_sec"] == 145.0


# ─── API: «Ціла | 2 частини», downloads per part ─────────────────────────────

def _story_project(tmp_path):
    src = server.UPLOADS_DIR / "s.mp4"
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    Path(src).write_bytes(b"\0" * 2048)
    db.create_project("p_s", "Відео", str(src), 600.0)
    db.save_candidate_clips("p_s", [_story(10.0, 40.0, title="Кліп"),
                                    _story(100.0, 220.0, title="Історія", kind="story", split=160.0)])
    db.update_project_status("p_s", "ready")


def test_story_view_and_cut_are_saved_and_checked(tmp_path):
    _story_project(tmp_path)
    r = client.put("/api/projects/p_s/clips/2/story", json={"view": "parts"})
    assert r.status_code == 200 and r.json()["clip"]["story_view"] == "parts"
    assert client.put("/api/projects/p_s/clips/2/story", json={"split_sec": 150.5}).json()["clip"]["split_sec"] == 150.5
    r = client.put("/api/projects/p_s/clips/2/story", json={"split_sec": 105.0})
    assert r.status_code == 400 and "15 с" in r.json()["detail"]
    assert client.put("/api/projects/p_s/clips/2/story", json={"view": "halves"}).status_code == 422
    assert client.put("/api/projects/p_s/clips/1/story", json={"view": "parts"}).status_code == 400


def test_parts_are_clips_of_their_own_and_only_stories_have_them(tmp_path):
    _story_project(tmp_path)
    proj = db.get_project("p_s")
    clips = {c["clip_index"]: c for c in db.get_candidate_clips("p_s")}
    one = server._part_clip(proj, clips[2], 1)
    two = server._part_clip(proj, clips[2], 2)
    assert (one["start_sec"], one["end_sec"], two["start_sec"], two["end_sec"]) == (100.0, 160.0, 160.0, 220.0)
    assert one["part_label"] == "Частина 1/2" and two["title"] == "Історія · Частина 2"
    assert server._file_prefix(one) == "clip_2p1" and server._file_prefix(clips[2]) == "clip_2"
    assert server._story_parts(proj, clips[2]) == [clips[2]]  # «Ціла»: one file
    assert len(server._story_parts(proj, {**clips[2], "story_view": "parts"})) == 2
    assert client.get("/api/projects/p_s/clips/1/download?part=1").status_code == 400
    assert client.get("/api/projects/p_s/clips/2/download?part=3").status_code == 400


def test_trimming_a_story_moves_a_cut_that_fell_out(tmp_path):
    _story_project(tmp_path)
    r = client.put("/api/projects/p_s/clips/2/trim", json={"start_sec": 150.0, "end_sec": 220.0})
    assert r.status_code == 200
    cut = r.json()["clip"]["split_sec"]
    assert 165.0 <= cut <= 205.0
    # a cut that still fits stays where the author put it
    client.put("/api/projects/p_s/clips/2/story", json={"split_sec": 170.0})
    r = client.put("/api/projects/p_s/clips/2/trim", json={"start_sec": 140.0, "end_sec": 220.0})
    assert r.json()["clip"]["split_sec"] == 170.0


def test_part_label_is_drawn_in_the_vertical_subtitles():
    ass = vertical_render.build_ass([], 30.0, "none", part_label="Частина 1/2")
    assert "Style: Part," in ass and "ЧАСТИНА 1/2" in ass and "\\an9" in ass
    assert "Part" not in vertical_render.build_ass([], 30.0, "none")

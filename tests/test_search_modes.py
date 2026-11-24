"""
tests/test_search_modes.py — «Класичний» / «Експериментальний» clip search switch.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import ai_selector
from ai_selector import _call_gemini_single_window, _overlapping_windows, find_clips_experimental
from backend import db, job_runner, queue_runner, server
from backend.server import app
from clip_refiner import align_start_to_hook, refine_clips, trim_lead_fillers

client = TestClient(app)


def _resp(clips):
    r = MagicMock()
    r.text = json.dumps(clips)
    return r


def _segments(minutes: float, chars_per_seg: int = 120):
    """One 10-second segment after another, `minutes` long."""
    n = int(minutes * 6)
    return [{"start": i * 10.0, "end": i * 10.0 + 10.0, "text": "с" * chars_per_seg} for i in range(n)]


def _clip(start, end, **kw):
    return {"start": start, "end": end, "title": "T", "category": "content",
            "hook": 7, "emotion": 6, "standalone": 7, "value": 6, **kw}


# ─── Prompt ──────────────────────────────────────────────────────────────────

def test_classic_prompt_is_unchanged_and_experimental_asks_for_strongest_with_hook():
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([_clip(0, 30)])
    segs = _segments(5)

    _call_gemini_single_window(mc, segs, 5, 15, 60, "Viral Hooks", "uk")
    classic = mc.models.generate_content.call_args.kwargs["contents"]
    assert "Кількість фрагментів: рівно 5" in classic
    assert "ХУК:" not in classic and "НЕ розподіляй" not in classic

    _call_gemini_single_window(mc, segs, 5, 15, 60, "Viral Hooks", "uk",
                               experimental=True, max_chars=ai_selector.EXPERIMENTAL_MAX_CHARS)
    exp = mc.models.generate_content.call_args.kwargs["contents"]
    assert "ДО 5" in exp and "УСЬОГО відео" in exp
    assert "НЕ розподіляй кліпи рівномірно" in exp and "ХУК:" in exp


def test_experimental_reads_a_22_minute_video_in_one_request():
    """Classic splits 80 requested clips into 6 windows; experimental sends the whole transcript once."""
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([_clip(10, 40), _clip(300, 330, hook=9)])
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=53)
    assert mc.models.generate_content.call_count == 1
    prompt = mc.models.generate_content.call_args.kwargs["contents"]
    assert f"ДО {ai_selector.EXPERIMENTAL_MAX_CLIPS}" in prompt  # 53 is capped to 40
    assert "[1310.0s - 1320.0s]" in prompt  # the end of the video is in the same prompt
    assert all(isinstance(c.get("score"), int) for c in clips)
    assert clips[1]["score"] > clips[0]["score"]


def test_overlapping_windows_cover_everything_and_overlap_by_two_minutes():
    segs = _segments(300, chars_per_seg=400)  # 5 h of dense speech ≈ 720k chars
    wins = _overlapping_windows(segs, ai_selector.EXPERIMENTAL_MAX_CHARS, ai_selector.EXPERIMENTAL_OVERLAP_SEC)
    assert len(wins) >= 2
    assert wins[0][0] is segs[0] and wins[-1][-1] is segs[-1]
    for a, b in zip(wins, wins[1:]):
        assert b[0]["start"] <= a[-1]["end"] - 110  # ≈ 2 minutes shared
        assert b[0]["start"] > a[0]["start"]        # always moves forward
    assert _overlapping_windows(_segments(10), 400_000, 120) == [_segments(10)]


def test_experimental_raises_gemini_error_instead_of_inventing_clips(monkeypatch):
    def down(**kw):
        raise ai_selector.GeminiUnavailableError("Gemini перевантажений")
    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", down)
    with pytest.raises(ai_selector.GeminiUnavailableError):
        find_clips_experimental(client=MagicMock(), segments=_segments(5), count=5)


# ─── Hook ────────────────────────────────────────────────────────────────────

def _words(text, t0=0.0, step=0.5):
    return [{"start": t0 + i * step, "end": t0 + i * step + 0.4, "word": w} for i, w in enumerate(text.split())]


def test_start_moves_to_the_hook_phrase_found_in_word_timestamps():
    words = _words("Привіт усім. Ну от сьогодні про гроші. А тепер уявіть, що ваша мама продала квартиру.")
    clip = {"start": 0.0, "end": 30.0, "hook_text": "А тепер уявіть, що ваша мама"}
    out = align_start_to_hook(clip, words)
    assert out["start"] == words[7]["start"] and out["hook_aligned"]
    assert align_start_to_hook({**clip, "hook_text": "чого тут немає"}, words) is not None
    assert align_start_to_hook({**clip, "hook_text": "чого тут немає"}, words)["start"] == 0.0


def test_lead_fillers_are_skipped_but_clip_keeps_min_length():
    words = _words("Ну отже головна помилка підприємців " + "слово " * 60)
    clip = {"start": 0.0, "end": 30.0, "hook_text": "Ну отже головна помилка"}
    out = trim_lead_fillers(clip, words, min_len=15)
    assert out["start"] >= words[2]["start"] - 0.2
    assert out["hook_text"].startswith("головна")
    # a clip at its minimum length is left alone
    assert trim_lead_fillers({"start": 0.0, "end": 15.0}, words, min_len=15)["start"] == 0.0


def test_refine_with_hook_first_starts_on_hook_and_classic_does_not():
    text = "Добрий вечір. " + "Ну це було давно. " * 3 + "Тепер уявіть таку річ. " + "Далі йде історія. " * 20
    words = _words(text)
    hook_i = text.split().index("Тепер")
    clip = {**_clip(0.0, 30.0), "hook_text": "Тепер уявіть таку річ."}
    exp = refine_clips([dict(clip)], words, video_duration=200, count=1, min_len=15, max_len=60, hook_first=True)
    cls = refine_clips([dict(clip)], words, video_duration=200, count=1, min_len=15, max_len=60)
    assert abs(exp[0]["start"] - words[hook_i]["start"]) < 0.3
    assert cls[0]["start"] < 1.0
    assert "hook_aligned" not in exp[0]


# ─── Repeated search keeps marked clips ──────────────────────────────────────

def test_repeated_search_keeps_reviewed_clips_and_skips_their_duplicates():
    db.create_project("p_keep", "v.mp4", "v.mp4", 600.0)
    db.save_candidate_clips("p_keep", [_clip(i * 60.0, i * 60.0 + 30, title=f"old{i}", score=90 - i) for i in range(4)])
    db.update_clip_fields("p_keep", 1, review_status="keep")
    db.update_clip_fields("p_keep", 3, review_status="done")

    db.save_candidate_clips("p_keep", [
        _clip(0.0, 28.0, title="dup of kept"),
        _clip(300.0, 330.0, title="new A"),
        _clip(400.0, 430.0, title="new B"),
    ], keep_reviewed=True)
    clips = {c["title"]: c for c in db.get_candidate_clips("p_keep")}
    assert set(clips) == {"old0", "old2", "new A", "new B"}
    assert clips["old0"]["review_status"] == "keep" and clips["old2"]["review_status"] == "done"
    assert clips["new A"]["clip_index"] > 3 and clips["new B"]["clip_index"] > 3

    # a plain save (first search) still replaces everything
    db.save_candidate_clips("p_keep", [_clip(1.0, 30.0, title="fresh")])
    assert [c["title"] for c in db.get_candidate_clips("p_keep")] == ["fresh"]


# ─── Worker / queue / settings ───────────────────────────────────────────────

def _run_worker(monkeypatch, search_mode, clips_count=53):
    db.create_project("p_mode", "v.mp4", "v.mp4", 1320.0)
    db.create_job("job_mode", "p_mode", "analyze")
    segs = _segments(22)
    monkeypatch.setattr(job_runner, "transcribe_video", lambda p, status_cb=None, language=None: (segs, [], "uk", False))
    calls = {}
    monkeypatch.setattr(job_runner, "analyze_and_find_clips", lambda **kw: calls.setdefault("classic", kw) and [_clip(10, 40)])
    monkeypatch.setattr(job_runner, "find_clips_experimental", lambda **kw: calls.setdefault("experimental", kw) and [_clip(10, 40)])
    monkeypatch.setattr(job_runner, "rerank_clips", lambda clips, **kw: clips)
    job_runner._run_analyze_worker("job_mode", "p_mode", clips_count, "Viral Hooks", search_mode=search_mode)
    return calls


def test_worker_uses_the_chosen_search_and_remembers_it_on_the_project(monkeypatch):
    calls = _run_worker(monkeypatch, "experimental")
    assert list(calls) == ["experimental"] and calls["experimental"]["count"] == 40
    assert db.get_job("job_mode")["status"] == "completed"
    assert db.get_project("p_mode")["search_mode"] == "experimental"


def test_worker_falls_back_to_the_settings_default_which_is_classic(monkeypatch):
    calls = _run_worker(monkeypatch, None)
    assert list(calls) == ["classic"] and calls["classic"]["count"] == 80
    assert db.get_project("p_mode")["search_mode"] == "classic"


def test_queue_and_settings_carry_the_search_mode(monkeypatch):
    r = client.get("/api/settings").json()
    assert r["values"]["studio"]["default_search"] == "classic"
    assert [c["value"] for c in r["choices"]["search"]] == ["classic", "experimental"]
    assert client.post("/api/settings", json={"studio": {"default_search": "experimental"}}).status_code == 200
    assert client.get("/api/settings").json()["values"]["studio"]["default_search"] == "experimental"
    assert client.post("/api/settings", json={"studio": {"default_search": "magic"}}).status_code == 400

    src = server.UPLOADS_DIR / "p_q.mp4"
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    Path(src).write_bytes(b"\0" * 2048)
    db.create_project("p_q", "p_q.mp4", str(src), 600.0)
    seen = []
    monkeypatch.setattr(job_runner, "_run_analyze_worker",
                        lambda job_id, pid, n, f, engine="whisper_local", language=None, search_mode=None: seen.append(search_mode))
    assert client.post("/api/queue", json={"project_ids": ["p_q"], "search_mode": "experimental"}).status_code == 200
    assert client.post("/api/queue", json={"project_ids": ["p_q"], "search_mode": "turbo"}).status_code == 422
    queue_runner.process_next()
    assert seen == ["experimental"]

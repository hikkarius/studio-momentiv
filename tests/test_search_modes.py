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


# ─── Audience hints: YouTube «most replayed», laughter ───────────────────────

import random  # noqa: E402

import reactions  # noqa: E402
from backend import compare  # noqa: E402


def test_replayed_parts_merge_buckets_skip_the_start_and_keep_the_strongest():
    heat = [(i * 10.0, i * 10.0 + 10, 0.1) for i in range(100)]
    heat[0] = (0.0, 10.0, 1.0)              # everybody watches the start: ignored
    heat[40] = (400.0, 410.0, 0.9)          # one part of two buckets
    heat[41] = (410.0, 420.0, 0.7)
    heat[80] = (800.0, 810.0, 0.5)
    parts = reactions.replayed_parts(heat)
    assert parts == [(400.0, 420.0, 0.9), (800.0, 810.0, 0.5)]
    assert reactions.replayed_parts([]) == []
    info = {"heatmap": [{"start_time": 0, "end_time": 5, "value": 0.3}, {"bad": 1}]}
    assert reactions.heatmap_from_info(info) == [(0.0, 5.0, 0.3)]


_REAL_FETCH_HEATMAP = reactions.fetch_heatmap  # taken at import: conftest swaps it for every test


def test_fetch_heatmap_asks_only_youtube(monkeypatch):
    monkeypatch.setattr(reactions.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert _REAL_FETCH_HEATMAP("https://www.twitch.tv/videos/1") == []
    out = MagicMock(stdout='noise\n{"heatmap": [{"start_time": 1, "end_time": 2, "value": 0.5}]}')
    monkeypatch.setattr(reactions.subprocess, "run", lambda *a, **k: out)
    assert _REAL_FETCH_HEATMAP("https://youtu.be/abc") == [(1.0, 2.0, 0.5)]


def test_marks_and_clip_signals():
    segs = [{"start": i * 10.0, "end": i * 10.0 + 10, "text": f"р{i}"} for i in range(30)]
    marked, n = reactions.mark_segments(segs, loud=[(55.0, 12.0)], replayed=[(200.0, 230.0, 0.8)])
    assert n == 2 and "сплеск гучності" in marked[5]["text"] and "глядачі YouTube" in marked[20]["text"]
    assert segs[5]["text"] == "р5"  # the original transcript is untouched
    assert reactions.clip_signals(50, 80, loud=[(55.0, 12.0)], replayed=[(200.0, 230.0, 0.8)]) == ["loud"]
    assert reactions.clip_signals(195, 225, replayed=[(200.0, 230.0, 0.8)]) == ["replayed"]
    assert reactions.clip_signals(0, 30, loud=[(55.0, 12.0)]) == []


def test_experimental_prompt_explains_youtube_marks():
    mc = MagicMock()
    mc.models.generate_content.return_value = _resp([_clip(0, 30)])
    segs, _ = reactions.mark_segments(_segments(5), replayed=[(60.0, 90.0, 0.9)])
    _call_gemini_single_window(mc, segs, 5, 15, 60, "Viral Hooks", "uk", experimental=True,
                               max_chars=ai_selector.EXPERIMENTAL_MAX_CHARS)
    prompt = mc.models.generate_content.call_args.kwargs["contents"]
    assert "глядачі YouTube переглядали найчастіше" in prompt and "Це запис стріму" not in prompt


def test_experimental_job_tags_clips_and_boosts_their_score(monkeypatch):
    db.create_project("p_sig", "v.mp4", "v.mp4", 1320.0)
    db.set_project_source_url("p_sig", "https://youtu.be/abc")
    db.create_job("job_sig", "p_sig", "analyze")
    monkeypatch.setattr(job_runner, "transcribe_video", lambda p, status_cb=None, language=None: (_segments(22), [], "uk", False))
    monkeypatch.setattr(reactions, "loudness_peaks", lambda src: [(15.0, 11.0)])
    asked = []
    monkeypatch.setattr(reactions, "fetch_heatmap",
                        lambda url, timeout=120: asked.append(url) or [(300.0, 330.0, 0.9), (0.0, 10.0, 1.0)])
    seen = {}

    def fake_exp(**kw):
        seen["text"] = "\n".join(s["text"] for s in kw["segments"])
        return [_clip(10, 40, title="LOUD"), _clip(300, 330, title="REPLAYED"), _clip(600, 630, title="PLAIN")]
    monkeypatch.setattr(job_runner, "find_clips_experimental", fake_exp)
    monkeypatch.setattr(job_runner, "rerank_clips", lambda clips, **kw: clips)
    job_runner._run_analyze_worker("job_sig", "p_sig", 10, "Viral Hooks", search_mode="experimental")
    assert db.get_job("job_sig")["status"] == "completed", db.get_job("job_sig")
    assert "[реакція:" in seen["text"] and "глядачі YouTube" in seen["text"]
    clips = {c["title"]: c for c in db.get_candidate_clips("p_sig")}
    assert clips["LOUD"]["signals"] == ["loud"] and clips["REPLAYED"]["signals"] == ["replayed"]
    assert clips["PLAIN"]["signals"] == []
    assert clips["REPLAYED"]["virality_score"] > clips["PLAIN"]["virality_score"]
    # the curve is kept next to the clips: YouTube is asked once
    job_runner._heatmap("p_sig")
    assert asked == ["https://youtu.be/abc"]


def test_source_url_falls_back_to_the_queue_item():
    db.create_project("p_url", "v.mp4", "v.mp4", 60.0)
    assert db.get_project_source_url("p_url") == ""
    db.add_queue_item("q_url", source_url="https://youtu.be/xyz", project_id="p_url")
    assert db.get_project_source_url("p_url") == "https://youtu.be/xyz"


# ─── «Порівняти наосліп» ─────────────────────────────────────────────────────

def test_pairs_are_best_vs_best_in_random_order_and_tally_counts_votes():
    sets = {"classic": [{"score": s} for s in (50, 90, 70)], "experimental": [{"score": s} for s in (80, 95)]}
    pairs = compare.make_pairs(sets, random.Random(1))
    assert len(pairs) == 2
    for k, p in enumerate(pairs):
        assert {p["left"], p["right"]} == {"classic", "experimental"}
        best = {"classic": [1, 2], "experimental": [1, 0]}
        assert p["l"] == best[p["left"]][k] and p["r"] == best[p["right"]][k]
    sides = {p["left"] for p in compare.make_pairs({m: [{"score": 1}] * 8 for m in compare.MODES}, random.Random(3))}
    assert sides == {"classic", "experimental"}  # not always the same side
    pairs[0]["vote"] = "left"
    pairs[1]["vote"] = "same"
    t = compare.tally(pairs)
    assert t[pairs[0]["left"]] == 1 and t["same"] == 1 and t["open"] == 0


def test_compare_runs_both_searches_hides_the_modes_until_done_and_applies_the_winner(monkeypatch):
    src = server.UPLOADS_DIR / "p_cmp.mp4"
    Path(src).parent.mkdir(parents=True, exist_ok=True)
    Path(src).write_bytes(b"\0" * 2048)
    db.create_project("p_cmp", "p_cmp.mp4", str(src), 600.0)
    db.save_candidate_clips("p_cmp", [_clip(0, 30, title="mine", score=60)])
    db.update_clip_fields("p_cmp", 1, review_status="keep")
    monkeypatch.setattr(job_runner, "transcribe_video", lambda p, status_cb=None, language=None: (_segments(10), [], "uk", True),
                        raising=False)
    import core
    monkeypatch.setattr(core, "transcribe_video", lambda p, status_cb=None, language=None: (_segments(10), [], "uk", True))
    modes = []

    def fake_find(pid, proj, segs, words, lang, n, flt, mode, progress=None):
        modes.append(mode)
        base = 100 if mode == "classic" else 300
        return [{**_clip(base + i * 40, base + i * 40 + 30, title=f"{mode}{i}"), "score": 90 - i} for i in range(3)]
    monkeypatch.setattr(job_runner, "find_clips_for", fake_find)
    monkeypatch.setattr(compare.threading, "Thread",
                        lambda target, args, daemon: MagicMock(start=lambda: target(*args)))

    v = client.post("/api/projects/p_cmp/compare").json()
    assert modes == ["classic", "experimental"]
    assert v["status"] == "ready" and len(v["pairs"]) == 3 and not v["done"]
    assert "left_mode" not in v["pairs"][0]
    assert [c["title"] for c in db.get_candidate_clips("p_cmp")] == ["mine"]  # nothing saved yet

    state = compare.load("p_cmp")
    for n, p in enumerate(state["pairs"]):
        pick = "left" if p["left"] == "experimental" else "right"
        v = client.post("/api/projects/p_cmp/compare/vote", json={"n": n, "pick": pick}).json()
    assert v["done"] and v["tally"]["experimental"] == 3 and v["pairs"][0]["left_mode"] in compare.MODES
    assert client.post("/api/projects/p_cmp/compare/vote", json={"n": 9, "pick": "left"}).status_code == 409
    assert client.post("/api/projects/p_cmp/compare/vote", json={"n": 0, "pick": "both"}).status_code == 422

    r = client.post("/api/projects/p_cmp/compare/apply", json={"mode": "experimental"})
    assert r.status_code == 200
    titles = {c["title"] for c in db.get_candidate_clips("p_cmp")}
    assert titles == {"mine", "experimental0", "experimental1", "experimental2"}
    assert db.get_project("p_cmp")["search_mode"] == "experimental"


def test_compare_failure_is_shown_honestly(monkeypatch):
    db.create_project("p_cf", "v.mp4", "v.mp4", 600.0)
    import core
    monkeypatch.setattr(core, "transcribe_video", lambda p, status_cb=None, language=None: (_segments(10), [], "uk", True))

    def down(*a, **kw):
        raise ai_selector.GeminiUnavailableError("Ліміт безкоштовного ключа Gemini вичерпано.")
    monkeypatch.setattr(job_runner, "find_clips_for", down)
    compare.run("p_cf", 10, "Viral Hooks")
    v = client.get("/api/projects/p_cf/compare").json()
    assert v["status"] == "failed" and "Ліміт" in v["error"]


def test_compare_left_running_without_a_thread_is_shown_as_interrupted():
    compare._save("p_orphan", {"status": "running", "progress": 0.3, "message": "Пошук 1 з 2"})
    compare._threads.pop("p_orphan", None)
    v = compare.view("p_orphan")
    assert v["status"] == "failed" and "перервалося" in v["error"]


def test_compare_save_retries_while_windows_holds_the_file(monkeypatch):
    real = compare.os.replace
    fails = {"n": 3}

    def busy(a, b):
        if fails["n"]:
            fails["n"] -= 1
            raise PermissionError("[WinError 5] Access is denied")
        return real(a, b)
    monkeypatch.setattr(compare.os, "replace", busy)
    monkeypatch.setattr(compare.time, "sleep", lambda s: None)
    compare._save("p_busy", {"status": "ready", "pairs": []})
    assert fails["n"] == 0 and compare.load("p_busy")["status"] == "ready"


def test_hook_found_mid_sentence_moves_back_to_the_sentence_start():
    from clip_refiner import start_on_sentence
    words = _words("Все погано. Прилетів снайпер, у котрого три заряди, тімфайт в топі пропав. Далі.")
    clip = {"start": 4.0, "end": 30.0, "hook_text": "тімфайт в топі пропав"}
    out = align_start_to_hook(clip, words)
    assert out["start"] == words[2]["start"]  # «Прилетів»
    # a clip left mid-sentence after overlaps: back to the sentence start if it fits, else forward
    assert start_on_sentence({"start": words[8]["start"], "end": 30.0}, words, 15, 60)["start"] < words[2]["start"] + 0.01
    fwd = start_on_sentence({"start": words[8]["start"], "end": 30.0}, words, 1, words[-1]["end"] + 0.5 - words[2]["start"] - 0.5)
    assert fwd["start"] >= words[11]["end"] - 0.01  # «Далі.»

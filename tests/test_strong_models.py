"""
tests/test_strong_models.py — a stronger Gemini model for the «🧪 Експериментальний» search,
«Що зараз доступно твоєму ключу» and «🧪 Gemini 4 Hydrogen» on the video card.
"""
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import ai_selector
from ai_selector import (_call_gemini_single_window, _is_paid_only, analyze_and_find_clips,
                         find_clips_experimental, rerank_clips, strong_plan)
from backend import db, gemini_models, job_runner
from backend.server import app
from config_loader import CANDIDATE_MODELS, app_config

client = TestClient(app)

HYDROGEN, FLASH38, FLASH36 = "gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash"
BUSY = "503 UNAVAILABLE. This model is currently experiencing high demand."
PAID = ("429 RESOURCE_EXHAUSTED. Quota exceeded for metric: generativelanguage.googleapis.com/"
        "generate_content_free_tier_requests, limit: 0, model: gemini-4-neon")
DAILY = "429 RESOURCE_EXHAUSTED. Quota exceeded: GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20"


def _resp(clips):
    r = MagicMock()
    r.text = json.dumps(clips)
    return r


def _clip(start, end, **kw):
    return {"start": start, "end": end, "title": "T", "category": "content",
            "hook": 7, "emotion": 6, "standalone": 7, "value": 6, **kw}


def _segments(minutes: float):
    return [{"start": i * 10.0, "end": i * 10.0 + 10.0, "text": "с" * 120} for i in range(int(minutes * 6))]


def _client(answers: dict):
    """Fake Gemini: answers[model] is a list of results in order (an Exception is raised)."""
    mc = MagicMock()
    asked = []

    def gen(model, contents, config):
        asked.append(model)
        queue = answers.get(model, [])
        out = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else Exception(BUSY))
        if isinstance(out, Exception):
            raise out
        return out

    mc.models.generate_content.side_effect = gen
    mc.asked = asked
    return mc


@pytest.fixture
def no_sleep(monkeypatch):
    rec = []
    monkeypatch.setattr("ai_selector.time.sleep", lambda s: rec.append(s))
    return rec


@pytest.fixture
def auto(monkeypatch):
    monkeypatch.setattr(app_config.gemini, "experimental_model", "auto")
    monkeypatch.setattr(app_config.gemini, "strong_models", [HYDROGEN, FLASH38, FLASH36])


# ─── Which models, in which order ────────────────────────────────────────────

def test_plan_auto_fast_chosen_and_out_of_limit(monkeypatch, auto):
    assert strong_plan() == [(HYDROGEN, 1), (FLASH38, 1), (FLASH36, 1)]
    ai_selector.mark_exhausted_today(FLASH38)
    assert strong_plan() == [(HYDROGEN, 1), (FLASH36, 1)]
    monkeypatch.setattr(app_config.gemini, "experimental_model", HYDROGEN)
    assert strong_plan() == [(HYDROGEN, 3), (FLASH36, 1)]  # a chosen model gets a few tries, then the others
    monkeypatch.setattr(app_config.gemini, "experimental_model", "fast")
    assert strong_plan() == []


def test_paid_only_is_told_from_a_used_up_daily_limit():
    assert _is_paid_only(PAID)
    assert not _is_paid_only(DAILY) and not _is_paid_only(BUSY)


# ─── Experimental search ─────────────────────────────────────────────────────

def test_experimental_asks_the_strongest_model_first_with_more_thinking_and_time(auto):
    mc = _client({HYDROGEN: [_resp([_clip(10, 40)])]})
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert mc.asked == [HYDROGEN]
    assert clips[0]["_model"] == HYDROGEN
    cfg = mc.models.generate_content.call_args.kwargs["config"]
    assert cfg.thinking_config.thinking_level.name == "LOW"
    assert cfg.http_options.timeout == app_config.gemini.strong_timeout_sec * 1000


def test_busy_strong_models_fall_through_to_the_fast_ones(auto, no_sleep):
    lite = CANDIDATE_MODELS[0]
    mc = _client({HYDROGEN: [Exception(BUSY)], FLASH38: [Exception(BUSY)], FLASH36: [Exception(BUSY)],
                  lite: [_resp([_clip(10, 40)])]})
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert mc.asked[:4] == [HYDROGEN, FLASH38, FLASH36, lite]
    assert clips[0]["_model"] == lite
    assert not ai_selector._exhausted_today()  # «busy» is not a limit
    assert no_sleep == []                     # auto does not wait for a busy strong model


def test_paid_only_and_daily_limit_are_remembered_and_skipped_next_time(auto):
    mc = _client({HYDROGEN: [Exception(PAID)], FLASH38: [Exception(DAILY)], FLASH36: [_resp([_clip(10, 40)])]})
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert clips[0]["_model"] == FLASH36
    assert ai_selector._exhausted_today() == {HYDROGEN, FLASH38}
    mc2 = _client({FLASH36: [_resp([_clip(10, 40)])]})
    find_clips_experimental(client=mc2, segments=_segments(22), count=10)
    assert mc2.asked == [FLASH36]


def test_a_chosen_model_is_retried_with_a_pause_then_the_fast_ones_answer(monkeypatch, no_sleep):
    monkeypatch.setattr(app_config.gemini, "experimental_model", HYDROGEN)
    lite = CANDIDATE_MODELS[0]
    mc = _client({HYDROGEN: [Exception(BUSY)], FLASH38: [Exception(BUSY)], FLASH36: [Exception(BUSY)],
                  lite: [_resp([_clip(10, 40)])]})
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert mc.asked[:6] == [HYDROGEN, HYDROGEN, HYDROGEN, FLASH38, FLASH36, lite]
    assert no_sleep[:2] == [20.0, 40.0]
    assert clips[0]["_model"] == lite


def test_fast_choice_and_the_classic_search_never_touch_strong_models(monkeypatch, auto):
    lite = CANDIDATE_MODELS[0]
    mc = _client({lite: [_resp([_clip(10, 40)])]})
    analyze_and_find_clips(client=mc, segments=_segments(5), count=5)
    assert set(mc.asked) <= set(CANDIDATE_MODELS)
    monkeypatch.setattr(app_config.gemini, "experimental_model", "fast")
    mc = _client({lite: [_resp([_clip(10, 40)])]})
    find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert mc.asked == [lite]


def test_wrong_key_on_a_strong_model_still_gives_the_honest_key_message(auto):
    bad = Exception("400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key.")
    mc = _client({m: [bad] for m in [HYDROGEN, FLASH38, FLASH36, *CANDIDATE_MODELS]})
    with pytest.raises(ai_selector.GeminiUnavailableError) as exc:
        _call_gemini_single_window(mc, _segments(5), 5, 15, 60, "Viral Hooks", "uk",
                                   experimental=True, strong=strong_plan())
    assert str(exc.value) == ai_selector.GEMINI_BAD_KEY_MESSAGE
    assert mc.asked[0] == HYDROGEN and FLASH38 not in mc.asked  # stops at the first «wrong key»


def test_rerank_asks_the_strong_model_that_found_the_clips_first():
    mc = _client({HYDROGEN: [_resp([{"n": 1, "score": 90}, {"n": 2, "score": 40}])]})
    clips = [{**_clip(10, 40), "score": 60}, {**_clip(60, 90), "score": 60}]
    out = rerank_clips(clips, client=mc, strong_model=HYDROGEN)
    assert mc.asked == [HYDROGEN] and out[0]["score"] > out[1]["score"]
    mc = _client({CANDIDATE_MODELS[0]: [_resp([{"n": 1, "score": 90}, {"n": 2, "score": 40}])]})
    rerank_clips(clips, client=mc)
    assert mc.asked == [CANDIDATE_MODELS[0]]


# ─── The video card remembers the model ──────────────────────────────────────

def test_worker_saves_which_model_chose_the_clips_and_reranks_with_it(monkeypatch):
    db.create_project("p_model", "v.mp4", "v.mp4", 1320.0)
    db.create_job("job_model", "p_model", "analyze")
    segs = _segments(22)
    monkeypatch.setattr(job_runner, "transcribe_video", lambda p, status_cb=None, language=None: (segs, [], "uk", False))
    monkeypatch.setattr(job_runner, "find_clips_experimental",
                        lambda **kw: [{**_clip(10, 40), "_model": HYDROGEN}, {**_clip(100, 130), "_model": HYDROGEN}])
    seen = {}
    monkeypatch.setattr(job_runner, "rerank_clips", lambda clips, **kw: seen.update(kw) or clips)
    job_runner._run_analyze_worker("job_model", "p_model", 10, "Viral Hooks", search_mode="experimental")
    assert db.get_job("job_model")["status"] == "completed"
    assert db.get_project("p_model")["ai_model"] == HYDROGEN
    assert seen.get("strong_model") == HYDROGEN
    listed = {p["id"]: p for p in client.get("/api/projects").json()["projects"]}
    assert listed.get("p_model", {}).get("ai_model", HYDROGEN) == HYDROGEN


# ─── «Що зараз доступно твоєму ключу» ────────────────────────────────────────

def test_check_models_reports_each_model_and_remembers_limits(monkeypatch, auto):
    monkeypatch.setattr(gemini_models, "CHECK_PATH", ai_selector.QUOTA_STATE_PATH.parent / "models_check.json")
    lite = CANDIDATE_MODELS[0]
    mc = _client({HYDROGEN: [Exception(BUSY)], FLASH38: [Exception(PAID)], FLASH36: [_resp([])],
                  lite: [Exception(DAILY)]})
    data = gemini_models.check_models(client=mc)
    rows = {r["model"]: r for r in data["models"]}
    assert [r["role"] for r in data["models"]][:3] == ["strong"] * 3
    assert rows[HYDROGEN]["status"] == "busy" and rows[HYDROGEN]["name"] == "Gemini 4 Hydrogen"
    assert rows[FLASH38]["status"] == "paid" and rows[FLASH38]["text"] == "лише з платним ключем"
    assert rows[FLASH36]["status"] == "ok" and rows[FLASH36]["seconds"] is not None
    assert rows[lite]["status"] == "limit"
    assert {FLASH38, lite} <= ai_selector._exhausted_today()
    assert gemini_models.last_check()["models"] == data["models"]


def test_classify_and_pretty_names():
    assert gemini_models.classify("404 NOT_FOUND models/gemini-9 is not found") == "missing"
    assert gemini_models.classify("429 RESOURCE_EXHAUSTED per minute") == "minute"
    assert gemini_models.classify("API key not valid") == "bad_key"
    assert gemini_models.pretty_name("gemini-3.5-flash-lite") == "Gemini 3.5 Flash-Lite"
    assert gemini_models.pretty_name("gemini-3.8-flash") == "Gemini 3.8 Flash"


def test_models_endpoints_and_the_experimental_model_setting(monkeypatch, auto):
    monkeypatch.setattr(gemini_models, "CHECK_PATH", ai_selector.QUOTA_STATE_PATH.parent / "models_check.json")
    assert client.get("/api/gemini/models").json() == {"check": None}
    monkeypatch.setattr("config_loader.ensure_gemini_api_key", lambda: "")
    assert client.post("/api/gemini/models/check").status_code == 400  # no key yet
    monkeypatch.setattr("config_loader.ensure_gemini_api_key", lambda: "k" * 39)
    monkeypatch.setattr(gemini_models, "check_models", lambda: {"checked_at": 1.0, "models": []})
    assert client.post("/api/gemini/models/check").json()["check"]["checked_at"] == 1.0

    view = client.get("/api/settings").json()
    assert view["values"]["gemini"]["experimental_model"] == "auto"
    assert [c["value"] for c in view["choices"]["experimental_model"]] == ["auto", "fast"]
    assert client.post("/api/settings", json={"gemini": {"experimental_model": "fast"}}).status_code == 200
    assert app_config.gemini.experimental_model == "fast"
    assert client.post("/api/settings", json={"gemini": {"experimental_model": "gpt-5"}}).status_code == 400


def test_a_strong_model_that_times_out_sends_the_search_to_the_fast_ones(auto):
    lite = CANDIDATE_MODELS[0]
    mc = _client({HYDROGEN: [Exception("httpx.ReadTimeout: The read operation timed out")],
                  lite: [_resp([_clip(10, 40)])]})
    clips = find_clips_experimental(client=mc, segments=_segments(22), count=10)
    assert mc.asked == [HYDROGEN, lite]  # 3.8 and 3.6 would wait just as long on this video
    assert clips[0]["_model"] == lite

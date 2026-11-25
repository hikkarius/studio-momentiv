"""Free-tier request budget: only Lite models, at most 4 requests per window per round,
a per-model requests-per-minute limiter, and models out of their daily limit are skipped until reset."""
from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock

import pytest

import ai_selector
from ai_selector import (MAX_ATTEMPTS_PER_WINDOW, GeminiIncompleteError, GeminiQuotaError,
                         analyze_and_find_clips)
from config_loader import CANDIDATE_MODELS, app_config
from config_schema import GeminiConfig

_DAILY_429 = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your current quota.', "
              "'details': [{'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}]}}")


def _segments(n=300):
    return [{"start": i * 2.0, "end": i * 2.0 + 1.8, "text": f"речення {i}."} for i in range(n)]


@pytest.fixture
def sleeps(monkeypatch):
    rec: list[float] = []
    monkeypatch.setattr("ai_selector.time.sleep", lambda s: rec.append(s))
    return rec


def _ok(text='[{"start": 1, "end": 30, "title": "ОК"}]'):
    r = MagicMock()
    r.text = text
    return r


# ─── 1. Only Lite models in the automatic list ────────────────────────────────

def test_only_lite_models_are_used_automatically():
    assert CANDIDATE_MODELS == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]
    assert GeminiConfig().candidate_models == CANDIDATE_MODELS
    assert all("lite" in m for m in CANDIDATE_MODELS)


def test_rerank_asks_lite_models_only(monkeypatch):
    asked = []
    fake = MagicMock()

    def gen(model, contents, config):
        asked.append(model)
        raise Exception("503 UNAVAILABLE")

    fake.models.generate_content.side_effect = gen
    clips = [{"start": 0, "end": 20, "score": 60}, {"start": 30, "end": 50, "score": 70}]
    assert ai_selector.rerank_clips(clips, client=fake) == clips  # scores kept on failure
    assert asked == CANDIDATE_MODELS


# ─── 2. At most 4 requests per window per round ───────────────────────────────

def test_one_window_makes_at_most_four_requests_per_round(sleeps):
    fake = MagicMock()
    fake.models.generate_content.side_effect = Exception("503 UNAVAILABLE high demand")
    with pytest.raises(GeminiIncompleteError):
        analyze_and_find_clips(client=fake, segments=_segments(20), count=5)
    assert MAX_ATTEMPTS_PER_WINDOW == 4
    # 4 tries of the Lite models; no second round when Google answered nothing; the classic search
    # does not spend the fallback model's small daily limit
    assert fake.models.generate_content.call_count == 4
    models = [c.kwargs["model"] for c in fake.models.generate_content.call_args_list]
    assert models == ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"] * 2


def test_worst_case_for_a_long_video_is_bounded(sleeps):
    """5 windows, Google down: it gives up after a handful of «overloaded» answers in all windows
    (was 5 × 4 × 2 rounds = 40 requests and 3-4 minutes)."""
    fake = MagicMock()
    fake.models.generate_content.side_effect = Exception("503 UNAVAILABLE")
    with pytest.raises(GeminiIncompleteError):
        analyze_and_find_clips(client=fake, segments=_segments(), count=75)
    assert fake.models.generate_content.call_count <= ai_selector.OVERLOAD_TRIP + 3  # + windows in flight
    models = [c.kwargs["model"] for c in fake.models.generate_content.call_args_list]
    assert "gemini-3.6-flash" not in models


def test_fallback_model_answers_the_experimental_search_when_every_lite_model_is_overloaded(sleeps):
    fake = MagicMock()
    fake.models.generate_content.side_effect = [Exception("503 UNAVAILABLE")] * 4 + [_ok()]
    clips = ai_selector.find_clips_experimental(client=fake, segments=_segments(20), count=1)
    assert clips[0]["title"] == "ОК"
    assert fake.models.generate_content.call_args_list[-1].kwargs["model"] == "gemini-3.6-flash"


def test_fallback_model_is_not_used_for_a_limit_or_a_wrong_key(sleeps):
    for err in ("429 RESOURCE_EXHAUSTED quota", "400 API key not valid"):
        fake = MagicMock()
        fake.models.generate_content.side_effect = Exception(err)
        with pytest.raises(Exception):
            analyze_and_find_clips(client=fake, segments=_segments(20), count=1)
        models = [c.kwargs["model"] for c in fake.models.generate_content.call_args_list]
        assert "gemini-3.6-flash" not in models


def test_answer_on_the_third_try_stops_there(sleeps):
    fake = MagicMock()
    fake.models.generate_content.side_effect = [Exception("503"), Exception("503"), _ok()]
    clips = analyze_and_find_clips(client=fake, segments=_segments(20), count=1)
    assert clips[0]["title"] == "ОК"
    assert fake.models.generate_content.call_count == 3
    assert sleeps == [5.0]  # one pause, when the model list started over


# ─── Requests-per-minute limiter ──────────────────────────────────────────────

class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_limiter_holds_the_13th_request_of_a_minute():
    clock = _Clock()
    lim = ai_selector._RateLimiter(clock=clock.now, sleep=clock.sleep)
    for _ in range(12):
        lim.acquire("m", 12)
        clock.t += 1.0  # 12 requests over 12 s
    assert clock.slept == []
    lim.acquire("m", 12)  # 13th: waits until the first one is 60 s old
    assert len(clock.slept) == 1 and 47.0 <= clock.slept[0] <= 49.0
    lim.acquire("other-model", 12)  # each model has its own budget
    assert len(clock.slept) == 1


def test_limiter_off_when_zero():
    clock = _Clock()
    lim = ai_selector._RateLimiter(clock=clock.now, sleep=clock.sleep)
    for _ in range(100):
        lim.acquire("m", 0)
    assert clock.slept == []


def test_limiter_is_shared_by_parallel_windows():
    clock_lock = threading.Lock()
    clock = _Clock()

    def now():
        with clock_lock:
            return clock.t

    def sleep(s):
        with clock_lock:
            clock.slept.append(s)
            clock.t += s

    lim = ai_selector._RateLimiter(clock=now, sleep=sleep)
    threads = [threading.Thread(target=lambda: [lim.acquire("m", 12) for _ in range(5)]) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(clock.slept) >= 1  # 15 requests from 3 windows: some had to wait


def test_generate_goes_through_the_limiter(monkeypatch):
    seen = []
    monkeypatch.setattr(app_config.gemini, "max_rpm_per_model", 12)
    monkeypatch.setattr(ai_selector._rate_limiter, "acquire", lambda m, limit: seen.append((m, limit)))
    fake = MagicMock()
    fake.models.generate_content.return_value = _ok()
    ai_selector._generate(fake, "gemini-3.1-flash-lite", "p", {}, 0.3)
    assert seen == [("gemini-3.1-flash-lite", 12)]


# ─── Daily limit remembered until Google resets it ───────────────────────────

def test_daily_limit_model_is_skipped_until_reset_and_remembered_on_disk(sleeps):
    fake = MagicMock()
    fake.models.generate_content.side_effect = [Exception(_DAILY_429), _ok()]
    clips = analyze_and_find_clips(client=fake, segments=_segments(20), count=1)
    assert clips[0]["title"] == "ОК"
    assert "gemini-3.1-flash-lite" in ai_selector._exhausted_today()
    state = json.loads(ai_selector.QUOTA_STATE_PATH.read_text(encoding="utf-8"))
    assert state["gemini-3.1-flash-lite"] > time.time() + 60  # until the next Pacific midnight

    # next analysis: the exhausted model is not asked at all
    fake2 = MagicMock()
    fake2.models.generate_content.return_value = _ok()
    analyze_and_find_clips(client=fake2, segments=_segments(20), count=1)
    assert [c.kwargs["model"] for c in fake2.models.generate_content.call_args_list] == ["gemini-3.5-flash-lite"]


def test_all_models_out_for_the_day_sends_no_request(sleeps):
    for m in CANDIDATE_MODELS:
        ai_selector.mark_exhausted_today(m)
    fake = MagicMock()
    with pytest.raises(GeminiIncompleteError) as exc:
        analyze_and_find_clips(client=fake, segments=_segments(20), count=1)
    assert exc.value.daily is True and isinstance(exc.value.cause, GeminiQuotaError)
    fake.models.generate_content.assert_not_called()


def test_note_expires_after_the_reset():
    ai_selector.mark_exhausted_today("gemini-3.1-flash-lite", now=1_000_000.0)
    assert ai_selector._exhausted_today(now=1_000_000.0) == {"gemini-3.1-flash-lite"}
    assert ai_selector._exhausted_today(now=1_000_000.0 + 2 * 86400) == set()


def test_per_minute_429_is_not_remembered_for_the_day(sleeps):
    fake = MagicMock()
    fake.models.generate_content.side_effect = [
        Exception("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerMinutePerProjectPerModel-FreeTier"), _ok()]
    analyze_and_find_clips(client=fake, segments=_segments(20), count=1)
    assert ai_selector._exhausted_today() == set()


def test_quota_note_file_survives_updates():
    from pathlib import Path
    ps1 = (Path(__file__).resolve().parent.parent / "installer" / "update.ps1").read_text(encoding="utf-8-sig")
    assert '"gemini_quota_state.json"' in ps1

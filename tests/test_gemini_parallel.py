"""Long videos: transcript windows go to Gemini in parallel, merged exactly as one-by-one."""
from __future__ import annotations

import threading
import time

import pytest

import ai_selector
from ai_selector import GeminiQuotaError, analyze_and_find_clips
from config_loader import app_config


def _segments(n=300):
    return [{"start": i * 6.0, "end": i * 6.0 + 5.0, "text": f"речення {i}"} for i in range(n)]


@pytest.fixture
def sleeps(monkeypatch):
    rec: list[float] = []
    monkeypatch.setattr("ai_selector.time.sleep", lambda s: rec.append(s))
    return rec


def _window_clips(segs, per_window=15):
    """Deterministic fake answer: clips spread over the window, scores depend on position."""
    out = []
    for k in range(per_window):
        seg = segs[(k * len(segs)) // per_window]
        out.append({"start": seg["start"], "end": seg["start"] + 20, "title": f"{seg['start']}",
                    "score": int(seg["start"]) % 97})
    return out


def test_parallel_merge_matches_one_by_one(monkeypatch):
    def fake_window(**kw):
        # the first windows answer last, to shake up the completion order
        time.sleep(0.05 * (5 - int(kw["segments"][0]["start"] // 360)) / 5)
        return _window_clips(kw["segments"])

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    monkeypatch.setattr(app_config.gemini, "parallel_windows", 1)
    one_by_one = analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    monkeypatch.setattr(app_config.gemini, "parallel_windows", 3)
    parallel = analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert parallel == one_by_one
    assert [c["start"] for c in parallel] == sorted(c["start"] for c in parallel)


def test_at_most_three_windows_at_once(monkeypatch):
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_window(**kw):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return _window_clips(kw["segments"], 3)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    monkeypatch.setattr(app_config.gemini, "parallel_windows", 3)
    analyze_and_find_clips(client=object(), segments=_segments(), count=75)  # 5 windows
    assert active["max"] == 3


def test_per_minute_limit_waits_and_retries_the_window(monkeypatch, sleeps):
    calls: dict[float, int] = {}
    lock = threading.Lock()

    def fake_window(**kw):
        first = kw["segments"][0]["start"]
        with lock:
            calls[first] = calls.get(first, 0) + 1
            n = calls[first]
        if first == 360.0 and n == 1:  # window 2 hits the per-minute limit once
            raise GeminiQuotaError("limit", daily=False, retry_after=41.0)
        return _window_clips(kw["segments"], 3)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    monkeypatch.setattr(app_config.gemini, "parallel_windows", 1)  # deterministic call order
    clips = analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert sleeps == [43.0]                  # Google's delay + 2 s, longer than the 30 s default
    assert sorted(calls.values()) == [1, 1, 1, 1, 2]
    starts = {c["start"] for c in clips}
    assert any(360 <= s < 720 for s in starts)  # the retried window's clips are in the result


def test_daily_limit_is_not_retried_and_reaches_the_queue(monkeypatch, sleeps):
    def fake_window(**kw):
        raise GeminiQuotaError("day", daily=True)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    with pytest.raises(GeminiQuotaError) as exc:
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert exc.value.daily is True
    assert sleeps == []


def test_per_minute_limit_that_never_lifts_gives_up(monkeypatch, sleeps):
    def fake_window(**kw):
        raise GeminiQuotaError("minute", daily=False)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    monkeypatch.setattr(app_config.gemini, "parallel_windows", 1)
    with pytest.raises(GeminiQuotaError):
        analyze_and_find_clips(client=object(), segments=_segments(), count=75)
    assert sleeps == [30.0, 60.0] * 5  # two pauses per window, then the queue takes over

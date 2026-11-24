"""Batched Whisper: segment splitting, refilling unheard speech, out-of-memory fallback,
and the model's background preload / idle unload."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import transcription
from state import AppState


def _w(start, end, word):
    return {"start": start, "end": end, "word": word}


# ─── Splitting batched segments back into natural ones ────────────────────────

def test_split_on_sentence_end_and_pause():
    words = [
        _w(0.0, 0.5, "Привіт,"), _w(0.6, 1.1, "друзі."),           # sentence ends
        _w(1.2, 1.5, "Сьогодні"), _w(1.6, 1.8, "граємо"),
        _w(2.6, 3.0, "далі"), _w(3.1, 3.5, "йдемо"),               # 0.8 s pause before «далі»
    ]
    segs = transcription.split_segments_from_words(words)
    assert [s["text"] for s in segs] == ["Привіт, друзі.", "Сьогодні граємо", "далі йдемо"]
    assert segs[0]["start"] == 0.0 and segs[0]["end"] == 1.1
    assert segs[2]["start"] == 2.6


def test_very_short_sentence_is_not_cut_off_alone():
    words = [_w(0.0, 0.3, "Так."), _w(0.35, 0.7, "Це"), _w(0.75, 1.2, "правда.")]
    segs = transcription.split_segments_from_words(words)
    assert [s["text"] for s in segs] == ["Так. Це правда."]


def test_long_run_without_punctuation_is_capped_at_a_comma():
    words = []
    t = 0.0
    for i in range(40):  # 40 words × 0.5 s = 20 s of speech, no pauses, no full stops
        text = f"слово{i}," if i == 15 else f"слово{i}"
        words.append(_w(round(t, 2), round(t + 0.45, 2), text))
        t += 0.5
    segs = transcription.split_segments_from_words(words, max_len=12.0)
    assert all(s["end"] - s["start"] <= 12.0 for s in segs)
    assert segs[0]["text"].endswith("слово15,")  # cut at the comma, not mid-phrase
    assert sum(len(s["text"].split()) for s in segs) == 40  # nothing lost


def test_split_of_nothing_is_nothing():
    assert transcription.split_segments_from_words([]) == []


# ─── Refilling speech that batched decoding skipped ─────────────────────────

def test_finds_the_skipped_region_like_the_real_video():
    # VAD pieces around the real case: speech at 988.9-998.4 heard, 1001.7-1015.5 lost
    speech = [(988.9, 998.4), (1001.7, 1003.2), (1003.3, 1004.9), (1005.9, 1007.0),
              (1007.1, 1013.3), (1013.8, 1015.5), (1015.9, 1020.4)]
    heard = [_w(989.0 + i * 0.4, 989.3 + i * 0.4, "слово") for i in range(23)]   # 989-998
    heard.append(_w(1002.0, 1002.1, "У"))                                        # one stray word
    heard += [_w(1016.0 + i * 0.4, 1016.3 + i * 0.4, "далі") for i in range(10)]
    gaps = transcription.find_unheard_regions(speech, heard)
    assert gaps == [(1001.7, 1015.5)]


def test_short_or_heard_regions_are_left_alone():
    speech = [(0.0, 1.5), (10.0, 14.0)]
    words = [_w(10.0 + i * 0.5, 10.4 + i * 0.5, "так") for i in range(8)]
    assert transcription.find_unheard_regions(speech, words) == []


def test_refilled_words_replace_stray_ones_in_time_order():
    words = [_w(1.0, 1.2, "раз"), _w(5.0, 5.1, "У"), _w(9.0, 9.2, "три")]
    refill = {(4.0, 8.0): [_w(4.1, 4.5, "Привіт,"), _w(4.6, 5.0, "я"), _w(5.1, 5.6, "тут.")]}
    out = transcription.merge_refilled_words(words, refill)
    assert [w["word"] for w in out] == ["раз", "Привіт,", "я", "тут.", "три"]


def test_refill_that_hears_less_keeps_the_original():
    words = [_w(4.1, 4.5, "один"), _w(4.6, 5.0, "два")]
    out = transcription.merge_refilled_words(words, {(4.0, 8.0): [_w(4.1, 4.5, "один")]})
    assert [w["word"] for w in out] == ["один", "два"]


# ─── Batch size and out-of-memory fallback ──────────────────────────────────

def test_batch_size_only_on_gpu(monkeypatch):
    monkeypatch.setitem(transcription.CONFIG["whisper"], "batch_size", 8)
    assert transcription.whisper_batch_size("cuda") == 8
    assert transcription.whisper_batch_size("cpu") == 0
    monkeypatch.setitem(transcription.CONFIG["whisper"], "batch_size", 0)
    assert transcription.whisper_batch_size("cuda") == 0


def _fake_sequential_model():
    seg = SimpleNamespace(start=0.0, end=1.0, text=" Привіт", words=[SimpleNamespace(start=0.0, end=1.0, word=" Привіт")])
    model = MagicMock()
    model.transcribe.return_value = (iter([seg]), SimpleNamespace(language="uk", language_probability=1.0, duration=1.0))
    return model


@pytest.fixture
def no_audio(monkeypatch):
    import faster_whisper.audio
    monkeypatch.setattr(faster_whisper.audio, "decode_audio", lambda *a, **k: [0.0] * 16000)


def test_out_of_memory_steps_down_to_4_then_sequential(monkeypatch, no_audio):
    tried = []

    def oom(model, audio, kwargs, cfg_lang, status_cb, b, label):
        tried.append(b)
        raise RuntimeError("CUDA failed with error out of memory")

    monkeypatch.setattr(transcription, "_batched_pass", oom)
    model = _fake_sequential_model()
    msgs = []
    segs, words, lang = transcription.faster_whisper_pass(
        model, "a.wav", {"language": "uk"}, "uk", lambda *a: msgs.append(a[-1]), batch_size=8, device="cuda")
    assert tried == [8, 4]
    assert model.transcribe.call_count == 1  # the old sequential mode finished the job
    assert [w["word"] for w in words] == ["Привіт"] and lang == "uk"
    assert any("відеопам'яті" in m for m in msgs)


def test_smaller_batch_that_fits_is_used(monkeypatch, no_audio):
    tried = []

    def pass_(model, audio, kwargs, cfg_lang, status_cb, b, label):
        tried.append(b)
        if b == 8:
            raise RuntimeError("out of memory")
        return [{"start": 0, "end": 1, "text": "так"}], [_w(0, 1, "так")], "uk"

    monkeypatch.setattr(transcription, "_batched_pass", pass_)
    model = _fake_sequential_model()
    segs, words, lang = transcription.faster_whisper_pass(model, "a.wav", {}, "uk", None, batch_size=8, device="cuda")
    assert tried == [8, 4] and segs[0]["text"] == "так"
    model.transcribe.assert_not_called()


def test_other_errors_are_not_swallowed(monkeypatch, no_audio):
    def broken(*a):
        raise ValueError("something else")

    monkeypatch.setattr(transcription, "_batched_pass", broken)
    with pytest.raises(ValueError):
        transcription.faster_whisper_pass(_fake_sequential_model(), "a.wav", {}, "uk", None, batch_size=8, device="cuda")


def test_cpu_stays_sequential(monkeypatch):
    monkeypatch.setattr(transcription, "_batched_pass", MagicMock(side_effect=AssertionError("no batching on CPU")))
    model = _fake_sequential_model()
    transcription.faster_whisper_pass(model, "a.wav", {}, "uk", None, batch_size=0, device="cpu")
    model.transcribe.assert_called_once()


# ─── Preload at start, unload when idle ─────────────────────────────────────

def test_idle_model_is_unloaded_and_busy_one_is_kept():
    st = AppState()
    st._whisper = object()
    st._whisper_last_used = 1000.0
    assert not st.unload_idle_whisper(600, now=1300.0)   # only 5 min idle
    st.whisper_acquire()
    assert not st.unload_idle_whisper(600, now=99999.0)  # a transcription is running
    st.whisper_release()
    assert st._whisper is not None
    assert st.unload_idle_whisper(600, now=st._whisper_last_used + 601)
    assert st._whisper is None


def test_zero_minutes_means_never_unload():
    st = AppState()
    st._whisper = object()
    st._whisper_last_used = 0.0
    assert not st.unload_idle_whisper(0, now=1e9)
    assert st._whisper is not None


def test_preload_loads_in_background(monkeypatch):
    st = AppState()
    loaded = []
    monkeypatch.setattr(st, "get_whisper", lambda: loaded.append(True) or "model")
    st.preload_whisper_async().join(timeout=5)
    assert loaded == [True]


def test_preload_failure_does_not_crash(monkeypatch):
    st = AppState()

    def boom():
        raise RuntimeError("no GPU")

    monkeypatch.setattr(st, "get_whisper", boom)
    st.preload_whisper_async().join(timeout=5)  # just logs


def test_transcription_marks_the_model_busy(monkeypatch, tmp_path):
    """While transcribe_video runs, the idle unloader must leave the model alone."""
    from state import app_state
    seen_busy = []
    model = _fake_sequential_model()

    def fake_get():
        seen_busy.append(app_state._whisper_busy)
        return model

    monkeypatch.setattr(transcription, "get_whisper", fake_get)
    monkeypatch.setattr(transcription, "extract_audio", lambda v, a: a)
    monkeypatch.setattr(transcription, "save_transcript_cache", lambda *a, **k: None)
    monkeypatch.setattr(transcription, "load_transcript_cache_for", lambda *a: None)
    monkeypatch.setattr(app_state, "whisper_device", "cpu")
    before = app_state._whisper_busy
    t0 = time.monotonic()
    transcription.transcribe_video(str(tmp_path / "v.mp4"), language="uk")
    assert seen_busy == [before + 1]
    assert app_state._whisper_busy == before
    assert app_state._whisper_last_used >= t0


def test_refill_keeps_a_heard_word_right_at_the_region_edge():
    words = [_w(3.9, 4.2, "до"), _w(8.1, 8.5, "після")]  # just outside 4.0–8.0, heard by batched decoding
    refill = {(4.0, 8.0): [_w(4.3, 4.8, "Привіт,"), _w(5.0, 5.6, "друзі.")]}
    out = transcription.merge_refilled_words(words, refill)
    assert [w["word"] for w in out] == ["до", "Привіт,", "друзі.", "після"]

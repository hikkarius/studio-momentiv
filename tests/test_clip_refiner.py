"""
tests/test_clip_refiner.py — Sentence snapping, length limits, overlap removal and filtering of clips.
"""
from clip_refiner import refine_clips, resolve_overlaps, rubric_score, snap_clip


def _words(text: str, start: float = 0.0, step: float = 0.5) -> list[dict]:
    """One word every `step` seconds."""
    out = []
    t = start
    for w in text.split():
        out.append({"start": t, "end": t + step * 0.8, "word": w})
        t += step
    return out


# 4 sentences of 20 words each → each sentence lasts 10 s (0–10, 10–20, 20–30, 30–40)
SENT = "слово " * 19 + "кінець."
WORDS = _words(" ".join([SENT] * 4))


def test_snap_moves_bounds_to_sentence_edges():
    clip = {"start": 11.3, "end": 28.7, "title": "T", "score": 80}
    out = snap_clip(clip, WORDS, 15, 60, 40.0)
    assert out is not None
    assert abs(out["start"] - 10.0) < 0.3      # start of 2nd sentence (minus a small pad)
    assert abs(out["end"] - 29.9) < 0.3        # end of 3rd sentence (plus a small pad)
    assert out["hook_text"].startswith("слово")


def test_too_short_clip_is_extended_to_full_sentences():
    clip = {"start": 10.0, "end": 19.0, "title": "SHORT", "score": 80}   # 9 s from Gemini
    out = snap_clip(clip, WORDS, 15, 60, 40.0)
    assert out is not None
    assert 15 <= out["end"] - out["start"] <= 60


def test_too_long_clip_is_cut_on_a_sentence_end():
    clip = {"start": 0.0, "end": 40.0, "title": "LONG", "score": 80}
    out = snap_clip(clip, WORDS, 15, 25, 40.0)
    assert out is not None
    assert out["end"] - out["start"] <= 25.5
    assert abs(out["end"] - 19.9) < 0.3


def test_overlapping_clips_keep_the_higher_score():
    a = {"start": 0.0, "end": 20.0, "title": "A", "score": 90}
    b = {"start": 10.0, "end": 40.0, "title": "B", "score": 70}
    out = resolve_overlaps([b, a], WORDS, 15, 60, 40.0, max_overlap=2.0)
    titles = {c["title"] for c in out}
    assert "A" in titles
    for x in out:
        for y in out:
            if x is not y:
                assert min(x["end"], y["end"]) - max(x["start"], y["start"]) <= 2.0


def test_overlapping_clip_is_trimmed_when_enough_remains():
    a = {"start": 0.0, "end": 20.0, "title": "A", "score": 90}
    b = {"start": 15.0, "end": 40.0, "title": "B", "score": 70}
    out = resolve_overlaps([a, b], WORDS, 15, 60, 40.0, max_overlap=2.0)
    b_out = next(c for c in out if c["title"] == "B")
    assert b_out["start"] >= 19.5
    assert b_out["end"] - b_out["start"] >= 15


def test_rubric_score_spreads_the_scale():
    strong = rubric_score({"hook": 9, "emotion": 8, "standalone": 9, "value": 8})
    weak = rubric_score({"hook": 3, "emotion": 4, "standalone": 5, "value": 3})
    assert strong > 80 and weak < 45
    assert rubric_score({"title": "no ratings"}) is None


def test_refine_drops_ads_and_intros_and_limits_count():
    clips = [
        {"start": 0, "end": 10, "title": "ПРИВІТ", "category": "intro", "hook": 9, "emotion": 9, "standalone": 9, "value": 9},
        {"start": 10, "end": 20, "title": "ПРОМОКОД", "category": "ad", "hook": 9, "emotion": 9, "standalone": 9, "value": 9},
        {"start": 20, "end": 30, "title": "ДОБРИЙ", "category": "content", "hook": 8, "emotion": 7, "standalone": 8, "value": 7},
        {"start": 30, "end": 40, "title": "СЛАБКИЙ", "category": "content", "hook": 3, "emotion": 3, "standalone": 4, "value": 3},
    ]
    words = _words(" ".join([SENT] * 4), step=0.75)  # sentences of 15 s → 0–60 s
    for c in clips:
        c["start"] *= 1.5
        c["end"] *= 1.5
    out = refine_clips(clips, words, 60.0, count=1, min_len=15, max_len=30)
    assert [c["title"] for c in out] == ["ДОБРИЙ"]
    assert out[0]["score"] > 60


def test_refine_without_words_falls_back_to_time_bounds():
    clips = [{"start": 5.0, "end": 12.0, "title": "X", "score": 80}]
    out = refine_clips(clips, [], 100.0, count=5, min_len=15, max_len=60)
    assert len(out) == 1
    assert out[0]["end"] - out[0]["start"] >= 15


def _caps_words(n_sent: int = 8, per: int = 12, step: float = 0.5, pause: float = 0.4) -> list[dict]:
    """Turbo-style transcript: no periods, each sentence just starts with a capital letter after a pause."""
    out, t = [], 0.0
    for k in range(n_sent):
        for j in range(per):
            word = ("Тоді" if j == 0 else "слово") if k else ("Початок" if j == 0 else "слово")
            out.append({"start": t, "end": t + step * 0.8, "word": word})
            t += step
        t += pause
    return out


def test_capitalised_next_word_counts_as_sentence_end():
    words = _caps_words()          # sentences of ~6.4 s, no punctuation at all
    clip = {"start": 0.0, "end": 17.0, "title": "T", "score": 70}
    out = snap_clip(clip, words, 15, 60, 60.0)
    assert out is not None
    assert out["ends_on_sentence"]
    assert out["end"] - out["start"] < 27      # not stretched towards the 60 s maximum


def test_without_any_boundary_clip_is_not_stretched_to_max():
    words = _words("слово " * 200)  # 100 s of speech, no punctuation, no pauses, no capitals
    clip = {"start": 10.0, "end": 30.0, "title": "T", "score": 70}
    out = snap_clip(clip, words, 15, 60, 100.0)
    assert out is not None
    assert out["end"] - out["start"] <= 31.5


def test_clip_does_not_end_on_a_conjunction_or_comma():
    text = "Перше речення тут слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово слово і, далі"
    words = _words(text)
    clip = {"start": 0.0, "end": 12.0, "title": "T", "score": 70}
    out = snap_clip(clip, words, 5, 60, 30.0)
    last = [w for w in words if w["start"] < out["end"]][-1]["word"]
    assert last not in ("і,", "і")

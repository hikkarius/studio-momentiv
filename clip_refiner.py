"""
clip_refiner.py — Deterministic post-processing of AI-proposed clips.

Gemini proposes rough moments; this module turns them into publishable clips:
  • snaps start/end to sentence boundaries using Whisper word timestamps,
  • enforces the configured length range (e.g. 15–60 s),
  • drops intros, outros and ad integrations flagged by the model,
  • removes overlaps between clips (the higher-scoring clip keeps the speech),
  • keeps the best `count` clips and returns them in chronological order.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

log = logging.getLogger("shorts_core")

# Small paddings so a cut never clips the first/last syllable
_START_PAD = 0.15
_END_PAD = 0.35

# How far a boundary may move when snapping to a sentence edge (seconds)
_START_SEARCH_BACK = 6.0
_START_SEARCH_FWD = 3.0
_END_SEARCH_BACK = 5.0
_END_SEARCH_FWD = 6.0

_SENTENCE_END_RE = re.compile(r"[.!?…]+[\"»”')\]]*$")

# Categories that are not worth publishing as a short
NON_CONTENT_CATEGORIES = {"intro", "outro", "ad", "sponsor", "promo", "реклама", "вступ", "прощання"}

# Rubric weights: criterion -> points at the maximum value 10 (sum = 100)
RUBRIC_WEIGHTS: dict[str, float] = {
    "hook": 3.0,
    "emotion": 2.5,
    "standalone": 2.5,
    "value": 2.0,
}


# ─── Words / sentences ───────────────────────────────────────────────────────

def _clean_words(words: Iterable[dict] | None) -> list[dict]:
    out: list[dict] = []
    for w in words or []:
        try:
            s = float(w["start"])
            e = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        out.append({"start": s, "end": max(e, s), "word": text})
    out.sort(key=lambda w: w["start"])
    return out


def _is_sentence_end(word: str) -> bool:
    return bool(_SENTENCE_END_RE.search(word))


# Words a sentence almost never ends on (conjunctions, prepositions, fillers)
_NO_END_WORDS = {
    "і", "й", "та", "а", "але", "або", "чи", "що", "бо", "як", "щоб", "коли", "якщо", "тому",
    "в", "у", "з", "із", "зі", "на", "до", "від", "за", "по", "про", "для", "без", "під", "над",
    "от", "ну", "ось", "це", "той", "та", "the", "a", "and", "or", "to", "of", "in",
}


def _bare(word: str) -> str:
    return re.sub(r"[^\w'’-]+", "", word).lower()


def _starts_upper(word: str) -> bool:
    w = word.lstrip("«\"'(-–— ")
    return bool(w) and w[0].isupper()


def _end_flags(words: list[dict], segment_ends: list[float] | None = None) -> list[bool]:
    """
    flags[i] is True when a sentence plausibly ends after word i. Whisper large-v3-turbo often
    drops the period and simply starts the next sentence with a capital letter, so besides
    punctuation we use segment ends, long pauses and "pause + capital letter" as boundaries.
    """
    n = len(words)
    seg = sorted(segment_ends or [])
    flags = [False] * n
    j = 0
    for i in range(n):
        w = words[i]
        if i == n - 1:
            flags[i] = True
            break
        nxt = words[i + 1]
        gap = nxt["start"] - w["end"]
        text = w["word"]
        bare = _bare(text)
        if text.endswith(",") or text.endswith(";") or text.endswith(":") or bare in _NO_END_WORDS:
            flags[i] = gap > 1.5  # only a really long pause can close a sentence here
            continue
        if _is_sentence_end(text) or gap > 1.0:
            flags[i] = True
            continue
        while j < len(seg) and seg[j] < w["end"] - 0.15:
            j += 1
        at_seg_end = j < len(seg) and seg[j] <= nxt["start"] + 0.15
        if _starts_upper(nxt["word"]) and (at_seg_end or gap >= 0.3):
            flags[i] = True
    return flags


def sentence_ends(words: list[dict], segment_ends: list[float] | None = None) -> list[int]:
    """Indices of words that close a sentence."""
    return [i for i, f in enumerate(_end_flags(words, segment_ends)) if f]


def sentence_starts(words: list[dict], segment_ends: list[float] | None = None) -> list[int]:
    """Indices of words that open a sentence (the first word, and every word after a sentence end)."""
    if not words:
        return []
    flags = _end_flags(words, segment_ends)
    return [0] + [i + 1 for i in range(len(words) - 1) if flags[i]]


# ─── Scoring ─────────────────────────────────────────────────────────────────

def rubric_score(clip: dict) -> int | None:
    """
    Combines per-criterion ratings (1–10) into a 0–100 score.
    Returns None when the clip carries no usable ratings.
    """
    total = 0.0
    seen = 0
    for key, weight in RUBRIC_WEIGHTS.items():
        raw = clip.get(key)
        if raw is None and isinstance(clip.get("scores"), dict):
            raw = clip["scores"].get(key)
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue
        val = max(1.0, min(10.0, val))
        total += val * weight
        seen += 1
    if seen < len(RUBRIC_WEIGHTS) // 2 + 1:
        return None
    # Rescale if some criteria are missing
    max_points = sum(w for k, w in RUBRIC_WEIGHTS.items() if k in clip or k in (clip.get("scores") or {}))
    if max_points <= 0:
        return None
    return int(round(total / max_points * 10))


# ─── Boundaries ──────────────────────────────────────────────────────────────

# How far past the AI's proposed end a clip may grow while looking for a sentence end
_MAX_EXTENSION = 10.0


def snap_clip(
    clip: dict,
    words: list[dict],
    min_len: float,
    max_len: float,
    video_duration: float,
    segment_ends: list[float] | None = None,
) -> dict | None:
    """
    Moves start/end to sentence edges and fits the duration into [min_len, max_len].
    The end never moves more than ~10 s past the proposed end (or past start + min_len for a
    too-short proposal), so a transcript without punctuation cannot stretch clips to max_len.
    Returns None when no valid clip can be built from that region.
    """
    s = float(clip["start"])
    e = float(clip["end"])
    if not words:
        return _fit_without_words(clip, s, e, min_len, max_len, video_duration)

    flags = _end_flags(words, segment_ends)
    starts = [0] + [i + 1 for i in range(len(words) - 1) if flags[i]]

    # 1. Start: closest sentence start in [s - back, s + fwd], else first word at/after s
    cand = [i for i in starts if s - _START_SEARCH_BACK <= words[i]["start"] <= s + _START_SEARCH_FWD]
    if cand:
        si = min(cand, key=lambda i: (abs(words[i]["start"] - s), words[i]["start"] > s))
    else:
        after = [i for i, w in enumerate(words) if w["start"] >= s - 0.5]
        if not after:
            return None
        si = after[0]
    t0 = words[si]["start"]

    # 2. End window: at least min_len, at most max_len, and not far past the proposed end
    lo = max(t0 + min_len, e - _END_SEARCH_BACK)
    hi = min(t0 + max_len, max(e, t0 + min_len) + _MAX_EXTENSION)
    if lo > hi:
        lo = t0 + min_len
    in_window = [i for i in range(si + 1, len(words)) if lo <= words[i]["end"] <= hi]
    if not in_window:
        return _fit_without_words(clip, s, e, min_len, max_len, video_duration)

    target = min(max(e, lo), hi)
    sentence = [i for i in in_window if flags[i]]
    if sentence:
        ei = min(sentence, key=lambda i: abs(words[i]["end"] - target))
    else:
        # No sentence edge nearby: end at the clearest pause near the proposed end,
        # avoiding conjunctions/prepositions and words followed by a comma
        def pause(i: int) -> float:
            nxt = words[i + 1]["start"] if i + 1 < len(words) else words[i]["end"] + 1.0
            bad = words[i]["word"].endswith(",") or _bare(words[i]["word"]) in _NO_END_WORDS
            return (nxt - words[i]["end"]) - (1.0 if bad else 0.0) - 0.02 * abs(words[i]["end"] - target)
        ei = max(in_window, key=pause)

    start = max(0.0, t0 - _START_PAD)
    if si > 0:
        start = max(start, words[si - 1]["end"])
    end = words[ei]["end"] + _END_PAD
    if ei + 1 < len(words):
        end = min(end, words[ei + 1]["start"])
    end = min(end, video_duration)
    if end - start < min_len - 0.5 or end - start > max_len + 1.0:
        return None

    out = dict(clip)
    out["start"] = round(start, 2)
    out["end"] = round(end, 2)
    out["ends_on_sentence"] = bool(flags[ei])
    if not isinstance(clip.get("hook_text"), str) or not clip.get("hook_text"):
        out["hook_text"] = " ".join(w["word"] for w in words[si:si + 14])
    return out


def _fit_without_words(clip: dict, s: float, e: float, min_len: float, max_len: float, dur: float) -> dict | None:
    s = max(0.0, s)
    e = min(e, dur)
    if e - s > max_len:
        e = s + max_len
    if e - s < min_len:
        e = min(dur, s + min_len)
        if e - s < min_len:
            s = max(0.0, e - min_len)
    if e - s < min_len - 0.5:
        return None
    out = dict(clip)
    out["start"], out["end"] = round(s, 2), round(e, 2)
    out["ends_on_sentence"] = False
    return out


# ─── Overlaps ────────────────────────────────────────────────────────────────

def _overlap(a: dict, b: dict) -> float:
    return max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def resolve_overlaps(
    clips: list[dict],
    words: list[dict],
    min_len: float,
    max_len: float,
    video_duration: float,
    max_overlap: float,
    segment_ends: list[float] | None = None,
) -> list[dict]:
    """
    Accepts clips from the highest score down. A clip that overlaps an accepted one by more than
    `max_overlap` seconds is trimmed to the free part (re-snapped to sentences); if the free part is
    too short, it is dropped.
    """
    accepted: list[dict] = []
    for clip in sorted(clips, key=lambda c: c.get("score", 0), reverse=True):
        cur: dict | None = clip
        for _ in range(3):
            if cur is None:
                break
            clash = next((a for a in accepted if _overlap(cur, a) > max_overlap), None)
            if clash is None:
                break
            # Keep the larger free side of the clip
            left = clash["start"] - cur["start"]
            right = cur["end"] - clash["end"]
            if max(left, right) < min_len:
                cur = None
                break
            trimmed = dict(cur)
            if right >= left:
                trimmed["start"] = clash["end"]
            else:
                trimmed["end"] = clash["start"]
            cur = snap_clip(trimmed, words, min_len, max_len, video_duration, segment_ends)
        else:
            cur = None
        if cur is not None and all(_overlap(cur, a) <= max_overlap for a in accepted):
            accepted.append(cur)
        else:
            log.info(f"Dropped overlapping clip {clip.get('title')!r} ({clip['start']:.1f}–{clip['end']:.1f}s)")
    return accepted


# ─── Pipeline ────────────────────────────────────────────────────────────────

def refine_clips(
    clips: list[dict],
    words: list[dict] | None,
    video_duration: float,
    count: int,
    min_len: float,
    max_len: float,
    max_overlap: float = 2.0,
    segments: list[dict] | None = None,
) -> list[dict]:
    """Full post-processing pipeline. Returns at most `count` clips, sorted by start time."""
    w = _clean_words(words)
    seg_ends = [float(sg["end"]) for sg in (segments or []) if isinstance(sg, dict) and "end" in sg]
    prepared: list[dict] = []
    dropped_category = 0
    for c in clips:
        category = str(c.get("category") or "content").strip().lower()
        if category in NON_CONTENT_CATEGORIES:
            dropped_category += 1
            continue
        score = rubric_score(c)
        if score is not None:
            c = {**c, "score": score}
        snapped = snap_clip(c, w, min_len, max_len, video_duration, seg_ends)
        if snapped is not None:
            prepared.append(snapped)

    result = resolve_overlaps(prepared, w, min_len, max_len, video_duration, max_overlap, seg_ends)
    result.sort(key=lambda c: c.get("score", 0), reverse=True)
    result = result[:count]
    result.sort(key=lambda c: c["start"])
    log.info(
        f"Clip refinement: {len(clips)} proposed → {dropped_category} intro/outro/ads dropped → "
        f"{len(prepared)} snapped → {len(result)} kept (target {count}, {min_len:.0f}–{max_len:.0f}s, "
        f"max overlap {max_overlap:.0f}s)"
    )
    return result


def trim_silence(
    words: Iterable[dict] | None,
    start: float,
    end: float,
    lead: float = _START_PAD,
    tail: float = _END_PAD,
    min_len: float = 5.0,
    min_gain: float = 0.1,
) -> tuple[float, float] | None:
    """
    Tightens clip bounds to the speech: from just before the first word to just after the last one.
    Only shrinks the clip (never extends it), keeps at least min_len seconds.
    Returns (new_start, new_end), or None when there is nothing worth trimming or no words.
    """
    inside = [w for w in _clean_words(words) if w["end"] > start and w["start"] < end]
    if not inside:
        return None
    new_start = max(start, inside[0]["start"] - lead)
    new_end = min(end, inside[-1]["end"] + tail)
    if new_end - new_start < min_len:
        return None
    new_start, new_end = round(new_start, 2), round(new_end, 2)
    if (new_start - start) + (end - new_end) < min_gain:
        return None
    return new_start, new_end

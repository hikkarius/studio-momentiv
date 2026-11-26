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
from typing import Iterable

log = logging.getLogger("shorts_core")

# Small paddings so a cut never clips the first/last syllable
_START_PAD = 0.15
_END_PAD = 0.35

# How far a boundary may move when snapping to a sentence edge (seconds)
_START_SEARCH_BACK = 6.0
_START_SEARCH_FWD = 3.0
_END_SEARCH_BACK = 5.0
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

    # 1. Start: closest sentence start in [s - back, s + fwd], else first word at/after s.
    # A start already aligned to the hook phrase stays where it is.
    cand = [] if clip.get("hook_aligned") else [
        i for i in starts if s - _START_SEARCH_BACK <= words[i]["start"] <= s + _START_SEARCH_FWD
    ]
    if cand:
        si = min(cand, key=lambda i: (abs(words[i]["start"] - s), words[i]["start"] > s))
    else:
        slack = 0.05 if clip.get("hook_aligned") else 0.5
        after = [i for i, w in enumerate(words) if w["start"] >= s - slack]
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
    out.pop("hook_aligned", None)
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


# ─── Hook (experimental search) ──────────────────────────────────────────────

# Words that only warm up the speech: a clip should not open on them
LEAD_FILLERS = {
    "ну", "отже", "от", "ось", "значить", "коротше", "загалом", "слухайте", "слухай", "ем", "еее", "ее", "е", "м", "мм",
    "а", "і", "й", "та", "но", "ну-ну", "типу", "well", "so", "um", "uh", "uhm", "like", "okay", "ok", "and",
}
# How far from the AI's start the hook phrase may be found (seconds back / forward)
_HOOK_SEARCH_BACK = 4.0
_HOOK_SEARCH_FWD = 15.0


def align_start_to_hook(clip: dict, words: list[dict]) -> dict:
    """
    Gemini sees only segment timestamps, so its start is coarse. When the clip's `hook_text` (its first
    phrase) is found in the word timestamps near the proposed start, the clip starts exactly on it.
    The end is not moved here; snap_clip fits the length afterwards.
    """
    hook = [_bare(t) for t in str(clip.get("hook_text") or "").split()]
    hook = [h for h in hook if h][:4]
    if len(hook) < 2 or not words:
        return clip
    s = float(clip["start"])
    bare = [_bare(w["word"]) for w in words]
    best = None
    for i, w in enumerate(words):
        if w["start"] < s - _HOOK_SEARCH_BACK:
            continue
        if w["start"] > s + _HOOK_SEARCH_FWD:
            break
        if bare[i:i + len(hook)] == hook:
            if best is None or abs(w["start"] - s) < abs(words[best]["start"] - s):
                best = i
    if best is None:
        return clip
    best = _sentence_start_before(words, best, _end_flags(words))
    return {**clip, "start": words[best]["start"], "hook_aligned": True}


# A hook found mid-sentence («…, тімфайт в топі…») moves back to its sentence start, at most this far
_SENTENCE_BACK = 8.0


def _sentence_start_before(words: list[dict], i: int, flags: list[bool], back: float = _SENTENCE_BACK) -> int:
    """Index of the first word of the sentence holding words[i], if it is at most `back` seconds earlier;
    otherwise i (a long run-on sentence keeps the hook)."""
    j = i
    while j > 0 and not flags[j - 1]:
        j -= 1
        if words[i]["start"] - words[j]["start"] > back:
            return i
    return j


def start_on_sentence(clip: dict, words: list[dict], min_len: float, max_len: float,
                      flags: list[bool] | None = None, fwd: float = 6.0) -> dict:
    """Experimental search: a clip that still opens mid-sentence (after overlaps were trimmed) starts at the
    previous sentence start when the clip stays within max_len, else at the next one when it stays ≥ min_len."""
    if not words:
        return clip
    flags = flags if flags is not None else _end_flags(words)
    s, e = float(clip["start"]), float(clip["end"])
    i = next((k for k, w in enumerate(words) if w["start"] >= s - 0.05), None)
    if i is None or i == 0 or flags[i - 1] or words[i]["start"] >= e:
        return clip
    j = _sentence_start_before(words, i, flags)
    if j != i and e - words[j]["start"] + _START_PAD <= max_len:
        new_start = max(words[j]["start"] - _START_PAD, words[j - 1]["end"] if j > 0 else 0.0)
    else:
        k = next((k for k in range(i, len(words) - 1) if flags[k]), None)
        if k is None or words[k + 1]["start"] - s > fwd or e - words[k + 1]["start"] < min_len - 0.5:
            return clip
        new_start = max(words[k + 1]["start"] - _START_PAD, words[k]["end"])
        j = k + 1
    out = {**clip, "start": round(new_start, 2)}
    if clip.get("hook_text"):
        out["hook_text"] = " ".join(w["word"] for w in words[j:j + 14])
    return out


def trim_lead_fillers(clip: dict, words: list[dict], min_len: float, max_words: int = 3) -> dict:
    """Moves the start past filler words («ну», «отже», «а»…) while the clip stays at least min_len long."""
    s, e = float(clip["start"]), float(clip["end"])
    first = next((i for i, w in enumerate(words) if w["start"] >= s - 0.05), None)
    if first is None:
        return clip
    i = first
    while i < len(words) and i - first < max_words and _bare(words[i]["word"]) in LEAD_FILLERS:
        i += 1
    if i == first or i >= len(words) or words[i]["start"] >= e:
        return clip
    new_start = max(s, words[i]["start"] - _START_PAD, words[i - 1]["end"])
    if e - new_start < min_len - 0.5:
        return clip
    nxt = str(clip.get("hook_text") or "")
    out = {**clip, "start": round(new_start, 2)}
    if nxt:
        out["hook_text"] = " ".join(w["word"] for w in words[i:i + 14])
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
    hook_first: bool = False,
) -> list[dict]:
    """
    Full post-processing pipeline. Returns at most `count` clips, sorted by start time.
    hook_first (experimental search): start exactly on the clip's hook phrase and skip lead fillers.
    """
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
        if hook_first:
            c = align_start_to_hook(c, w)
        snapped = snap_clip(c, w, min_len, max_len, video_duration, seg_ends)
        if snapped is not None and hook_first and w:
            snapped = trim_lead_fillers(snapped, w, min_len)
        if snapped is not None:
            prepared.append(snapped)

    result = resolve_overlaps(prepared, w, min_len, max_len, video_duration, max_overlap, seg_ends)
    if hook_first and w:
        flags = _end_flags(w, seg_ends)
        result = [start_on_sentence(c, w, min_len, max_len, flags) for c in result]
    result.sort(key=lambda c: c.get("score", 0), reverse=True)
    result = result[:count]
    result.sort(key=lambda c: c["start"])
    log.info(
        f"Clip refinement: {len(clips)} proposed → {dropped_category} intro/outro/ads dropped → "
        f"{len(prepared)} snapped → {len(result)} kept (target {count}, {min_len:.0f}–{max_len:.0f}s, "
        f"max overlap {max_overlap:.0f}s)"
    )
    return result


"""
pauses.py — «Прибрати паузи»: find long silences inside a clip and cut them out (jump cut).

How a pause is found:
  1. the clip audio is read as loudness levels (50 per second) with ffmpeg;
  2. quiet runs longer than min_gap are pauses (the threshold adapts to each clip's own noise floor),
     because Whisper often stretches a word's end over the silence after it, so word gaps alone miss them;
  3. a run that contains the start of a word is skipped: someone is (quietly) speaking there;
  4. without audio levels, gaps between words are used instead.
Each pause keeps `keep` seconds of silence so the speech does not glue together.

Cuts are stored on the clip as absolute seconds [[a, b], ...]. All functions here are pure except
audio_levels() and render_with_cuts(), which call ffmpeg.
"""
from __future__ import annotations

import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("shorts_core")

LEVEL_RATE = 50          # loudness values per second
DEFAULT_MIN_GAP = 0.6    # pauses longer than this are cut
KEEP = 0.2               # silence left in place of a cut pause
# «Темп» in the editor: (min_gap, keep) — calm keeps more air, dense cuts tighter
TEMPOS = {"calm": (1.0, 0.3), "normal": (0.6, 0.2), "dense": (0.4, 0.1)}
EDGE = 0.25              # never cut this close to the clip bounds
MIN_CUT = 0.15           # a cut shorter than this is not worth a jump
SMOOTH = 0.16            # crossfade at each cut (s); it overlaps into the removed pause, timing stays

Cut = Tuple[float, float]


# ─── Audio levels ─────────────────────────────────────────────────────────────

def audio_levels(source: str, start: float, end: float, rate: int = LEVEL_RATE, timeout: int = 60) -> List[float]:
    """Loudness (dBFS) of the audio between start and end, `rate` values per second. [] when there is no audio."""
    import numpy as np

    sr = 8000
    dur = max(0.1, end - start)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-i", os.path.abspath(source), "-t", f"{dur:.3f}",
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"Audio levels failed: {e}")
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    pcm = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    hop = sr // rate
    # ffmpeg's resampler can return a few samples less than asked; pad that tail so the wave
    # keeps exactly `rate` values per second and stays aligned with the time ruler.
    want = int(round(dur * rate)) * hop
    if 0 < want - len(pcm) < 2 * hop:
        pcm = np.pad(pcm, (0, want - len(pcm)), mode="edge")
    n = len(pcm) // hop
    if n == 0:
        return []
    frames = pcm[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1)) + 1e-6
    return [round(float(v), 1) for v in 20 * np.log10(rms)]


def silence_threshold(levels: Sequence[float]) -> float:
    """dB below which a frame counts as silence: a bit above this clip's own noise floor."""
    import numpy as np

    arr = np.asarray(levels, dtype=float)
    floor = float(np.percentile(arr, 10))
    loud = float(np.percentile(arr, 90))
    return floor + max(6.0, 0.35 * (loud - floor))


def has_dynamics(levels: Sequence[float], min_db: float = 10.0) -> bool:
    """True when loud and quiet parts differ enough for the audio to show pauses."""
    import numpy as np

    arr = np.asarray(levels, dtype=float)
    return float(np.percentile(arr, 90) - np.percentile(arr, 10)) >= min_db


# ─── Finding pauses ───────────────────────────────────────────────────────────

def _word_starts(words: Optional[Sequence[Dict[str, Any]]], lo: float, hi: float) -> List[float]:
    out = []
    for w in words or []:
        try:
            s = float(w["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if lo <= s <= hi and str(w.get("word", "")).strip():
            out.append(s)
    return sorted(out)


def _trim_run(a: float, b: float, keep: float) -> Optional[Cut]:
    a2, b2 = round(a + keep / 2, 2), round(b - keep / 2, 2)
    return (a2, b2) if b2 - a2 >= MIN_CUT else None


def find_pauses(
    start: float,
    end: float,
    levels: Optional[Sequence[float]] = None,
    words: Optional[Sequence[Dict[str, Any]]] = None,
    min_gap: float = DEFAULT_MIN_GAP,
    keep: float = KEEP,
    rate: int = LEVEL_RATE,
) -> List[Cut]:
    """
    Pauses inside [start, end] longer than min_gap, as absolute cuts (each leaves `keep` s of silence).
    `levels` are the clip's loudness values from `start`; without them, gaps between words are used.
    """
    lo, hi = start + EDGE, end - EDGE
    starts = _word_starts(words, start, end)
    cuts: List[Cut] = []
    if levels and not has_dynamics(levels):
        levels = None  # flat sound (music bed, constant noise): the audio can't tell speech from pause
    if levels:
        thr = silence_threshold(levels)
        quiet = [v < thr for v in levels]
        i = 0
        while i < len(quiet):
            if not quiet[i]:
                i += 1
                continue
            j = i
            while j < len(quiet) and quiet[j]:
                j += 1
            a, b = start + i / rate, start + j / rate
            a, b = max(a, lo), min(b, hi)
            if b - a >= min_gap:
                cut = _trim_run(a, b, keep)
                # a word starting inside means quiet speech, not a pause
                if cut and not any(cut[0] + 0.05 < s < cut[1] - 0.05 for s in starts):
                    cuts.append(cut)
            i = j
    else:
        ws = sorted(
            ((float(w["start"]), float(w["end"])) for w in (words or []) if "start" in w and "end" in w),
            key=lambda x: x[0],
        )
        ws = [w for w in ws if w[1] > start and w[0] < end]
        for (s0, e0), (s1, _e1) in zip(ws, ws[1:]):
            a, b = max(e0, lo), min(s1, hi)
            if b - a >= min_gap:
                cut = _trim_run(a, b, keep)
                if cut:
                    cuts.append(cut)
    return cuts


def normalize_cuts(raw: Any, start: float, end: float) -> List[Cut]:
    """Cleans cuts from the UI/DB: inside the clip, sorted, merged, not too short."""
    out: List[Cut] = []
    for c in raw or []:
        try:
            a, b = float(c[0]), float(c[1])
        except (TypeError, ValueError, IndexError):
            continue
        a, b = max(a, start + 0.05), min(b, end - 0.05)
        if b - a >= MIN_CUT:
            out.append((round(a, 2), round(b, 2)))
    out.sort()
    merged: List[Cut] = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


# ─── Timeline after cutting ───────────────────────────────────────────────────

def removed(cuts: Sequence[Cut]) -> float:
    return round(sum(b - a for a, b in cuts), 2)


def kept_segments(start: float, end: float, cuts: Sequence[Cut]) -> List[Cut]:
    """Parts of [start, end] that stay, as absolute seconds."""
    segs, t = [], start
    for a, b in cuts:
        if a > t:
            segs.append((t, a))
        t = max(t, b)
    if end > t:
        segs.append((t, end))
    return segs


def map_time(t_rel: float, cuts_rel: Sequence[Cut]) -> Optional[float]:
    """Clip-relative time → time in the cut clip; None when t falls inside a cut."""
    shift = 0.0
    for a, b in cuts_rel:
        if t_rel >= b:
            shift += b - a
        elif t_rel > a:
            return None
        else:
            break
    return t_rel - shift


def map_time_clamped(t_rel: float, cuts_rel: Sequence[Cut]) -> float:
    """Like map_time, but a time inside a cut lands on the cut point."""
    shift = 0.0
    for a, b in cuts_rel:
        if t_rel >= b:
            shift += b - a
        elif t_rel > a:
            return a - shift
        else:
            break
    return t_rel - shift


def relative(cuts: Sequence[Cut], start: float) -> List[Cut]:
    return [(round(a - start, 3), round(b - start, 3)) for a, b in cuts]


def remap_words(words: Sequence[Dict[str, Any]], cuts_rel: Sequence[Cut]) -> List[Dict[str, Any]]:
    """
    Clip-relative words → the cut timeline. A word that lies inside a cut is dropped.
    A word that starts inside a cut but ends after it is kept and starts at the join: speech recognition
    sometimes stretches a word over the silence before it, and that silence is what the cut removed.
    """
    out = []
    for w in words:
        s = map_time(float(w["start"]), cuts_rel)
        if s is None:
            if map_time(float(w["end"]), cuts_rel) is None:
                continue
            s = map_time_clamped(float(w["start"]), cuts_rel)
        e = map_time_clamped(float(w["end"]), cuts_rel)
        out.append({**w, "start": round(s, 3), "end": round(max(s + 0.05, e), 3)})
    return out


def cuts_key(cuts: Sequence[Cut]) -> str:
    """Short stable tag of a cut list for cache file names ('' when there are none)."""
    if not cuts:
        return ""
    import hashlib

    # SMOOTH and the word-remap version are part of the key, so older cut renders are made again
    # (v2: words stretched over a cut pause are no longer missing from the subtitles)
    return hashlib.sha1(repr(([(round(a, 2), round(b, 2)) for a, b in cuts], SMOOTH, "words-v2")).encode()).hexdigest()[:8]


# ─── Rendering ────────────────────────────────────────────────────────────────

def has_audio(source: str, timeout: int = 20) -> bool:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                               "-of", "csv=p=0", os.path.abspath(source)], capture_output=True, text=True, timeout=timeout)
        return bool(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


def concat_filter(segments_rel: Sequence[Cut], audio: bool, vin: str = "0:v", ain: str = "0:a",
                  smooth: float = SMOOTH) -> str:
    """
    filter_complex part that keeps only segments_rel of the input and joins them into [cv] (+ [ca]).
    smooth > 0: each join is a short crossfade (picture and sound) instead of a hard jump. Both sides
    borrow half of it from the removed pause, so the speech lands at exactly the same moments as with
    a hard cut and the subtitles stay in sync. smooth = 0: plain concat.
    """
    n = len(segments_rel)
    # half-overlap at each join, never more than half of the removed gap
    ext = [0.0] * max(0, n - 1)
    if smooth > 0:
        for k in range(n - 1):
            gap = segments_rel[k + 1][0] - segments_rel[k][1]
            ext[k] = round(max(0.0, min(smooth / 2, gap / 2 - 0.01)), 3)
    if n < 2 or not any(e >= 0.02 for e in ext):
        parts, ins = [], []
        for k, (a, b) in enumerate(segments_rel):
            parts.append(f"[{vin}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{k}]")
            ins.append(f"[v{k}]")
            if audio:
                parts.append(f"[{ain}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{k}]")
                ins.append(f"[a{k}]")
        parts.append("".join(ins) + f"concat=n={n}:v=1:a={1 if audio else 0}[cv]" + ("[ca]" if audio else ""))
        return ";".join(parts)

    parts, lens = [], []
    for k, (a, b) in enumerate(segments_rel):
        a2 = a - (ext[k - 1] if k > 0 else 0.0)
        b2 = b + (ext[k] if k < n - 1 else 0.0)
        lens.append(b2 - a2)
        parts.append(f"[{vin}]trim=start={a2:.3f}:end={b2:.3f},setpts=PTS-STARTPTS,settb=AVTB[v{k}]")
        if audio:
            parts.append(f"[{ain}]atrim=start={a2:.3f}:end={b2:.3f},asetpts=PTS-STARTPTS[a{k}]")
    vprev, aprev, total = "v0", "a0", lens[0]
    for k in range(1, n):
        d = 2 * ext[k - 1]
        vout = "cv" if k == n - 1 else f"vx{k}"
        aout = "ca" if k == n - 1 else f"ax{k}"
        if d >= 0.04:
            parts.append(f"[{vprev}][v{k}]xfade=transition=fade:duration={d:.3f}:offset={total - d:.3f}[{vout}]")
            if audio:
                parts.append(f"[{aprev}][a{k}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{aout}]")
        else:
            parts.append(f"[{vprev}][v{k}]concat=n=2:v=1:a=0[{vout}]")
            if audio:
                parts.append(f"[{aprev}][a{k}]concat=n=2:v=0:a=1[{aout}]")
            d = 0.0
        total += lens[k] - d
        vprev, aprev = vout, aout
    return ";".join(parts)


def render_with_cuts(source: str, start: float, end: float, cuts: Sequence[Cut], output_path: str,
                     has_nvenc: bool = False, timeout: int = 600) -> str:
    """The plain 16:9 clip with the pauses taken out (re-encoded, high quality)."""
    segs = relative(kept_segments(start, end, cuts), start)
    audio = has_audio(source)
    filters = [concat_filter(segs, audio), concat_filter(segs, audio, smooth=0)]
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"tmp_{uuid.uuid4().hex[:6]}_{out.name}")
    encoders = [["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]]
    if has_nvenc:
        encoders.insert(0, ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "12M", "-pix_fmt", "yuv420p"])
    last = ""
    try:
        for fc, enc in ((f, e) for f in dict.fromkeys(filters) for e in encoders):
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", os.path.abspath(source), "-t", f"{end - start:.3f}",
                   "-filter_complex", fc, "-map", "[cv]", *(["-map", "[ca]", "-c:a", "aac", "-b:a", "192k"] if audio else []),
                   *enc, "-movflags", "+faststart", str(tmp.resolve())]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1000:
                os.replace(tmp, out)
                return str(out)
            last = (proc.stderr or "")[-400:]
            log.warning(f"Cut render with {enc[1]} failed: {last}")
        raise RuntimeError("Не вдалося зібрати кліп без пауз: " + last.strip()[-300:])
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

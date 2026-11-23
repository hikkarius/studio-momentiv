"""
pauses.py — «Прибрати паузи»: find long silences inside a clip and cut them out (jump cut).

How a pause is found:
  1. the clip audio is read as loudness levels (50 per second) with ffmpeg;
  2. quiet runs longer than min_gap are pauses (the threshold adapts to each clip's own noise floor),
     because Whisper often stretches a word's end over the silence after it, so word gaps alone miss them;
  3. a run that contains the start of a word is skipped: someone is (quietly) speaking there;
  4. without audio levels, gaps between words are used instead.
Each pause keeps `keep` seconds of silence so the speech does not glue together.

Cuts are stored on the clip as absolute seconds [[a, b], ...]. How a cut looks («Перехід»: різкий, м'який,
наїзд) is one studio setting, studio.pause_transition. All functions here are pure except
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
MIN_GAP_CHOICES = (0.4, 0.6, 1.0)
KEEP = 0.2               # silence left in place of a cut pause
EDGE = 0.25              # never cut this close to the clip bounds
MIN_CUT = 0.15           # a cut shorter than this is not worth a jump

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
    """Clip-relative words → the cut timeline. A word that starts inside a cut is dropped."""
    out = []
    for w in words:
        s = map_time(float(w["start"]), cuts_rel)
        if s is None:
            continue
        e = map_time_clamped(float(w["end"]), cuts_rel)
        out.append({**w, "start": round(s, 3), "end": round(max(s + 0.05, e), 3)})
    return out


def cuts_key(cuts: Sequence[Cut]) -> str:
    """Short stable tag of a cut list for cache file names ('' when there are none)."""
    if not cuts:
        return ""
    import hashlib

    return hashlib.sha1(repr([(round(a, 2), round(b, 2)) for a, b in cuts]).encode()).hexdigest()[:8]


# ─── Rendering ────────────────────────────────────────────────────────────────

def has_audio(source: str, timeout: int = 20) -> bool:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                               "-of", "csv=p=0", os.path.abspath(source)], capture_output=True, text=True, timeout=timeout)
        return bool(proc.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


TRANSITIONS = ("hard", "soft", "zoom")
DEFAULT_TRANSITION = "soft"
XFADE = 0.12      # «М'який»: picture and sound blend into each other over this long at every cut
ZOOM = 1.08       # «Наїзд»: every other piece is shown a bit closer, so the jump looks like a camera change
AFADE = 0.015     # «Різкий»/«Наїзд»: tiny sound fades so a cut never clicks


def video_size(source: str, timeout: int = 20) -> Optional[Tuple[int, int]]:
    try:
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                               "-of", "csv=p=0:s=x", os.path.abspath(source)], capture_output=True, text=True, timeout=timeout)
        w, h = proc.stdout.strip().splitlines()[0].split("x")[:2]
        return int(w), int(h)
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def concat_filter(segments_rel: Sequence[Cut], audio: bool, vin: str = "0:v", ain: str = "0:a",
                  transition: str = "hard", size: Optional[Tuple[int, int]] = None) -> str:
    """
    filter_complex part that keeps only segments_rel of the input and joins them into [cv] (+ [ca]).
    transition: "hard" = plain jump cut; "soft" = a short cross-fade of picture and sound at every cut;
    "zoom" = every other piece slightly closer (needs `size`, the input frame size). The output length and the
    timing of every kept moment are the same in all three, so subtitles and camera moves stay in place.
    """
    segs = list(segments_rel)
    n = len(segs)
    if transition == "zoom" and not size:
        transition = "hard"
    if transition == "soft" and n > 1:
        d = min(XFADE, min(b - a for a, b in segs) / 2)
        d = max(0.0, min(d, min(segs[k + 1][0] - segs[k][1] for k in range(n - 1))))
        if d < 0.04:
            transition = "hard"
    parts: List[str] = []
    if transition == "soft" and n > 1:
        # each piece borrows d/2 of the cut pause on both sides; the cross-fades eat exactly that back
        ext = [(a - (d / 2 if k > 0 else 0.0), b + (d / 2 if k < n - 1 else 0.0)) for k, (a, b) in enumerate(segs)]
        for k, (a, b) in enumerate(ext):
            parts.append(f"[{vin}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{k}]")
            if audio:
                parts.append(f"[{ain}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{k}]")
        vprev, aprev, t = "v0", "a0", 0.0
        for k in range(1, n):
            t += ext[k - 1][1] - ext[k - 1][0] - d
            vo = "cv" if k == n - 1 else f"x{k}"
            parts.append(f"[{vprev}][v{k}]xfade=transition=fade:duration={d:.3f}:offset={t:.3f}[{vo}]")
            vprev = vo
            if audio:
                ao = "ca" if k == n - 1 else f"y{k}"
                parts.append(f"[{aprev}][a{k}]acrossfade=d={d:.3f}:c1=tri:c2=tri[{ao}]")
                aprev = ao
        return ";".join(parts)
    ins = []
    for k, (a, b) in enumerate(segs):
        v = f"[{vin}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS"
        if transition == "zoom" and k % 2 == 1:
            w, h = size
            v += f",crop=trunc(iw/{ZOOM}/2)*2:trunc(ih/{ZOOM}/2)*2,scale={w}:{h},setsar=1"
        elif transition == "zoom":
            v += ",setsar=1"
        parts.append(v + f"[v{k}]")
        ins.append(f"[v{k}]")
        if audio:
            af = f"[{ain}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS"
            if n > 1:
                L = b - a
                af += f",afade=t=in:d={AFADE}" if k > 0 else ""
                af += f",afade=t=out:st={max(0.0, L - AFADE):.3f}:d={AFADE}" if k < n - 1 else ""
            parts.append(af + f"[a{k}]")
            ins.append(f"[a{k}]")
    parts.append("".join(ins) + f"concat=n={n}:v=1:a={1 if audio else 0}[cv]" + ("[ca]" if audio else ""))
    return ";".join(parts)


def render_with_cuts(source: str, start: float, end: float, cuts: Sequence[Cut], output_path: str,
                     has_nvenc: bool = False, timeout: int = 600, transition: str = "hard") -> str:
    """The plain 16:9 clip with the pauses taken out (re-encoded, high quality)."""
    segs = relative(kept_segments(start, end, cuts), start)
    audio = has_audio(source)
    fc = concat_filter(segs, audio, transition=transition, size=video_size(source) if transition == "zoom" else None)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"tmp_{uuid.uuid4().hex[:6]}_{out.name}")
    encoders = [["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]]
    if has_nvenc:
        encoders.insert(0, ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19", "-b:v", "12M", "-pix_fmt", "yuv420p"])
    last = ""
    try:
        for enc in encoders:
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

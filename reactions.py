"""
reactions.py — «Моменти за реакцією» for stream recordings: where the streamer shouts or laughs
(loud audio) and where the chat explodes. These are only HINTS for Gemini: the transcript lines at
such moments get a short mark, the AI still chooses the clips (and never invents them).

  loudness_peaks(source)  — ffmpeg loudness per second → moments much louder than the minute around them;
  fetch_chat(url, dest)   — best effort: the chat replay of a Twitch VOD / YouTube stream (yt-dlp) → message times;
  chat_spikes(times)      — 10-second windows with many more messages than usual;
  mark_segments(...)      — copies of the transcript segments with «[реакція: ...]» marks for the prompt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("shorts_core")

MARK = "[реакція:"
PEAK_DB = 9.0          # louder than the surrounding minute by this much
PEAK_GAP = 20.0        # peaks closer than this are one moment
MAX_PEAKS = 40
CHAT_WIN = 10.0
CHAT_RATIO = 3.0       # a window with 3× the usual messages
MAX_SPIKES = 40

_RMS_RE = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?[0-9.]+|-inf)")


def loudness_per_second(source: str, timeout: int = 900) -> List[float]:
    """RMS level (dB) of every second of the audio. Mono 8 kHz is plenty and keeps a 3-hour stream fast."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", source, "-vn", "-ac", "1", "-ar", "8000",
           "-af", "aresample=8000,asetnsamples=n=8000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
           "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    out = []
    for m in _RMS_RE.finditer(proc.stderr or ""):
        v = m.group(1)
        out.append(-120.0 if v == "-inf" else max(-120.0, float(v)))
    return out


def peaks(levels: Sequence[float], db: float = PEAK_DB, gap: float = PEAK_GAP, limit: int = MAX_PEAKS) -> List[Tuple[float, float]]:
    """Pure. Seconds where the level jumps `db` above the median of the minute around it → [(t, dB above)]."""
    n = len(levels)
    if n < 10:
        return []
    found: List[Tuple[float, float]] = []
    for i, v in enumerate(levels):
        if v <= -60:
            continue
        around = sorted(levels[max(0, i - 30): i + 31])
        base = around[len(around) // 2]
        if v - base >= db:
            found.append((float(i), round(v - base, 1)))
    found.sort(key=lambda p: -p[1])
    out: List[Tuple[float, float]] = []
    for t, d in found:
        if all(abs(t - o[0]) >= gap for o in out):
            out.append((t, d))
        if len(out) >= limit:
            break
    return sorted(out)


def loudness_peaks(source: str) -> List[Tuple[float, float]]:
    try:
        return peaks(loudness_per_second(source))
    except Exception as e:  # a hint only: never stop the analysis
        log.warning(f"Loudness hints skipped: {e}")
        return []


# ─── Chat ─────────────────────────────────────────────────────────────────────

def chat_times_from_file(path: str) -> List[float]:
    """Message times (s) from a yt-dlp chat file: YouTube live_chat (JSON lines) or Twitch rechat (JSON)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    times: List[float] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            if "videoOffsetTimeMsec" in o:  # YouTube: one line = one or more messages at this offset
                try:
                    times.append(float(o["videoOffsetTimeMsec"]) / 1000.0)
                    return
                except (TypeError, ValueError):
                    pass
            for k in ("content_offset_seconds", "contentOffsetSeconds"):
                if k in o:
                    try:
                        times.append(float(o[k]))
                        return
                    except (TypeError, ValueError):
                        pass
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    try:
        walk(json.loads(text))
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    walk(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return sorted(t for t in times if t >= 0)


def chat_spikes(times: Sequence[float], win: float = CHAT_WIN, ratio: float = CHAT_RATIO,
                limit: int = MAX_SPIKES) -> List[Tuple[float, int]]:
    """Pure. 10-second windows with `ratio`× the usual (median of non-empty windows) messages → [(start, count)]."""
    if len(times) < 30:
        return []
    counts: Dict[int, int] = {}
    for t in times:
        counts[int(t // win)] = counts.get(int(t // win), 0) + 1
    usual = sorted(counts.values())[len(counts) // 2]
    found = [(k * win, c) for k, c in counts.items() if c >= max(8, ratio * usual)]
    found.sort(key=lambda p: -p[1])
    out: List[Tuple[float, int]] = []
    for t, c in found:
        if all(abs(t - o[0]) >= PEAK_GAP for o in out):
            out.append((t, c))
        if len(out) >= limit:
            break
    return sorted(out)


def fetch_chat(url: str, dest: str, timeout: int = 300) -> int:
    """Best effort: downloads the chat replay of a Twitch VOD / YouTube stream and keeps only the message
    times in `dest` (JSON list). Returns how many messages; 0 when the video has no chat replay."""
    host = (re.sub(r"^https?://", "", url).split("/")[0]).lower()
    if not any(h in host for h in ("twitch.tv", "youtube.com", "youtu.be")):
        return 0
    with tempfile.TemporaryDirectory(prefix="chat_") as tmp:
        cmd = [sys.executable, "-m", "yt_dlp", "--skip-download", "--write-subs", "--sub-langs", "live_chat,rechat",
               "--no-playlist", "-o", os.path.join(tmp, "chat.%(ext)s"), "--", url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
        except Exception as e:
            log.warning(f"Chat replay download skipped: {e}")
        times: List[float] = []
        for f in Path(tmp).glob("chat*.json"):
            try:
                times += chat_times_from_file(str(f))
            except Exception as e:
                log.warning(f"Chat replay unreadable ({f.name}): {e}")
    if times:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_text(json.dumps([round(t, 1) for t in sorted(times)]), encoding="utf-8")
    return len(times)


def load_chat(path: str) -> List[float]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return [float(t) for t in data] if isinstance(data, list) else []
    except Exception:
        return []


# ─── Marks for the prompt ─────────────────────────────────────────────────────

def mark_segments(segments: Sequence[Dict[str, Any]], loud: Iterable[Tuple[float, float]] = (),
                  chat: Iterable[Tuple[float, int]] = ()) -> Tuple[List[Dict[str, Any]], int]:
    """Copies of the segments; the one at each reaction gets «[реакція: ...]» before its text. Returns (segments, marks)."""
    segs = [dict(s) for s in segments]
    if not segs:
        return segs, 0
    notes: Dict[int, List[str]] = {}

    def at(t: float) -> int:
        best, dist = 0, float("inf")
        for i, s in enumerate(segs):
            a, b = float(s["start"]), float(s["end"])
            d = 0.0 if a <= t <= b else min(abs(t - a), abs(t - b))
            if d < dist:
                best, dist = i, d
        return best if dist <= 15 else -1

    for t, d in loud:
        i = at(t)
        if i >= 0:
            notes.setdefault(i, []).append(f"різкий сплеск гучності (+{d:.0f} дБ: крик, сміх?)")
    for t, c in chat:
        i = at(t + CHAT_WIN / 2)
        if i >= 0:
            notes.setdefault(i, []).append(f"чат вибухнув ({c} повідомлень за {int(CHAT_WIN)} с)")
    for i, ns in notes.items():
        segs[i]["text"] = f"{MARK} {'; '.join(dict.fromkeys(ns))}] {segs[i].get('text', '')}"
    return segs, len(notes)

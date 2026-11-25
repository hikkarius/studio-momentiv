"""
backend/compare.py — «Порівняти наосліп»: the classic and the experimental search on the same video,
side by side, without saying which is which.

Both searches run on the cached transcript and are NOT saved as the video's clips. Their best clips
are paired by rank (best vs best, second vs second…), each pair in random order. The author picks the
better clip of every pair; at the end the studio shows which search won and can take its clips.

State lives in web_output/<project>/compare.json:
  {"status": "running|ready|failed", "message", "progress", "error",
   "sets": {"classic": [...], "experimental": [...]},
   "pairs": [{"left": "classic", "right": "experimental", "l": 0, "r": 0, "vote": "left|right|same|"}]}
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend import db

log = logging.getLogger("shorts_core")

MODES = ("classic", "experimental")
MAX_PAIRS = 8
_lock = threading.Lock()


def _path(project_id: str) -> Path:
    from backend import job_runner
    return Path(job_runner.OUTPUT_DIR) / project_id / "compare.json"


def load(project_id: str) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(_path(project_id).read_text(encoding="utf-8"))
    except Exception:
        return None


def _save(project_id: str, state: Dict[str, Any]) -> None:
    p = _path(project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def make_pairs(sets: Dict[str, List[Dict[str, Any]]], rng: Optional[random.Random] = None) -> List[Dict[str, Any]]:
    """Pure. Best vs best, second vs second… by score; which side is which is random for every pair."""
    rng = rng or random.Random()
    ranked = {m: sorted(range(len(sets.get(m) or [])), key=lambda i: -float(sets[m][i].get("score", 0))) for m in MODES}
    n = min(MAX_PAIRS, *(len(ranked[m]) for m in MODES))
    pairs = []
    for k in range(n):
        left, right = list(MODES)
        if rng.random() < 0.5:
            left, right = right, left
        pairs.append({"left": left, "right": right, "l": ranked[left][k], "r": ranked[right][k], "vote": ""})
    return pairs


def tally(pairs: List[Dict[str, Any]]) -> Dict[str, int]:
    """Pure. Wins of every search plus «same» and how many pairs are still open."""
    out = {m: 0 for m in MODES}
    out.update(same=0, open=0)
    for p in pairs:
        v = p.get("vote") or ""
        if v in ("left", "right"):
            out[p[v]] += 1
        elif v == "same":
            out["same"] += 1
        else:
            out["open"] += 1
    return out


def view(project_id: str) -> Dict[str, Any]:
    """What the page needs: status, the pairs with both clips (but not which search made them, until the end)."""
    st = load(project_id)
    if not st:
        return {"status": "none"}
    out = {k: st.get(k) for k in ("status", "message", "progress", "error")}
    if st.get("status") != "ready":
        return out
    sets = st.get("sets") or {}
    t = tally(st["pairs"])
    done = t["open"] == 0
    pairs = []
    for i, p in enumerate(st["pairs"]):
        side = lambda s: _clip_view(sets[p[s]][p["l" if s == "left" else "r"]])  # noqa: E731
        item = {"n": i, "left": side("left"), "right": side("right"), "vote": p.get("vote") or ""}
        if done:
            item["left_mode"], item["right_mode"] = p["left"], p["right"]
        pairs.append(item)
    out.update(pairs=pairs, done=done, counts={m: len(sets.get(m) or []) for m in MODES})
    if done:
        out["tally"] = t
    return out


def _clip_view(c: Dict[str, Any]) -> Dict[str, Any]:
    return {k: c.get(k) for k in ("start", "end", "title", "hook_text", "reason")}


def vote(project_id: str, n: int, pick: str) -> Dict[str, Any]:
    if pick not in ("left", "right", "same", ""):
        raise ValueError("Невідомий вибір.")
    with _lock:
        st = load(project_id)
        if not st or st.get("status") != "ready" or not (0 <= n < len(st["pairs"])):
            raise ValueError("Такої пари немає.")
        st["pairs"][n]["vote"] = pick
        _save(project_id, st)
    return view(project_id)


def clips_of(project_id: str, mode: str) -> List[Dict[str, Any]]:
    st = load(project_id) or {}
    if st.get("status") != "ready" or mode not in MODES:
        raise ValueError("Порівняння ще не готове.")
    return list((st.get("sets") or {}).get(mode) or [])


def run(project_id: str, clips_count: int, filter_mode: str, language: Optional[str] = None) -> None:
    """Both searches one after another (the analysis lock keeps the GPU and Gemini to one job at a time)."""
    from backend import job_runner
    from constants import resolve_filter_mode
    from core import transcribe_video

    def progress(frac: float, msg: str) -> None:
        _save(project_id, {"status": "running", "progress": round(frac, 2), "message": msg, "started": started})

    started = time.time()
    progress(0.02, "Чекаю своєї черги...")
    with job_runner._analyze_lock:
        try:
            proj = db.get_project(project_id)
            if not proj:
                raise RuntimeError("Відео зникло зі студії.")
            progress(0.05, "Беру транскрипцію...")
            segments, words, lang, _ = transcribe_video(proj["source_path"], language=language)
            if not segments:
                raise RuntimeError("У відео не виявлено мовлення.")
            sets: Dict[str, List[Dict[str, Any]]] = {}
            labels = {"classic": "📼 перший спосіб", "experimental": "🧪 другий спосіб"}
            for i, mode in enumerate(MODES):
                base = 0.1 + 0.45 * i
                sets[mode] = job_runner.find_clips_for(
                    project_id, proj, segments, words, lang, clips_count, resolve_filter_mode(filter_mode), mode,
                    progress=lambda p, m, base=base, i=i: progress(base + 0.45 * p, f"Пошук {i + 1} з 2: {m}"),
                )
                log.info(f"Compare {project_id}: {labels[mode]} → {len(sets[mode])} clips")
            _save(project_id, {"status": "ready", "progress": 1.0, "message": "Готово", "started": started,
                               "sets": sets, "pairs": make_pairs(sets)})
        except Exception as e:
            log.exception(f"Compare {project_id} failed: {e}")
            _save(project_id, {"status": "failed", "progress": 1.0, "error": str(e), "message": "Помилка",
                               "started": started})


def start(project_id: str, clips_count: int, filter_mode: str, language: Optional[str] = None) -> None:
    st = load(project_id)
    if st and st.get("status") == "running" and time.time() - float(st.get("started") or 0) < 3600:
        return  # already running
    _save(project_id, {"status": "running", "progress": 0.0, "message": "Починаємо...", "started": time.time()})
    threading.Thread(target=run, args=(project_id, clips_count, filter_mode, language), daemon=True).start()

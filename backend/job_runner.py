"""
backend/job_runner.py — Background task executor for AI Video Moments Studio.
Handles audio transcription and Gemini virality analysis in background threads.
Provides SSE subscriber queues so frontend receives real-time progress without polling.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional, Set

from backend import db
from constants import resolve_filter_mode
from core import (
    analyze_and_find_clips,
    app_config,
    transcribe_video,
    validate_clips,
)
from ai_selector import GeminiIncompleteError, GeminiQuotaError, rerank_clips
from clip_refiner import refine_clips
from languages import language_label

log = logging.getLogger("shorts_core")

UPLOADS_DIR = "web_uploads"
OUTPUT_DIR = "web_output"

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="MomentsWorker")
_subscribers_lock = threading.Lock()
_subscribers: Dict[str, Set[asyncio.Queue]] = {}
_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop):
    global _loop
    _loop = loop


def register_subscriber(job_id: str) -> asyncio.Queue:
    """Registers an SSE client queue for real-time updates of job_id in a thread-safe manner."""
    q: asyncio.Queue = asyncio.Queue()
    with _subscribers_lock:
        if job_id not in _subscribers:
            _subscribers[job_id] = set()
        _subscribers[job_id].add(q)
    return q


def unregister_subscriber(job_id: str, q: asyncio.Queue):
    """Unregisters an SSE client queue in a thread-safe manner."""
    with _subscribers_lock:
        if job_id in _subscribers and q in _subscribers[job_id]:
            _subscribers[job_id].remove(q)
            if not _subscribers[job_id]:
                del _subscribers[job_id]


def broadcast_update(job_id: str, data: Dict[str, Any]):
    """Broadcasts update to SSE queues and updates database thread-safely."""
    db.update_job(
        job_id,
        status=data.get("status"),
        progress=data.get("progress"),
        message=data.get("message"),
        error=data.get("error"),
    )

    with _subscribers_lock:
        queues = list(_subscribers.get(job_id, []))

    if queues and _loop is not None:
        for q in queues:
            _loop.call_soon_threadsafe(q.put_nowait, data)


# ─── Worker Task ──────────────────────────────────────────────────────────────

# One analysis at a time: Whisper on the GPU and the Gemini quota do not like parallel videos.
_analyze_lock = threading.Lock()

# Jobs that stopped on the Gemini free limit or on Google overload (some windows unanswered):
# the queue reads these to pause instead of failing.
_quota_hits: Dict[str, Exception] = {}
_quota_lock = threading.Lock()


def pop_quota_hit(job_id: str) -> Optional[Exception]:
    """The limit / overload error a finished job stopped on, or None (read once).
    It has .daily and .retry_after; GeminiIncompleteError also .overloaded, .done and .total."""
    with _quota_lock:
        return _quota_hits.pop(job_id, None)


# Windows Gemini already answered for a project, kept until the analysis completes, so a pause
# or a studio restart only re-asks the missing ones.
_WINDOWS_FILE = "gemini_windows.json"


def _windows_path(project_id: str) -> str:
    return os.path.join(OUTPUT_DIR, project_id, _WINDOWS_FILE)


def load_done_windows(project_id: str) -> Dict[str, Any]:
    try:
        with open(_windows_path(project_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_done_windows(project_id: str, done: Dict[str, Any]) -> None:
    path = _windows_path(project_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log.warning(f"Could not save finished Gemini windows for {project_id}: {e}")


def clear_done_windows(project_id: str) -> None:
    try:
        os.remove(_windows_path(project_id))
    except OSError:
        pass


def transcribe_only(project_id: str, language: Optional[str] = None,
                    progress_cb: Optional[Callable[[float, str], None]] = None) -> None:
    """Only the Whisper step (its result is cached): the queue uses it while Gemini's limit is
    exhausted, so the later analysis goes straight to Gemini."""
    proj = db.get_project(project_id)
    if not proj:
        raise ValueError(f"Проєкт {project_id} не знайдено.")

    def cb(*args):
        if progress_cb and len(args) >= 2 and isinstance(args[0], (int, float)):
            progress_cb(max(0.0, min(1.0, float(args[0]))), str(args[1]))

    with _analyze_lock:
        segments, _words, _lang, _cached = transcribe_video(proj["source_path"], status_cb=cb, language=language)
    if not segments:
        raise RuntimeError("У відео не виявлено мовлення або транскрипція порожня.")


def auto_clips_count(duration: float) -> int:
    """How many clips to ask for when the user leaves the count on «Авто»: ~1 per 25 s, 5..100."""
    return max(5, min(100, round((duration or 0) / 25)))


def _run_analyze_worker(
    job_id: str,
    project_id: str,
    clips_count: int,
    filter_mode: str,
    engine: str = "whisper_local",
    language: Optional[str] = None,
):
    if not _analyze_lock.acquire(blocking=False):
        broadcast_update(job_id, {"status": "queued", "progress": 0.0, "message": "Чекає, поки закінчиться обробка іншого відео..."})
        _analyze_lock.acquire()
    try:
        _run_analyze_worker_locked(job_id, project_id, clips_count, filter_mode, engine, language)
    finally:
        _analyze_lock.release()


def _new_job_id(project_id: str) -> str:
    clean_suffix = project_id[-8:] if len(project_id) >= 8 else project_id
    return f"job_analyze_{clean_suffix}_{int(time.time() * 1000) % 100000}"


def _run_analyze_worker_locked(
    job_id: str,
    project_id: str,
    clips_count: int,
    filter_mode: str,
    engine: str = "whisper_local",
    language: Optional[str] = None,
):
    try:
        broadcast_update(job_id, {"status": "running", "progress": 0.05, "message": "Ініціалізація аналізу відео..."})
        proj = db.get_project(project_id)
        if not proj:
            raise ValueError(f"Проєкт {project_id} не знайдено.")

        video_path = proj["source_path"]
        db.update_project_status(project_id, "analyzing")

        canonical_filter = resolve_filter_mode(filter_mode)

        # Local Faster-Whisper GPU/CPU Transcription
        def whisper_cb(*args):
            if len(args) >= 2 and isinstance(args[0], (int, float)):
                pct = max(0.0, min(1.0, float(args[0])))
                mapped_progress = round(0.15 + 0.40 * pct, 2)
                msg = str(args[1])
            else:
                mapped_progress = 0.25
                msg = str(args[0]) if args else "Транскрипція..."
            broadcast_update(job_id, {"status": "running", "progress": mapped_progress, "message": msg})

        broadcast_update(job_id, {"status": "running", "progress": 0.10, "message": f"Розпізнавання мови (Whisper {app_config.whisper.model})..."})
        segments, words, detected_lang, from_cache = transcribe_video(video_path, status_cb=whisper_cb, language=language)
        if not segments:
            raise RuntimeError("У відео не виявлено мовлення або транскрипція порожня.")

        broadcast_update(job_id, {"status": "running", "progress": 0.60, "message": "ШІ-пошук вірусних моментів (Google Gemini)..."})

        # Gemini Virality Selection (raises GeminiUnavailableError with a user-facing message)
        min_len = app_config.clips.default_min_len
        max_len = app_config.clips.default_max_len
        # Ask for ~50% more candidates than needed: some get dropped as intros/ads, overlaps or too short
        request_count = min(app_config.clips.max_count, math.ceil(clips_count * 1.5))
        # Clips must not overlap, so the requested count limits their length: aim for ~80% coverage
        target_len = max(min_len + 3, min(45.0, proj["duration"] * 0.8 / max(1, clips_count)))
        done_windows = load_done_windows(project_id)
        try:
            raw_candidates = analyze_and_find_clips(
                client=None,
                segments=segments,
                count=request_count,
                min_len=min_len,
                max_len=max_len,
                filter_mode=canonical_filter,
                detected_lang=detected_lang,
                target_len=target_len,
                done_windows=done_windows,
            )
        except GeminiIncompleteError as e:
            if e.done:
                save_done_windows(project_id, {**done_windows, **e.done})
            raise
        clear_done_windows(project_id)

        broadcast_update(job_id, {"status": "running", "progress": 0.85, "message": "Підганяємо межі кліпів під речення..."})
        # Loose sanity check only: the refiner below snaps short clips out to full sentences
        rough = validate_clips(raw_candidates, proj["duration"], min_len=8, max_len=max_len + 30, tolerance=0)
        valid_clips = refine_clips(
            rough,
            words,
            video_duration=proj["duration"],
            count=clips_count,
            min_len=min_len,
            max_len=max_len,
            max_overlap=app_config.clips.max_overlap_sec,
            segments=segments,
        )
        if not valid_clips:
            raise RuntimeError(
                f"ШІ не повернув жодного коректного кліпу тривалістю {min_len}–{max_len} с. "
                "Транскрипцію збережено, спробуйте ще раз або змініть фільтр."
            )

        if app_config.gemini.rerank and len(valid_clips) > 1:
            broadcast_update(job_id, {"status": "running", "progress": 0.92, "message": "Другий прохід: порівнюємо кліпи між собою..."})
            valid_clips = rerank_clips(valid_clips, filter_mode=canonical_filter, detected_lang=detected_lang)

        # 3. Generate structured metadata files in OUTPUT_DIR/{project_id}
        out_dir = os.path.join(OUTPUT_DIR, project_id)
        os.makedirs(out_dir, exist_ok=True)
        txt_path = os.path.join(out_dir, "metadata.txt")
        json_path = os.path.join(out_dir, "metadata.json")

        with open(json_path, "w", encoding="utf-8") as jf:
            json.dump(valid_clips, jf, ensure_ascii=False, indent=2)

        with open(txt_path, "w", encoding="utf-8") as tf:
            tf.write("====================================================================\n")
            tf.write(f"  ВІРУСНІ МОМЕНТИ: {proj.get('title', 'Відео')}                     \n")
            tf.write(f"  Дата: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}      \n")
            tf.write(f"  Мова відео: {language_label(detected_lang)}\n")
            tf.write(f"  Всього знайдено моментів: {len(valid_clips)}                     \n")
            tf.write("====================================================================\n\n")

            for i, c in enumerate(valid_clips, 1):
                s_m, s_s = divmod(int(c.get("start", 0)), 60)
                e_m, e_s = divmod(int(c.get("end", 0)), 60)
                dur = int(c.get("end", 0) - c.get("start", 0))
                title = c.get("title", f"Кліп #{i}")
                score = c.get("score", 85)
                hook = c.get("hook_text") or (c.get("hook") if isinstance(c.get("hook"), str) else "")
                reason = c.get("reason", "")
                caption = c.get("caption", "")
                tags = " ".join(c.get("hashtags", []))

                tf.write(f"═══════════════════════════════════════════════════════════════════\n")
                tf.write(f"🎬 КЛІП #{i}: {title}\n")
                tf.write(f"═══════════════════════════════════════════════════════════════════\n")
                tf.write(f"⏱️ Таймкод:     {s_m:02d}:{s_s:02d} — {e_m:02d}:{e_s:02d}  ({dur} сек)\n")
                tf.write(f"🔥 Вірусний бал: {score}/100\n")
                if hook:
                    tf.write(f"🎯 Початковий хук: «{hook}»\n")
                if reason:
                    tf.write(f"💡 Чому це вірусно: {reason}\n")
                if caption:
                    tf.write(f"📝 Опис для посту: {caption}\n")
                if tags:
                    tf.write(f"🏷️ Хештеги:       {tags}\n")
                tf.write("\n")

        # 4. Save to DB
        db.save_candidate_clips(project_id, valid_clips)
        db.update_project_status(project_id, "ready")

        broadcast_update(job_id, {
            "status": "completed",
            "progress": 1.0,
            "message": f"Готово! Знайдено {len(valid_clips)} вірусних моментів.",
        })

    except Exception as e:
        log.exception(f"Analyze job {job_id} failed: {e}")
        if isinstance(e, (GeminiQuotaError, GeminiIncompleteError)):
            with _quota_lock:
                _quota_hits[job_id] = e
        err_text = str(e)
        if isinstance(e, GeminiIncompleteError) and e.overloaded:
            err_text += " Натисни «Знайти моменти» ще раз через кілька хвилин: доробимо лише те, чого бракує."
        db.update_project_status(project_id, "failed")
        broadcast_update(job_id, {"status": "failed", "progress": 1.0, "error": err_text, "message": f"Помилка: {err_text}"})


def enqueue_analyze_job(
    project_id: str,
    clips_count: int = 5,
    filter_mode: str = "Viral Hooks",
    engine: str = "whisper_local",
    language: Optional[str] = None,
) -> str:
    """Enqueues an analyze job for a project and returns the job ID."""
    job_id = _new_job_id(project_id)
    db.create_job(job_id, project_id, "analyze")
    _executor.submit(_run_analyze_worker, job_id, project_id, clips_count, filter_mode, engine, language)
    return job_id

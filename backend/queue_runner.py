"""
backend/queue_runner.py — Processing queue: several links / videos handled one after another.

One background thread takes the first waiting item, downloads it (for a link), then runs the
same analysis as the «Знайти моменти» button. Everything lives in the queue_items table, so the
queue survives a studio restart: items that were cut off go back to the front.

When the free Gemini limit runs out, the item is not failed: it gets status 'limit' and a resume
time (the per-day limit resets at midnight Pacific time, a per-minute one after a minute or so).
Meanwhile the queue keeps running Whisper on the next videos, so the later analysis only needs Gemini.
"""
from __future__ import annotations

import datetime
import logging
import threading
import time
import uuid
from typing import Any, Dict, Optional

from backend import db, job_runner, keep_awake, storage

log = logging.getLogger("shorts_core")

_wake = threading.Event()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def new_item_id() -> str:
    return "q_" + uuid.uuid4().hex[:10]


def notify() -> None:
    """Wakes the worker after something was added to the queue."""
    _wake.set()


def start() -> None:
    """Starts the worker thread once (called from the server lifespan)."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    resumed = db.requeue_interrupted_items()
    if resumed:
        log.info(f"Черга: {resumed} відео продовжимо після перезапуску.")
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="QueueWorker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()


def _update_keep_awake() -> None:
    from config_loader import app_config
    try:
        keep_awake.set_awake(bool(app_config.studio.keep_awake) and db.queue_has_pending_work())
    except Exception as e:
        log.warning(f"Черга: не вдалося перевірити, чи не давати ноутбуку заснути: {e}")


def _loop() -> None:
    while not _stop.is_set():
        _update_keep_awake()
        try:
            item = process_next()
        except Exception as e:  # never let the worker die
            log.exception(f"Черга: несподівана помилка: {e}")
            item = None
        if item is None:
            _wake.wait(timeout=5)
            _wake.clear()
    keep_awake.set_awake(False)


# ─── Gemini limit pause ──────────────────────────────────────────────────────

# Unknown or per-minute limit: wait longer each time it hits again in a row
_LIMIT_BACKOFF = (60, 5 * 60, 15 * 60, 60 * 60)
_DAILY_MARGIN = 5 * 60  # Google's reset is not to the second


def _pacific_offset_hours(utc: datetime.datetime) -> int:
    """UTC offset of US Pacific time (−7 in summer time, −8 otherwise), without the tz database
    (Windows Python ships without it unless the tzdata package is installed)."""
    y = utc.year

    def nth_sunday(month: int, n: int) -> datetime.date:
        d = datetime.date(y, month, 1)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)

    # DST: 2nd Sunday of March 02:00 PST (= 10:00 UTC) to 1st Sunday of November 02:00 PDT (= 09:00 UTC)
    start = datetime.datetime.combine(nth_sunday(3, 2), datetime.time(10), tzinfo=datetime.timezone.utc)
    end = datetime.datetime.combine(nth_sunday(11, 1), datetime.time(9), tzinfo=datetime.timezone.utc)
    return -7 if start <= utc < end else -8


def next_daily_reset(now: Optional[float] = None) -> float:
    """Unix time of the next midnight in US Pacific time, when the free per-day limit resets."""
    utc = datetime.datetime.fromtimestamp(now if now is not None else time.time(), tz=datetime.timezone.utc)
    pt = utc + datetime.timedelta(hours=_pacific_offset_hours(utc))
    midnight_pt = datetime.datetime.combine(pt.date() + datetime.timedelta(days=1), datetime.time(0),
                                            tzinfo=datetime.timezone.utc)
    reset = midnight_pt - datetime.timedelta(hours=_pacific_offset_hours(midnight_pt))
    return reset.timestamp()


def limit_resume_at(daily: bool, retry_after: Optional[float], hits: int, now: Optional[float] = None) -> float:
    """When to try Gemini again after a limit error (hits = how many pauses in a row before this one)."""
    now = now if now is not None else time.time()
    if daily:
        return next_daily_reset(now) + _DAILY_MARGIN
    wait = _LIMIT_BACKOFF[min(hits, len(_LIMIT_BACKOFF) - 1)]
    return now + max(wait, (retry_after or 0) + 5)


def _limit_message(daily: bool) -> str:
    return ("Безкоштовний ліміт Gemini на сьогодні закінчився. Мову вже розпізнано й збережено."
            if daily else
            "Gemini попросив перепочити: забагато запитів за хвилину. Мову вже розпізнано й збережено.")


def _pause_on_limit(item: Dict[str, Any], err) -> bool:
    """Puts the item on pause after a limit error. False when the user turned waiting off."""
    from config_loader import app_config
    if not app_config.studio.wait_for_limit:
        return False
    hits = int(item.get("limit_hits") or 0)
    resume_at = limit_resume_at(err.daily, err.retry_after, hits)
    db.pause_queue_item(item["id"], resume_at, err.daily, _limit_message(err.daily), hits=hits + 1)
    when = datetime.datetime.fromtimestamp(resume_at).strftime("%d.%m %H:%M")
    log.warning(f"Черга: ліміт Gemini ({'денний' if err.daily else 'хвилинний'}), продовжимо о {when}.")
    return True


def _prepare_while_paused(item: Dict[str, Any], project_id: str, paused_until: float) -> None:
    """While Gemini's limit is exhausted: only transcribe, then wait with the others."""
    item_id = item["id"]
    db.update_queue_item(item_id, status="analyzing", progress=0.0,
                         message="Поки чекаємо ліміт Gemini, розпізнаємо мову...")

    def on_progress(frac: float, msg: str) -> None:
        # same scale as a full analysis: speech recognition is the 15–55 % part
        db.update_queue_item(item_id, progress=round(0.15 + 0.40 * frac, 3), message=msg)

    job_runner.transcribe_only(project_id, language=item.get("language") or None, progress_cb=on_progress)
    paused = [i for i in db.list_queue_items() if i["status"] == "limit"]
    daily = bool(paused and paused[0].get("limit_daily"))
    db.pause_queue_item(item_id, max(paused_until, db.queue_paused_until()), daily,
                        "Мову вже розпізнано. Чекає ліміт Gemini.")


def process_next() -> Optional[Dict[str, Any]]:
    """Processes the next item to the end. Returns it (final state) or None if there is nothing to do."""
    from config_loader import app_config

    # A paused item whose time has come goes first; while the pause lasts only Whisper runs.
    item = db.claim_next_queue_item(due_limit_only=True)
    paused_until = 0.0
    if item is None:
        paused_until = db.queue_paused_until() if app_config.studio.wait_for_limit else 0.0
        item = db.claim_next_queue_item()
    if item is None:
        return None
    item_id = item["id"]
    try:
        project_id = item["project_id"]
        if not project_id:
            project_id = _download(item)
            if project_id is None:
                return db.get_queue_item(item_id)
        proj = db.get_project(project_id)
        if not proj:
            raise RuntimeError("Відео зникло зі студії до початку обробки.")

        if paused_until:
            db.update_queue_item(item_id, project_id=project_id, title=proj.get("title") or item["title"])
            _prepare_while_paused(item, project_id, paused_until)
            return db.get_queue_item(item_id)

        db.update_queue_item(
            item_id, project_id=project_id, title=proj.get("title") or item["title"],
            status="analyzing", progress=0.0, message="Починаємо аналіз...",
        )
        job_id = job_runner._new_job_id(project_id)
        db.create_job(job_id, project_id, "analyze")
        db.update_queue_item(item_id, job_id=job_id)
        clips_count = item["clips_count"] or job_runner.auto_clips_count(proj.get("duration") or 0)
        job_runner._run_analyze_worker(job_id, project_id, clips_count, item["filter_mode"],
                                       language=item.get("language") or None)

        job = db.get_job(job_id) or {}
        quota = job_runner.pop_quota_hit(job_id)
        if job.get("status") != "completed" and quota is not None and _pause_on_limit(item, quota):
            return db.get_queue_item(item_id)
        if job.get("status") == "completed":
            db.update_queue_item(item_id, status="done", progress=1.0, message=job.get("message") or "Готово")
        else:
            err = job.get("error") or job.get("message") or "Аналіз не вдався."
            db.update_queue_item(item_id, status="failed", progress=1.0, error=err, message="Помилка аналізу")
    except Exception as e:
        log.exception(f"Черга: {item_id} не вдалося: {e}")
        db.update_queue_item(item_id, status="failed", progress=1.0, error=str(e), message="Помилка")
    return db.get_queue_item(item_id)


def _download(item: Dict[str, Any]) -> Optional[str]:
    """Downloads the item's link. Returns the project ID, or None when the item is already finished."""
    from backend import server  # late import: server imports this module

    item_id = item["id"]
    db.update_queue_item(item_id, status="downloading", progress=0.0, message="Завантажуємо відео...")

    def on_progress(frac: float, msg: str) -> None:
        db.update_queue_item(item_id, progress=round(frac, 3), message=msg)

    try:
        project, reused = download_audio_first(item["source_url"], on_progress, item.get("language") or None)
    except storage.NotEnoughSpaceError:
        raise
    except ValueError as e:
        raise RuntimeError(f"Некоректне посилання: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Не вдалося завантажити відео: {_short_error(e)}") from e

    db.update_queue_item(item_id, project_id=project["id"], title=project.get("title") or "")
    if reused and project.get("status") == "ready" and db.get_candidate_clips(project["id"]):
        db.update_queue_item(
            item_id, status="done", progress=1.0,
            message="Це відео вже оброблене, кліпи готові",
        )
        return None
    return project["id"]


def download_audio_first(url: str, on_progress, language: Optional[str] = None):
    """Downloads the link's audio first (seconds), then the video in a parallel thread while
    Whisper transcribes the audio; the transcript is cached under the final video file, so the
    analysis skips Whisper. Any problem with the audio falls back to the old way (Whisper runs on
    the video later); a failed video download fails the item as before. Returns ingest_url's result."""
    import shutil
    import transcription
    from backend import server
    from utils import download_audio_from_url

    on_progress(0.0, "Дізнаюсь розмір відео…")
    size = server.probe_download_size(url)
    # The audio track (~60 MB per hour) fits in the disk reserve kept on top of the video size
    storage.ensure_space(server.UPLOADS_DIR, size)

    audio_dir = server.UPLOADS_DIR / f"temp_audio_{uuid.uuid4().hex[:8]}"
    try:
        on_progress(0.0, "Завантажуємо звук, щоб одразу почати розпізнавання...")
        try:
            audio_path = download_audio_from_url(url, str(audio_dir))
        except Exception as e:
            log.warning(f"Черга: звук окремо не завантажився ({_short_error(e)}), качаємо відео як раніше.")
            return server.ingest_url(url, progress_cb=on_progress)

        lock = threading.Lock()
        progress = {"video": 0.0, "whisper": None}

        def report() -> None:
            v, w = progress["video"], progress["whisper"]
            msg = f"Завантаження відео: {v * 100:.0f}%"
            msg += " · мову вже розпізнано" if w is not None and w >= 1.0 else (
                f" · розпізнавання мови: {w * 100:.0f}%" if w is not None else " · розпізнаємо мову...")
            try:
                on_progress(v, msg)
            except Exception:
                pass

        def video_cb(frac: float, _msg: str) -> None:
            with lock:
                progress["video"] = max(progress["video"], float(frac))
                report()

        def whisper_cb(*args) -> None:
            if len(args) >= 2 and isinstance(args[0], (int, float)):
                with lock:
                    progress["whisper"] = max(0.0, min(1.0, float(args[0])))
                    report()

        outcome: Dict[str, Any] = {}

        def fetch_video() -> None:
            try:
                outcome["result"] = server.ingest_url(url, progress_cb=video_cb, space_checked=True)
            except BaseException as e:  # handed to the queue thread below
                outcome["error"] = e

        video_thread = threading.Thread(target=fetch_video, name="VideoDownload", daemon=True)
        video_thread.start()

        cfg_lang = transcription.resolve_language(language)
        transcript = None
        t0 = time.monotonic()
        try:
            with job_runner._analyze_lock:
                transcript = transcription.transcribe_media(audio_path, cfg_lang, whisper_cb)
            log.info(f"Черга: мову розпізнано зі звуку за {time.monotonic() - t0:.0f} с, поки качалося відео.")
            with lock:
                progress["whisper"] = 1.0
                report()
        except Exception as e:
            log.warning(f"Черга: не вдалося розпізнати звук заздалегідь ({e}); розпізнаємо з відео.")

        video_thread.join()
        if "error" in outcome:
            raise outcome["error"]
        project, reused = outcome["result"]
        if transcript and transcript[0]:
            segments, words, lang = transcript
            video_path = project["source_path"]
            if transcription.load_transcript_cache_for(video_path, cfg_lang) is None:
                transcription.save_transcript_cache(video_path, segments, words, lang, requested=cfg_lang)
        return project, reused
    finally:
        shutil.rmtree(audio_dir, ignore_errors=True)


def _short_error(e: Exception) -> str:
    """yt-dlp errors carry the whole stderr; keep the line that says what went wrong."""
    stderr = getattr(e, "stderr", None) or ""
    if storage.is_no_space_error(f"{stderr}\n{e}"):
        return f"{storage.NO_SPACE_MARK}: він заповнився під час завантаження."
    for line in reversed(str(stderr).splitlines()):
        if "ERROR" in line:
            return line.split("ERROR:", 1)[-1].strip()[:300]
    return str(e)[:300]


def public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Queue item as the UI sees it: analysis progress comes live from its job."""
    out = dict(item)
    out["no_space"] = item["status"] == "failed" and storage.is_no_space_error(item.get("error") or "")
    if item["status"] == "analyzing" and item.get("job_id"):
        job = db.get_job(item["job_id"])
        if job:
            out["progress"] = job.get("progress") or 0.0
            out["message"] = job.get("message") or item["message"]
    return out

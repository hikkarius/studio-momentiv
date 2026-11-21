"""
backend/queue_runner.py — Processing queue: several links / videos handled one after another.

One background thread takes the first waiting item, downloads it (for a link), then runs the
same analysis as the «Знайти моменти» button. Everything lives in the queue_items table, so the
queue survives a studio restart: items that were cut off go back to the front.
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Optional

from backend import db, job_runner

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


def _loop() -> None:
    while not _stop.is_set():
        try:
            item = process_next()
        except Exception as e:  # never let the worker die
            log.exception(f"Черга: несподівана помилка: {e}")
            item = None
        if item is None:
            _wake.wait(timeout=5)
            _wake.clear()


def process_next() -> Optional[Dict[str, Any]]:
    """Processes the first waiting item to the end. Returns it (final state) or None if the queue is empty."""
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

        db.update_queue_item(
            item_id, project_id=project_id, title=proj.get("title") or item["title"],
            status="analyzing", progress=0.0, message="Починаємо аналіз...",
        )
        job_id = job_runner._new_job_id(project_id)
        db.create_job(job_id, project_id, "analyze")
        db.update_queue_item(item_id, job_id=job_id)
        clips_count = item["clips_count"] or job_runner.auto_clips_count(proj.get("duration") or 0)
        job_runner._run_analyze_worker(job_id, project_id, clips_count, item["filter_mode"])

        job = db.get_job(job_id) or {}
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
        project, reused = server.ingest_url(item["source_url"], progress_cb=on_progress)
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


def _short_error(e: Exception) -> str:
    """yt-dlp errors carry the whole stderr; keep the line that says what went wrong."""
    stderr = getattr(e, "stderr", None) or ""
    for line in reversed(str(stderr).splitlines()):
        if "ERROR" in line:
            return line.split("ERROR:", 1)[-1].strip()[:300]
    return str(e)[:300]


def public_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Queue item as the UI sees it: analysis progress comes live from its job."""
    out = dict(item)
    if item["status"] == "analyzing" and item.get("job_id"):
        job = db.get_job(item["job_id"])
        if job:
            out["progress"] = job.get("progress") or 0.0
            out["message"] = job.get("message") or item["message"]
    return out

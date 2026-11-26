"""
backend/server.py — High-performance FastAPI server for AI Video Moments Studio.
Serves modern REST API, SSE real-time job progress, safe media range streaming, and modern Web UI.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

import pauses
import subtitles_generator
import reactions
import streamer
import title_ideas
import vertical_render
from backend import app_update, backup, compare, db, job_runner, keep_awake, queue_runner, storage, support_report
from constants import resolve_filter_mode
from state import app_state
from transcription import load_transcript_cache
from languages import is_supported, language_choices
from utils import (
    download_from_url_with_title,
    probe_download_size,
    extract_clip_slice,
    find_duplicate_video,
    generate_video_thumbnail,
    get_video_duration,
    setup_console_encoding,
)

setup_console_encoding()
log = logging.getLogger("shorts_core")

# ─── Storage & Security Constants ─────────────────────────────────────────────

UPLOADS_DIR = Path("web_uploads")
OUTPUT_DIR = Path("web_output")

ALLOWED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"})
ALLOWED_MEDIA_EXTENSIONS = ALLOWED_VIDEO_EXTENSIONS | frozenset({".mp3", ".wav", ".jpg", ".jpeg", ".png", ".webp"})
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024 * 1024)))  # 10 GB default

_SAFE_PROJECT_ID_RE = re.compile(r"^[\w\-]{1,128}$", re.UNICODE)

_DEFAULT_CORS_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
_env_cors = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
ALLOWED_CORS_ORIGINS = [o.strip() for o in _env_cors.split(",") if o.strip()] if _env_cors else _DEFAULT_CORS_ORIGINS


def _validate_project_id(project_id: str) -> str:
    """Strictly validates project_id to block path traversal and control characters."""
    if not project_id or ".." in project_id or not _SAFE_PROJECT_ID_RE.fullmatch(project_id):
        raise HTTPException(status_code=400, detail="Некоректний ідентифікатор проєкту.")
    return project_id


def _safe_project_output_dir(project_id: str) -> Path:
    """Returns resolved web_output/{project_id} verified to reside strictly inside OUTPUT_DIR."""
    clean_id = _validate_project_id(project_id)
    base = OUTPUT_DIR.resolve()
    target = (base / clean_id).resolve()
    if not target.is_relative_to(base) or target == base:
        raise HTTPException(status_code=400, detail="Некоректний шлях проєкту.")
    return target


def _safe_temp_audio_path(project_id: str) -> Path:
    """Returns resolved temp audio path verified to reside strictly inside UPLOADS_DIR."""
    clean_id = _validate_project_id(project_id)
    base = UPLOADS_DIR.resolve()
    target = (base / f"audio_{clean_id}.mp3").resolve()
    if not target.is_relative_to(base):
        raise HTTPException(status_code=400, detail="Некоректний шлях тимчасового аудіо.")
    return target


def generate_readable_project_id(filename_or_title: str) -> str:
    """
    Generates human-readable, timestamped project ID, e.g.:
    '2026-11-17_1820_15_хв_a1b2'
    Ensures created folders in web_uploads/ and web_output/ are self-documenting and sorted by date.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    base_name = os.path.splitext(os.path.basename(filename_or_title))[0]
    slug = re.sub(r'[^\w]+', '_', base_name, flags=re.UNICODE)
    slug = re.sub(r'_+', '_', slug).strip('_')
    slug = slug[:24] if slug else "video"
    salt = uuid.uuid4().hex[:4]
    return f"{timestamp}_{slug}_{salt}"


def _start_whisper_housekeeping() -> None:
    """Loads Whisper in the background right away and unloads it after idle minutes (tests replace this)."""
    try:
        from state import app_state
        app_state.start_whisper_housekeeping()
    except Exception as e:
        log.warning(f"Не вдалося запустити фонове завантаження Whisper: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_runner.set_event_loop(asyncio.get_running_loop())
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    db.init_db()
    stale = db.fail_interrupted_jobs()
    if stale:
        log.warning(f"Позначено як перервані {stale} задач(і), що висіли після минулого запуску.")
    try:
        storage.auto_cleanup(UPLOADS_DIR, OUTPUT_DIR)
    except Exception as e:
        log.warning(f"Автоприбирання не вдалося: {e}")
    queue_runner.start()
    backup.start()
    _start_whisper_housekeeping()
    log.info("AI Video Moments Studio Backend started.")
    yield
    queue_runner.stop()
    backup.stop()


_slice_lock = threading.Lock()
_slice_path_locks: Dict[str, threading.Lock] = {}


def _get_slice_path_lock(output_path: str) -> threading.Lock:
    """Returns a per-clip mutex so unrelated clip slices can extract concurrently."""
    norm_key = os.path.normcase(os.path.abspath(output_path))
    with _slice_lock:
        lock = _slice_path_locks.get(norm_key)
        if lock is None:
            lock = threading.Lock()
            _slice_path_locks[norm_key] = lock
        return lock

app = FastAPI(title="AI Video Moments Studio", version="3.0.0", lifespan=lifespan)

# Restrict CORS strictly to local Studio origins (prevents cross-origin CSRF/SSRF against localhost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Range", "Accept"],
    expose_headers=["Content-Disposition", "Content-Range", "Accept-Ranges", "Content-Length"],
)



@app.middleware("http")
async def revalidate_interface_files(request, call_next):
    """Interface files (page, CSS, JS) are always re-checked by the browser, so after an update
    people see the new look without Ctrl+F5. Unchanged files still come back as a cheap 304."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(("/css/", "/js/")):
        response.headers["Cache-Control"] = "no-cache"
    return response

# ─── Pydantic Models ──────────────────────────────────────────────────────────

class UrlUploadRequest(BaseModel):
    url: str

class AnalyzeRequest(BaseModel):
    clips_count: int = Field(default=5, ge=1, le=100)
    filter_mode: str = "Viral Hooks"
    engine: str = "whisper_local"
    language: str = ""   # '' = the language from the settings
    search_mode: str = Field(default="", pattern=r"^(|classic|experimental)$")

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and not is_supported(v):
            raise ValueError("Невідома мова відео.")
        return v

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, v: str) -> str:
        # Standardized exclusively on local Faster-Whisper pipeline
        return "whisper_local"

    @field_validator("filter_mode")
    @classmethod
    def validate_filter_mode(cls, v: str) -> str:
        return resolve_filter_mode(v).value


class QueueAddRequest(BaseModel):
    urls: List[str] = Field(default_factory=list, max_length=50)
    project_ids: List[str] = Field(default_factory=list, max_length=50)
    clips_count: int = Field(default=0, ge=0, le=100)   # 0 = auto by video length
    filter_mode: str = "Viral Hooks"
    language: str = ""   # '' = the language from the settings
    search_mode: str = Field(default="", pattern=r"^(|classic|experimental)$")  # '' = the default from the settings

    @field_validator("language")
    @classmethod
    def validate_language(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and not is_supported(v):
            raise ValueError("Невідома мова відео.")
        return v

    @field_validator("filter_mode")
    @classmethod
    def validate_filter_mode(cls, v: str) -> str:
        return resolve_filter_mode(v).value


class QueueMoveRequest(BaseModel):
    direction: int = Field(ge=-1, le=1)


class StorageCleanupRequest(BaseModel):
    junk: bool = False
    clips: bool = False
    older_than_days: int = Field(default=0, ge=0, le=3650)
    project_ids: Optional[List[str]] = None


class TrimClipRequest(BaseModel):
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(gt=0.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> TrimClipRequest:
        if self.start_sec >= self.end_sec:
            raise ValueError("Початок кліпу (start_sec) має бути строго меншим за кінець (end_sec).")
        if self.end_sec - self.start_sec < 5.0:
            raise ValueError("Мінімальна тривалість кліпу становить 5.0 секунд.")
        return self


# ─── REST Endpoints ───────────────────────────────────────────────────────────

@app.get("/api/health")
def get_health():
    caps = app_state.get_gpu_capabilities()
    return {
        "status": "ok",
        "has_nvenc": caps.has_nvenc,
        "has_cuda": caps.has_cuda,
        "gpu_name": caps.nvenc_device_name or caps.cuda_device_name or "CPU Mode",
    }


@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Handles video file upload with extension/size validation, automatic deduplication, and safe persistence."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="Файл не вибрано.")
    try:
        storage.ensure_space(UPLOADS_DIR, int(request.headers.get("content-length") or 0))
    except storage.NotEnoughSpaceError as e:
        raise HTTPException(status_code=507, detail=str(e))

    raw_filename = os.path.basename(file.filename)
    safe_filename = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', raw_filename).strip('. ')
    safe_filename = safe_filename or "video.mp4"

    ext = Path(safe_filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Непідтримуваний формат файлу '{ext or 'без розширення'}'. Дозволені: {', '.join(sorted(ALLOWED_VIDEO_EXTENSIONS))}.",
        )

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    project_id = generate_readable_project_id(safe_filename)
    staging_target = str(UPLOADS_DIR / f"staging_{project_id}_{safe_filename}")

    total_written = 0
    try:
        with open(staging_target, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                total_written += len(chunk)
                if total_written > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="Файл перевищує максимально допустимий розмір.")
                buffer.write(chunk)
        if total_written == 0:
            raise HTTPException(status_code=400, detail="Завантажений файл порожній.")
    except HTTPException:
        if os.path.exists(staging_target):
            try:
                os.remove(staging_target)
            except OSError:
                pass
        raise
    except Exception:
        if os.path.exists(staging_target):
            try:
                os.remove(staging_target)
            except OSError:
                pass
        raise

    # Automatic deduplication: check asynchronously if identical video already exists in UPLOADS_DIR
    existing_duplicate = await asyncio.to_thread(find_duplicate_video, staging_target, str(UPLOADS_DIR))
    if existing_duplicate:
        log.info(f"Deduplication: Identical video already exists at '{existing_duplicate}'. Reusing existing file.")
        try:
            os.remove(staging_target)
        except OSError:
            pass
        final_video_path = existing_duplicate

        existing_project = db.get_project_by_source_path(final_video_path)
        if existing_project:
            log.info(f"Deduplication: Reusing existing project '{existing_project['id']}' for video '{final_video_path}'.")
            dur = existing_project.get("duration", 60.0)
            return {
                "status": "uploaded",
                "project": existing_project,
                "duration_formatted": f"{int(dur // 60)} хв {int(dur % 60)} с",
                "reused": True,
            }
    else:
        final_video_path = str(UPLOADS_DIR / f"{project_id}_{safe_filename}")
        if os.path.exists(final_video_path):
            try:
                os.remove(final_video_path)
            except OSError:
                pass
        os.rename(staging_target, final_video_path)

    try:
        duration = await asyncio.to_thread(get_video_duration, final_video_path)
    except Exception as e:
        log.warning(f"Could not probe duration with ffprobe: {e}")
        duration = 60.0

    project = db.create_project(
        project_id=project_id,
        title=safe_filename,
        source_path=final_video_path,
        duration=duration,
    )

    return {
        "status": "uploaded",
        "project": project,
        "duration_formatted": f"{int(duration // 60)} хв {int(duration % 60)} с",
    }


_fetch_chat = reactions.fetch_chat  # tests replace it: no network


def ingest_url(url: str, progress_cb=None, space_checked: bool = False) -> tuple[Dict[str, Any], bool]:
    """
    Downloads a video by URL into UPLOADS_DIR and creates its project.
    Returns (project, reused); reused=True when the same video was already in the studio.
    Raises ValueError for a bad link and other exceptions for download failures.
    space_checked=True: the caller already made sure the disk has room (skips the size probe).
    """
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("Некоректний URL.")

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Ask the site how big the video is and stop before downloading it onto a full disk
    if not space_checked:
        if progress_cb is not None:
            progress_cb(0.0, "Дізнаюсь розмір відео…")
        storage.ensure_space(UPLOADS_DIR, probe_download_size(url))
    staging_temp_id = uuid.uuid4().hex[:8]
    temp_dir = str(UPLOADS_DIR / f"temp_url_{staging_temp_id}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        if progress_cb is None:
            downloaded_raw_path, video_title = download_from_url_with_title(url, output_dir=temp_dir)
        else:
            downloaded_raw_path, video_title = download_from_url_with_title(url, output_dir=temp_dir, progress_cb=progress_cb)
        raw_filename = os.path.basename(downloaded_raw_path)
        safe_filename = re.sub(r'[\x00-\x1f\\/:*?"<>|]', '_', raw_filename).strip('. ') or "downloaded_video.mp4"
        project_id = generate_readable_project_id(video_title or safe_filename)

        # Automatic deduplication for URL download
        existing_duplicate = find_duplicate_video(downloaded_raw_path, str(UPLOADS_DIR))
        if existing_duplicate:
            log.info(f"Deduplication: Identical downloaded video already exists at '{existing_duplicate}'.")
            try:
                os.remove(downloaded_raw_path)
            except OSError:
                pass
            video_path = existing_duplicate

            existing_project = db.get_project_by_source_path(video_path)
            if existing_project:
                log.info(f"Deduplication: Reusing existing project '{existing_project['id']}' for URL video '{video_path}'.")
                return existing_project, True
        else:
            final_dest = str(UPLOADS_DIR / f"{project_id}_{safe_filename}")
            if os.path.exists(final_dest):
                try:
                    os.remove(final_dest)
                except OSError:
                    pass
            os.rename(downloaded_raw_path, final_dest)
            video_path = final_dest

        duration = get_video_duration(video_path)
        project = db.create_project(
            project_id=project_id,
            title=(video_title.strip()[:200] if video_title.strip() else safe_filename),
            source_path=video_path,
            duration=duration,
        )
        db.set_project_source_url(project["id"], url)  # later: YouTube «most replayed» for the experimental search
        try:  # a Twitch VOD / YouTube stream: its chat replay helps find the reactions (best effort, ≤5 min)
            n = _fetch_chat(url, str(_safe_project_output_dir(project["id"]) / "chat.json"))
            if n:
                log.info(f"Chat replay for {project['id']}: {n} messages")
        except Exception as e:
            log.warning(f"Chat replay skipped: {e}")
        return project, False
    finally:
        if os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass


def _duration_formatted(project: Dict[str, Any]) -> str:
    dur = project.get("duration") or 60.0
    return f"{int(dur // 60)} хв {int(dur % 60)} с"


@app.post("/api/upload-url")
def upload_from_url(payload: UrlUploadRequest):
    """Downloads video from URL (YouTube/TikTok) with automatic deduplication and safe cleanup."""
    url = payload.url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="Некоректний URL.")
    try:
        project, reused = ingest_url(url)
    except storage.NotEnoughSpaceError as e:
        raise HTTPException(status_code=507, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некоректне посилання: {e}")
    except Exception as e:
        log.exception(f"Download URL failed: {e}")
        raise HTTPException(status_code=500, detail=f"Помилка завантаження відео за посиланням: {e}")
    result = {"status": "uploaded", "project": project, "duration_formatted": _duration_formatted(project)}
    if reused:
        result["reused"] = True
    return result


@app.post("/api/projects/{project_id}/analyze")
def start_analyze(project_id: str, payload: AnalyzeRequest):
    """Enqueues video transcription and virality discovery job with chosen engine."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    job_id = job_runner.enqueue_analyze_job(
        project_id=project_id,
        clips_count=payload.clips_count,
        filter_mode=payload.filter_mode,
        engine=payload.engine,
        language=payload.language or None,
        search_mode=payload.search_mode or None,
    )
    return {"job_id": job_id, "project_id": project_id, "status": "queued"}


# ─── «Порівняти наосліп»: classic vs experimental search ───────────────────────

class CompareVoteRequest(BaseModel):
    n: int = Field(ge=0, le=100)
    pick: str = Field(pattern=r"^(left|right|same|)$")


class CompareApplyRequest(BaseModel):
    mode: str = Field(pattern=r"^(classic|experimental)$")


@app.post("/api/projects/{project_id}/compare")
def start_compare(project_id: str):
    """Runs both searches on the cached transcript; the video's clips stay as they are."""
    from config_loader import app_config

    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    compare.start(project_id, job_runner.auto_clips_count(proj.get("duration") or 0), app_config.studio.default_filter)
    return compare.view(project_id)


@app.get("/api/projects/{project_id}/compare")
def get_compare(project_id: str):
    _validate_project_id(project_id)
    return compare.view(project_id)


@app.post("/api/projects/{project_id}/compare/vote")
def vote_compare(project_id: str, payload: CompareVoteRequest):
    _validate_project_id(project_id)
    try:
        return compare.vote(project_id, payload.n, payload.pick)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/projects/{project_id}/compare/apply")
def apply_compare(project_id: str, payload: CompareApplyRequest):
    """«Взяти кліпи переможця»: the chosen search's clips replace the unmarked ones (marked clips stay)."""
    _validate_project_id(project_id)
    if not db.get_project(project_id):
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    try:
        clips = compare.clips_of(project_id, payload.mode)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    db.save_candidate_clips(project_id, clips, keep_reviewed=True)
    db.set_project_search_mode(project_id, payload.mode)
    return {"status": "applied", "clips": len(clips)}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    """Server-Sent Events (SSE) endpoint for real-time progress updates."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Завдання не знайдено.")

    q = job_runner.register_subscriber(job_id)

    async def event_generator():
        try:
            # Yield initial status
            init_data = json.dumps({
                "status": job["status"],
                "progress": job["progress"],
                "message": job["message"],
                "error": job.get("error"),
            })
            yield f"data: {init_data}\n\n"

            if job["status"] in ("completed", "failed"):
                return

            while True:
                data = await q.get()
                yield f"data: {json.dumps(data)}\n\n"
                if data.get("status") in ("completed", "failed"):
                    break
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            job_runner.unregister_subscriber(job_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/projects")
def list_projects():
    """Lists recent projects."""
    projects = db.list_recent_projects(limit=500)  # the home page searches and sorts them itself
    return {"projects": projects}


@app.get("/api/projects/{project_id}")
def get_project_details(project_id: str):
    """Returns project details, original video path, and candidate clips."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    clips = db.get_candidate_clips(project_id)

    # Format timecodes for frontend convenience
    formatted_clips = []
    for c in clips:
        s_m, s_s = divmod(int(c["start_sec"]), 60)
        e_m, e_s = divmod(int(c["end_sec"]), 60)
        dur = int(c["duration"])
        formatted_clips.append({
            **c,
            "timecode_formatted": f"{s_m:02d}:{s_s:02d} — {e_m:02d}:{e_s:02d}",
            "duration_formatted": f"{dur} сек",
        })

    return {
        "project": proj,
        "clips": formatted_clips,
        "count": len(formatted_clips),
    }


@app.get("/api/projects/{project_id}/export-txt")
def export_metadata_txt(project_id: str):
    """Downloads structured metadata.txt file."""
    out_dir = _safe_project_output_dir(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    txt_path = out_dir / "metadata.txt"
    if not txt_path.exists():
        raise HTTPException(status_code=404, detail="Файл метаданих ще не сформовано.")

    return FileResponse(
        str(txt_path),
        media_type="text/plain; charset=utf-8",
        filename=f"moments_{project_id}.txt",
    )


@app.get("/api/projects/{project_id}/export-json")
def export_metadata_json(project_id: str):
    """Downloads structured metadata.json file."""
    out_dir = _safe_project_output_dir(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    json_path = out_dir / "metadata.json"
    if not json_path.exists():
        raise HTTPException(status_code=404, detail="Файл JSON ще не сформовано.")

    return FileResponse(
        str(json_path),
        media_type="application/json; charset=utf-8",
        filename=f"moments_{project_id}.json",
    )


@app.get("/api/projects/{project_id}/thumbnail")
def get_project_thumbnail(project_id: str):
    """Returns project thumbnail image, generating it fast via FFmpeg on-the-fly if needed."""
    thumb_dir = _safe_project_output_dir(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    thumb_path = thumb_dir / "thumbnail.jpg"

    if not thumb_path.exists():
        source_path = proj.get("source_path", "")
        if source_path and os.path.exists(source_path):
            seek_sec = min(5.0, max(0.5, proj.get("duration", 10.0) / 4))
            generate_video_thumbnail(source_path, str(thumb_path), at_sec=seek_sec)

    if thumb_path.exists():
        return FileResponse(str(thumb_path), media_type="image/jpeg")

    raise HTTPException(status_code=404, detail="Прев'ю недоступне.")


@app.delete("/api/projects/{project_id}")
def delete_project_endpoint(project_id: str):
    """Deletes a project and cleanly purges all its physical files from disk and database."""
    _purge_project(project_id)
    return {"status": "deleted", "project_id": project_id}


def _purge_project(project_id: str) -> None:
    """Removes a project with its output folder, temporary audio, source video (if no other project uses it) and DB rows."""
    output_dir = _safe_project_output_dir(project_id)
    temp_audio = _safe_temp_audio_path(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    source_path = proj.get("source_path")

    # 1. Purge output directory (OUTPUT_DIR/{project_id} containing metadata, thumbnails, and slices)
    if output_dir.exists():
        try:
            shutil.rmtree(str(output_dir), ignore_errors=True)
            log.info(f"Purged project output directory: {output_dir}")
        except Exception as e:
            log.warning(f"Could not purge output dir {output_dir}: {e}")

    # 2. Purge temp audio if present
    if temp_audio.exists():
        try:
            temp_audio.unlink()
        except OSError:
            pass

    # 3. Purge physical source video if no other project references it
    if source_path and not db.is_source_path_used_by_other_projects(source_path, project_id):
        if os.path.exists(source_path):
            try:
                os.remove(source_path)
                log.info(f"Purged unreferenced source video: {source_path}")
            except Exception as e:
                log.warning(f"Could not remove source video {source_path}: {e}")

    # 4. Remove all records from database
    success = db.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=404, detail="Помилка видалення з бази даних.")


def _clip_file_stem(clip: Dict[str, Any]) -> str:
    safe_title = "".join(c for c in clip["title"] if c.isalnum() or c in (" ", "_", "-")).strip()
    safe_title = safe_title[:40].replace(" ", "_") or f"clip_{clip['clip_index']}"
    return f"clip_{clip['clip_index']}_{int(clip['start_sec'])}s_{safe_title}"


def _clip_number(project_id: str, clip: Dict[str, Any]) -> int:
    """The clip's number as the studio shows it (№1, №2…): its place by start time among the video's clips."""
    clips = db.get_candidate_clips(project_id)
    order = sorted(range(len(clips)), key=lambda i: clips[i]["start_sec"])  # stable, like the editor's sort
    for num, i in enumerate(order, 1):
        if clips[i]["clip_index"] == clip["clip_index"]:
            return num
    return int(clip["clip_index"]) + 1


def _download_name(project_id: str, clip: Dict[str, Any], tag: str = "", ext: str = "mp4") -> str:
    """Name of a file the user downloads: «06 Скільки грошей треба для щастя 9x16.mp4», number as in the studio."""
    title = re.sub(r'[\x00-\x1f\\/:*?"<>|]', "", clip.get("title") or "")
    title = re.sub(r"\s+", " ", title).strip(" .")[:60].rstrip(" .") or "кліп"
    return f"{_clip_number(project_id, clip):02d} {title}{' ' + tag if tag else ''}.{ext}"


def _ensure_clip_slice(project_id: str, proj: Dict[str, Any], clip: Dict[str, Any]) -> tuple[str, str]:
    """Cuts (once, cached on disk) the MP4 for a clip. Returns (path, filename)."""
    project_out_dir = _safe_project_output_dir(project_id)
    source_path = proj["source_path"]
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Вихідне відео не знайдено на диску.")

    slices_dir = project_out_dir / "slices"
    slices_dir.mkdir(parents=True, exist_ok=True)
    slice_filename = f"{_clip_file_stem(clip)}.mp4"
    output_slice_path = str(slices_dir / slice_filename)

    path_lock = _get_slice_path_lock(output_slice_path)
    with path_lock:
        if not os.path.exists(output_slice_path):
            temp_slice_path = str(slices_dir / f"tmp_{uuid.uuid4().hex[:6]}_{slice_filename}")
            try:
                extract_clip_slice(
                    source_path=source_path,
                    start_sec=clip["start_sec"],
                    end_sec=clip["end_sec"],
                    output_path=temp_slice_path,
                )
                if os.path.exists(temp_slice_path):
                    os.replace(temp_slice_path, output_slice_path)
            finally:
                if os.path.exists(temp_slice_path):
                    try:
                        os.remove(temp_slice_path)
                    except OSError:
                        pass
    return output_slice_path, _download_name(project_id, clip)


def _get_project_and_clip(project_id: str, clip_index: int) -> tuple[Dict[str, Any], Dict[str, Any]]:
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    clip = next((c for c in db.get_candidate_clips(project_id) if c["clip_index"] == clip_index), None)
    if not clip:
        raise HTTPException(status_code=404, detail="Кліп не знайдено.")
    return proj, clip


def _clip_cuts(clip: Dict[str, Any]) -> List[tuple]:
    """The clip's pauses to cut, cleaned against its current bounds."""
    return pauses.normalize_cuts(clip.get("cuts"), float(clip["start_sec"]), float(clip["end_sec"]))


def _ensure_clip_file(project_id: str, proj: Dict[str, Any], clip: Dict[str, Any], raw: bool = False) -> tuple[str, str]:
    """The 16:9 clip to hand out: without its cut pauses when it has any (re-encoded), else the lossless slice."""
    cuts = [] if raw else _clip_cuts(clip)
    if not cuts:
        return _ensure_clip_slice(project_id, proj, clip)
    source_path = proj["source_path"]
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Вихідне відео не знайдено на диску.")
    slices_dir = _safe_project_output_dir(project_id) / "slices"
    slices_dir.mkdir(parents=True, exist_ok=True)
    stem = _clip_file_stem(clip)
    path = slices_dir / f"{stem}_cut{pauses.cuts_key(cuts)}.mp4"
    lock = _get_slice_path_lock(str(path))
    with lock:
        if not path.exists():
            for old in slices_dir.glob(f"clip_{clip['clip_index']}_*_cut*.mp4"):
                try:
                    old.unlink()
                except OSError:
                    pass
            pauses.render_with_cuts(source_path, float(clip["start_sec"]), float(clip["end_sec"]), cuts, str(path),
                                    has_nvenc=app_state.get_gpu_capabilities().has_nvenc)
    return str(path), _download_name(project_id, clip, "без пауз")


@app.get("/api/projects/{project_id}/clips/{clip_index}/download")
def download_clip_slice(project_id: str, clip_index: int, raw: bool = False):
    """The 16:9 clip: lossless stream copy, or without its cut pauses (raw=1 always gives the untouched cut)."""
    _safe_project_output_dir(project_id)
    proj, target_clip = _get_project_and_clip(project_id, clip_index)
    output_slice_path, slice_filename = _ensure_clip_file(project_id, proj, target_clip, raw=raw)
    return FileResponse(
        output_slice_path,
        media_type="video/mp4",
        filename=slice_filename,
    )


@app.get("/api/projects/{project_id}/clips/{clip_index}/thumbnail")
def get_clip_thumbnail(project_id: str, clip_index: int):
    """A small picture of the clip for the clip strip: a frame from its first seconds, made once per bounds."""
    thumb_dir = _safe_project_output_dir(project_id) / "clip_thumbs"
    proj, target_clip = _get_project_and_clip(project_id, clip_index)
    start = float(target_clip.get("start_sec") or 0.0)
    end = float(target_clip.get("end_sec") or start)
    thumb_path = thumb_dir / f"{int(clip_index)}_{int(round(start * 10))}.jpg"
    if not thumb_path.exists():
        source_path = proj.get("source_path", "")
        if source_path and os.path.exists(source_path):
            at = start + min(2.0, max(0.0, (end - start) / 3))
            generate_video_thumbnail(source_path, str(thumb_path), at_sec=at)
    if thumb_path.exists():
        return FileResponse(str(thumb_path), media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})
    raise HTTPException(status_code=404, detail="Прев'ю недоступне.")


class ClipUpdateRequest(BaseModel):
    review_status: Optional[str] = Field(default=None, pattern=r"^(|keep|done|drop)$")
    title: Optional[str] = Field(default=None, max_length=200)
    caption: Optional[str] = Field(default=None, max_length=2000)
    hashtags: Optional[List[str]] = Field(default=None, max_length=30)
    vertical_opts: Optional[Dict[str, Any]] = None
    cuts: Optional[List[List[float]]] = Field(default=None, max_length=300)
    reserve: Optional[bool] = None  # False = «⬆ Перенести в Нові»


@app.patch("/api/projects/{project_id}/clips/{clip_index}")
def update_clip(project_id: str, clip_index: int, payload: ClipUpdateRequest):
    """Marks a clip as kept/rejected and edits its title, caption and hashtags."""
    proj, current = _get_project_and_clip(project_id, clip_index)
    fields = payload.model_dump(exclude_none=True)
    if "cuts" in fields:
        fields["cuts"] = pauses.normalize_cuts(fields["cuts"], float(current["start_sec"]), float(current["end_sec"]))
    if "vertical_opts" in fields:
        fields["vertical_opts"] = vertical_render.normalize_opts(fields["vertical_opts"])
    if fields.get("review_status"):
        fields["reserve"] = False  # a reviewed clip leaves «Запас» for good
    if "title" in fields and not fields["title"].strip():
        raise HTTPException(status_code=400, detail="Заголовок не може бути порожнім.")
    clip = db.update_clip_fields(project_id, clip_index, **fields)
    if not clip:
        raise HTTPException(status_code=404, detail="Кліп не знайдено.")
    freed = 0
    if fields.get("review_status") in db.FREED_STATUSES:
        # Finished or rejected: the rendered files are not needed any more. Bounds, texts and
        # vertical settings stay in the DB, so «Скачати» simply renders the clip again.
        freed = _drop_clip_files(project_id, clip_index, vertical=True)
    return {"status": "updated", "clip": clip, "freed_bytes": freed}


def _drop_clip_files(project_id: str, clip_index: int, vertical: bool = False) -> int:
    """Deletes cached 16:9 slices and SRTs of a clip (and its 9:16 renders when vertical=True). Returns freed bytes."""
    out_dir = _safe_project_output_dir(project_id)
    groups = [("slices", f"clip_{clip_index}_*"), ("subtitles", f"clip_{clip_index}_*")]
    if vertical:
        groups.append(("vertical", f"clip_{clip_index}_*.mp4"))
    freed = 0
    for sub, pattern in groups:
        d = out_dir / sub
        if not d.is_dir():
            continue
        for f in d.glob(pattern):
            if not f.is_file() or not f.resolve().is_relative_to(out_dir):
                continue
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
            except OSError:
                pass
    if freed:
        log.info(f"Кліп {clip_index} ({project_id}): звільнено {freed / 1e6:.1f} МБ.")
    return freed


def _levels(project_id: str, proj: Dict[str, Any], lo: float, hi: float) -> List[float]:
    """Loudness of [lo, hi] (50 values a second), cached on disk."""
    adir = _safe_project_output_dir(project_id) / "audio"
    adir.mkdir(parents=True, exist_ok=True)
    path = adir / f"levels_{lo:.2f}_{hi:.2f}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if not os.path.exists(proj["source_path"]):
        raise HTTPException(status_code=404, detail="Вихідне відео не знайдено на диску.")
    levels = pauses.audio_levels(proj["source_path"], lo, hi)
    if levels:
        olds = sorted(adir.glob("levels_*.json"), key=lambda f: f.stat().st_mtime)
        for old in olds[:-40]:  # keep the cache small
            try:
                old.unlink()
            except OSError:
                pass
        path.write_text(json.dumps(levels), encoding="utf-8")
    return levels


@app.get("/api/projects/{project_id}/clips/{clip_index}/levels")
def get_clip_levels(project_id: str, clip_index: int, pad: float = 10.0):
    """Audio loudness around a clip (clip ± pad) for the waveform on the word track."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    pad = max(0.0, min(float(pad), 30.0))
    dur = float(proj.get("duration") or 0.0) or float(clip["end_sec"]) + pad
    lo = round(max(0.0, float(clip["start_sec"]) - pad), 2)
    hi = round(min(dur, float(clip["end_sec"]) + pad), 2)
    levels = _levels(project_id, proj, lo, hi)
    return {"start": lo, "rate": pauses.LEVEL_RATE, "db": levels,
            "threshold": round(pauses.silence_threshold(levels), 1) if levels else None}


class PausesRequest(BaseModel):
    min_gap: float = Field(default=pauses.DEFAULT_MIN_GAP, ge=0.3, le=3.0)
    keep: float = Field(default=pauses.KEEP, ge=0.05, le=0.5)  # silence left in place of each cut pause


def _clip_sound(project_id: str, clip_index: int):
    """The clip, its loudness levels and the transcript words, for finding pauses."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    start, end = float(clip["start_sec"]), float(clip["end_sec"])
    levels = _levels(project_id, proj, round(start, 2), round(end, 2))
    cached = load_transcript_cache(proj["source_path"], any_model=True)
    words = cached[1] if cached else []
    if not levels and not words:
        raise HTTPException(status_code=409, detail="Не вдалося прочитати звук і немає транскрипції, тож паузи не видно.")
    return start, end, levels, words


@app.post("/api/projects/{project_id}/clips/{clip_index}/pauses")
def cut_clip_pauses(project_id: str, clip_index: int, payload: PausesRequest):
    """«Прибрати паузи»: finds pauses longer than min_gap inside the clip and saves them as cuts."""
    start, end, levels, words = _clip_sound(project_id, clip_index)
    keep = min(payload.keep, payload.min_gap - 0.1)
    cuts = pauses.find_pauses(start, end, levels=levels, words=words, min_gap=payload.min_gap, keep=keep)
    updated = db.update_clip_fields(project_id, clip_index, cuts=cuts)
    return {"cuts": [list(c) for c in cuts], "count": len(cuts), "removed_sec": pauses.removed(cuts),
            "clip": updated, "by": "audio" if levels else "words"}


@app.get("/api/projects/{project_id}/clips/{clip_index}/pauses/preview")
def preview_clip_pauses(project_id: str, clip_index: int):
    """What each tempo (Спокійний / Звичайний / Щільний) would cut, without saving anything."""
    start, end, levels, words = _clip_sound(project_id, clip_index)
    out = {}
    for name, (gap, keep) in pauses.TEMPOS.items():
        cuts = pauses.find_pauses(start, end, levels=levels, words=words, min_gap=gap, keep=keep)
        out[name] = {"min_gap": gap, "keep": keep, "count": len(cuts), "removed_sec": pauses.removed(cuts)}
    return {"tempos": out}


@app.post("/api/projects/{project_id}/clips/{clip_index}/title-ideas")
def make_title_ideas(project_id: str, clip_index: int):
    """Three hook titles (шок / питання / інтрига) for the 9:16 title plate, from Gemini; stored on the clip."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    cached = load_transcript_cache(proj["source_path"], any_model=True)
    words = cached[1] if cached else []
    lang = (cached[2] if cached else "") or "uk"
    text = title_ideas.clip_text_from_words(words, float(clip["start_sec"]), float(clip["end_sec"]))
    try:
        variants = title_ideas.suggest_titles(text, str(clip.get("title") or ""), lang, taste=db.title_choices(),
                                              why=str(clip.get("reason") or "")[:300])
    except title_ideas.TitleIdeasError as e:
        raise HTTPException(status_code=503, detail=str(e))
    updated = db.update_clip_fields(project_id, clip_index, title_variants=variants)
    return {"variants": variants, "clip": updated, "taste": db.title_choices()["total"]}


class TitleChoiceRequest(BaseModel):
    kind: str = Field(max_length=20)
    text: str = Field(min_length=1, max_length=200)


@app.post("/api/projects/{project_id}/clips/{clip_index}/title-choice")
def save_title_choice(project_id: str, clip_index: int, payload: TitleChoiceRequest):
    """Remembers which title the author put on the clip, so later suggestions follow their taste."""
    _get_project_and_clip(project_id, clip_index)
    db.add_title_choice(project_id, clip_index, payload.kind, payload.text)
    return {"ok": True, "taste": db.title_choices()["total"]}


@app.get("/api/projects/{project_id}/clips/{clip_index}/words")
def get_clip_words(project_id: str, clip_index: int, pad: float = 6.0):
    """Word-level transcript around a clip (clip ± pad seconds) for click-to-trim in the editor."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    pad = max(0.0, min(float(pad), 30.0))
    cached = load_transcript_cache(proj["source_path"], any_model=True)
    if not cached:
        return {"words": [], "available": False}
    _, words, _ = cached
    lo, hi = clip["start_sec"] - pad, clip["end_sec"] + pad
    out = [
        {"start": round(float(w["start"]), 2), "end": round(float(w["end"]), 2), "word": str(w.get("word", "")).strip()}
        for w in words
        if lo <= float(w.get("start", -1)) <= hi
    ]
    return {"words": out, "available": True}


@app.get("/api/projects/{project_id}/export-zip")
def export_selected_zip(project_id: str, clips: str = ""):
    """
    One archive with everything needed to publish: an MP4 + SRT per clip and posts.txt with
    titles, captions and hashtags. `clips` = comma-separated clip indexes; empty = clips marked 'keep'.
    """
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    all_clips = db.get_candidate_clips(project_id)
    if clips.strip():
        try:
            wanted = {int(x) for x in clips.split(",") if x.strip()}
        except ValueError:
            raise HTTPException(status_code=400, detail="Некоректний список кліпів.")
        chosen = [c for c in all_clips if c["clip_index"] in wanted]
    else:
        chosen = [c for c in all_clips if c.get("review_status") == "keep"]
    if not chosen:
        raise HTTPException(status_code=400, detail="Жодного кліпу «В роботі». Познач їх на кроці 1.")
    chosen.sort(key=lambda c: c.get("virality_score", 0), reverse=True)

    project_out_dir = _safe_project_output_dir(project_id)
    project_out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = project_out_dir / f"{project_id}_selected.zip"
    tmp_zip = project_out_dir / f"tmp_{uuid.uuid4().hex[:6]}.zip"
    posts: List[str] = []
    import zipfile
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for c in chosen:
                slice_path, slice_name = _ensure_clip_file(project_id, proj, c)
                zf.write(slice_path, arcname=slice_name)
                srt = subtitles_generator.generate_srt_for_clip(
                    video_path=proj["source_path"], start_sec=c["start_sec"], end_sec=c["end_sec"], target_words_per_cue=3,
                    cuts=_clip_cuts(c),
                )
                zf.writestr(slice_name[:-4] + ".srt", srt)
                s_m, s_s = divmod(int(c["start_sec"]), 60)
                posts.append(
                    f"№{_clip_number(project_id, c)}. {c['title']}  (оцінка {c.get('virality_score', '')}, {s_m:02d}:{s_s:02d}, {int(c['duration'])} с)\n"
                    f"{c.get('caption', '')}\n{' '.join(c.get('hashtags') or [])}\n"
                )
            zf.writestr("posts.txt", f"{proj.get('title', '')}\n\n" + "\n".join(posts))
        os.replace(tmp_zip, zip_path)
    finally:
        if tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass

    safe_proj_title = "".join(ch for ch in proj.get("title", "video") if ch.isalnum() or ch in (" ", "_", "-")).strip()
    safe_proj_title = safe_proj_title[:40].replace(" ", "_") or "video"
    return FileResponse(str(zip_path), media_type="application/zip", filename=f"{safe_proj_title}_{len(chosen)}_clips.zip")


@app.get("/api/projects/{project_id}/clips/{clip_index}/export-srt")
def export_clip_srt(project_id: str, clip_index: int):
    """Generates and downloads a clean, relative zero-based .SRT subtitle file for a clip."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    clips = db.get_candidate_clips(project_id)
    target_clip = next((c for c in clips if c["clip_index"] == clip_index), None)
    if not target_clip:
        raise HTTPException(status_code=404, detail="Кліп не знайдено.")

    source_path = proj["source_path"]
    project_out_dir = _safe_project_output_dir(project_id)
    subs_dir = project_out_dir / "subtitles"
    subs_dir.mkdir(parents=True, exist_ok=True)

    safe_title = "".join(c for c in target_clip["title"] if c.isalnum() or c in (" ", "_", "-")).strip()
    safe_title = safe_title[:40].replace(" ", "_") or f"clip_{clip_index}"
    srt_filename = f"clip_{clip_index}_{int(target_clip['start_sec'])}s_{safe_title}.srt"
    output_srt_path = str(subs_dir / srt_filename)

    subtitles_generator.write_clip_srt_file(
        video_path=source_path,
        start_sec=target_clip["start_sec"],
        end_sec=target_clip["end_sec"],
        output_path=output_srt_path,
        target_words_per_cue=3,
        cuts=_clip_cuts(target_clip),
    )

    return FileResponse(
        output_srt_path,
        media_type="text/plain; charset=utf-8",
        filename=_download_name(project_id, target_clip, ext="srt"),
    )


@app.get("/api/projects/{project_id}/export-all-subtitles")
def export_all_subtitles(project_id: str):
    """Exports all clips' .SRT subtitles as a single downloadable ZIP archive."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    clips = db.get_candidate_clips(project_id)
    if not clips:
        raise HTTPException(status_code=404, detail="У проєкті немає збережених кліпів.")

    project_out_dir = _safe_project_output_dir(project_id)
    zip_path = str(project_out_dir / f"{project_id}_subtitles.zip")

    subtitles_generator.create_all_subtitles_zip(
        project_id=project_id,
        project_title=proj.get("title", "video"),
        source_path=proj["source_path"],
        clips=clips,
        output_zip_path=zip_path,
    )

    safe_proj_title = "".join(c for c in proj.get("title", "video") if c.isalnum() or c in (" ", "_", "-")).strip()
    safe_proj_title = safe_proj_title[:30].replace(" ", "_") or "video"
    download_filename = f"{safe_proj_title}_subtitles.zip"

    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename=download_filename,
    )


# ─── Vertical 9:16 clips with burned-in subtitles ─────────────────────────────

def _vertical_dir(project_id: str) -> Path:
    d = _safe_project_output_dir(project_id) / "vertical"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _clip_faces(project_id: str, proj: Dict[str, Any], clip: Dict[str, Any]) -> Dict[str, Any]:
    """Face samples for the clip's current bounds (cached on disk, ~3-5 s on first call)."""
    source_path = proj["source_path"]
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Вихідне відео не знайдено на диску.")
    vdir = _vertical_dir(project_id)
    path = vdir / f"faces2_{clip['clip_index']}_{clip['start_sec']:.2f}_{clip['end_sec']:.2f}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    lock = _get_slice_path_lock(str(path))
    with lock:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            faces = vertical_render.detect_faces(source_path, clip["start_sec"], clip["end_sec"])
        except ImportError:
            raise HTTPException(status_code=500, detail="Не встановлено OpenCV. Виконай: pip install opencv-python")
        except Exception as e:
            log.warning(f"Face detection failed for clip {clip['clip_index']}: {e}")
            raise HTTPException(status_code=500, detail=f"Не вдалося знайти обличчя: {e}")
        for old in [*vdir.glob(f"faces_{clip['clip_index']}_*.json"), *vdir.glob(f"faces2_{clip['clip_index']}_*.json")]:
            try:
                old.unlink()
            except OSError:
                pass
        path.write_text(json.dumps(faces), encoding="utf-8")
        return faces


def _clip_caption_words(proj: Dict[str, Any], clip: Dict[str, Any], opts: Dict[str, Any]) -> List[Dict[str, Any]]:
    cached = load_transcript_cache(proj["source_path"], any_model=True)
    if not cached:
        return []
    _, words, _ = cached
    return vertical_render.clip_words(words, clip["start_sec"], clip["end_sec"], opts.get("edits"), clip.get("title", ""))


def _clip_vertical_opts(project_id: str, clip: Dict[str, Any]) -> Dict[str, Any]:
    """The clip's settings; a clip nobody touched on a stream recording («Стрім» on for the video) gets «Стрім»."""
    raw = clip.get("vertical_opts") or {}
    opts = vertical_render.normalize_opts(raw)
    if "frame" not in raw:
        path = _stream_path(project_id)
        if path.exists():
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("use"):
                    opts["frame"] = "stream"
            except Exception:
                pass
    return opts


def _vertical_target(project_id: str, clip: Dict[str, Any], subs: bool,
                     proj: Optional[Dict[str, Any]] = None) -> tuple[Path, str]:
    """Where the 9:16 file for the clip's current settings lives (it may not be rendered yet), and its download name."""
    opts = _clip_vertical_opts(project_id, clip)
    subs = subs and opts["style"] != "none"
    cuts = _clip_cuts(clip)
    key = vertical_render.cache_key(clip["start_sec"], clip["end_sec"], opts, subs, clip.get("title", "")) + pauses.cuts_key(cuts)
    if opts["frame"] == "stream":
        proj = proj or db.get_project(project_id)
        if proj:
            key += _stream_key(_stream_info(project_id, proj))
    filename = _download_name(project_id, clip, "9x16" if subs else "9x16 без субтитрів")
    return _vertical_dir(project_id) / f"clip_{clip['clip_index']}_{key}_{'s' if subs else 'n'}.mp4", filename


def _ensure_vertical(project_id: str, proj: Dict[str, Any], clip: Dict[str, Any], subs: bool) -> tuple[str, str]:
    """Renders (once, cached by settings) the vertical MP4 of a clip. Returns (path, filename)."""
    opts = _clip_vertical_opts(project_id, clip)
    subs = subs and opts["style"] != "none"
    cuts = _clip_cuts(clip)
    cuts_rel = pauses.relative(cuts, float(clip["start_sec"]))
    stream_info = _stream_info(project_id, proj) if opts["frame"] == "stream" else None
    idx = clip["clip_index"]
    vdir = _vertical_dir(project_id)
    path, filename = _vertical_target(project_id, clip, subs, proj)
    if path.exists():
        return str(path), filename
    duration = clip["end_sec"] - clip["start_sec"]
    if stream_info:
        layout = _stream_layout(stream_info, opts, _stream_clip_faces(project_id, proj, clip, stream_info))
    else:
        faces = _clip_faces(project_id, proj, clip)
        layout = vertical_render.plan_layout(faces, opts["frame"], duration)
    if cuts_rel:
        # the camera path and the captions move to the timeline without the pauses
        if layout.keyframes:
            keys: Dict[float, float] = {}
            for t, x in layout.keyframes:
                keys[round(pauses.map_time_clamped(t, cuts_rel), 3)] = x  # a move inside a cut lands on the cut
            layout.keyframes = sorted(keys.items())
        if layout.kind == "stream" and layout.stream.get("scenes"):
            moved = []
            for sc in layout.stream["scenes"]:
                a, b = pauses.map_time_clamped(sc["start"], cuts_rel), pauses.map_time_clamped(sc["end"], cuts_rel)
                if b - a > 0.3:
                    moved.append({**sc, "start": round(a, 3), "end": round(b, 3)})
            layout.stream = {**layout.stream, "scenes": moved}
        duration -= pauses.removed(cuts)
    ass = None
    if subs:
        words = _clip_caption_words(proj, clip, opts)
        if cuts_rel:
            words = pauses.remap_words(words, cuts_rel)
        pos = opts["pos"] if opts["pos"] is not None else (50 if layout.kind == "split" else layout.stream.get("pos"))
        ass = vertical_render.build_ass(
            words, duration, opts["style"], pos,
            hook=(opts["hook_text"] or clip.get("title")) if opts["hook"] else None,
            hook_sec=None if opts["hook_full"] else 3.0,
            size=opts["size"], accent=opts["color"], text_color=opts["text"],
            hook_style=opts["hook_style"], hook_size=opts["hook_size"],
            round_plates=opts["round"], stroke=opts["stroke"],
            hook_round=opts["hook_round"], hook_stroke=opts["hook_stroke"],
            hook_pos=opts["hook_pos"] if opts["hook_pos"] is not None else layout.stream.get("hook_pos"),
        )
    if layout.kind == "stream":
        ass = streamer.nick_ass(ass, layout.stream)  # the streamer's nick is drawn even without subtitles
    caps = app_state.get_gpu_capabilities()
    with vertical_render.RENDER_LOCK:
        if not path.exists():
            vertical_render.render_vertical(proj["source_path"], clip["start_sec"], clip["end_sec"], str(path), layout, ass,
                                            has_nvenc=caps.has_nvenc, cuts_rel=cuts_rel or None)
    # Old renders of this clip with other settings are no longer reachable.
    for old in vdir.glob(f"clip_{idx}_*_{'s' if subs else 'n'}.mp4"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                pass
    return str(path), filename


# ─── «Стрім: вебка + гра» (streamer.py) ───────────────────────────────────────

def _get_project(project_id: str) -> Dict[str, Any]:
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    return proj


def _stream_path(project_id: str) -> Path:
    """Next to the project's folders, not inside vertical/: «Прибрати диск» clears renders but keeps the user's frame."""
    d = _safe_project_output_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "stream.json"


def _stream_info(project_id: str, proj: Dict[str, Any], redetect: bool = False) -> Dict[str, Any]:
    """The video's webcam rectangle: found once (~5-10 s), then kept in stream.json; the user can move it."""
    path = _stream_path(project_id)
    if path.exists() and not redetect:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    source_path = proj["source_path"]
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail="Вихідне відео не знайдено на диску.")
    with _get_slice_path_lock(str(path)):
        if path.exists() and not redetect:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass  # unreadable even under the lock: search again, keeping nothing
        old = {}
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        try:
            found = streamer.detect_webcam(source_path)
        except ImportError:
            raise HTTPException(status_code=500, detail="Не встановлено OpenCV. Виконай: pip install opencv-python")
        except Exception as e:
            log.warning(f"Webcam detection failed for {project_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Не вдалося знайти вебку: {e}")
        info = {"cam": found["cam"], "face": found["face"], "w": found["w"], "h": found["h"], "note": found["note"],
                "auto": True, "look": _stream_look(old), "use": old.get("use")}
        streamer.save_info(str(path), info)
        return info


def _stream_look(info: Dict[str, Any]) -> Dict[str, Any]:
    """The video's look (older files kept only game_x at the top level)."""
    base = {"game_x": info["game_x"]} if "game_x" in info else {}
    return streamer.clean_look(info.get("look") or {}, base)


def _stream_layout(info: Dict[str, Any], opts: Dict[str, Any], faces: Optional[Dict[str, Any]] = None) -> "vertical_render.Layout":
    look = opts.get("stream_look") or _stream_look(info)
    w, h = int(info["w"]), int(info["h"])
    sp = streamer.plan(info.get("cam"), info.get("face"), w, h, look)
    if faces and info.get("cam"):
        # parts of the clip where the streamer fills the screen or the webcam moved get their own picture
        sp = streamer.with_scenes(sp, streamer.scenes(faces.get("samples") or [], info["cam"], info.get("face")), w, h)
    if not info.get("cam"):
        note = info.get("note") or "Вебки немає."
        note += " Показую лише гру. Щоб додати вебку, натисни «Позначити вебку»."
    else:
        note = "" if info.get("auto") is False else (info.get("note") or "")
    kinds = {sc["kind"] for sc in sp.get("scenes") or []}
    if kinds:
        parts = (["стрімер на весь екран: там показую його крупно"] if "chat" in kinds else []) + \
                (["вебка переїхала: там беру її з нового місця"] if "cam" in kinds else [])
        note = (note + " " if note else "") + "У цьому кліпі змінюється сцена (" + "; ".join(parts) + ")."
    return vertical_render.Layout("stream", note=note, stream=sp)


def _stream_clip_faces(project_id: str, proj: Dict[str, Any], clip: Dict[str, Any], info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Face samples for the scene switches; a stream without a webcam or a failed search simply has none."""
    if not info.get("cam"):
        return None
    try:
        return _clip_faces(project_id, proj, clip)
    except HTTPException as e:
        log.warning(f"Stream scenes skipped for clip {clip.get('clip_index')}: {e.detail}")
        return None


def _stream_key(info: Dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps([info.get("cam"), info.get("face"), _stream_look(info)], sort_keys=True).encode()).hexdigest()[:6]


class StreamCamRequest(BaseModel):
    cam: Optional[List[float]] = None
    keep_cam: bool = False  # only change the look
    use: Optional[bool] = None  # «Стрім» on/off for this video (clips nobody touched follow it)
    look: Optional[Dict[str, Any]] = None  # partial: merged over the video's look


@app.get("/api/projects/{project_id}/stream")
def get_stream_info(project_id: str, detect: bool = True):
    """Where the webcam is in this stream recording (found automatically the first time).
    detect=false: only what is already known ({"known": false} if the video was never searched)."""
    proj = _get_project(project_id)
    if not detect and not _stream_path(project_id).exists():
        return {"known": False}
    return {**_stream_public(_stream_info(project_id, proj)), "known": True}


@app.post("/api/projects/{project_id}/stream/redetect")
def redetect_stream(project_id: str):
    proj = _get_project(project_id)
    return _stream_public(_stream_info(project_id, proj, redetect=True))


def _stream_public(info: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in info.items() if k != "game_x"}
    out["look"] = _stream_look(info)
    return out


@app.put("/api/projects/{project_id}/stream")
def set_stream_info(project_id: str, payload: StreamCamRequest):
    """The user moved the webcam frame (or switched it off with cam=null) or changed the look (sizes, zoom, styling)."""
    proj = _get_project(project_id)
    first = _stream_info(project_id, proj)  # searched once before the first change
    path = _stream_path(project_id)
    with _get_slice_path_lock(str(path)):  # quick clicks: one change at a time, each over the last one
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            info = dict(first)
        return _change_stream_info(project_id, info, payload)


def _change_stream_info(project_id: str, info: Dict[str, Any], payload: StreamCamRequest) -> Dict[str, Any]:
    info["look"] = _stream_look(info)
    if not payload.keep_cam:
        cam = streamer.clean_cam(payload.cam)
        info["cam"] = cam
        info["auto"] = False
        info["note"] = ""
        if cam and info.get("face"):
            fx, fy = info["face"]
            if not (cam[0] <= fx <= cam[0] + cam[2] and cam[1] <= fy <= cam[1] + cam[3]):
                info["face"] = None  # the face we found is not in the new frame: centre on the frame
    info["look"] = streamer.clean_look(payload.look or {}, _stream_look(info))
    info.pop("game_x", None)
    if payload.use is not None:
        info["use"] = payload.use
    streamer.save_info(str(_stream_path(project_id)), info)
    return info


@app.get("/api/projects/{project_id}/clips/{clip_index}/vertical-plan")
def get_vertical_plan(project_id: str, clip_index: int, frame: Optional[str] = None):
    """What the vertical clip will look like: crop plan + caption words (for the live preview)."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    opts = _clip_vertical_opts(project_id, clip)
    if frame:
        opts["frame"] = frame if frame in vertical_render.FRAME_MODES else "auto"
    if opts["frame"] == "stream":
        info = _stream_info(project_id, proj)
        layout = _stream_layout(info, opts, _stream_clip_faces(project_id, proj, clip, info))
        src = {"w": info["w"], "h": info["h"]}
    else:
        faces = _clip_faces(project_id, proj, clip)
        layout = vertical_render.plan_layout(faces, opts["frame"], clip["end_sec"] - clip["start_sec"])
        src = {"w": faces["w"], "h": faces["h"]}
    words = _clip_caption_words(proj, clip, opts)
    return {"opts": opts, "layout": layout.to_dict(), "src": src, "words": words}


@app.post("/api/projects/{project_id}/clips/{clip_index}/vertical")
def render_vertical_clip(project_id: str, clip_index: int, subs: bool = True):
    """Renders the vertical clip (waits until done) so the download afterwards is instant."""
    proj, clip = _get_project_and_clip(project_id, clip_index)
    try:
        _, filename = _ensure_vertical(project_id, proj, clip, subs)
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Vertical render failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ready", "filename": filename}


@app.get("/api/projects/{project_id}/clips/{clip_index}/vertical")
def download_vertical_clip(project_id: str, clip_index: int, subs: bool = True):
    proj, clip = _get_project_and_clip(project_id, clip_index)
    try:
        path, filename = _ensure_vertical(project_id, proj, clip, subs)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return FileResponse(path, media_type="video/mp4", filename=filename)


class VerticalZipRequest(BaseModel):
    clips: List[int] = Field(default_factory=list, max_length=200)
    subs: bool = True


_vjobs: Dict[str, Dict[str, Any]] = {}
_vjobs_lock = threading.Lock()


def _run_vertical_batch(job_id: str, project_id: str, proj: Dict[str, Any], chosen: List[Dict[str, Any]], subs: bool) -> None:
    """«Зробити всі»: renders the clips one by one (no archive); the list view polls which are ready."""
    job = _vjobs[job_id]
    try:
        for rank, c in enumerate(chosen, 1):
            job["current"] = c.get("title", "")
            job["current_index"] = c["clip_index"]
            fresh = next((x for x in db.get_candidate_clips(project_id) if x["clip_index"] == c["clip_index"]), None)
            if fresh is not None:  # the author may change a clip while the others render
                _ensure_vertical(project_id, proj, fresh, subs)
            job["done"] = rank
            job["ready"].append(c["clip_index"])
        job["status"] = "completed"
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        log.error(f"Vertical batch failed: {detail}")
        job["status"] = "failed"
        job["error"] = str(detail)
    finally:
        job["current_index"] = None


def _run_vertical_zip(job_id: str, project_id: str, proj: Dict[str, Any], chosen: List[Dict[str, Any]], subs: bool) -> None:
    import zipfile
    job = _vjobs[job_id]
    out_dir = _safe_project_output_dir(project_id)
    zip_path = out_dir / f"{project_id}_vertical.zip"
    tmp_zip = out_dir / f"tmp_{uuid.uuid4().hex[:6]}_vertical.zip"
    try:
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for rank, c in enumerate(chosen, 1):
                job["current"] = c.get("title", "")
                path, name = _ensure_vertical(project_id, proj, c, subs)
                zf.write(path, arcname=name)
                job["done"] = rank
        os.replace(tmp_zip, zip_path)
        job["status"] = "completed"
        job["file"] = str(zip_path)
    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        log.error(f"Vertical ZIP failed: {detail}")
        job["status"] = "failed"
        job["error"] = str(detail)
    finally:
        if tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass


@app.post("/api/projects/{project_id}/export-vertical-zip")
def start_vertical_zip(project_id: str, payload: VerticalZipRequest):
    """Starts rendering all chosen clips as vertical videos in the background; poll /api/vertical-jobs/{id}."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    all_clips = db.get_candidate_clips(project_id)
    if payload.clips:
        wanted = set(payload.clips)
        chosen = [c for c in all_clips if c["clip_index"] in wanted]
    else:
        chosen = [c for c in all_clips if c.get("review_status") == "keep"]
    if not chosen:
        raise HTTPException(status_code=400, detail="Жодного кліпу «В роботі». Познач їх на кроці 1.")
    chosen.sort(key=lambda c: c.get("virality_score", 0), reverse=True)
    job_id = uuid.uuid4().hex[:12]
    safe = "".join(ch for ch in proj.get("title", "video") if ch.isalnum() or ch in (" ", "_", "-")).strip()
    with _vjobs_lock:
        _vjobs[job_id] = {"id": job_id, "project_id": project_id, "status": "running", "done": 0, "total": len(chosen),
                          "current": "", "error": "", "file": "",
                          "filename": f"{(safe[:40].replace(' ', '_') or 'video')}_{len(chosen)}_vertical.zip"}
    threading.Thread(target=_run_vertical_zip, args=(job_id, project_id, proj, chosen, payload.subs), daemon=True).start()
    return {k: v for k, v in _vjobs[job_id].items() if k != "file"}


def _kept_clips(project_id: str, wanted: Optional[List[int]] = None) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
    clips = [c for c in db.get_candidate_clips(project_id) if c.get("review_status") == "keep"]
    if wanted:
        clips = [c for c in clips if c["clip_index"] in set(wanted)]
    return proj, clips


@app.get("/api/projects/{project_id}/vertical-status")
def vertical_status(project_id: str):
    """For the «Усі разом» list: each clip «В роботі», is its 9:16 (with subtitles) ready, which template it matches."""
    _, clips = _kept_clips(project_id)
    templates = db.list_vertical_templates()
    shapes = [(t["name"], vertical_render.template_of(t["opts"])) for t in templates]
    out = []
    for c in clips:
        path, _ = _vertical_target(project_id, c, True)
        look = vertical_render.template_of(c.get("vertical_opts"))
        out.append({"clip_index": c["clip_index"], "ready": path.exists(),
                    "template": next((n for n, shape in shapes if shape == {k: look[k] for k in shape}), None)})
    job = next((j for j in _vjobs.values() if j.get("project_id") == project_id and j.get("kind") == "batch"
                and j["status"] == "running"), None)
    return {"clips": out, "job": {k: v for k, v in job.items() if k != "file"} if job else None}


class VerticalBatchRequest(BaseModel):
    clips: List[int] = Field(default_factory=list, max_length=200)  # empty = every clip «В роботі» not yet made


@app.post("/api/projects/{project_id}/render-vertical")
def start_vertical_batch(project_id: str, payload: VerticalBatchRequest):
    """«Зробити незроблені»: renders 9:16 with subtitles in the background, one clip at a time."""
    proj, clips = _kept_clips(project_id, payload.clips)
    todo = [c for c in clips if not _vertical_target(project_id, c, True)[0].exists()]
    running = next((j for j in _vjobs.values() if j.get("project_id") == project_id and j.get("kind") == "batch"
                    and j["status"] == "running"), None)
    if running:
        return {k: v for k, v in running.items() if k != "file"}
    if not todo:
        raise HTTPException(status_code=400, detail="Усі кліпи «В роботі» вже зроблені.")
    todo.sort(key=lambda c: c.get("virality_score", 0), reverse=True)
    job_id = uuid.uuid4().hex[:12]
    with _vjobs_lock:
        _vjobs[job_id] = {"id": job_id, "kind": "batch", "project_id": project_id, "status": "running", "done": 0,
                          "total": len(todo), "current": "", "current_index": None, "ready": [], "error": "", "file": ""}
    threading.Thread(target=_run_vertical_batch, args=(job_id, project_id, proj, todo, True), daemon=True).start()
    return {k: v for k, v in _vjobs[job_id].items() if k != "file"}


class TemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    opts: Dict[str, Any]

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v or any(ch in v for ch in '/\\?#%'):
            raise ValueError("Назва шаблону порожня або має символи / \\ ? # %.")
        return v


@app.get("/api/vertical-templates")
def get_vertical_templates():
    return {"templates": db.list_vertical_templates()}


@app.put("/api/vertical-templates")
def put_vertical_template(payload: TemplateRequest):
    """Saves the look (style, colours, title plate…) under a name; the same name overwrites."""
    db.save_vertical_template(payload.name, vertical_render.template_of(payload.opts))
    return {"templates": db.list_vertical_templates()}


@app.delete("/api/vertical-templates/{name}")
def remove_vertical_template(name: str):
    if not db.delete_vertical_template(name):
        raise HTTPException(status_code=404, detail="Шаблон не знайдено.")
    return {"templates": db.list_vertical_templates()}


class ApplyTemplateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    clips: List[int] = Field(default_factory=list, max_length=200)  # empty = every clip «В роботі»


@app.post("/api/projects/{project_id}/apply-template")
def apply_vertical_template(project_id: str, payload: ApplyTemplateRequest):
    """Puts a template's look on clips; each clip keeps its own word edits and title text."""
    tpl = next((t for t in db.list_vertical_templates() if t["name"] == payload.name), None)
    if not tpl:
        raise HTTPException(status_code=404, detail="Шаблон не знайдено.")
    _, clips = _kept_clips(project_id, payload.clips)
    if not clips:
        raise HTTPException(status_code=400, detail="Жодного кліпу «В роботі». Познач їх на кроці 1.")
    updated = [db.update_clip_fields(project_id, c["clip_index"],
                                     vertical_opts=vertical_render.apply_template(c.get("vertical_opts"), tpl["opts"]))
               for c in clips]
    return {"clips": updated}


@app.get("/api/vertical-jobs/{job_id}")
def get_vertical_job(job_id: str):
    job = _vjobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Задачу не знайдено (сервер перезапускався?).")
    return {k: v for k, v in job.items() if k != "file"}


@app.get("/api/vertical-jobs/{job_id}/file")
def download_vertical_job(job_id: str):
    job = _vjobs.get(job_id)
    if not job or job.get("status") != "completed" or not os.path.exists(job.get("file", "")):
        raise HTTPException(status_code=404, detail="Архів ще не готовий.")
    return FileResponse(job["file"], media_type="application/zip", filename=job["filename"])


@app.put("/api/projects/{project_id}/clips/{clip_index}/trim")
def trim_clip(project_id: str, clip_index: int, payload: TrimClipRequest):
    """Adjusts start_sec and end_sec of a clip, persists to DB, and invalidates old slices."""
    _validate_project_id(project_id)
    proj = db.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Проєкт не знайдено.")

    duration = float(proj.get("duration", 0.0) or 0.0)
    if duration > 0 and payload.end_sec > duration:
        raise HTTPException(
            status_code=400,
            detail=f"Кінець кліпу ({payload.end_sec}с) перевищує тривалість відео ({duration:.1f}с)."
        )

    updated_clip = db.update_clip_bounds(
        project_id=project_id,
        clip_index=clip_index,
        new_start=payload.start_sec,
        new_end=payload.end_sec,
    )
    if not updated_clip:
        raise HTTPException(status_code=404, detail="Кліп не знайдено.")

    return {"status": "updated", "clip": _after_bounds_change(project_id, clip_index, updated_clip)}


def _after_bounds_change(project_id: str, clip_index: int, updated_clip: Dict[str, Any]) -> Dict[str, Any]:
    """Drops stale slices/SRTs of a clip, syncs metadata.json and adds formatted fields for the UI."""
    _drop_clip_files(project_id, clip_index)
    project_out_dir = _safe_project_output_dir(project_id)

    # Format fields for API response
    s = updated_clip["start_sec"]
    e = updated_clip["end_sec"]
    dur = updated_clip["duration"]
    s_m, s_s = divmod(int(s), 60)
    e_m, e_s = divmod(int(e), 60)
    updated_clip["timecode_formatted"] = f"{s_m:02d}:{s_s:02d} - {e_m:02d}:{e_s:02d}"
    updated_clip["duration_formatted"] = f"{int(dur)} сек"

    # Also sync metadata.json
    all_clips = db.get_candidate_clips(project_id)
    json_path = project_out_dir / "metadata.json"
    if json_path.exists():
        try:
            json_path.write_text(json.dumps(all_clips, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as ex:
            log.warning(f"Could not update metadata.json after trim: {ex}")
    return updated_clip


# ─── Safe Media Streaming (HTTP 206 Range Requests) ───────────────────────────

# ─── Processing queue ─────────────────────────────────────────────────────────

@app.get("/api/queue")
def get_queue():
    items = [queue_runner.public_item(i) for i in db.list_queue_items()]
    active = any(i["status"] in db.QUEUE_ACTIVE or i["status"] in db.QUEUE_PENDING for i in items)
    paused = [i for i in items if i["status"] == "limit"]
    return {
        "items": items, "active": active, "disk": storage.disk_status(UPLOADS_DIR),
        # Gemini limit pause: when the queue goes on by itself, and whether it is the per-day limit
        "paused_until": db.queue_paused_until(),
        "limit_daily": bool(paused and paused[0].get("limit_daily")),
        # '' = Gemini limit, 'overload' = Google's servers were overloaded
        "limit_reason": (paused[0].get("limit_reason") or "") if paused else "",
        "keep_awake": keep_awake.is_active(),
    }


@app.get("/api/disk")
def get_disk():
    """Free space for the home page notice and the check before sending files."""
    return {**storage.disk_status(UPLOADS_DIR), "reserve": storage.SPACE_RESERVE_BYTES}


@app.post("/api/queue")
def add_to_queue(payload: QueueAddRequest):
    """Adds links and/or already uploaded videos to the processing queue."""
    urls = [u.strip() for u in payload.urls if u and u.strip()]
    bad = [u for u in urls if not (u.startswith("http://") or u.startswith("https://"))]
    if bad:
        raise HTTPException(status_code=400, detail=f"Некоректне посилання: {bad[0][:120]}")
    if not urls and not payload.project_ids:
        raise HTTPException(status_code=400, detail="Додайте хоча б одне посилання або відео.")

    busy = {i["project_id"] for i in db.list_queue_items()
            if i["project_id"] and (i["status"] in db.QUEUE_PENDING or i["status"] in db.QUEUE_ACTIVE)}
    added = []
    for url in dict.fromkeys(urls):
        added.append(db.add_queue_item(queue_runner.new_item_id(), source_url=url, title=url,
                                       clips_count=payload.clips_count, filter_mode=payload.filter_mode,
                                       language=payload.language, search_mode=payload.search_mode))
    for pid in dict.fromkeys(payload.project_ids):
        _validate_project_id(pid)
        proj = db.get_project(pid)
        if not proj:
            raise HTTPException(status_code=404, detail="Проєкт не знайдено.")
        if pid in busy:
            continue
        added.append(db.add_queue_item(queue_runner.new_item_id(), project_id=pid, title=proj.get("title") or pid,
                                       clips_count=payload.clips_count, filter_mode=payload.filter_mode,
                                       language=payload.language, search_mode=payload.search_mode))
    queue_runner.notify()
    return {"added": added}


@app.delete("/api/queue/{item_id}")
def remove_from_queue(item_id: str):
    item = db.get_queue_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="У черзі такого немає.")
    if item["status"] in db.QUEUE_ACTIVE:
        raise HTTPException(status_code=409, detail="Це відео вже обробляється, його не можна зупинити посередині.")
    db.delete_queue_item(item_id)
    return {"status": "removed"}


@app.post("/api/queue/{item_id}/retry")
def retry_queue_item(item_id: str):
    item = db.get_queue_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="У черзі такого немає.")
    if item["status"] != "failed":
        raise HTTPException(status_code=409, detail="Повторити можна лише те, що не вдалося.")
    db.delete_queue_item(item_id)
    new = db.add_queue_item(queue_runner.new_item_id(), source_url=item["source_url"], project_id=item["project_id"],
                            title=item["title"], clips_count=item["clips_count"], filter_mode=item["filter_mode"],
                            language=item.get("language") or "", search_mode=item.get("search_mode") or "")
    queue_runner.notify()
    return {"item": new}


@app.post("/api/queue/{item_id}/resume")
def resume_queue_item(item_id: str):
    """«Спробувати зараз» on an item paused by the Gemini limit."""
    if not db.resume_queue_item_now(item_id):
        raise HTTPException(status_code=409, detail="Це відео не чекає ліміт Gemini.")
    queue_runner.notify()
    return {"item": db.get_queue_item(item_id)}


@app.post("/api/queue/{item_id}/move")
def move_queue_item(item_id: str, payload: QueueMoveRequest):
    if not db.move_queue_item(item_id, payload.direction):
        raise HTTPException(status_code=409, detail="Пересунути не вийшло.")
    return {"status": "moved"}


@app.post("/api/queue/clear-finished")
def clear_finished_queue():
    return {"removed": db.clear_finished_queue_items()}


# ─── Settings ─────────────────────────────────────────────────────────────────

WHISPER_MODEL_CHOICES = [
    {"value": "large-v3-turbo", "label": "Найкраща (large-v3-turbo) — рекомендовано"},
    {"value": "large-v3", "label": "Дуже точна, повільніша (large-v3)"},
    {"value": "medium", "label": "Середня (medium)"},
    {"value": "small", "label": "Швидка, менш точна (small)"},
]
LANGUAGE_CHOICES = language_choices()
FILTER_CHOICES = [
    {"value": "Viral Hooks", "label": "🔥 Вірусні гачки"},
    {"value": "Actionable Advice", "label": "💡 Експертні поради"},
    {"value": "Storytelling", "label": "🎭 Емоційні історії"},
]
SEARCH_CHOICES = [
    {"value": "classic", "label": "📼 Класичний",
     "hint": "Рівномірно нарізає все відео. Багато кліпів, зокрема середніх. Перевірений спосіб."},
    {"value": "experimental", "label": "🧪 Експериментальний",
     "hint": "ШІ читає все відео разом і бере лише сильні місця, кліп починається з хука. Кліпів менше (до 40), зате кращі."},
]
# Only these settings can be changed from the browser
_EDITABLE_SETTINGS = {
    "whisper": {"model", "language"},
    "clips": {"default_min_len", "default_max_len", "max_overlap_sec"},
    "gemini": {"rerank", "experimental_model"},
    "studio": {"default_clips", "default_filter", "default_search", "cleanup_days", "daily_backup", "keep_awake",
               "wait_for_limit"},
}


def _settings_view() -> Dict[str, Any]:
    from config_loader import app_config, ensure_gemini_api_key

    key = ensure_gemini_api_key() or ""
    cfg = app_config.model_dump()
    return {
        "values": {sec: {k: cfg[sec][k] for k in keys} for sec, keys in _EDITABLE_SETTINGS.items()},
        "gemini_key": {"set": bool(key), "hint": ("…" + key[-4:]) if len(key) >= 8 else ""},
        "choices": {"whisper_model": WHISPER_MODEL_CHOICES, "language": LANGUAGE_CHOICES, "filter": FILTER_CHOICES,
                    "search": SEARCH_CHOICES, "experimental_model": _experimental_model_choices()},
    }


def _experimental_model_choices() -> List[Dict[str, str]]:
    """«Модель ШІ для 🧪 експериментального пошуку»: auto (strongest free that answers) or fast.
    A model id set by hand in user_settings.json still works and shows as «Вибрано вручну»."""
    from backend.gemini_models import pretty_name
    from config_loader import app_config

    strong = list(app_config.gemini.strong_models or [])
    chain = ", далі ".join(pretty_name(m) for m in strong[:3]) or "найсильнішу безкоштовну модель"
    out = [{"value": "auto", "label": "Авто: найсильніша вільна", "tag": "Рекомендовано",
            "hint": f"Пробує {chain}. Якщо всі зайняті, бере швидку."}]
    out.append({"value": "fast", "label": "Швидка",
                "hint": "Як у класичному: відповідь за секунди, найбільший денний ліміт."})
    return out


@app.get("/api/settings")
def get_settings():
    return _settings_view()


@app.post("/api/settings")
def update_settings(payload: Dict[str, Any]):
    """Saves a partial change: {"clips": {"default_max_len": 45}, "gemini_api_key": "..."}."""
    from config_loader import app_config, save_gemini_api_key, save_user_settings
    from state import app_state

    patch: Dict[str, Any] = {}
    for sec, fields in payload.items():
        if sec == "gemini_api_key":
            continue
        if sec not in _EDITABLE_SETTINGS or not isinstance(fields, dict):
            raise HTTPException(status_code=400, detail=f"Невідоме налаштування: {sec}")
        for k, v in fields.items():
            if k not in _EDITABLE_SETTINGS[sec]:
                raise HTTPException(status_code=400, detail=f"Невідоме налаштування: {sec}.{k}")
        patch[sec] = fields
    if "studio" in patch and "default_filter" in patch["studio"]:
        if patch["studio"]["default_filter"] not in {c["value"] for c in FILTER_CHOICES}:
            raise HTTPException(status_code=400, detail="Невідомий стиль пошуку.")
    if "studio" in patch and "default_search" in patch["studio"]:
        if patch["studio"]["default_search"] not in {c["value"] for c in SEARCH_CHOICES}:
            raise HTTPException(status_code=400, detail="Невідомий спосіб пошуку.")
    if "gemini" in patch and "experimental_model" in patch["gemini"]:
        if patch["gemini"]["experimental_model"] not in {c["value"] for c in _experimental_model_choices()}:
            raise HTTPException(status_code=400, detail="Невідома модель ШІ.")
    if "whisper" in patch and "language" in patch["whisper"]:
        if not is_supported(str(patch["whisper"]["language"])):
            raise HTTPException(status_code=400, detail="Невідома мова відео.")

    old_model = app_config.whisper.model
    if patch:
        try:
            save_user_settings(patch)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=_friendly_settings_error(str(e)))
    if app_config.whisper.model != old_model:
        app_state.reset_whisper()

    key = payload.get("gemini_api_key")
    if isinstance(key, str) and key.strip():
        if len(key.strip()) < 20 or any(c.isspace() for c in key.strip()):
            raise HTTPException(status_code=400, detail="Це не схоже на ключ Gemini (має бути довгий рядок без пробілів).")
        save_gemini_api_key(key)
    return _settings_view()


class _KeyCheck(BaseModel):
    key: str
    save: bool = True


@app.get("/api/gemini/models")
def gemini_models_last():
    """The last «Перевірити зараз» result (null before the first check)."""
    from backend.gemini_models import last_check

    return {"check": last_check()}


@app.post("/api/gemini/models/check")
def gemini_models_check():
    """One tiny request to each model the studio may use: which ones this key can use right now."""
    from backend.gemini_models import check_models
    from config_loader import ensure_gemini_api_key

    if not (ensure_gemini_api_key() or "").strip():
        raise HTTPException(status_code=400, detail="Спершу додай ключ Gemini.")
    return {"check": check_models()}


@app.post("/api/settings/check-saved-key")
def check_saved_key():
    """«Налаштування»: asks Google whether the key saved in .env still works (free, no clips used)."""
    from config_loader import ensure_gemini_api_key

    key = (ensure_gemini_api_key() or "").strip()
    if not key:
        return {"ok": False, "message": "Ключа Gemini немає.", "settings": _settings_view()}
    return {**app_update.check_gemini_key(key), "settings": _settings_view()}


@app.post("/api/settings/check-key")
def check_key(payload: _KeyCheck):
    """First-run screen: really asks Google whether the key works, and saves it if it does."""
    from config_loader import save_gemini_api_key

    key = payload.key.strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        raise HTTPException(status_code=400, detail="Це не схоже на ключ Gemini (має бути довгий рядок без пробілів).")
    result = app_update.check_gemini_key(key)
    if result["ok"] and payload.save:
        save_gemini_api_key(key)
    return {**result, "settings": _settings_view()}


def _log_file_path() -> Path:
    for h in logging.getLogger("shorts_core").handlers + logging.getLogger().handlers:
        name = getattr(h, "baseFilename", None)
        if name:
            return Path(name)
    return Path(os.environ.get("SHORTS_LOG_FILE") or (Path(__file__).resolve().parent.parent / "shorts_automator.log"))


@app.get("/api/support-report")
def get_support_report():
    """«Зібрати звіт про проблему»: a ZIP with version, system, settings, recent errors and logs, key removed."""
    info: Dict[str, Any] = {}
    try:
        info["версія"] = app_update.app_info()
    except Exception as e:
        info["версія"] = f"помилка: {e}"
    try:
        info["відеокарта"] = get_health()
    except Exception as e:
        info["відеокарта"] = f"помилка: {e}"
    try:
        view = _settings_view()
        info["налаштування"] = {"values": view["values"], "ключ Gemini збережено": view["gemini_key"]["set"]}
    except Exception as e:
        info["налаштування"] = f"помилка: {e}"
    try:
        items = db.list_queue_items()[:15]
        info["черга (останні)"] = [
            {k: it.get(k) for k in ("title", "source_url", "status", "message", "error")} for it in items
        ]
    except Exception as e:
        info["черга (останні)"] = f"помилка: {e}"
    try:
        du = shutil.disk_usage(str(OUTPUT_DIR if Path(OUTPUT_DIR).exists() else "."))
        info["диск"] = {"вільно ГБ": round(du.free / 1e9, 1), "усього ГБ": round(du.total / 1e9, 1)}
    except Exception as e:
        info["диск"] = f"помилка: {e}"

    data = support_report.build_report(info, _log_file_path())
    name = f"studio-report-{datetime.now().strftime('%Y%m%d-%H%M')}.zip"
    return StreamingResponse(
        iter([data]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/app-info")
def get_app_info(refresh: bool = False):
    return app_update.app_info(force=refresh)


@app.post("/api/update")
def start_self_update():
    info = app_update.app_info()
    if info["dev_checkout"]:
        raise HTTPException(status_code=400, detail="Це робоча копія з git: оновлюй її через git pull.")
    if not info["can_self_update"]:
        raise HTTPException(status_code=400, detail="Оновлення з кнопки працює тільки у версії, встановленій інсталятором на Windows.")
    app_update.start_update()
    app_update.exit_soon(1.5)
    return {"ok": True, "message": "Студія закривається і оновлюється. Вона відкриється сама за кілька хвилин."}


# ─── Backups («Налаштування → Надійність») ───────────────────────────────────

@app.get("/api/backups")
def get_backups():
    items = backup.list_backups()
    return {"items": items, "keep": backup.KEEP, "total_size": sum(b["size"] for b in items)}


@app.post("/api/backups")
def make_backup():
    try:
        return {"item": backup.create_backup("manual")}
    except Exception as e:
        log.exception(f"Копія бази не вдалася: {e}")
        raise HTTPException(status_code=500, detail=f"Не вдалося зробити копію: {e}")


@app.post("/api/backups/{name}/restore")
def restore_backup(name: str):
    try:
        return backup.restore_backup(name)
    except backup.BackupError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/settings/reset")
def reset_settings():
    from config_loader import app_config, reset_user_settings
    from state import app_state

    old_model = app_config.whisper.model
    reset_user_settings()
    if app_config.whisper.model != old_model:
        app_state.reset_whisper()
    return _settings_view()


def _friendly_settings_error(err: str) -> str:
    if "default_min_len" in err and "strictly less" in err:
        return "Мінімальна довжина кліпу має бути меншою за максимальну."
    if "greater than or equal" in err or "less than or equal" in err:
        return "Одне зі значень виходить за дозволені межі."
    return f"Налаштування не збережено: {err[:200]}"


# ─── Disk space ───────────────────────────────────────────────────────────────

@app.get("/api/storage")
def get_storage():
    return storage.scan(UPLOADS_DIR, OUTPUT_DIR)


@app.post("/api/storage/cleanup")
def cleanup_storage(payload: StorageCleanupRequest):
    if not payload.junk and not payload.clips:
        raise HTTPException(status_code=400, detail="Нічого не вибрано для прибирання.")
    for pid in payload.project_ids or []:
        _validate_project_id(pid)
    result = storage.cleanup(UPLOADS_DIR, OUTPUT_DIR, junk=payload.junk, clips=payload.clips,
                             older_than_days=payload.older_than_days, project_ids=payload.project_ids)
    return {**result, "storage": storage.scan(UPLOADS_DIR, OUTPUT_DIR)}


class RemoveSourcesRequest(BaseModel):
    project_ids: List[str] = Field(min_length=1, max_length=500)


@app.post("/api/storage/delete-finished")
def delete_finished_videos(payload: RemoveSourcesRequest):
    """«Видалити відео, з якими закінчив»: deletes whole finished projects, the same way as «Видалити відео»."""
    for pid in payload.project_ids:
        _validate_project_id(pid)
    try:
        ids = storage.check_finished(UPLOADS_DIR, payload.project_ids, busy=db.busy_project_ids())
    except storage.NotRemovable as e:
        raise HTTPException(status_code=400, detail=str(e))
    before = storage.scan(UPLOADS_DIR, OUTPUT_DIR)
    sizes = {p["id"]: p["total_bytes"] for p in before["projects"]}
    for pid in ids:
        _purge_project(pid)
        log.info(f"Видалено відео, з яким закінчили: {pid}")
    after = storage.scan(UPLOADS_DIR, OUTPUT_DIR)
    return {"deleted": len(ids), "freed_bytes": sum(sizes.get(pid, 0) for pid in ids), "storage": after}


@app.get("/api/media")
def stream_media(path: str, request: Request):
    """Streams safe media files with HTTP Range support, strict path containment, and anti-XSS headers."""
    allowed_dirs = [
        UPLOADS_DIR.resolve(),
        OUTPUT_DIR.resolve(),
    ]
    target_path = Path(path).resolve()

    if not any(target_path.is_relative_to(d) and target_path != d for d in allowed_dirs):
        raise HTTPException(status_code=403, detail="Доступ заборонено.")

    if target_path.suffix.lower() not in ALLOWED_MEDIA_EXTENSIONS:
        raise HTTPException(status_code=403, detail="Непідтримуваний тип медіафайлу.")

    if not target_path.exists() or not target_path.is_file():
        raise HTTPException(status_code=404, detail="Файл не знайдено.")

    abs_path = str(target_path)
    file_size = os.path.getsize(abs_path)
    guessed_type, _ = mimetypes.guess_type(abs_path)
    if guessed_type and guessed_type.startswith(("video/", "audio/", "image/")):
        content_type = guessed_type
    else:
        content_type = "application/octet-stream"

    security_headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
    }

    range_header = request.headers.get("range")
    if not range_header:
        # Full content response
        def iter_file():
            with open(abs_path, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk

        return StreamingResponse(
            iter_file(),
            status_code=200,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                **security_headers,
            },
        )

    # Parse Range: bytes=start-end
    match = re.match(r"^bytes=(\d+)-(\d*)$", range_header)
    if not match:
        raise HTTPException(status_code=416, detail="Requested Range Not Satisfiable")

    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else file_size - 1

    if start >= file_size or end >= file_size or start > end:
        raise HTTPException(status_code=416, detail="Requested Range Not Satisfiable")

    content_length = end - start + 1

    def iter_range(offset: int, length: int):
        bytes_left = length
        with open(abs_path, "rb") as f:
            f.seek(offset)
            while bytes_left > 0:
                chunk_size = min(bytes_left, 1024 * 512)
                data = f.read(chunk_size)
                if not data:
                    break
                bytes_left -= len(data)
                try:
                    yield data
                except (ConnectionResetError, asyncio.CancelledError, GeneratorExit, Exception):
                    break

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(content_length),
        "Content-Type": content_type,
        **security_headers,
    }

    return StreamingResponse(iter_range(start, content_length), status_code=206, headers=headers)


# ─── Static Files & SPA Mounting ──────────────────────────────────────────────

static_dir = os.path.abspath("frontend")
if os.path.exists(static_dir):
    app.mount("/css", StaticFiles(directory=os.path.join(static_dir, "css")), name="css")
    app.mount("/js", StaticFiles(directory=os.path.join(static_dir, "js")), name="js")
    if os.path.isdir(os.path.join(static_dir, "fonts")):
        app.mount("/fonts", StaticFiles(directory=os.path.join(static_dir, "fonts")), name="fonts")

    @app.get("/favicon.svg", include_in_schema=False)
    @app.get("/favicon.ico", include_in_schema=False)
    def serve_favicon():
        return FileResponse(os.path.join(static_dir, "favicon.svg"), media_type="image/svg+xml",
                            headers={"Cache-Control": "max-age=86400"})

    @app.get("/")
    def serve_index():
        return FileResponse(os.path.join(static_dir, "index.html"))

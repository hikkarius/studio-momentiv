"""
tests/test_studio_backend.py — Comprehensive tests for Video Moments Studio:
FastAPI REST API, SQLite DB, Background Worker, and Metadata Export.
"""
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend import db, server
from backend.server import app

client = TestClient(app)


# ─── 1. SQLite Database Tests ────────────────────────────────────────────────

def test_db_projects_lifecycle():
    proj_id = f"test_p_{uuid.uuid4().hex[:8]}"
    p = db.create_project(proj_id, "test_video.mp4", "web_uploads/test.mp4", 120.0)
    assert p["id"] == proj_id
    assert p["title"] == "test_video.mp4"
    assert p["status"] == "uploaded"

    db.update_project_status(proj_id, "ready")
    fetched = db.get_project(proj_id)
    assert fetched["status"] == "ready"


def test_db_candidates_clips():
    proj_id = f"test_p_cand_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "sample.mp4", "sample.mp4", 90.0)

    clips = [
        {
            "start": 10.0,
            "end": 35.0,
            "title": "Хайлайт 1",
            "hook": "Крутий момент",
            "reason": "Висока вірусність",
            "caption": "Дивіться обов'язково",
            "hashtags": ["#shorts", "#інсайти"],
            "score": 92,
        }
    ]
    db.save_candidate_clips(proj_id, clips)
    saved = db.get_candidate_clips(proj_id)
    assert len(saved) == 1
    assert saved[0]["title"] == "Хайлайт 1"
    assert saved[0]["virality_score"] == 92
    assert saved[0]["hook_text"] == "Крутий момент"
    assert saved[0]["hashtags"] == ["#shorts", "#інсайти"]


def test_db_jobs_lifecycle():
    job_id = f"job_test_{uuid.uuid4().hex[:8]}"
    proj_id = f"test_p_jobs_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "jobs_test.mp4", "web_uploads/jobs_test.mp4", 30.0)
    j = db.create_job(job_id, proj_id, "analyze")
    assert j["status"] == "queued"

    db.update_job(job_id, status="running", progress=0.45, message="Аналіз...")
    updated = db.get_job(job_id)
    assert updated["status"] == "running"
    assert updated["progress"] == 0.45


# ─── 2. FastAPI Endpoints ───────────────────────────────────────────────────

def test_api_health():
    res = client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "has_nvenc" in data
    assert "has_cuda" in data


def test_api_projects_list():
    res = client.get("/api/projects")
    assert res.status_code == 200
    data = res.json()
    assert "projects" in data
    assert isinstance(data["projects"], list)


def test_api_project_not_found():
    res = client.get("/api/projects/non_existent_id_xyz")
    assert res.status_code == 404


def test_api_media_security_rejects_outside_paths():
    # Paths outside web_uploads and web_output must return 403 Forbidden
    res = client.get("/api/media?path=C:\\Windows\\System32\\calc.exe")
    assert res.status_code == 403


def test_run_analyze_worker(tmp_path):
    from backend.job_runner import _run_analyze_worker
    proj_id = f"test_proj_{uuid.uuid4().hex[:8]}"
    job_id = f"job_test_{uuid.uuid4().hex[:8]}"

    dummy_video = str(tmp_path / "video.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"dummy")

    db.create_project(proj_id, "video.mp4", dummy_video, 60.0)
    db.create_job(job_id, proj_id, "analyze")

    mock_segments = [{"start": 0.0, "end": 10.0, "text": "тестовий текст"}]
    mock_words = [{"word": "тестовий", "start": 0.0, "end": 5.0}, {"word": "текст", "start": 5.0, "end": 10.0}]

    def fake_transcribe(video_path, status_cb=None, language=None):
        if status_cb:
            status_cb("Транскрипцію знайдено у кеші — Whisper пропущено!")
        return (mock_segments, mock_words, "uk", True)

    mock_clips = [{
        "start": 0.0,
        "end": 20.0,
        "title": "Хайлайт",
        "score": 95,
        "reason": "Тестовий опис",
        "hook": "Тестовий хук",
        "caption": "Тестовий опис",
        "hashtags": ["#shorts"],
    }]

    with patch("backend.job_runner.transcribe_video", side_effect=fake_transcribe), \
         patch("backend.job_runner.analyze_and_find_clips", return_value=mock_clips):
        _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks")

    j = db.get_job(job_id)
    assert j["status"] == "completed"
    assert j["progress"] == 1.0

    candidates = db.get_candidate_clips(proj_id)
    assert len(candidates) == 1
    assert candidates[0]["title"] == "Хайлайт"

    # Verify export endpoints work
    res_txt = client.get(f"/api/projects/{proj_id}/export-txt")
    assert res_txt.status_code == 200
    assert "ВІРУСНІ МОМЕНТИ" in res_txt.text

    res_json = client.get(f"/api/projects/{proj_id}/export-json")
    assert res_json.status_code == 200
    data = res_json.json()
    assert len(data) == 1
    assert data[0]["title"] == "Хайлайт"


def test_upload_file_deduplication(tmp_path):
    # Uploading the exact same video file twice reuses the existing file
    content = b"VIDEO_CONTENT_BYTES" * 1000
    file_payload1 = {"file": ("dup_test.mp4", content, "video/mp4")}
    res1 = client.post("/api/upload", files=file_payload1)
    assert res1.status_code == 200
    path1 = res1.json()["project"]["source_path"]
    assert os.path.exists(path1)

    file_payload2 = {"file": ("dup_test2.mp4", content, "video/mp4")}
    res2 = client.post("/api/upload", files=file_payload2)
    assert res2.status_code == 200
    path2 = res2.json()["project"]["source_path"]

    # Reused the identical existing file on disk
    assert os.path.abspath(path1) == os.path.abspath(path2)

    # Clean up test upload
    if os.path.exists(path1):
        os.remove(path1)


def test_analyze_large_count_request():
    proj_id = f"test_p_100_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "big.mp4", "web_uploads/big.mp4", 1200.0)

    with patch("backend.job_runner._executor.submit"):
        res = client.post(f"/api/projects/{proj_id}/analyze", json={"clips_count": 100, "filter_mode": "Viral Hooks"})
        assert res.status_code == 200
        assert "job_id" in res.json()


def test_delete_project_endpoint():
    proj_id = f"test_del_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "delete_me.mp4", "web_uploads/delete_me.mp4", 60.0)
    assert db.get_project(proj_id) is not None

    res = client.delete(f"/api/projects/{proj_id}")
    assert res.status_code == 200
    assert res.json()["status"] == "deleted"
    assert db.get_project(proj_id) is None

    # Deleting non-existent project returns 404
    res_not_found = client.delete(f"/api/projects/{proj_id}")
    assert res_not_found.status_code == 404


def test_list_projects_with_clips_count():
    proj_id = f"test_hist_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "hist_test.mp4", "web_uploads/hist_test.mp4", 125.0)
    clips = [
        {"start": 10.0, "end": 30.0, "title": "Кліп 1", "score": 90},
        {"start": 40.0, "end": 65.0, "title": "Кліп 2", "score": 85},
    ]
    db.save_candidate_clips(proj_id, clips)

    res = client.get("/api/projects")
    assert res.status_code == 200
    projects = res.json()["projects"]
    target = next((p for p in projects if p["id"] == proj_id), None)
    assert target is not None
    assert target["clips_count"] == 2
    assert target["duration_formatted"] == "02:05"


def test_analyze_request_with_dual_engine():
    proj_id = f"test_dual_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "dual.mp4", "web_uploads/dual.mp4", 180.0)

    with patch("backend.job_runner._executor.submit") as mock_submit:
        # 1. Cloud engine
        res_cloud = client.post(
            f"/api/projects/{proj_id}/analyze",
            json={"clips_count": 5, "filter_mode": "Viral Hooks", "engine": "gemini_cloud"},
        )
        assert res_cloud.status_code == 200

        # 2. Local Whisper engine
        res_local = client.post(
            f"/api/projects/{proj_id}/analyze",
            json={"clips_count": 5, "filter_mode": "Viral Hooks", "engine": "whisper_local"},
        )
        assert res_local.status_code == 200
        assert mock_submit.call_count == 2


def test_run_analyze_worker_gemini_cloud(tmp_path):
    from backend.job_runner import _run_analyze_worker
    proj_id = f"test_cld_{uuid.uuid4().hex[:8]}"
    job_id = f"job_cld_{uuid.uuid4().hex[:8]}"

    dummy_video = str(tmp_path / "cld_video.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"dummy_cloud_video")

    db.create_project(proj_id, "cld_video.mp4", dummy_video, 90.0)
    db.create_job(job_id, proj_id, "analyze")

    mock_segments = [{"start": 5.0, "end": 35.0, "text": "Тестовий транскрибований текст"}]
    mock_clips = [{
        "start": 5.0,
        "end": 35.0,
        "title": "ХАЙЛАЙТ",
        "score": 96,
        "reason": "Аналіз через Whisper + Gemini",
        "hook": "Перші слова",
        "caption": "Опис для рілз",
        "hashtags": ["#viral", "#shorts"],
    }]

    with patch("backend.job_runner.transcribe_video", return_value=(mock_segments, [], "uk", False)), \
         patch("backend.job_runner.analyze_and_find_clips", return_value=mock_clips):
        _run_analyze_worker(job_id, proj_id, clips_count=1, filter_mode="Viral Hooks", engine="gemini_cloud")

    j = db.get_job(job_id)
    assert j["status"] == "completed"
    assert j["progress"] == 1.0

    candidates = db.get_candidate_clips(proj_id)
    assert len(candidates) == 1
    assert candidates[0]["title"] == "ХАЙЛАЙТ"


def test_project_thumbnail_endpoint(tmp_path):
    proj_id = f"test_thumb_{uuid.uuid4().hex[:8]}"
    dummy_video = str(tmp_path / "thumb_video.mp4")
    with open(dummy_video, "wb") as f:
        f.write(b"dummy_video_bytes")

    db.create_project(proj_id, "thumb_video.mp4", dummy_video, 45.0)

    def fake_generate_thumb(video_path, output_image_path, at_sec=3.0):
        os.makedirs(os.path.dirname(output_image_path), exist_ok=True)
        with open(output_image_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")  # Valid JPEG header bytes
        return output_image_path

    with patch("backend.server.generate_video_thumbnail", side_effect=fake_generate_thumb):
        res = client.get(f"/api/projects/{proj_id}/thumbnail")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/jpeg"

    # Non-existent project returns 404
    res_err = client.get("/api/projects/non_existent_thumb/thumbnail")
    assert res_err.status_code == 404


def test_generate_readable_project_id():
    from backend.server import generate_readable_project_id
    from datetime import datetime

    today_str = datetime.now().strftime("%Y-%m-%d")
    pid1 = generate_readable_project_id("15 хв.mp4")
    assert today_str in pid1
    assert "15_хв" in pid1

    pid2 = generate_readable_project_id("Podcast - Episode #42 & Special.mov")
    assert today_str in pid2
    assert "Podcast_Episode_42" in pid2

    pid3 = generate_readable_project_id("")
    assert today_str in pid3
    assert "video" in pid3


def test_extract_audio_slice(tmp_path):
    import subprocess
    from utils import extract_audio_slice

    src_audio = str(tmp_path / "test_src.mp3")
    out_audio = str(tmp_path / "test_slice.mp3")

    # Generate synthetic 5-second mp3 with lavfi sine
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "sine=frequency=1000:duration=5",
        "-c:a", "libmp3lame", "-b:a", "64k", src_audio
    ]
    subprocess.run(cmd, capture_output=True, check=True, timeout=30)
    assert os.path.exists(src_audio)

    res_path = extract_audio_slice(src_audio, out_audio, start_sec=1.0, duration_sec=2.0)
    assert res_path == out_audio
    assert os.path.exists(out_audio)
    assert os.path.getsize(out_audio) > 0


def test_delete_project_purges_physical_files(tmp_path):
    # Setup mock project and files
    pid = f"test_purge_{uuid.uuid4().hex[:8]}"
    mock_src = str(tmp_path / "unique_video_to_purge.mp4")
    with open(mock_src, "wb") as f:
        f.write(b"video_content")

    mock_out_dir = server.OUTPUT_DIR / pid
    mock_out_dir.mkdir(parents=True, exist_ok=True)
    mock_meta = mock_out_dir / "metadata.txt"
    with open(mock_meta, "w") as f:
        f.write("meta")

    db.create_project(pid, "unique_video.mp4", mock_src, 40.0)

    # Delete project
    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 200

    # Verify output dir is purged
    assert not mock_out_dir.exists()
    # Verify source file is purged because it's not referenced by other projects
    assert not os.path.exists(mock_src)
    # Verify DB record is gone
    assert db.get_project(pid) is None


def test_delete_project_preserves_shared_source_path(tmp_path):
    mock_src = str(tmp_path / "shared_video.mp4")
    with open(mock_src, "wb") as f:
        f.write(b"shared_content")

    pid1 = f"test_shared_1_{uuid.uuid4().hex[:8]}"
    pid2 = f"test_shared_2_{uuid.uuid4().hex[:8]}"

    db.create_project(pid1, "shared.mp4", mock_src, 50.0)
    db.create_project(pid2, "shared.mp4", mock_src, 50.0)

    # Delete pid1 only
    res = client.delete(f"/api/projects/{pid1}")
    assert res.status_code == 200

    # Source file must still exist because pid2 still references it
    assert os.path.exists(mock_src)

    # Clean up pid2
    client.delete(f"/api/projects/{pid2}")
    assert not os.path.exists(mock_src)


def test_media_refuses_a_video_outside_the_studio_folders(tmp_path):
    # A real .mp4 that is not in web_uploads/web_output: only the folder guard can stop it.
    outside = tmp_path / "someone_elses_video.mp4"
    outside.write_bytes(b"private video bytes")
    assert client.get("/api/media", params={"path": str(outside)}).status_code == 403
    assert client.get("/api/media", params={"path": str(server.UPLOADS_DIR / ".." / ".." / outside.name)}).status_code == 403
    inside = server.UPLOADS_DIR / "own_video.mp4"
    inside.write_bytes(b"own video bytes")
    assert client.get("/api/media", params={"path": str(inside)}).content == b"own video bytes"


def test_media_streaming_path_traversal_guard():
    # Attempt directory traversal outside web_uploads and web_output
    traversal_path = "web_uploads/../../Windows/System32/drivers/etc/hosts"
    res = client.get(f"/api/media?path={traversal_path}")
    assert res.status_code in (403, 404)


def test_analyze_request_validation():
    from backend.server import AnalyzeRequest
    from pydantic import ValidationError

    # Valid request (engine automatically normalizes to whisper_local)
    req1 = AnalyzeRequest(clips_count=25, engine="gemini_cloud")
    assert req1.clips_count == 25
    assert req1.engine == "whisper_local"

    # Any engine string standardizes on whisper_local
    req2 = AnalyzeRequest(clips_count=10, engine="unknown_engine")
    assert req2.engine == "whisper_local"

    # Invalid clips_count (> 100 or < 1) raises ValidationError
    with pytest.raises(ValidationError):
        AnalyzeRequest(clips_count=150)

    with pytest.raises(ValidationError):
        AnalyzeRequest(clips_count=-5)





def test_media_range_requests():
    server.UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    sample_file = str(server.UPLOADS_DIR / "sample_range_test.mp4")
    try:
        with open(sample_file, "wb") as f:
            f.write(b"0123456789abcdefghijklmnopqrstuvwxyz")

        # 1. Full content request (no Range header)
        res = client.get(f"/api/media?path={sample_file}")
        assert res.status_code == 200
        assert res.content == b"0123456789abcdefghijklmnopqrstuvwxyz"
        assert res.headers.get("x-content-type-options") == "nosniff"
        assert res.headers.get("content-security-policy") == "sandbox"

        # 2. Valid partial range: bytes 0-9
        res = client.get(f"/api/media?path={sample_file}", headers={"Range": "bytes=0-9"})
        assert res.status_code == 206
        assert res.content == b"0123456789"
        assert "bytes 0-9/36" in res.headers.get("Content-Range", "")
        assert res.headers.get("x-content-type-options") == "nosniff"

        # 3. Open-ended range: bytes 30-
        res = client.get(f"/api/media?path={sample_file}", headers={"Range": "bytes=30-"})
        assert res.status_code == 206
        assert res.content == b"uvwxyz"

        # 4. Invalid range: start > end
        res = client.get(f"/api/media?path={sample_file}", headers={"Range": "bytes=20-10"})
        assert res.status_code == 416
    finally:
        if os.path.exists(sample_file):
            os.remove(sample_file)


def test_download_clip_slice(tmp_path, monkeypatch):
    pid = f"test_clip_dl_{uuid.uuid4().hex[:8]}"
    mock_src = tmp_path / "source.mp4"
    mock_src.write_bytes(b"dummy_video_for_slicing")

    db.create_project(pid, "test_dl.mp4", str(mock_src), 60.0)
    db.save_candidate_clips(pid, [
        {
            "start": 10.0,
            "end": 25.0,
            "title": "Хайлайт для завантаження",
            "score": 90,
            "hook": "Хук",
            "reason": "Причина",
        }
    ])

    # Case 1: Clip slice does not exist, generated on-demand
    call_count = 0

    def mock_extract_slice(source_path, start_sec, end_sec, output_path):
        nonlocal call_count
        call_count += 1
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(b"sliced_mp4_content")
        return output_path

    monkeypatch.setattr("backend.server.extract_clip_slice", mock_extract_slice)

    res = client.get(f"/api/projects/{pid}/clips/1/download")
    assert res.status_code == 200
    assert res.headers["content-type"] == "video/mp4"
    assert res.content == b"sliced_mp4_content"
    assert call_count == 1

    # Case 2: Clip slice already cached (must not re-extract)
    res2 = client.get(f"/api/projects/{pid}/clips/1/download")
    assert res2.status_code == 200
    assert res2.content == b"sliced_mp4_content"
    assert call_count == 1

    # Case 3: Invalid clip index (e.g. index 99)
    res_404 = client.get(f"/api/projects/{pid}/clips/99/download")
    assert res_404.status_code == 404


def test_upload_from_url_validation_and_errors(monkeypatch):
    # Case 1: Empty URL
    res = client.post("/api/upload-url", json={"url": ""})
    assert res.status_code == 400

    # Case 2: Invalid scheme
    res = client.post("/api/upload-url", json={"url": "ftp://example.com/video.mp4"})
    assert res.status_code == 400

    # Case 3: Download failure
    def mock_dl_fail(url, output_dir):
        raise RuntimeError("Network error or invalid video link")

    monkeypatch.setattr("backend.server.download_from_url_with_title", mock_dl_fail)
    res = client.post("/api/upload-url", json={"url": "https://youtube.com/watch?v=invalid123"})
    assert res.status_code == 500
    assert "Помилка завантаження" in res.json().get("detail", "")


def test_stream_job_progress_sse():
    # 1. Job not found -> 404
    res_404 = client.get("/api/jobs/missing_job_123/stream")
    assert res_404.status_code == 404

    # 2. Existing completed job -> returns initial data event
    pid = f"test_sse_{uuid.uuid4().hex[:8]}"
    jid = f"job_sse_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "sse.mp4", "sse.mp4", 45.0)
    db.create_job(jid, pid, "analyze")
    db.update_job(jid, status="completed", progress=1.0, message="Готово")

    with client.stream("GET", f"/api/jobs/{jid}/stream") as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]
        assert len(lines) >= 1
        data = json.loads(lines[0][6:])
        assert data["status"] == "completed"
        assert data["progress"] == 1.0


def test_get_project_details_formatted_clips():
    pid = f"test_proj_fmt_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "video_fmt.mp4", "video_fmt.mp4", 95.0)
    db.save_candidate_clips(pid, [
        {
            "start": 15.0,
            "end": 45.0,
            "title": "Хайлайт з таймкодом",
            "score": 90,
            "hook": "Хук",
            "reason": "Причина",
        }
    ])

    res = client.get(f"/api/projects/{pid}")
    assert res.status_code == 200
    data = res.json()
    assert data["project"]["id"] == pid
    assert len(data["clips"]) == 1
    clip = data["clips"][0]
    assert clip["timecode_formatted"] == "00:15 — 00:45"
    assert clip["duration_formatted"] == "30 сек"


def test_media_non_existent_file_returns_404():
    missing_path = str(server.UPLOADS_DIR / "totally_missing_file_abc.mp4")
    res = client.get(f"/api/media?path={missing_path}")
    assert res.status_code == 404


def test_upload_rejects_disallowed_extensions_and_empty_files(monkeypatch):
    # SEC-01: Disallowed HTML file (prevents Stored XSS)
    res_html = client.post("/api/upload", files={"file": ("evil.html", b"<script>alert(1)</script>", "text/html")})
    assert res_html.status_code == 400

    # SEC-01: Disallowed executable
    res_exe = client.post("/api/upload", files={"file": ("malware.exe", b"MZ...", "application/octet-stream")})
    assert res_exe.status_code == 400

    # Empty MP4 file
    res_empty = client.post("/api/upload", files={"file": ("empty.mp4", b"", "video/mp4")})
    assert res_empty.status_code == 400

    # Exceeding MAX_UPLOAD_BYTES -> 413
    monkeypatch.setattr(server, "MAX_UPLOAD_BYTES", 10)
    res_big = client.post("/api/upload", files={"file": ("too_big.mp4", b"0123456789ABCDEF", "video/mp4")})
    assert res_big.status_code == 413


def test_media_rejects_non_media_extensions():
    # SEC-01: Even if an HTML file somehow exists in uploads, /api/media must refuse to serve it
    html_file = server.UPLOADS_DIR / "injected.html"
    html_file.write_text("<h1>XSS</h1>", encoding="utf-8")
    res = client.get(f"/api/media?path={html_file}")
    assert res.status_code == 403


def test_project_id_traversal_rejected():
    # SEC-02: Invalid project_id containing '..' or unsafe characters must return 400
    res_del = client.delete("/api/projects/..bad_id")
    assert res_del.status_code == 400

    res_txt = client.get("/api/projects/..secret/export-txt")
    assert res_txt.status_code == 400

    res_thumb = client.get("/api/projects/bad%20id!$/thumbnail")
    assert res_thumb.status_code == 400


def test_cors_restricts_untrusted_origins():
    # SEC-03: Untrusted external origin must not receive Access-Control-Allow-Origin
    res_evil = client.options(
        "/api/projects",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert res_evil.headers.get("access-control-allow-origin") is None

    # Local studio origin must be allowed
    res_local = client.options(
        "/api/projects",
        headers={
            "Origin": "http://127.0.0.1:8000",
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert res_local.headers.get("access-control-allow-origin") == "http://127.0.0.1:8000"


def test_analyze_request_resolves_filter_mode():
    # BUG-02: UI string aliases must resolve to canonical FilterMode values
    from constants import FilterMode
    from backend.server import AnalyzeRequest

    assert AnalyzeRequest(filter_mode="Viral Hooks").filter_mode == FilterMode.VIRAL_HOOKS.value
    assert AnalyzeRequest(filter_mode="Actionable Advice").filter_mode == FilterMode.ACTIONABLE_ADVICE.value
    assert AnalyzeRequest(filter_mode="Storytelling").filter_mode == FilterMode.STORYTELLING.value


def test_health_uses_cached_app_state_gpu_capabilities(monkeypatch):
    """PERF-01: /api/health must use app_state.get_gpu_capabilities(), not raw subprocess probing."""
    from utils import GpuCapabilities

    mock_get = MagicMock(return_value=GpuCapabilities(has_nvenc=True, has_cuda=True, nvenc_device_name="Mock RTX"))
    monkeypatch.setattr(server.app_state, "get_gpu_capabilities", mock_get)

    res1 = client.get("/api/health")
    res2 = client.get("/api/health")
    assert res1.status_code == 200
    assert res2.status_code == 200
    assert res1.json()["gpu_name"] == "Mock RTX"
    assert mock_get.call_count == 2


def test_per_path_slice_locks_do_not_block_unrelated_clips(tmp_path):
    """PERF-02: Distinct output paths must receive distinct locks, while identical paths share one."""
    p1 = str(tmp_path / "clip_1.mp4")
    p2 = str(tmp_path / "clip_2.mp4")
    lock1_a = server._get_slice_path_lock(p1)
    lock1_b = server._get_slice_path_lock(p1)
    lock2 = server._get_slice_path_lock(p2)

    assert lock1_a is lock1_b
    assert lock1_a is not lock2


def test_db_create_project_upsert_preserves_clips_and_safe_scores():
    """DB-01: Re-saving an existing project_id via UPSERT must NOT cascade-delete candidate_clips."""
    pid = f"test_upsert_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "orig.mp4", "orig.mp4", 100.0)
    db.save_candidate_clips(pid, [
        {"start": 5.0, "end": 30.0, "title": "Clip 1", "score": "92/100"},
        {"start": 40.0, "end": 65.0, "title": "Clip 2", "score": None},
    ])

    # Re-create/update same project_id
    updated = db.create_project(pid, "renamed.mp4", "orig.mp4", 105.0)
    assert updated["title"] == "renamed.mp4"
    assert updated["duration"] == 105.0

    # Clips must remain intact (no CASCADE wipe) with defensively parsed scores
    clips = db.get_candidate_clips(pid)
    assert len(clips) == 2
    assert clips[0]["virality_score"] == 92
    assert clips[1]["virality_score"] == 85


def test_upload_url_success_and_deduplication(monkeypatch):
    """Covers /api/upload-url happy path and duplicate reuse."""
    payload_bytes = b"UNIQUE_URL_VIDEO_BYTES" * 500

    def fake_dl(url, output_dir):
        os.makedirs(output_dir, exist_ok=True)
        dest = os.path.join(output_dir, "yt_clip.mp4")
        with open(dest, "wb") as f:
            f.write(payload_bytes)
        return dest, "Справжня назва з YouTube"

    monkeypatch.setattr("backend.server.download_from_url_with_title", fake_dl)
    monkeypatch.setattr("backend.server.get_video_duration", lambda p: 90.0)

    res1 = client.post("/api/upload-url", json={"url": "https://youtube.com/watch?v=abc123"})
    assert res1.status_code == 200
    proj1 = res1.json()["project"]
    assert proj1["duration"] == 90.0
    assert proj1["title"] == "Справжня назва з YouTube"

    # Uploading same content again reuses existing project
    res2 = client.post("/api/upload-url", json={"url": "https://youtube.com/watch?v=abc123"})
    assert res2.status_code == 200
    assert res2.json().get("reused") is True
    assert res2.json()["project"]["id"] == proj1["id"]


def test_export_missing_metadata_and_index_route():
    # GET / serves frontend index.html
    res_idx = client.get("/")
    assert res_idx.status_code == 200
    assert "Студія моментів" in res_idx.text

    # Existing project without generated metadata returns 404 on export
    pid = f"test_no_meta_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "nometa.mp4", "nometa.mp4", 30.0)
    assert client.get(f"/api/projects/{pid}/export-txt").status_code == 404
    assert client.get(f"/api/projects/{pid}/export-json").status_code == 404
    assert client.get("/api/projects/missing_proj_xyz/export-txt").status_code == 404
    assert client.get("/api/projects/missing_proj_xyz/export-json").status_code == 404


def test_delete_project_also_purges_temp_audio():
    pid = f"test_del_audio_{uuid.uuid4().hex[:8]}"
    temp_audio = server.UPLOADS_DIR / f"audio_{pid}.mp3"
    temp_audio.write_bytes(b"temp_mp3")
    db.create_project(pid, "vid.mp4", str(server.UPLOADS_DIR / "missing.mp4"), 20.0)

    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 200
    assert not temp_audio.exists()


def test_export_clip_srt_endpoint(tmp_path):
    pid = f"test_srt_{uuid.uuid4().hex[:8]}"
    vid_file = tmp_path / "vid.mp4"
    vid_file.write_bytes(b"dummy")
    db.create_project(pid, "vid.mp4", str(vid_file), 120.0)
    db.save_candidate_clips(pid, [
        {"start": 10.0, "end": 40.0, "title": "SRT Кліп 1", "score": 90}
    ])

    # 404 on missing project or clip
    assert client.get(f"/api/projects/missing_proj/clips/1/export-srt").status_code == 404
    assert client.get(f"/api/projects/{pid}/clips/99/export-srt").status_code == 404

    # 200 success
    res = client.get(f"/api/projects/{pid}/clips/1/export-srt")
    assert res.status_code == 200
    assert "00:00:00,000" in res.text
    assert "attachment" in res.headers.get("content-disposition", "")


def test_export_all_subtitles_endpoint(tmp_path):
    pid = f"test_zip_{uuid.uuid4().hex[:8]}"
    vid_file = tmp_path / "vid.mp4"
    vid_file.write_bytes(b"dummy")
    db.create_project(pid, "vid.mp4", str(vid_file), 120.0)

    # 404 when no clips
    assert client.get(f"/api/projects/{pid}/export-all-subtitles").status_code == 404

    db.save_candidate_clips(pid, [
        {"start": 5.0, "end": 25.0, "title": "Clip A", "score": 85},
        {"start": 30.0, "end": 60.0, "title": "Clip B", "score": 92},
    ])

    res = client.get(f"/api/projects/{pid}/export-all-subtitles")
    assert res.status_code == 200
    assert res.headers.get("content-type") == "application/zip"


def test_trim_clip_endpoint_success_and_validation(tmp_path):
    pid = f"test_trim_{uuid.uuid4().hex[:8]}"
    vid_file = tmp_path / "vid.mp4"
    vid_file.write_bytes(b"dummy")
    db.create_project(pid, "vid.mp4", str(vid_file), 100.0)
    db.save_candidate_clips(pid, [
        {"start": 10.0, "end": 40.0, "title": "Original Clip", "score": 88}
    ])

    # Invalid payloads
    res_inv1 = client.put(f"/api/projects/{pid}/clips/1/trim", json={"start_sec": 30.0, "end_sec": 20.0})
    assert res_inv1.status_code == 422

    res_inv2 = client.put(f"/api/projects/{pid}/clips/1/trim", json={"start_sec": 10.0, "end_sec": 12.0})
    assert res_inv2.status_code == 422

    res_inv3 = client.put(f"/api/projects/{pid}/clips/1/trim", json={"start_sec": 10.0, "end_sec": 150.0})
    assert res_inv3.status_code == 400

    # 404 on missing clip
    res_404 = client.put(f"/api/projects/{pid}/clips/99/trim", json={"start_sec": 10.0, "end_sec": 30.0})
    assert res_404.status_code == 404

    # Pre-create cached slice, subtitle, and metadata files to verify cache invalidation
    p_out = server._safe_project_output_dir(pid)
    slices_dir = p_out / "slices"
    slices_dir.mkdir(parents=True, exist_ok=True)
    old_slice = slices_dir / "clip_1_old.mp4"
    old_slice.write_bytes(b"old_slice")

    subs_dir = p_out / "subtitles"
    subs_dir.mkdir(parents=True, exist_ok=True)
    old_sub = subs_dir / "clip_1_old.srt"
    old_sub.write_bytes(b"old_sub")

    meta_json = p_out / "metadata.json"
    meta_json.write_text("[]", encoding="utf-8")

    # Valid trim
    res_ok = client.put(f"/api/projects/{pid}/clips/1/trim", json={"start_sec": 12.5, "end_sec": 38.5})
    assert res_ok.status_code == 200
    assert not old_slice.exists()
    assert not old_sub.exists()
    clip_data = res_ok.json()["clip"]
    assert clip_data["start_sec"] == 12.5
    assert clip_data["end_sec"] == 38.5
    assert clip_data["duration"] == 26.0
    assert clip_data["timecode_formatted"] == "00:12 - 00:38"

    # Verify DB state
    db_clips = db.get_candidate_clips(pid)
    assert db_clips[0]["start_sec"] == 12.5
    assert db_clips[0]["end_sec"] == 38.5
    assert db_clips[0]["duration"] == 26.0


def test_fail_interrupted_jobs_marks_stale_work_failed():
    proj_id = f"test_stale_{uuid.uuid4().hex[:8]}"
    job_id = f"job_stale_{uuid.uuid4().hex[:8]}"
    db.create_project(proj_id, "stale.mp4", "stale.mp4", 60.0)
    db.update_project_status(proj_id, "analyzing")
    db.create_job(job_id, proj_id, "analyze")
    db.update_job(job_id, status="running", progress=0.4, message="...")

    assert db.fail_interrupted_jobs() >= 1
    assert db.get_job(job_id)["status"] == "failed"
    assert db.get_project(proj_id)["status"] == "failed"


# ─── Монтажний стіл: review status, post texts, words, ZIP ───────────────────

def _make_review_project(tmp_path):
    pid = f"test_review_{uuid.uuid4().hex[:8]}"
    src = tmp_path / "source.mp4"
    src.write_bytes(b"dummy")
    db.create_project(pid, "Подкаст", str(src), 120.0)
    db.save_candidate_clips(pid, [
        {"start": 10.0, "end": 30.0, "title": "Перший", "score": 70, "caption": "о1", "hashtags": ["#a"]},
        {"start": 40.0, "end": 62.0, "title": "Другий", "score": 95, "caption": "о2", "hashtags": ["#b"]},
    ])
    return pid, src


def test_db_refuses_an_unknown_clip_status(tmp_path):
    pid, _ = _make_review_project(tmp_path)
    with pytest.raises(ValueError):
        db.update_clip_fields(pid, 1, review_status="maybe")
    assert db.get_candidate_clips(pid)[0]["review_status"] == ""


def test_clip_review_status_and_texts(tmp_path):
    pid, _ = _make_review_project(tmp_path)
    assert all(c["review_status"] == "" for c in db.get_candidate_clips(pid))

    res = client.patch(f"/api/projects/{pid}/clips/2", json={"review_status": "keep", "title": "Новий", "hashtags": ["#x", "#y"]})
    assert res.status_code == 200
    c2 = next(c for c in db.get_candidate_clips(pid) if c["clip_index"] == 2)
    assert (c2["review_status"], c2["title"], c2["hashtags"]) == ("keep", "Новий", ["#x", "#y"])

    assert client.patch(f"/api/projects/{pid}/clips/2", json={"review_status": "maybe"}).status_code == 422
    assert client.patch(f"/api/projects/{pid}/clips/2", json={"title": "   "}).status_code == 400
    assert client.patch(f"/api/projects/{pid}/clips/99", json={"review_status": "drop"}).status_code == 404
    # Undo the mark
    assert client.patch(f"/api/projects/{pid}/clips/2", json={"review_status": ""}).status_code == 200
    assert next(c for c in db.get_candidate_clips(pid) if c["clip_index"] == 2)["review_status"] == ""


def test_clip_words_endpoint(tmp_path, monkeypatch):
    pid, _ = _make_review_project(tmp_path)
    words = [{"start": float(t), "end": float(t) + 0.4, "word": f"w{t}"} for t in range(0, 120, 2)]
    monkeypatch.setattr("backend.server.load_transcript_cache", lambda p, **kw: ([], words, "uk"))
    data = client.get(f"/api/projects/{pid}/clips/1/words?pad=4").json()
    assert data["available"] is True
    assert data["words"][0]["start"] == 6.0 and data["words"][-1]["start"] == 34.0

    monkeypatch.setattr("backend.server.load_transcript_cache", lambda p, **kw: None)
    assert client.get(f"/api/projects/{pid}/clips/1/words").json() == {"words": [], "available": False}


def test_export_zip_of_kept_clips(tmp_path, monkeypatch):
    import io
    import zipfile
    pid, _ = _make_review_project(tmp_path)

    def fake_slice(source_path, start_sec, end_sec, output_path):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Path(output_path).write_bytes(b"mp4")
        return output_path

    monkeypatch.setattr("backend.server.extract_clip_slice", fake_slice)
    monkeypatch.setattr("backend.server.subtitles_generator.generate_srt_for_clip", lambda **kw: "1\n00:00:00,000 --> 00:00:01,000\nпривіт\n")

    # Nothing kept yet → clear error
    assert client.get(f"/api/projects/{pid}/export-zip").status_code == 400

    for idx in (1, 2):
        client.patch(f"/api/projects/{pid}/clips/{idx}", json={"review_status": "keep"})
    res = client.get(f"/api/projects/{pid}/export-zip")
    assert res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert sum(n.endswith(".mp4") for n in names) == 2
    assert sum(n.endswith(".srt") for n in names) == 2
    posts = zf.read("posts.txt").decode("utf-8")
    # Sorted by score: «Другий» (95) comes first; numbers are the studio's №N (by start time)
    assert posts.index("№2. Другий") < posts.index("№1. Перший")
    assert sorted(n for n in names if n.endswith(".mp4")) == ["01 Перший.mp4", "02 Другий.mp4"]

    # Explicit list overrides marks; bad list is rejected
    one = zipfile.ZipFile(io.BytesIO(client.get(f"/api/projects/{pid}/export-zip?clips=1").content))
    assert sum(n.endswith(".mp4") for n in one.namelist()) == 1
    assert client.get(f"/api/projects/{pid}/export-zip?clips=a,b").status_code == 400


# ─── Clip statuses (todo-like) and «Підрізати тишу» ──────────────────────────

def test_done_and_drop_free_rendered_files_but_keep_clip(tmp_path):
    pid, _ = _make_review_project(tmp_path)
    out = server.OUTPUT_DIR / pid
    files = {
        "slices/clip_2_40s_x.mp4": 1000, "subtitles/clip_2_40s_x.srt": 10,
        "vertical/clip_2_abc_s.mp4": 3000, "vertical/faces2_2_40.00_62.00.json": 5,
        "slices/clip_1_10s_y.mp4": 700, "slices/clip_22_10s_z.mp4": 50,
    }
    for rel, size in files.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_bytes(b"x" * size)
    try:
        res = client.patch(f"/api/projects/{pid}/clips/2", json={"review_status": "keep"})
        assert res.json()["freed_bytes"] == 0 and (out / "slices/clip_2_40s_x.mp4").exists()

        res = client.patch(f"/api/projects/{pid}/clips/2", json={"review_status": "done"})
        assert res.status_code == 200
        assert res.json()["freed_bytes"] == 4010
        assert not (out / "slices/clip_2_40s_x.mp4").exists()
        assert not (out / "vertical/clip_2_abc_s.mp4").exists()
        # Other clips and the face cache stay; the clip itself keeps bounds and texts
        assert (out / "slices/clip_1_10s_y.mp4").exists() and (out / "slices/clip_22_10s_z.mp4").exists()
        assert (out / "vertical/faces2_2_40.00_62.00.json").exists()
        c2 = next(c for c in db.get_candidate_clips(pid) if c["clip_index"] == 2)
        assert (c2["review_status"], c2["start_sec"], c2["title"]) == ("done", 40.0, "Другий")

        res = client.patch(f"/api/projects/{pid}/clips/1", json={"review_status": "drop"})
        assert res.json()["freed_bytes"] == 700
        proj = next(p for p in db.list_recent_projects(limit=500, include_test=True) if p["id"] == pid)
        assert proj["done_count"] == 1
    finally:
        shutil.rmtree(out, ignore_errors=True)


def test_trim_silence_bounds():
    from clip_refiner import trim_silence
    words = [{"start": 12.0, "end": 12.4, "word": "Привіт"}, {"start": 20.0, "end": 27.5, "word": "друзі."}]
    assert trim_silence(words, 10.0, 30.0) == (11.85, 27.85)
    # Never extends the clip, and a word cut by the bound keeps the bound
    assert trim_silence([{"start": 9.5, "end": 10.3, "word": "а"}, {"start": 29.8, "end": 30.6, "word": "б"}], 10.0, 30.0) is None
    assert trim_silence([], 10.0, 30.0) is None
    # Would leave less than 5 s of clip
    assert trim_silence([{"start": 15.0, "end": 16.0, "word": "так"}], 10.0, 30.0) is None


def test_trim_silence_endpoint(tmp_path, monkeypatch):
    pid, _ = _make_review_project(tmp_path)
    words = [{"start": 13.0, "end": 13.5, "word": "Ну"}, {"start": 24.0, "end": 25.0, "word": "все."}]
    monkeypatch.setattr("backend.server.load_transcript_cache", lambda p, **kw: ([], words, "uk"))
    data = client.post(f"/api/projects/{pid}/clips/1/trim-silence").json()
    assert data["status"] == "updated"
    assert (data["clip"]["start_sec"], data["clip"]["end_sec"]) == (12.85, 25.35)
    assert data["trimmed_sec"] == round(2.85 + 4.65, 2)
    # Second press: nothing left to trim
    assert client.post(f"/api/projects/{pid}/clips/1/trim-silence").json()["status"] == "unchanged"

    monkeypatch.setattr("backend.server.load_transcript_cache", lambda p, **kw: None)
    assert client.post(f"/api/projects/{pid}/clips/1/trim-silence").status_code == 409
    assert client.post(f"/api/projects/{pid}/clips/99/trim-silence").status_code == 404


def test_download_names_use_the_studio_clip_number(tmp_path, monkeypatch):
    """Files are named «06 Title.mp4» with the same №N the studio shows (place by start time), not the DB index."""
    from urllib.parse import unquote
    pid = f"test_names_{uuid.uuid4().hex[:8]}"
    src = tmp_path / "source.mp4"
    src.write_bytes(b"dummy")
    db.create_project(pid, "names.mp4", str(src), 300.0)
    db.save_candidate_clips(pid, [
        {"start": 200.0, "end": 230.0, "title": "Пізній: кліп?", "score": 95},
        {"start": 10.0, "end": 40.0, "title": "Ранній кліп", "score": 70},
        {"start": 100.0, "end": 130.0, "title": "Середній", "score": 80},
    ])

    def fake_slice(source_path, start_sec, end_sec, output_path):
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Path(output_path).write_bytes(b"x")
        return output_path

    monkeypatch.setattr("backend.server.extract_clip_slice", fake_slice)
    late = next(c for c in db.get_candidate_clips(pid) if c["start_sec"] == 200.0)

    def name(res):
        cd = res.headers["content-disposition"]
        return unquote(cd.split("filename*=utf-8''")[1]) if "filename*=" in cd else cd.split('filename="')[1].rstrip('"')

    res = client.get(f"/api/projects/{pid}/clips/{late['clip_index']}/download")
    assert res.status_code == 200
    assert name(res) == "03 Пізній кліп.mp4"  # third by time; characters Windows forbids are dropped
    srt = client.get(f"/api/projects/{pid}/clips/{late['clip_index']}/export-srt")
    assert name(srt) == "03 Пізній кліп.srt"

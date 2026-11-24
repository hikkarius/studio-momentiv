"""
tests/conftest.py — Global Pytest configuration and fixtures.
Ensures test execution uses an isolated SQLite database and isolated upload/output
directories so tests never pollute the user's production studio_data.db,
web_uploads/, or web_output/.
"""
import os
import tempfile
from pathlib import Path

# Redirect application logging away from the real shorts_automator.log
# BEFORE any app module (core/logger) is imported and configures handlers.
os.environ["SHORTS_LOG_FILE"] = os.path.join(tempfile.gettempdir(), "shorts_automator_pytest.log")

import pytest
from backend import db, job_runner, server


@pytest.fixture(autouse=True)
def isolate_test_database(tmp_path, monkeypatch):
    """
    Autouse fixture that runs for every test.
    Redirects backend.db.DB_PATH and upload/output storage roots into tmp_path.
    After the test finishes, all temporary artifacts are cleaned up automatically.
    """
    isolated_root = tmp_path / "_isolated_studio"
    test_db = str(isolated_root / "test_studio_isolated.db")
    test_uploads = isolated_root / "web_uploads"
    test_output = isolated_root / "web_output"
    test_uploads.mkdir(parents=True, exist_ok=True)
    test_output.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(db, "DB_PATH", test_db)
    monkeypatch.setattr(db, "SKIP_MISSING_SOURCE_FILES", False)
    monkeypatch.setattr(server, "UPLOADS_DIR", test_uploads)
    monkeypatch.setattr(server, "OUTPUT_DIR", test_output)
    monkeypatch.setattr(job_runner, "UPLOADS_DIR", str(test_uploads))
    monkeypatch.setattr(job_runner, "OUTPUT_DIR", str(test_output))

    # Settings changed through the «Налаштування» screen go to a temp file, and the live
    # config is restored afterwards, so one test cannot change another's clip lengths.
    import config_loader
    monkeypatch.setattr(config_loader, "USER_SETTINGS_PATH", isolated_root / "user_settings.json")
    monkeypatch.setattr(config_loader, "ENV_PATH", isolated_root / ".env")
    saved_cfg = config_loader.app_config.model_copy(deep=True)

    # Whisper transcripts and render temp files also go to tmp_path. Without this the disk
    # cleanup («Автоприбирання», «Прибрати сміття») treats every real transcript in
    # .transcript_cache as an orphan of the test database and deletes it. The folder is
    # replaced at the function, not in CONFIG: saving settings rebuilds CONFIG from config.yaml.
    # A test may still point CONFIG at its own absolute folder; the relative default never counts.
    import transcription
    from backend import storage
    safe_cache = isolated_root / "transcript_cache"

    def _test_cache_dir():
        d = Path(config_loader.CONFIG["cache"]["dir"])
        return d if d.is_absolute() and d.resolve().is_relative_to(tmp_path.resolve()) else safe_cache

    monkeypatch.setattr(transcription, "transcript_cache_dir", _test_cache_dir)
    monkeypatch.setattr(storage, "_RENDER_TMP", isolated_root / "render_tmp")
    # Never ask a real site for the video size from tests (no network); tests that need a size patch it
    monkeypatch.setattr(server, "probe_download_size", lambda url, **kw: None)
    # Nor download a link's audio track (the queue then falls back to the video-only path)
    import utils

    def _no_audio_download(url, output_dir, **kw):
        raise RuntimeError("no network in tests")

    monkeypatch.setattr(utils, "download_audio_from_url", _no_audio_download)

    # Daily database copies go to tmp_path too
    from backend import backup
    monkeypatch.setattr(backup, "BACKUP_DIR", isolated_root / "backups")
    monkeypatch.setattr(backup, "start", lambda: None)  # no background copies while tests run
    # Never load the real Whisper model in the background while tests run
    monkeypatch.setattr(server, "_start_whisper_housekeeping", lambda: None)
    from state import app_state as _app_state
    monkeypatch.setattr(_app_state, "preload_whisper_async", lambda: None)
    # Gemini's second round for unanswered windows waits 90 s in real life; not in tests
    import ai_selector
    monkeypatch.setattr(ai_selector, "_SECOND_ROUND_WAIT", 0.0)

    db.init_db()
    yield test_db
    config_loader._assign_in_place(config_loader.app_config, saved_cfg)

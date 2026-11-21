"""
tests/conftest.py — Global Pytest configuration and fixtures.
Ensures test execution uses an isolated SQLite database and isolated upload/output
directories so tests never pollute the user's production studio_data.db,
web_uploads/, or web_output/.
"""
import os
import tempfile

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

    db.init_db()
    yield test_db
    config_loader._assign_in_place(config_loader.app_config, saved_cfg)

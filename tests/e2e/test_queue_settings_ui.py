"""
tests/e2e/test_queue_settings_ui.py — the processing queue on the home page and the «Налаштування» sheet.
The queue worker is not running in e2e (lifespan off), so tests drive it with queue_runner.process_next().
"""
import pytest

import config_loader
from backend import db, job_runner, queue_runner, server

from .conftest import PROJECT_ID


@pytest.fixture(autouse=True)
def clean_queue_and_settings(tmp_path):
    saved_cfg = config_loader.app_config.model_copy(deep=True)
    paths = (config_loader.USER_SETTINGS_PATH, config_loader.ENV_PATH)
    config_loader.USER_SETTINGS_PATH = tmp_path / "user_settings.json"
    config_loader.ENV_PATH = tmp_path / ".env"
    for i in db.list_queue_items():
        db.delete_queue_item(i["id"])
    yield
    for i in db.list_queue_items():
        db.delete_queue_item(i["id"])
    config_loader.USER_SETTINGS_PATH, config_loader.ENV_PATH = paths
    config_loader._assign_in_place(config_loader.app_config, saved_cfg)


def _fake_analyze(job_id, project_id, clips_count, filter_mode, engine="whisper_local"):
    db.update_project_status(project_id, "ready")
    job_runner.broadcast_update(job_id, {"status": "completed", "progress": 1.0, "message": "Готово!"})


def test_paste_several_links_and_manage_queue(page, studio, project):
    page.goto(studio["url"] + "/")
    page.fill("#urlInput", "https://youtu.be/one\nhttps://youtu.be/twohttps://youtu.be/three")
    page.select_option("#heroClips", "20")
    page.click("#urlDownloadBtn")
    rows = page.locator("#queueList .q-item")
    rows.nth(2).wait_for()
    assert rows.count() == 3
    assert [i["clips_count"] for i in db.list_queue_items()] == [20, 20, 20]
    assert "youtu.be/one" in rows.nth(0).inner_text()

    rows.nth(2).locator("[data-move$=':-1']").click()
    page.wait_for_function("document.querySelectorAll('#queueList .q-item')[1].innerText.includes('three')")
    rows.nth(0).locator("[data-remove]").click()
    page.wait_for_function("document.querySelectorAll('#queueList .q-item').length === 2")
    assert not page.errors


def test_single_link_opens_clips_when_done(page, studio, project, monkeypatch):
    page.goto(studio["url"] + "/")
    page.fill("#urlInput", "https://youtu.be/podcast")
    page.keyboard.press("Enter")
    page.locator("#queueList .q-item").first.wait_for()

    monkeypatch.setattr(server, "ingest_url", lambda url, progress_cb=None: (db.get_project(PROJECT_ID), False))
    monkeypatch.setattr(job_runner, "_run_analyze_worker", _fake_analyze)
    assert queue_runner.process_next()["status"] == "done"
    page.locator("#editor").wait_for(state="visible", timeout=8000)
    assert not page.errors


def test_failed_item_shows_error_and_retry(page, studio, project, monkeypatch):
    db.add_queue_item("q_fail", source_url="https://youtu.be/x", title="https://youtu.be/x")
    db.update_queue_item("q_fail", status="failed", error="Не вдалося завантажити відео: private")
    page.goto(studio["url"] + "/")
    row = page.locator("#queueList .q-item.failed")
    row.wait_for()
    assert "private" in row.inner_text()
    row.locator("[data-retry]").click()
    page.locator("#queueList .q-item.waiting").wait_for()


def test_settings_change_applies_to_home(page, studio, project):
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.fill("[data-key='clips.default_max_len']", "45")
    page.press("[data-key='clips.default_max_len']", "Tab")
    page.wait_for_function("document.getElementById('heroHint').textContent.includes('15–45')")
    assert config_loader.app_config.clips.default_max_len == 45

    page.fill("[data-key='clips.default_min_len']", "50")
    page.press("[data-key='clips.default_min_len']", "Tab")
    page.wait_for_function("document.getElementById('settingsSaved').textContent.includes('меншою')")
    assert page.input_value("[data-key='clips.default_min_len']") == "15"

    page.select_option("[data-key='studio.default_clips']", "30")
    page.wait_for_function("document.getElementById('heroClips').value === '30'")
    page.keyboard.press("Escape")
    assert page.locator("#settingsScreen").is_hidden()
    assert not page.errors


def test_disk_tab_shows_sizes_and_cleans_clip_files(page, studio, project):
    d = server.OUTPUT_DIR / PROJECT_ID / "slices"
    d.mkdir(parents=True, exist_ok=True)
    (d / "c.mp4").write_bytes(b"\0" * 3_000_000)
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.click(".settings-tabs [data-tab=disk]")
    row = page.locator("#diskProjects li", has_text="Тестовий подкаст")
    row.wait_for()
    assert "3 МБ" in row.inner_text()
    row.locator("[data-clean]").click()
    page.wait_for_function("!document.querySelector('#diskProjects [data-clean]')")
    assert not d.exists()
    assert not page.errors


def test_first_run_asks_for_gemini_key(page, studio, monkeypatch, tmp_path):
    import os
    import config_loader
    from backend import app_update
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config_loader, "ENV_PATH", tmp_path / ".env")  # never touch the real .env
    monkeypatch.setattr(config_loader, "ensure_gemini_api_key", lambda: os.environ.get("GEMINI_API_KEY"))
    monkeypatch.setattr(app_update, "check_gemini_key", lambda k: {"ok": True, "message": "Ключ працює."})
    page.goto(studio["url"] + "/")
    page.locator("#welcomeScreen").wait_for(state="visible")
    page.fill("#welcomeKey", "AIza" + "k" * 35)
    page.click("#welcomeSave")
    page.locator("#welcomeMsg.ok").wait_for()
    page.locator("#welcomeScreen").wait_for(state="hidden")
    assert os.environ["GEMINI_API_KEY"] == "AIza" + "k" * 35
    assert (tmp_path / ".env").read_text(encoding="utf-8").strip().endswith("k" * 35)

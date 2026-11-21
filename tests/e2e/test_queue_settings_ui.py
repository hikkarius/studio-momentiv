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
    page.click("#homeOptsEdit")  # the search options open under «Шукаю: … Змінити»
    page.select_option("#heroClips", "20")
    assert "кліпів: 20" in page.locator("#heroHint").inner_text()
    page.click("#urlDownloadBtn")
    rows = page.locator("#queueList .q-item")
    rows.nth(2).wait_for()
    assert rows.count() == 3
    assert [i["clips_count"] for i in db.list_queue_items()] == [20, 20, 20]
    assert "youtu.be/one" in rows.nth(0).inner_text()

    # queue order lives in the card's «⋯» menu
    assert rows.nth(2).locator(".vc-menu").is_hidden()
    rows.nth(2).hover()
    rows.nth(2).locator(".vc-dots").click()
    rows.nth(2).locator("[data-move$=':-1']").click()
    page.wait_for_function("document.querySelectorAll('#queueList .q-item')[1].innerText.includes('three')")
    rows.nth(0).hover()
    rows.nth(0).locator(".vc-dots").click()
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


def _set_range(page, key, value):
    page.evaluate(
        """([k, v]) => { const el = document.querySelector(`[data-key='${k}']`); el.value = v;
             el.dispatchEvent(new Event('input')); el.dispatchEvent(new Event('change')); }""",
        [key, str(value)],
    )


def test_settings_change_applies_to_home(page, studio, project):
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.wait_for_function("document.getElementById('setLenBig').textContent.includes('15–60')")
    _set_range(page, "clips.default_max_len", 45)
    page.wait_for_function("document.getElementById('heroHint').textContent.includes('15–45')")
    assert config_loader.app_config.clips.default_max_len == 45
    assert "15–45 с" in page.locator("#setLenBig").inner_text()

    # presets set both ends at once
    page.click("#setLenPresets button[data-max='90']")
    page.wait_for_function("document.getElementById('heroHint').textContent.includes('30–90')")
    assert (config_loader.app_config.clips.default_min_len, config_loader.app_config.clips.default_max_len) == (30, 90)
    assert page.locator("#setLenPresets button[data-max='90']").get_attribute("aria-pressed") == "true"
    page.click("#setLenPresets button[data-max='60']")
    page.wait_for_function("document.getElementById('setLenBig').textContent.includes('15–60')")

    # a minimum that is not below the maximum is refused and the slider goes back
    page.evaluate("""fetch('/api/settings', {method: 'POST', headers: {'Content-Type': 'application/json'},
                     body: JSON.stringify({clips: {default_min_len: 70}})}).then(r => r.status)""")
    assert config_loader.app_config.clips.default_min_len == 15

    page.click("[data-key='studio.default_clips'] [data-v='20']")
    page.wait_for_function("document.getElementById('heroClips').value === '20'")
    assert page.locator("[data-key='studio.default_clips'] [data-v='20']").get_attribute("aria-pressed") == "true"
    page.click("[data-key='studio.default_filter'] [data-v='Storytelling']")
    page.wait_for_function("document.getElementById('heroFilter').value === 'Storytelling'")
    page.click("[data-key='clips.max_overlap_sec'] [data-v='0']")
    page.wait_for_function("document.getElementById('setOverlapNote').textContent.includes('не перетинаються')")
    assert config_loader.app_config.clips.max_overlap_sec == 0
    page.keyboard.press("Escape")
    assert page.locator("#settingsScreen").is_hidden()
    assert not page.errors


def test_settings_key_status_and_sections(page, studio, project, monkeypatch):
    from backend import app_update
    calls = []
    monkeypatch.setattr(app_update, "check_gemini_key", lambda k: calls.append(k) or {"ok": True, "message": "Ключ працює."})
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.click(".settings-tabs [data-tab=ai]")
    assert page.locator("#settingsSection").inner_text() == "Ключ і розпізнавання"
    page.wait_for_function("document.getElementById('setKeyBox').dataset.state === 'ok'")
    assert "працює" in page.locator("#setKeyState").inner_text()
    assert len(calls) == 1
    assert page.locator("#setKeyDot").is_hidden()

    monkeypatch.setattr(app_update, "check_gemini_key", lambda k: {"ok": False, "message": "Google не прийняв цей ключ."})
    page.click("#setKeyCheck")
    page.wait_for_function("document.getElementById('setKeyBox').dataset.state === 'bad'")
    assert page.locator("#setKeyDot").is_visible()
    assert "не прийняв" in page.locator("#setKeyMsg").inner_text()

    # quality cards and the «Точніші оцінки» switch
    page.click("#setModel [data-v='medium']")
    page.wait_for_function("document.querySelector(\"#setModel [data-v='medium']\").getAttribute('aria-pressed') === 'true'")
    assert config_loader.app_config.whisper.model == "medium"
    page.click("#setModel [data-v='large-v3-turbo']")
    was = config_loader.app_config.gemini.rerank
    page.click("[data-key='gemini.rerank']")
    page.wait_for_function(f"Boolean({str(not was).lower()}) === document.querySelector(\"[data-key='gemini.rerank']\").checked")
    assert config_loader.app_config.gemini.rerank is (not was)
    page.click("[data-key='gemini.rerank']")

    page.click(".settings-tabs [data-tab=about]")
    assert page.locator("#btnReport").is_visible()
    page.wait_for_function("document.getElementById('setVersion').textContent.includes('Студія моментів ')")
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

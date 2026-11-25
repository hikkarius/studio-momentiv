"""Settings → «Модель ШІ для 🧪 експериментального пошуку» and «Що зараз доступно твоєму ключу»,
including a page that talks to a studio started before this update (not restarted yet)."""
import json

import config_loader
from backend import gemini_models


def _open_ai(page, studio):
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.click(".settings-tabs [data-tab=ai]")


def test_choose_experimental_model_and_check_models(page, studio, project, monkeypatch):
    monkeypatch.setattr(config_loader, "ensure_gemini_api_key", lambda: "k" * 39)
    rows = [{"model": "gemini-4-hydrogen", "role": "strong", "status": "busy", "seconds": None,
             "name": "Gemini 4 Hydrogen", "text": gemini_models.STATUS_TEXT["busy"]},
            {"model": "gemini-3.6-flash", "role": "strong", "status": "ok", "seconds": 7.5,
             "name": "Gemini 3.6 Flash", "text": gemini_models.STATUS_TEXT["ok"]}]
    monkeypatch.setattr(gemini_models, "check_models", lambda: {"checked_at": 1764090000, "models": rows})
    monkeypatch.setattr(config_loader.app_config.gemini, "experimental_model", "auto")
    _open_ai(page, studio)
    cards = page.locator("#setExpModel button")
    cards.first.wait_for()
    assert cards.count() == 3
    assert page.locator("#setExpModel [data-v=auto]").get_attribute("aria-pressed") == "true"
    page.click("#setExpModel [data-v='gemini-4-hydrogen']")
    page.wait_for_function("document.querySelector(\"#setExpModel [data-v='gemini-4-hydrogen']\").getAttribute('aria-pressed') === 'true'")
    assert config_loader.app_config.gemini.experimental_model == "gemini-4-hydrogen"

    page.click("#setModelsCheck")
    page.locator("#setModelsList li").nth(1).wait_for()
    text = page.locator("#setModelsList").inner_text()
    assert "Gemini 4 Hydrogen" in text and "перевантажена" in text and "відповідь 7,5 с" in text
    assert "undefined" not in page.locator("#settingsScreen").inner_text()
    assert page.errors == []


def test_page_survives_a_studio_that_was_not_restarted(page, studio, project):
    """Old server: /api/settings without the new values, /api/gemini/models missing (404)."""
    def old_settings(route):
        resp = route.fetch()
        data = resp.json()
        data["values"]["studio"].pop("default_search", None)
        data["values"]["gemini"].pop("experimental_model", None)
        data["choices"].pop("experimental_model", None)
        route.fulfill(response=resp, body=json.dumps(data))

    page.route("**/api/settings", old_settings)
    page.route("**/api/gemini/models**", lambda r: r.fulfill(status=404, body='{"detail":"Not Found"}',
                                                             content_type="application/json"))
    page.goto(studio["url"] + "/")
    page.click("#btnOpenSettings")
    page.wait_for_function("document.getElementById('setLenBig').textContent.includes('15–60')")
    # the length slider is drawn (fill() did not stop half-way) and no «undefined» card appears
    left = page.locator("#setLenFill").evaluate("e => e.style.left")
    assert left.endswith("%")
    assert "undefined" not in page.locator("#settingsScreen").inner_text()
    page.click(".settings-tabs [data-tab=ai]")
    page.wait_for_function("document.getElementById('setModelsNote').textContent.includes('запусти її знову')")
    assert "запусти її знову" in page.locator("#setExpModel").inner_text()
    page.click("#setModelsCheck")
    page.wait_for_function("document.getElementById('setModelsNote').textContent.includes('запусти її знову')")
    assert page.errors == []

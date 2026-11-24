"""
tests/e2e/test_home.py — the home page: first-run hero vs. the compact «Додай відео» block, the «Шукаю: …»
line, video cards with a state badge and a «⋯» menu, search and sort once there are many videos.
"""
import pytest

from backend import db

from .conftest import PROJECT_ID

EXTRA = [f"e2e_extra_{i}" for i in range(7)]


def _clear(studio):
    for pid in [PROJECT_ID, *EXTRA]:
        if db.get_project(pid):
            db.delete_project(pid)
    for item in db.list_queue_items():
        db.delete_queue_item(item["id"])


@pytest.fixture
def extra_projects(studio, project):
    for k, pid in enumerate(EXTRA):
        db.create_project(pid, f"Стендап {k + 1}", f"/nonexistent/standup_{k}.mp4", 60)
        db.save_candidate_clips(pid, [{"start": 1, "end": 20, "score": 50, "title": "x", "reason": "", "caption": "", "hashtags": []}])
        db.update_project_status(pid, "ready")
    yield
    for pid in EXTRA:
        if db.get_project(pid):
            db.delete_project(pid)


def test_first_run_shows_hero_with_steps(page, studio):
    _clear(studio)
    page.goto(studio["url"] + "/")
    page.wait_for_function("document.body.classList.contains('home-empty')")
    assert page.locator(".home-how li").count() == 3
    assert page.locator(".home-how").is_visible()
    assert page.locator(".hero-title").is_visible()
    assert page.locator(".home-add-title").is_hidden()
    assert page.locator("#historyCard").is_hidden()
    assert not page.errors


def test_with_videos_compact_block_badge_and_menu(page, studio, project):
    page.goto(studio["url"] + "/")
    card = page.locator(".video-card", has_text="Тестовий подкаст")
    card.wait_for()
    assert not page.evaluate("document.body.classList.contains('home-empty')")
    assert page.locator(".home-add-title").is_visible()
    assert page.locator(".home-how").is_hidden()
    assert "Нове" in card.locator(".vc-badge").inner_text()
    assert "4 вірусних" in card.locator(".video-card-meta").inner_text()
    assert page.locator("#dropzone, #queueSection, #hwBadge").count() == 0

    # «Видалити» hides in the «⋯» menu; Escape closes it
    menu = card.locator(".vc-menu")
    assert menu.is_hidden()
    card.hover()
    card.locator(".vc-dots").click()
    assert menu.is_visible()
    assert "Видалити" in menu.inner_text()
    page.keyboard.press("Escape")
    assert menu.is_hidden()

    # the search options open under «Змінити» and the chips follow them
    assert page.locator("#homeOptsPanel").is_hidden()
    page.click("#homeOptsEdit")
    page.select_option("#heroFilter", "Storytelling")
    assert "емоційні історії" in page.locator("#heroHint").inner_text()
    assert not page.errors


def test_reviewed_video_shows_progress(page, studio, project):
    db.update_clip_fields(PROJECT_ID, 1, review_status="keep")
    db.update_clip_fields(PROJECT_ID, 2, review_status="drop")
    page.goto(studio["url"] + "/")
    card = page.locator(".video-card", has_text="Тестовий подкаст")
    card.wait_for()
    assert "Розбираю" in card.locator(".vc-badge").inner_text()
    assert "2 вірусних · 1 в роботі" in card.locator(".video-card-meta").inner_text()
    for idx in (3, 4):
        db.update_clip_fields(PROJECT_ID, idx, review_status="drop")
    page.reload()
    assert "Розібрано" in page.locator(".video-card", has_text="Тестовий подкаст").locator(".vc-badge").inner_text()


def test_search_and_sort_with_many_videos(page, studio, extra_projects):
    page.goto(studio["url"] + "/")
    page.locator(".video-card", has_text="Стендап 7").wait_for()
    assert page.locator("#homeSearch").is_visible()
    page.fill("#homeSearch", "подкаст")
    assert page.locator("#historyList .video-card").count() == 1
    page.fill("#homeSearch", "нема такого")
    assert page.locator("#homeNothing").is_visible()
    page.fill("#homeSearch", "")
    assert page.locator("#historyList .video-card").count() == 8
    first = lambda: page.locator("#historyList .video-card").first.inner_text()
    newest = first()
    page.click("#homeSort button[data-s='old']")
    assert first() != newest
    page.reload()
    page.locator(".video-card").first.wait_for()
    assert page.locator("#homeSort button[data-s='old']").get_attribute("aria-pressed") == "true"
    page.click("#homeSort button[data-s='new']")
    assert not page.errors


def test_failed_video_says_why_and_offers_retry(page, studio, project):
    """A failed video: a short reason and what to do, the full text under «Детальніше», «Ще раз» / «Видалити»."""
    import ai_selector
    db.create_project("e2e_failed", "Невдалий стрім", "/nonexistent/failed.mp4", 600)
    db.update_project_status("e2e_failed", "failed")
    db.create_job("e2e_failed_job", "e2e_failed", "analyze")
    db.update_job("e2e_failed_job", status="failed", error=ai_selector.GEMINI_QUOTA_MESSAGE)
    try:
        page.goto(studio["url"] + "/")
        card = page.locator(".video-card", has_text="Невдалий стрім")
        card.wait_for()
        meta = card.locator(".err-sum")
        assert "Ліміт Gemini на зараз вичерпано" in meta.locator("b").inner_text()
        assert "тихоокеанським" not in meta.inner_text()  # the long text waits under «Детальніше»
        card.locator(".err-more summary").click()
        assert "тихоокеанським" in meta.inner_text()
        assert card.locator(".vc-acts button", has_text="Ще раз").is_visible()

        # «Видалити» asks in the studio's own window (no browser confirm); Escape keeps the video
        card.locator(".vc-acts button", has_text="Видалити").click()
        ask = page.locator(".ask-dialog")
        ask.wait_for()
        assert "Невдалий стрім" in ask.inner_text()
        page.keyboard.press("Escape")
        assert ask.count() == 0 and db.get_project("e2e_failed")
        card.locator(".vc-acts button", has_text="Видалити").click()
        page.locator(".ask-dialog button", has_text="Видалити").click()
        card.wait_for(state="detached")
        assert db.get_project("e2e_failed") is None
        assert not page.errors
    finally:
        if db.get_project("e2e_failed"):
            db.delete_project("e2e_failed")


def test_video_count_includes_the_queue(page, studio, project):
    for item in db.list_queue_items():
        db.delete_queue_item(item["id"])
    db.add_queue_item("e2e_q1", "https://www.youtube.com/watch?v=aaaaaaaaaaa", None, "Стендап", 0)
    try:
        page.goto(studio["url"] + "/")
        page.wait_for_function("document.getElementById('historyCount').textContent.includes('у черзі 1')")
        assert page.locator("#historyCount").text_content() == "1 · у черзі 1"
    finally:
        db.delete_queue_item("e2e_q1")

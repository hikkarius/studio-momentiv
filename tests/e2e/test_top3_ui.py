"""
tests/e2e/test_top3_ui.py — «🏆 Топ-3» window: picks the best «В роботі» clips (#3 first, #1 last),
reordering and removing, the 3-minute check, and the 16:9 download of the glued video.
"""
from backend import db

from .conftest import PROJECT_ID
from .test_ui import open_editor


def test_top3_window_orders_reorders_and_downloads(page, studio, project):
    for idx in (1, 2, 3):  # «Найкращий момент» (95), «Четвертий» (88)… go to work
        db.update_clip_fields(PROJECT_ID, idx, review_status="keep")
    db.update_clip_fields(PROJECT_ID, 4, review_status="keep")
    open_editor(page, studio)
    page.click("#edTop3")
    dlg = page.locator("#top3Dialog")
    dlg.wait_for(state="visible")
    rows = dlg.locator(".top3-list li")
    assert rows.count() == 3
    titles = [rows.nth(i).locator(".top3-t b").inner_text() for i in range(3)]
    assert titles[2] == "Найкращий момент"  # #1 comes last
    assert [rows.nth(i).locator(".top3-n").inner_text() for i in range(3)] == ["#3", "#2", "#1"]

    dlg.locator('[data-up="2"]').click()  # the best one to #2
    assert rows.nth(1).locator(".top3-t b").inner_text() == "Найкращий момент"
    dlg.locator('[data-rm="0"]').click()
    assert rows.count() == 2 and "Топ-2" in dlg.locator("#top3Title").inner_text()
    dlg.locator("#top3Pick").select_option(index=1)
    assert rows.count() == 3

    dlg.locator("#top3Name").fill("Мій топ")
    with page.expect_download(timeout=60000) as dl:
        dlg.locator('[data-a="h"]').click()
    assert dl.value.suggested_filename == "Мій топ 16x9.mp4"
    page.keyboard.press("Escape")
    dlg.wait_for(state="detached")
    assert page.errors == []

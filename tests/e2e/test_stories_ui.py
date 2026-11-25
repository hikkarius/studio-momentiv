"""
tests/e2e/test_stories_ui.py — «📖 Довга історія» in the real UI: the card mark, «Ціла | 2 частини»,
the ✂ cut on the track (drag saves it), a download button per part, «Частина 1/2» on the 9:16 phone,
and the «Довгі історії» option on the home page.
"""
import pytest

from backend import db

from .conftest import PROJECT_ID, VIDEO_SEC
from .test_ui import open_editor, wait_for


@pytest.fixture
def story_project(studio):
    if db.get_project(PROJECT_ID):
        db.delete_project(PROJECT_ID)
    db.create_project(PROJECT_ID, "Тестовий подкаст", studio["video"], VIDEO_SEC)
    db.save_candidate_clips(PROJECT_ID, [
        {"start": 5.0, "end": 22.0, "score": 80, "title": "Короткий момент", "reason": "Хук."},
        {"start": 20.0, "end": 110.0, "score": 95, "title": "Історія з митницею", "reason": "Ціла історія.",
         "kind": "story", "split": 65.0},
    ])
    db.update_project_status(PROJECT_ID, "ready")
    return PROJECT_ID


def story():
    return next(c for c in db.get_candidate_clips(PROJECT_ID) if c["kind"] == "story")


def test_story_whole_then_two_parts_with_a_draggable_cut(page, studio, story_project):
    open_editor(page, studio)
    row = page.locator("#edList .ed-row", has_text="Історія з митницею")
    assert "📖 ціла" in row.inner_text() and "1:30" in row.inner_text()
    row.click()
    page.locator("#edStory").wait_for(state="visible")
    assert page.locator("#edMp4Lab").inner_text().startswith("⬇ Ціла історія")
    assert page.locator("#edMp4Parts").is_hidden()

    page.click('#edStoryView [data-v="parts"]')
    assert wait_for(page, lambda: story()["story_view"] == "parts")
    page.locator("#edMp4Parts").wait_for(state="visible")
    assert page.locator("#edTrack .split").count() == 1
    assert page.locator("#edTrack .partlab").count() == 2
    assert "×2" in row.inner_text()

    box = page.locator("#edTrack .split").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 20)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 60, box["y"] + 20, steps=4)
    page.mouse.up()
    assert wait_for(page, lambda: story()["split_sec"] > 66.0)
    cut = story()["split_sec"]
    assert 35.0 <= cut <= 95.0  # at least 15 s on each side

    with page.expect_download() as dl:
        page.click('#edMp4Parts [data-part="2"]')
    assert "Частина 2" in dl.value.suggested_filename
    assert page.errors == []


def test_part_label_on_the_phone_and_home_option(page, studio, story_project):
    db.update_clip_fields(PROJECT_ID, story()["clip_index"], review_status="keep", story_view="parts")
    open_editor(page, studio)
    page.click('#edMode [data-m="vx"]')
    page.locator("#vxPart").wait_for(state="visible")
    assert page.locator("#vxPart").inner_text().upper() == "ЧАСТИНА 1/2"

    page.goto(studio["url"] + "/")
    page.click("#homeOptsEdit")
    assert page.locator("#heroStories").input_value() == "on"
    assert "історії до 3 хв" in page.locator("#heroHint").inner_text()
    page.select_option("#heroStories", "off")
    assert "історії" not in page.locator("#heroHint").inner_text()
    assert page.errors == []

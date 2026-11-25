"""Step 2 on a video whose id has Cyrillic letters and spaces (like real uploads): every request must succeed."""
import io
import zipfile

from backend import db

from .conftest import CLIPS

PID = "2026-11-19_1039_друга_частина_відео_c047"


def _project(studio):
    if db.get_project(PID):
        db.delete_project(PID)
    db.create_project(PID, "друга частина відео.mp4", studio["video"], 120.0)
    db.save_candidate_clips(PID, [{"start": s, "end": e, "score": sc, "title": t, "caption": f"Опис: {t}", "hashtags": ["#тест"]}
                                  for s, e, sc, t in CLIPS])
    db.update_project_status(PID, "ready")
    for c in db.get_candidate_clips(PID)[:2]:
        db.update_clip_fields(PID, c["clip_index"], review_status="keep")


def test_step2_walkthrough_with_cyrillic_video_id(page, studio, project):
    _project(studio)
    bad = []
    page.on("response", lambda r: r.status >= 400 and "/api/" in r.url and bad.append(f"{r.status} {r.url}"))
    try:
        page.evaluate("1")
        page.goto(studio["url"] + "/")
        page.evaluate("localStorage.setItem('studio-vx-view', 'one')")
        page.locator(".history-item-left", has_text="друга частина відео").click()
        page.locator("#editor").wait_for(state="visible")
        page.wait_for_function("Editor.state().words > 0")
        page.click("#edMode button[data-m='vx']")
        page.wait_for_function("Vertical.state().layout", timeout=20000)
        # titles from the AI
        page.click("#vxTabs button[data-t='hook']")
        page.locator("#vxIdeaList .vx-idea").first.wait_for(timeout=8000)
        page.locator("#vxIdeaList .vx-idea").nth(2).click()
        page.wait_for_function("document.getElementById('vxHook').textContent.includes('приховують')")
        # a template, then the list
        page.click("#vxTabs button[data-t='style']")
        page.click("#vxTplSave")
        page.fill("#vxTplName", "Подкаст")
        page.click("#vxTplOk")
        page.wait_for_function("document.getElementById('vxTpl').value === 'Подкаст'")
        page.click("#vxToList")
        rows = page.locator("#vxLRows .vx-lrow")
        rows.first.wait_for()
        assert rows.count() == 2
        page.wait_for_function("[...document.querySelectorAll('#vxLRows img')].every((i) => i.complete && i.naturalWidth > 0)", timeout=20000)
        page.click("#vxLApply")
        page.wait_for_function("[...document.querySelectorAll('#vxLRows select')].every((s) => s.value === 'Подкаст')")
        page.click("#vxLMake")
        page.wait_for_function("document.querySelectorAll('#vxLRows .vx-lok').length === 2", timeout=180000)
        with page.expect_download(timeout=60000) as dl:
            page.locator("#vxLRows button[data-dl]").first.click()
        assert dl.value.suggested_filename.endswith("9x16.mp4")
        with page.expect_download(timeout=120000) as dl:
            page.click("#vxLZip")
        assert len(zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read())).namelist()) == 2
        # back to one clip; its title opens from the list
        page.locator("#vxLRows .vx-lname").nth(1).click()
        assert page.locator("#vxPhone").is_visible()
        assert not bad, bad
        assert not page.errors
    finally:
        db.delete_project(PID)
        for t in db.list_vertical_templates():
            db.delete_vertical_template(t["name"])

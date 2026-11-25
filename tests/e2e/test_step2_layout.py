"""
tests/e2e/test_step2_layout.py — step 2 as drawn in the 2026-11-25 mockups: five tabs (Кадр, Субтитри, Заголовок,
Текст, Допис) with no «Тонко», the subtitles and the title dragged right on the phone (the sliders follow),
«По одному / Усі разом» in the header, the «Шаблони» explainer, and the logo that leads home.
"""
from .test_ui import clip, keep_first_clip, open_editor, wait_for


def open_step2(page, studio):
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_function("Vertical.state().layout", timeout=20000)
    page.wait_for_function("Vertical.state().words > 0")


def test_five_tabs_and_every_subtitle_setting_on_one_tab(page, studio, project):
    open_step2(page, studio)
    tabs = page.locator("#vxTabs button").all_inner_texts()
    assert [t.split("\n")[-1].strip() for t in tabs] == ["Кадр", "Субтитри", "Заголовок", "Текст", "Допис"]
    page.click("#vxTabs button[data-t='style']")
    pane = page.locator("#edVx .vx-pane[data-t='style']")
    for sel in ("#vxStyle", "#vxSize", "#vxPos", "#vxStroke", "#vxColor", "#vxText"):
        assert pane.locator(sel).is_visible(), sel
    page.click("#vxTabs button[data-t='hook']")
    pane = page.locator("#edVx .vx-pane[data-t='hook']")
    for sel in ("#vxHookOn", "#vxHookSize", "#vxHookPos", "#vxHookStyle", "#vxHookStroke"):
        assert pane.locator(sel).count() == 1, sel
    # the template bar is shared by every tab
    page.click("#vxTabs button[data-t='text']")
    assert page.locator("#vxTpl").is_visible() and page.locator("#vxTplHelp").is_visible()
    assert not page.errors


def test_drag_subtitles_and_title_on_the_phone(page, studio, project):
    open_step2(page, studio)
    idx = page.evaluate("Editor.state().clipIndex")
    page.click("#vxTabs button[data-t='hook']")
    page.check("#vxHookOn")
    page.click("#vxHookDur button[data-v='all']")
    page.click("#vxTabs button[data-t='frame']")
    page.wait_for_function("document.getElementById('vxCap').textContent.trim().length > 0")

    # subtitles: press on them, drag up; the «Субтитри» tab opens and its height slider follows
    pos0 = int(page.input_value("#vxPos"))
    box = page.evaluate("(() => { const r = document.createRange(); r.selectNodeContents(document.getElementById('vxCap'));"
                        " const b = r.getBoundingClientRect(); return {x: b.x + b.width / 2, y: b.y + b.height / 2}; })()")
    ph = page.locator("#vxPhone").bounding_box()
    page.mouse.move(box["x"], box["y"])
    page.mouse.down()
    page.mouse.move(box["x"], box["y"] - ph["height"] * 0.15, steps=6)
    page.mouse.up()
    assert page.locator("#vxTabs button[data-t='style']").get_attribute("aria-selected") == "true"
    assert page.locator("#vxTSel").is_visible()
    pos1 = int(page.input_value("#vxPos"))
    assert pos1 <= pos0 - 10, (pos0, pos1)
    assert wait_for(page, lambda: clip(idx).get("vertical_opts", {}).get("pos") == pos1)
    assert page.evaluate("document.getElementById('edVideo').paused")  # a press on the text doesn't play the clip

    # the title: a corner of its frame changes the size
    hb = page.locator("#vxHook span").bounding_box()
    page.mouse.click(hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2)
    assert page.locator("#vxTabs button[data-t='hook']").get_attribute("aria-selected") == "true"
    size0 = int(page.input_value("#vxHookSize"))
    hb = page.locator("#vxHook span").bounding_box()
    page.mouse.move(hb["x"] + hb["width"] - 2, hb["y"] + hb["height"] - 2)
    page.mouse.down()
    page.mouse.move(hb["x"] + hb["width"] + 60, hb["y"] + hb["height"] + 10, steps=6)
    page.mouse.up()
    size1 = int(page.input_value("#vxHookSize"))
    assert size1 > size0, (size0, size1)
    assert wait_for(page, lambda: abs(clip(idx).get("vertical_opts", {}).get("hook_size", 0) - size1 / 100) < 0.001)

    # a click on an empty spot drops the frame; Esc too
    page.keyboard.press("Escape")
    assert page.locator("#vxTSel").is_hidden()
    assert not page.errors


def test_view_switch_in_header_and_back(page, studio, project):
    open_step2(page, studio)
    assert page.locator(".vx-hview").is_visible()
    page.click("#vxToList")
    page.locator("#vxList").wait_for(state="visible")
    assert page.locator(".vx-lhint").is_visible()
    page.click("#vxToOne")
    page.locator("#vxPhone").wait_for(state="visible")
    assert page.locator(".vx-hview button[aria-pressed='true']").inner_text() == "По одному"


def test_templates_explainer(page, studio, project):
    open_step2(page, studio)
    page.click("#vxTplHelp")
    page.locator("#vxTplModal").wait_for(state="visible")
    assert "Шаблон пам’ятає" in page.locator("#vxTplModal").inner_text()
    assert "Ще жодного" in page.locator("#vxTmMine").inner_text()
    page.keyboard.press("Escape")
    assert page.locator("#vxTplModal").is_hidden()
    # save one, then it shows up in the dialog, marked as this clip's
    page.click("#vxTplSave")
    page.fill("#vxTplName", "Подкаст")
    page.click("#vxTplOk")
    page.wait_for_function("document.querySelector('#vxTpl').value === 'Подкаст'")
    page.click("#vxTplHelp")
    row = page.locator("#vxTmMine .vx-tm-row")
    assert row.count() == 1 and "на цьому кліпі" in row.inner_text()
    row.locator("button[data-del]").click()
    page.wait_for_function("document.querySelectorAll('#vxTmMine .vx-tm-row').length === 0")
    page.click("#vxTmOk")
    assert page.locator("#vxTplModal").is_hidden()
    assert not page.errors


def test_logo_leads_home(page, studio, project):
    open_editor(page, studio)
    page.click("#edHome")
    page.locator("#editor").wait_for(state="hidden")
    assert page.locator(".history-item-left", has_text="Тестовий подкаст").is_visible()

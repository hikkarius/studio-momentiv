"""
tests/e2e/test_ui.py — clicks through the real UI in Chromium: theme switch on the home page,
opening a project, reviewing clips («Беру» / «Не беру», keys), player clicks, jumping by words,
scrubbing and changing clip bounds on the track under the video, editing the post, and downloading one clip or the ZIP of kept clips.
"""
import io
import zipfile

from backend import db

from .conftest import PROJECT_ID


def clip(idx):
    return next(c for c in db.get_candidate_clips(PROJECT_ID) if c["clip_index"] == idx)


def wait_for(page, fn, timeout=6000):
    """Polls a Python condition (e.g. DB state written by the page) while letting the page run."""
    step = 100
    for _ in range(timeout // step):
        if fn():
            return True
        page.wait_for_timeout(step)
    return fn()


def open_editor(page, studio):
    page.goto(studio["url"] + "/")
    page.locator(".history-item-left", has_text="Тестовий подкаст").click()
    page.locator("#editor").wait_for(state="visible")
    page.wait_for_function("Editor.state().words > 0")
    page.wait_for_function("document.getElementById('edVideo').readyState >= 1")


# ─── Home page ───────────────────────────────────────────────────────────────

def pick_theme(page, scope, mode):
    """Themes live in a drop-down: open it, pick one, it closes again."""
    page.click(f"{scope} .theme-menu-btn")
    page.click(f"{scope} .theme-switch button[data-theme='{mode}']")
    assert page.locator(f"{scope} .theme-menu-pop").is_hidden()


def test_home_theme_switch_persists(page, studio, project):
    page.goto(studio["url"] + "/")
    assert page.locator(".app-header .theme-menu-pop").is_hidden()
    pick_theme(page, ".app-header", "light")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")

    assert "Світла" in page.locator(".app-header .theme-menu-btn").inner_text()
    pick_theme(page, ".app-header", "dark")
    dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert light_bg != dark_bg

    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert page.locator(".app-header .theme-switch button[data-theme='dark']").get_attribute("aria-pressed") == "true"

    page.click(".app-header .theme-menu-btn")
    page.keyboard.press("Escape")  # Escape closes the menu without a change
    assert page.locator(".app-header .theme-menu-pop").is_hidden()
    pick_theme(page, ".app-header", "system")
    assert page.evaluate("document.documentElement.hasAttribute('data-theme')") is False
    assert not page.errors


def head(page):
    """«№N · title» of the open clip, as the stage heading shows it."""
    return f'{page.locator("#edNo").text_content()} · {page.locator("#edClipTitle").text_content()}'


def test_open_project_shows_editor_sorted_by_score(page, studio, project):
    open_editor(page, studio)
    rows = page.locator("#edList .ed-row")
    assert rows.count() == 4
    assert "Найкращий момент" in rows.nth(0).inner_text()
    # numbers follow the place in the video: the best clip (30–50 s) is the 2nd one in time
    assert head(page).startswith("№2 · Найкращий момент")
    assert page.locator("#edList .ed-num").all_inner_texts() == ["№2", "№4", "№1", "№3"]
    assert page.locator("#edOv .c").count() == 4
    assert page.locator("#edOv .lab").all_inner_texts() == ["1", "2", "3", "4"]
    assert page.locator("#edTrack").is_visible()  # the clip track sits right under the video
    screen = page.locator("#edScreen").bounding_box()
    track = page.locator("#edTrack").bounding_box()
    assert 0 <= track["y"] - (screen["y"] + screen["height"]) < 110  # only the «Початок · с · Кінець» line, the clip map and the time ruler between
    # trimmed screen: no score line next to the title, no ±0,5 / ↑↓ buttons, no bottom bar with the ZIP
    assert page.locator("#edClipMeta, [data-nudge], #edPrev, #edNext, .ed-bar, #edZip, #hwBadge").count() == 0
    # step rail with both steps, download always on top of the side panel, «Чому» under the title
    assert page.locator("#edMode button[data-m='table'][aria-selected='true']").is_visible()
    assert "переглянуто 0/4" in page.locator("#edStep1").inner_text()
    assert page.locator("#edMp4").is_visible()
    assert "Сильний хук" in page.locator("#edWhy").inner_text()
    # four status shelves on top; the strip opens on «Нові»
    assert page.locator("#edCnt_new").inner_text() == "4"
    assert page.locator("#edShelves [data-f='new'][aria-pressed='true']").is_visible()
    assert page.locator("#edFilters").count() == 0
    assert not page.errors


def test_editor_theme_menu(page, studio, project):
    open_editor(page, studio)
    pick_theme(page, "#editor", "dark")
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.click("#editor .theme-menu-btn")
    page.mouse.click(5, 300)  # a click elsewhere closes it
    assert page.locator("#editor .theme-menu-pop").is_hidden()
    assert not page.errors


# ─── Reviewing ───────────────────────────────────────────────────────────────

def test_status_menu_shelves_and_keys(page, studio, project):
    open_editor(page, studio)
    assert page.locator("#edStatus").inner_text().startswith("Нове")
    page.click("#edStatus")  # the menu lists all four statuses
    assert page.locator("#edStatusMenu").is_visible()
    assert page.locator("#edStatusMenu button").count() == 4
    page.click("#edStatusMenu button[data-s='keep']")  # best clip (#2) → В роботі; it stays open
    assert wait_for(page, lambda: clip(2)["review_status"] == "keep")
    assert page.locator("#edStatusMenu").is_hidden()
    assert head(page).startswith("№2")
    assert page.locator("#edStatus").inner_text().startswith("В роботі")
    # it left the «Нові» shelf: the strip shows the other three, the counters moved
    assert page.locator("#edList .ed-row").count() == 3
    assert page.locator("#edCnt_new").inner_text() == "3" and page.locator("#edCnt_keep").inner_text() == "1"
    assert "В роботі" in page.locator("#edShelfNote").inner_text()

    page.keyboard.press("ArrowDown")  # on to the next new clip, where №2 was
    assert head(page).startswith("№4")
    page.keyboard.press("n")  # second best (#4) → Не беру
    assert wait_for(page, lambda: clip(4)["review_status"] == "drop")
    assert page.locator("#edToast .ed-toast-undo").is_visible()
    page.click("#edToast .ed-toast-undo")  # ↶ Повернути
    assert wait_for(page, lambda: clip(4)["review_status"] == "")
    page.keyboard.press("u")  # U → Виконано
    assert wait_for(page, lambda: clip(4)["review_status"] == "done")

    page.click("#edShelves [data-f='keep']")
    assert page.locator("#edList .ed-row").count() == 1
    assert head(page).startswith("№2")
    assert page.locator("#edOv .c.keep").count() == 1
    assert page.locator("#edOv .c.done").count() == 1
    # a clip picked on the map opens its own shelf
    page.locator("#edOv .c.done").click()
    assert page.locator("#edShelves [data-f='done'][aria-pressed='true']").is_visible()
    page.keyboard.press("0")  # 0 → back to Нове
    assert wait_for(page, lambda: clip(4)["review_status"] == "")
    assert not page.errors


def test_pauses_button_and_track(page, studio, project):
    open_editor(page, studio)  # best clip №2: 30-50 s, words every 0.5 s, so there are no long pauses
    assert page.locator("#edRuler b").count() > 0  # time ruler
    assert page.locator("#edTrack canvas.wave").count() == 1
    page.click("#edPauses")
    page.wait_for_function("document.getElementById('edToast').textContent.includes('Пауз')")
    assert clip(2)["cuts"] == [] and page.locator("#edPauseBar").is_hidden()
    # a saved cut shows as a band; clicking it restores the pause
    db.update_clip_fields(PROJECT_ID, 2, cuts=[[35.0, 36.0]])
    open_editor(page, studio)
    page.locator("#edTrack .cut").first.wait_for(state="visible")
    assert page.locator("#edPauseBar").is_visible() and page.locator("#edMp4Raw").is_visible()
    page.locator("#edTrack .cut").first.click()
    assert wait_for(page, lambda: clip(2)["cuts"] == [])
    assert page.locator("#edTrack .cut.ghost").count() == 1
    assert not page.errors


def test_sort_best_or_in_order(page, studio, project):
    open_editor(page, studio)
    first = lambda: page.locator("#edList .ed-row").first.inner_text()
    assert "Найкращий момент" in first()
    page.click("#edSort button[data-s='time']")
    assert "Перший момент" in first()
    page.reload()
    page.locator(".history-item-left", has_text="Тестовий подкаст").click()
    page.locator("#editor").wait_for(state="visible")
    assert "Перший момент" in first()  # remembered
    page.click("#edSort button[data-s='score']")
    assert not page.errors


def test_timeline_and_arrows_navigate(page, studio, project):
    open_editor(page, studio)
    page.locator("#edOv .lab", has_text="1").first.click()  # №1 = first clip in time
    page.wait_for_function("Editor.state().clipIndex === 1")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("Editor.state().clipIndex !== 1")
    page.keyboard.press("ArrowUp")
    page.wait_for_function("Editor.state().clipIndex === 1")


# ─── Player ──────────────────────────────────────────────────────────────────

def test_click_on_video_plays_and_pauses(page, studio, project):
    open_editor(page, studio)
    assert page.evaluate("Editor.state().paused") is True
    page.click("#edScreen")
    page.wait_for_function("!Editor.state().paused")
    page.wait_for_function("document.getElementById('edPlay').getAttribute('aria-label') === 'Пауза'")
    page.wait_for_timeout(400)
    page.click("#edScreen")
    page.wait_for_function("Editor.state().paused")
    t = page.evaluate("Editor.state().time")
    assert 30.0 <= t <= 50.0  # stayed inside the clip


def test_click_on_word_jumps_player_there(page, studio, project):
    open_editor(page, studio)
    word = page.locator("#edWords .ed-w", has_text="слово90").first  # 90 * 0.5 s = 45 s
    word.click()
    page.wait_for_function("Math.abs(Editor.state().time - 45) < 1.0")
    # the clip bounds are NOT changed by a word click
    assert clip(2)["start_sec"] == 30.0 and clip(2)["end_sec"] == 50.0

    # a grey word after the clip does not jump past the clip end
    page.click("#edScreen")  # pause
    page.wait_for_function("Editor.state().paused")
    before = page.evaluate("Editor.state().time")
    page.locator("#edWords .ed-w.out", has_text="слово110").first.click()  # 55 s, outside 30–50
    page.wait_for_timeout(300)
    assert abs(page.evaluate("Editor.state().time") - before) < 0.5
    assert page.evaluate("Editor.state().paused") is True


def test_bracket_keys_set_bounds_at_playhead(page, studio, project):
    open_editor(page, studio)
    page.evaluate("document.getElementById('edVideo').currentTime = 33.0")
    page.wait_for_function("Math.abs(Editor.state().time - 33) < 0.2")
    page.keyboard.press("[")
    assert wait_for(page, lambda: abs(clip(2)["start_sec"] - 32.88) < 0.3)  # snapped just before the word at 33.0

    page.evaluate("document.getElementById('edVideo').currentTime = 47.1")
    page.wait_for_function("Math.abs(Editor.state().time - 47.1) < 0.2")
    page.keyboard.press("]")
    assert wait_for(page, lambda: abs(clip(2)["end_sec"] - 47.5) < 0.3)  # snapped to the end of the word


def test_drag_on_track_scrubs_video(page, studio, project):
    open_editor(page, studio)
    page.wait_for_function("document.querySelectorAll('#edTrack .w').length > 0")
    track = page.locator("#edTrack").bounding_box()
    y = track["y"] + track["height"] - 6  # below the word chips: plain track
    page.mouse.move(track["x"] + track["width"] * 0.3, y)
    page.mouse.down()
    first = page.evaluate("Editor.state().time")
    page.mouse.move(track["x"] + track["width"] * 0.7, y, steps=8)
    page.wait_for_function(f"Editor.state().time > {first} + 3")
    page.mouse.up()
    assert page.evaluate("Editor.state().paused") is True
    assert page.locator("#edPop").count() == 0
    # bounds untouched by scrubbing
    assert clip(2)["start_sec"] == 30.0 and clip(2)["end_sec"] == 50.0


def test_playhead_stays_inside_the_clip(page, studio, project):
    open_editor(page, studio)
    page.wait_for_function("document.querySelectorAll('#edTrack .w').length > 0")
    track = page.locator("#edTrack").bounding_box()
    y = track["y"] + track["height"] - 4
    page.mouse.move(track["x"] + track["width"] * 0.5, y)
    page.mouse.down()
    page.mouse.move(track["x"] + track["width"] - 2, y, steps=6)  # far past the end bracket
    page.mouse.up()
    page.wait_for_function("Math.abs(Editor.state().time - 50) < 0.3")  # stopped at the clip end (50 s)
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    for _ in range(12):
        page.keyboard.press("ArrowLeft")
    page.wait_for_function("Math.abs(Editor.state().time - 30) < 0.3")  # and at the start (30 s)

    # moving the end bracket later lets the playhead reach further
    page.evaluate("document.getElementById('edVideo').currentTime = 51.0")
    page.wait_for_function("Math.abs(Editor.state().time - 51) < 0.2")
    page.keyboard.press("]")
    assert wait_for(page, lambda: clip(2)["end_sec"] > 50.7)
    new_end = clip(2)["end_sec"]
    page.evaluate("document.getElementById('edVideo').currentTime = 40.0")
    page.wait_for_function("Math.abs(Editor.state().time - 40) < 0.2")
    track = page.locator("#edTrack").bounding_box()  # the zoom follows the new bounds
    page.mouse.move(track["x"] + track["width"] * 0.5, y)
    page.mouse.down()
    page.mouse.move(track["x"] + track["width"] - 2, y, steps=6)
    page.mouse.up()
    page.wait_for_function(f"Math.abs(Editor.state().time - {new_end}) < 0.3")


def test_drag_handle(page, studio, project):
    open_editor(page, studio)

    handle = page.locator("#edTrack .hd[data-h='start']")
    box = handle.bounding_box()
    zoom = page.locator("#edTrack").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] / 2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + zoom["width"] * 0.1, y, steps=5)
    page.mouse.up()
    assert wait_for(page, lambda: clip(2)["start_sec"] > 31.0)
    assert clip(2)["end_sec"] - clip(2)["start_sec"] >= 3


def test_track_navigation(page, studio, project):
    open_editor(page, studio)
    first_label = lambda: page.locator("#edRuler b").first.inner_text()
    span = lambda: page.evaluate("(() => { const w = document.querySelector('#edMap .win'); return w.getBoundingClientRect().width / w.parentElement.getBoundingClientRect().width; })()")
    assert span() > 0.95 and page.locator("#edZoomAll").is_disabled()  # whole clip in view
    page.click("#edZoomIn"); page.click("#edZoomIn"); page.click("#edZoomIn")
    assert span() < 0.5 and page.locator("#edZoomAll").is_enabled()
    # the plain wheel scrolls sideways, it does not zoom
    before, w0 = first_label(), span()
    box = page.locator("#edTrack").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 20)
    for _ in range(3):
        page.mouse.wheel(0, 200)
    page.wait_for_timeout(200)
    assert first_label() != before and abs(span() - w0) < 0.02
    # Ctrl + wheel zooms
    page.keyboard.down("Control"); page.mouse.wheel(0, 200); page.keyboard.up("Control")
    assert span() > w0 + 0.05
    # dragging the frame on the map moves the track
    before = first_label()
    win = page.locator("#edMap .win").bounding_box()
    page.mouse.move(win["x"] + win["width"] / 2, win["y"] + win["height"] / 2)
    page.mouse.down(); page.mouse.move(win["x"] + win["width"] / 2 - 120, win["y"] + 5, steps=4); page.mouse.up()
    assert first_label() != before
    page.click("#edZoomAll")
    assert span() > 0.95
    assert not page.errors


def test_word_menu_sets_end(page, studio, project):
    open_editor(page, studio)
    page.wait_for_function("document.querySelectorAll('#edTrack .w').length > 0")
    chips = page.locator("#edTrack .w")
    # pick a visible word chip after the clip end (50 s); its number n sits at n * 0.5 s
    target = None
    for k in range(chips.count()):
        n = int(chips.nth(k).inner_text().lower().replace("слово", ""))
        if 51 <= n * 0.5 <= 54:
            target, t = chips.nth(k), n * 0.5
            break
    assert target is not None
    target.locator("xpath=..").hover()  # a phrase opens up under the mouse, so every word in it can be clicked
    target.click()
    page.locator("#edPop").wait_for(state="visible")
    page.click("#edPop [data-p='end']")
    assert wait_for(page, lambda: abs(clip(2)["end_sec"] - (t + 0.5)) < 0.05)
    assert page.locator("#edPop").count() == 0


# ─── Post text & downloads ───────────────────────────────────────────────────

def test_edit_post_texts(page, studio, project):
    open_editor(page, studio)
    page.click("#edTabs button[data-tab='post']")
    page.fill("#edTitle", "Новий заголовок")
    page.press("#edTitle", "Tab")
    page.fill("#edTags", "шорти, подкаст")
    page.press("#edTags", "Tab")
    assert wait_for(page, lambda: clip(2)["title"] == "Новий заголовок" and clip(2)["hashtags"] == ["#шорти", "#подкаст"])
    assert "Новий заголовок" in page.locator("#edList .ed-row").nth(0).text_content()
    assert "Новий заголовок" in head(page)


def test_download_single_clip_and_zip(page, studio, project):
    open_editor(page, studio)
    with page.expect_download() as dl:
        page.click("#edMp4")
    assert dl.value.suggested_filename.endswith(".mp4")

    with page.expect_download() as dl:
        page.click("#edSrt")
    assert dl.value.suggested_filename.endswith(".srt")

    page.keyboard.press("y")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("y")
    assert wait_for(page, lambda: sum(c["review_status"] == "keep" for c in db.get_candidate_clips(PROJECT_ID)) == 2)
    page.click("#edMode button[data-m='vx']")  # the ZIP of kept clean clips lives on «Вертикальні кліпи» → «Ще»
    with page.expect_download(timeout=30000) as dl:
        page.click("#vxMoreBtn")
        page.click("#vxZipClean")
    path = dl.value.path()
    names = zipfile.ZipFile(io.BytesIO(open(path, "rb").read())).namelist()
    assert sum(n.endswith(".mp4") for n in names) == 2
    assert "posts.txt" in names
    assert not page.errors


def test_back_returns_home(page, studio, project):
    open_editor(page, studio)
    page.click("#edBack")
    page.locator("#editor").wait_for(state="hidden")
    assert page.locator(".history-item-left", has_text="Тестовий подкаст").is_visible()
    # The home page must come back intact: top of the page, centred hero layout.
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") == 0
    assert page.evaluate("getComputedStyle(document.getElementById('step1Card')).display") == "flex"


def test_editor_fits_phone_and_dark_theme(browser, studio, project):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
    p = ctx.new_page()
    open_editor(p, studio)
    assert p.evaluate("document.getElementById('editor').scrollWidth <= window.innerWidth")
    bg = p.evaluate("getComputedStyle(document.getElementById('editor')).backgroundColor")
    assert bg == "rgb(12, 12, 12)"
    ctx.close()


# ─── Vertical 9:16 export ────────────────────────────────────────────────────

def vx_tab(page, t):
    """«Вертикальні кліпи» shows one settings tab at a time: Кадр, Стиль, Заголовок, Текст, Тонко."""
    page.click(f"#vxTabs button[data-t='{t}']")


def vx_more(page, item):
    """Rarer downloads live under «Ще ▾» next to the big download button."""
    page.click("#vxMoreBtn")
    page.click(item)


def test_vertical_preview_settings_and_download(page, studio, project):
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    # The test video has no faces, so «Авто» shows the whole frame on a blurred background.
    page.wait_for_function("Vertical.state().layout === 'blur'", timeout=20000)
    page.wait_for_function("Vertical.state().words > 0")
    assert page.locator("#vxNote").is_visible()

    # Pick a caption style and framing: saved per clip.
    assert page.locator("#vxStyle button[data-v='one'] .vx-tile").is_visible()  # «Стиль» is the first tab, styles as pictures
    page.click("#vxStyle button[data-v='one']")
    vx_tab(page, "frame")
    page.click("#vxFrame button[data-v='blur']")
    idx = page.evaluate("Editor.state().clipIndex")
    assert wait_for(page, lambda: clip(idx).get("vertical_opts", {}).get("style") == "one"
                    and clip(idx)["vertical_opts"].get("frame") == "blur")

    # Fix a word in the captions.
    vx_tab(page, "text")
    page.locator("#vxWords button").first.click()
    page.fill("#vxWinput", "ВИПРАВЛЕНО")
    page.click("#vxWsave")
    assert wait_for(page, lambda: any(e.get("w") == "ВИПРАВЛЕНО" for e in clip(idx)["vertical_opts"].get("edits", {}).values()))
    assert "dot" in page.get_attribute("#vxTabText", "class")  # the «Текст» tab shows there are fixes
    page.click("#vxPhone")  # click on the preview plays it
    page.wait_for_function("Vertical.state().cap.length > 0", timeout=8000)
    page.wait_for_function("!document.getElementById('edVideo').paused")
    page.click("#vxPlay")
    page.wait_for_function("document.getElementById('edVideo').paused")
    # «Скачати» is wide: the player column is out of the way, and comes back on «Текст»
    assert page.evaluate("document.querySelector('.ed-cols').classList.contains('vx-mode')")
    assert not page.locator("#edTabs").is_visible()  # its own screen, the table's tabs are out of the way
    assert page.locator("#vxPhone").bounding_box()["width"] >= 200

    with page.expect_download(timeout=120000) as dl:
        page.click("#vxDlSubs")
    assert dl.value.suggested_filename.endswith("_9x16_subs.mp4")
    import subprocess
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", dl.value.path()], capture_output=True, text=True).stdout.strip()
    assert info == "1080,1920"

    # The clean 16:9 clip is still one click away.
    with page.expect_download() as dl:
        page.click("#vxMp4")
    assert not dl.value.suggested_filename.endswith("_9x16.mp4")
    # Back to the table: Текст / Допис / Скачати are where they were.
    page.click("#edMode button[data-m='table']")
    assert page.locator("#edPlay").is_visible()
    assert page.locator("#edMp4").is_visible() and page.locator("#edGoVx").is_visible()
    page.click("#edGoVx")
    assert page.locator("#vxPhone").is_visible()
    assert not page.errors


def test_vertical_title_size_colour_and_scrub(page, studio, project):
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_function("Vertical.state().layout", timeout=20000)
    idx = page.evaluate("Editor.state().clipIndex")
    vx_tab(page, "hook")
    page.check("#vxHookOn")
    page.fill("#vxHookText", "МІЙ ЗАГОЛОВОК")
    page.keyboard.press("Enter")
    page.click("#vxHookStyle button[data-v='black']")
    page.click("#vxHookDur button[data-v='all']")
    vx_tab(page, "fine")
    page.click("#vxColor button[data-v='FF4D4D']")
    page.locator("#vxSize").fill("130")
    page.locator("#vxSize").dispatch_event("change")
    assert wait_for(page, lambda: (lambda o: o.get("hook_text") == "МІЙ ЗАГОЛОВОК" and o.get("hook_style") == "black"
                                   and o.get("hook_full") is True and o.get("color") == "FF4D4D" and o.get("size") == 1.3)(clip(idx).get("vertical_opts", {})))
    assert page.locator("#vxHook").inner_text() == "МІЙ ЗАГОЛОВОК"
    # Scrub to the middle of the clip
    page.locator("#vxScrub").fill("8")
    page.locator("#vxScrub").dispatch_event("input")
    t = page.evaluate("Editor.state().time - Editor.state().start")
    assert abs(t - 8) < 0.3
    # «Беру» right here, and the clip stays open
    page.click("#vxStatus")
    page.click("#edStatusMenu button[data-s='keep']")
    assert wait_for(page, lambda: clip(idx)["review_status"] == "keep")
    assert page.evaluate("Editor.state().clipIndex") == idx
    assert page.locator("#vxStatus").inner_text().startswith("В роботі")
    # Rounded plates with a white edge, for captions and for the title
    page.click("#vxRound button[data-v='1']")
    page.click("#vxStroke button[data-v='FFFFFF']")
    vx_tab(page, "hook")
    page.click("#vxHookRound button[data-v='1']")
    page.click("#vxHookStroke button[data-v='000000']")
    assert wait_for(page, lambda: (lambda o: o.get("round") is True and o.get("stroke") == "FFFFFF" and o.get("hook_round") is True
                                   and o.get("hook_stroke") == "000000")(clip(idx).get("vertical_opts", {})))
    assert page.evaluate("getComputedStyle(document.getElementById('vxPhone')).getPropertyValue('--vx-edge').trim()") == "#FFFFFF"
    assert not page.errors


def test_vertical_zip_of_kept_clips(page, studio, project):
    open_editor(page, studio)
    page.keyboard.press("y")
    assert wait_for(page, lambda: sum(c["review_status"] == "keep" for c in db.get_candidate_clips(PROJECT_ID)) == 1)
    page.click("#edMode button[data-m='vx']")
    with page.expect_download(timeout=180000) as dl:
        vx_more(page, "#vxZip")
    names = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read())).namelist()
    assert len(names) == 1 and names[0].endswith("_9x16_subs.mp4")
    assert not page.errors


# ─── Step rail, header menu, clip strip (2026-11 layout) ────────────────────

def test_title_is_edited_in_place(page, studio, project):
    open_editor(page, studio)
    t = page.locator("#edClipTitle")
    t.click()
    page.keyboard.press("Control+A")
    page.keyboard.type("Заголовок з місця")
    page.keyboard.press("y")  # typing in the title never marks the clip
    page.keyboard.press("Enter")
    assert wait_for(page, lambda: clip(2)["title"] == "Заголовок з місцяy")
    assert clip(2)["review_status"] == ""
    assert "Заголовок з місцяy" in page.locator("#edList .ed-row").nth(0).inner_text()
    t.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.keyboard.press("Enter")  # an empty title is refused and the old one comes back
    assert t.text_content() == "Заголовок з місцяy"
    assert not page.errors


def test_rail_folds_and_remembers(page, studio, project):
    open_editor(page, studio)
    assert page.locator("#edMode button[data-m='table'] b").is_visible()
    page.click("#edFold")
    assert page.evaluate("document.getElementById('editor').classList.contains('rail-mini')")
    assert not page.locator("#edMode button[data-m='table'] b").is_visible()
    assert page.locator("#edStep1").inner_text() == "0/4"
    page.reload()
    page.locator(".history-item-left", has_text="Тестовий подкаст").click()
    page.locator("#editor").wait_for(state="visible")
    assert page.evaluate("document.getElementById('editor').classList.contains('rail-mini')")
    page.click("#edFold")
    assert page.locator("#edMode button[data-m='table'] b").is_visible()
    assert not page.errors


def test_reanalyze_visible_and_next_button(page, studio, project):
    open_editor(page, studio)
    assert page.locator("#edReanalyze").is_visible()
    assert "Шукати кліпи заново" in page.locator("#edReanalyze").inner_text()
    page.keyboard.press("y")
    assert wait_for(page, lambda: clip(2)["review_status"] == "keep")
    assert "1/4" in page.locator("#edStep1").inner_text()
    assert page.locator("#edCnt_keep").inner_text() == "1"
    page.click("#edGoNext")  # after a decision: on to the next clip in the strip
    page.wait_for_function("Editor.state().num === 4")
    assert not page.errors


def test_step_two_strip_lists_kept_clips(page, studio, project):
    open_editor(page, studio)
    page.keyboard.press("y")
    assert wait_for(page, lambda: clip(2)["review_status"] == "keep")
    page.click("#edMode button[data-m='vx']")
    assert page.locator("#edList .ed-row").count() == 1
    assert page.locator("#edStripTitle").inner_text().lower() == "обрані кліпи"
    assert page.locator("#edShelves").is_hidden()
    page.click("#edMode button[data-m='table']")
    assert page.locator("#edList .ed-row").count() == 3  # the «Нові» shelf
    assert not page.errors


def test_clip_thumbnail(studio, project):
    import urllib.request
    with urllib.request.urlopen(f"{studio['url']}/api/projects/{PROJECT_ID}/clips/2/thumbnail", timeout=20) as r:
        assert r.headers["Content-Type"] == "image/jpeg"
        assert len(r.read()) > 500


def test_strip_drags_with_mouse(page, studio, project):
    page.set_viewport_size({"width": 900, "height": 800})
    open_editor(page, studio)
    lst = page.locator("#edList")
    page.evaluate("document.getElementById('edList').style.width = '300px'")
    before = page.evaluate("document.getElementById('edList').scrollLeft")
    cur = page.evaluate("Editor.state().cur")
    lst.scroll_into_view_if_needed()
    box = lst.bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 250, y)
    page.mouse.down()
    page.mouse.move(box["x"] + 150, y, steps=5)
    page.mouse.move(box["x"] + 60, y, steps=5)
    page.mouse.up()
    page.wait_for_timeout(300)
    assert page.evaluate("document.getElementById('edList').scrollLeft") > before + 50
    assert page.evaluate("Editor.state().cur") == cur  # a drag does not open the clip under the mouse
    assert not page.errors


def test_favicon(studio):
    import urllib.request
    with urllib.request.urlopen(studio["url"] + "/favicon.svg") as r:
        assert r.headers["Content-Type"].startswith("image/svg")
        assert b"<svg" in r.read()


def test_vertical_tabs_more_menu_and_prev_next(page, studio, project):
    open_editor(page, studio)
    page.keyboard.press("y")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("y")
    assert wait_for(page, lambda: sum(c["review_status"] == "keep" for c in db.get_candidate_clips(PROJECT_ID)) == 2)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_function("Vertical.state().layout", timeout=20000)
    # The header speaks about step 2
    assert page.locator(".ed-stat-vx").is_visible() and not page.locator("#edShelves").is_visible()
    # One tab at a time, remembered
    vx_tab(page, "hook")
    assert page.locator("#vxHookOn").is_visible() and not page.locator("#vxStyle").is_visible()
    page.click("#edMode button[data-m='table']")
    page.click("#edMode button[data-m='vx']")
    assert page.locator("#vxHookOn").is_visible()
    # Height of the captions stays a slider on «Стиль»
    vx_tab(page, "style")
    assert page.locator("#vxPos").is_visible()
    # «Ще ▾» opens and closes
    assert page.locator("#vxZip").is_hidden()
    page.click("#vxMoreBtn")
    assert page.locator("#vxZip").is_visible() and page.locator("#vxDlClean").is_visible()
    page.keyboard.press("Escape")
    assert page.locator("#vxZip").is_hidden()
    # Previous / next kept clip
    # (the current clip may be first or last by time, so go whichever way moves)
    first = page.evaluate("Editor.state().clipIndex")
    page.click("#vxNext")
    page.wait_for_timeout(300)
    go, back = ("#vxNext", "#vxPrev")
    if page.evaluate("Editor.state().clipIndex") == first:
        go, back = ("#vxPrev", "#vxNext")
        page.click(go)
    page.wait_for_function(f"Editor.state().clipIndex !== {first}")
    page.click(back)
    page.wait_for_function(f"Editor.state().clipIndex === {first}")
    assert not page.errors

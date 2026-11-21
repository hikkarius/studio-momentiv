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


def test_open_project_shows_editor_sorted_by_score(page, studio, project):
    open_editor(page, studio)
    rows = page.locator("#edList .ed-row")
    assert rows.count() == 4
    assert "Найкращий момент" in rows.nth(0).inner_text()
    # numbers follow the place in the video: the best clip (30–50 s) is the 2nd one in time
    assert page.locator("#edClipNo").text_content().startswith("№2 · Найкращий момент")
    assert page.locator("#edList .ed-num").all_inner_texts() == ["№2", "№4", "№1", "№3"]
    assert page.locator("#edOv .c").count() == 4
    assert page.locator("#edOv .lab").all_inner_texts() == ["1", "2", "3", "4"]
    assert page.locator("#edTrack").is_visible()  # the clip track sits right under the video
    screen = page.locator("#edScreen").bounding_box()
    track = page.locator("#edTrack").bounding_box()
    assert 0 <= track["y"] - (screen["y"] + screen["height"]) < 30
    # trimmed screen: no score line next to the title, no ±0,5 / ↑↓ buttons, no bottom bar with the ZIP
    assert page.locator("#edClipMeta, [data-nudge], #edPrev, #edNext, .ed-bar, #edZip, #hwBadge").count() == 0
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

def test_keep_and_drop_with_buttons_and_keys(page, studio, project):
    open_editor(page, studio)
    page.click("#edKeep")  # best clip (#2) → keep; it stays open and the buttons turn into its status
    assert wait_for(page, lambda: clip(2)["review_status"] == "keep")
    assert page.locator("#edClipNo").text_content().startswith("№2")
    assert not page.locator("#edKeep").is_visible() and not page.locator("#edDrop").is_visible()
    assert "Беру" in page.locator("#edDecide .ed-verdict-b").inner_text()

    page.keyboard.press("ArrowDown")  # move on by hand
    assert page.locator("#edClipNo").text_content().startswith("№4")
    page.keyboard.press("n")  # second best (#4) → drop
    assert wait_for(page, lambda: clip(4)["review_status"] == "drop")

    page.click("#edFilters button[data-f='keep']")
    assert page.locator("#edList .ed-row").count() == 1
    assert "1" in page.locator("#edCnt_keep").inner_text()
    assert page.locator("#edOv .c.keep").count() == 1
    assert page.locator("#edOv .c.drop").count() == 1

    # «Змінити» on a kept clip removes the mark and brings the buttons back
    page.click("#edFilters button[data-f='all']")
    page.locator("#edList .ed-row", has_text="Найкращий момент").click()
    page.click("#edDecide .ed-change")
    assert wait_for(page, lambda: clip(2)["review_status"] == "")
    assert page.locator("#edKeep").is_visible()
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
    page.wait_for_function("document.getElementById('edPlay').innerText.includes('Пауза')")
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
    assert "Новий заголовок" in page.locator("#edClipNo").text_content()


def test_download_single_clip_and_zip(page, studio, project):
    open_editor(page, studio)
    page.click("#edTabs button[data-tab='dl']")
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
    page.click("#edMode button[data-m='vx']")  # the ZIP of kept clean clips lives on «Вертикальні кліпи»
    with page.expect_download(timeout=30000) as dl:
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

def test_vertical_preview_settings_and_download(page, studio, project):
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    # The test video has no faces, so «Авто» shows the whole frame on a blurred background.
    page.wait_for_function("Vertical.state().layout === 'blur'", timeout=20000)
    page.wait_for_function("Vertical.state().words > 0")
    assert page.locator("#vxNote").is_visible()

    # Pick a caption style and framing: saved per clip.
    page.click("#vxStyle button[data-v='one']")
    page.click("#vxFrame button[data-v='blur']")
    idx = page.evaluate("Editor.state().clipIndex")
    assert wait_for(page, lambda: clip(idx).get("vertical_opts", {}).get("style") == "one"
                    and clip(idx)["vertical_opts"].get("frame") == "blur")

    # Fix a word in the captions.
    page.locator("#vxWords button").first.click()
    page.fill("#vxWinput", "ВИПРАВЛЕНО")
    page.click("#vxWsave")
    assert wait_for(page, lambda: any(e.get("w") == "ВИПРАВЛЕНО" for e in clip(idx)["vertical_opts"].get("edits", {}).values()))
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
    page.click("#edTabs button[data-tab='dl']")
    assert page.locator("#edMp4").is_visible() and page.locator("#edGoVx").is_visible()
    page.click("#edGoVx")
    assert page.locator("#vxPhone").is_visible()
    assert not page.errors


def test_vertical_title_size_colour_and_scrub(page, studio, project):
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_function("Vertical.state().layout", timeout=20000)
    idx = page.evaluate("Editor.state().clipIndex")
    page.check("#vxHookOn")
    page.fill("#vxHookText", "МІЙ ЗАГОЛОВОК")
    page.keyboard.press("Enter")
    page.click("#vxHookStyle button[data-v='black']")
    page.click("#vxHookDur button[data-v='all']")
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
    page.click("#vxKeep")
    assert wait_for(page, lambda: clip(idx)["review_status"] == "keep")
    assert page.evaluate("Editor.state().clipIndex") == idx
    assert page.locator("#vxDecide .ed-verdict").is_visible() and not page.locator("#vxKeep").is_visible()
    # Rounded plates with a white edge, for captions and for the title
    page.click("#vxRound button[data-v='1']")
    page.click("#vxStroke button[data-v='FFFFFF']")
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
        page.click("#vxZip")
    names = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), "rb").read())).namelist()
    assert len(names) == 1 and names[0].endswith("_9x16_subs.mp4")
    assert not page.errors

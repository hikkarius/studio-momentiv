"""
tests/e2e/test_focus.py — keyboard focus and element states across the studio: Space presses the focused
button (not the player), the focus survives re-renders (clip strip, status menu, vertical style tiles),
Esc returns it to the button that opened a menu, arrows move inside menus and tab bars, and the mouse
keeps the editor's hotkeys.
"""
from .test_ui import keep_first_clip, open_editor

ACTIVE = """() => { const e = document.activeElement; if (!e || e === document.body) return 'BODY';
  return (e.id ? '#' + e.id : '') + '|' + (e.dataset.i ?? e.dataset.act ?? e.dataset.v ?? e.dataset.f ?? e.dataset.tab ?? e.dataset.t ?? e.dataset.m ?? '') +
  '|' + e.className + '|' + (e.matches(':focus-visible') ? 'ring' : 'no-ring'); }"""


def active(page):
    return page.evaluate(ACTIVE)


def kfocus(page, selector):
    """Focus as a keyboard user would (the browser then shows :focus-visible on it)."""
    page.keyboard.press("Shift")
    page.locator(selector).first.focus()
    assert active(page).endswith("|ring"), active(page)


def paused(page):
    return page.evaluate("document.getElementById('edVideo').paused")


def test_space_presses_focused_button_not_the_player(page, studio, project):
    open_editor(page, studio)
    kfocus(page, "#edShelves .ed-shelf[data-f='drop']")
    page.keyboard.press(" ")
    page.wait_for_timeout(300)
    assert page.locator("#edShelves .ed-shelf[data-f='drop']").get_attribute("aria-pressed") == "true"
    assert paused(page), "Space on a focused button must not start the video"
    kfocus(page, "#edSort button[data-s='time']")
    page.keyboard.press(" ")
    assert page.locator("#edSort button[data-s='time']").get_attribute("aria-pressed") == "true"
    assert paused(page)


def test_space_after_mouse_click_still_plays(page, studio, project):
    """A clicked button keeps no ring, and Space keeps its editor job: play/pause."""
    open_editor(page, studio)
    page.click("#edSort button:not([aria-pressed='true'])")
    assert paused(page)
    page.keyboard.press(" ")
    page.wait_for_function("!document.getElementById('edVideo').paused")


def test_focus_stays_on_clip_strip_after_pick_and_arrows(page, studio, project):
    open_editor(page, studio)
    kfocus(page, "#edList .ed-row:nth-child(2)")
    want = page.locator("#edList .ed-row:nth-child(2)").get_attribute("data-i")
    page.keyboard.press("Enter")
    page.wait_for_function(f"document.querySelector('#edList .ed-row[data-i=\"{want}\"]').getAttribute('aria-current') === 'true'")
    assert active(page).split("|")[1] == want, active(page)
    page.keyboard.press("ArrowDown")  # next clip; the focus follows it
    page.wait_for_timeout(400)
    cur = page.locator("#edList .ed-row[aria-current='true']").get_attribute("data-i")
    assert cur != want
    assert active(page).split("|")[1] == cur, active(page)
    page.keyboard.press(" ")  # Space on a clip row opens it instead of playing
    page.wait_for_timeout(300)
    assert paused(page)


def test_status_menu_returns_focus_to_its_button(page, studio, project):
    open_editor(page, studio)
    kfocus(page, "#edStatus")
    page.keyboard.press("Enter")
    page.locator("#edStatusMenu").wait_for(state="visible")
    page.keyboard.press("Escape")
    assert page.locator("#edStatusMenu").is_hidden()
    assert active(page).startswith("#edStatus|"), active(page)
    # pick a status with the keyboard: the menu closes, the focus is on «Статус ▾» again (not on <body>)
    page.keyboard.press("Enter")
    page.locator("#edStatusMenu button[data-s='reserve'], #edStatusMenu button[data-s='keep']").last.focus()
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    assert active(page) != "BODY"


def test_tabs_move_with_arrows(page, studio, project):
    open_editor(page, studio)
    kfocus(page, "#edMode button[data-m='table']")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("document.querySelector('#edMode button[data-m=\"vx\"]').getAttribute('aria-selected') === 'true'")


def test_vertical_style_keeps_focus(page, studio, project):
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.click("#vxTabs button[data-t='style']")
    page.locator("#vxStyle button").nth(2).wait_for()
    v = page.locator("#vxStyle button").nth(2).get_attribute("data-v")
    kfocus(page, f"#vxStyle button[data-v='{v}']")
    page.keyboard.press("Enter")
    page.wait_for_function(f"document.querySelector('#vxStyle button[data-v=\"{v}\"]').getAttribute('aria-pressed') === 'true'")
    assert active(page).split("|")[1] == v, active(page)


def test_home_card_menu_keyboard(page, studio, project):
    page.goto(studio["url"] + "/")
    card = page.locator(".video-card", has_text="Тестовий подкаст")
    card.wait_for()
    page.keyboard.press("Shift")
    card.locator(".vc-dots").focus()
    page.keyboard.press("Enter")
    menu = card.locator(".vc-menu")
    menu.wait_for(state="visible")
    first = active(page)
    page.keyboard.press("ArrowDown")
    assert active(page) != first, "ArrowDown moves to the next menu item"
    page.keyboard.press("Escape")
    assert menu.is_hidden()
    assert "vc-dots" in active(page), active(page)
    # Tab out of an open menu closes it
    page.keyboard.press("Enter")
    menu.wait_for(state="visible")
    for _ in range(4):
        page.keyboard.press("Tab")
    assert menu.is_hidden()


def test_home_card_opens_with_space(page, studio, project):
    page.goto(studio["url"] + "/")
    left = page.locator(".history-item-left", has_text="Тестовий подкаст")
    left.wait_for()
    page.keyboard.press("Shift")
    left.focus()
    page.keyboard.press(" ")
    page.locator("#editor").wait_for(state="visible")


def test_one_focus_ring_everywhere(page, studio, project):
    """Home, settings and the editor draw the same ring (2px, --focus-color)."""
    page.goto(studio["url"] + "/")
    page.locator("#btnOpenSettings").wait_for()
    kfocus(page, "#btnOpenSettings")
    page.wait_for_timeout(400)  # .btn animates every property, the outline too
    ring = page.evaluate("getComputedStyle(document.activeElement).outlineStyle + ' ' + getComputedStyle(document.activeElement).outlineColor")
    accent = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()")
    assert ring.startswith("solid"), ring
    page.evaluate("Settings.open()")
    page.locator("#settingsScreen").wait_for(state="visible")
    kfocus(page, ".settings-tablist [role='tab']")
    page.wait_for_timeout(400)
    ring2 = page.evaluate("getComputedStyle(document.activeElement).outlineStyle + ' ' + getComputedStyle(document.activeElement).outlineColor")
    assert ring2 == ring, (ring, ring2, accent)
    page.keyboard.press("ArrowDown")
    assert page.locator(".settings-tablist [role='tab'][data-tab='ai']").get_attribute("aria-selected") == "true"
    assert not page.errors


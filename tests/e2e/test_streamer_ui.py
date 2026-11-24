"""
tests/e2e/test_streamer_ui.py — «Стрім: вебка + гра» in the vertical screen: the card, layouts,
the «Рамки на кадрі» view and dragging the webcam frame (the webcam search itself is faked here;
tests/test_streamer.py checks it on real frames).
"""
import json

import pytest

import streamer
from backend import server
from tests.e2e.conftest import PROJECT_ID
from tests.e2e.test_ui import keep_first_clip, open_editor, vx_tab

CAM = [0.02, 0.62, 0.3, 0.34]


@pytest.fixture(autouse=True)
def _no_stream_frame_left():
    """The e2e project id is shared: a leftover stream.json would switch later tests' clips to «Стрім»."""
    server._stream_path(PROJECT_ID).unlink(missing_ok=True)
    yield
    server._stream_path(PROJECT_ID).unlink(missing_ok=True)


def test_stream_mode_card_layouts_and_dragging(page, studio, project, monkeypatch):
    found = {"n": 0}

    def fake_detect(source, **k):
        found["n"] += 1
        return {"cam": CAM, "face": [0.17, 0.78], "w": 320, "h": 180, "note": ""}

    monkeypatch.setattr(streamer, "detect_webcam", fake_detect)
    server._stream_path(PROJECT_ID).unlink(missing_ok=True)
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    # a stream recording: the webcam is found, «Стрім» switches on by itself and a window says so
    page.locator("#vxStreamModal").wait_for(state="visible")
    page.wait_for_function("document.getElementById('vxSmTitle').textContent.includes('Схоже, це запис стріму')", timeout=20000)
    assert "зліва" in page.locator("#vxSmSteps").inner_text() and page.locator("#vxSmThumb").is_visible()
    page.click("#vxSmOk")
    assert page.locator("#vxStreamModal").is_hidden()
    vx_tab(page, "frame")
    page.wait_for_function("Vertical.state().layout === 'stream' && !!Vertical.state().cam", timeout=20000)
    assert page.locator("#vxFrame button[data-v='stream']").get_attribute("aria-pressed") == "true"
    assert page.locator("#vxStream").is_visible()
    assert "Вебку знайдено" in page.locator("#vxCamSt").inner_text()
    assert page.evaluate("Vertical.state().stream.layout") == "top"
    assert page.locator("#vxLay button[data-v='top']").get_attribute("aria-pressed") == "true"

    # the other layouts
    page.click("#vxLay button[data-v='pip']")
    page.wait_for_function("Vertical.state().stream.layout === 'pip'")
    page.click("#vxLay button[data-v='full']")
    page.wait_for_function("Vertical.state().stream.layout === 'full'")
    page.click("#vxLay button[data-v='top']")
    page.wait_for_function("Vertical.state().stream.layout === 'top'")

    # «Рамки на кадрі»: the webcam frame sits over the picture, dragging a corner saves a new frame
    page.click("#vxView button[data-v='frame']")
    assert page.locator("#vxFed").is_visible() and page.locator("#vxPhone").is_hidden()
    assert page.locator("#vxCamB").is_visible() and page.locator("#vxGameB").is_visible()
    h = page.locator("#vxCamB i[data-h='se']").bounding_box()
    page.mouse.move(h["x"] + 8, h["y"] + 8)
    page.mouse.down()
    page.mouse.move(h["x"] + 60, h["y"] - 30, steps=4)
    page.mouse.up()
    page.wait_for_function(f"Vertical.state().cam && Vertical.state().cam[2] > {CAM[2] + 0.02}")
    assert "вручну" in page.locator("#vxCamSt").inner_text()
    # the game area moves left-right
    g = page.locator("#vxGameB").bounding_box()
    page.mouse.move(g["x"] + g["width"] / 2, g["y"] + g["height"] / 2)
    page.mouse.down()
    page.mouse.move(g["x"] + g["width"] / 2 + 60, g["y"] + g["height"] / 2, steps=4)
    page.mouse.up()
    page.wait_for_function("Vertical.state().stream.parts[1].src[0] > 0.3")

    # «Без вебки»: the game only
    page.click("#vxCamOff")
    page.wait_for_function("Vertical.state().stream.layout === 'game'")
    assert "Вебку вимкнено" in page.locator("#vxCamSt").inner_text()
    assert found["n"] == 1

    # back to «Авто»: the stream controls go away and the phone comes back
    page.click("#vxFrame button[data-v='auto']")
    page.wait_for_function("Vertical.state().layout !== 'stream'", timeout=20000)
    assert page.locator("#vxStream").is_hidden() and page.locator("#vxPhone").is_visible()
    page.wait_for_function("fetch('/api/projects/%s/stream?detect=false').then((r) => r.json()).then((s) => s.use === false)" % PROJECT_ID)
    # opening the video again does not switch it back, and the window is not shown twice
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_timeout(800)
    assert page.evaluate("Vertical.state().opts.frame") != "stream"
    assert page.locator("#vxStreamModal").is_hidden()
    assert page.errors == []


def test_stream_choice_is_remembered_per_video(page, studio, project, monkeypatch):
    monkeypatch.setattr(streamer, "detect_webcam",
                        lambda source, **k: {"cam": None, "face": None, "w": 320, "h": 180, "note": "Вебку не знайдено."})
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    vx_tab(page, "frame")
    page.click("#vxFrame button[data-v='stream']")
    page.wait_for_function("Vertical.state().layout === 'stream'", timeout=20000)
    # the template for other videos keeps its own frame, this video remembers «Стрім»
    tpl = page.evaluate("JSON.parse(localStorage.getItem('studio-vertical-template'))")
    assert tpl["frame"] != "stream"
    assert PROJECT_ID in page.evaluate("JSON.parse(localStorage.getItem('studio-stream-videos'))")
    assert page.errors == []


def _same_plan(js, py):
    assert js["layout"] == py["layout"] and js["bg"] == py["bg"] and js["divider"] == py["divider"]
    assert js["pos"] == py["pos"] and js["hook_pos"] == py["hook_pos"]
    if py["nick"]:  # the width comes from the browser's own font metrics: a few px apart is fine
        for key in ("text", "style", "y"):
            assert js["nick"][key] == py["nick"][key]
        assert abs(js["nick"]["x"] - py["nick"]["x"]) <= 12 and abs(js["nick"]["w"] - py["nick"]["w"]) <= 12
    else:
        assert js["nick"] is None
    for a, b in zip(js["parts"], py["parts"]):
        assert a["role"] == b["role"] and a["radius"] == b["radius"] and a["color"] == b["color"]
        assert all(abs(x - y) <= 2 for x, y in zip(a["dst"], b["dst"])), (a, b)
        assert all(abs(x - y) <= 0.002 for x, y in zip(a["src"], b["src"])), (a, b)


def test_stream_sizes_zoom_phone_drag_and_styling(page, studio, project, monkeypatch):
    monkeypatch.setattr(streamer, "detect_webcam",
                        lambda source, **k: {"cam": CAM, "face": [0.17, 0.78], "w": 320, "h": 180, "note": ""})
    server._stream_path(PROJECT_ID).unlink(missing_ok=True)
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_function("document.getElementById('vxSmTitle').textContent.includes('Схоже')", timeout=20000)
    page.click("#vxSmOk")
    vx_tab(page, "frame")
    page.wait_for_function("Vertical.state().layout === 'stream' && !!Vertical.state().cam", timeout=20000)
    page.wait_for_timeout(300)
    look = lambda: page.evaluate("Vertical.state().look")  # noqa: E731
    saved = lambda: json.loads(server._stream_path(PROJECT_ID).read_text(encoding="utf-8"))["look"]  # noqa: E731

    box = page.locator("#vxCanvas").bounding_box()
    k = box["height"] / 1920
    at = lambda x, y: (box["x"] + x * k, box["y"] + y * k)  # noqa: E731  phone px → screen

    # a click on the webcam selects it (a box with handles), it does not start the video
    page.mouse.click(*at(540, 300))
    assert page.locator("#vxSel").is_visible() and "Вебка" in page.locator("#vxSelName").inner_text()
    assert page.evaluate("document.getElementById('vxPhone').classList.contains('playing')") is False
    # pulling a corner away from the middle zooms the webcam in
    ch = page.evaluate("Vertical.state().stream.parts[0].dst[3]")
    page.mouse.move(*at(1076, ch - 4))
    page.mouse.down()
    page.mouse.move(*at(1076 + 200, ch + 200), steps=6)
    page.mouse.up()
    page.wait_for_function("Vertical.state().look.cam_zoom > 1.2")
    page.wait_for_timeout(300)
    assert saved()["cam_zoom"] > 1.2
    assert "%" in page.locator("#vxSelPct").inner_text()
    # «↺» on the box: the webcam as at first
    page.click("#vxSelReset")
    page.wait_for_function("Vertical.state().look.cam_zoom === 1")

    # the game: click selects it, the wheel zooms it
    page.mouse.click(*at(540, 1400))
    page.wait_for_function("document.getElementById('vxSel').classList.contains('game')")
    page.mouse.move(*at(540, 1400))
    page.mouse.wheel(0, -100)
    page.mouse.wheel(0, -100)
    page.wait_for_function("Vertical.state().look.game_zoom >= 1.15")
    page.wait_for_timeout(800)
    assert saved()["game_zoom"] >= 1.15

    # the edge between webcam and game sets the webcam's height
    ch = page.evaluate("Vertical.state().stream.parts[0].dst[3]")
    page.mouse.move(*at(300, ch))
    page.mouse.down()
    page.mouse.move(*at(300, ch + 250), steps=5)
    page.mouse.up()
    page.wait_for_function(f"Math.abs(Vertical.state().look.share - {(ch + 250) / 1920:.3f}) < 0.02")

    # the preview twin draws exactly what the server will render
    page.wait_for_function("document.fonts.status === 'loaded'")
    for lk in ({"layout": "top", "divider": True, "frame": "gold", "nick": "lebiga", "cam_zoom": 1.7, "cam_fx": 0.2, "cam_fy": 0.3},
               {"layout": "full", "share": 0.3, "game_zoom": 1.3, "game_x": 0.8, "nick": "lebiga", "nick_style": "tape"},
               {"layout": "pip", "pip_w": 1.0, "pip_x": 1, "pip_y": 0.4, "round": True, "frame": "none"},
               {"layout": "pip", "pip_w": 0.6, "pip_x": 1, "pip_y": 1, "nick": "lebiga", "nick_style": "neon"}):
        full = {**streamer.LOOK_DEFAULT, **lk}
        js = page.evaluate("(lk) => Vertical.twin(lk)", full)
        _same_plan(js, streamer.plan(CAM, [0.17, 0.78], 320, 180, full))

    # «Гра на весь екран»: drag the window anywhere, pull its corner to the full width
    page.click("#vxLay button[data-v='pip']")
    page.wait_for_function("Vertical.state().stream.layout === 'pip'")
    assert page.locator("#vxRoundRow").is_visible() and page.locator("#vxDivRow").is_hidden()
    c = page.evaluate("Vertical.state().stream.parts[1].dst")
    page.mouse.click(*at(c[0] + c[2] / 2, c[1] + c[3] / 2))
    page.mouse.move(*at(c[0] + c[2] / 2, c[1] + c[3] / 2))
    page.mouse.down()
    page.mouse.move(*at(c[0] + c[2] / 2, c[1] + c[3] / 2 + 600), steps=5)
    page.mouse.up()
    page.wait_for_function(f"Vertical.state().stream.parts[1].dst[1] > {c[1] + 500}")
    c = page.evaluate("Vertical.state().stream.parts[1].dst")
    page.mouse.move(*at(c[0] + c[2] - 2, c[1] + c[3] - 2))
    page.mouse.down()
    page.mouse.move(*at(1300, c[1] + c[3] + 200), steps=6)
    page.mouse.up()
    page.wait_for_function("Vertical.state().stream.parts[1].dst[2] >= 1070")
    assert "ширини" in page.locator("#vxSelPct").inner_text()

    # styling and the nick: pick a style, drag the nick
    page.click("#vxCamRound button[data-v='1']")
    page.click("#vxCamFrame button[data-v='gold']")
    page.fill("#vxNick", "lebiga")
    page.press("#vxNick", "Enter")
    page.click("#vxNickStyle button[data-v='tape']")
    page.wait_for_function("Vertical.state().stream.nick && Vertical.state().stream.nick.style === 'tape'")
    nk = page.evaluate("Vertical.state().stream.nick")
    page.mouse.move(*at(nk["x"] + nk["w"] / 2, nk["y"] + nk["h"] / 2))
    page.mouse.down()
    to_y = 300 if nk["y"] > 900 else 1500  # wherever there is room
    page.mouse.move(*at(nk["x"] + nk["w"] / 2 + 100, to_y), steps=5)
    page.mouse.up()
    page.wait_for_function(f"Math.abs(Vertical.state().stream.nick.y + Vertical.state().stream.nick.h / 2 - {to_y}) < 30")
    page.wait_for_timeout(300)
    s = saved()
    assert s["round"] is True and s["frame"] == "gold" and s["nick"] == "lebiga" and s["layout"] == "pip"
    assert s["nick_style"] == "tape" and abs(s["nick_y"] * 1920 + nk["h"] / 2 - to_y) < 30

    # one clip on its own: the video's look stays
    page.check("#vxLookOwn")
    page.click("#vxLay button[data-v='full']")
    page.wait_for_function("Vertical.state().stream.layout === 'full'")
    page.wait_for_timeout(700)
    assert saved()["layout"] == "pip"
    assert page.evaluate("Vertical.state().opts.stream_look.layout") == "full"
    page.uncheck("#vxLookOwn")
    page.wait_for_function("Vertical.state().stream.layout === 'pip'")

    # «↺ Як було»: sizes back, styling kept
    page.click("#vxLookReset")
    page.wait_for_function("Vertical.state().look.game_zoom === 1 && Vertical.state().look.pip_w === 0.48")
    assert look()["nick"] == "lebiga" and look()["layout"] == "pip"
    assert page.errors == []

"""
tests/e2e/test_streamer_ui.py — «Стрім: вебка + гра» in the vertical screen: the card, layouts,
the «Рамки на кадрі» view and dragging the webcam frame (the webcam search itself is faked here;
tests/test_streamer.py checks it on real frames).
"""
import json

import streamer
from backend import server
from tests.e2e.conftest import PROJECT_ID
from tests.e2e.test_ui import keep_first_clip, open_editor, vx_tab

CAM = [0.02, 0.62, 0.3, 0.34]


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
    vx_tab(page, "frame")
    # a stream recording: the webcam is found and «Стрім» switches on by itself
    page.wait_for_function("Vertical.state().layout === 'stream' && !!Vertical.state().cam", timeout=20000)
    assert "запис стріму" in page.locator("#edToast").inner_text()
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
    # opening the video again does not switch it back
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    page.wait_for_timeout(800)
    assert page.evaluate("Vertical.state().opts.frame") != "stream"
    assert page.errors == []


def test_stream_choice_is_remembered_per_video(page, studio, project, monkeypatch):
    monkeypatch.setattr(streamer, "detect_webcam",
                        lambda source, **k: {"cam": CAM, "face": None, "w": 320, "h": 180, "note": ""})
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
    assert js["pos"] == py["pos"] and js["hook_pos"] == py["hook_pos"] and js["nick"] == py["nick"]
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
    vx_tab(page, "frame")
    page.click("#vxFrame button[data-v='stream']")
    page.wait_for_function("Vertical.state().layout === 'stream' && !!Vertical.state().cam", timeout=20000)
    look = lambda: page.evaluate("Vertical.state().look")  # noqa: E731
    saved = lambda: json.loads(server._stream_path(PROJECT_ID).read_text(encoding="utf-8"))["look"]  # noqa: E731

    # sliders: the phone follows at once, the video keeps it
    page.locator("#vxCamSize").fill("50")
    page.wait_for_function("Vertical.state().stream.parts[0].dst[3] === 960")
    page.wait_for_function("Vertical.state().look.share === 0.5")
    page.locator("#vxGameZoom").fill("200")
    page.wait_for_function("Vertical.state().look.game_zoom === 2")
    assert saved()["share"] == 0.5 and saved()["game_zoom"] == 2

    # the preview twin draws exactly what the server will render
    for lk in ({"layout": "top", "divider": True, "frame": "gold", "nick": "lebiga", "cam_zoom": 1.7, "cam_fx": 0.2, "cam_fy": 0.3},
               {"layout": "full", "share": 0.3, "game_zoom": 1.3, "game_x": 0.8},
               {"layout": "pip", "pip_w": 0.6, "pip_x": 1, "pip_y": 1, "round": True, "frame": "none"}):
        full = {**streamer.LOOK_DEFAULT, **lk}
        js = page.evaluate("(lk) => Vertical.twin(lk)", full)
        _same_plan(js, streamer.plan(CAM, [0.17, 0.78], 320, 180, full))

    # dragging the seam between webcam and game on the phone
    box = page.locator("#vxCanvas").bounding_box()
    k = box["height"] / 1920
    seam_y = box["y"] + 960 * k
    page.mouse.move(box["x"] + box["width"] / 2, seam_y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2, seam_y - 300 * k, steps=5)
    page.mouse.up()
    page.wait_for_function("Math.abs(Vertical.state().look.share - 0.344) < 0.02")
    # the wheel over the game zooms it
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 1500 * k)
    page.mouse.wheel(0, -100)
    page.mouse.wheel(0, -100)
    page.wait_for_function("Vertical.state().look.game_zoom >= 2.15")
    page.wait_for_timeout(800)
    assert saved()["game_zoom"] >= 2.15
    assert page.evaluate("document.getElementById('vxPhone').classList.contains('playing')") is False  # a drag is not a click

    # the window layout: corner buttons, styling controls
    page.click("#vxLay button[data-v='pip']")
    page.wait_for_function("Vertical.state().stream.layout === 'pip'")
    assert page.locator("#vxPipRow").is_visible() and page.locator("#vxRoundRow").is_visible()
    assert page.locator("#vxDivRow").is_hidden()
    page.click("#vxPipCorner button[data-v='br']")
    page.wait_for_function("(() => { const c = Vertical.state().stream.parts[1].dst; return c[0] + c[2] > 1000 && c[1] + c[3] > 1800; })()")
    page.click("#vxCamRound button[data-v='1']")
    page.click("#vxCamFrame button[data-v='gold']")
    page.fill("#vxNick", "lebiga")
    page.press("#vxNick", "Enter")
    page.wait_for_function("Vertical.state().stream.nick && Vertical.state().stream.nick.text === 'lebiga'")
    page.wait_for_timeout(300)
    s = saved()
    assert s["round"] is True and s["frame"] == "gold" and s["nick"] == "lebiga" and s["layout"] == "pip"

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

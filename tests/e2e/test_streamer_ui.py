"""
tests/e2e/test_streamer_ui.py — «Стрім: вебка + гра» in the vertical screen: the card, layouts,
the «Рамки на кадрі» view and dragging the webcam frame (the webcam search itself is faked here;
tests/test_streamer.py checks it on real frames).
"""
import streamer
from tests.e2e.conftest import PROJECT_ID
from tests.e2e.test_ui import keep_first_clip, open_editor, vx_tab

CAM = [0.02, 0.62, 0.3, 0.34]


def test_stream_mode_card_layouts_and_dragging(page, studio, project, monkeypatch):
    found = {"n": 0}

    def fake_detect(source, **k):
        found["n"] += 1
        return {"cam": CAM, "face": [0.17, 0.78], "w": 320, "h": 180, "note": ""}

    monkeypatch.setattr(streamer, "detect_webcam", fake_detect)
    keep_first_clip()
    open_editor(page, studio)
    page.click("#edMode button[data-m='vx']")
    vx_tab(page, "frame")
    assert page.locator("#vxStream").is_hidden() and page.locator("#vxView").is_hidden()

    page.click("#vxFrame button[data-v='stream']")
    page.wait_for_function("Vertical.state().layout === 'stream' && !!Vertical.state().cam", timeout=20000)
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

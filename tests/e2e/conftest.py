"""
tests/e2e/conftest.py — real browser tests of the studio UI (home page + «Монтажний стіл»).

Starts the FastAPI app with an isolated database and a generated test video, then drives it
with Playwright (Chromium). Skipped when Playwright or ffmpeg is not installed.

Install once:   pip install playwright   and   python -m playwright install chromium
Run only these: python -m pytest tests/e2e
"""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from backend import db, job_runner, server  # noqa: E402

PROJECT_ID = "e2e_podcast"
VIDEO_SEC = 120.0

# Clip layout: (start, end, score, title). Highest score first after sorting = clip_index 2.
CLIPS = [
    (5.0, 22.0, 80, "Перший момент"),
    (30.0, 50.0, 95, "Найкращий момент"),
    (60.0, 78.0, 70, "Третій момент"),
    (85.0, 104.0, 88, "Четвертий момент"),
]


def _words():
    """One word every 0.5 s across the whole video, with a capitalised word every 4 s."""
    out = []
    t = 0.0
    n = 0
    while t < VIDEO_SEC - 0.5:
        word = f"Слово{n}" if n % 8 == 0 else f"слово{n}"
        out.append({"start": round(t, 2), "end": round(t + 0.4, 2), "word": word})
        t += 0.5
        n += 1
    return out


@pytest.fixture(autouse=True)
def isolate_test_database():
    """Overrides the root autouse fixture: the e2e server keeps one database for the whole module."""
    yield None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def studio(tmp_path_factory):
    """Runs the real app on a free port with test data; yields its base URL."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg is not installed")
    import uvicorn
    import subtitles_generator

    root = tmp_path_factory.mktemp("e2e_studio")
    uploads, output = root / "web_uploads", root / "web_output"
    uploads.mkdir()
    output.mkdir()
    video = uploads / "podcast.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10",
         "-f", "lavfi", "-i", "sine=frequency=300", "-t", str(VIDEO_SEC),
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "32k", str(video)],
        check=True,
    )

    saved = {
        "db": db.DB_PATH, "skip": db.SKIP_MISSING_SOURCE_FILES,
        "up": server.UPLOADS_DIR, "out": server.OUTPUT_DIR, "jup": job_runner.UPLOADS_DIR, "jout": job_runner.OUTPUT_DIR,
        "ltc": server.load_transcript_cache, "sltc": subtitles_generator.load_transcript_cache,
    }
    db.DB_PATH = str(root / "studio.db")
    db.SKIP_MISSING_SOURCE_FILES = False
    server.UPLOADS_DIR, server.OUTPUT_DIR = uploads, output
    job_runner.UPLOADS_DIR, job_runner.OUTPUT_DIR = str(uploads), str(output)
    words = _words()
    server.load_transcript_cache = lambda p, **kw: ([], words, "uk")
    subtitles_generator.load_transcript_cache = lambda p, **kw: ([], words, "uk")
    # No real Gemini: the «Заголовок» tab gets three fixed hook titles
    # (only the client is replaced, so the unit tests of title_ideas, which bring their own client, are unaffected)
    import json as _json
    from types import SimpleNamespace
    import title_ideas
    ideas = _json.dumps([
        {"kind": "шок", "text": "90% людей не знають цього"},
        {"kind": "питання", "text": "Чи можна стати мільйонером чесно?"},
        {"kind": "інтрига", "text": "Він сказав те, що всі приховують"},
    ], ensure_ascii=False)
    fake_models = SimpleNamespace(generate_content=lambda **kw: SimpleNamespace(text=ideas))
    saved["ideas"] = title_ideas.get_gemini_client
    title_ideas.get_gemini_client = lambda: SimpleNamespace(models=fake_models)
    db.init_db()
    # The test video is no stream: skip the real webcam search the editor starts in the background
    import streamer
    saved["detect"] = streamer.detect_webcam
    streamer.detect_webcam = lambda source, **k: {"cam": None, "face": None, "w": 320, "h": 180, "note": "Вебку не знайдено."}
    # A key is "saved", so the first-run «встав ключ Gemini» screen stays closed in these tests
    saved["key"] = os.environ.get("GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "e2e-fake-key-" + "x" * 30

    port = _free_port()
    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)

    yield {"url": f"http://127.0.0.1:{port}", "video": str(video)}

    srv.should_exit = True
    th.join(timeout=5)
    db.DB_PATH, db.SKIP_MISSING_SOURCE_FILES = saved["db"], saved["skip"]
    server.UPLOADS_DIR, server.OUTPUT_DIR = saved["up"], saved["out"]
    job_runner.UPLOADS_DIR, job_runner.OUTPUT_DIR = saved["jup"], saved["jout"]
    server.load_transcript_cache = saved["ltc"]
    title_ideas.get_gemini_client = saved["ideas"]
    if saved["key"] is None:
        os.environ.pop("GEMINI_API_KEY", None)
    else:
        os.environ["GEMINI_API_KEY"] = saved["key"]
    subtitles_generator.load_transcript_cache = saved["sltc"]
    streamer.detect_webcam = saved["detect"]


@pytest.fixture
def project(studio):
    """A fresh ready project with 4 unreviewed clips for every test."""
    if db.get_project(PROJECT_ID):
        db.delete_project(PROJECT_ID)
    db.create_project(PROJECT_ID, "Тестовий подкаст", studio["video"], VIDEO_SEC)
    db.save_candidate_clips(PROJECT_ID, [
        {"start": s, "end": e, "score": sc, "title": t, "reason": "Сильний хук.", "caption": f"Опис: {t}", "hashtags": ["#подкаст"]}
        for s, e, sc, t in CLIPS
    ])
    db.update_project_status(PROJECT_ID, "ready")
    return PROJECT_ID


def _launch(pw):
    kw = {}
    path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH")
    if path:
        kw["executable_path"] = path
    try:
        return pw.chromium.launch(**kw)
    except Exception:
        fallback = Path("/opt/pw-browsers/chromium")
        if not path and fallback.exists():
            return pw.chromium.launch(executable_path=str(fallback))
        pytest.skip("Chromium for Playwright is not installed (python -m playwright install chromium)")


@pytest.fixture(scope="module")
def browser():
    # module scope: Playwright's sync API keeps an asyncio loop running while open, which would
    # break later (non-browser) tests that start their own event loop
    with playwright_api.sync_playwright() as pw:
        b = _launch(pw)
        yield b
        b.close()


@pytest.fixture
def page(browser, studio):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
    p = ctx.new_page()
    p.errors = []
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    p.set_default_timeout(8000)
    yield p
    ctx.close()

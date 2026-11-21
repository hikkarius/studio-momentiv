"""Screenshots and a walkthrough video of the studio for the friends guide.

Starts the studio on a free port with a generated demo podcast, an isolated database and folders,
a fake transcript and no Gemini key (so the first-run screen shows), then drives it with Playwright.

    python scripts/guide_capture.py OUT_DIR --shots    # 10 PNG screenshots at 2x (01-welcome … 10-zip)
    python scripts/guide_capture.py OUT_DIR            # ~27 s video into OUT_DIR/raw/*.webm

Options: --work DIR (temp studio data, default: system temp), --chromium PATH (browser binary).
See .claude/skills/guide-screenshots/SKILL.md for setup and post-processing.
"""
import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("out", type=Path)
ap.add_argument("--shots", action="store_true", help="screenshots instead of a video")
ap.add_argument("--work", type=Path, default=Path(tempfile.gettempdir()) / "studio_guide_capture")
ap.add_argument("--chromium", default=os.environ.get("CHROMIUM_PATH")
                or ("/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None))
args = ap.parse_args()

OUT = args.out.resolve()
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(REPO))
os.chdir(REPO)
root = args.work.resolve()
shutil.rmtree(root, ignore_errors=True)
uploads, output = root / "web_uploads", root / "web_output"
uploads.mkdir(parents=True)
output.mkdir()
os.environ["SHORTS_LOG_FILE"] = str(root / "guide.log")
os.environ.pop("GEMINI_API_KEY", None)

import uvicorn  # noqa: E402

import config_loader  # noqa: E402
import subtitles_generator  # noqa: E402
from backend import app_update, db, job_runner, server  # noqa: E402

# Demo video: gradient background, title, running timecode, quiet tone.
video = uploads / "podcast.mp4"
font = (REPO / "frontend/fonts/Rubik-Bold.ttf").as_posix().replace(":", "\\:")
VID = 180.0
vf = (f"drawtext=fontfile='{font}':text='ПОДКАСТ':fontsize=90:fontcolor=white:x=(w-tw)/2:y=h/2-110,"
      f"drawtext=fontfile='{font}':text='епізод 12 · розмова про гроші':fontsize=38:fontcolor=0xE6C068:x=(w-tw)/2:y=h/2+10,"
      f"drawtext=fontfile='{font}':text='%{{pts\\:hms}}':fontsize=30:fontcolor=white@0.7:x=40:y=h-70")
subprocess.run(["ffmpeg", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"gradients=s=1280x720:c0=0x1d2b53:c1=0x7e2553:speed=0.01:d={VID}",
                "-f", "lavfi", "-i", "sine=frequency=220", "-t", str(VID), "-vf", vf,
                "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "48k",
                str(video)], check=True, timeout=300)

# Isolated studio state with one ready project.
db.DB_PATH = str(root / "studio.db")
db.SKIP_MISSING_SOURCE_FILES = False
server.UPLOADS_DIR, server.OUTPUT_DIR = uploads, output
job_runner.UPLOADS_DIR, job_runner.OUTPUT_DIR = str(uploads), str(output)
words, t, n = [], 0.0, 0
sample = "і от тоді я зрозумів що головне це не гроші а люди які поруч".split()
while t < VID - 0.5:
    words.append({"start": round(t, 2), "end": round(t + 0.4, 2), "word": sample[n % len(sample)]})
    t += 0.5
    n += 1
server.load_transcript_cache = lambda p, **kw: ([], words, "uk")
subtitles_generator.load_transcript_cache = lambda p, **kw: ([], words, "uk")
config_loader.ENV_PATH = root / ".env"
config_loader.ensure_gemini_api_key = lambda: os.environ.get("GEMINI_API_KEY")
app_update.check_gemini_key = lambda k: {"ok": True, "message": "Ключ працює."}
app_update._fetch_latest = lambda repo: (None, None)
db.init_db()
PID = "guide_podcast"
db.create_project(PID, "Подкаст: розмова про гроші", str(video), VID)
db.save_candidate_clips(PID, [
    {"start": s, "end": e, "score": sc, "title": ti, "reason": "Сильний хук у перші 3 секунди.",
     "caption": f"{ti}. Дивись до кінця!", "hashtags": ["#подкаст", "#гроші"]}
    for s, e, sc, ti in [(8, 40, 82, "Перший мільйон: з чого почати"), (52, 95, 96, "Чи в грошах щастя?"),
                         (104, 140, 74, "Страх знову стати бідним"), (146, 176, 88, "Скільки грошей треба для щастя")]])
db.update_project_status(PID, "ready")

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started:
    time.sleep(0.05)
URL = f"http://127.0.0.1:{port}"

from playwright.sync_api import sync_playwright  # noqa: E402

HEALTH = '{"status":"ok","has_nvenc":true,"has_cuda":true,"gpu_name":"NVIDIA GeForce RTX 3060"}'
SHOTS = args.shots
with sync_playwright() as pw:
    b = pw.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
    ctx = b.new_context(viewport={"width": 1280, "height": 800}, device_scale_factor=2 if SHOTS else 1,
                        color_scheme="dark", record_video_dir=None if SHOTS else str(OUT / "raw"),
                        record_video_size={"width": 1280, "height": 800})
    pg = ctx.new_page()
    pg.set_default_timeout(10000)
    pg.route("**/api/health", lambda r: r.fulfill(status=200, content_type="application/json", body=HEALTH))

    def shot(name):
        if SHOTS:
            pg.screenshot(path=str(OUT / f"{name}.png"))

    def wait(sec):
        pg.wait_for_timeout(int(sec * 1000))

    pg.goto(URL + "/")
    pg.evaluate("localStorage.setItem('studio-theme','dark')")
    pg.reload()
    pg.locator("#welcomeScreen").wait_for(state="visible"); wait(1.5); shot("01-welcome")
    pg.hover("a[href*='aistudio']"); wait(1.2)
    pg.click("#welcomeKey"); pg.keyboard.type("AIzaSyB" + "x7Qk2mN" * 4 + "Ab12", delay=25); wait(0.6)
    shot("02-key-pasted")
    pg.click("#welcomeSave"); pg.locator("#welcomeMsg.ok").wait_for(); wait(0.3); shot("03-key-ok")
    pg.locator("#welcomeScreen").wait_for(state="hidden"); wait(1.2)
    shot("04-home")
    pg.click("#urlInput"); pg.keyboard.type("https://www.youtube.com/watch?v=dQw4w9WgXcQ", delay=20); wait(0.8)
    pg.hover("#urlDownloadBtn"); wait(1.2); shot("05-home-link")
    pg.fill("#urlInput", ""); wait(0.5)
    pg.locator(".history-item-left", has_text="Подкаст").scroll_into_view_if_needed(); wait(0.8)
    shot("06-history")
    pg.locator(".history-item-left", has_text="Подкаст").click()
    pg.locator("#editor").wait_for(state="visible"); wait(3.5); shot("07-editor")
    pg.click("#edPlay"); wait(3); pg.click("#edPlay"); wait(0.5)
    pg.click("#edKeep"); wait(1.2); shot("08-keep")
    pg.keyboard.press("ArrowDown"); wait(1); pg.keyboard.press("y"); wait(1)
    pg.hover("#edMp4"); wait(1.2); shot("09-download")  # «Скачати» sits on top of the side panel
    pg.click("#edMode button[data-m='vx']"); wait(1)
    pg.locator("#vxZipClean").scroll_into_view_if_needed(); pg.hover("#vxZipClean"); wait(1.5); shot("10-zip")
    ctx.close()
    b.close()
srv.should_exit = True
print("done:", ", ".join(sorted(p.name for p in OUT.iterdir())))

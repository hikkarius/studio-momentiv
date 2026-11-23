"""Screenshots of the main screens in both themes, for before/after checks of CSS changes.

Usage: .venv/Scripts/python.exe scripts/design_shots.py <out_dir> [project_id]
Needs the studio running on :8000 and Playwright (requirements-dev.txt); uses the installed Edge.
Read-only: it opens screens, never changes clips.
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
project = sys.argv[2] if len(sys.argv) > 2 else None
URL = "http://127.0.0.1:8000/"

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True)
    for theme in ("dark", "light"):
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                                  color_scheme=theme)
        ctx.add_init_script(f"try {{ localStorage.setItem('studio-theme', '{theme}') }} catch (e) {{}}")
        page = ctx.new_page()
        page.goto(URL)
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / f"{theme}-1-home.png"))
        pid = project or page.evaluate("fetch('/api/projects').then(r => r.json()).then(d => (d.projects || d)[0].id)")
        page.evaluate(f"openProject({pid!r})")
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / f"{theme}-2-editor.png"))
        page.click('#edMode [data-m="vx"]')
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / f"{theme}-3-vertical.png"))
        page.click('#edMode [data-m="table"]')
        page.goto(URL)
        page.wait_for_timeout(2000)
        page.evaluate("Settings.open()")
        page.wait_for_timeout(800)
        page.screenshot(path=str(out / f"{theme}-4-settings-clips.png"))
        page.evaluate("Settings.setTab('disk')")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / f"{theme}-5-settings-disk.png"))
        ctx.close()
    browser.close()
print("saved to", out)

"""
scripts/compare_search_real.py — the classic and the experimental search on REAL videos with the real Gemini,
without touching the studio's data.

Run it from a separate copy of the code (a git worktree), pointing at the real studio folder:

    C:\\studio_momentiv\\.venv\\Scripts\\python.exe scripts\\compare_search_real.py --studio C:\\studio_momentiv ^
        --out C:\\studio_momentiv\\backups\\quality-report --match "ЯРЕМІЙ" --match "ІГРИ" --longest

Reads (never writes) the studio's database copy, transcript cache and Gemini key. Everything it makes goes to --out:
report.json (both clip sets per video, timings, Gemini calls, errors) and report.md (a short table).
Only videos that already have a cached transcript are used (no Whisper run).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _read_key(studio: Path) -> str:
    env = studio / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("GEMINI_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("У .env студії немає GEMINI_API_KEY")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--studio", required=True, help="the real studio folder (read only)")
    ap.add_argument("--out", required=True, help="where the report goes")
    ap.add_argument("--match", action="append", default=[], help="part of a video title (repeatable)")
    ap.add_argument("--longest", action="store_true", help="also the longest video with a transcript")
    args = ap.parse_args()
    studio = Path(args.studio).resolve()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if out.is_relative_to(ROOT):
        raise SystemExit("--out must be outside the code copy")

    # The studio's own settings (Whisper model = the transcript cache name), but never its key file
    us = studio / "user_settings.json"
    if us.exists():
        shutil.copyfile(us, ROOT / "user_settings.json")
    import os
    os.environ["GEMINI_API_KEY"] = _read_key(studio)

    tmp = Path(tempfile.mkdtemp(prefix="cmp_real_"))
    src = sqlite3.connect(f"file:{studio / 'studio_data.db'}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp / "studio.db")
    src.backup(dst)
    src.close()
    dst.close()

    import ai_selector
    import transcription
    from backend import db, job_runner
    from constants import resolve_filter_mode
    from config_loader import app_config

    db.DB_PATH = str(tmp / "studio.db")
    db.init_db()
    job_runner.OUTPUT_DIR = str(tmp / "out")
    cache = studio / ".transcript_cache"
    transcription.transcript_cache_dir = lambda: cache

    calls = {"n": 0}
    real_client = ai_selector.get_gemini_client

    def counting_client(*a, **k):
        c = real_client(*a, **k)
        gen = c.models.generate_content

        class M:
            def generate_content(self, *aa, **kk):
                calls["n"] += 1
                return gen(*aa, **kk)

        class C:
            models = M()
        return C()
    ai_selector.get_gemini_client = counting_client

    with db.get_db() as conn:
        projects = [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY duration")]
    for p in projects:  # the studio stores paths relative to its own folder
        if not Path(p["source_path"]).is_absolute():
            p["source_path"] = str(studio / p["source_path"])
    chosen = []
    for p in projects:
        if not Path(p["source_path"]).exists() or transcription.load_transcript_cache(p["source_path"], any_model=True) is None:
            continue
        if any(m.lower() in (p["title"] or "").lower() for m in args.match):
            chosen.append(p)
    if args.longest:
        ok = [p for p in projects if Path(p["source_path"]).exists()
              and transcription.load_transcript_cache(p["source_path"], any_model=True) is not None]
        if ok and ok[-1] not in chosen:
            chosen.append(ok[-1])
    print(f"Videos: {[p['title'] for p in chosen]}", flush=True)

    report = {"started": time.strftime("%Y-%m-%d %H:%M"), "models": list(app_config.gemini.candidate_models), "videos": []}
    for p in chosen:
        segs, words, lang = transcription.load_transcript_cache(p["source_path"], any_model=True)
        if not db.get_project_source_url(p["id"]):
            m = re.search(r"([A-Za-z0-9_-]{11})\.(mp4|mkv|webm)$", p["source_path"])
            if m:
                db.set_project_source_url(p["id"], f"https://youtu.be/{m.group(1)}")
        n = job_runner.auto_clips_count(p["duration"])
        v = {"title": p["title"], "minutes": round(p["duration"] / 60, 1), "auto_count": n,
             "url": db.get_project_source_url(p["id"]), "runs": {}}
        for mode in ("classic", "experimental"):
            calls["n"] = 0
            t0 = time.time()
            print(f"→ {p['title']} · {mode}", flush=True)
            try:
                clips = job_runner.find_clips_for(p["id"], p, segs, words, lang, n,
                                                  resolve_filter_mode(app_config.studio.default_filter), mode,
                                                  progress=lambda f, msg: print(f"   {int(f * 100)}% {msg}", flush=True))
                err = ""
            except Exception as e:
                clips, err = [], f"{type(e).__name__}: {e}"
            run = {"seconds": round(time.time() - t0, 1), "gemini_calls": calls["n"], "error": err, "clips": []}
            for c in sorted(clips, key=lambda c: -float(c.get("score", 0))):
                text = " ".join(w["word"] for w in words if c["start"] - 0.05 <= w["start"] < c["end"])
                run["clips"].append({"start": c["start"], "end": c["end"], "dur": round(c["end"] - c["start"], 1),
                                     "score": c.get("score"), "title": c.get("title"), "signals": c.get("signals", []),
                                     "first_words": " ".join(text.split()[:12]), "text": text[:600]})
            v["runs"][mode] = run
            print(f"   {len(run['clips'])} clips, {run['seconds']} s, {calls['n']} Gemini calls {err}", flush=True)
        heat = tmp / "out" / p["id"] / "heatmap.json"
        v["heatmap_points"] = len(json.loads(heat.read_text())) if heat.exists() else 0
        report["videos"].append(v)
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = ["| Відео | Спосіб | Кліпів | Сер. довжина | Час | Запитів Gemini | Помилка |", "|---|---|---|---|---|---|---|"]
    for v in report["videos"]:
        for mode, r in v["runs"].items():
            avg = statistics.mean([c["dur"] for c in r["clips"]]) if r["clips"] else 0
            lines.append(f"| {v['title'][:40]} | {mode} | {len(r['clips'])} | {avg:.0f} с | {r['seconds']} с | "
                         f"{r['gemini_calls']} | {r['error'][:80]} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    shutil.rmtree(tmp, ignore_errors=True)
    (ROOT / "user_settings.json").unlink(missing_ok=True)


if __name__ == "__main__":
    main()

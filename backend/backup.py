"""
backend/backup.py — «Копія бази щодня» and «Повернути все як було».

Once a day (on start, then checked every 30 minutes) the studio packs into backups/daily/ a ZIP with
the database (videos, clips, statuses, bounds, titles) and the Whisper transcripts. The video files
themselves are not copied: they are big and can be downloaded again. The 7 newest copies are kept.

Restoring first saves the current state as one more copy («перед відновленням»), so it can be undone.
"""
from __future__ import annotations

import datetime
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend import db

log = logging.getLogger("shorts_core")

BACKUP_DIR = Path("backups") / "daily"
KEEP = 7                  # daily / manual copies
KEEP_BEFORE_RESTORE = 2   # safety copies made by «Відновити»
DAY = 20 * 3600           # a new daily copy when the newest one is older than this
CHECK_EVERY = 30 * 60

KINDS = ("daily", "manual", "before-restore")
_NAME_RE = re.compile(r"^studio-\d{8}-\d{6}-(daily|manual|before-restore)\.zip$")
_lock = threading.Lock()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None


class BackupError(RuntimeError):
    """A message for the user."""


def _cache_dir() -> Path:
    import transcription
    return transcription.transcript_cache_dir()


def _counts(db_file: Path) -> Dict[str, int]:
    conn = sqlite3.connect(str(db_file))
    try:
        videos = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        clips = conn.execute("SELECT COUNT(*) FROM candidate_clips").fetchone()[0]
    except sqlite3.Error:
        videos = clips = 0
    finally:
        conn.close()
    return {"videos": int(videos), "clips": int(clips)}


def create_backup(kind: str = "manual") -> Dict[str, Any]:
    """Packs the database and transcripts into a new ZIP. Returns its description."""
    if kind not in KINDS:
        raise ValueError(kind)
    with _lock:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.datetime.now()
        name = f"studio-{now:%Y%m%d-%H%M%S}-{kind}.zip"
        target = BACKUP_DIR / name
        n = 1
        while target.exists():  # two copies within one second
            now += datetime.timedelta(seconds=n)
            name = f"studio-{now:%Y%m%d-%H%M%S}-{kind}.zip"
            target, n = BACKUP_DIR / name, n + 1

        with tempfile.TemporaryDirectory() as tmp:
            db_copy = Path(tmp) / "studio_data.db"
            # SQLite's own backup: a consistent copy even while the studio writes (WAL)
            src = sqlite3.connect(db.DB_PATH, timeout=30.0)
            dst = sqlite3.connect(str(db_copy))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            meta = {"created": time.time(), "kind": kind, **_counts(db_copy)}
            partial = target.with_suffix(".part")
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_copy, "studio_data.db")
                cache = _cache_dir()
                transcripts = 0
                if cache.is_dir():
                    for f in sorted(cache.iterdir()):
                        if f.is_file() and f.suffix == ".json":
                            zf.write(f, f"transcripts/{f.name}")
                            transcripts += 1
                meta["transcripts"] = transcripts
                zf.writestr("meta.json", json.dumps(meta, ensure_ascii=False))
            partial.replace(target)
        _prune()
    log.info(f"Копія бази збережена: {target} ({meta['videos']} відео, {meta['clips']} кліпів).")
    return _describe(target, meta)


def _prune() -> None:
    files = sorted(BACKUP_DIR.glob("studio-*.zip"), key=lambda p: p.name, reverse=True)
    regular = [f for f in files if not f.name.endswith("-before-restore.zip")]
    safety = [f for f in files if f.name.endswith("-before-restore.zip")]
    for f in regular[KEEP:] + safety[KEEP_BEFORE_RESTORE:]:
        try:
            f.unlink()
        except OSError as e:
            log.warning(f"Не вдалося видалити стару копію {f.name}: {e}")
    for f in BACKUP_DIR.glob("*.part"):  # left over from a copy cut off by a crash
        if time.time() - f.stat().st_mtime > 3600:
            f.unlink(missing_ok=True)


def _read_meta(path: Path) -> Dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as zf:
            return json.loads(zf.read("meta.json").decode("utf-8"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return {}


def _describe(path: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    kind = meta.get("kind") or (_NAME_RE.match(path.name).group(1) if _NAME_RE.match(path.name) else "daily")
    created = meta.get("created") or path.stat().st_mtime
    return {
        "name": path.name, "kind": kind, "created": created, "size": path.stat().st_size,
        "videos": meta.get("videos"), "clips": meta.get("clips"),
    }


def list_backups() -> List[Dict[str, Any]]:
    """Copies, newest first."""
    if not BACKUP_DIR.is_dir():
        return []
    out = [_describe(f, _read_meta(f)) for f in BACKUP_DIR.glob("studio-*.zip") if _NAME_RE.match(f.name)]
    return sorted(out, key=lambda b: b["name"], reverse=True)


def _resolve(name: str) -> Path:
    if not _NAME_RE.match(name or ""):
        raise BackupError("Такої копії немає.")
    path = (BACKUP_DIR / name).resolve()
    if not path.is_relative_to(BACKUP_DIR.resolve()) or not path.is_file():
        raise BackupError("Такої копії немає.")
    return path


def restore_backup(name: str) -> Dict[str, Any]:
    """Puts the database from the copy back. The current state is saved as a copy first.
    Transcripts from the copy come back only where they are missing now."""
    path = _resolve(name)
    if db.analysis_running():
        raise BackupError("Зараз обробляється відео. Відновити можна, коли обробка закінчиться.")
    safety = create_backup("before-restore")
    with _lock, tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(path) as zf:
            if "studio_data.db" not in zf.namelist():
                raise BackupError("Копія пошкоджена: у ній немає бази.")
            zf.extract("studio_data.db", tmp)
            restored_transcripts = 0
            cache = _cache_dir()
            for member in zf.namelist():
                if not member.startswith("transcripts/") or not member.endswith(".json"):
                    continue
                fname = Path(member).name
                dest = (cache / fname).resolve()
                if not dest.is_relative_to(cache.resolve()) or dest.exists():
                    continue
                cache.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src_f, open(dest, "wb") as out:
                    shutil.copyfileobj(src_f, out)
                restored_transcripts += 1
        src = sqlite3.connect(str(Path(tmp) / "studio_data.db"))
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("Копія пошкоджена: базу не вдалося прочитати.")
            dst = sqlite3.connect(db.DB_PATH, timeout=30.0)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    db.init_db()  # a copy from an older studio version gets the new columns
    log.warning(f"Базу відновлено з копії {name}; поточний стан збережено як {safety['name']}.")
    return {"restored": name, "safety": safety["name"], "transcripts": restored_transcripts}


def newest_backup_time() -> float:
    regular = [b for b in list_backups() if b["kind"] != "before-restore"]
    return max((b["created"] for b in regular), default=0.0)


def maybe_daily_backup() -> Optional[Dict[str, Any]]:
    """Makes the daily copy if it is on and the newest copy is older than ~a day."""
    from config_loader import app_config
    if not app_config.studio.daily_backup:
        return None
    if time.time() - newest_backup_time() < DAY:
        return None
    if not Path(db.DB_PATH).exists():
        return None
    return create_backup("daily")


FIRST_CHECK_AFTER = 2 * 60  # let the studio start up first


def _loop() -> None:
    if _stop.wait(FIRST_CHECK_AFTER):
        return
    while not _stop.is_set():
        try:
            maybe_daily_backup()
        except Exception as e:  # never break the studio over a backup
            log.warning(f"Щоденна копія бази не вдалася: {e}")
        _stop.wait(CHECK_EVERY)


def start() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="DailyBackup", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()

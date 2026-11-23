"""
backend/storage.py — What the studio keeps on disk, and safe cleanup of it.

Categories:
  sources      original videos of the projects (deleting one = deleting the project)
  clips        cut 16:9 clips in web_output/<id>/slices   (re-created on the next download)
  vertical     9:16 renders in web_output/<id>/vertical    (re-created on the next download)
  transcripts  Whisper transcripts in .transcript_cache    (save re-transcribing)
  junk         leftovers nobody needs: half-finished downloads, temporary audio,
               files and folders of deleted projects, transcripts of deleted videos
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from backend import db
from config_loader import CONFIG

log = logging.getLogger("shorts_core")

# Temporary files younger than this may belong to a download/render that is still running
_TEMP_GRACE_SEC = 6 * 3600
_RENDER_TMP = Path(".render_tmp")


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        total = 0
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _mtime(path: Path) -> float:
    """Newest modification time inside path (a folder counts as fresh if any file in it is)."""
    try:
        newest = path.stat().st_mtime
        if path.is_dir():
            for root, _dirs, files in os.walk(path):
                for f in files:
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(root, f)))
                    except OSError:
                        pass
        return newest
    except OSError:
        return 0.0


def _remove(path: Path) -> int:
    size = _size(path)
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return size
    except OSError as e:
        log.warning(f"Не вдалося видалити {path}: {e}")
        return 0


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _norm_t(p: str) -> str:
    return os.path.normcase(os.path.abspath(p))


def _transcript_dir() -> Path:
    from transcription import transcript_cache_dir

    return transcript_cache_dir()


def _transcript_hashes(sources: Iterable[str]) -> Set[str]:
    """Fingerprints used as transcript cache names, for every source video that still exists."""
    from transcription import _cache_path

    hashes: Set[str] = set()
    for src in sources:
        if src and os.path.exists(src):
            try:
                hashes.add(_cache_path(src).stem.split("_")[0])
            except OSError:
                pass
    return hashes


def _junk(uploads: Path, output: Path, projects: List[Dict[str, Any]], with_transcripts: bool = True) -> List[Path]:
    now = time.time()
    used_sources = {_norm(p["source_path"]) for p in projects if p.get("source_path")}
    project_ids = {p["id"] for p in projects}
    junk: List[Path] = []

    if uploads.is_dir():
        for entry in uploads.iterdir():
            name = entry.name
            is_temp = name.startswith(("temp_url_", "staging_", "audio_")) or entry.is_dir()
            if is_temp:
                if now - _mtime(entry) > _TEMP_GRACE_SEC:
                    junk.append(entry)
            elif _norm(str(entry)) not in used_sources and now - _mtime(entry) > _TEMP_GRACE_SEC:
                junk.append(entry)  # video of a deleted project

    if output.is_dir():
        for entry in output.iterdir():
            if entry.is_dir() and entry.name not in project_ids:
                junk.append(entry)
            elif entry.is_dir():
                for sub in ("slices", "vertical"):
                    d = entry / sub
                    if d.is_dir():
                        junk.extend(f for f in d.iterdir()
                                    if f.name.startswith("tmp_") and now - _mtime(f) > _TEMP_GRACE_SEC)

    if _RENDER_TMP.is_dir():
        junk.extend(f for f in _RENDER_TMP.iterdir() if now - _mtime(f) > _TEMP_GRACE_SEC)

    if with_transcripts:
        cache_dir = _transcript_dir()
        if cache_dir.is_dir():
            keep = _transcript_hashes(p.get("source_path", "") for p in projects)
            # videos removed with «Прибрати відео» keep their transcript (the clip text still shows)
            from transcription import removed_sources
            gone = removed_sources()
            keep |= {gone[_norm_t(p["source_path"])] for p in projects
                     if p.get("source_removed") and p.get("source_path") and _norm_t(p["source_path"]) in gone}
            junk.extend(f for f in cache_dir.glob("*.json") if f.stem.split("_")[0] not in keep)
    return junk


def is_removed(p: Dict[str, Any]) -> bool:
    """The long video was removed on purpose («Прибрати відео»), and has not been uploaded again."""
    return bool(p.get("source_removed")) and not (p.get("source_path") and os.path.exists(p["source_path"]))


def is_finished(p: Dict[str, Any]) -> bool:
    """
    «Закінчив з цим відео»: it has clips and none is left in «Нові» or «В роботі».
    Clips in «Запас», «Виконано» and «Не беру» do not hold the video.
    """
    return bool(p.get("clips_count")) and not p.get("new_count") and not p.get("kept_count")


def _source_in_uploads(src: str, uploads: Path) -> Optional[Path]:
    """The source video, only when it is a file inside the studio's uploads folder (never anything else)."""
    if not src:
        return None
    path = Path(src)
    try:
        if path.is_file() and path.resolve().is_relative_to(uploads.resolve()):
            return path
    except OSError:
        return None
    return None


def _shared_with_unfinished(projects: List[Dict[str, Any]], pid: str, src: str) -> bool:
    n = _norm(src)
    return any(q["id"] != pid and q.get("source_path") and _norm(q["source_path"]) == n and not is_finished(q)
               for q in projects)


def scan(uploads: Path, output: Path) -> Dict[str, Any]:
    """Sizes by category plus a per-video list (biggest first)."""
    projects = db.list_all_projects()
    by_project: List[Dict[str, Any]] = []
    totals = {"sources": 0, "clips": 0, "vertical": 0, "transcripts": 0, "junk": 0}
    seen_sources: Set[str] = set()

    for p in projects:
        src = p.get("source_path") or ""
        removed = is_removed(p)
        if not removed and (not src or not os.path.exists(src)):
            continue
        src_size = 0
        if not removed and _norm(src) not in seen_sources:
            seen_sources.add(_norm(src))
            src_size = _size(Path(src))
        out_dir = output / p["id"]
        clips = _size(out_dir / "slices")
        vertical = _size(out_dir / "vertical")
        other = max(0, _size(out_dir) - clips - vertical)
        totals["sources"] += src_size
        totals["clips"] += clips
        totals["vertical"] += vertical
        finished = not removed and is_finished(p)
        by_project.append({
            "id": p["id"],
            "title": p.get("title") or p["id"],
            "created_at": p.get("created_at"),
            "clips_count": p.get("clips_count") or 0,
            "kept_count": p.get("kept_count") or 0,
            "new_count": p.get("new_count") or 0,
            "done_count": p.get("done_count") or 0,
            "drop_count": p.get("drop_count") or 0,
            "reserve_count": p.get("reserve_count") or 0,
            "source_bytes": src_size,
            "clips_bytes": clips + vertical,
            "total_bytes": src_size + clips + vertical + other,
            "finished": finished,
            # can the button remove it: in uploads and not shared with a video still in work
            "removable": finished and _source_in_uploads(src, uploads) is not None
                         and not _shared_with_unfinished(projects, p["id"], src),
            "source_removed": removed,
        })

    cache_dir = _transcript_dir()
    totals["transcripts"] = _size(cache_dir) if cache_dir.is_dir() else 0
    junk = _junk(uploads, output, projects)
    totals["junk"] = sum(_size(j) for j in junk)
    # transcripts of deleted videos are counted as junk, not twice
    totals["transcripts"] -= sum(_size(j) for j in junk if j.parent == cache_dir)

    try:
        du = shutil.disk_usage(".")
        disk = {"free": du.free, "total": du.total}
    except OSError:
        disk = {"free": 0, "total": 0}

    by_project.sort(key=lambda r: r["total_bytes"], reverse=True)
    can_go = [r for r in by_project if r["removable"]]
    return {
        "totals": totals,
        "studio_bytes": sum(totals.values()),
        "disk": disk,
        "projects": by_project,
        "junk_items": len(junk),
        "finished_count": len(can_go),
        "finished_bytes": sum(r["source_bytes"] + r["clips_bytes"] for r in can_go),
    }


class NotRemovable(ValueError):
    """A video that «Прибрати відео» must not touch; the message is for the user."""


def remove_sources(uploads: Path, output: Path, project_ids: List[str], busy: Iterable[str] = ()) -> Dict[str, Any]:
    """
    «Прибрати відео, з якими закінчив»: deletes the long source video and the clip files of finished projects.
    The project, its clips, statuses, texts and the transcript stay (the project is marked source_removed).
    Checks every project first and changes nothing if one of them may not be removed.
    busy: projects being analysed or waiting in the queue right now.
    """
    from transcription import remember_removed_source

    projects = {p["id"]: p for p in db.list_all_projects()}
    busy = set(busy)
    plan = []
    for pid in project_ids:
        p = projects.get(pid)
        if not p:
            raise NotRemovable("Такого відео немає.")
        title = p.get("title") or pid
        if pid in busy:
            raise NotRemovable(f"«{title}» зараз аналізується або чекає в черзі.")
        if is_removed(p):
            continue
        if not is_finished(p):
            raise NotRemovable(f"У «{title}» ще є кліпи в «Нових» або «В роботі».")
        src = _source_in_uploads(p.get("source_path") or "", uploads)
        if src is None:
            raise NotRemovable(f"Файл відео «{title}» лежить не в папці студії, тому студія його не видаляє.")
        if _shared_with_unfinished(list(projects.values()), pid, str(src)):
            raise NotRemovable(f"Той самий файл «{title}» ще використовує інше відео, з яким ти не закінчив.")
        plan.append((pid, src))

    freed = 0
    for pid, src in plan:
        remember_removed_source(str(src))  # the transcript stays findable without the video
        for sub in ("slices", "vertical"):
            d = output / pid / sub
            if d.exists():
                freed += _remove(d)
        if src.exists():
            freed += _remove(src)
        db.set_source_removed(pid)
        log.info(f"Прибрано відео, з яким закінчили: {pid}")
    return {"freed_bytes": freed, "removed": len(plan)}


def cleanup(
    uploads: Path,
    output: Path,
    junk: bool = False,
    clips: bool = False,
    older_than_days: int = 0,
    project_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Deletes junk and/or re-creatable clip files (16:9 slices and 9:16 renders).
    older_than_days > 0 keeps clip folders touched more recently than that.
    project_ids limits the clip cleanup to those projects. Returns freed bytes.
    """
    projects = db.list_all_projects()
    freed = 0
    removed = 0
    if junk:
        for path in _junk(uploads, output, projects):
            got = _remove(path)
            freed += got
            removed += 1
    if clips:
        cutoff = time.time() - older_than_days * 86400 if older_than_days > 0 else None
        wanted = set(project_ids) if project_ids else None
        for p in projects:
            if wanted is not None and p["id"] not in wanted:
                continue
            for sub in ("slices", "vertical"):
                d = output / p["id"] / sub
                if not d.is_dir():
                    continue
                if cutoff is not None and _mtime(d) > cutoff:
                    continue
                freed += _remove(d)
                removed += 1
    if freed:
        log.info(f"Прибирання диска: звільнено {freed / 1e6:.1f} МБ ({removed} об'єктів).")
    return {"freed_bytes": freed, "removed": removed}


def auto_cleanup(uploads: Path, output: Path) -> Optional[Dict[str, Any]]:
    """Runs on studio start when «Автоприбирання» is on in the settings."""
    days = int(CONFIG.get("studio", {}).get("cleanup_days") or 0)
    if days <= 0:
        return None
    return cleanup(uploads, output, junk=True, clips=True, older_than_days=days)

"""
backend/db.py — Lightweight, robust SQLite database for AI Video Moments Studio.
Stores projects, candidate clips, and background analyze jobs.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

DB_PATH = "studio_data.db"
SKIP_MISSING_SOURCE_FILES = True


@contextmanager
def get_db():
    """Thread-safe context manager for SQLite database connection with foreign key enforcement."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Initializes tables, indexes, and optimizes SQLite journal settings."""
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            source_path TEXT NOT NULL,
            duration REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'uploaded', -- uploaded, analyzing, ready, failed
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS candidate_clips (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            clip_index INTEGER NOT NULL,
            start_sec REAL NOT NULL,
            end_sec REAL NOT NULL,
            duration REAL NOT NULL,
            virality_score INTEGER NOT NULL DEFAULT 80,
            title TEXT NOT NULL,
            hook_text TEXT NOT NULL,
            reason TEXT NOT NULL,
            caption TEXT NOT NULL DEFAULT '',
            hashtags TEXT NOT NULL DEFAULT '[]',
            is_selected INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            job_type TEXT NOT NULL, -- analyze
            status TEXT NOT NULL DEFAULT 'queued', -- queued, running, completed, failed
            progress REAL NOT NULL DEFAULT 0.0,
            message TEXT NOT NULL DEFAULT '',
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        -- Processing queue: links and uploaded videos waiting to be downloaded and analysed one by one
        CREATE TABLE IF NOT EXISTS queue_items (
            id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL DEFAULT '',
            project_id TEXT,
            title TEXT NOT NULL DEFAULT '',
            clips_count INTEGER NOT NULL DEFAULT 0, -- 0 = auto (by video length)
            filter_mode TEXT NOT NULL DEFAULT 'Viral Hooks',
            language TEXT NOT NULL DEFAULT '', -- '' = the language from the settings, else uk/en/auto/...
            status TEXT NOT NULL DEFAULT 'waiting', -- waiting, downloading, analyzing, done, failed
            progress REAL NOT NULL DEFAULT 0.0,
            message TEXT NOT NULL DEFAULT '',
            error TEXT,
            job_id TEXT,
            position REAL NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_candidates_proj ON candidate_clips(project_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_proj ON jobs(project_id);
        """)

        # Migration safe-check: if older DB lacks caption or hashtags in candidate_clips
        cursor = conn.execute("PRAGMA table_info(candidate_clips);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "caption" not in columns:
            conn.execute("ALTER TABLE candidate_clips ADD COLUMN caption TEXT NOT NULL DEFAULT '';")
        if "hashtags" not in columns:
            conn.execute("ALTER TABLE candidate_clips ADD COLUMN hashtags TEXT NOT NULL DEFAULT '[]';")
        if "review_status" not in columns:
            # '' = new, 'keep' = in work, 'done' = finished (files freed), 'drop' = rejected
            conn.execute("ALTER TABLE candidate_clips ADD COLUMN review_status TEXT NOT NULL DEFAULT '';")
        if "vertical_opts" not in columns:
            # JSON: vertical 9:16 export settings of the clip (caption style, framing, word edits)
            conn.execute("ALTER TABLE candidate_clips ADD COLUMN vertical_opts TEXT NOT NULL DEFAULT '{}';")

        if "cuts" not in columns:
            # JSON: pauses cut out of the clip («Прибрати паузи»), absolute seconds [[a, b], ...]
            conn.execute("ALTER TABLE candidate_clips ADD COLUMN cuts TEXT NOT NULL DEFAULT '[]';")

        queue_columns = [row["name"] for row in conn.execute("PRAGMA table_info(queue_items);").fetchall()]
        if "language" not in queue_columns:
            conn.execute("ALTER TABLE queue_items ADD COLUMN language TEXT NOT NULL DEFAULT '';")


def _parse_score(raw_score: Any, default: int = 85) -> int:
    """Defensively parses score values (int, float, '92/100', None) without raising ValueError."""
    if isinstance(raw_score, (int, float)):
        return int(raw_score)
    if isinstance(raw_score, str):
        m = re.search(r"\d+", raw_score)
        if m:
            return int(m.group(0))
    return default


def create_project(project_id: str, title: str, source_path: str, duration: float) -> Dict[str, Any]:
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, source_path, duration, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'uploaded', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "title = excluded.title, source_path = excluded.source_path, "
            "duration = excluded.duration, status = excluded.status, updated_at = excluded.updated_at",
            (project_id, title, source_path, duration, now, now),
        )
    return get_project(project_id)


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
        if not row:
            return None
        return dict(row)


def update_project_status(project_id: str, status: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), project_id),
        )


def save_candidate_clips(project_id: str, clips: List[Dict[str, Any]]):
    with get_db() as conn:
        conn.execute("DELETE FROM candidate_clips WHERE project_id = ?", (project_id,))
        for idx, c in enumerate(clips, 1):
            clip_id = f"{project_id}_clip_{idx}"
            start = float(c.get("start", 0.0))
            end = float(c.get("end", 0.0))
            dur = max(0.0, end - start)
            raw_score = c.get("score") if c.get("score") is not None else c.get("virality_score")
            score = _parse_score(raw_score, default=85)
            title = str(c.get("title") or f"Кліп #{idx}")
            raw_hook = c.get("hook_text") or c.get("hook")
            hook = raw_hook if isinstance(raw_hook, str) and raw_hook.strip() else title
            reason = str(c.get("reason") or "Обрано алгоритмом вірусності")
            caption = str(c.get("caption") or "")
            tags = c.get("hashtags")
            tags_json = json.dumps(tags if isinstance(tags, list) else ["#shorts", "#відео"])

            conn.execute(
                "INSERT INTO candidate_clips (id, project_id, clip_index, start_sec, end_sec, duration, "
                "virality_score, title, hook_text, reason, caption, hashtags, is_selected) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (clip_id, project_id, idx, start, end, dur, score, title, hook, reason, caption, tags_json),
            )


def _clip_row(row: Any) -> Dict[str, Any]:
    """sqlite row → clip dict with JSON columns decoded."""
    d = dict(row)
    try:
        d["hashtags"] = json.loads(d.get("hashtags") or "[]")
    except Exception:
        d["hashtags"] = []
    try:
        vo = json.loads(d.get("vertical_opts") or "{}")
        d["vertical_opts"] = vo if isinstance(vo, dict) else {}
    except Exception:
        d["vertical_opts"] = {}
    try:
        cuts = json.loads(d.get("cuts") or "[]")
        d["cuts"] = cuts if isinstance(cuts, list) else []
    except Exception:
        d["cuts"] = []
    return d


def get_candidate_clips(project_id: str) -> List[Dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM candidate_clips WHERE project_id = ? ORDER BY clip_index ASC",
            (project_id,),
        ).fetchall()
        result = []
        for r in rows:
            result.append(_clip_row(r))
        return result


def update_clip_bounds(
    project_id: str,
    clip_index: int,
    new_start: float,
    new_end: float,
) -> Optional[Dict[str, Any]]:
    """Updates start_sec, end_sec, and duration of a candidate clip in SQLite."""
    new_start = round(float(new_start), 2)
    new_end = round(float(new_end), 2)
    dur = round(max(0.0, new_end - new_start), 2)
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE candidate_clips SET start_sec = ?, end_sec = ?, duration = ? "
            "WHERE project_id = ? AND clip_index = ?",
            (new_start, new_end, dur, project_id, clip_index),
        )
        if cursor.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM candidate_clips WHERE project_id = ? AND clip_index = ?",
            (project_id, clip_index),
        ).fetchone()
        if not row:
            return None
        return _clip_row(row)


REVIEW_STATUSES = ("", "keep", "done", "drop")
# Statuses whose rendered files are deleted to free disk space (bounds and texts stay).
FREED_STATUSES = ("done", "drop")


def update_clip_fields(project_id: str, clip_index: int, **fields: Any) -> Optional[Dict[str, Any]]:
    """
    Updates editable clip fields: review_status ('', 'keep', 'done', 'drop'), title, caption, hashtags (list).
    Unknown fields are ignored. Returns the updated clip or None when it does not exist.
    """
    sets: List[str] = []
    values: List[Any] = []
    if "review_status" in fields and fields["review_status"] is not None:
        st = str(fields["review_status"])
        if st not in REVIEW_STATUSES:
            raise ValueError(f"Невідомий статус: {st!r}")
        sets.append("review_status = ?")
        values.append(st)
    for key in ("title", "caption"):
        if fields.get(key) is not None:
            sets.append(f"{key} = ?")
            values.append(str(fields[key]).strip())
    if fields.get("hashtags") is not None:
        tags = [str(t).strip() for t in fields["hashtags"] if str(t).strip()]
        sets.append("hashtags = ?")
        values.append(json.dumps(tags, ensure_ascii=False))
    if fields.get("cuts") is not None:
        sets.append("cuts = ?")
        values.append(json.dumps([[float(a), float(b)] for a, b in fields["cuts"]]))
    if fields.get("vertical_opts") is not None:
        sets.append("vertical_opts = ?")
        values.append(json.dumps(fields["vertical_opts"], ensure_ascii=False))
    with get_db() as conn:
        if sets:
            cur = conn.execute(
                f"UPDATE candidate_clips SET {', '.join(sets)} WHERE project_id = ? AND clip_index = ?",
                (*values, project_id, clip_index),
            )
            if cur.rowcount == 0:
                return None
        row = conn.execute(
            "SELECT * FROM candidate_clips WHERE project_id = ? AND clip_index = ?",
            (project_id, clip_index),
        ).fetchone()
        if not row:
            return None
        return _clip_row(row)


def create_job(job_id: str, project_id: str, job_type: str) -> Dict[str, Any]:
    now = time.time()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO jobs (id, project_id, job_type, status, progress, message, created_at, updated_at) "
            "VALUES (?, ?, ?, 'queued', 0.0, 'Очікування в черзі...', ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "project_id = excluded.project_id, job_type = excluded.job_type, "
            "status = excluded.status, progress = excluded.progress, "
            "message = excluded.message, error = NULL, updated_at = excluded.updated_at",
            (job_id, project_id, job_type, now, now),
        )
    return get_job(job_id)


def update_job(
    job_id: str,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    message: Optional[str] = None,
    error: Optional[str] = None,
):
    with get_db() as conn:
        fields = ["updated_at = ?"]
        params = [time.time()]
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if progress is not None:
            fields.append("progress = ?")
            params.append(float(progress))
        if message is not None:
            fields.append("message = ?")
            params.append(str(message))
        if error is not None:
            fields.append("error = ?")
            params.append(str(error))
        params.append(job_id)
        query = "UPDATE jobs SET " + ", ".join(fields) + " WHERE id = ?"
        conn.execute(query, params)


def fail_interrupted_jobs() -> int:
    """
    Marks jobs and projects left 'running'/'analyzing' by a previous server process as failed,
    so the UI offers a retry instead of a progress bar that never moves. Returns the number of jobs.
    """
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, message = ? "
            "WHERE status IN ('queued', 'running')",
            ("Перервано перезапуском студії", "Аналіз перервано перезапуском студії. Спробуйте ще раз."),
        )
        conn.execute("UPDATE projects SET status = 'failed' WHERE status = 'analyzing'")
        return cur.rowcount or 0


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if not row:
            return None
        return dict(row)


def get_project_by_source_path(source_path: str) -> Optional[Dict[str, Any]]:
    """Returns an existing project matching source_path or identical file content on disk."""
    if not source_path:
        return None

    target_norm = os.path.normcase(os.path.abspath(source_path))
    target_size = os.path.getsize(source_path) if os.path.exists(source_path) else None

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        for r in rows:
            p_src = r["source_path"]
            if not p_src:
                continue
            if os.path.normcase(os.path.abspath(p_src)) == target_norm:
                return dict(r)
            if target_size is not None and os.path.exists(p_src):
                try:
                    if os.path.getsize(p_src) == target_size and os.path.basename(p_src) == os.path.basename(source_path):
                        return dict(r)
                except OSError:
                    pass
    return None


def is_source_path_used_by_other_projects(source_path: str, exclude_project_id: str) -> bool:
    """Checks if any other active project still references this physical source_path."""
    if not source_path:
        return False
    target_norm = os.path.normcase(os.path.abspath(source_path))
    with get_db() as conn:
        rows = conn.execute("SELECT id, source_path FROM projects WHERE id != ?", (exclude_project_id,)).fetchall()
        for r in rows:
            p_src = r["source_path"]
            if p_src and os.path.normcase(os.path.abspath(p_src)) == target_norm:
                return True
    return False


def list_recent_projects(limit: int = 25, include_test: Optional[bool] = None) -> List[Dict[str, Any]]:
    """
    Returns unique, non-duplicated recent projects.
    In production (SKIP_MISSING_SOURCE_FILES=True), ensures physical video files still exist on disk.
    """
    should_skip_missing = SKIP_MISSING_SOURCE_FILES if include_test is None else (not include_test)
    with get_db() as conn:
        query = """
        SELECT p.*, COUNT(c.id) AS clips_count,
               SUM(CASE WHEN COALESCE(c.review_status, '') != '' THEN 1 ELSE 0 END) AS reviewed_count,
               SUM(CASE WHEN c.review_status = 'keep' THEN 1 ELSE 0 END) AS kept_count,
               SUM(CASE WHEN c.review_status = 'done' THEN 1 ELSE 0 END) AS done_count
        FROM projects p
        LEFT JOIN candidate_clips c ON p.id = c.project_id
        GROUP BY p.id
        ORDER BY (COUNT(c.id) > 0) DESC, p.created_at DESC
        """
        rows = conn.execute(query).fetchall()
        seen_keys = set()
        result = []

        for r in rows:
            d = dict(r)
            pid = d.get("id", "")
            src = d.get("source_path", "")

            if should_skip_missing:
                # In production, skip projects whose physical source video was deleted
                if not src or not os.path.exists(src):
                    continue

            # Deduplication: ensure one unique entry per physical video file
            if src:
                try:
                    norm_path = os.path.normcase(os.path.abspath(src))
                    file_size = os.path.getsize(src) if os.path.exists(src) else 0
                    dedup_key = (file_size, os.path.basename(norm_path))
                except OSError:
                    dedup_key = src
            else:
                dedup_key = pid

            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            s_m, s_s = divmod(int(d.get("duration", 0)), 60)
            d["duration_formatted"] = f"{s_m:02d}:{s_s:02d}"
            result.append(d)

            if len(result) >= limit:
                break

        return result


def delete_project(project_id: str) -> bool:
    """Deletes a project and its clips and jobs from database."""
    with get_db() as conn:
        conn.execute("DELETE FROM candidate_clips WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM jobs WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM queue_items WHERE project_id = ? AND status NOT IN ('downloading', 'analyzing')", (project_id,))
        cursor = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cursor.rowcount > 0


# ─── Processing queue ────────────────────────────────────────────────────────

QUEUE_ACTIVE = ("downloading", "analyzing")
QUEUE_FINISHED = ("done", "failed")


def add_queue_item(
    item_id: str,
    source_url: str = "",
    project_id: Optional[str] = None,
    title: str = "",
    clips_count: int = 0,
    filter_mode: str = "Viral Hooks",
    language: str = "",
) -> Dict[str, Any]:
    now = time.time()
    with get_db() as conn:
        row = conn.execute("SELECT MAX(position) AS p FROM queue_items").fetchone()
        position = (row["p"] or 0) + 1
        conn.execute(
            "INSERT INTO queue_items (id, source_url, project_id, title, clips_count, filter_mode, language, status, "
            "progress, message, position, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting', 0.0, 'Чекає своєї черги', ?, ?, ?)",
            (item_id, source_url, project_id, title, int(clips_count), filter_mode, language or "", position, now, now),
        )
    return get_queue_item(item_id)


def get_queue_item(item_id: str) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM queue_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def list_queue_items() -> List[Dict[str, Any]]:
    """Active first, then waiting in order, then finished (newest first)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM queue_items ORDER BY "
            "CASE WHEN status IN ('downloading', 'analyzing') THEN 0 WHEN status = 'waiting' THEN 1 ELSE 2 END, "
            "CASE WHEN status = 'waiting' THEN position ELSE -updated_at END"
        ).fetchall()
        return [dict(r) for r in rows]


def update_queue_item(item_id: str, **fields: Any) -> None:
    allowed = {"project_id", "title", "status", "progress", "message", "error", "job_id", "position"}
    sets = ["updated_at = ?"]
    params: List[Any] = [time.time()]
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"Unknown queue field {k}")
        sets.append(f"{k} = ?")
        params.append(v)
    params.append(item_id)
    with get_db() as conn:
        conn.execute("UPDATE queue_items SET " + ", ".join(sets) + " WHERE id = ?", params)


def claim_next_queue_item() -> Optional[Dict[str, Any]]:
    """Atomically takes the first waiting item and marks it as started. None if nothing waits."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM queue_items WHERE status = 'waiting' ORDER BY position LIMIT 1"
        ).fetchone()
        if not row:
            return None
        status = "downloading" if row["source_url"] and not row["project_id"] else "analyzing"
        cur = conn.execute(
            "UPDATE queue_items SET status = ?, progress = 0.0, error = NULL, updated_at = ? "
            "WHERE id = ? AND status = 'waiting'",
            (status, time.time(), row["id"]),
        )
        if not cur.rowcount:
            return None
    return get_queue_item(row["id"])


def requeue_interrupted_items() -> int:
    """Items cut off by a studio restart go back to the front of the queue."""
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE queue_items SET status = 'waiting', progress = 0.0, "
            "message = 'Продовжимо після перезапуску студії', position = position - 1000000 "
            "WHERE status IN ('downloading', 'analyzing')"
        )
        return cur.rowcount or 0


def delete_queue_item(item_id: str) -> bool:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE id = ?", (item_id,))
        return cur.rowcount > 0


def clear_finished_queue_items() -> int:
    with get_db() as conn:
        cur = conn.execute("DELETE FROM queue_items WHERE status IN ('done', 'failed')")
        return cur.rowcount or 0


def move_queue_item(item_id: str, direction: int) -> bool:
    """Swaps a waiting item with its waiting neighbour (direction -1 = earlier, +1 = later)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, position FROM queue_items WHERE status = 'waiting' ORDER BY position"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if item_id not in ids:
            return False
        i = ids.index(item_id)
        j = i + (1 if direction > 0 else -1)
        if j < 0 or j >= len(ids):
            return False
        a, b = rows[i], rows[j]
        conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (b["position"], a["id"]))
        conn.execute("UPDATE queue_items SET position = ? WHERE id = ?", (a["position"], b["id"]))
        return True


def list_all_projects() -> List[Dict[str, Any]]:
    """Every project row (no dedup / missing-file filtering), for disk accounting."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT p.*, SUM(CASE WHEN c.review_status = 'keep' THEN 1 ELSE 0 END) AS kept_count, "
            "COUNT(c.id) AS clips_count "
            "FROM projects p LEFT JOIN candidate_clips c ON p.id = c.project_id GROUP BY p.id"
        ).fetchall()
        return [dict(r) for r in rows]

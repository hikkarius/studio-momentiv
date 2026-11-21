"""
support_report.py — «Зібрати звіт про проблему»: one ZIP a friend can send to whoever gave them the
studio. Holds the studio version, system and GPU info, settings, recent queue errors and the logs.
The Gemini key, other key-shaped strings and the Windows user name are removed from every file.
"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
LOG_TAIL_BYTES = 2 * 1024 * 1024

_KEY_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"AQ\.[0-9A-Za-z_.\-]{20,}"),
    re.compile(r"(GEMINI_API_KEY\s*[=:]\s*)\S+"),
]


def redact(text: str) -> str:
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if len(key) >= 8:
        text = text.replace(key, "[КЛЮЧ ПРИХОВАНО]")
    for pat in _KEY_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[КЛЮЧ ПРИХОВАНО]", text)
    # C:\Users\<name>\... → C:\Users\<user>\...
    text = re.sub(r"(?i)([A-Z]:[\\/]+Users[\\/]+)[^\\/\s\"']+", r"\1<user>", text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~")
    return text


def _tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        if size > limit:
            text = f"[… показано останні {limit // 1024} КБ із {size // 1024} КБ]\n" + text.split("\n", 1)[-1]
        return text
    except OSError as e:
        return f"[не вдалося прочитати {path.name}: {e}]"


def _ffmpeg_line() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        return "ffmpeg не знайдено"
    try:
        out = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10).stdout
        return f"{exe}: {out.splitlines()[0] if out else '?'}"
    except Exception as e:
        return f"{exe}: {e}"


def _section(title: str, body: Any) -> str:
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False, indent=2, default=str)
    return f"===== {title} =====\n{body}\n\n"


def build_report(info: Dict[str, Any], log_file: Path) -> bytes:
    """`info` holds what the server already knows (version, GPU, settings, queue, disk)."""
    summary = _section("Звіт Студії моментів", {
        "створено": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "система": platform.platform(),
        "python": sys.version.split()[0],
        "папка студії": str(ROOT),
        "ffmpeg": _ffmpeg_line(),
    })
    for title, body in info.items():
        summary += _section(title, body)

    files: List[tuple[str, str]] = [("звіт.txt", summary)]
    for name, path in (("shorts_automator.log", log_file), ("install.log", ROOT / "install.log"), ("update.log", ROOT / "update.log")):
        if path.exists():
            files.append((name, _tail(path)))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in files:
            z.writestr(name, redact(text))
    return buf.getvalue()

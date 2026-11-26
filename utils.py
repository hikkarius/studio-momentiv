"""
utils.py — Media, FFmpeg, URL ingestion, deduplication, and hardware probing utilities
for AI Video Moments Analyzer Studio.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import ipaddress
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger("shorts_core")


def _ensure_ffmpeg_on_path() -> None:
    """Ensures ffmpeg and ffprobe are in os.environ['PATH'] even if terminal wasn't restarted."""
    # ffmpeg, поставлений інсталятором для друзів (tools\ffmpeg\bin), має перевагу
    bundled = Path(__file__).resolve().parent / "tools" / "ffmpeg" / "bin"
    if (bundled / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")).exists():
        if str(bundled) not in os.environ.get("PATH", "").split(os.pathsep):
            os.environ["PATH"] = str(bundled) + os.pathsep + os.environ.get("PATH", "")
        return
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            winget_pkgs = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
            if winget_pkgs.exists():
                for bin_dir in winget_pkgs.glob("**/ffmpeg*/bin"):
                    if (bin_dir / "ffmpeg.exe").exists():
                        os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
                        log.info(f"Auto-discovered ffmpeg at {bin_dir} and added to PATH")
                        return


def setup_console_encoding() -> None:
    """Configures Windows stdout and stderr for UTF-8 encoding."""
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_cuda_dll_handles: list = []
_cuda_dll_dirs_registered: set[str] = set()


def register_cuda_dll_dirs() -> list[str]:
    """
    Windows-only: makes pip-installed NVIDIA CUDA runtime DLLs (cublas64_12.dll, cudnn*.dll)
    from site-packages/nvidia/*/bin discoverable by CTranslate2 / faster-whisper.
    Idempotent and never raises. Returns the list of registered directories.
    """
    if sys.platform != "win32":
        return []
    try:
        import site

        roots: list[str] = []
        try:
            roots.extend(site.getsitepackages())
        except Exception:
            pass
        roots.append(os.path.join(sys.prefix, "Lib", "site-packages"))

        found: list[str] = []
        for root in roots:
            nvidia_dir = Path(root) / "nvidia"
            if not nvidia_dir.is_dir():
                continue
            for bin_dir in sorted(nvidia_dir.glob("*/bin")):
                if bin_dir.is_dir():
                    resolved = str(bin_dir.resolve())
                    if resolved not in found:
                        found.append(resolved)

        new_dirs = [d for d in found if d not in _cuda_dll_dirs_registered]
        if new_dirs:
            os.environ["PATH"] = os.pathsep.join(new_dirs) + os.pathsep + os.environ.get("PATH", "")
            for d in new_dirs:
                try:
                    _cuda_dll_handles.append(os.add_dll_directory(d))
                except Exception as e:
                    log.debug(f"add_dll_directory failed for {d}: {e}")
                _cuda_dll_dirs_registered.add(d)
            log.debug(f"Registered CUDA DLL directories: {new_dirs}")
        return found
    except Exception as e:
        log.debug(f"register_cuda_dll_dirs failed: {e}")
        return []


setup_console_encoding()
_ensure_ffmpeg_on_path()


def wait_for_file_ready(file_path: str | None, timeout: float = 5.0, check_interval: float = 0.25) -> bool:
    """Waits until a file exists, is non-empty, and can be read."""
    if not file_path or not os.path.exists(file_path):
        return False
    start = time.time()
    while time.time() - start < timeout:
        try:
            if os.path.getsize(file_path) > 0:
                with open(file_path, "rb") as f:
                    f.seek(0, os.SEEK_END)
                return True
        except (PermissionError, OSError):
            time.sleep(check_interval)
        except Exception:
            break
    try:
        return bool(os.path.exists(file_path) and os.path.getsize(file_path) > 0)
    except Exception:
        return False


def get_video_duration(video_path: str | None) -> float:
    """Returns video duration in seconds via ffprobe, or 0.0 on any error."""
    if not video_path or not os.path.exists(video_path):
        return 0.0
    wait_for_file_ready(video_path, timeout=2.0)
    _ensure_ffmpeg_on_path()
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, check=True, timeout=30,
            )
            val = float(r.stdout.strip())
            if val > 0:
                return val
        except Exception as e:
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
            else:
                log.warning(f"ffprobe error for {video_path!r}: {e}")
    return 0.0


def extract_audio(video_path: str, audio_path: str) -> str:
    """Extracts mono 16 kHz WAV audio from a video file via ffmpeg."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path,
    ]
    subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=True, timeout=600,
    )
    return audio_path


def extract_clip_slice(source_path: str, start_sec: float, end_sec: float, output_path: str) -> str:
    """
    Extracts a video slice from source_path between start_sec and end_sec.
    Attempts lightning-fast lossless stream copy (-c copy) first, falling back
    to NVENC or ultrafast libx264 re-encode when stream copy fails.
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source video not found: {source_path}")

    _ensure_ffmpeg_on_path()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    dur = max(0.5, end_sec - start_sec)

    # 1. Primary: Lightning-fast stream copy (-c copy) in ~0.5s with zero quality loss
    cmd_copy = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.2f}",
        "-i", source_path,
        "-t", f"{dur:.2f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-f", "mp4",
        output_path,
    ]
    try:
        proc_copy = subprocess.run(cmd_copy, capture_output=True, text=True, timeout=60)
        if proc_copy.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            return output_path
        err_msg = proc_copy.stderr[-300:] if proc_copy.stderr else "Stream copy returned non-zero code"
        log.warning(f"Fast stream copy non-zero ({err_msg}), falling back to frame-accurate transcode...")
    except Exception as e:
        log.warning(f"Fast stream copy failed ({e}), falling back to frame-accurate transcode...")

    # 2. Fallback: Frame-accurate hardware/software re-encode (uses cached GPU capabilities)
    from state import app_state
    caps = app_state.get_gpu_capabilities()
    if caps.has_nvenc:
        vcodec_args = ["-c:v", "h264_nvenc", "-preset", "p2", "-b:v", "8M", "-pix_fmt", "yuv420p"]
    else:
        vcodec_args = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]

    cmd_encode = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.2f}",
        "-i", source_path,
        "-t", f"{dur:.2f}",
        *vcodec_args,
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        "-f", "mp4",
        output_path,
    ]
    try:
        proc_encode = subprocess.run(cmd_encode, capture_output=True, text=True, timeout=120)
        if proc_encode.returncode != 0:
            err = proc_encode.stderr[-300:] if proc_encode.stderr else "Encode returned non-zero"
            raise RuntimeError(f"FFmpeg slice transcode failed: {err}")
    except Exception as e:
        # Ultimate fallback: libx264 CPU if NVENC failed
        if caps.has_nvenc:
            log.warning(f"NVENC transcode failed ({e}), falling back to libx264 CPU...")
            cmd_cpu = [
                "ffmpeg", "-y",
                "-ss", f"{start_sec:.2f}",
                "-i", source_path,
                "-t", f"{dur:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "192k",
                "-movflags", "+faststart",
                "-f", "mp4",
                output_path,
            ]
            proc_cpu = subprocess.run(cmd_cpu, capture_output=True, text=True, timeout=120)
            if proc_cpu.returncode != 0:
                err_cpu = proc_cpu.stderr[-300:] if proc_cpu.stderr else "CPU fallback failed"
                raise RuntimeError(f"FFmpeg slice extraction completely failed: {err_cpu}")
        else:
            raise

    return output_path


def generate_video_thumbnail(video_path: str, output_image_path: str, at_sec: float = 3.0) -> Optional[str]:
    """
    Extracts a lightweight JPG poster thumbnail from video_path at at_sec.
    Executes in ~0.05 seconds via FFmpeg fast seek.
    """
    if not os.path.exists(video_path):
        return None

    _ensure_ffmpeg_on_path()
    os.makedirs(os.path.dirname(os.path.abspath(output_image_path)), exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{at_sec:.2f}",
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "3",
        "-vf", "scale=360:-1",
        output_image_path,
    ]
    try:
        subprocess.run(cmd, capture_output=True, check=True, timeout=15)
        return output_image_path if os.path.exists(output_image_path) else None
    except Exception as e:
        log.warning(f"Failed to generate thumbnail for {video_path}: {e}")
        return None


def download_from_url(url: str, output_dir: str = "web_uploads") -> str:
    """Downloads a video and returns its local path (see download_from_url_with_title)."""
    return download_from_url_with_title(url, output_dir)[0]


_YTDLP_PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")


def _run_ytdlp_with_progress(cmd: list, env: dict, progress_cb: Callable[[float, str], None]) -> str:
    """Runs yt-dlp, reporting «[download] 42.0%» lines through progress_cb. Returns its stdout."""
    cmd = cmd[:]
    sep = cmd.index("--")
    cmd[sep:sep] = ["--progress", "--newline"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []
    passes = [0]   # video and audio are downloaded one after another: 0 → 100 % twice
    last = [-1.0]

    def read(stream, sink):
        for line in stream:
            sink.append(line)
            m = _YTDLP_PROGRESS_RE.search(line)
            if not m:
                continue
            pct = min(100.0, float(m.group(1)))
            if pct < last[0] - 50:
                passes[0] += 1
            last[0] = pct
            try:
                progress_cb(pct / 100.0, "Завантаження" + (" звуку" if passes[0] else "") + f": {pct:.0f}%")
            except Exception:
                pass

    t_err = threading.Thread(target=read, args=(proc.stderr, err_lines), daemon=True)
    t_err.start()
    try:
        read(proc.stdout, out_lines)
    except BaseException:  # «Скасувати»: progress_cb raised job_runner.Cancelled
        proc.kill()
        proc.wait(timeout=30)
        raise
    try:
        proc.wait(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    t_err.join(timeout=5)
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, "".join(out_lines), "".join(err_lines))
    return "".join(out_lines)


_YTDLP_FORMAT = [
    "-S", "vcodec:h264,res,acodec:m4a",
    "-f", "bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo[vcodec^=avc1]+bestaudio/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
]


def _checked_url(url: str) -> str:
    """Returns the stripped URL, or raises ValueError for a bad scheme, a missing host or a local/private host (SSRF)."""
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https"):
        raise ValueError(f"Неприпустима схема URL: {parsed.scheme!r}. Дозволені тільки http:// та https://")

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("Некоректний URL: відсутній хост.")
    if hostname in ("localhost", "localhost.localdomain") or hostname.endswith(".local"):
        raise ValueError(f"Доступ до локального хоста заборонено: {hostname!r}")
    try:
        ip_obj = ipaddress.ip_address(hostname)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved or ip_obj.is_multicast:
            raise ValueError(f"Доступ до внутрішньої IP-адреси заборонено: {hostname!r}")
    except ValueError as ip_err:
        if "заборонено" in str(ip_err):
            raise

    return url


def probe_download_size(url: str, timeout: int = 90) -> Optional[int]:
    """
    Bytes the video at url will take once downloaded in the same format as download_from_url_with_title,
    asked from the site without downloading. None when the site does not tell (the download then just goes ahead).
    """
    url = _checked_url(url)
    cmd = [sys.executable, "-m", "yt_dlp", *_YTDLP_FORMAT, "--no-playlist", "--skip-download", "-j", "--", url]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                check=True, timeout=timeout, env=env)
        line = next((ln for ln in reversed(result.stdout.splitlines()) if ln.startswith("{")), "")
        info = json.loads(line) if line else {}
    except Exception as e:
        log.warning(f"Could not ask the size of {url}: {str(e)[:200]}")
        return None
    return estimate_download_size(info)


def estimate_download_size(info: dict) -> Optional[int]:
    """Size from a yt-dlp info dict: the chosen video + audio formats, or bitrate × duration as a fallback."""
    def one(f: dict) -> int:
        size = f.get("filesize") or f.get("filesize_approx")
        if size:
            return int(size)
        if f.get("tbr") and (f.get("duration") or info.get("duration")):
            return int(float(f["tbr"]) * 1000 / 8 * float(f.get("duration") or info["duration"]))
        return 0

    parts = info.get("requested_formats") or [info]
    total = sum(one(f) for f in parts)
    return total or None


# Audio track only: the same m4a stream the video download merges in, so timestamps match
_YTDLP_AUDIO_FORMAT = ["-f", "bestaudio[acodec^=mp4a]/bestaudio[ext=m4a]/bestaudio"]


def download_audio_from_url(url: str, output_dir: str, timeout: int = 1800) -> str:
    """Downloads only the audio of a link (a few MB, seconds) so Whisper can start while the
    video is still downloading. Same URL checks and output containment as the video download."""
    url = _checked_url(url)
    os.makedirs(output_dir, exist_ok=True)
    cmd = [
        sys.executable, "-m", "yt_dlp",
        *_YTDLP_AUDIO_FORMAT,
        "-o", os.path.join(output_dir, "%(id)s.audio.%(ext)s"),
        "--no-playlist",
        "--print", "after_move:filepath",
        "--",
        url,
    ]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    result = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True, timeout=timeout, env=env,
    )
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("yt-dlp did not return an audio file path.")
    path = os.path.normpath(lines[-1])
    if not Path(path).resolve().is_relative_to(Path(output_dir).resolve()):
        raise ValueError(f"Downloaded audio {path!r} resolved outside output directory {output_dir!r}")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"Downloaded audio does not exist or is empty: {path}")
    log.info(f"Audio downloaded → {path}")
    return path


def download_from_url_with_title(
    url: str,
    output_dir: str = "web_uploads",
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> tuple[str, str]:
    """
    Returns (local_path, title). The title is the video's real name on the site
    (e.g. the YouTube title), or "" when the site does not provide one.

    Downloads a video from a public URL (YouTube, TikTok, Vimeo, etc.) using yt-dlp.
    Enforces http/https scheme validation, SSRF host blocking, '--' CLI separator,
    and strict Path.resolve().is_relative_to() containment.
    """
    url = _checked_url(url)

    os.makedirs(output_dir, exist_ok=True)
    tpl = os.path.join(output_dir, "%(id)s.%(ext)s")
    cmd = [
        sys.executable, "-m", "yt_dlp",
        *_YTDLP_FORMAT,
        "--merge-output-format", "mp4",
        "-o", tpl,
        "--no-playlist",
        "--print", "after_move:title",
        "--print", "after_move:filepath",
        "--",
        url,
    ]
    log.info(f"Downloading video from: {url}")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    if progress_cb is None:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            check=True, timeout=3600, env=env,
        )
        stdout = result.stdout
    else:
        stdout = _run_ytdlp_with_progress(cmd, env, progress_cb)
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip() and not _YTDLP_PROGRESS_RE.search(ln)]
    if not lines:
        raise RuntimeError("yt-dlp did not return a file path.")
    path = os.path.normpath(lines[-1])
    resolved_output_dir = Path(output_dir).resolve()
    resolved_path = Path(path).resolve()
    if not resolved_path.is_relative_to(resolved_output_dir):
        raise ValueError(f"Downloaded file {path!r} resolved outside output directory {output_dir!r}")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RuntimeError(f"Downloaded file does not exist or is empty: {path}")
    title = lines[-2].strip() if len(lines) >= 2 else ""
    if title.upper() == "NA":
        title = ""
    log.info(f"Download complete → {path} ({title or 'без назви'})")
    return path, title


@dataclass
class GpuCapabilities:
    """Hardware acceleration capabilities detected on the current system."""
    has_nvenc: bool = False
    nvenc_device_name: str = ""
    has_cuda: bool = False
    cuda_device_name: str = ""

def probe_gpu_capabilities() -> GpuCapabilities:
    """
    Probes system hardware to detect NVIDIA GPU capabilities for both
    FFmpeg (h264_nvenc) and Whisper (CTranslate2 CUDA / PyTorch CUDA).
    Thread-safe, non-blocking, exception-guarded.
    """
    _ensure_ffmpeg_on_path()
    has_nvenc = False
    gpu_name = ""

    # 1. Try nvidia-smi for clean GPU model name
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            gpu_name = proc.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass

    # 2. Probe FFmpeg NVENC functionality
    try:
        test_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
            "-c:v", "h264_nvenc",
            "-f", "null", "-",
        ]
        res = subprocess.run(test_cmd, capture_output=True, timeout=5)
        if res.returncode == 0:
            has_nvenc = True
    except Exception:
        has_nvenc = False

    # 3. Probe CUDA functionality (prefer CTranslate2 used by faster-whisper, fallback to PyTorch)
    has_cuda = False
    cuda_name = ""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            has_cuda = True
            cuda_name = gpu_name or "CUDA GPU"
    except Exception:
        pass

    if not has_cuda:
        try:
            import torch
            if torch.cuda.is_available():
                has_cuda = True
                cuda_name = torch.cuda.get_device_name(0)
        except Exception:
            has_cuda = False

    caps = GpuCapabilities(
        has_nvenc=has_nvenc,
        nvenc_device_name=gpu_name or ("NVIDIA GPU" if has_nvenc else ""),
        has_cuda=has_cuda,
        cuda_device_name=cuda_name or gpu_name,
    )
    log.info(f"GPU capabilities probed: NVENC={caps.has_nvenc}, CUDA={caps.has_cuda}, Model={gpu_name or 'N/A'}")
    return caps


def compute_file_fingerprint(file_path: str) -> tuple[int, str]:
    """Computes a fast fingerprint: (size_in_bytes, head_and_tail_hash)."""
    if not file_path or not os.path.exists(file_path) or not os.path.isfile(file_path):
        return (0, "")
    size = os.path.getsize(file_path)
    if size == 0:
        return (0, "")
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        head = f.read(65536)  # first 64KB
        hasher.update(head)
        if size > 65536:
            f.seek(max(0, size - 65536))
            tail = f.read(65536)  # last 64KB
            hasher.update(tail)
    return (size, hasher.hexdigest())


def find_duplicate_video(file_path: str, search_dir: str = "web_uploads") -> Optional[str]:
    """
    Checks if an identical video file already exists inside search_dir.
    Returns the path to the existing duplicate file, or None if unique.
    """
    if not file_path or not os.path.exists(file_path):
        return None
    target_size, target_hash = compute_file_fingerprint(file_path)
    if target_size == 0:
        return None

    abs_target = os.path.abspath(file_path)
    search_dir_abs = os.path.abspath(search_dir)
    if not os.path.exists(search_dir_abs):
        return None

    for root, _, files in os.walk(search_dir_abs):
        for fname in files:
            candidate = os.path.abspath(os.path.join(root, fname))
            if candidate == abs_target:
                continue
            if not candidate.lower().endswith((".mp4", ".mov", ".mkv", ".webm", ".avi")):
                continue
            try:
                c_size, c_hash = compute_file_fingerprint(candidate)
                if c_size == target_size and c_hash == target_hash:
                    return candidate
            except Exception:
                continue
    return None

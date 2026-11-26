"""
«🏆 Топ-3»: a few short clips glued into one video, counting down from #3 to #1.

Each clip is taken as the studio hands it out (16:9 without its cut pauses, or the finished 9:16 with
subtitles); this module only adds a big «#3» for the first second of each clip, the author's title over
the start of the first one, and joins them. Nothing here talks to Gemini.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

# a ready clip file, or (source video, start, duration): the clean 16:9 is cut right here, frame-accurately
Piece = Union[str, Tuple[str, float, float]]

import pauses
import vertical_render

log = logging.getLogger("shorts_core")

NUMBER_SEC = 1.0  # how long the big number stays on screen
TITLE_SEC = 2.5   # how long the title stays over the first clip
MAX_TOTAL_SEC = 180.0  # Shorts and TikTok take up to 3 min
FPS = 30


def probe(path: str, timeout: int = 30) -> tuple[int, int, float]:
    """(width, height, duration) of a video file."""
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "default=nw=1", os.path.abspath(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    vals = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
    try:
        return int(vals["width"]), int(vals["height"]), float(vals.get("duration") or 0)
    except (KeyError, ValueError):
        raise RuntimeError(f"Не вдалося прочитати відео {Path(path).name}")


def overlay_ass(w: int, h: int, number: Optional[int], title: str = "") -> str:
    """Subtitles for one clip of the countdown: a big «#N» in the middle, the title at the top (first clip only)."""
    short = min(w, h)
    num_size = int(short * 0.42)
    title_size = int(short * 0.075)
    margin = int(w * 0.06)
    styles = [
        f"Style: Num,Montserrat Black,{num_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"1,0,0,0,100,100,0,0,1,{max(4, num_size // 18)},{max(2, num_size // 30)},5,0,0,0,1",
        f"Style: Title,Montserrat Black,{title_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
        f"1,0,0,0,100,100,0,0,1,{max(3, title_size // 9)},{max(1, title_size // 18)},8,{margin},{margin},{int(h * 0.07)},1",
    ]
    t = vertical_render._ass_time
    events = []
    if number is not None:
        pop = "\\fscx60\\fscy60\\t(0,160,\\fscx112\\fscy112)\\t(160,260,\\fscx100\\fscy100)\\fad(0,220)"
        events.append(f"Dialogue: 5,{t(0)},{t(NUMBER_SEC)},Num,,0,0,0,,{{\\an5\\pos({w // 2},{h // 2}){pop}}}#{number}")
    if title.strip():
        events.append(f"Dialogue: 6,{t(0)},{t(TITLE_SEC)},Title,,0,0,0,,{{\\fad(150,250)}}"
                      + vertical_render._ass_text(title.strip().upper()))
    return (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
        "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n" + "\n".join(styles)
        + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n"
    )


def build(files: Sequence[Piece], output_path: str, title: str = "", numbers: bool = True,
          has_nvenc: bool = False, timeout: int = 900) -> str:
    """
    Joins `files` (in the order shown: #N first, #1 last) into `output_path`. Every clip is re-encoded to the
    size of the first one with the overlays, then the pieces are joined without another re-encode.
    """
    if len(files) < 2:
        raise ValueError("Для топу потрібно щонайменше 2 кліпи.")
    w, h, _ = probe(files[0] if isinstance(files[0], str) else files[0][0])
    w, h = w - w % 2, h - h % 2
    work = vertical_render.RENDER_TMP / f"top_{uuid.uuid4().hex[:10]}"
    work.mkdir(parents=True, exist_ok=True)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f"tmp_{uuid.uuid4().hex[:6]}_{out.name}")
    fonts_rel = os.path.relpath(vertical_render.FONTS_DIR, work).replace("\\", "/")
    try:
        pieces: List[str] = []
        for i, src in enumerate(files):
            number = len(files) - i if numbers else None
            (work / f"o{i}.ass").write_text(overlay_ass(w, h, number, title if i == 0 else ""), encoding="utf-8")
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                  f"fps={FPS},ass=o{i}.ass:fontsdir={fonts_rel},format=yuv420p")
            path = src if isinstance(src, str) else src[0]
            audio = pauses.has_audio(path)
            inputs = ["-i", os.path.abspath(path)]
            if not isinstance(src, str):  # a re-encode seeks exactly, unlike the stream-copied slice
                inputs = ["-ss", f"{src[1]:.3f}", "-t", f"{src[2]:.3f}", *inputs]
            amap = ["-map", "0:a:0"]
            if not audio:  # a silent clip still needs a sound track, or the join fails
                inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
                amap = ["-map", "1:a:0", "-shortest"]
            piece = f"p{i}.mp4"
            last_err = ""
            for enc in vertical_render._encoders(has_nvenc):
                cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-map", "0:v:0", *amap, "-vf", vf, *enc,
                       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", piece]
                proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True, encoding="utf-8",
                                      errors="replace", timeout=timeout)
                if proc.returncode == 0 and (work / piece).exists():
                    break
                last_err = (proc.stderr or "")[-600:]
                log.warning(f"Top clip {i + 1} with {enc[1]} failed: {last_err}")
            else:
                raise RuntimeError("Не вдалося зібрати топ: " + last_err.strip()[-300:])
            pieces.append(piece)
        (work / "list.txt").write_text("".join(f"file '{p}'\n" for p in pieces), encoding="utf-8")
        proc = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy",
             "-movflags", "+faststart", str(tmp_out.resolve())],
            cwd=str(work), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        if proc.returncode != 0 or not tmp_out.exists():
            raise RuntimeError("Не вдалося склеїти топ: " + (proc.stderr or "")[-300:].strip())
        os.replace(tmp_out, out)
        return str(out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass

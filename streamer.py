"""
streamer.py — «Стрім: вебка + гра»: vertical 9:16 clips from stream recordings.

A stream recording is a game (or screen) with the streamer's webcam pasted into a corner.
  1. detect_webcam(): once per video, finds the webcam rectangle. A webcam is a face that sits in the
     same small spot in most frames; the rectangle's edges are lines that stay put in every frame
     (the overlay border) with changing game picture outside and a calmer room inside.
  2. plan(): turns the rectangle + chosen layout into «parts»: which piece of the source goes where
     on the 1080×1920 phone. The browser preview (vertical.js) and the renderer draw the same parts.
  3. video_filter(): the ffmpeg filter_complex for those parts.

Layouts (the «look», see LOOK_DEFAULT: size, zoom and position of both pictures, window, styling):
  top  — webcam on top (share of the height), game below, cropped around game_x;
  full — webcam on top, the whole 16:9 game under it, blurred game behind;
  pip  — game on the whole phone, webcam as a small window in the corner;
  game — no webcam (none found or switched off): the game only.
In every layout the webcam inside the game picture is blurred, so the face is not shown twice.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

OUT_W, OUT_H = 1080, 1920
LAYOUTS = ("top", "full", "pip")
DEFAULT_LAYOUT = "top"
SHARE_MIN, SHARE_MAX, DEFAULT_SHARE = 0.22, 0.6, 0.34
PIP_BORDER, PIP_RADIUS = 6, 28
FRAME_COLORS = {"none": None, "white": "FFFFFF", "cam": "9146FF", "gold": "E6C068"}
# How the stream looks on the phone. Kept per video (stream.json «look»); one clip may have its own (opts «stream_look»).
LOOK_DEFAULT: Dict[str, Any] = {
    "layout": DEFAULT_LAYOUT, "share": DEFAULT_SHARE,        # webcam's share of the phone height (top / full)
    "cam_zoom": 1.0, "game_zoom": 1.0,                        # 1 = the whole webcam / the most of the game
    "cam_fx": None, "cam_fy": None,                           # centre of the webcam crop inside its frame (None = face)
    "game_x": 0.5, "game_y": 0.5,                             # centre of the game crop
    "pip_x": 0.04, "pip_y": 0.035, "pip_w": 0.48,             # «Гра на весь екран»: the window, fractions of the phone
    "game_w": None, "game_cx": 0.5, "game_cy": 0.5,           # «Гра на весь екран»: the game as a window too (None = whole
                                                              # phone); its width and centre in phone widths/heights
    "round": False, "frame": "white", "divider": False,
    "nick": "", "nick_style": "twitch", "nick_x": None, "nick_y": None,  # nick_x/y: top-left on the phone (None = auto)
}
SAMPLES = 20
NICK_TOP = (36, 48)  # default top-left corner of the nick on the phone
# How the nick looks: (ASS font, size in px like CSS, uppercase, padding x, padding y, icon width)
NICK_STYLES: Dict[str, Tuple[str, int, bool, int, int, int]] = {
    "twitch": ("Rubik Black", 42, False, 18, 8, 44),    # purple plate with a chat-bubble icon
    "neon": ("Unbounded Black", 38, True, 12, 10, 0),     # glowing cyan
    "pill": ("Montserrat Black", 40, False, 26, 8, 0),    # white pill, black letters
    "bold": ("Rubik Black", 52, True, 8, 4, 0),          # yellow, black edge and shadow
    "tape": ("Oswald SemiBold", 48, True, 20, 4, 0),      # yellow sticker, slightly tilted
}
# libass sizes a font by its whole height (ascent + descent), CSS by the em: ASS size = CSS size / this
FONT_EM = {"Rubik Black": 0.653, "Unbounded Black": 0.646, "Montserrat Black": 0.64, "Oswald SemiBold": 0.588}

Box = Tuple[float, float, float, float]  # x, y, w, h


def save_info(path: str, info: Dict[str, Any]) -> None:
    """Writes stream.json whole or not at all: a reader never sees half a file (fast clicks send several saves)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(info), encoding="utf-8")
    for attempt in range(10):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:  # Windows: someone is reading the old file right now
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _even(v: float) -> int:
    return max(2, int(round(v)) // 2 * 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ─── 1. Finding the webcam ────────────────────────────────────────────────────

def _duration(source: str) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", source],
                         capture_output=True, text=True, timeout=30)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _grab(source: str, t: float, w: int, h: int):
    import numpy as np
    proc = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", source, "-frames:v", "1",
                           "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                          capture_output=True, timeout=60)
    if proc.returncode != 0 or len(proc.stdout) < w * h * 3:
        return None
    return np.frombuffer(proc.stdout, dtype=np.uint8, count=w * h * 3).reshape(h, w, 3)


def detect_webcam(source: str, samples: int = SAMPLES, width: int = 640) -> Dict[str, Any]:
    """
    Looks at `samples` frames spread over the whole video. Returns
    {"cam": [x, y, w, h] | None, "face": [cx, cy] | None, "w", "h", "note"} with fractions of the frame.
    """
    import numpy as np
    from vertical_render import _detector_lock, _make_detector, probe_size
    src_w, src_h = probe_size(source)
    dur = _duration(source)
    sw = width
    sh = _even(src_h * sw / src_w)
    times = [dur * (0.05 + 0.9 * i / max(1, samples - 1)) for i in range(samples)] if dur > 1 else [0.0]
    frames, faces = [], []
    with _detector_lock:
        detect = _make_detector(sw, sh)
        for t in times:
            img = _grab(source, t, sw, sh)
            if img is None:
                continue
            k = len(frames)
            frames.append(img.mean(axis=2).astype(np.float32))
            for (x, y, fw, fh, score) in detect(img):
                if score >= 0.6:
                    faces.append(((x + fw / 2) / sw, (y + fh / 2) / sh, fw / sw, fh / sh, k))
    res = {"cam": None, "face": None, "w": src_w, "h": src_h, "note": ""}
    if len(frames) < 3:
        res["note"] = "Не вдалося прочитати кадри відео."
        return res
    found = find_cam_box(faces, np.stack(frames))
    res.update(found)
    return res


def find_cam_box(faces: Sequence[Tuple[float, float, float, float, int]], frames) -> Dict[str, Any]:
    """
    Pure part of detect_webcam (easy to test). faces: (cx, cy, w, h, frame_no) as fractions;
    frames: grey frames, shape (n, H, W).
    """
    import numpy as np
    n, H, W = frames.shape
    out: Dict[str, Any] = {"cam": None, "face": None, "note": ""}
    # The face that stays in one spot in most frames.
    best: List[Tuple[float, float, float, float, int]] = []
    for f in faces:
        group = [g for g in faces if abs(g[0] - f[0]) < 0.05 and abs(g[1] - f[1]) < 0.07]
        if len({g[4] for g in group}) > len({g[4] for g in best}):
            best = group
    hits = len({g[4] for g in best})
    if hits < max(3, 0.3 * n):
        out["note"] = "Вебку не знайдено: обличчя не з'являється в одному місці."
        return out
    med = lambda i: float(np.median([g[i] for g in best]))
    fx, fy, fw, fh = med(0), med(1), med(2), med(3)
    if fh > 0.3:
        out["note"] = "Обличчя займає великий шмат кадру: це звичайне відео, а не стрім із вебкою."
        return out

    # Lines that are there in every frame: the median of the gradient over frames.
    dx = np.median(np.abs(np.diff(frames, axis=2)), axis=0)  # (H, W-1): vertical edges
    dy = np.median(np.abs(np.diff(frames, axis=1)), axis=0)  # (H-1, W): horizontal edges
    std = frames.std(axis=0)                                  # how much each pixel changes over the video
    px, py, pw, ph = fx * W, fy * H, fw * W, fh * H

    def band(lo: float, hi: float, size: int) -> slice:
        return slice(int(_clamp(lo, 0, size - 1)), int(_clamp(hi, 1, size)))

    rows = band(py - ph, py + ph, H)
    cols = band(px - pw, px + pw, W)
    prof_x = dx[rows, :].mean(axis=0)
    prof_y = dy[:, cols].mean(axis=1)
    std_x = std[rows, :].mean(axis=0)
    std_y = std[:, cols].mean(axis=1)

    def edge(prof, stdp, lo: float, hi: float, inside_is_right: bool) -> Optional[int]:
        lo_i, hi_i = int(_clamp(lo, 1, len(prof) - 2)), int(_clamp(hi, 1, len(prof) - 2))
        if hi_i - lo_i < 3:
            return None
        k = 4
        best_i, best_s = None, 0.0
        for i in range(lo_i, hi_i + 1):
            a = stdp[max(0, i - k):i].mean() if i > 0 else 0.0
            b = stdp[i + 1:i + 1 + k].mean() if i + 1 < len(stdp) else 0.0
            outside, inside = (a, b) if inside_is_right else (b, a)
            step = max(0.0, (outside - inside) / (outside + inside + 1.0))
            s = float(prof[i]) * (0.25 + step)
            if s > best_s:
                best_i, best_s = i, s
        base = float(np.median(prof[lo_i:hi_i + 1])) * 0.25 + 0.5
        return best_i if best_i is not None and best_s > max(3.0, 2 * base) else None

    # Where to look: a webcam is a few faces wide around the face.
    left = edge(prof_x, std_x, px - 5 * pw, px - 0.55 * pw, True)
    right = edge(prof_x, std_x, px + 0.55 * pw, px + 5 * pw, False)
    top = edge(prof_y, std_y, py - 4 * ph, py - 0.55 * ph, True)
    bottom = edge(prof_y, std_y, py + 0.55 * ph, py + 4 * ph, False)

    # Missing edge: guess from the face; close to the frame border → the webcam touches it.
    def guess(v: float, size: int) -> float:
        if v < 0.05 * size:
            return 0.0
        if v > 0.95 * size:
            return float(size)
        return v
    x0 = (left + 1) if left is not None else guess(px - 2.2 * pw, W)
    x1 = (right + 1) if right is not None else guess(px + 2.2 * pw, W)
    y0 = (top + 1) if top is not None else guess(py - 1.6 * ph, H)
    y1 = (bottom + 1) if bottom is not None else guess(py + 2.0 * ph, H)
    x0, x1 = _clamp(x0, 0, W), _clamp(x1, 0, W)
    y0, y1 = _clamp(y0, 0, H), _clamp(y1, 0, H)
    if x1 - x0 < pw * 1.2 or y1 - y0 < ph * 1.2:
        out["note"] = "Не вдалося знайти межі вебки."
        return out
    out["cam"] = [round(x0 / W, 4), round(y0 / H, 4), round((x1 - x0) / W, 4), round((y1 - y0) / H, 4)]
    out["face"] = [round(fx, 4), round(fy, 4)]
    edges = sum(e is not None for e in (left, right, top, bottom))
    out["note"] = "" if edges >= 2 else "Межі вебки вгадано за обличчям, перевір рамку."
    return out


def clean_cam(cam: Any) -> Optional[List[float]]:
    """A user-sent rectangle → [x, y, w, h] inside the frame, or None."""
    if not isinstance(cam, (list, tuple)) or len(cam) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in cam)
    except (TypeError, ValueError):
        return None
    x, y = _clamp(x, 0, 0.98), _clamp(y, 0, 0.98)
    w, h = _clamp(w, 0.02, 1 - x), _clamp(h, 0.02, 1 - y)
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


# ─── 2. Layout plan ───────────────────────────────────────────────────────────

def clean_look(raw: Any, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """User-sent look (may be partial) over `base` (default LOOK_DEFAULT) → every key, in range."""
    o = dict(LOOK_DEFAULT)
    if base:
        o.update({k: v for k, v in base.items() if k in LOOK_DEFAULT})
    raw = raw if isinstance(raw, dict) else {}

    def num(k: str, lo: float, hi: float, nullable: bool = False) -> None:
        if k not in raw:
            return
        v = raw[k]
        if v is None or v == "":
            if nullable:
                o[k] = None
            return
        try:
            o[k] = round(_clamp(float(v), lo, hi), 4)
        except (TypeError, ValueError):
            pass
    if raw.get("layout") in LAYOUTS:
        o["layout"] = raw["layout"]
    num("share", SHARE_MIN, SHARE_MAX)
    num("cam_zoom", 1.0, 3.0)
    num("game_zoom", 1.0, 3.0)
    num("cam_fx", 0.0, 1.0, True)
    num("cam_fy", 0.0, 1.0, True)
    num("game_x", 0.0, 1.0)
    num("game_y", 0.0, 1.0)
    num("pip_w", 0.25, 1.0)
    num("game_w", 0.2, 4.0, True)
    num("game_cx", -1.0, 2.0)
    num("game_cy", -1.0, 2.0)
    num("nick_x", 0.0, 1.0, True)
    num("nick_y", 0.0, 1.0, True)
    if raw.get("nick_style") in NICK_STYLES:
        o["nick_style"] = raw["nick_style"]
    num("pip_x", 0.0, 1.0)
    num("pip_y", 0.0, 1.0)
    if "round" in raw:
        o["round"] = bool(raw["round"])
    if raw.get("frame") in FRAME_COLORS:
        o["frame"] = raw["frame"]
    if "divider" in raw:
        o["divider"] = bool(raw["divider"])
    if "nick" in raw:
        o["nick"] = re.sub(r"[{}\\\n\r]", "", str(raw.get("nick") or "")).strip()[:30]
    if o["layout"] not in LAYOUTS:
        o["layout"] = DEFAULT_LAYOUT
    return o


def _fit(aspect: float, box: Box, focus: Tuple[float, float], zoom: float = 1.0, bias: float = 0.45) -> Box:
    """The biggest piece of `box` with this aspect (w/h), made `zoom` times smaller, around focus (px)."""
    bx, by, bw, bh = box
    if bw / bh > aspect:
        w, h = bh * aspect, bh
    else:
        w, h = bw, bw / aspect
    w, h = w / zoom, h / zoom
    x = _clamp(focus[0] - w / 2, bx, bx + bw - w)
    y = _clamp(focus[1] - h * bias, by, by + bh - h)
    return (x, y, w, h)


def plan(cam: Optional[Sequence[float]], face: Optional[Sequence[float]], src_w: int, src_h: int,
         look: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Pure function (vertical.js streamPlan() is its twin for the live preview). Returns
    {"layout", "bg", "parts": [{"role", "src": [x,y,w,h] fractions, "dst": [x,y,w,h] px, "border", "radius", "color"}],
     "blur": [x,y,w,h] fractions | None, "pos": subtitle height in % | None, "hook_pos", "divider", "nick", "look"}.
    Parts are listed in drawing order.
    """
    lk = clean_look(look)
    W, H = float(src_w), float(src_h)
    layout = lk["layout"] if cam else "game"
    share = lk["share"]
    game_focus = (lk["game_x"] * W, lk["game_y"] * H)
    frame = (0.0, 0.0, W, H)
    m = 0.0
    if cam:
        cb = (cam[0] * W, cam[1] * H, cam[2] * W, cam[3] * H)
        m = 0.004 * W  # leave out the overlay's own border line
        if cb[2] > 8 * m and cb[3] > 8 * m:
            cb = (cb[0] + m, cb[1] + m, cb[2] - 2 * m, cb[3] - 2 * m)
        if lk["cam_fx"] is not None and lk["cam_fy"] is not None:
            f = (cb[0] + lk["cam_fx"] * cb[2], cb[1] + lk["cam_fy"] * cb[3])
        elif face:
            f = (face[0] * W, face[1] * H)
        else:
            f = (cb[0] + cb[2] / 2, cb[1] + cb[3] / 2)
        f = (_clamp(f[0], cb[0], cb[0] + cb[2]), _clamp(f[1], cb[1], cb[1] + cb[3]))
    color = FRAME_COLORS.get(lk["frame"])

    def frac(b: Box) -> List[float]:
        return [round(b[0] / W, 5), round(b[1] / H, 5), round(b[2] / W, 5), round(b[3] / H, 5)]

    def part(role: str, src: Box, dst: Tuple[int, int, int, int], border: int = 0, radius: int = 0) -> Dict[str, Any]:
        return {"role": role, "src": frac(src), "dst": [int(v) for v in dst], "border": border, "radius": radius,
                "color": color if border else None}

    parts: List[Dict[str, Any]] = []
    bg, pos, hook_pos, divider, nick_at = False, None, None, None, None
    if layout == "game":
        parts.append(part("game", _fit(OUT_W / OUT_H, frame, game_focus, lk["game_zoom"], 0.5), (0, 0, OUT_W, OUT_H)))
        nick_at = NICK_TOP
    elif layout in ("top", "full"):
        ch = _even(OUT_H * share)
        parts.append(part("cam", _fit(OUT_W / ch, cb, f, lk["cam_zoom"]), (0, 0, OUT_W, ch)))
        if layout == "top":
            gh = OUT_H - ch
            parts.append(part("game", _fit(OUT_W / gh, frame, game_focus, lk["game_zoom"], 0.5), (0, ch, OUT_W, gh)))
            pos = round(100 * ch / OUT_H, 1)
            if lk["divider"]:
                divider = [0, ch - 3, OUT_W, 6]
        else:
            # the whole game, but without the strip where the webcam sits (no blurred streamer in the game)
            gbox = frame
            cam_l, cam_r = cam[0], cam[0] + cam[2]
            if cam_r <= 0.4:
                gbox = (cam_r * W + 2, 0.0, W - cam_r * W - 2, H)
            elif cam_l >= 0.6:
                gbox = (0.0, 0.0, cam_l * W - 2, H)
            gh = min(_even(OUT_W * gbox[3] / gbox[2]), _even(OUT_H - ch - 260))
            bg = True
            parts.append(part("game", _fit(OUT_W / gh, gbox, game_focus, lk["game_zoom"], 0.5), (0, ch, OUT_W, gh)))
            pos = round(min(85.0, 100 * (ch + gh + 150) / OUT_H), 1)
            if lk["divider"]:
                divider = [0, ch - 3, OUT_W, 6]
        nick_at = NICK_TOP  # top-left of the webcam: the subtitles sit on the seam
    else:  # pip
        b = PIP_BORDER if color else 0
        pw = min(_even(OUT_W * lk["pip_w"]), OUT_W - 2 * b)
        ph = _even(pw * cb[3] / cb[2])
        ow, oh = pw + 2 * b, ph + 2 * b
        px = int(_clamp(lk["pip_x"] * OUT_W, 0, OUT_W - ow))
        py = int(_clamp(lk["pip_y"] * OUT_H, 0, OUT_H - oh))
        game, bg = _game_window(lk, frame, game_focus)
        parts.append({**part("game", game["src"], game["dst"]), "win": game["win"]})
        parts.append(part("cam", _fit(cb[2] / cb[3], cb, f, lk["cam_zoom"]), (px, py, ow, oh), b,
                          PIP_RADIUS if lk["round"] else 0))
        if py < OUT_H * 0.3:
            hook_pos = round(min(80.0, 100 * (py + oh + 40) / OUT_H), 1)  # the title goes under the window
        # under the window, or above it when the window is at the bottom
        nh = nick_size(lk["nick"] or "x", lk["nick_style"])[1]
        nick_at = (px + 8, py + oh + 14) if py + oh + 14 + nh < OUT_H - 40 else (px + 8, max(0, py - 14 - nh))
    blur = None
    if cam:
        mm = m + 4.0  # a few px around, so the overlay border goes too
        bx, by = max(0.0, cb[0] - mm), max(0.0, cb[1] - mm)
        blur = frac((bx, by, min(W, cb[0] + cb[2] + mm) - bx, min(H, cb[1] + cb[3] + mm) - by))
    nick = None
    if lk["nick"] and nick_at:
        nw, nh = nick_size(lk["nick"], lk["nick_style"])
        x, y = nick_at
        if lk["nick_x"] is not None and lk["nick_y"] is not None:
            x, y = lk["nick_x"] * OUT_W, lk["nick_y"] * OUT_H
        nick = {"text": lk["nick"], "style": lk["nick_style"], "w": nw, "h": nh,
                "x": int(_clamp(x, 0, OUT_W - nw)), "y": int(_clamp(y, 0, OUT_H - nh))}
    return {"layout": layout, "bg": bg, "parts": parts, "blur": blur, "pos": pos, "hook_pos": hook_pos,
            "divider": divider, "divider_color": color or "FFFFFF", "nick": nick, "look": lk}


WIN_KEEP = 120  # px of the game window that always stay on the phone


def _game_window(lk: Dict[str, Any], frame: Box, focus: Tuple[float, float]) -> Tuple[Dict[str, Any], bool]:
    """«Гра на весь екран»: the game is a 16:9 window on the phone (CapCut-like). Returns
    ({"src": visible piece of the source (px), "dst": where it lands, "win": the whole window, may stick out}, needs bg).
    game_w None: the game fills the phone, as before (win = the same picture as a window)."""
    W, H = frame[2], frame[3]
    shown = _fit(W / H, frame, focus, lk["game_zoom"], 0.5)  # the window shows this (the whole game at zoom 1)
    if lk["game_w"] is None:
        r = _fit(OUT_W / OUT_H, frame, focus, lk["game_zoom"], 0.5)
        k = OUT_H / r[3]
        win = [(shown[0] - r[0]) * k, (shown[1] - r[1]) * k, shown[2] * k, shown[3] * k]
        return {"src": r, "dst": (0, 0, OUT_W, OUT_H), "win": [round(v, 1) for v in win]}, False
    ww = lk["game_w"] * OUT_W
    wh = ww * shown[3] / shown[2]
    cx = _clamp(lk["game_cx"] * OUT_W, WIN_KEEP - ww / 2, OUT_W - WIN_KEEP + ww / 2)
    cy = _clamp(lk["game_cy"] * OUT_H, WIN_KEEP - wh / 2, OUT_H - WIN_KEEP + wh / 2)
    wx, wy = cx - ww / 2, cy - wh / 2
    x0, y0, x1, y1 = max(0.0, wx), max(0.0, wy), min(float(OUT_W), wx + ww), min(float(OUT_H), wy + wh)
    src = (shown[0] + (x0 - wx) / ww * shown[2], shown[1] + (y0 - wy) / wh * shown[3],
           (x1 - x0) / ww * shown[2], (y1 - y0) / wh * shown[3])
    covers = x0 <= 0 and y0 <= 0 and x1 >= OUT_W and y1 >= OUT_H
    dst = (0, 0, OUT_W, OUT_H) if covers else (x0, y0, x1 - x0, y1 - y0)
    return {"src": src, "dst": dst, "win": [round(v, 1) for v in (wx, wy, ww, wh)]}, not covers


def _nick_text(text: str, style: str) -> str:
    return text.upper() if NICK_STYLES[style][2] else text


def nick_size(text: str, style: str) -> Tuple[int, int]:
    """Width and height (px on the 1080×1920 phone) of the nick in this style."""
    import vertical_render
    font, size, _, px, py, icon = NICK_STYLES.get(style) or NICK_STYLES["twitch"]
    style = style if style in NICK_STYLES else "twitch"
    tw = vertical_render.text_width(font, False, _nick_text(text, style), size / FONT_EM[font])
    return int(round(tw + 2 * px + icon)), int(round(size * 1.25) + 2 * py)


def _ass_color(rgb: str) -> str:
    return f"&H{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def nick_events(nk: Dict[str, Any]) -> List[str]:
    """ASS Dialogue lines that draw the nick (plate shapes + text), layers 7-9, for the whole clip."""
    from vertical_render import rounded_rect
    style = nk.get("style") if nk.get("style") in NICK_STYLES else "twitch"
    font, size, _, padx, pady, icon = NICK_STYLES[style]
    x, y, w, h = nk["x"], nk["y"], nk["w"], nk["h"]
    text = _nick_text(nk["text"], style)
    span = "0:00:00.00,9:59:59.00"

    def shape(layer: int, path: str, color: str, extra: str = "") -> str:
        return f"Dialogue: {layer},{span},NickShape,,0,0,0,,{{\\an7\\pos(0,0){extra}\\p1\\1c{_ass_color(color)}\\bord0\\shad0}}{path}{{\\p0}}"

    fs = round(size / FONT_EM[font])

    def label(layer: int, tx: float, ty: float, tags: str) -> str:  # ty is ignored: the text is centred in the plate
        return f"Dialogue: {layer},{span},Nick,,0,0,0,,{{\\an4\\pos({tx:.0f},{y + h / 2:.0f}){tags}\\fn{font}\\fs{fs}}}{text}"

    out: List[str] = []
    if style == "twitch":
        out.append(shape(7, rounded_rect(x, y, x + w, y + h, 14), "9146FF"))
        ix, iy = x + padx, y + (h - 34) / 2
        bubble = rounded_rect(ix, iy, ix + 32, iy + 26, 5) + f" m {ix + 6:.0f} {iy + 25:.0f} l {ix + 6:.0f} {iy + 34:.0f} l {ix + 15:.0f} {iy + 25:.0f}"
        out.append(shape(8, bubble, "FFFFFF"))
        out.append(shape(9, f"m {ix + 11:.0f} {iy + 7:.0f} l {ix + 15:.0f} {iy + 7:.0f} l {ix + 15:.0f} {iy + 17:.0f} l {ix + 11:.0f} {iy + 17:.0f} "
                            f"m {ix + 20:.0f} {iy + 7:.0f} l {ix + 24:.0f} {iy + 7:.0f} l {ix + 24:.0f} {iy + 17:.0f} l {ix + 20:.0f} {iy + 17:.0f}", "9146FF"))
        out.append(label(9, x + padx + icon, y + pady, "\\1c&HFFFFFF&\\bord0\\shad0"))
    elif style == "neon":
        out.append(label(7, x + padx, y + pady, "\\1c&HFFFFFF&\\3c&HFFE500&\\bord7\\blur14\\shad0"))
        out.append(label(8, x + padx, y + pady, "\\1c&HFFFFFF&\\3c&HFFE500&\\bord2\\blur1\\shad0"))
    elif style == "pill":
        out.append(shape(7, rounded_rect(x, y + 5, x + w, y + h + 5, h / 2), "000000"))
        out.append(shape(8, rounded_rect(x, y, x + w, y + h, h / 2), "FFFFFF"))
        out.append(label(9, x + padx, y + pady, "\\1c&H111111&\\bord0\\shad0"))
    elif style == "bold":
        out.append(label(8, x + padx, y + pady, "\\i1\\1c&H00D4FF&\\3c&H000000&\\4c&H000000&\\bord4\\shad5"))
    else:  # tape
        org = f"\\org({x + w / 2:.0f},{y + h / 2:.0f})\\frz4"
        out.append(shape(7, rounded_rect(x + 7, y + 7, x + w + 7, y + h + 7, 2), "111111", org))
        out.append(shape(8, rounded_rect(x, y, x + w, y + h, 2), "FFD400", org))
        out.append(label(9, x + padx, y + pady, org + "\\1c&H111111&\\bord0\\shad0"))
    return out


def nick_ass(ass_text: Optional[str], sp: Dict[str, Any]) -> Optional[str]:
    """Adds the streamer's nick (all clip long) to the subtitle document, or makes one just for it."""
    nk = sp.get("nick")
    if not nk:
        return ass_text
    styles = ("Style: Nick,Rubik Black,44,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1\n"
              "Style: NickShape,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")
    lines = "\n".join(nick_events(nk))
    if not ass_text:
        return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
                "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
                "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding\n" + styles + "\n\n[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + lines + "\n")
    out = re.sub(r"(\[V4\+ Styles\]\nFormat:[^\n]*\n)", lambda mt: mt.group(1) + styles + "\n", ass_text, count=1)
    return out.rstrip("\n") + "\n" + lines + "\n"


# ─── 2b. Scenes inside a clip ─────────────────────────────────────────────────

CHAT_FACE_H = 0.2     # a face this tall (share of the frame) is the streamer on the whole screen («Just Chatting»)
SCENE_MIN = {"chat": 2.0, "cam": 3.0}  # shorter runs are noise (a game cutscene, a passing character)


def _inside(cam: Sequence[float], f: Sequence[float], m: float = 0.03) -> bool:
    return cam[0] - m <= f[0] <= cam[0] + cam[2] + m and cam[1] - m <= f[1] <= cam[1] + cam[3] + m


def _median(v: List[float]) -> float:
    v = sorted(v)
    return v[len(v) // 2]


def scenes(samples: Sequence[Sequence[Any]], cam: Optional[Sequence[float]], face: Optional[Sequence[float]] = None
           ) -> List[Dict[str, Any]]:
    """
    Pure. Face samples of one clip ([[t, [[cx, cy, w, h, score], ...]], ...] as vertical_render.detect_faces gives)
    → the parts of the clip that are not the usual «webcam + game»:
      {"kind": "chat", "start", "end", "x"}  — the streamer fills the screen (no game): a 9:16 crop around the face;
      {"kind": "cam",  "start", "end", "cam", "face"} — the webcam moved to another spot: the same layout with it.
    Times are seconds from the clip start; the rest of the clip keeps the video's layout.
    """
    if not samples or not cam:
        return []
    step = samples[1][0] - samples[0][0] if len(samples) > 1 else 0.25
    labels: List[Optional[Tuple[str, Tuple[float, float]]]] = []
    for _, faces in samples:
        faces = [f for f in faces if len(f) >= 4]
        in_cam = [f for f in faces if _inside(cam, f)]
        big = [f for f in faces if f[3] >= CHAT_FACE_H and not _inside(cam, f)]
        small_out = [f for f in faces if f[3] < CHAT_FACE_H and not _inside(cam, f)]
        if big and not in_cam:
            b = max(big, key=lambda f: f[3])
            labels.append(("chat", (b[0], b[1])))
        elif in_cam:
            labels.append(("normal", (0.0, 0.0)))
        elif small_out and len(small_out) == 1:
            labels.append(("cam", (small_out[0][0], small_out[0][1])))
        else:
            labels.append(None)  # no face (or a crowd): the scene goes on
    # a sample without a face keeps the scene around it
    last = next((lb for lb in labels if lb), ("normal", (0.0, 0.0)))
    for i, lb in enumerate(labels):
        if lb is None:
            labels[i] = last
        else:
            last = lb
    runs: List[Dict[str, Any]] = []
    for i, (kind, pt) in enumerate(labels):  # type: ignore[misc]
        if runs and runs[-1]["kind"] == kind:
            runs[-1]["n"] += 1
            runs[-1]["pts"].append(pt)
        else:
            runs.append({"kind": kind, "i": i, "n": 1, "pts": [pt]})
    out: List[Dict[str, Any]] = []
    for r in runs:
        if r["kind"] == "normal" or r["n"] * step < SCENE_MIN[r["kind"]]:
            continue
        xs, ys = [p[0] for p in r["pts"]], [p[1] for p in r["pts"]]
        if r["kind"] == "cam" and (max(xs) - min(xs) > 0.06 or max(ys) - min(ys) > 0.06):
            continue  # a moving face is someone in the game, not a webcam
        start = round(samples[r["i"]][0], 3)
        end = round(samples[r["i"] + r["n"] - 1][0] + step, 3)
        fx, fy = _median(xs), _median(ys)
        if r["kind"] == "chat":
            out.append({"kind": "chat", "start": start, "end": end, "x": round(fx, 4)})
        else:
            ox, oy = (face[0], face[1]) if face else (cam[0] + cam[2] / 2, cam[1] + cam[3] * 0.45)
            nx = _clamp(cam[0] + fx - ox, 0.0, 1.0 - cam[2])
            ny = _clamp(cam[1] + fy - oy, 0.0, 1.0 - cam[3])
            out.append({"kind": "cam", "start": start, "end": end, "cam": [round(nx, 4), round(ny, 4), cam[2], cam[3]],
                        "face": [round(fx, 4), round(fy, 4)]})
    return out


def with_scenes(sp: Dict[str, Any], found: List[Dict[str, Any]], src_w: int, src_h: int) -> Dict[str, Any]:
    """Adds each scene's own picture to the plan: «chat» a 9:16 crop, «cam» a plan with the moved webcam."""
    out = []
    for sc in found:
        sc = dict(sc)
        if sc["kind"] == "chat":
            w = min(1.0, (src_h * OUT_W / OUT_H) / src_w)
            sc["src"] = [round(_clamp(sc["x"] - w / 2, 0.0, 1.0 - w), 5), 0.0, round(w, 5), 1.0]
        else:
            sub = plan(sc["cam"], sc["face"], src_w, src_h, sp.get("look"))
            sc["plan"] = {k: sub[k] for k in ("layout", "bg", "parts", "blur", "divider", "divider_color")}
        out.append(sc)
    return {**sp, "scenes": out}


# ─── 3. ffmpeg ────────────────────────────────────────────────────────────────

def _px(fr: Sequence[float], src_w: int, src_h: int) -> Tuple[int, int, int, int]:
    w = _even(min(src_w, fr[2] * src_w))
    h = _even(min(src_h, fr[3] * src_h))
    x = int(_clamp(fr[0] * src_w, 0, src_w - w))
    y = int(_clamp(fr[1] * src_h, 0, src_h - h))
    return x, y, w, h


def _round_mask(r: int) -> str:
    """geq alpha: transparent outside a rounded rectangle with corner radius r."""
    return (f"geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='if(gt(abs(X-W/2),W/2-{r})*gt(abs(Y-H/2),H/2-{r}),"
            f"if(lte(hypot(abs(X-W/2)-(W/2-{r}),abs(Y-H/2)-(H/2-{r})),{r}),255,0),255)'")


def video_filter(sp: Dict[str, Any], src_w: int, src_h: int, fps: int = 30) -> str:
    """filter_complex producing [base] (1080×1920) from input [0:v]; scenes (with_scenes) are laid over in their time."""
    found = sp.get("scenes") or []
    if not found:
        return _one_filter(sp, src_w, src_h, fps)
    n = len(found) + 1
    chains = [f"[0:v]split={n}" + "".join(f"[sv{i}]" for i in range(n))]
    chains.append(_tagged(_one_filter(sp, src_w, src_h, fps), "m", "[sv0]"))
    cur = "[mbase]"
    for i, sc in enumerate(found, 1):
        if sc["kind"] == "chat":
            x, y, w, h = _px(sc["src"], src_w, src_h)
            chains.append(f"[sv{i}]crop={w}:{h}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1,fps={fps},format=yuv420p[s{i}base]")
        else:
            chains.append(_tagged(_one_filter(sc["plan"], src_w, src_h, fps), f"s{i}", f"[sv{i}]"))
        chains.append(f"{cur}[s{i}base]overlay=0:0:enable='between(t,{sc['start']:.3f},{sc['end']:.3f})'[sc{i}]")
        cur = f"[sc{i}]"
    chains.append(f"{cur}null[base]")
    return ";".join(chains)


def _tagged(fc: str, tag: str, inp: str) -> str:
    """Renames a filter graph's own labels (so several can live in one graph) and feeds it from `inp`."""
    fc = re.sub(r"\[(?!0:v\])([A-Za-z][A-Za-z0-9]*)\]", lambda mt: f"[{tag}{mt.group(1)}]", fc)
    return fc.replace("[0:v]", inp)


def _one_filter(sp: Dict[str, Any], src_w: int, src_h: int, fps: int = 30) -> str:
    parts = sp["parts"]
    games = [p for p in parts if p["role"] == "game"]
    cams = [p for p in parts if p["role"] == "cam"]
    chains: List[str] = []
    n_clean = len(games) + (1 if sp.get("bg") else 0)
    raw = ["[raw]"] + [f"[rc{i}]" for i in range(len(cams))]
    chains.append(f"[0:v]split={len(raw)}" + "".join(raw))
    # the game picture with the webcam blurred out
    if sp.get("blur"):
        bx, by, bw, bh = _px(sp["blur"], src_w, src_h)
        chains.append("[raw]split=2[ra][rb]")
        chains.append(f"[rb]crop={bw}:{bh}:{bx}:{by},boxblur=20:3[rbl]")
        chains.append(f"[ra][rbl]overlay={bx}:{by}[clean]")
    else:
        chains.append("[raw]null[clean]")
    clean = [f"[cl{i}]" for i in range(n_clean)]
    chains.append(f"[clean]split={n_clean}" + "".join(clean) if n_clean > 1 else f"[clean]null{clean[0]}")
    labels: Dict[int, str] = {}
    gi = ci = 0
    for k, p in enumerate(parts):
        x, y, w, h = _px(p["src"], src_w, src_h)
        b = int(p.get("border") or 0)
        r = int(p.get("radius") or 0)
        dw, dh = _even(p["dst"][2] - 2 * b), _even(p["dst"][3] - 2 * b)
        if p["role"] == "game":
            src = clean[gi]
            gi += 1
        else:
            src = f"[rc{ci}]"
            ci += 1
        base = f"{src}crop={w}:{h}:{x}:{y},scale={dw}:{dh}:flags=lanczos,setsar=1"
        col = p.get("color") or "FFFFFF"
        if r:
            ow, oh = dw + 2 * b, dh + 2 * b
            chains.append(f"{base},format=rgba,{_round_mask(max(2, r - b))}[pi{k}]")
            if b:
                chains.append(f"color=c=0x{col}:s={ow}x{oh},format=rgba,{_round_mask(r)}[pf{k}]")
                chains.append(f"[pf{k}][pi{k}]overlay={b}:{b}:shortest=1[p{k}]")
            else:
                chains.append(f"[pi{k}]null[p{k}]")
        else:
            pad = f",pad={dw + 2 * b}:{dh + 2 * b}:{b}:{b}:color=0x{col}" if b else ""
            chains.append(f"{base}{pad}[p{k}]")
        labels[k] = f"[p{k}]"
    tail = ""
    if sp.get("divider"):
        dx, dy, dw, dh = sp["divider"]
        tail = f",drawbox=x={dx}:y={dy}:w={dw}:h={dh}:color=0x{sp.get('divider_color') or 'FFFFFF'}:t=fill"
    if sp.get("bg"):
        chains.append(f"{clean[-1]}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
                      f"boxblur=25:2,eq=brightness=-0.12,setsar=1[bg]")
        cur, start = "[bg]", 0
    else:
        first = parts[0]
        if first["dst"] != [0, 0, OUT_W, OUT_H]:
            # «top»: the parts fill the phone from top to bottom
            chains.append("".join(labels[k] for k in range(len(parts))) + f"vstack={len(parts)}[stk]")
            chains.append(f"[stk]fps={fps}{tail},format=yuv420p[base]")
            return ";".join(chains)
        cur, start = labels[0], 1
    for k in range(start, len(parts)):
        x, y = parts[k]["dst"][0], parts[k]["dst"][1]
        chains.append(f"{cur}{labels[k]}overlay={x}:{y}[o{k}]")
        cur = f"[o{k}]"
    chains.append(f"{cur}fps={fps}{tail},format=yuv420p[base]")
    return ";".join(chains)

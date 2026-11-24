"""
streamer.py — «Стрім: вебка + гра»: vertical 9:16 clips from stream recordings.

A stream recording is a game (or screen) with the streamer's webcam pasted into a corner.
  1. detect_webcam(): once per video, finds the webcam rectangle. A webcam is a face that sits in the
     same small spot in most frames; the rectangle's edges are lines that stay put in every frame
     (the overlay border) with changing game picture outside and a calmer room inside.
  2. plan(): turns the rectangle + chosen layout into «parts»: which piece of the source goes where
     on the 1080×1920 phone. The browser preview (vertical.js) and the renderer draw the same parts.
  3. video_filter(): the ffmpeg filter_complex for those parts.

Layouts:
  top  — webcam on top (share of the height), game below, cropped around game_x;
  full — webcam on top, the whole 16:9 game under it, blurred game behind;
  pip  — game on the whole phone, webcam as a small window in the corner;
  game — no webcam (none found or switched off): the game only.
In every layout the webcam inside the game picture is blurred, so the face is not shown twice.
"""
from __future__ import annotations

import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple

OUT_W, OUT_H = 1080, 1920
LAYOUTS = ("top", "full", "pip")
DEFAULT_LAYOUT = "top"
SHARE_MIN, SHARE_MAX, DEFAULT_SHARE = 0.22, 0.5, 0.34
PIP_MARGIN, PIP_BORDER = 40, 6
SAMPLES = 20

Box = Tuple[float, float, float, float]  # x, y, w, h


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

def _fit(aspect: float, box: Box, focus: Tuple[float, float]) -> Box:
    """The biggest piece of `box` with this aspect (w/h), centred on focus (in px)."""
    bx, by, bw, bh = box
    if bw / bh > aspect:
        w, h = bh * aspect, bh
        x = _clamp(focus[0] - w / 2, bx, bx + bw - w)
        return (x, by, w, h)
    w, h = bw, bw / aspect
    y = _clamp(focus[1] - h * 0.45, by, by + bh - h)
    return (bx, y, w, h)


def plan(cam: Optional[Sequence[float]], face: Optional[Sequence[float]], src_w: int, src_h: int,
         layout: str = DEFAULT_LAYOUT, share: Optional[float] = None, game_x: float = 0.5) -> Dict[str, Any]:
    """
    Pure function. Returns {"layout", "bg", "parts": [{"role", "src": [x,y,w,h] fractions, "dst": [x,y,w,h] px,
    "border"}], "blur": [x,y,w,h] fractions | None, "pos": subtitle height in %}. Parts are listed in drawing order.
    """
    W, H = float(src_w), float(src_h)
    layout = layout if layout in LAYOUTS else DEFAULT_LAYOUT
    share = _clamp(float(share if share is not None else DEFAULT_SHARE), SHARE_MIN, SHARE_MAX)
    game_focus = (_clamp(float(game_x), 0, 1) * W, H / 2)
    frame = (0.0, 0.0, W, H)
    if not cam:
        layout = "game"
    else:
        cb = (cam[0] * W, cam[1] * H, cam[2] * W, cam[3] * H)
        m = 0.004 * W  # leave out the overlay's own border line
        if cb[2] > 8 * m and cb[3] > 8 * m:
            cb = (cb[0] + m, cb[1] + m, cb[2] - 2 * m, cb[3] - 2 * m)
        f = (face[0] * W, face[1] * H) if face else (cb[0] + cb[2] / 2, cb[1] + cb[3] / 2)
        f = (_clamp(f[0], cb[0], cb[0] + cb[2]), _clamp(f[1], cb[1], cb[1] + cb[3]))

    def frac(b: Box) -> List[float]:
        return [round(b[0] / W, 5), round(b[1] / H, 5), round(b[2] / W, 5), round(b[3] / H, 5)]

    def part(role: str, src: Box, dst: Tuple[int, int, int, int], border: int = 0) -> Dict[str, Any]:
        return {"role": role, "src": frac(src), "dst": list(dst), "border": border}

    parts: List[Dict[str, Any]] = []
    bg, pos, hook_pos = False, None, None
    if layout == "game":
        parts.append(part("game", _fit(OUT_W / OUT_H, frame, game_focus), (0, 0, OUT_W, OUT_H)))
    elif layout == "top":
        ch = _even(OUT_H * share)
        gh = OUT_H - ch
        parts.append(part("cam", _fit(OUT_W / ch, cb, f), (0, 0, OUT_W, ch)))
        parts.append(part("game", _fit(OUT_W / gh, frame, game_focus), (0, ch, OUT_W, gh)))
        pos = round(100 * ch / OUT_H, 1)
    elif layout == "full":
        ch = _even(OUT_H * share)
        gh = _even(OUT_W * H / W)
        bg = True
        parts.append(part("cam", _fit(OUT_W / ch, cb, f), (0, 0, OUT_W, ch)))
        parts.append(part("game", frame, (0, ch, OUT_W, gh)))
        pos = round(min(85.0, 100 * (ch + gh + 150) / OUT_H), 1)
    else:  # pip
        pw = _even(OUT_W * min(0.7, share * 1.4))
        ph = _even(pw * cb[3] / cb[2])
        parts.append(part("game", _fit(OUT_W / OUT_H, frame, game_focus), (0, 0, OUT_W, OUT_H)))
        parts.append(part("cam", cb, (PIP_MARGIN, PIP_MARGIN + 24, pw + 2 * PIP_BORDER, ph + 2 * PIP_BORDER), PIP_BORDER))
        hook_pos = round(100 * (PIP_MARGIN + 24 + ph + 2 * PIP_BORDER + 40) / OUT_H, 1)  # the title goes under the window
    blur = None
    if cam:
        m += 4.0  # a few px around, so the overlay border goes too
        bx, by = max(0.0, cb[0] - m), max(0.0, cb[1] - m)
        blur = frac((bx, by, min(W, cb[0] + cb[2] + m) - bx, min(H, cb[1] + cb[3] + m) - by))
    return {"layout": layout, "bg": bg, "parts": parts, "blur": blur, "pos": pos, "hook_pos": hook_pos, "share": round(share, 3)}


# ─── 3. ffmpeg ────────────────────────────────────────────────────────────────

def _px(fr: Sequence[float], src_w: int, src_h: int) -> Tuple[int, int, int, int]:
    w = _even(min(src_w, fr[2] * src_w))
    h = _even(min(src_h, fr[3] * src_h))
    x = int(_clamp(fr[0] * src_w, 0, src_w - w))
    y = int(_clamp(fr[1] * src_h, 0, src_h - h))
    return x, y, w, h


def video_filter(sp: Dict[str, Any], src_w: int, src_h: int, fps: int = 30) -> str:
    """filter_complex producing [base] (1080×1920) from input [0:v]."""
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
        chains.append(f"[raw]split=2[ra][rb]")
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
        dw, dh = p["dst"][2] - 2 * b, p["dst"][3] - 2 * b
        if p["role"] == "game":
            src = clean[gi]
            gi += 1
        else:
            src = f"[rc{ci}]"
            ci += 1
        pad = f",pad={dw + 2 * b}:{dh + 2 * b}:{b}:{b}:color=white" if b else ""
        chains.append(f"{src}crop={w}:{h}:{x}:{y},scale={dw}:{dh}:flags=lanczos,setsar=1{pad}[p{k}]")
        labels[k] = f"[p{k}]"
    if sp.get("bg"):
        chains.append(f"{clean[-1]}scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
                      f"boxblur=25:2,eq=brightness=-0.12,setsar=1[bg]")
        cur, start = "[bg]", 0
    else:
        first = parts[0]
        if first["dst"] != [0, 0, OUT_W, OUT_H]:
            # «top»: the parts fill the phone from top to bottom
            chains.append("".join(labels[k] for k in range(len(parts))) + f"vstack={len(parts)}[stk]")
            chains.append(f"[stk]fps={fps}[base]")
            return ";".join(chains)
        cur, start = labels[0], 1
    for k in range(start, len(parts)):
        x, y = parts[k]["dst"][0], parts[k]["dst"][1]
        chains.append(f"{cur}{labels[k]}overlay={x}:{y}[o{k}]")
        cur = f"[o{k}]"
    chains.append(f"{cur}fps={fps}[base]")
    return ";".join(chains)

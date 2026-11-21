"""
vertical_render.py — vertical 9:16 clips with burned-in subtitles (stage 4).

Three parts, each usable on its own:
  1. Captions: clip words (from the Whisper word cache, with the user's edits) → an .ass file
     in one of CAPTION_STYLES, word-by-word highlight like Opus Clip / Submagic.
  2. Reframing: find faces (OpenCV YuNet, offline) a few times per second and plan the 9:16 crop:
     follow one speaker ("track"), stack two speakers ("split") or fit the whole frame on a
     blurred background ("blur"). "auto" picks one of these by itself.
  3. Rendering: one ffmpeg run (NVENC when available) → 1080×1920, 30 fps, H.264 + AAC.

The plain 16:9 clip download is untouched; this is an extra export.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import threading
import uuid
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("shorts_core")

ROOT = Path(__file__).resolve().parent
FONTS_DIR = ROOT / "frontend" / "fonts"
YUNET_MODEL = ROOT / "assets" / "models" / "face_detection_yunet_2023mar.onnx"
RENDER_TMP = ROOT / ".render_tmp"

OUT_W, OUT_H, OUT_FPS = 1080, 1920, 30
RENDER_VERSION = 5  # bump to invalidate every cached vertical file after a renderer change

# One render at a time: the GPU encoder and the face detector share the laptop.
RENDER_LOCK = threading.Lock()

FRAME_MODES = ("auto", "track", "blur", "split", "center")


# ─── 1. Caption styles ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaptionStyle:
    id: str
    name: str
    font: str
    size: int
    upper: bool
    max_words: int
    max_chars: int
    mode: str              # "box" (active word on a plate) | "color" (active word recolored) | "fill" (karaoke) | "one" | "none"
    primary: str = "FFFFFF"   # RGB hex
    active: str = "FFE14D"
    key: str = "FFCF33"
    outline: float = 6.0
    shadow: float = 3.0
    bold: bool = False
    back_box: Optional[str] = None  # RGBA hex of the plate behind the whole line ("none"-mode box style)
    default_pos: int = 66


CAPTION_STYLES: Dict[str, CaptionStyle] = {
    s.id: s
    for s in (
        CaptionStyle("hl", "Хайлайт", "Montserrat Black", 100, True, 3, 16, "box", active="FFE14D", outline=7, shadow=4),
        CaptionStyle("kar", "Караоке", "Rubik Black", 90, False, 6, 22, "fill", active="7CFF4F", outline=6, shadow=3),
        CaptionStyle("one", "Одне слово", "Unbounded Black", 116, True, 1, 14, "one", key="FFCF33", outline=8, shadow=5),
        CaptionStyle("box", "Блок", "Rubik", 72, False, 6, 30, "none", outline=16, shadow=0, bold=True, back_box="000000B8"),
        CaptionStyle("gold", "Чорне золото", "Russo One", 96, True, 3, 16, "color", primary="FFF8EC", active="E6C068", outline=5, shadow=4),
        CaptionStyle("min", "Мінімал", "Oswald SemiBold", 70, False, 7, 34, "none", outline=2.5, shadow=2, default_pos=72),
    )
}
DEFAULT_STYLE = "hl"


def _ass_color(rgb: str, alpha: int = 0) -> str:
    """'FFE14D' → '&H004DE1FF' (ASS is AABBGGRR, alpha 0 = opaque)."""
    rgb = rgb.strip("#")[:6].upper()
    return f"&H{alpha:02X}{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"


def _ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    cs = int(round(sec * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_text(t: str) -> str:
    return t.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


_NUM_RE = re.compile(r"\d")
_WORD_RE = re.compile(r"[\w'’ʼ-]+", re.UNICODE)


def _norm(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower())


def clip_words(
    all_words: Sequence[Dict[str, Any]],
    start: float,
    end: float,
    edits: Optional[Dict[str, Any]] = None,
    title: str = "",
) -> List[Dict[str, Any]]:
    """
    Words inside [start, end], times relative to the clip start, with the user's edits applied.
    edits: {"<absolute start, 2 decimals>": {"w": "new text" ("" deletes), "k": true/false}}.
    Each result: {"start", "end", "word", "key", "id"} where id is the edits key.
    """
    edits = edits or {}
    title_words = {_norm(w) for w in _WORD_RE.findall(title or "") if len(_norm(w)) >= 5}
    out: List[Dict[str, Any]] = []
    for w in all_words:
        try:
            ws, we = float(w.get("start", 0.0)), float(w.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if we <= start or ws >= end:
            continue
        mid = (ws + we) / 2
        if mid < start or mid > end:
            continue
        wid = f"{ws:.2f}"
        text = str(w.get("word", "")).strip()
        e = edits.get(wid) or {}
        if isinstance(e, dict) and "w" in e:
            text = str(e["w"]).strip()
        if not text:
            continue
        auto_key = bool(_NUM_RE.search(text)) or (_norm(text) in title_words) or len(_norm(text)) >= 10
        key = bool(e.get("k")) if isinstance(e, dict) and "k" in e else auto_key
        out.append({
            "id": wid,
            "start": round(max(0.0, ws - start), 3),
            "end": round(min(end, we) - start, 3),
            "word": text,
            "key": key,
        })
    return out


def chunk_words(words: Sequence[Dict[str, Any]], max_words: int, max_chars: int, max_gap: float = 0.7) -> List[List[Dict[str, Any]]]:
    """Groups words into on-screen lines: by count, by length, at sentence ends and at pauses."""
    chunks: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            chars = sum(len(x["word"]) + 1 for x in cur) + len(w["word"])
            if len(cur) >= max_words or chars > max_chars or gap > max_gap:
                chunks.append(cur)
                cur = []
        cur.append(w)
        if max_words > 1 and re.search(r"[.!?…]$", w["word"]) and len(cur) >= 2:
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


# Font files behind the family names used in the .ass (libass picks the same files via fontsdir).
FONT_FILES: Dict[Tuple[str, bool], str] = {
    ("Montserrat Black", False): "Montserrat-Black.ttf",
    ("Rubik Black", False): "Rubik-Black.ttf",
    ("Rubik", True): "Rubik-Bold.ttf",
    ("Unbounded Black", False): "Unbounded-Black.ttf",
    ("Russo One", False): "RussoOne-Regular.ttf",
    ("Oswald SemiBold", False): "Oswald-SemiBold.ttf",
}


class _FontMetrics:
    """Advance widths from a TrueType file (cmap 4/12 + hmtx), scaled the way libass scales fonts:
    the .ass font size equals the OS/2 winAscent+winDescent height. Measured against libass to ±1 px."""

    def __init__(self, path: Path):
        d = path.read_bytes()
        tables = {}
        for i in range(struct.unpack(">H", d[4:6])[0]):
            tag, _, off, ln = struct.unpack(">4sIII", d[12 + 16 * i:28 + 16 * i])
            tables[tag.decode("latin-1")] = off
        o = tables["hhea"]
        n_hm = struct.unpack(">H", d[o + 34:o + 36])[0]
        o = tables["OS/2"]
        asc, desc = struct.unpack(">HH", d[o + 74:o + 78])
        self.height = float(asc + desc)
        o = tables["hmtx"]
        self.adv = [struct.unpack(">H", d[o + 4 * i:o + 4 * i + 2])[0] for i in range(n_hm)]
        self.cmap: Dict[int, int] = {}
        o = tables["cmap"]
        subs = [struct.unpack(">HHI", d[o + 4 + 8 * i:o + 12 + 8 * i]) for i in range(struct.unpack(">H", d[o + 2:o + 4])[0])]
        subs.sort(key=lambda x: 0 if struct.unpack(">H", d[o + x[2]:o + x[2] + 2])[0] == 12 else 1)
        for pid, _eid, so in subs:
            so += o
            fmt = struct.unpack(">H", d[so:so + 2])[0]
            if fmt == 12:
                for g in range(struct.unpack(">I", d[so + 12:so + 16])[0]):
                    a, b, gid = struct.unpack(">III", d[so + 16 + 12 * g:so + 28 + 12 * g])
                    for c in range(a, min(b, 0x2FFFF) + 1):
                        self.cmap.setdefault(c, gid + c - a)
                break
            if fmt == 4 and pid in (0, 3):
                seg = struct.unpack(">H", d[so + 6:so + 8])[0] // 2
                e = so + 14
                ends = struct.unpack(f">{seg}H", d[e:e + 2 * seg])
                st_ = e + 2 * seg + 2
                starts = struct.unpack(f">{seg}H", d[st_:st_ + 2 * seg])
                dl = st_ + 2 * seg
                deltas = struct.unpack(f">{seg}h", d[dl:dl + 2 * seg])
                ro = dl + 2 * seg
                offs = struct.unpack(f">{seg}H", d[ro:ro + 2 * seg])
                for i in range(seg):
                    for c in range(starts[i], ends[i] + 1):
                        if c == 0xFFFF:
                            continue
                        if offs[i] == 0:
                            g = (c + deltas[i]) & 0xFFFF
                        else:
                            a = ro + 2 * i + offs[i] + 2 * (c - starts[i])
                            g = struct.unpack(">H", d[a:a + 2])[0]
                            g = (g + deltas[i]) & 0xFFFF if g else 0
                        self.cmap.setdefault(c, g)
                break

    def width(self, text: str, size: float) -> float:
        last = len(self.adv) - 1
        return sum(self.adv[min(self.cmap.get(ord(ch), 0), last)] for ch in text) * size / self.height


@lru_cache(maxsize=None)
def _metrics(font: str, bold: bool) -> Optional[_FontMetrics]:
    name = FONT_FILES.get((font, bold))
    try:
        return _FontMetrics(FONTS_DIR / name) if name else None
    except Exception as e:  # pragma: no cover - a broken font file must not break rendering
        log.warning("font metrics for %s failed: %s", name, e)
        return None


def text_width(font: str, bold: bool, text: str, size: float) -> float:
    m = _metrics(font, bold)
    return m.width(text, size) if m else len(text) * size * 0.6


def wrap_lines(widths: Sequence[float], space: float, max_w: float) -> List[List[int]]:
    """Greedy line breaking of word widths → lists of word indexes (one list per line)."""
    lines: List[List[int]] = []
    cur: List[int] = []
    cur_w = 0.0
    for i, w in enumerate(widths):
        if cur and cur_w + space + w > max_w:
            lines.append(cur)
            cur, cur_w = [], 0.0
        cur_w = w if not cur else cur_w + space + w
        cur.append(i)
    if cur:
        lines.append(cur)
    return lines


def rounded_rect(x1: float, y1: float, x2: float, y2: float, r: float) -> str:
    """ASS vector drawing (\\p1) of a rectangle with rounded corners (r=0 → square)."""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    f = lambda v: str(int(round(v)))  # noqa: E731
    if r < 1:
        return f"m {f(x1)} {f(y1)} l {f(x2)} {f(y1)} {f(x2)} {f(y2)} {f(x1)} {f(y2)}"
    k = r * 0.4477  # control point offset for a circular arc (1 - 0.5523)
    return (
        f"m {f(x1 + r)} {f(y1)} l {f(x2 - r)} {f(y1)} b {f(x2 - k)} {f(y1)} {f(x2)} {f(y1 + k)} {f(x2)} {f(y1 + r)} "
        f"l {f(x2)} {f(y2 - r)} b {f(x2)} {f(y2 - k)} {f(x2 - k)} {f(y2)} {f(x2 - r)} {f(y2)} "
        f"l {f(x1 + r)} {f(y2)} b {f(x1 + k)} {f(y2)} {f(x1)} {f(y2 - k)} {f(x1)} {f(y2 - r)} "
        f"l {f(x1)} {f(y1 + r)} b {f(x1)} {f(y1 + k)} {f(x1 + k)} {f(y1)} {f(x1 + r)} {f(y1)}"
    )


def _style_line(name: str, st: CaptionStyle, *, border_style: int = 1, primary: Optional[str] = None,
                outline_col: str = "000000", outline_alpha: int = 0, back: str = "000000", back_alpha: int = 0x80,
                outline: Optional[float] = None, shadow: Optional[float] = None, primary_alpha: int = 0,
                align: int = 5) -> str:
    return (
        f"Style: {name},{st.font},{st.size},{_ass_color(primary or st.primary, primary_alpha)},{_ass_color(st.primary)},"
        f"{_ass_color(outline_col, outline_alpha)},{_ass_color(back, back_alpha)},{-1 if st.bold else 0},0,0,0,100,100,0,0,"
        f"{border_style},{st.outline if outline is None else outline},{st.shadow if shadow is None else shadow},{align},90,90,0,1"
    )


LIGHT_TEXT = ("FFFFFF", "FFF8EC")
MAX_LINE_W = OUT_W - 2 * 90  # same side margins as the style lines


def build_ass(
    words: Sequence[Dict[str, Any]],
    duration: float,
    style_id: str = DEFAULT_STYLE,
    pos_pct: Optional[float] = None,
    hook: Optional[str] = None,
    hook_sec: Optional[float] = 3.0,
    size: float = 1.0,
    accent: Optional[str] = None,
    text_color: Optional[str] = None,
    hook_style: str = "white",
    hook_size: float = 1.0,
    round_plates: bool = False,
    stroke: Optional[str] = None,
    hook_round: bool = False,
    hook_stroke: Optional[str] = None,
    hook_pos: Optional[float] = None,
) -> str:
    """
    Builds the .ass subtitle document (1080×1920) for one clip.
    size / hook_size scale the fonts; accent recolours the highlighted & key words; text_color the rest;
    hook_sec=None keeps the title on screen for the whole clip.
    Plates are drawn as vector shapes (square or rounded) from measured word widths, so they can have
    round corners and an edge. stroke / hook_stroke ("000000" / "FFFFFF"): colour of the letters' outline
    and of a plate's edge; None keeps the style's own look (black letter outline, no plate edge).
    """
    st = CAPTION_STYLES.get(style_id) or CAPTION_STYLES[DEFAULT_STYLE]
    size = max(0.6, min(1.6, float(size or 1.0)))
    st = replace(st, size=int(round(st.size * size)), outline=round(st.outline * size, 1), shadow=round(st.shadow * size, 1),
                 active=accent or st.active, key=accent or st.key, primary=text_color or st.primary)
    y = int(OUT_H * (max(20.0, min(85.0, float(pos_pct if pos_pct is not None else st.default_pos)))) / 100)
    x = OUT_W // 2

    events: List[str] = []

    def ev(layer: int, a: float, b: float, style: str, text: str) -> None:
        if b - a < 0.02:
            return
        events.append(f"Dialogue: {layer},{_ass_time(a)},{_ass_time(b)},{style},,0,0,0,,{text}")

    def plates(layer: int, a: float, b: float, rects: Sequence[Tuple[float, float, float, float]], fill: str,
               fill_alpha: int, radius: float, edge: Optional[str], edge_w: float) -> None:
        """Fill (+ optional edge) for a group of rectangles. Edges go one layer below all fills, so
        overlapping plates of one group merge into one shape with one outline."""
        if not rects:
            return
        shape = lambda r: rounded_rect(r[0], r[1], r[2], r[3], radius)  # noqa: E731
        if edge:
            for r in rects:
                ev(layer, a, b, "Shape", "{\\an7\\pos(0,0)\\p1\\1c" + _ass_color(edge) + f"\\1a&H00&\\bord{edge_w:g}\\3c"
                   + _ass_color(edge) + "}" + shape(r) + "{\\p0}")
        for r in rects:
            ev(layer + 1, a, b, "Shape", "{\\an7\\pos(0,0)\\p1\\1c" + _ass_color(fill) + f"\\1a&H{fill_alpha:02X}&\\bord0" + "}"
               + shape(r) + "{\\p0}")

    def disp(w: Dict[str, Any]) -> str:
        t = _ass_text(w["word"])
        return t.upper() if st.upper else t

    # A white edge on white letters would melt them into a blob, so light letters keep the black one.
    letter_col = stroke if stroke and not (stroke == "FFFFFF" and st.primary in LIGHT_TEXT) else "000000"
    plated_line = bool(st.back_box)
    styles = [_style_line("Txt", st, outline_col=letter_col, outline=0 if plated_line else None,
                          shadow=0 if plated_line else None),
              "Style: Shape,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1"]
    lh = float(st.size)  # libass line height = font size for these fonts (measured)
    pad = max(8.0, st.size * 0.13)
    radius_k = 0.32 if round_plates else 0.0
    edge_w = max(3.0, round(st.size * 0.06, 1))
    pos = f"\\an5\\pos({x},{y})"

    chunks = chunk_words(words, st.max_words, st.max_chars)
    for ci, ch in enumerate(chunks):
        c_start = ch[0]["start"]
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else duration
        c_end = min(nxt, ch[-1]["end"] + 0.35, duration)
        texts = [disp(w) for w in ch]
        widths = [text_width(st.font, st.bold, t, st.size) for t in texts]
        space = text_width(st.font, st.bold, " ", st.size)
        lines = wrap_lines(widths, space, MAX_LINE_W)
        line_of = {i: li for li, ln in enumerate(lines) for i in ln}
        top = y - len(lines) * lh / 2
        boxes: List[Tuple[float, float, float, float]] = []  # per word: x1, y1, x2, y2 (no padding)
        line_boxes: List[Tuple[float, float, float, float]] = []
        for li, ln in enumerate(lines):
            lw = sum(widths[i] for i in ln) + space * (len(ln) - 1)
            cx = x - lw / 2
            y1 = top + li * lh
            line_boxes.append((x - lw / 2, y1, x + lw / 2, y1 + lh))
            for i in ln:
                boxes.append((cx, y1, cx + widths[i], y1 + lh))
                cx += widths[i] + space

        def joined(parts: List[str]) -> str:
            out = ""
            for i, p_ in enumerate(parts):
                if i:
                    out += "\\N" if line_of[i] != line_of[i - 1] else " "
                out += p_
            return out

        if st.mode == "none":
            if plated_line:
                rgba = st.back_box
                bp = st.outline  # the block's own padding
                plates(0, c_start, c_end, [(b[0] - bp, b[1] - bp * 0.55, b[2] + bp, b[3] + bp * 0.55) for b in line_boxes],
                       rgba[:6], 0xFF - int(rgba[6:8], 16), (lh + bp * 1.1) * radius_k, stroke, edge_w)
            ev(2, c_start, c_end, "Txt", "{" + pos + "}" + joined(texts))
            continue
        for wi, w in enumerate(ch):
            a = w["start"] if wi else c_start
            b = ch[wi + 1]["start"] if wi + 1 < len(ch) else c_end
            if st.mode == "one":
                col = _ass_color(st.key) if w["key"] else _ass_color(st.primary)
                anim = "\\fscx70\\fscy70\\t(0,90,\\fscx112\\fscy112)\\t(90,170,\\fscx100\\fscy100)"
                ev(2, a, b, "Txt", "{" + pos + anim + f"\\c{col}" + "}" + disp(w))
                continue
            parts = []
            for wj, t in enumerate(texts):
                if st.mode == "box":
                    parts.append("{\\c&H000000&\\bord0\\shad0}" + t + "{\\r}" if wj == wi else t)
                elif st.mode == "color":
                    parts.append("{\\c" + _ass_color(st.active) + "}" + t + "{\\r}" if wj == wi else t)
                elif st.mode == "fill":
                    parts.append("{\\c" + _ass_color(st.active) + "}" + t + "{\\r}" if wj <= wi else t)
            ev(2, a, b, "Txt", "{" + pos + "}" + joined(parts))
            if st.mode == "box":
                bx = boxes[wi]
                plates(0, a, b, [(bx[0] - pad, bx[1] - pad, bx[2] + pad, bx[3] + pad)], st.active, 0,
                       (lh + 2 * pad) * radius_k, stroke, edge_w)

    if hook and hook.strip():
        until = duration if hook_sec is None else min(hook_sec, duration)
        fade = "" if hook_sec is None else "\\fad(0,250)"
        hs = max(0.6, min(1.8, float(hook_size or 1.0)))
        hook_bg = {"white": ("FFFFFF", "111111"), "black": ("000000", "FFFFFF"), "accent": (accent or "FFE14D", "111111")}
        htxt = _ass_text(hook.upper())
        hwords = htxt.split()
        if hook_style in hook_bg:
            bg, fg = hook_bg[hook_style]
            hst = CaptionStyle("hook", "", "Montserrat Black", int(60 * hs), True, 0, 0, "none", primary=fg, outline=0, shadow=0)
            styles.append(_style_line("Hook", hst, align=8))
            hpad = 18 * hs
        else:  # "outline": big letters with an edge, no plate
            hst = CaptionStyle("hook", "", "Montserrat Black", int(72 * hs), True, 0, 0, "none", primary=text_color or "FFFFFF",
                               outline=round(6 * hs, 1), shadow=round(3 * hs, 1))
            edge = hook_stroke if hook_stroke and not (hook_stroke == "FFFFFF" and hst.primary in LIGHT_TEXT) else "000000"
            styles.append(_style_line("Hook", hst, outline_col=edge, align=8))
            hpad = 0.0
        hw = [text_width(hst.font, False, t, hst.size) for t in hwords]
        hsp = text_width(hst.font, False, " ", hst.size)
        hlines = wrap_lines(hw, hsp, MAX_LINE_W - 2 * hpad)
        hlh = float(hst.size)
        htop = 190.0 if hook_pos is None else OUT_H * max(3.0, min(80.0, float(hook_pos))) / 100
        if hook_style in hook_bg:
            rects = []
            for li, ln in enumerate(hlines):
                lw = sum(hw[i] for i in ln) + hsp * (len(ln) - 1)
                y1 = htop + li * hlh
                rects.append((x - lw / 2 - hpad, y1 - hpad, x + lw / 2 + hpad, y1 + hlh + hpad))
            # the fade must also apply to the plate, so it goes into the drawing too
            before = len(events)
            plates(3, 0.0, until, rects, bg, 0, (hlh + 2 * hpad) * (0.32 if hook_round else 0.0), hook_stroke, max(3.0, 5 * hs))
            if fade:
                events[before:] = [e.replace("{\\an7\\pos(0,0)", "{\\an7\\pos(0,0)" + fade, 1) for e in events[before:]]
        text = "\\N".join(" ".join(hwords[i] for i in ln) for ln in hlines)
        ev(5, 0.0, until, "Hook", "{\\an8\\pos(540," + str(int(htop)) + ")" + fade + "}" + text)

    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\nYCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )
    return head + "\n".join(styles) + "\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n" + "\n".join(events) + "\n"


# ─── 2. Reframing ─────────────────────────────────────────────────────────────

@dataclass
class Layout:
    """How to turn the 16:9 frame into 9:16. Coordinates are fractions of the source frame."""
    kind: str                                   # track | split | blur | center
    keyframes: List[Tuple[float, float]] = field(default_factory=list)  # (t, crop centre x) for track/center
    split: List[Tuple[float, float, float]] = field(default_factory=list)  # [(cx, cy, h)] top, bottom for split
    faces_found: float = 0.0                    # share of samples with a face
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "keyframes": [[round(t, 2), round(x, 4)] for t, x in self.keyframes],
                "split": [[round(a, 4), round(b, 4), round(c, 4)] for a, b, c in self.split],
                "faces_found": round(self.faces_found, 2), "note": self.note}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Layout":
        return cls(kind=d.get("kind", "center"), keyframes=[tuple(k) for k in d.get("keyframes", [])],
                   split=[tuple(s) for s in d.get("split", [])], faces_found=float(d.get("faces_found", 0.0)),
                   note=d.get("note", ""))


def probe_size(path: str) -> Tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:stream_side_data=rotation",
         "-of", "json", path], capture_output=True, text=True, timeout=30,
    )
    data = json.loads(out.stdout or "{}")
    s = (data.get("streams") or [{}])[0]
    w, h = int(s.get("width") or 0), int(s.get("height") or 0)
    rot = 0
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    if not w or not h:
        raise RuntimeError("Не вдалося прочитати розмір відео.")
    return w, h


_detector_lock = threading.Lock()


def _make_detector(w: int, h: int):
    import cv2  # local import: the rest of the app works without OpenCV
    if YUNET_MODEL.exists() and hasattr(cv2, "FaceDetectorYN"):
        det = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (w, h), 0.6, 0.3, 5000)

        def detect(img):
            _, faces = det.detect(img)
            if faces is None:
                return []
            return [(float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])) for f in faces]
        return detect
    cascade = cv2.CascadeClassifier(os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml"))
    log.warning("YuNet model missing, falling back to Haar cascade face detection")

    def detect_haar(img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        found = cascade.detectMultiScale(gray, 1.1, 5, minSize=(24, 24))
        return [(float(x), float(y), float(fw), float(fh), 0.8) for (x, y, fw, fh) in found]
    return detect_haar


def detect_faces(source: str, start: float, end: float, sample_fps: float = 4.0, width: int = 640) -> Dict[str, Any]:
    """
    Samples frames from [start, end] and finds faces.
    Returns {"w", "h", "samples": [[t, [[cx, cy, w, h, score], ...]], ...]} with fractions of the frame.
    """
    import numpy as np
    src_w, src_h = probe_size(source)
    sw = width
    sh = int(round(src_h * sw / src_w / 2)) * 2
    dur = max(0.5, end - start)
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start:.3f}", "-i", source, "-t", f"{dur:.3f}",
           "-vf", f"fps={sample_fps},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg не зміг прочитати кадри: " + proc.stderr.decode("utf-8", "replace")[-300:])
    frame_bytes = sw * sh * 3
    n = len(proc.stdout) // frame_bytes
    samples: List[List[Any]] = []
    with _detector_lock:
        detect = _make_detector(sw, sh)
        for i in range(n):
            img = np.frombuffer(proc.stdout, dtype=np.uint8, count=frame_bytes, offset=i * frame_bytes).reshape(sh, sw, 3)
            faces = []
            for (x, y, fw, fh, score) in detect(img):
                faces.append([round((x + fw / 2) / sw, 4), round((y + fh / 2) / sh, 4), round(fw / sw, 4), round(fh / sh, 4), round(score, 3)])
            samples.append([round(i / sample_fps, 3), faces])
    try:
        cuts = detect_cuts(source, start, end)
    except Exception as e:  # cuts only make the camera smarter; never fail the plan because of them
        log.warning(f"Scene cut detection failed: {e}")
        cuts = []
    return {"w": src_w, "h": src_h, "samples": samples, "cuts": cuts}


_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def detect_cuts(source: str, start: float, end: float, threshold: float = 0.3) -> List[float]:
    """Exact times (relative to start) where the source switches camera, via ffmpeg's scene score."""
    dur = max(0.5, end - start)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-i", source, "-t", f"{dur:.3f}", "-an",
           "-vf", f"scale=192:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    out = []
    for line in (proc.stderr or "").splitlines():
        if "Parsed_showinfo" in line:
            m = _PTS_RE.search(line)
            if m:
                t = float(m.group(1))
                if 0.1 < t < dur - 0.1:
                    out.append(round(t, 3))
    return sorted(set(out))


def _crop_w_frac(src_w: int, src_h: int) -> float:
    return min(1.0, (src_h * 9 / 16) / src_w)


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def plan_layout(faces: Dict[str, Any], mode: str = "auto", duration: Optional[float] = None) -> Layout:
    """Turns face samples into a crop plan. Pure function (easy to test)."""
    src_w, src_h = int(faces["w"]), int(faces["h"])
    samples = faces.get("samples") or []
    duration = duration if duration is not None else (samples[-1][0] if samples else 0.0)
    cw = _crop_w_frac(src_w, src_h)
    if mode not in FRAME_MODES:
        mode = "auto"
    if cw >= 0.999:
        # Already vertical (or narrower): nothing to crop.
        return Layout("center", [(0.0, 0.5)], note="Відео вже вертикальне.")
    if mode == "blur":
        return Layout("blur")
    if mode == "center" or not samples:
        return Layout("center", [(0.0, 0.5)], note="" if mode == "center" else "Кадри не прочитано, показую центр.")

    # Keep only real people: drop faces much smaller than the biggest one in the same sample.
    clean: List[Tuple[float, List[List[float]]]] = []
    for t, fs in samples:
        fs = [f for f in fs if f[4] >= 0.6]
        if fs:
            big = max(f[2] * f[3] for f in fs)
            fs = [f for f in fs if f[2] * f[3] >= 0.3 * big]
        clean.append((float(t), fs))
    with_face = sum(1 for _, fs in clean if fs)
    found = with_face / len(clean)

    # Two people talking (podcast, one camera): two close-ish faces of similar size, far apart, in most samples.
    if mode in ("auto", "split"):
        pairs = []
        for _, fs in clean:
            big = [f for f in fs if f[3] >= 0.10]  # background people are small; skip them
            if len(big) >= 2:
                a, b = sorted(sorted(big, key=lambda f: f[2] * f[3], reverse=True)[:2], key=lambda f: f[0])
                if b[0] - a[0] > cw * 0.9 and min(a[2] * a[3], b[2] * b[3]) >= 0.5 * max(a[2] * a[3], b[2] * b[3]):
                    pairs.append((a, b))
        if len(pairs) >= 0.7 * len(clean):
            out = []
            for k in (0, 1):
                fx = _median([p[k][0] for p in pairs])
                fy = _median([p[k][1] for p in pairs])
                fh = _median([p[k][3] for p in pairs])
                out.append((fx, fy, max(0.62, min(1.0, fh * 3.2))))
            return Layout("split", split=out, faces_found=found)
        if mode == "split":
            mode = "track"
            note = "У цьому кліпі двоє людей не сидять в одному кадрі, тому кадр слідкує за тим, хто в кадрі."
        else:
            note = ""
    else:
        note = ""

    if found < 0.25:
        if mode == "auto":
            return Layout("blur", faces_found=found, note="Обличчя майже не видно, тому показую весь кадр.")
        return Layout("center", [(0.0, 0.5)], faces_found=found, note="Обличчя не знайдено, тому кадр по центру.")

    keys = _track_keyframes(clean, [float(c) for c in faces.get("cuts") or []], cw, duration)
    return Layout("track", keyframes=keys, faces_found=found, note=note)


def _track_keyframes(clean: List[Tuple[float, List[List[float]]]], cuts: List[float], cw: float,
                     duration: float) -> List[Tuple[float, float]]:
    """
    Camera path for one followed person, like a calm camera operator:
      - at a camera cut in the source, the crop switches exactly on that frame (the jump hides in the cut);
      - inside one shot it holds still while the face stays near the centre, and glides (~1 s, ease in/out) when it doesn't.
    """
    lo, hi = cw / 2, 1 - cw / 2
    clamp = lambda v: max(lo, min(hi, v))
    dead = cw * 0.16   # how far the face may drift before the camera moves
    jump = cw * 0.65   # without detected cuts: a move this big is treated as a cut

    # Split samples into shots.
    bounds = [0.0] + [c for c in cuts if 0 < c < duration] + [max(duration, clean[-1][0] + 1)]
    shots = []
    for s0, s1 in zip(bounds, bounds[1:]):
        idx = [i for i, (t, _) in enumerate(clean) if s0 <= t < s1]
        if idx:
            shots.append((s0, s1, idx))

    keys: List[Tuple[float, float]] = []
    cam: Optional[float] = None
    for si, (s0, s1, idx) in enumerate(shots):
        # Pick the person per sample: sticky to the current one unless another is clearly bigger.
        targets: List[Optional[float]] = []
        prev: Optional[float] = None
        for i in idx:
            fs = clean[i][1]
            if not fs:
                targets.append(None)
                continue
            biggest = max(fs, key=lambda f: f[2] * f[3])
            choice = biggest
            if prev is not None:
                near = min(fs, key=lambda f: abs(f[0] - prev))
                if near[2] * near[3] >= 0.7 * biggest[2] * biggest[3]:
                    choice = near
            prev = choice[0]
            targets.append(choice[0])
        known = [x for x in targets if x is not None]
        if not known:
            if cam is None:
                cam = 0.5
            if not keys:
                keys.append((0.0, cam))
            continue
        filled, last = [], known[0]
        for x in targets:
            last = x if x is not None else last
            filled.append(last)
        k = 2
        smooth = [clamp(_median(filled[max(0, j - k): j + k + 1])) for j in range(len(filled))]
        times = [clean[i][0] for i in idx]

        start_x = smooth[0]
        if cam is None:
            keys.append((0.0, start_x))
        elif abs(start_x - cam) > dead * 0.5:
            # Hard switch exactly on the cut frame.
            # (the move is complete a few ms before the cut frame's timestamp, so that frame already shows the new crop)
            keys.append((max(keys[-1][0], s0 - 0.025), cam))
            keys.append((max(keys[-1][0] + 0.001, s0 - 0.005), start_x))
        else:
            start_x = cam
        cam = start_x

        j = 0
        while j < len(smooth):
            tgt = smooth[j]
            if abs(tgt - cam) > dead:
                hold = smooth[j: j + 3]
                if len(hold) >= 2 and all(abs(h - tgt) < dead for h in hold):
                    t = max(times[j], keys[-1][0])
                    if not cuts and abs(tgt - cam) > jump:
                        keys.append((t, cam))
                        keys.append((t + 0.04, tgt))
                    else:
                        dur = min(1.4, max(0.8, abs(tgt - cam) / cw * 1.2))
                        dur = min(dur, max(0.2, s1 - t - 0.05))
                        if t > keys[-1][0]:
                            keys.append((t, cam))
                        keys.append((t + dur, tgt))
                    cam = tgt
                    nxt = keys[-1][0]
                    while j < len(smooth) and times[j] < nxt:
                        j += 1
                    continue
            j += 1
    return keys or [(0.0, 0.5)]


def _ease(u: str) -> str:
    """smoothstep(u) = u²(3−2u): starts and stops gently (ffmpeg expression)."""
    return f"({u})*({u})*(3-2*({u}))"


def _x_expr(keys: Sequence[Tuple[float, float]], src_w: int, crop_px: int) -> str:
    """ffmpeg expression for the crop's left edge: eased moves between keyframes, as a flat sum (no nesting)."""
    def px(cx: float) -> float:
        return max(0.0, min(src_w - crop_px, cx * src_w - crop_px / 2))
    base = px(keys[0][1])
    terms = [f"{base:.1f}"]
    for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
        d = px(x1) - px(x0)
        if abs(d) < 0.5:
            continue
        span = max(0.02, t1 - t0)
        terms.append(f"{d:+.1f}*{_ease(f'clip((t-{t0:.3f})/{span:.3f},0,1)')}")
    return "".join(terms)


def video_filter(layout: Layout, src_w: int, src_h: int, ass_name: Optional[str], fonts_rel: str) -> str:
    """filter_complex string producing [v] (1080×1920, 30 fps, optional burned subtitles)."""
    even = lambda v: max(2, int(v) // 2 * 2)
    if layout.kind == "blur":
        chain = (
            f"[0:v]split=2[bga][fga];"
            f"[bga]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},boxblur=25:2,eq=brightness=-0.08[bg];"
            f"[fga]scale={OUT_W}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,fps={OUT_FPS}[base]"
        )
    elif layout.kind == "split":
        parts = []
        for name, (cx, cy, hf) in zip(("top", "bot"), layout.split):
            ch = even(src_h * hf)
            cwp = even(min(src_w, ch * OUT_W / (OUT_H / 2)))
            ch = even(min(ch, cwp * (OUT_H / 2) / OUT_W))
            x = max(0, min(src_w - cwp, int(cx * src_w - cwp / 2)))
            y = max(0, min(src_h - ch, int(cy * src_h - ch * 0.42)))
            parts.append(f"[{name}in]crop={cwp}:{ch}:{x}:{y},scale={OUT_W}:{OUT_H // 2}:flags=lanczos,setsar=1[{name}]")
        chain = "[0:v]split=2[topin][botin];" + ";".join(parts) + f";[top][bot]vstack=2,fps={OUT_FPS}[base]"
    else:
        crop_px = even(min(src_w, src_h * 9 / 16))
        keys = layout.keyframes or [(0.0, 0.5)]
        chain = (
            f"[0:v]crop={crop_px}:{even(src_h)}:x='{_x_expr(keys, src_w, crop_px)}':y=0,"
            f"scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1,fps={OUT_FPS}[base]"
        )
    if ass_name:
        chain += f";[base]ass={ass_name}:fontsdir={fonts_rel}[v]"
    else:
        chain += ";[base]null[v]"
    return chain


# ─── 3. Rendering ─────────────────────────────────────────────────────────────

def _encoders(has_nvenc: bool) -> List[List[str]]:
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    if has_nvenc:
        nv = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "10M", "-maxrate", "16M",
              "-bufsize", "20M", "-pix_fmt", "yuv420p", "-profile:v", "high"]
        return [nv, x264]
    return [x264]


def render_vertical(
    source: str,
    start: float,
    end: float,
    output_path: str,
    layout: Layout,
    ass_text: Optional[str],
    has_nvenc: bool = False,
    timeout: int = 900,
) -> str:
    """Renders one vertical clip. Writes to a temp file and renames, so a half-written file is never served."""
    src_w, src_h = probe_size(source)
    work = RENDER_TMP / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(f"tmp_{uuid.uuid4().hex[:6]}_{out.name}")
    try:
        ass_name = None
        if ass_text:
            (work / "subs.ass").write_text(ass_text, encoding="utf-8")
            ass_name = "subs.ass"
        fonts_rel = os.path.relpath(FONTS_DIR, work).replace("\\", "/")
        fc = video_filter(layout, src_w, src_h, ass_name, fonts_rel)
        dur = max(0.5, end - start)
        last_err = ""
        for enc in _encoders(has_nvenc):
            cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", os.path.abspath(source), "-t", f"{dur:.3f}",
                   "-filter_complex", fc, "-map", "[v]", "-map", "0:a?", *enc,
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(tmp_out.resolve())]
            proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
            if proc.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 1000:
                os.replace(tmp_out, out)
                return str(out)
            last_err = (proc.stderr or "")[-600:]
            log.warning(f"Vertical render with {enc[1]} failed: {last_err}")
        raise RuntimeError("Не вдалося зібрати вертикальне відео: " + last_err.strip()[-300:])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if tmp_out.exists():
            try:
                tmp_out.unlink()
            except OSError:
                pass


# ─── Options / cache keys ─────────────────────────────────────────────────────

DEFAULT_OPTS: Dict[str, Any] = {"style": DEFAULT_STYLE, "frame": "auto", "pos": None, "hook": False, "edits": {},
                                "size": 1.0, "color": None, "text": None,
                                "hook_text": None, "hook_style": "white", "hook_size": 1.0, "hook_full": False,
                                "round": False, "stroke": None, "hook_round": False, "hook_stroke": None, "hook_pos": None}
# Colour presets offered in the editor (RGB hex). None = the style's own colour.
ACCENT_COLORS = ("FFE14D", "7CFF4F", "FF4D4D", "4DC3FF", "FF5EC4", "E6C068", "FF9F1C")
TEXT_COLORS = ("FFFFFF", "FFE14D", "FFF8EC")
HOOK_STYLES = ("white", "black", "accent", "outline")
STROKE_COLORS = ("000000", "FFFFFF")


def normalize_opts(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Cleans user-provided vertical options (unknown values fall back to defaults)."""
    o = dict(DEFAULT_OPTS)
    raw = raw or {}
    style = raw.get("style", o["style"])
    o["style"] = style if style in CAPTION_STYLES or style == "none" else DEFAULT_STYLE
    frame = raw.get("frame", o["frame"])
    o["frame"] = frame if frame in FRAME_MODES else "auto"
    pos = raw.get("pos")
    try:
        o["pos"] = None if pos in (None, "") else max(20.0, min(85.0, float(pos)))
    except (TypeError, ValueError):
        o["pos"] = None
    o["hook"] = bool(raw.get("hook", False))

    def num(key: str, lo: float, hi: float) -> float:
        try:
            return round(max(lo, min(hi, float(raw.get(key, 1.0)))), 2)
        except (TypeError, ValueError):
            return 1.0
    o["size"] = num("size", 0.6, 1.6)
    o["hook_size"] = num("hook_size", 0.6, 1.8)
    c = str(raw.get("color") or "").upper().lstrip("#")
    o["color"] = c if c in ACCENT_COLORS else None
    tc = str(raw.get("text") or "").upper().lstrip("#")
    o["text"] = tc if tc in TEXT_COLORS else None
    ht = raw.get("hook_text")
    o["hook_text"] = str(ht).strip()[:120] if isinstance(ht, str) and ht.strip() else None
    o["hook_style"] = raw.get("hook_style") if raw.get("hook_style") in HOOK_STYLES else "white"
    o["hook_full"] = bool(raw.get("hook_full", False))
    o["round"] = bool(raw.get("round", False))
    hp = raw.get("hook_pos")
    try:
        o["hook_pos"] = None if hp in (None, "") else round(max(3.0, min(80.0, float(hp))), 1)
    except (TypeError, ValueError):
        o["hook_pos"] = None
    o["hook_round"] = bool(raw.get("hook_round", False))
    for k in ("stroke", "hook_stroke"):
        v = str(raw.get(k) or "").upper().lstrip("#")
        o[k] = v if v in STROKE_COLORS else None
    edits = raw.get("edits") or {}
    clean: Dict[str, Dict[str, Any]] = {}
    if isinstance(edits, dict):
        for k, v in list(edits.items())[:2000]:
            if not re.fullmatch(r"\d+\.\d{2}", str(k)) or not isinstance(v, dict):
                continue
            e: Dict[str, Any] = {}
            if "w" in v:
                e["w"] = str(v["w"])[:60]
            if "k" in v:
                e["k"] = bool(v["k"])
            if "orig" in v and e.get("w") == "":
                e["orig"] = str(v["orig"])[:60]  # shown struck-through in the editor so it can be restored
            if e:
                clean[str(k)] = e
    o["edits"] = clean
    return o


def cache_key(start: float, end: float, opts: Dict[str, Any], subs: bool, title: str = "") -> str:
    payload = json.dumps({"v": RENDER_VERSION, "s": round(start, 2), "e": round(end, 2), "o": opts, "subs": subs,
                          "t": (opts.get("hook_text") or title) if opts.get("hook") else ""}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]

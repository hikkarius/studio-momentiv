"""
subtitles_generator.py — High-precision .SRT subtitle generation for AI Video Moments Studio.
Tailored for viral short-form content (TikTok, Shorts, Reels) and video editors (CapCut, Premiere, DaVinci).
Generates relative zero-based timecodes starting at 00:00:00,000 for each moment clip.
"""
from __future__ import annotations

import io
import logging
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from transcription import load_transcript_cache

log = logging.getLogger("shorts_core")


def format_srt_timestamp(seconds: float) -> str:
    """Formats seconds into canonical SubRip format: HH:MM:SS,mmm."""
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    if secs >= 60:
        minutes += 1
        secs = 0
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _chunk_words_for_shorts(
    words: List[Dict[str, Any]],
    target_words_per_cue: int = 3,
    max_words_per_cue: int = 4,
) -> List[List[Dict[str, Any]]]:
    """
    Groups words into short, rhythmic chunks (2-4 words per cue) optimal for
    modern dynamic captions in vertical video (CapCut/TikTok style).
    Breaks early on sentence-ending punctuation (! ? .).
    """
    chunks: List[List[Dict[str, Any]]] = []
    current_chunk: List[Dict[str, Any]] = []

    for w in words:
        current_chunk.append(w)
        word_text = str(w.get("word", "")).strip()

        # Check sentence boundary
        ends_sentence = any(word_text.endswith(p) for p in (".", "!", "?", "…"))

        if len(current_chunk) >= max_words_per_cue or (
            len(current_chunk) >= target_words_per_cue and (ends_sentence or word_text.endswith(","))
        ) or (ends_sentence and len(current_chunk) >= 2):
            chunks.append(current_chunk)
            current_chunk = []

    if current_chunk:
        if chunks and len(current_chunk) == 1 and len(chunks[-1]) < max_words_per_cue:
            chunks[-1].extend(current_chunk)
        else:
            chunks.append(current_chunk)

    return chunks


def generate_srt_for_clip(
    video_path: str,
    start_sec: float,
    end_sec: float,
    target_words_per_cue: int = 3,
    cuts: Optional[Sequence[Sequence[float]]] = None,
) -> str:
    """
    Generates a full .SRT string for a clip slice [start_sec, end_sec].
    cuts: absolute pauses taken out of the clip («Прибрати паузи»); the cues follow the cut timeline.
    Uses cached word timestamps if available, shifting timecodes relative to start_sec (00:00:00,000).
    Falls back gracefully to sentence-level segments if word timestamps are missing.
    """
    start_sec = max(0.0, float(start_sec))
    end_sec = max(start_sec + 0.1, float(end_sec))
    clip_duration = end_sec - start_sec

    cached = load_transcript_cache(video_path, any_model=True) if video_path else None
    segments: List[Dict[str, Any]] = []
    words: List[Dict[str, Any]] = []

    if cached:
        segments, words, _ = cached

    if cuts:
        import pauses
        cl = pauses.normalize_cuts(cuts, start_sec, end_sec)
        if cl:
            rel = [{**w, "start": float(w["start"]) - start_sec, "end": float(w["end"]) - start_sec}
                   for w in (words or []) if float(w.get("end", 0.0)) > start_sec and float(w.get("start", 0.0)) < end_sec]
            words = [{**w, "start": start_sec + w["start"], "end": start_sec + w["end"]}
                     for w in pauses.remap_words(rel, pauses.relative(cl, start_sec))]
            end_sec = end_sec - pauses.removed(cl)
            clip_duration = end_sec - start_sec
            segments = []  # sentence fallback would ignore the cuts

    srt_entries: List[str] = []
    entry_index = 1

    # 1. Primary path: High-precision word timestamps
    clip_words = [
        w for w in (words or [])
        if float(w.get("end", 0.0)) > start_sec and float(w.get("start", 0.0)) < end_sec
    ]

    if clip_words:
        chunks = _chunk_words_for_shorts(clip_words, target_words_per_cue=target_words_per_cue)
        for chunk in chunks:
            w_first = chunk[0]
            w_last = chunk[-1]

            cue_start = max(0.0, float(w_first.get("start", start_sec)) - start_sec)
            cue_end = min(clip_duration, float(w_last.get("end", end_sec)) - start_sec)
            cue_start = min(cue_start, clip_duration)
            cue_end = max(cue_start + 0.2, cue_end)

            text_parts = [str(w.get("word", "")).strip() for w in chunk]
            cue_text = " ".join(p for p in text_parts if p)

            if cue_text:
                srt_entries.append(
                    f"{entry_index}\n"
                    f"{format_srt_timestamp(cue_start)} --> {format_srt_timestamp(cue_end)}\n"
                    f"{cue_text}\n"
                )
                entry_index += 1

        if srt_entries:
            return "\n".join(srt_entries).strip() + "\n"

    # 2. Fallback path: Segments interpolation if words were unavailable
    clip_segments = [
        s for s in (segments or [])
        if float(s.get("end", 0.0)) > start_sec and float(s.get("start", 0.0)) < end_sec
    ]

    for seg in clip_segments:
        s_start = max(0.0, float(seg.get("start", start_sec)) - start_sec)
        s_end = min(clip_duration, float(seg.get("end", end_sec)) - start_sec)
        s_dur = max(0.2, s_end - s_start)
        seg_text = str(seg.get("text", "")).strip()
        words_in_seg = seg_text.split()
        if not words_in_seg:
            continue

        # Split segment words into chunks
        step = max(2, min(4, target_words_per_cue))
        for i in range(0, len(words_in_seg), step):
            sub_words = words_in_seg[i : i + step]
            sub_text = " ".join(sub_words)
            rel_p1 = i / len(words_in_seg)
            rel_p2 = min(1.0, (i + len(sub_words)) / len(words_in_seg))

            c_start = s_start + (s_dur * rel_p1)
            c_end = s_start + (s_dur * rel_p2)
            c_end = max(c_start + 0.2, c_end)

            srt_entries.append(
                f"{entry_index}\n"
                f"{format_srt_timestamp(c_start)} --> {format_srt_timestamp(c_end)}\n"
                f"{sub_text}\n"
            )
            entry_index += 1

    if not srt_entries:
        # Minimal fallback cue so editor does not crash on empty file
        srt_entries.append(
            f"1\n"
            f"00:00:00,000 --> {format_srt_timestamp(min(5.0, clip_duration))}\n"
            f"[Фрагмент відео]\n"
        )

    return "\n".join(srt_entries).strip() + "\n"


def write_clip_srt_file(
    video_path: str,
    start_sec: float,
    end_sec: float,
    output_path: str,
    target_words_per_cue: int = 3,
    cuts: Optional[Sequence[Sequence[float]]] = None,
) -> str:
    """Generates SRT content and writes it directly to disk (UTF-8)."""
    srt_content = generate_srt_for_clip(
        video_path=video_path,
        start_sec=start_sec,
        end_sec=end_sec,
        target_words_per_cue=target_words_per_cue,
        cuts=cuts,
    )
    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(srt_content, encoding="utf-8")
    return str(p)


def create_all_subtitles_zip(
    project_id: str,
    project_title: str,
    source_path: str,
    clips: List[Dict[str, Any]],
    output_zip_path: str,
) -> str:
    """Packages .SRT files for all candidate clips in a project into a single ZIP archive."""
    p_zip = Path(output_zip_path)
    p_zip.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(str(p_zip), "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for c in clips:
            idx = c.get("clip_index", 1)
            start_s = float(c.get("start_sec", c.get("start", 0.0)))
            end_s = float(c.get("end_sec", c.get("end", 0.0)))
            raw_title = str(c.get("title", f"clip_{idx}"))
            safe_title = "".join(ch for ch in raw_title if ch.isalnum() or ch in (" ", "_", "-")).strip()
            safe_title = safe_title[:30].replace(" ", "_") or f"clip_{idx}"

            srt_text = generate_srt_for_clip(
                video_path=source_path,
                start_sec=start_s,
                end_sec=end_s,
                target_words_per_cue=3,
                cuts=c.get("cuts"),
            )
            arcname = f"clip_{idx:02d}_{int(start_s)}s_{safe_title}.srt"
            zf.writestr(arcname, srt_text.encode("utf-8"))

    return str(p_zip)

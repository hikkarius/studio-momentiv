"""
transcription.py — Whisper model singleton, audio extraction, caching, and transcription pipeline.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Callable, Optional

from config_loader import CONFIG
from languages import AUTO, INITIAL_PROMPTS, language_label
from utils import extract_audio

log = logging.getLogger("shorts_core")

from state import app_state


def get_whisper():
    """Delegates to thread-safe AppState singleton."""
    return app_state.get_whisper()


# ─── Transcript caching ───────────────────────────────────────────────────────

def transcript_cache_dir() -> Path:
    """Folder of the Whisper transcript cache. The disk cleanup reads it from here too; tests replace it."""
    return Path(CONFIG["cache"]["dir"])


def _cache_path(video_path: str) -> Path:
    """
    Computes a fast, collision-resistant fingerprint cache path based on
    file size and SHA-256 of boundary samples (1 MB head + 1 MB middle + 1 MB tail).
    Avoids reading entire multi-gigabyte video files while guaranteeing cache validity
    across uploads into different temporary directories.
    """
    h = hashlib.sha256()
    stat = os.stat(video_path)
    file_size = stat.st_size
    h.update(f"size_{file_size}".encode("utf-8"))

    sample_size = 1024 * 1024  # 1 MB boundary sample
    with open(video_path, "rb") as f:
        # Head sample
        h.update(f.read(sample_size))
        # Middle sample
        if file_size > sample_size * 3:
            f.seek(file_size // 2 - sample_size // 2, os.SEEK_SET)
            h.update(f.read(sample_size))
        # Tail sample
        if file_size > sample_size * 2:
            f.seek(-sample_size, os.SEEK_END)
            h.update(f.read(sample_size))

    cache_dir = transcript_cache_dir()
    cache_dir.mkdir(exist_ok=True)
    # The Whisper model is part of the key: switching models re-transcribes instead of reusing
    # a transcript made by a weaker model. "small" keeps the old file names (no cache loss).
    model = str(CONFIG.get("whisper", {}).get("model", "small"))
    suffix = "" if model == "small" else "_" + re.sub(r"[^A-Za-z0-9.-]+", "-", model)
    return cache_dir / f"{h.hexdigest()}{suffix}.json"


def load_transcript_cache(video_path: str, any_model: bool = False) -> tuple[list, list, str] | None:
    """
    Returns (segments, words, lang) from cache, or None on miss / disabled.
    any_model=True also accepts a transcript made by another Whisper model (newest first);
    used for reading (subtitles, clip text), never to skip a re-transcription.
    """
    if not CONFIG["cache"]["enabled"]:
        return None
    try:
        p = _cache_path(video_path)
        if not p.exists() and any_model:
            digest = p.stem.split("_")[0]
            others = sorted(p.parent.glob(f"{digest}*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
            if others:
                p = others[0]
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            log.info(f"Cache HIT → {p.name}")
            return data["segments"], data["words"], data["lang"]
    except Exception as e:
        log.warning(f"Cache read error: {e}")
    return None


def load_transcript_cache_for(video_path: str, language: str) -> tuple[list, list, str] | None:
    """
    The cached transcript, but only if it was made for this language choice: a video first
    transcribed as Ukrainian must be redone when the user says it is English (and the reverse).
    Old cache files have no "requested" field; the language Whisper used stands in for it.
    """
    if not CONFIG["cache"]["enabled"]:
        return None
    try:
        p = _cache_path(video_path)
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        requested = data.get("requested") or data["lang"]
        if requested != language and not (language != AUTO and data["lang"] == language):
            log.info(f"Cache {p.name} is for language {requested!r}, need {language!r}: transcribing again")
            return None
        log.info(f"Cache HIT → {p.name}")
        return data["segments"], data["words"], data["lang"]
    except Exception as e:
        log.warning(f"Cache read error: {e}")
    return None


def save_transcript_cache(
    video_path: str, segments: list, words: list, lang: str, requested: Optional[str] = None
) -> None:
    """Persists transcription result to disk cache (requested = the language choice it was made for)."""
    if not CONFIG["cache"]["enabled"]:
        return
    try:
        p = _cache_path(video_path)
        p.write_text(
            json.dumps(
                {"segments": segments, "words": words, "lang": lang, "requested": requested or lang},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        log.info(f"Transcript cached → {p.name}")
    except Exception as e:
        log.warning(f"Cache write error: {e}")


def transcribe_video(
    video_path: str,
    status_cb: Optional[Callable[[str], None]] = None,
    language: Optional[str] = None,
) -> tuple[list[dict], list[dict], str, bool]:
    """
    High-level transcription entry point with caching.

    Returns:
        (segments, words, detected_lang, from_cache)
        from_cache=True means Whisper was skipped.

    status_cb(msg) is called with human-readable progress strings.
    language: Whisper code ("uk", "en", ...) or "auto"; None = the language from the settings.
    """
    cfg_lang = (language or CONFIG.get("whisper", {}).get("language") or "uk").strip().lower()
    cached = load_transcript_cache_for(video_path, cfg_lang)
    if cached:
        if status_cb:
            status_cb("✅ Транскрипцію знайдено у кеші — Whisper пропущено!")
        return (*cached, True)

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name

    try:
        if status_cb:
            status_cb("Витяг аудіодоріжки...")
        log.info(f"Extracting audio from {video_path!r}")
        extract_audio(video_path, audio_path)

        if status_cb:
            status_cb("Транскрибація через Whisper...")
        log.info("Starting Whisper transcription")

        model = get_whisper()
        engine = getattr(app_state, "_whisper_engine", "faster_whisper")

        segments: list[dict] = []
        words: list[dict] = []
        detected_lang = cfg_lang

        if engine == "faster_whisper":
            beam_size = int(CONFIG.get("whisper", {}).get("beam_size", 5) or 5)
            transcribe_kwargs: dict = {
                "word_timestamps": True,
                "vad_filter": True,
                "vad_parameters": dict(min_silence_duration_ms=400, speech_pad_ms=200),
                "condition_on_previous_text": False,
                "temperature": [0.0, 0.2, 0.4],
                "beam_size": beam_size,
            }
            if cfg_lang != AUTO:
                transcribe_kwargs["language"] = cfg_lang
                if cfg_lang in INITIAL_PROMPTS:
                    transcribe_kwargs["initial_prompt"] = INITIAL_PROMPTS[cfg_lang]

            def _consume_segments(raw_stream, tot_dur, label="Whisper"):
                segs = []
                wrds = []
                for seg in raw_stream:
                    if status_cb and tot_dur > 0:
                        pct = min(1.0, seg.end / tot_dur)
                        try:
                            status_cb(pct, f"Транскрипція аудіо ({label}): {int(pct * 100)}%")
                        except TypeError:
                            status_cb(f"Транскрипція аудіо ({label}): {int(pct * 100)}%")

                    segs.append({
                        "start": round(seg.start, 2),
                        "end": round(seg.end, 2),
                        "text": seg.text.strip(),
                    })
                    if seg.words:
                        for w in seg.words:
                            wrds.append({
                                "start": round(w.start, 2),
                                "end": round(w.end, 2),
                                "word": w.word.strip(),
                            })
                return segs, wrds

            try:
                segments_raw, info = model.transcribe(audio_path, **transcribe_kwargs)
                detected_lang = info.language
                log.info(
                    f"Detected language (faster-whisper): {detected_lang} "
                    f"(p={getattr(info, 'language_probability', '?')})"
                )
                if cfg_lang == AUTO and detected_lang in INITIAL_PROMPTS:
                    # The stream is lazy, so nothing is transcribed yet: start again with the
                    # detected language pinned and its spelling hint (better punctuation, no drift)
                    transcribe_kwargs["language"] = detected_lang
                    transcribe_kwargs["initial_prompt"] = INITIAL_PROMPTS[detected_lang]
                    segments_raw, info = model.transcribe(audio_path, **transcribe_kwargs)
                if status_cb:
                    status_cb(f"Мова відео: {language_label(detected_lang)}")

                total_duration = getattr(info, "duration", 0.0)
                try:
                    total_duration = float(total_duration)
                except (TypeError, ValueError):
                    total_duration = 0.0

                label = "CUDA" if getattr(model, "device", "") == "cuda" else "Whisper"
                segments, words = _consume_segments(segments_raw, total_duration, label)

            except Exception as ex:
                err_text = str(ex).lower()
                if any(k in err_text for k in ("cublas", "cuda", "out of memory", "dll", "not found")):
                    log.warning(
                        f"Faster-Whisper GPU execution failed ({ex}). "
                        "Transparently falling back to CPU multi-threaded int8..."
                    )
                    if status_cb:
                        status_cb("Перемикання на резервний CPU режим транскрипції...")
                    from faster_whisper import WhisperModel
                    cfg = CONFIG.get("whisper", {})
                    cpu_model = WhisperModel(
                        cfg.get("model", "small"),
                        device="cpu",
                        compute_type="int8",
                        cpu_threads=cfg.get("cpu_threads", 8),
                    )
                    app_state.set_whisper_fallback(cpu_model, engine="faster_whisper")
                    segments_raw, info = cpu_model.transcribe(audio_path, **transcribe_kwargs)
                    detected_lang = info.language
                    total_duration = float(getattr(info, "duration", 0.0) or 0.0)
                    segments, words = _consume_segments(segments_raw, total_duration, "CPU")
                else:
                    raise
        else:
            # openai-whisper format
            oai_kwargs: dict = {
                "word_timestamps": True,
                "language": None if cfg_lang == AUTO else cfg_lang,
            }
            if cfg_lang in INITIAL_PROMPTS:
                oai_kwargs["initial_prompt"] = INITIAL_PROMPTS[cfg_lang]
            res = model.transcribe(audio_path, **oai_kwargs)
            detected_lang = res.get("language") or (cfg_lang if cfg_lang != AUTO else "uk")
            log.info(f"Detected language (openai-whisper): {detected_lang}")
            for seg in res.get("segments", []):
                segments.append({
                    "start": round(seg["start"], 2),
                    "end": round(seg["end"], 2),
                    "text": seg["text"].strip(),
                })
                if "words" in seg:
                    for w in seg["words"]:
                        words.append({
                            "start": round(w["start"], 2),
                            "end": round(w["end"], 2),
                            "word": w["word"].strip(),
                        })

        save_transcript_cache(video_path, segments, words, detected_lang, requested=cfg_lang)
        return segments, words, detected_lang, False

    finally:
        try:
            import os
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass

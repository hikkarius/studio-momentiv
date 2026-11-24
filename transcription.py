"""
transcription.py — Whisper model singleton, audio extraction, caching, and transcription pipeline.
"""
from __future__ import annotations

import bisect
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


# ─── Faster-Whisper passes: batched on GPU, sequential otherwise ─────────────

SAMPLE_RATE = 16000
# Batched decoding merges speech into 30 s chunks; split the result back into sentence-sized
# segments like the sequential mode gives (median ~5 s, rarely over 12 s): Gemini prompts,
# clip refinement and SRT all expect that shape.
SPLIT_PAUSE_S = 0.6
SPLIT_MAX_S = 12.0
SPLIT_MIN_SENTENCE_S = 1.0
_CLOSERS = "\"'»”)\\]"
_SENTENCE_END_RE = re.compile(r"[.!?…]+[" + _CLOSERS + r"]*$")
_SOFT_BREAK_RE = re.compile(r"[,;:–—]+[" + _CLOSERS + r"]*$")
# A speech region (by VAD) that batched decoding left (almost) empty is decoded again sequentially.
GAP_MIN_S = 2.0
GAP_JOIN_S = 1.0
GAP_MAX_REGION_S = 30.0
GAP_MIN_COVERAGE = 0.2
OOM_FALLBACK_BATCH = 4


def whisper_batch_size(device: str) -> int:
    """Batch size for this device: batched decoding only on a GPU; 0 = the old sequential mode."""
    if device != "cuda":
        return 0
    try:
        return max(0, int(CONFIG.get("whisper", {}).get("batch_size", 8) or 0))
    except (TypeError, ValueError):
        return 0


def _is_oom(ex: Exception) -> bool:
    low = str(ex).lower()
    return "out of memory" in low or "cudaerrormemoryallocation" in low


def _report(status_cb, pct: float, msg: str) -> None:
    if not status_cb:
        return
    try:
        status_cb(pct, msg)
    except TypeError:
        status_cb(msg)


def consume_segments(raw_stream, total_duration: float, status_cb=None, label: str = "Whisper"):
    """Reads a lazy Whisper segment stream into plain dicts, reporting progress."""
    segs: list[dict] = []
    wrds: list[dict] = []
    for seg in raw_stream:
        if total_duration > 0:
            pct = min(1.0, seg.end / total_duration)
            _report(status_cb, pct, f"Транскрипція аудіо ({label}): {int(pct * 100)}%")
        segs.append({"start": round(seg.start, 2), "end": round(seg.end, 2), "text": seg.text.strip()})
        for w in seg.words or []:
            wrds.append({"start": round(w.start, 2), "end": round(w.end, 2), "word": w.word.strip()})
    return segs, wrds


def _open_stream(transcribe, source, kwargs: dict, cfg_lang: str, status_cb):
    """Starts a transcription; with language «auto» pins the detected one and restarts
    (the stream is lazy, so nothing is decoded yet). Mutates kwargs. Returns (stream, info, lang)."""
    stream, info = transcribe(source, **kwargs)
    lang = info.language
    log.info(f"Detected language (faster-whisper): {lang} (p={getattr(info, 'language_probability', '?')})")
    if cfg_lang == AUTO and lang in INITIAL_PROMPTS:
        kwargs["language"] = lang
        kwargs["initial_prompt"] = INITIAL_PROMPTS[lang]
        stream, info = transcribe(source, **kwargs)
    elif cfg_lang == AUTO:
        kwargs["language"] = lang
    if status_cb:
        status_cb(f"Мова відео: {language_label(lang)}")
    return stream, info, lang


def _duration(info) -> float:
    try:
        return float(getattr(info, "duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def split_segments_from_words(
    words: list[dict],
    pause: float = SPLIT_PAUSE_S,
    max_len: float = SPLIT_MAX_S,
    min_sentence: float = SPLIT_MIN_SENTENCE_S,
) -> list[dict]:
    """Builds natural segments from word timestamps: a new one after a pause, after the end of a
    sentence, or before it grows past max_len (then at the last comma if there is one)."""
    segments: list[dict] = []

    def emit(ws: list[dict]) -> None:
        if ws:
            segments.append({
                "start": ws[0]["start"],
                "end": ws[-1]["end"],
                "text": " ".join(w["word"] for w in ws if w["word"]).strip(),
            })

    cur: list[dict] = []
    for w in words:
        if cur:
            last = cur[-1]
            gap = w["start"] - last["end"]
            sentence_done = (bool(_SENTENCE_END_RE.search(last["word"]))
                             and last["end"] - cur[0]["start"] >= min_sentence)
            if gap >= pause or sentence_done:
                emit(cur)
                cur = []
            elif w["end"] - cur[0]["start"] > max_len:
                # break at the last comma in the second half, else right here
                cut = next(
                    (i for i in range(len(cur) - 1, len(cur) // 2 - 1, -1)
                     if _SOFT_BREAK_RE.search(cur[i]["word"])),
                    len(cur) - 1,
                )
                emit(cur[: cut + 1])
                cur = cur[cut + 1:]
        cur.append(w)
    emit(cur)
    return segments


def find_unheard_regions(
    speech: list[tuple[float, float]],
    words: list[dict],
    min_len: float = GAP_MIN_S,
    join_gap: float = GAP_JOIN_S,
    max_region: float = GAP_MAX_REGION_S,
    min_coverage: float = GAP_MIN_COVERAGE,
) -> list[tuple[float, float]]:
    """Speech regions (seconds) at least min_len long that decoding left (almost) without words.
    A VAD piece counts as unheard when words cover less than min_coverage of its time; neighbouring
    unheard pieces (gap <= join_gap) are joined, up to max_region seconds each."""
    ws = sorted(words, key=lambda w: w["start"])
    starts = [w["start"] for w in ws]

    def coverage(s: float, e: float) -> float:
        covered = 0.0
        for w in ws[bisect.bisect_left(starts, s - 5.0): bisect.bisect_right(starts, e)]:
            covered += max(0.0, min(e, w["end"]) - max(s, w["start"]))
        return covered / max(1e-6, e - s)

    regions: list[list[float]] = []
    prev_unheard = False
    for s, e in sorted(speech):
        unheard = coverage(s, e) < min_coverage
        if unheard and prev_unheard and s - regions[-1][1] <= join_gap and e - regions[-1][0] <= max_region:
            regions[-1][1] = e
        elif unheard:
            regions.append([s, e])
        prev_unheard = unheard
    return [(round(s, 2), round(e, 2)) for s, e in regions if e - s >= min_len]


def merge_refilled_words(words: list[dict], refills: dict) -> list[dict]:
    """Puts re-decoded words into their regions (replacing the stray words batched decoding left
    there when the new pass heard more) and returns all words in time order."""
    out = list(words)
    for (s, e), new in refills.items():
        inside = [w for w in out if s - 0.2 <= w["start"] <= e + 0.2]
        if len(new) > len(inside):
            out = [w for w in out if not (s - 0.2 <= w["start"] <= e + 0.2)] + new
    out.sort(key=lambda w: (w["start"], w["end"]))
    return out


def _speech_regions(audio, vad_parameters: Optional[dict]) -> list[tuple[float, float]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    params = dict(vad_parameters or {})
    params["max_speech_duration_s"] = 30
    return [(c["start"] / SAMPLE_RATE, c["end"] / SAMPLE_RATE)
            for c in get_speech_timestamps(audio, VadOptions(**params))]


def _refill_gaps(model, audio, gaps, kwargs: dict) -> dict:
    """Decodes each gap region again with the sequential decoder (it falls back to higher
    temperatures, which batched decoding never does)."""
    kw = {k: v for k, v in kwargs.items() if k not in ("vad_filter", "vad_parameters")}
    kw["vad_filter"] = False
    refills = {}
    for s, e in gaps:
        piece = audio[int(s * SAMPLE_RATE): int(e * SAMPLE_RATE)]
        stream, _info = model.transcribe(piece, **kw)
        _segs, ws = consume_segments(stream, 0.0)
        refills[(s, e)] = [
            {"start": round(w["start"] + s, 2), "end": round(w["end"] + s, 2), "word": w["word"]} for w in ws
        ]
    return refills


def _batched_pass(model, audio, kwargs: dict, cfg_lang: str, status_cb, batch_size: int, label: str):
    from faster_whisper import BatchedInferencePipeline

    pipe = BatchedInferencePipeline(model)
    stream, info, lang = _open_stream(
        lambda src, **kw: pipe.transcribe(src, batch_size=batch_size, **kw), audio, kwargs, cfg_lang, status_cb
    )
    _segs, words = consume_segments(stream, _duration(info), status_cb, label)

    gaps = find_unheard_regions(_speech_regions(audio, kwargs.get("vad_parameters")), words)
    if gaps:
        refills = _refill_gaps(model, audio, gaps, kwargs)
        before = len(words)
        words = merge_refilled_words(words, refills)
        log.info(
            f"Batched Whisper left {len(gaps)} speech region(s) unheard "
            f"({', '.join(f'{s:.0f}-{e:.0f}s' for s, e in gaps[:10])}); "
            f"re-decoded them: {before} → {len(words)} words"
        )
    else:
        log.info("Batched Whisper: no unheard speech regions")
    return split_segments_from_words(words), words, lang


def faster_whisper_pass(model, audio_path: str, kwargs: dict, cfg_lang: str, status_cb,
                        batch_size: int = 0, device: str = "cpu"):
    """One Faster-Whisper transcription. batch_size > 0: batched decoding (several 30 s chunks at
    once, ~2.5x faster on a GPU); on CUDA out-of-memory retries with a smaller batch, then sequential.
    Returns (segments, words, detected_lang)."""
    kwargs = dict(kwargs)
    label = "CUDA" if device == "cuda" else "Whisper"
    source = audio_path
    if batch_size > 0:
        from faster_whisper.audio import decode_audio

        audio = decode_audio(audio_path, sampling_rate=SAMPLE_RATE)
        source = audio
        ladder = [batch_size] + ([OOM_FALLBACK_BATCH] if batch_size > OOM_FALLBACK_BATCH else [])
        for b in ladder:
            try:
                return _batched_pass(model, audio, dict(kwargs), cfg_lang, status_cb, b, label)
            except Exception as ex:
                if not _is_oom(ex):
                    raise
                log.warning(f"Batched Whisper (batch {b}) ran out of video memory: {ex}")
                if status_cb:
                    status_cb("Забракло відеопам'яті, розпізнаємо меншими порціями...")
        log.warning("Batched Whisper does not fit in video memory; using the sequential mode")

    stream, info, lang = _open_stream(model.transcribe, source, kwargs, cfg_lang, status_cb)
    segments, words = consume_segments(stream, _duration(info), status_cb, label)
    return segments, words, lang


def resolve_language(language: Optional[str] = None) -> str:
    """The language choice for Whisper: the one given, else the one from the settings."""
    return (language or CONFIG.get("whisper", {}).get("language") or "uk").strip().lower()


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
    cfg_lang = resolve_language(language)
    cached = load_transcript_cache_for(video_path, cfg_lang)
    if cached:
        if status_cb:
            status_cb("✅ Транскрипцію знайдено у кеші — Whisper пропущено!")
        return (*cached, True)

    segments, words, detected_lang = transcribe_media(video_path, cfg_lang, status_cb)
    save_transcript_cache(video_path, segments, words, detected_lang, requested=cfg_lang)
    return segments, words, detected_lang, False


def transcribe_media(
    video_path: str,
    cfg_lang: str,
    status_cb: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], list[dict], str]:
    """Whisper on any audio or video file, without the cache. Returns (segments, words, detected_lang)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = tmp.name
    acquired = False

    try:
        if status_cb:
            status_cb("Витяг аудіодоріжки...")
        log.info(f"Extracting audio from {video_path!r}")
        extract_audio(video_path, audio_path)

        if status_cb:
            status_cb("Транскрибація через Whisper...")
        log.info("Starting Whisper transcription")

        app_state.whisper_acquire()
        acquired = True
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

            device = getattr(app_state, "whisper_device", "cpu")
            try:
                segments, words, detected_lang = faster_whisper_pass(
                    model, audio_path, transcribe_kwargs, cfg_lang, status_cb,
                    batch_size=whisper_batch_size(device), device=device,
                )
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
                    segments, words, detected_lang = faster_whisper_pass(
                        cpu_model, audio_path, transcribe_kwargs, cfg_lang, status_cb, batch_size=0, device="cpu",
                    )
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

        return segments, words, detected_lang

    finally:
        if acquired:
            app_state.whisper_release()
        try:
            import os
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass

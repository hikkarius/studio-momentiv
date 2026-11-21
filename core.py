"""
core.py — Unified facade and public API for AI Video Moments Analyzer.

Decomposed into dedicated modules:
  • constants.py     — Enums, presets, filter modes.
  • config_loader.py — Configuration loading and Gemini client setup.
  • utils.py         — File paths, durations, audio extraction, escaping.
  • transcription.py — Whisper model singleton, transcript cache, audio pipeline.
  • ai_selector.py   — Gemini prompt generation, multi-model fallback, JSON parsing.
"""
from __future__ import annotations

import logging
from utils import register_cuda_dll_dirs, setup_console_encoding
setup_console_encoding()
register_cuda_dll_dirs()

# Centralized structured logging
from logger import JsonFormatter, LogFormat, get_logger, setup_logging

setup_logging()
log = get_logger("shorts_core")

# Re-exports from constants
from constants import (
    FILTER_INSTRUCTIONS,
    FILTER_MODES,
    FilterMode,
)

# Re-exports from state
from state import (
    AppState,
    app_state,
)

# Re-exports from config_schema & config_loader
from config_schema import (
    AppConfig,
    ClipsConfig,
    WhisperConfig,
)
from config_loader import (
    CANDIDATE_MODELS,
    CLIP_INTERVAL_SEC,
    CONFIG,
    app_config,
    ensure_gemini_api_key,
    get_gemini_client,
)

# Re-exports from utils
from utils import (
    download_from_url,
    extract_audio,
    get_video_duration,
    wait_for_file_ready,
    setup_console_encoding,
)

# Re-exports from transcription
from transcription import (
    _cache_path,
    get_whisper,
    load_transcript_cache,
    save_transcript_cache,
    transcribe_video,
)

# Re-exports from ai_selector
from ai_selector import (
    GeminiUnavailableError,
    analyze_and_find_clips,
    validate_clips,
)

__all__ = [
    # State & Lifecycle
    "AppState",
    "app_state",
    # Config & API Key
    "AppConfig",
    "app_config",
    "CONFIG",
    "CLIP_INTERVAL_SEC",
    "CANDIDATE_MODELS",
    "ensure_gemini_api_key",
    "get_gemini_client",
    # Constants
    "FILTER_INSTRUCTIONS",
    "FilterMode",
    "FILTER_MODES",
    # Utilities
    "get_video_duration",
    "wait_for_file_ready",
    "extract_audio",
    "download_from_url",
    "setup_console_encoding",
    # Transcription
    "get_whisper",
    "_cache_path",
    "load_transcript_cache",
    "save_transcript_cache",
    "transcribe_video",
    # AI Selection
    "GeminiUnavailableError",
    "validate_clips",
    "analyze_and_find_clips",
    # Logger
    "log",
    "setup_logging",
    "get_logger",
    "JsonFormatter",
    "LogFormat",
]

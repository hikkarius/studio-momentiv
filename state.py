"""
state.py — Centralized, thread-safe application state and resource manager (R-2).

Manages lifecycles of heavy/networked resources (Whisper model, Gemini client)
with double-checked locking to guarantee single initialization across concurrent threads.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from config_loader import CONFIG, ensure_gemini_api_key
from utils import GpuCapabilities

log = logging.getLogger("shorts_core")

# One Gemini request may take this long (ms): a long transcript window needs 1-2 minutes.
GEMINI_TIMEOUT_MS: int = 120_000


class AppState:
    """
    Thread-safe resource manager and state container.
    """

    def __init__(self):
        self._whisper_lock = threading.Lock()
        self._gemini_lock = threading.Lock()
        self._gpu_lock = threading.Lock()
        self._whisper = None
        self._gemini_client = None
        self._gpu_caps: Optional[GpuCapabilities] = None
        self._cached_api_key: Optional[str] = None
        self._whisper_engine: str = "faster_whisper"

    def get_whisper(self):
        """
        Thread-safe lazy initialization for WhisperModel with fallback support.
        Uses double-checked locking to ensure single instantiation across threads.
        """
        if self._whisper is not None:
            return self._whisper

        with self._whisper_lock:
            if self._whisper is not None:
                return self._whisper

            cfg = CONFIG["whisper"]
            device: str = cfg["device"]
            compute_type: str = cfg["compute_type"]

            if device == "auto":
                try:
                    caps = self.get_gpu_capabilities()
                    has_cuda = bool(caps.has_cuda)
                except Exception:
                    has_cuda = False

                device = "cuda" if has_cuda else "cpu"
                log.info(f"Whisper device auto-detect → using {device.upper()}")

            if device == "cuda":
                compute_type = "float16"

            if device == "cuda":
                from utils import register_cuda_dll_dirs
                register_cuda_dll_dirs()

            use_faster = cfg.get("faster_whisper", True)
            if use_faster:
                try:
                    from faster_whisper import WhisperModel

                    log.info(
                        f"Initializing Faster-Whisper (CTranslate2): model={cfg['model']!r} "
                        f"device={device} compute_type={compute_type}"
                    )
                    self._whisper = WhisperModel(
                        cfg["model"],
                        device=device,
                        compute_type=compute_type,
                        cpu_threads=cfg["cpu_threads"] if device == "cpu" else 0,
                    )
                    self._whisper_engine = "faster_whisper"
                    return self._whisper
                except Exception as e:
                    if device == "cuda":
                        log.warning(f"Faster-Whisper CUDA init failed ({e}), falling back to CPU int8...")
                        try:
                            from faster_whisper import WhisperModel
                            self._whisper = WhisperModel(
                                cfg["model"],
                                device="cpu",
                                compute_type="int8",
                                cpu_threads=cfg["cpu_threads"],
                            )
                            self._whisper_engine = "faster_whisper"
                            return self._whisper
                        except Exception as cpu_e:
                            log.warning(f"Faster-Whisper CPU init failed ({cpu_e}), attempting standard Whisper...")
                    else:
                        log.warning(f"Faster-Whisper initialization failed ({e}), attempting fallback to standard Whisper...")

            # Fallback to standard OpenAI Whisper if faster_whisper is disabled or failed
            try:
                import whisper

                log.info(f"Initializing standard openai-whisper: model={cfg['model']!r} device={device}")
                self._whisper = whisper.load_model(cfg["model"], device=device)
                self._whisper_engine = "openai_whisper"
                return self._whisper
            except Exception as e:
                log.error(f"Whisper initialization failed: {e}")
                raise RuntimeError(
                    f"Не вдалося ініціалізувати Whisper модель (перевірте faster-whisper або whisper): {e}"
                ) from e

    def warmup_whisper(self):
        """Eagerly warms up the Whisper model."""
        log.info("Warming up Whisper model...")
        return self.get_whisper()

    def set_whisper_fallback(self, model, engine: str = "faster_whisper") -> None:
        """Thread-safely caches a fallback Whisper model (e.g. CPU int8) after runtime GPU failure."""
        with self._whisper_lock:
            self._whisper = model
            self._whisper_engine = engine
        log.info(f"Cached fallback Whisper model in AppState (engine={engine}).")

    def reset_whisper(self) -> None:
        """Forgets the loaded Whisper model, so the next analysis loads the newly chosen one."""
        with self._whisper_lock:
            self._whisper = None
        log.info("Whisper model will be reloaded on next use.")

    def get_gemini_client(self, api_key: Optional[str] = None):
        """
        Thread-safe client factory and cache for Google GenAI Client.
        If a new api_key is passed, the client is refreshed.
        """
        key = api_key or ensure_gemini_api_key()
        if not key:
            raise ValueError("GEMINI_API_KEY не знайдено! Вкажіть його у файлі .env або run_studio.bat")

        # Fast path if key matches cached client
        if self._gemini_client is not None and self._cached_api_key == key:
            return self._gemini_client

        with self._gemini_lock:
            if self._gemini_client is not None and self._cached_api_key == key:
                return self._gemini_client

            from google import genai
            # 45 s was too short: Google cut long transcript windows with 504 DEADLINE_EXCEEDED.
            log.info(f"Initializing new Gemini API client with {GEMINI_TIMEOUT_MS // 1000}s request timeout")
            self._gemini_client = genai.Client(api_key=key, http_options={"timeout": GEMINI_TIMEOUT_MS})
            self._cached_api_key = key
            return self._gemini_client

    def get_gpu_capabilities(self, force_refresh: bool = False) -> GpuCapabilities:
        """
        Thread-safe probe and cache for system GPU capabilities (NVENC & CUDA).
        """
        if self._gpu_caps is not None and not force_refresh:
            return self._gpu_caps

        with self._gpu_lock:
            if self._gpu_caps is not None and not force_refresh:
                return self._gpu_caps

            from utils import probe_gpu_capabilities
            self._gpu_caps = probe_gpu_capabilities()
            return self._gpu_caps

    def reset(self) -> None:
        """Resets cached instances (useful for testing and configuration reloads)."""
        with self._whisper_lock:
            self._whisper = None
        with self._gemini_lock:
            self._gemini_client = None
            self._cached_api_key = None
        with self._gpu_lock:
            self._gpu_caps = None
        log.info("AppState resources reset.")


# Global application state instance
app_state = AppState()

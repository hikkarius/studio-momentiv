"""
config_schema.py — Pydantic schema validation for application configuration (R-4).
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class WhisperConfig(BaseModel):
    model: str = Field(default="small", min_length=1)
    language: str = Field(default="uk")
    device: str = Field(default="auto", pattern=r"^(auto|cpu|cuda)$")
    compute_type: str = Field(default="int8", pattern=r"^(int8|float16|float32)$")
    cpu_threads: int = Field(default=8, ge=1, le=64)
    faster_whisper: bool = True
    beam_size: int = Field(default=5, ge=1, le=10)
    batch_size: int = Field(default=8, ge=0, le=64)
    idle_unload_min: int = Field(default=10, ge=0, le=1440)


class ClipsConfig(BaseModel):
    interval_sec: int = Field(default=28, ge=5, le=3600)
    default_min_len: int = Field(default=15, ge=5, le=300)
    default_max_len: int = Field(default=60, ge=10, le=600)
    max_count: int = Field(default=100, ge=1, le=1000)
    max_overlap_sec: float = Field(default=2.0, ge=0, le=600)

    @model_validator(mode="after")
    def validate_lengths(self) -> ClipsConfig:
        if self.default_min_len >= self.default_max_len:
            raise ValueError(
                f"default_min_len ({self.default_min_len}) must be strictly less than "
                f"default_max_len ({self.default_max_len})"
            )
        return self


class GeminiConfig(BaseModel):
    candidate_models: List[str] = Field(
        default_factory=lambda: [
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash-lite",
        ],
        min_length=1,
    )
    rerank: bool = True
    parallel_windows: int = Field(default=3, ge=1, le=8)
    thinking_level: str = Field(default="minimal", pattern=r"^(|minimal|low|medium|high)$")
    max_rpm_per_model: int = Field(default=12, ge=0, le=1000)


class CacheConfig(BaseModel):
    enabled: bool = True
    dir: str = Field(default=".transcript_cache", min_length=1)


class LoggingConfig(BaseModel):
    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")
    format: str = Field(default="text", pattern=r"^(text|json)$")
    file: Optional[str] = "shorts_automator.log"


class StudioConfig(BaseModel):
    """Studio defaults changed from the «Налаштування» screen."""
    default_clips: int = Field(default=0, ge=0, le=100)         # 0 = auto (by video length)
    default_filter: str = Field(default="Viral Hooks")
    cleanup_days: int = Field(default=0, ge=0, le=3650)         # delete re-creatable clip files older than N days; 0 = never
    daily_backup: bool = True       # copy the database and transcripts to backups/ once a day, keep 7
    keep_awake: bool = True         # Windows: do not let the laptop sleep while the queue has work
    wait_for_limit: bool = True     # Gemini limit exhausted: pause the queue and resume, instead of failing


class AppConfig(BaseModel):
    whisper: WhisperConfig = Field(default_factory=WhisperConfig)
    clips: ClipsConfig = Field(default_factory=ClipsConfig)
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    studio: StudioConfig = Field(default_factory=StudioConfig)


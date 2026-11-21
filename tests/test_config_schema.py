"""
tests/test_config_schema.py — Unit tests for Pydantic AppConfig schema and validation.
"""
import pytest
from pydantic import ValidationError

from config_schema import (
    AppConfig,
    ClipsConfig,
    WhisperConfig,
)


def test_default_config_is_valid():
    cfg = AppConfig()
    assert cfg.whisper.model == "small"
    assert cfg.whisper.language == "uk"
    assert cfg.whisper.device == "auto"
    assert cfg.whisper.faster_whisper is True
    assert cfg.clips.interval_sec == 28
    assert cfg.clips.max_count == 100
    assert len(cfg.gemini.candidate_models) > 0
    assert cfg.cache.enabled is True
    assert cfg.logging.level == "INFO"


def test_custom_valid_config():
    data = {
        "whisper": {"model": "large-v3", "device": "cuda", "compute_type": "float16", "cpu_threads": 4},
        "clips": {"interval_sec": 30, "default_min_len": 15, "default_max_len": 45, "max_count": 50},
        "cache": {"enabled": False, "dir": ".cache"},
        "logging": {"level": "DEBUG", "format": "json"},
    }
    cfg = AppConfig.model_validate(data)
    assert cfg.whisper.model == "large-v3"
    assert cfg.whisper.device == "cuda"
    assert cfg.cache.enabled is False
    assert cfg.logging.format == "json"


def test_invalid_device_raises_validation_error():
    with pytest.raises(ValidationError):
        WhisperConfig(device="tpu")  # only auto, cpu, cuda


def test_invalid_cpu_threads_raises_validation_error():
    with pytest.raises(ValidationError):
        WhisperConfig(cpu_threads=0)  # ge=1 required


def test_inverted_clip_lengths_raises_validation_error():
    with pytest.raises(ValidationError) as exc:
        ClipsConfig(default_min_len=60, default_max_len=30)
    assert "default_min_len" in str(exc.value)


def test_empty_candidate_models_raises_validation_error():
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"gemini": {"candidate_models": []}})

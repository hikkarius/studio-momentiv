"""
tests/test_state.py — Unit tests for AppState resource management and thread safety.
"""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest
from state import AppState


def test_app_state_initialization():
    state = AppState()
    assert state._whisper is None
    assert state._gemini_client is None
    assert state._cached_api_key is None


def test_app_state_reset():
    state = AppState()
    state._whisper = "fake_whisper"
    state._gemini_client = "fake_client"
    state._cached_api_key = "fake_key"

    state.reset()
    assert state._whisper is None
    assert state._gemini_client is None
    assert state._cached_api_key is None


@patch("faster_whisper.WhisperModel")
def test_whisper_singleton_concurrent_threads(mock_whisper_cls):
    """Verifies that multiple concurrent threads receive the exact same Whisper instance."""
    mock_instance = MagicMock()
    mock_whisper_cls.return_value = mock_instance

    state = AppState()

    def _worker():
        return state.get_whisper()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_worker) for _ in range(20)]
        results = [f.result() for f in futures]

    assert len(results) == 20
    # All 20 threads must receive the identical object
    assert all(r is mock_instance for r in results)
    # The constructor must have been called exactly once
    assert mock_whisper_cls.call_count == 1


@patch("google.genai.Client")
def test_gemini_client_caching_and_refresh(mock_client_cls):
    mock_client1 = MagicMock()
    mock_client2 = MagicMock()
    mock_client_cls.side_effect = [mock_client1, mock_client2]

    state = AppState()

    # Call with key 1
    c1 = state.get_gemini_client(api_key="KEY_AAA")
    c1_again = state.get_gemini_client(api_key="KEY_AAA")
    assert c1 is mock_client1
    assert c1_again is mock_client1
    assert mock_client_cls.call_count == 1

    # Call with key 2 -> refreshes client
    c2 = state.get_gemini_client(api_key="KEY_BBB")
    assert c2 is mock_client2
    assert mock_client_cls.call_count == 2


@patch("utils.probe_gpu_capabilities")
def test_gpu_capabilities_caching_and_reset(mock_probe):
    fake_caps = MagicMock()
    mock_probe.return_value = fake_caps

    state = AppState()
    caps1 = state.get_gpu_capabilities()
    caps2 = state.get_gpu_capabilities()
    assert caps1 is fake_caps
    assert caps2 is fake_caps
    assert mock_probe.call_count == 1

    # Reset clears cached capabilities
    state.reset()
    assert state._gpu_caps is None
    caps3 = state.get_gpu_capabilities()
    assert caps3 is fake_caps
    assert mock_probe.call_count == 2


def test_gemini_client_missing_key_raises(monkeypatch):
    state = AppState()
    monkeypatch.setattr("state.ensure_gemini_api_key", lambda: None)
    with pytest.raises(ValueError) as exc:
        state.get_gemini_client(api_key=None)
    assert "GEMINI_API_KEY не знайдено" in str(exc.value)


@patch("faster_whisper.WhisperModel")
def test_whisper_cuda_fallback_to_cpu(mock_whisper_cls, monkeypatch):
    state = AppState()
    monkeypatch.setattr("state.CONFIG", {
        "whisper": {
            "faster_whisper": True,
            "device": "cuda",
            "model": "small",
            "compute_type": "float16",
            "cpu_threads": 4,
        }
    })

    # First call (CUDA) raises error, second call (CPU) succeeds
    mock_cpu_model = MagicMock()
    mock_whisper_cls.side_effect = [RuntimeError("CUDA out of memory"), mock_cpu_model]

    model = state.get_whisper()
    assert model is mock_cpu_model
    assert mock_whisper_cls.call_count == 2

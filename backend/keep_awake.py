"""
backend/keep_awake.py — «Не давати ноутбуку заснути, поки працює черга».

On Windows the queue thread asks the system not to sleep (SetThreadExecutionState with
ES_SYSTEM_REQUIRED): the screen may still turn off, the work goes on. The request belongs to the
calling thread and ends when it is cleared or the studio closes. Elsewhere this does nothing.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("shorts_core")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_active = False


def set_awake(on: bool) -> bool:
    """Turns the «stay awake» request on or off (call from the same thread). Returns the state."""
    global _active
    if on == _active:
        return _active
    if sys.platform == "win32":
        try:
            import ctypes
            flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
            if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
                log.warning("Не вдалося попросити Windows не засинати.")
                return _active
        except Exception as e:  # never let this break the queue
            log.warning(f"Не вдалося попросити Windows не засинати: {e}")
            return _active
    _active = on
    log.info("Черга працює: ноутбук не засне." if on else "Черга закінчилась: ноутбук може засинати.")
    return _active


def is_active() -> bool:
    return _active

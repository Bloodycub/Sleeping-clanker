"""Keep the PC awake while the runner works (2026-10-13, spec section 8).

SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) on, ES_CONTINUOUS off.
A no-op on other operating systems. A failure is logged and never stops the run.

(2026-10-20, 0.4.4 logfix F) on()/off() instead of one block around the whole run: the loop holds it while
an attempt runs (a goal's rounds follow each other without letting go) and while it waits for a
usage-limit reset (the runner must wake at reset; DECISIONS.md, error-log fixes), and lets go while it
polls an idle queue or waits for --start-at. Both calls are idempotent and log only on a change. The
context manager form (on at enter, off at exit) is kept.
"""

from __future__ import annotations

import sys

from .log import get_logger

log = get_logger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled and sys.platform == "win32"
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def _set(self, flags: int) -> bool:
        import ctypes

        prev = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags))
        return prev != 0

    def on(self, why: str = "the runner is working") -> None:
        if not self.enabled or self._active:
            return
        try:
            self._active = self._set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            if self._active:
                log.info("keep-awake: on (%s)", why)
            else:
                log.warning("keep-awake: SetThreadExecutionState returned 0; PC may sleep")
        except (OSError, AttributeError) as exc:
            log.warning("keep-awake unavailable: %s", exc)

    def off(self, why: str = "idle") -> None:
        if not self._active:
            return
        try:
            self._set(ES_CONTINUOUS)
            log.info("keep-awake: off (%s)", why)
        except (OSError, AttributeError) as exc:
            log.warning("keep-awake reset failed: %s", exc)
        self._active = False

    def __enter__(self) -> "KeepAwake":
        if not self.enabled:
            log.debug("keep-awake: not on Windows or disabled; no-op")
            return self
        self.on()
        return self

    def __exit__(self, *exc_info) -> None:
        self.off("runner exits")

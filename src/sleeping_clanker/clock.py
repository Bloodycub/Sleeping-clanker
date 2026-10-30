"""Injectable clock + sleep (2026-10-13): every wait goes through here so tests never really sleep."""

from __future__ import annotations

import time
from datetime import datetime, timezone, tzinfo


class Clock:
    """Real wall clock. `local_tz=None` means the OS local zone."""

    # (2026-10-13, SPEC_VSCODE section 2) Max sleep chunk while the loop watches stop.request.
    stop_poll_s = 1.0

    def __init__(self, local_tz: tzinfo | None = None) -> None:
        self._local_tz = local_tz

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def local_tz(self) -> tzinfo:
        if self._local_tz is not None:
            return self._local_tz
        tz = self.now().astimezone().tzinfo
        assert tz is not None
        return tz


def iso(dt: datetime | None) -> str | None:
    """ISO-8601 UTC with microsecond precision, or None.

    (2026-10-13, fix B2) Was whole seconds; same-second adds then tied on created_at.
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

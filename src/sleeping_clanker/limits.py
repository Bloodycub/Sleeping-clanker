"""Usage-limit detection and reset-time parsing (2026-10-13, spec section 7.4-7.5).

The limit text formats are not documented, so every plausible form is handled:
  "usage limit reached|<epoch s or ms>", "You've hit your session limit · resets 3:45pm",
  "resets 3pm (Europe/Helsinki)", "resets Mon 12:00am", "resets 15:00".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo

from .log import get_logger

log = get_logger(__name__)

LIMIT_TEXT_RES = (
    re.compile(r"hit your .*limit", re.IGNORECASE),
    re.compile(r"usage limit reached", re.IGNORECASE),
)
_EPOCH_RE = re.compile(r"usage limit reached\|(\d{9,14})", re.IGNORECASE)
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RESETS_RE = re.compile(
    r"resets\s+(?:at\s+)?"
    r"(?:(?P<day>mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
    r"(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
RESET_MARGIN = timedelta(seconds=90)


def text_says_limit(text: str | None) -> bool:
    return bool(text) and any(r.search(text) for r in LIMIT_TEXT_RES)


def epoch_to_dt(value: float | int | str) -> datetime:
    v = float(value)
    if v > 1e12:  # milliseconds
        v /= 1000.0
    return datetime.fromtimestamp(v, timezone.utc)


def _load_zone(name: str, fallback: tzinfo) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name.strip())
    except Exception as exc:  # ZoneInfoNotFoundError, ValueError, missing tzdata on Windows
        log.debug("zone %r not loadable (%s); using local time", name, exc)
        return fallback


def parse_resets_text(text: str, now: datetime, local_tz: tzinfo) -> datetime | None:
    """Next occurrence (UTC) of the `resets <time>` clause in `text`, or None."""
    m = _RESETS_RE.search(text or "")
    if not m:
        return None
    hour = int(m.group("hour"))
    minute = int(m.group("minute") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    tz = _load_zone(m.group("tz"), local_tz) if m.group("tz") else local_tz
    now_local = now.astimezone(tz)
    cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    day = m.group("day")
    if day:
        # (2026-10-13) Weekly limits name a weekday: roll to that weekday, a week later if passed.
        cand += timedelta(days=(_DAYS.index(day.lower()[:3]) - now_local.weekday()) % 7)
        if cand <= now_local:
            cand += timedelta(days=7)
    elif cand <= now_local:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc)


def compute_reset(
    resets_at: float | int | str | None,
    texts: list[str],
    now: datetime,
    local_tz: tzinfo,
    poll_min: float,
) -> tuple[datetime, str]:
    """Reset time (UTC, without margin) and the rule that produced it."""
    if resets_at not in (None, ""):
        try:
            return epoch_to_dt(resets_at), "rate_limit_info.resetsAt"
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            log.warning("unusable resetsAt %r: %s", resets_at, exc)
    for t in texts:
        m = _EPOCH_RE.search(t or "")
        if m:
            try:
                return epoch_to_dt(m.group(1)), "text: usage limit reached|<epoch>"
            except (ValueError, OverflowError, OSError) as exc:
                log.warning("unusable epoch in limit text %r: %s", m.group(1), exc)
    for t in texts:
        dt = parse_resets_text(t, now, local_tz)
        if dt is not None:
            return dt, "text: resets <time>"
    return now + timedelta(minutes=poll_min), f"fallback: now + poll-min ({poll_min:g} min)"

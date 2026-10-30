"""(2026-10-26, usage forecast) Usage samples from the stream's rate_limit_event lines.

Every rate_limit_event a run's stream-json carries (the runner already reads them) holds the account's
usage windows, e.g. rate_limit_info.unifiedWindows.five_hour = {"utilization": 0.93, "resetsAt": <epoch s>,
"status": "allowed_warning"}. The runner appends one compact sample per window to
<base data dir>/usage_samples.jsonl:

  {"t": <epoch s>, "window": "five_hour", "utilization": 0.93, "resets_at": <epoch s>, "status": "allowed_warning"}

Only these five fields: no prompt, no session id, no key, nothing read from another app's files, and no
network. The limits belong to the account, not to one queue, so the file is in the BASE data dir: one
file for every project and lane (RunOptions.usage_dir = the base dir).

Bounded: each write keeps the samples of the last KEEP_S (24 h) and at most KEEP_LINES (2000) lines.
Each write rewrites the file atomically (temp file in the same dir + os.replace) under a short lock
file, so two runners never lose each other's samples; a write that cannot get the lock within
LOCK_WAIT_S is skipped (a sample is never worth stalling a run). The extension computes the forecast
(vscode-extension/lib/usage.js) from summary(), which `lanes --json` and `status --json` expose as
`usage`.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Callable

from .log import get_logger
from .store import _replace_with_retry

log = get_logger(__name__)

SAMPLES_FILE = "usage_samples.jsonl"
KEEP_S = 24 * 3600          # samples older than this are dropped on each write
KEEP_LINES = 2000           # at most this many lines are kept
SAME_VALUE_GAP_S = 60.0     # an unchanged value is recorded again only after this long
RECENT_S = 6 * 3600         # summary(): the samples of the last 6 h ...
RECENT_MAX = 240            # ... at most this many (the newest), so the polled JSON stays small
LOCK_WAIT_S = 1.0
LOCK_STALE_S = 30.0
# (2026-10-27, integrate: review LOW-4) sane bounds (epoch s): a reset lies in 2000..2100, a sample time
# is never past 2100 (both far inside what a JS Date holds; the extension's usage.js has the same bounds)
EPOCH_MIN, EPOCH_MAX = 946684800.0, 4102444800.0   # 2000-01-01 .. 2100-01-01 UTC
_WINDOW_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _num(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _epoch_s(value) -> float | None:
    """An epoch in seconds (a value in ms, as some limit texts carry, is divided by 1000)."""
    f = _num(value)
    if f is None or f <= 0:
        return None
    return f / 1000.0 if f > 1e11 else f


def _sample(t: float, window, utilization, resets_at, status) -> dict | None:
    """(2026-10-27, integrate: usage forecast review LOW-4) a `t` past 2100 drops the sample and a
    `resets_at` outside 2000..2100 is stored as None: a garbage or nanosecond value used to reach the
    extension, whose Date().toISOString() then threw on every poll."""
    u = _num(utilization)
    t = _num(t)
    if not isinstance(window, str) or not _WINDOW_RE.match(window) or u is None or u < 0 or u > 10:
        return None
    if t is None or not 0 <= t <= EPOCH_MAX:
        return None
    st = status if isinstance(status, str) and _STATUS_RE.match(status) else None
    reset = _epoch_s(resets_at)
    if reset is not None and not EPOCH_MIN <= reset <= EPOCH_MAX:
        reset = None
    return {"t": round(t, 3), "window": window, "utilization": round(u, 4), "resets_at": reset, "status": st}


def samples_from_info(info: dict, t: float) -> list[dict]:
    """The samples one rate_limit_info gives: one per unifiedWindows entry with a numeric utilization,
    else the top-level window (rateLimitType + utilization) when it has one. [] = nothing usable."""
    if not isinstance(info, dict):
        return []
    out = []
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        for name, w in windows.items():
            if isinstance(w, dict):
                s = _sample(t, name, w.get("utilization"), w.get("resetsAt"), w.get("status"))
                if s is not None:
                    out.append(s)
    if not out:
        s = _sample(t, info.get("rateLimitType"), info.get("utilization"), info.get("resetsAt"),
                    info.get("status"))
        if s is not None:
            out.append(s)
    return out


def samples_path(base_dir: Path) -> Path:
    return Path(base_dir) / SAMPLES_FILE


def _valid(row) -> dict | None:
    if not isinstance(row, dict):
        return None
    t = _num(row.get("t"))
    if t is None:
        return None
    return _sample(t, row.get("window"), row.get("utilization"), row.get("resets_at"), row.get("status"))


def read_samples(base_dir: Path) -> list[dict]:
    """Every valid sample in the file, oldest first; a missing or unreadable file is []."""
    try:
        with open(samples_path(base_dir), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        log.debug("%s unreadable: %s", SAMPLES_FILE, exc)
        return []
    out = []
    for line in lines:
        try:
            s = _valid(json.loads(line))
        except ValueError:
            continue
        if s is not None:
            out.append(s)
    out.sort(key=lambda s: s["t"])
    return out


def _same(a: dict, b: dict) -> bool:
    return (a["utilization"], a["resets_at"], a["status"]) == (b["utilization"], b["resets_at"], b["status"])


def merge(old: list[dict], new: list[dict], now: float) -> tuple[list[dict], int]:
    """(old + new bounded to KEEP_S and KEEP_LINES, how many new samples were kept). A new sample equal
    to its window's last one and less than SAME_VALUE_GAP_S newer is dropped (a burst of identical
    events adds one line)."""
    rows = [s for s in old if now - s["t"] <= KEEP_S]
    last: dict[str, dict] = {}
    for s in rows:
        last[s["window"]] = s
    kept = 0
    for s in new:
        prev = last.get(s["window"])
        if prev is not None and _same(prev, s) and 0 <= s["t"] - prev["t"] < SAME_VALUE_GAP_S:
            continue
        rows.append(s)
        last[s["window"]] = s
        kept += 1
    rows.sort(key=lambda s: s["t"])
    return rows[-KEEP_LINES:], kept


@contextlib.contextmanager
def _lock(path: Path):
    """A lock file next to the samples; yields False when it could not be had within LOCK_WAIT_S."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + LOCK_WAIT_S
    fd = None
    while fd is None:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink()
                    continue
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(0.05)  # real sleep on purpose: a sub-second file lock retry, not a wait
    try:
        yield True
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def record(base_dir: Path, info: dict, now: float) -> int:
    """Append the samples of one rate_limit_info; returns how many were written (0 = none, or skipped)."""
    new = samples_from_info(info, now)
    if not new:
        return 0
    path = samples_path(base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock(path) as got:
        if not got:
            log.debug("%s busy; sample skipped", SAMPLES_FILE)
            return 0
        rows, kept = merge(read_samples(base_dir), new, now)
        if not kept:
            return 0
        fd, tmp = tempfile.mkstemp(prefix=SAMPLES_FILE + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                for s in rows:
                    fh.write(json.dumps(s, separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp, path)  # a poll reading the file holds it open for a moment
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return kept


def recorder(base_dir: Path | None, now: Callable[[], float]) -> Callable[[dict], None] | None:
    """The runner's on_rate_limit callback (None without a dir). It never raises: a sample that cannot be
    written is logged once per runner and the run goes on."""
    if base_dir is None:
        return None
    warned = []

    def on_rate_limit(info: dict) -> None:
        try:
            record(Path(base_dir), info, now())
        except Exception as exc:
            if not warned:
                warned.append(True)
                log.warning("usage sample not written (%s); the run goes on", exc)
    return on_rate_limit


def summary(base_dir: Path, now: float) -> dict | None:
    """The `usage` field of `lanes --json` / `status --json`: {"latest": {window: sample}, "samples":
    [the samples of the last RECENT_S, at most RECENT_MAX, oldest first]}; None = no samples at all."""
    rows = read_samples(base_dir)
    if not rows:
        return None
    latest: dict[str, dict] = {}
    for s in rows:
        latest[s["window"]] = s
    recent = [s for s in rows if now - s["t"] <= RECENT_S][-RECENT_MAX:]
    return {"latest": latest, "samples": recent}

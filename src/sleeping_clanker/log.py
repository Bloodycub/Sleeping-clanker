"""Shared logging setup for sleeping_clanker (2026-10-13).

Spec section 9: one shared logger, UTC ISO-8601 ms timestamps, level, module, RUN_ID, message.
Console at INFO (DEBUG with -v), <data>/logs/app.log at DEBUG rotated at UTC midnight (14 kept),
and <data>/logs/errors.log for WARNING and above (0.4.4 logfix; was ERROR).
A broken file sink falls back to the console.
Logs are never deleted at start-up; rotation is the only thing that removes old files.
"""

from __future__ import annotations

import logging
import secrets
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT_NAME = "sleeping_clanker"  # (2026-10-26, 0.4.13) the package name; the log format has no logger name
_FORMAT = "%(asctime)s %(levelname)s %(module)s %(run_id)s %(message)s"

_run_id: str = "-"


def new_run_id() -> str:
    """Return a short, sortable id for one process run (UTC time + random suffix)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


def get_run_id() -> str:
    return _run_id


def get_logger(name: str | None = None) -> logging.Logger:
    """Child of the one shared logger; `name` is usually the calling module's __name__."""
    if not name or name == ROOT_NAME:
        return logging.getLogger(ROOT_NAME)
    if name.startswith(ROOT_NAME + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_NAME}.{name}")


class _UtcFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        dt = datetime.fromtimestamp(record.created, timezone.utc)
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _RunIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id
        return True


class _FallbackMixin:
    """(2026-10-13) If writing to the file sink fails, the record still reaches the console."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        try:
            sys.stderr.write(f"[log file sink failed; console fallback] {self.format(record)}\n")  # type: ignore[attr-defined]
        except Exception:  # last resort: logging must never crash the program
            logging.Handler.handleError(self, record)  # type: ignore[arg-type]


class _SafeTimedHandler(_FallbackMixin, TimedRotatingFileHandler):
    pass


def setup_logging(data_dir: Path | None, verbose: bool = False, run_id: str | None = None) -> str:
    """Configure the shared logger; returns the RUN_ID. Safe to call repeatedly (tests do)."""
    global _run_id
    _run_id = run_id or new_run_id()
    logger = logging.getLogger(ROOT_NAME)
    shutdown_logging()
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    fmt = _UtcFormatter(_FORMAT)
    run_filter = _RunIdFilter()

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    console.addFilter(run_filter)
    logger.addHandler(console)

    if data_dir is not None:
        try:
            log_dir = Path(data_dir) / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            # (2026-10-20, 0.4.4 logfix G) errors.log = WARNING and up (blocked items, denials, takeovers);
            # the name is kept so existing shortcuts still work
            for fname, level in (("app.log", logging.DEBUG), ("errors.log", logging.WARNING)):
                h = _SafeTimedHandler(
                    log_dir / fname, when="midnight", backupCount=14, utc=True, encoding="utf-8"
                )
                h.setLevel(level)
                h.setFormatter(fmt)
                h.addFilter(run_filter)
                logger.addHandler(h)
        except OSError as exc:
            logger.warning("file log sink unavailable (%s); logging to console only", exc)
    return _run_id


def shutdown_logging() -> None:
    """Close and detach every handler (releases the log files; needed on Windows before cleanup)."""
    logger = logging.getLogger(ROOT_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception as exc:  # closing a broken handler must not hide the real outcome
            sys.stderr.write(f"[log] closing handler failed: {exc}\n")

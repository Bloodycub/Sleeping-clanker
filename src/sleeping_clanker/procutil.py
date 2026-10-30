"""Process helpers (2026-10-13): PID liveness and whole-tree kill, Windows-first."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # (2026-10-13) Access denied means the process exists but belongs to someone else.
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# (2026-10-26, runner identity) FILETIME counts 100 ns ticks since 1601-01-01 UTC; this is the tick
# count of the POSIX epoch 1970-01-01 UTC.
_FILETIME_EPOCH_TICKS = 116444736000000000


def process_create_time(pid: int) -> float | None:
    """(2026-10-26, runner identity) The creation time of process `pid` as a POSIX epoch float (UTC
    seconds), read with OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetProcessTimes.

    None when it cannot be read: no such process, access denied, any API failure, or not Windows
    (callers then keep the plain PID check). A PID plus its creation time names one process: Windows
    reuses PIDs, never the pair."""
    if pid <= 0 or sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if ticks <= 0:
            return None
        return (ticks - _FILETIME_EPOCH_TICKS) / 10_000_000
    finally:
        k32.CloseHandle(handle)


def _taskkill() -> str:
    """(2026-10-26, security M2) Always %SystemRoot%\\System32\\taskkill.exe by absolute path: Python
    3.11's shutil.which searched the cwd first, so a taskkill.exe in the run's folder would have run."""
    root = os.environ.get("SystemRoot") or ""
    if not (os.path.isabs(root) and os.path.splitdrive(root)[0]):
        root = r"C:\Windows"
    return str(Path(root) / "System32" / "taskkill.exe")


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and all its descendants; waits briefly for the child to be reaped."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        argv = [_taskkill(), "/PID", str(proc.pid), "/T", "/F"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30, shell=False)
            log.info("taskkill pid=%s exit=%s %s", proc.pid, r.returncode, (r.stdout or r.stderr).strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.error("taskkill failed for pid=%s: %s", proc.pid, exc)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # type: ignore[attr-defined]
        except (OSError, AttributeError) as exc:
            log.error("killpg failed for pid=%s: %s", proc.pid, exc)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        log.error("pid=%s survived tree kill; killing it directly", proc.pid)
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            log.error("pid=%s still alive after kill()", proc.pid)

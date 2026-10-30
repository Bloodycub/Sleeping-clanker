"""Locate the real claude.exe (2026-10-13, spec section 3).

Order: --claude / CLAUDE_AUTOPILOT_CLAUDE, PATH (claude.exe only), ~/.local/bin, the npm global
install. Shell shims (.cmd/.bat/.ps1) are refused because cmd.exe/PowerShell re-parse arguments.
The result is a command *prefix* list so tests can inject e.g. [python, fake_claude.py].

(2026-10-17, 0.4.1) An explicit --claude / env path still wins outright. Otherwise every existing
candidate is asked `--version` and the NEWEST x.y.z wins (ties and unparsable versions keep the order
above). The VS Code extension's bundled CLI
(~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe) is a candidate too:
an npm global install can lag behind the CLI that VS Code itself runs (seen 2026-10-17: the npm global
2.1.278 with only the claude.cmd shim on PATH, while VS Code ran its bundled 2.1.296).

(2026-10-27, security SEC-L9) The `--version` probe runs with version_probe_env(): no secret-named
variable and no TLS/Node switch, and claude.exe's nonessential traffic turned off.
"""

from __future__ import annotations

import os
import re
import shutil  # (2026-10-26, security M2) no which() call left here; tests still patch claude_path.shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Iterator

from .log import get_logger
from .providers import KEY_VARS_MARKER, is_secret_env_name, scrub_child_env

log = get_logger(__name__)

ENV_VAR = "CLAUDE_AUTOPILOT_CLAUDE"
# (2026-10-27, security SEC-L9) the same name as runner.NONESSENTIAL_TRAFFIC_ENV (runner imports this
# module, so it is not imported from there)
NONESSENTIAL_TRAFFIC_ENV = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
_REFUSED_SUFFIXES = (".cmd", ".bat", ".ps1")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
VERSION_TIMEOUT_S = 20.0
VSCODE_GLOB = "anthropic.claude-code-*"


class ClaudePathError(Exception):
    """No usable claude executable, or a refused one (shim / missing file)."""


def check_not_shim(path: str | os.PathLike) -> None:
    if str(path).lower().endswith(_REFUSED_SUFFIXES):
        raise ClaudePathError(
            f"refusing {path}: .cmd/.bat/.ps1 shims re-parse arguments; point at claude.exe"
        )


def which_on_path(name: str, path: str | None = None) -> str | None:
    """(2026-10-26, security M2) shutil.which without the current directory. Python 3.11's which()
    on Windows searches the cwd FIRST, so a claude.exe / git.exe planted in the folder a run starts
    from was found (and claude.exe was even run by the --version probe). Only ABSOLUTE PATH entries
    are searched: `.`, empty, relative and drive-relative (`C:foo`) entries and the cwd itself are
    skipped. `path` defaults to os.environ's PATH; a bare name gets the PATHEXT suffixes on Windows."""
    path = os.environ.get("PATH", "") if path is None else path
    try:
        here = os.path.normcase(os.path.abspath(os.getcwd()))
    except OSError:
        here = None
    names = [name]
    if sys.platform == "win32":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
        if not any(name.lower().endswith(e.lower()) for e in exts):
            names = [name + e for e in exts]
    for entry in path.split(os.pathsep):
        d = entry.strip().strip('"')
        if not d or not os.path.isabs(d) or (sys.platform == "win32" and not os.path.splitdrive(d)[0]):
            continue
        if here is not None and os.path.normcase(os.path.abspath(d)) == here:
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def candidates(explicit: str | None = None, env: dict | None = None) -> list[tuple[str, str]]:
    """(source, path) pairs in priority order; paths may not exist."""
    env = os.environ if env is None else env
    out: list[tuple[str, str]] = []
    if explicit:
        out.append(("--claude", explicit))
    elif env.get(ENV_VAR):
        out.append((ENV_VAR, env[ENV_VAR]))
    # (2026-10-26, security M2) absolute PATH entries only, never the cwd (which_on_path)
    found = which_on_path("claude.exe", env.get("PATH", ""))
    if found:
        out.append(("PATH", found))
    home = env.get("USERPROFILE")
    if home:
        out.append(("USERPROFILE", str(Path(home) / ".local" / "bin" / "claude.exe")))
    appdata = env.get("APPDATA")
    if appdata:
        out.append(
            (
                "APPDATA",
                str(
                    Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code"
                    / "bin" / "claude.exe"
                ),
            )
        )
    if home:
        # (2026-10-17, 0.4.1) the CLI bundled with the VS Code extension (newest dir name first)
        ext = Path(home) / ".vscode" / "extensions"
        try:
            dirs = sorted(ext.glob(VSCODE_GLOB), key=lambda d: d.name, reverse=True)
        except OSError:
            dirs = []
        for d in dirs:
            out.append(("VSCODE", str(d / "resources" / "native-binary" / "claude.exe")))
    return out


def parse_version(text: str | None) -> tuple[int, int, int] | None:
    m = _VERSION.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def version_probe_env(base: dict | None = None) -> dict[str, str]:
    """(2026-10-27, security SEC-L9) The env of a `--version` probe: a copy of `base` (default
    os.environ) without the variables providers.scrub_child_env drops, without any other secret-named
    one either (printing a version needs no login route) or the key-name marker, and with
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 (no auto-update check, telemetry or error report), even
    over an explicit parent value. `base` itself is never changed."""
    env = dict(os.environ if base is None else base)
    scrub_child_env(env)
    for key in [k for k in env if is_secret_env_name(k) or k.upper() == KEY_VARS_MARKER]:
        del env[key]
    env[NONESSENTIAL_TRAFFIC_ENV] = "1"
    return env


def cli_version(path: str) -> tuple[int, int, int] | None:
    """`<path> --version` parsed as x.y.z, or None (a failing or silent binary is never fatal)."""
    try:
        res = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=VERSION_TIMEOUT_S, stdin=subprocess.DEVNULL, env=version_probe_env())
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("claude candidate %s: --version failed: %s", path, exc)
        return None
    return parse_version(res.stdout) or parse_version(res.stderr)


def resolve_claude(explicit: str | None = None, env: dict | None = None,
                   version_of: Callable[[str], tuple[int, int, int] | None] = cli_version) -> list[str]:
    """Return [abs path to claude.exe]. An explicit/env path is binding: it is never skipped.
    Otherwise the newest existing candidate wins (2026-10-17, 0.4.1); `version_of` is injectable."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for source, path in candidates(explicit, env):
        check_not_shim(path)
        if Path(path).is_file():
            resolved = str(Path(path).resolve())
            if source in ("--claude", ENV_VAR):
                log.debug("claude resolved from %s: %s", source, resolved)
                return [resolved]
            if os.path.normcase(resolved) not in seen:
                seen.add(os.path.normcase(resolved))
                found.append((source, resolved))
            continue
        if source in ("--claude", ENV_VAR):
            # (2026-10-13) An explicit choice that does not exist is an error, not a silent fallback.
            raise ClaudePathError(f"{source} points at a missing file: {path}")
        log.debug("claude candidate from %s not found: %s", source, path)
    if not found:
        raise ClaudePathError(
            "claude.exe not found; pass --claude PATH or set " + ENV_VAR
        )
    if len(found) == 1:
        log.debug("claude resolved from %s: %s", *found[0])
        return [found[0][1]]
    # (2026-10-26) probe in parallel: each claude.exe --version takes ~1 s, so 4 candidates in a row
    # held the runner ~4.4 s after Start before the first task (owner: "start took long time")
    with ThreadPoolExecutor(max_workers=min(len(found), 8)) as pool:
        versions = list(pool.map(version_of, [path for _source, path in found]))
    # newest known version first; an unknown version ranks below every known one; ties keep order
    best = max(range(len(found)), key=lambda i: (versions[i] is not None, versions[i] or (0, 0, 0), -i))
    log.info("claude resolved from %s: %s (version %s; candidates: %s)", found[best][0], found[best][1],
             ".".join(map(str, versions[best])) if versions[best] else "?",
             ", ".join(f"{p} = {'.'.join(map(str, v)) if v else '?'}" for (_s, p), v in zip(found, versions)))
    return [found[best][1]]


class ClaudeResolver:
    """(2026-10-19, 0.4.1) The runner's claude.exe for its whole life. A VS Code extension update
    deletes the old bundled CLI folder under a long `run --forever`, so `current()` (called before
    every attempt) re-resolves when the chosen file no longer exists, in the same order; a pinned
    --claude / env path that vanished raises ClaudePathError as at startup. `--version` results are
    cached per path, so the ~3.5 s probe runs at most once per new path."""

    def __init__(self, explicit: str | None = None, env: dict | None = None,
                 version_of: Callable[[str], tuple[int, int, int] | None] = cli_version) -> None:
        self.explicit, self.env = explicit, env
        self._probe = version_of
        self._versions: dict[str, tuple[int, int, int] | None] = {}
        self.cmd = resolve_claude(explicit, env, self.version_of)

    def version_of(self, path: str) -> tuple[int, int, int] | None:
        key = os.path.normcase(path)
        if key not in self._versions:
            self._versions[key] = self._probe(path)
        return self._versions[key]

    def current(self) -> list[str]:
        if Path(self.cmd[0]).is_file():
            return self.cmd
        old = self.cmd[0]
        self._versions.pop(os.path.normcase(old), None)
        self.cmd = resolve_claude(self.explicit, self.env, self.version_of)
        log.warning("claude.exe vanished: %s; switched to %s", old, self.cmd[0])
        return self.cmd

    __call__ = current


# ---- Git Bash for claude.exe hooks (2026-10-20, hotfix; DESIGN 0.5.0 section 0) -------------------
# claude.exe 2.1.304 did not find Git Bash in a non-standard root (e.g. D:\Git), ran the bash-style hooks of
# ~/.claude/settings.json through PowerShell, they failed to parse and 2.1.304 BLOCKS the tool on that
# failure (2.1.296 let it through). Setting CLAUDE_CODE_GIT_BASH_PATH in the child env fixes it (probe p1b).
GIT_BASH_ENV = "CLAUDE_CODE_GIT_BASH_PATH"
_GIT_SUBDIRS = ("cmd", "bin")           # <root>\cmd\git.exe, <root>\bin\git.exe
_GIT_MINGW = ("mingw64", "mingw32")     # <root>\mingw64\bin\git.exe
_DRIVE_FIXED, _DRIVE_REMOVABLE = 3, 2   # GetDriveTypeW; remote/optical drives are never probed


def _bash_of(root: str | os.PathLike) -> str:
    return str(Path(root) / "bin" / "bash.exe")


def is_wsl_bash(path: str) -> bool:
    """True for the WSL launcher (%SystemRoot%\\System32\\bash.exe and its Sysnative/SysWOW64 views)."""
    p = os.path.normcase(os.path.normpath(path)).replace("/", "\\")
    if not p.endswith("\\bash.exe"):
        return False
    parent = p.rsplit("\\", 2)
    return len(parent) == 3 and parent[1] in ("system32", "sysnative", "syswow64")


def git_root_from_exe(git_exe: str) -> str | None:
    """<root> for <root>\\cmd\\git.exe, <root>\\bin\\git.exe or <root>\\mingw64\\bin\\git.exe."""
    parent = Path(git_exe).parent
    if parent.name.lower() not in _GIT_SUBDIRS:
        return None
    root = parent.parent
    if parent.name.lower() == "bin" and root.name.lower() in _GIT_MINGW:
        root = root.parent
    return str(root)


def registry_git_roots() -> list[str]:
    """InstallPath of HKLM / HKCU SOFTWARE\\GitForWindows (64-bit view first); [] off Windows."""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    out: list[str] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"SOFTWARE\GitForWindows", 0, winreg.KEY_READ | view) as key:
                    value, _type = winreg.QueryValueEx(key, "InstallPath")
            except OSError:
                continue
            if isinstance(value, str) and value and value not in out:
                out.append(value)
    return out


def drive_roots() -> list[str]:
    """'X:\\' of every existing fixed/removable drive (no I/O on absent, network or optical drives)."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        mask = k32.GetLogicalDrives()
    except (ImportError, AttributeError, OSError):
        return []
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(ord('A') + i)}:\\"
            if k32.GetDriveTypeW(root) in (_DRIVE_FIXED, _DRIVE_REMOVABLE):
                out.append(root)
    return out


def git_bash_candidates(env: dict | None = None, which: Callable[[str], str | None] = which_on_path,
                        registry: Callable[[], list[str]] = registry_git_roots,
                        drives: Callable[[], list[str]] = drive_roots) -> Iterator[tuple[str, str]]:
    """(source, bash.exe path) in priority order: PATH git, registry, common roots, X:\\Git.
    Lazy: the registry read and the drive scan only run when the earlier sources missed."""
    env = os.environ if env is None else env
    git = which("git")
    if git:
        root = git_root_from_exe(git)
        if root:
            yield "PATH git", _bash_of(root)
    for root in registry():
        yield "registry", _bash_of(root)
    for var, tail in (("ProgramFiles", ("Git",)), ("ProgramFiles(x86)", ("Git",)),
                      ("LOCALAPPDATA", ("Programs", "Git"))):
        base = env.get(var)
        if base:
            yield var, _bash_of(Path(base, *tail))
    for drive in drives():
        yield "drive", _bash_of(Path(drive) / "Git")


def find_git_bash(env: dict | None = None, which: Callable[[str], str | None] = which_on_path,
                  registry: Callable[[], list[str]] = registry_git_roots,
                  drives: Callable[[], list[str]] = drive_roots,
                  exists: Callable[[str], bool] = os.path.isfile) -> tuple[str, str] | None:
    """(source, path) of the first existing Git Bash; the WSL bash.exe is never returned."""
    for source, path in git_bash_candidates(env, which, registry, drives):
        if is_wsl_bash(path):
            continue
        if exists(path):
            return source, path
        log.debug("git bash candidate from %s not found: %s", source, path)
    return None

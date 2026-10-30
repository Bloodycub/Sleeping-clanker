r"""Private copies for "Tasks at once" (2026-10-27, owner "Rakenna rinnakkaisajo" + "fully private git";
binding spec: docs/SPEC_V05_PARALLEL.md sections 2, 3, 4 and 9).

- One private bare git G = <project data dir>\shadow per project, shared by its lanes. The project's own
  .git, index, branches, stash, hooks and config are never written: a snapshot of the project folder P is
  `add -A` with --git-dir=G, --work-tree=P and a separate GIT_INDEX_FILE (G\sc\snap.index); system and
  global git config are never read.
- Each copy item gets one worktree W of G at <wt_base>\<hash8>\<id>. Its path never changes (Claude keeps
  its sessions per cwd). .venv, venv and node_modules are junctions to the project's (ctypes kernel32).
- A finished copy is merged back with `merge-tree --write-tree --merge-base` (base = the copy's snapshot,
  ours = a fresh snapshot of P, theirs = a checkpoint of W). Only the changed files are written into P,
  after a backup, through a journal (planned / staged / committing / committed); bytes stay exact.
- Secrets never enter G: .env, .env.*, *.env and providers.env are excluded by pathspec on every `add`
  (a `!.env` line in the project's own .gitignore cannot bring them back) and by G\info\exclude.
- This is the only module that runs git: one process call site (_run_git), the subcommand allow-list
  GIT_SUBCOMMANDS (no network command), a minimal environment.
- Deletion never follows a reparse point (safe_rmtree) and never uses `worktree remove`: probe P2 deleted
  the project's .venv contents through a junction.
- Lock order: ProjectLock first, then the queue mutex; nothing here is called inside a store transaction.
- (2026-10-27, review) `merge-tree` keeps rename detection on (git 2.43: neither `-X no-renames` nor
  merge.renames=false turns it off), so a conflicted path may be one the task never touched: take-task
  keeps P's file there. Every git call runs with GIT_ALLOW_PROTOCOL=none (no transport at all, whatever
  a planted config says) and G's config, hooks dir and alternates are re-checked under the ProjectLock.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import claude_path, procutil
from . import store as store_mod
from .clock import iso, parse_iso
from .log import get_logger

log = get_logger(__name__)

# ---- constants (spec 11 U1) ------------------------------------------------------------------------
GIT_MIN = (2, 40, 0)
# (2026-10-27) the only git subcommands this app runs; none of them talks to a network
GIT_SUBCOMMANDS = ("init", "config", "add", "rm", "ls-files", "write-tree", "read-tree", "commit-tree",
                   "update-ref", "rev-parse", "cat-file", "diff-tree", "diff", "merge-tree", "rev-list",
                   "worktree", "gc", "version", "check-ref-format")
ALWAYS_EXCLUDE = (".git", ".env", ".env.*", "*.env", "providers.env", ".venv/", "venv/", "node_modules/",
                  "__pycache__/", ".claude/worktrees/")
# (2026-10-27) our own merge temp files: a crash leaves them in P until recovery; never in a snapshot
OWN_EXCLUDE = (".sc-merge-*.tmp",)
DEFAULT_EXCLUDE = ("dist/", "build/", "out/", "target/", ".next/", ".gradle/", ".vs/", "bin/", "obj/", "*.pyc",
                   ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/")
# (2026-10-27) the secret names, also excluded by pathspec on every `add`: G\info\exclude alone loses
# to a negation in the project's .gitignore (probe: `!.env` brought .env back into the index)
SECRET_PATTERNS = (".env", ".env.*", "*.env", "providers.env")
# (2026-10-29, delta L1) + `<name>/**`: the files inside a secret-NAMED folder (.env/, .env.d/, prod.env/) too,
# whatever a negation in the project's .gitignore says
ADD_PATHSPECS = tuple(f":(exclude,glob,icase)**/{p}" for p in SECRET_PATTERNS + OWN_EXCLUDE) \
    + tuple(f":(exclude,glob,icase)**/{p}/**" for p in SECRET_PATTERNS)
LINK_DIRS = (".venv", "venv", "node_modules")
# (2026-10-29, delta M1) tool caches and installs a task leaves in its copy: never "work the merge cannot carry"
# (Shadow.not_merged); everything else the app's own excludes drop from a copy is
QUIET_DROPS = ("__pycache__/", "*.pyc", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".tox/", ".gradle/", ".vs/",
               ".next/", ".claude/worktrees/", *LINK_DIRS)
LINK_SCAN_MAX = 3 * 20000   # (2026-10-29, delta L2) entries Shadow._links_scan lists at most (3 x MAX_FILES)
IGNORE_READ_MAX = 1 << 20   # (2026-10-29, delta L2 fix round 5) a bigger .gitignore (git may skip it) prunes nothing
COPY_FILES = (".claude/settings.local.json", "CLAUDE.local.md")
MERGE_ACTIONS = ("let-claude", "retry", "take-task", "discard", "now", "undo")
QUESTION_ACTIONS = ("let-claude", "retry", "take-task", "discard")
# (2026-10-27, review) + nested-repo: the task made a folder with its own .git; git cannot merge it
CONFLICT_KINDS = ("content", "markers", "link", "case", "branch-switched", "changed-during-merge", "repo-busy",
                  "nested-repo")
MERGE_STATES = (None, "pending", "waiting", "merging", "conflict", "fixing", "held", "merged", "nothing",
                "discarded", "undone")
MERGE_ACTIVE = ("pending", "waiting", "merging")
MERGE_FINAL = ("merged", "nothing", "discarded", "undone")
WAIT_CODES = ("repo-busy", "low-disk", "merge-error", "changed-during-merge")
WT_STATES = ("creating", "active", "removing", "missing", "removed")
RUNNER_GATES = ("git-missing", "git-old", "shadow-broken", "no-root")
GATE_CODES = RUNNER_GATES + ("root-missing", "unc", "too-many-files", "too-big", "slow-listing", "low-disk",
                             "long-path", "nested-repo", "link-in-tree", "cwd-outside", "wt-root-denied",
                             # (2026-10-29, delta H1, C-M1)
                             "cwd-excluded", "wt-root-granted")
ERROR_KINDS = ("busy", "timeout", "lock", "corrupt", "disk", "gate", "stopped", "failed", "unavailable")
MAX_FILES = 20000
MAX_BYTES = 1 << 30
MAX_LIST_S = 60
MAX_PATH = 240
LOCK_WAIT_S = 120
GC_EVERY_S = 7 * 86400
NOTE_MAX = 1500
SLOW_KEEP_S = 86400
MARKER_SCAN_MAX = 5 * 1024 * 1024
MERGE_TRIES = 3
PATHS_MAX = 200
QUESTION_PATHS_MAX = 20
NOT_COPIED_MAX = 10
DISK_MARGIN = 50 * 1024 * 1024
TIMEOUT_S = 60
ADD_TIMEOUT_S = 600
GC_TIMEOUT_S = 900
LOCK_SETTLE_S = 2.0
RMTREE_DELAYS_S = (0.2, 0.5, 1.0, 2.0)
ENV_GIT = "CLAUDE_AUTOPILOT_GIT"
ENV_WORKTREES = "CLAUDE_AUTOPILOT_WORKTREES"
ENV_CRASHPOINT = "CLAUDE_AUTOPILOT_CRASHPOINT"
CRASHPOINTS = ("after-create-record", "after-worktree-add", "after-planned", "mid-stage", "after-staged",
               "mid-commit-1", "after-committed")
SHADOW_DIR = "shadow"
NOHOOKS_DIR = "shadow-nohooks"
HOME_DIR = "shadow-home"
LOCK_FILE = "shadow.lock"
BACKUPS_DIR = "merge_backups"
JOURNAL_DIR = "merge_active"
WORKTREE_JSON = "worktree.json"
# (2026-10-27) info/attributes beats every .gitattributes of the tree: bytes exact, no EOL conversion, no
# filters, no ident, no re-encoding (-working-tree-encoding is on top of the spec's four)
GIT_ATTRIBUTES = "* -text -filter -ident -eol -working-tree-encoding\n"
SHADOW_CONFIG = (("core.autocrlf", "false"), ("core.longpaths", "true"), ("core.symlinks", "false"),
                 ("core.fsmonitor", "false"), ("core.filemode", "false"), ("gc.auto", "0"),
                 ("maintenance.auto", "false"), ("protocol.allow", "never"))
GIT_NAME = "Sleeping Clanker"
GIT_EMAIL = "sleeping-clanker@invalid"
REPARSE_POINT = 0x400   # FILE_ATTRIBUTE_REPARSE_POINT
_DIRECTORY = 0x10       # FILE_ATTRIBUTE_DIRECTORY
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION_RE = re.compile(rb"(\d+)\.(\d+)\.(\d+)")
_OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_P_BUSY_FILES = ("MERGE_HEAD", "rebase-merge", "rebase-apply", "CHERRY_PICK_HEAD", "REVERT_HEAD")
_MARKER_RE = re.compile(rb"^(?:<<<<<<<|>>>>>>>) refs/sc/", re.M)
_BASE_OPTS = ("core.autocrlf=false", "core.longpaths=true", "core.symlinks=false", "core.fsmonitor=false")
_TAIL_OPTS = ("credential.helper=", "safe.directory=*", "protocol.allow=never", "gc.auto=0",
              "maintenance.auto=false", "core.quotepath=false")
# (2026-10-27, review) the only diff form: no external diff program, no textconv filter
DIFF_OPTS = ("--binary", "--no-ext-diff", "--no-textconv")


class WorktreeError(Exception):
    """A copy or merge step failed. `kind` in ERROR_KINDS; `code` names a gate/availability code when the
    failure is one (GATE_CODES); `detail` is for the log."""

    def __init__(self, kind: str, detail: str = "", code: str | None = None, paths: list[str] | None = None) -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
        self.code = code
        self.paths = list(paths or [])   # (2026-10-27, review) the paths a nested-repo / link code names


class GitRefused(WorktreeError):
    """(2026-10-27) A git call outside GIT_SUBCOMMANDS or its argument rules; nothing was started."""

    def __init__(self, detail: str) -> None:
        super().__init__("failed", f"git call refused: {detail}")


class CrashPoint(BaseException):
    """(2026-10-27, spec 10.3) A simulated crash: raised by crashpoint() when CLAUDE_AUTOPILOT_CRASHPOINT
    names the point. A BaseException, so no `except Exception` cleanup runs (as with a real kill)."""


def crashpoint(name: str) -> None:
    if os.environ.get(ENV_CRASHPOINT) == name:
        raise CrashPoint(name)


def _now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def blob_sha(data: bytes) -> str:
    """git's blob id of `data` (sha1 of "blob <n>\\0" + bytes): compares a file with a blob, no git call."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _hash_file(path: str | os.PathLike) -> str | None:
    """blob_sha of a file's bytes; None when it does not exist; "<dir>" for a folder (matches nothing)."""
    try:
        with open(path, "rb") as fh:
            return blob_sha(fh.read())
    except FileNotFoundError:
        return None
    except (IsADirectoryError, PermissionError):
        return "<dir>" if os.path.isdir(path) else "<unreadable>"


# ---- git: the one process call site (spec 9.1) ------------------------------------------------------
def check_git_args(args: list[str]) -> None:
    """Refuse (GitRefused) anything but GIT_SUBCOMMANDS with their argument rules: config only our own
    keys (core./gc./maintenance./protocol.), rm only with --cached, worktree only add|prune, diff only
    `--binary --no-ext-diff --no-textconv <commit> <commit>` (2026-10-27, review: a diff.external or
    textconv planted in G's config never runs), init only --bare/-q/--template=; no argument that moves
    the git dir, the work tree or points git at another program."""
    if not args or not isinstance(args[0], str):
        raise GitRefused("no subcommand")
    sub = args[0]
    if sub not in GIT_SUBCOMMANDS:
        raise GitRefused(f"subcommand {sub!r} is not allowed")
    for a in args[1:]:
        if not isinstance(a, str) or "\0" in a:
            raise GitRefused(f"bad argument {a!r}")
        low = a.lower()
        if low.startswith(("--git-dir", "--work-tree", "--exec-path", "--upload-pack", "--receive-pack",
                           "--namespace", "--config-env", "--super-prefix", "--output")):
            raise GitRefused(f"argument {a!r} is not allowed")
    if sub == "config":
        keys = {k.lower() for k, _v in SHADOW_CONFIG} | {"core.hookspath"}
        if len(args) != 3 or args[1].lower() not in keys \
                or not args[1].lower().startswith(("core.", "gc.", "maintenance.", "protocol.")):
            raise GitRefused(f"config {' '.join(args[1:2])!r} is not allowed")
    elif sub == "rm" and "--cached" not in args[1:]:
        raise GitRefused("rm without --cached")
    elif sub == "worktree" and (len(args) < 2 or args[1] not in ("add", "prune")):
        raise GitRefused(f"worktree {' '.join(args[1:2])!r} is not allowed")
    elif sub == "diff" and not (len(args) == 6 and tuple(args[1:4]) == DIFF_OPTS
                                and not any(a.startswith("-") for a in args[4:])):
        raise GitRefused("diff is only `--binary --no-ext-diff --no-textconv <commit> <commit>`")
    elif sub == "init" and any(a not in ("--bare", "-q", "--quiet", "--template=") for a in args[1:]):
        raise GitRefused("init takes only --bare, -q and --template=")


def git_env(home: str | None, index: str | None = None) -> dict[str, str]:
    """(spec 9.1) The minimal environment of every git call: nothing inherited but SystemRoot, PATH, TEMP
    and TMP; HOME and the global config point at the shadow's own empty files; no system config, no
    prompts, no askpass; a fixed author. No GIT_DIR, GIT_WORK_TREE or HOME of the parent ever leaks in.
    (2026-10-27, review) GIT_ALLOW_PROTOCOL=none beats every protocol.<name>.allow of any config, so a
    promisor remote planted in G's config cannot lazy-fetch; GIT_NO_LAZY_FETCH=1 too (git >= 2.45)."""
    env = {k: os.environ[k] for k in ("SystemRoot", "PATH", "TEMP", "TMP") if os.environ.get(k)}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
                "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_ALLOW_PROTOCOL": "none",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_AUTHOR_NAME": GIT_NAME, "GIT_COMMITTER_NAME": GIT_NAME,
                "GIT_AUTHOR_EMAIL": GIT_EMAIL, "GIT_COMMITTER_EMAIL": GIT_EMAIL})
    if home:
        env["HOME"] = home
        env["GIT_CONFIG_GLOBAL"] = os.path.join(home, "empty.gitconfig")
    if index:
        env["GIT_INDEX_FILE"] = index
    return env


def _run_git(exe: str, git_dir: str | os.PathLike | None, args: list[str], *, hooks_dir: str | None = None,
             home: str | None = None, work_tree: str | os.PathLike | None = None,
             index: str | os.PathLike | None = None, timeout: float = TIMEOUT_S, input: bytes | None = None,
             stop_event: threading.Event | None = None) -> tuple[int, bytes, bytes]:
    """Run one git command; (exit code, stdout bytes, stderr bytes). The only process call in this module:
    argv list, no shell, no window, the minimal env. A timeout or a stop kills the process tree."""
    check_git_args(args)
    argv = [exe]
    for opt in _BASE_OPTS:
        argv += ["-c", opt]
    if hooks_dir:
        argv += ["-c", f"core.hooksPath={hooks_dir}"]
    for opt in _TAIL_OPTS:
        argv += ["-c", opt]
    if git_dir is not None:
        argv += ["--git-dir", str(git_dir)]
    if work_tree is not None:
        argv += ["--work-tree", str(work_tree)]
    argv += list(args)
    env = git_env(home, str(index) if index is not None else None)
    try:
        proc = subprocess.Popen(argv, shell=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except OSError as exc:
        raise WorktreeError("unavailable", f"git could not start ({exc})", code="git-missing") from None
    deadline = time.monotonic() + timeout
    feed = input
    while True:
        try:
            out, err = proc.communicate(input=feed, timeout=0.25)
            return proc.returncode, out or b"", err or b""
        except subprocess.TimeoutExpired:
            feed = None
            stopped = stop_event is not None and stop_event.is_set()
            if stopped or time.monotonic() > deadline:
                procutil.kill_tree(proc)
                kind = "stopped" if stopped else "timeout"
                raise WorktreeError(kind, f"git {args[0]} {kind} after {timeout:.0f}s") from None


_GIT_CACHE: dict[str, str | WorktreeError] = {}


def git_version(exe: str) -> tuple[int, int, int]:
    """The version of a git executable; (0, 0, 0) when it cannot be read."""
    try:
        rc, out, _err = _run_git(exe, None, ["version"], timeout=20)
    except WorktreeError:
        return (0, 0, 0)
    m = _VERSION_RE.search(out) if rc == 0 else None
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)  # type: ignore[return-value]


def _git_candidates(env, which, registry) -> list[str]:
    explicit = env.get(ENV_GIT)
    if explicit:
        # (2026-10-27, review) absolute only (as every other source): a relative value would resolve
        # against the runner's cwd, which can be the project folder (a planted P\git.exe)
        if not os.path.isabs(explicit) or (sys.platform == "win32" and not os.path.splitdrive(explicit)[0]):
            raise WorktreeError("unavailable", f"{ENV_GIT} must be an absolute path", code="git-missing")
        return [explicit]
    out: list[str] = []
    found = which("git.exe")
    if found:
        out.append(found)
    roots = list(registry())
    for var, tail in (("ProgramFiles", ("Git",)), ("LOCALAPPDATA", ("Programs", "Git"))):
        base = env.get(var)
        if base:
            roots.append(os.path.join(base, *tail))
    for root in roots:
        out += [os.path.join(root, "cmd", "git.exe"), os.path.join(root, "mingw64", "bin", "git.exe"),
                os.path.join(root, "bin", "git.exe")]
    return out


def _real_git(path: str) -> str:
    """<root>\\cmd\\git.exe and <root>\\bin\\git.exe are launchers: use <root>\\mingw64\\bin\\git.exe."""
    root = claude_path.git_root_from_exe(path)
    if root:
        real = os.path.join(root, "mingw64", "bin", "git.exe")
        if os.path.isfile(real):
            return real
    return path


def find_git(env: dict | None = None, which=None, registry=None, version=None) -> str:
    """(spec 9.2) The git executable: env CLAUDE_AUTOPILOT_GIT, PATH (never the cwd), the Git for Windows
    registry roots, %ProgramFiles%\\Git, %LOCALAPPDATA%\\Programs\\Git; a launcher maps to
    mingw64\\bin\\git.exe; a .cmd/.bat/.ps1 shim is refused; version >= GIT_MIN. Raises WorktreeError
    kind "unavailable" with code git-missing or git-old. Cached when called with the defaults."""
    default = env is None and which is None and registry is None and version is None
    env = os.environ if env is None else env
    key = env.get(ENV_GIT) or ""
    if default and key in _GIT_CACHE:
        hit = _GIT_CACHE[key]
        if isinstance(hit, WorktreeError):
            raise hit
        return hit
    which = which or claude_path.which_on_path
    registry = registry or claude_path.registry_git_roots
    version = version or git_version
    result: str | WorktreeError = WorktreeError("unavailable", "no git executable found", code="git-missing")
    try:
        cands = _git_candidates(env, which, registry)
    except WorktreeError as exc:
        result, cands = exc, []
    for cand in cands:
        if not cand or not os.path.isfile(cand):
            continue
        try:
            claude_path.check_not_shim(cand)
        except claude_path.ClaudePathError as exc:
            result = WorktreeError("unavailable", str(exc), code="git-missing")
            break
        exe = _real_git(cand)
        ver = version(exe)
        if tuple(ver) < GIT_MIN:
            result = WorktreeError("unavailable", f"git {'.'.join(map(str, ver))} at {exe} is older than "
                                                  f"{'.'.join(map(str, GIT_MIN))}", code="git-old")
        else:
            result = exe
        break
    if default:
        _GIT_CACHE[key] = result
    if isinstance(result, WorktreeError):
        raise result
    return result


def reset_git_cache() -> None:
    _GIT_CACHE.clear()


def wt_base(base_dir: str | os.PathLike) -> Path:
    """(spec 2.4) Where copies live: env CLAUDE_AUTOPILOT_WORKTREES (an absolute local path) or
    "<base>_wt", a sibling of the data dir (outside it, so no deny rule of the data dir matches a copy).
    (2026-10-29, delta C-L2) in its long form (_long): a copy's cwd never keeps an 8.3 short name of a short
    LOCALAPPDATA / CLAUDE_AUTOPILOT_HOME / --data-dir / CLAUDE_AUTOPILOT_WORKTREES (the profiles' 8.3 deny)."""
    v = os.environ.get(ENV_WORKTREES)
    if v and os.path.isabs(v) and not _is_unc(v) and (sys.platform != "win32" or os.path.splitdrive(v)[0]):
        return Path(_long(v))
    return Path(_long(_long(base_dir).rstrip("\\/") + "_wt"))   # the data dir long first: never `LONGNA~1_wt`


def shadow_for(store, stop_event: threading.Event | None = None) -> Shadow | None:
    """The Shadow of a lane store's project; None when the store is not a project store (no-root)."""
    d = store_mod.project_dir_of(store)
    if d is None:
        return None
    root = store_mod.read_project_meta(d).get("root")
    if not isinstance(root, str) or not root:
        return None
    return Shadow(d, root, store_mod.base_dir_of(d), stop_event=stop_event)


# ---- paths -----------------------------------------------------------------------------------------
def _is_unc(path: str | os.PathLike) -> bool:
    s = str(path)
    return s.startswith(("\\\\", "//"))


def _norm(path: str | os.PathLike) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _long(path: str | os.PathLike) -> str:
    """(2026-10-28, F1) `path` in its resolved long form (os.path.realpath: 8.3 short names expanded, links
    followed, no \\\\?\\ prefix); the absolute typed form when that fails, keeps a \\\\?\\ prefix, or names a
    network share the typed form did not (a mapped drive keeps its letter)."""
    text = os.path.abspath(str(path))
    try:
        full = os.path.realpath(text)
    except (OSError, ValueError):
        return text
    if full.startswith(("\\\\?\\", "\\\\.\\")) or (_is_unc(full) and not _is_unc(text)):
        return text
    return full


def _list_form(path: str) -> str:
    """(2026-10-29, delta L2 fix round 6) The folder `path` as os.scandir must get it to list what git walks: a
    Windows path of MAX_PATH chars or more in the extended-length form (\\\\?\\C:\\..., \\\\?\\UNC\\srv\\share\\...).
    Without the LongPathsEnabled setting (the Windows default) Python cannot list a plain path of 260+ chars,
    while git (core.longpaths) walks it. An absolute backslash path is prefixed as is (abspath would drop a
    last name's trailing dot or space); a short path, a \\\\?\\ or \\\\.\\ one, or POSIX: unchanged."""
    if os.name != "nt" or len(path) < MAX_PATH or path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    full = path if os.path.splitdrive(path)[0] and os.path.isabs(path) and "/" not in path \
        else os.path.abspath(path)
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


def _under(a: str, b: str) -> bool:
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def _is_under(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    """True when `path` is `root` or inside it (string compare, case-insensitive on Windows).
    (2026-10-28, F1) Else the resolved long forms are compared too: a root typed by its 8.3 short path
    (a part like ABCDEF~1) holds the long spelling of its folders (integrate round 7)."""
    a, b = _norm(path), _norm(root)
    if _under(a, b):
        return True
    la, lb = _norm(_long(path)), _norm(_long(root))
    return (la, lb) != (a, b) and _under(la, lb)


def _rel_under(path: str | os.PathLike, root: str | os.PathLike) -> str:
    """(2026-10-28, F1) os.path.relpath of `path` under `root` in the spelling _is_under matched them in."""
    a, b = os.path.abspath(str(path)), os.path.abspath(str(root))
    if _under(os.path.normcase(a), os.path.normcase(b)):
        return os.path.relpath(a, b)
    return os.path.relpath(_long(path), _long(root))


def _same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    if _norm(a) == _norm(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _safe_rel(rel: str) -> bool:
    """A git path we may write: relative, no empty/./.. parts, no `:` (alternate streams) or backslash,
    never a .git part."""
    if not rel or rel.startswith("/") or "\\" in rel or ":" in rel:
        return False
    return all(p not in ("", ".", "..") and p.lower() != ".git" for p in rel.split("/"))


def _nested_repos(root: str | os.PathLike, limit: int = PATHS_MAX) -> list[str]:
    """(2026-10-27, review) Folders under `root` (git paths) that hold their own `.git`: a walk that never
    enters a reparse point, a .git folder or LINK_DIRS."""
    out: list[str] = []
    skip = {".git"} | {n.lower() for n in LINK_DIRS}
    for cur, dirs, files in os.walk(str(root)):
        rel = os.path.relpath(cur, str(root)).replace("\\", "/")
        if rel != "." and (".git" in dirs or ".git" in files):
            out.append(rel)
            if len(out) >= limit:
                break
        dirs[:] = [d for d in dirs if d.lower() not in skip and not is_reparse_point(os.path.join(cur, d))]
    return out


def _check_id(item_id) -> str:
    """(2026-10-27, review) An item id we build paths and refs from: [A-Za-z0-9_-]{1,64}, else
    WorktreeError("failed") (`..\\..\\shadow` once made remove() delete G itself)."""
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        raise WorktreeError("failed", f"unsafe item id {item_id!r}")
    return item_id


def _link_on_path(root: str, rel: str) -> bool:
    """True when an existing folder between `root` and the target of `rel` is a reparse point (a write
    there would land outside the root)."""
    cur = root
    for part in rel.split("/")[:-1]:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return False
        except OSError:
            return True
        if getattr(st, "st_file_attributes", 0) & REPARSE_POINT:
            return True
    return False


def _target(root: str, rel: str) -> str:
    return os.path.join(root, *rel.split("/"))


def project_git_dir(root: str) -> str | None:
    """The project's own git dir (read only): P\\.git, or the folder a `.git` file names; None when P is
    not a git repo."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot
    try:
        text = Path(dot).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if text.startswith("gitdir:"):
        g = text[len("gitdir:"):].strip()
        return g if os.path.isabs(g) else os.path.normpath(os.path.join(root, g))
    return None


def project_busy(root: str) -> str | None:
    """(spec 2.7) The name of the file that says P's git is in the middle of a merge, rebase, cherry-pick
    or revert; None otherwise. Read only."""
    g = project_git_dir(root)
    if g is None:
        return None
    for name in _P_BUSY_FILES:
        if os.path.exists(os.path.join(g, name)):
            return name
    return None


def project_branch(root: str) -> str | None:
    """P's branch (from its HEAD as text, read only), a sha when detached, None when not a git repo."""
    g = project_git_dir(root)
    if g is None:
        return None
    try:
        head = Path(g, "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
    return head or None


# ---- junctions and safe deletion (spec 2.9, 9.4) ----------------------------------------------------
def is_reparse_point(path: str | os.PathLike) -> bool:
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def create_junction(link: str | os.PathLike, target: str | os.PathLike) -> None:
    """(spec 9.4) Make `link` (must not exist) a junction (mount point) to the folder `target`: an empty
    folder turned into a reparse point with kernel32 CreateFileW + DeviceIoControl(FSCTL_SET_REPARSE_POINT).
    Mount points need no admin rights. On failure the folder is removed again and OSError is raised."""
    if sys.platform != "win32":
        raise OSError("junctions are Windows only")
    import ctypes
    from ctypes import wintypes

    link, target = os.path.abspath(str(link)), os.path.abspath(str(target))
    sub = "\\??\\" + target
    sub_b, print_b = sub.encode("utf-16-le"), target.encode("utf-16-le")
    path_buf = sub_b + b"\0\0" + print_b + b"\0\0"
    header = struct.pack("<LHHHHHH", 0xA0000003, 8 + len(path_buf), 0, 0, len(sub_b), len(sub_b) + 2,
                         len(print_b))
    data = header + path_buf
    os.mkdir(link)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.DeviceIoControl.restype = wintypes.BOOL
    k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                    wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                    wintypes.LPVOID]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    generic_write, open_existing = 0x40000000, 3
    flags = 0x00200000 | 0x02000000   # FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS
    handle = k32.CreateFileW(link, generic_write, 0, None, open_existing, flags, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        err = ctypes.get_last_error()
        with contextlib.suppress(OSError):
            os.rmdir(link)
        raise OSError(f"cannot open {link} to make a junction (error {err})")
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        returned = wintypes.DWORD(0)
        ok = k32.DeviceIoControl(handle, 0x000900A4, buf, len(data), None, 0, ctypes.byref(returned), None)
        err = ctypes.get_last_error()
    finally:
        k32.CloseHandle(handle)
    if not ok:
        with contextlib.suppress(OSError):
            os.rmdir(link)
        raise OSError(f"cannot make {link} a junction to {target} (error {err})")


def _retry_os(fn, *args) -> None:
    """fn(*args), retried on PermissionError after 0.2/0.5/1/2 s (antivirus, an indexer, an editor)."""
    for delay in (*RMTREE_DELAYS_S, None):
        try:
            fn(*args)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)  # real sleep on purpose: a short file-sharing retry


def _remove_link(path: str, attrs: int) -> None:
    """Remove a reparse point itself (never what it points at)."""
    if attrs & _DIRECTORY:
        _retry_os(os.rmdir, path)
    else:
        _retry_os(os.unlink, path)


def _remove_file(path: str) -> None:
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o666)   # read-only files: clear the flag and retry
    _retry_os(os.unlink, path)


def _rmtree_walk(path: str) -> None:
    with os.scandir(path) as it:
        entries = list(it)
    for entry in entries:
        st = entry.stat(follow_symlinks=False)
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & REPARSE_POINT:
            _remove_link(entry.path, attrs)       # a junction or link: unlink it, NEVER descend
        elif entry.is_dir(follow_symlinks=False):
            _rmtree_walk(entry.path)
            _retry_os(os.rmdir, entry.path)
        else:
            _remove_file(entry.path)


def safe_rmtree(path: str | os.PathLike) -> bool:
    """(spec 2.9) Delete a folder bottom-up without ever following a reparse point: a junction or link is
    removed itself (os.rmdir / os.unlink), its target is never touched. Read-only files are made writable;
    PermissionError is retried after 0.2/0.5/1/2 s. True when the path is gone (or never existed)."""
    p = str(path)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return True
    except OSError as exc:
        log.warning("cannot look at %s for deletion: %s", p, exc)
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    try:
        if attrs & REPARSE_POINT:
            _remove_link(p, attrs)
        elif os.path.isdir(p):
            _rmtree_walk(p)
            _retry_os(os.rmdir, p)
        else:
            _remove_file(p)
    except OSError as exc:
        log.warning("could not delete %s: %s", p, exc)
        return False
    return not os.path.lexists(p)


def _simple_ignores(text: str) -> tuple[list[str], list[str]]:
    """(2026-10-29, delta L2) The folder-pruning part of an ignore file: (names, here) = the name globs that drop
    a folder at any depth below the file, and the `/name` globs that drop one in its own folder only. A file with
    a `!` line gives none (a negation may bring a folder back); a pattern with an inner `/` or a `\\` is not used.
    Matched case-exact (fnmatchcase): a doubt walks a folder more, never less.
    (2026-10-29, delta L2 fix round 5) Lines are read as git reads them (dir.c add_patterns_from_buffer): split on
    LF only with one CR before it dropped, trailing SPACES only trimmed (not a tab, NBSP or form feed), one
    trailing `/` dropped (`build//` drops nothing in git). Only a plain glob is used: a line that is not wholly
    ASCII letters, digits and `_ . * ? / -` or space (a `[...]` class, a backslash, a control or non-ASCII char)
    is skipped, and so is a `/` left after `**/`: what the walk cannot read exactly as git does, it never prunes."""
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")]
    if any(ln.startswith("!") for ln in lines):
        return [], []
    names: list[str] = []
    here: list[str] = []
    for ln in lines:
        p = ln.rstrip(" ")
        if not p or not _PLAIN_GLOB.fullmatch(p):
            continue
        p = p[:-1] if p.endswith("/") else p
        if p.startswith("/"):
            if p[1:] and "/" not in p[1:]:
                here.append(p[1:])
            continue
        while p.startswith("**/"):
            p = p[3:]
        if p and "/" not in p:
            names.append(p)
    return names, here


_PLAIN_GLOB = re.compile(r"[A-Za-z0-9_.*?/ -]+")   # (2026-10-29, delta L2 fix round 5) see _simple_ignores


def _ignored_name(name: str, globs: list[str]) -> bool:
    """(2026-10-29, delta L2 fix round 5) A folder name one of _simple_ignores' globs drops, matched as git's
    wildmatch does: on the UTF-8 bytes (`?` is one byte, so `a?` drops no `a` + e-acute), case-exact. A name
    that is not valid UTF-8 is never dropped."""
    try:
        raw = name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return any(fnmatch.fnmatchcase(raw, p.encode("ascii")) for p in globs)


# ---- deny rules (spec 2.7 wt-root-denied) -----------------------------------------------------------
def _glob_rx(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and pattern.find("]", i + 2) > 0:
            # (2026-10-29, delta C-L2) a `[...]` class is a regex class (the profiles' `*~[0-9]*` 8.3 deny),
            # never literal text; `[!...]` / `[^...]` negate it, and no class matches a `/`
            j = pattern.find("]", i + 2)
            body = pattern[i + 1:j]
            neg = body[:1] in ("!", "^")
            body = body[1:] if neg else body
            chars = "".join("\\" + c if c in "\\^[]" else c for c in body)
            out.append(f"[^/{chars}]" if neg else f"(?!/)[{chars}]")
            i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out), re.I)


def wt_root_denied(path: str | os.PathLike, rules: list[str]) -> bool:
    """True when an absolute Read/Edit/Write deny rule (`~/...` or `//...`, Claude Code's forms) matches
    the copy folder or anything in it: Claude could not work in its own copy. Relative rules (`./...`)
    follow the cwd into the copy and never match it as a whole."""
    probe = os.path.abspath(str(path)).replace("\\", "/").rstrip("/")
    home = os.path.expanduser("~").replace("\\", "/").rstrip("/")
    for rule in rules or []:
        m = re.match(r"^\s*(\w+)\((.*)\)\s*$", str(rule))
        if not m or m.group(1) not in ("Read", "Edit", "Write"):
            continue
        pat = m.group(2).strip()
        if pat.startswith("~/"):
            full = home + pat[1:]
        elif pat.startswith("//"):
            rest = pat[2:]
            full = rest[0] + ":" + rest[1:] if re.match(r"^[A-Za-z]/", rest) else rest
        else:
            continue
        rx = _glob_rx(full)
        if rx.fullmatch(probe) or rx.fullmatch(probe + "/x"):
            return True
    return False


def _deny_rules(profile: str | None) -> list[str]:
    """The --disallowedTools rules of a profile (none for an unknown one)."""
    from . import profiles
    try:
        args = profiles.profile_args(profile or profiles.DEFAULT_PROFILE, None)
    except Exception:  # noqa: BLE001 - an unknown profile has no rules to check here
        return []
    rules: list[str] = []
    for i, a in enumerate(args):
        if a == "--disallowedTools" and i + 1 < len(args):
            rules += [r.strip() for r in re.split(r",(?![^(]*\))", args[i + 1]) if r.strip()]
    return rules


# ---- ProjectLock (spec 2.12) ------------------------------------------------------------------------
class _LockState:
    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.info: dict | None = None   # (2026-10-27, review) what this process wrote into the lock file


_LOCKS: dict[str, _LockState] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_key(project_dir: Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(project_dir))
    except OSError:
        return _norm(project_dir)


class ProjectLock:
    """One lock per project data dir D for every shadow operation: an in-process RLock per D (re-entrant
    on one thread), then an O_EXCL file D\\shadow.lock {pid, create_time, purpose, since, key}. A holder
    store.lock_identity judges dead or reused is stale and taken over; only then are stale git lock files
    of G deleted. Waits at most `timeout` (LOCK_WAIT_S) and raises WorktreeError("busy")."""

    def __init__(self, project_dir: str | os.PathLike, stop_event: threading.Event | None = None) -> None:
        self.project_dir = Path(project_dir)
        self.path = self.project_dir / LOCK_FILE
        self.git_dir = self.project_dir / SHADOW_DIR
        self.stop_event = stop_event
        self.key = _lock_key(self.project_dir)
        with _LOCKS_GUARD:
            self._st = _LOCKS.setdefault(self.key, _LockState())
        self.took_over = False
        self.cleaned: list[str] = []

    def holder(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {}

    def _stale(self, info: dict | None) -> bool:
        if info is None:
            return False
        if not info:
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            return age > LOCK_SETTLE_S
        if info.get("pid") == os.getpid():
            # this process holds the in-process lock for this key, so no thread of ours holds the file
            # under the same key: a leftover of an interrupted step
            return info.get("key") in (None, self.key)
        return not store_mod.lock_identity(info)[0]

    def acquire(self, purpose: str = "work", timeout: float = LOCK_WAIT_S) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self._st.rlock.acquire(timeout=0.2):
            self._check_wait(deadline, purpose)
        if self._st.depth > 0:
            self._st.depth += 1
            return
        info = {"pid": os.getpid(), "create_time": procutil.process_create_time(os.getpid()),
                "purpose": purpose, "since": _now_iso(), "started_at": _now_iso(), "key": self.key}
        try:
            self.project_dir.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    try:
                        os.write(fd, json.dumps(info).encode("utf-8"))
                    finally:
                        os.close(fd)
                    break
                except FileExistsError:
                    old = self.holder()
                    if self._stale(old) and self._take_stale(old):
                        log.warning("taking over a stale project lock %s (holder %s)", self.path, old)
                        self.took_over = True
                        continue
                except PermissionError:
                    pass   # the last holder's delete is pending (Windows): wait
                self._check_wait(deadline, purpose)
                time.sleep(0.1)  # real sleep on purpose: a short lock-file poll
        except BaseException:
            self._st.rlock.release()
            raise
        self._st.depth = 1
        self._st.info = info
        if self.took_over:
            self.cleaned = self.clean_git_locks()

    def _take_stale(self, old: dict | None) -> bool:
        """(2026-10-27, review) Compare-and-delete: move the lock file aside under a unique name, then
        delete it only when it still holds what was judged stale. Another taker that replaced it in
        between gets its lock file back (two takers can no longer both hold the lock). True = try to
        create the lock now."""
        aside = self.path.with_name(f"{LOCK_FILE}.stale-{os.getpid()}-{threading.get_ident()}-{time.time_ns()}")
        try:
            os.rename(self.path, aside)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            data = json.loads(aside.read_text(encoding="utf-8"))
            now = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            now = {}
        if now == (old or {}):
            with contextlib.suppress(OSError):
                aside.unlink()
            return True
        try:
            os.rename(aside, self.path)   # not the stale one: give it back (fails if a new lock exists)
        except OSError as exc:
            log.warning("a project lock changed hands during a stale takeover (%s); %s kept", exc, aside)
        return False

    def _check_wait(self, deadline: float, purpose: str) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise WorktreeError("stopped", f"stopped while waiting for the project lock ({purpose})")
        if time.monotonic() > deadline:
            raise WorktreeError("busy", f"the project lock {self.path} is held by {self.holder()}")

    def release(self) -> None:
        if self._st.depth <= 0:
            return
        self._st.depth -= 1
        if self._st.depth == 0 and self.holder() == self._st.info:
            # (2026-10-27, review) only our own lock file: never one another process holds now
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._st.rlock.release()

    @contextlib.contextmanager
    def hold(self, purpose: str = "work", timeout: float = LOCK_WAIT_S):
        self.acquire(purpose, timeout)
        try:
            yield self
        finally:
            self.release()

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def clean_git_locks(self) -> list[str]:
        """(spec 2.12) After a stale takeover only: delete git lock files our own git left in G (never a
        lock under G\\worktrees\\<id>\\, which Claude's own git in a copy may hold)."""
        g = self.git_dir
        found: list[Path] = []
        for pattern in ("sc/*.lock", "sc/wtidx/*.lock", "refs/sc/**/*.lock"):
            found += list(g.glob(pattern))
        found += [p for p in (g / "packed-refs.lock", g / "index.lock") if p.exists()]
        out = []
        for p in found:
            try:
                p.unlink()
                out.append(str(p))
            except OSError as exc:
                log.warning("could not delete the stale git lock %s: %s", p, exc)
        if out:
            log.warning("deleted %d stale git lock file(s) in %s", len(out), g)
        return out


def _write_json_durable(path: Path, data: dict) -> None:
    """Atomic and fsynced (journals): a unique temp file, fsync, replace with retry."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        store_mod._replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _replace_bytes(path: str | os.PathLike, data: bytes, durable: bool = False, replace_dir: bool = False) -> None:
    """(2026-10-27, review) Write `data` as a NEW directory entry: a unique temp file next to `path`, then
    a replace. A hard link or a file symlink at `path` is replaced itself; the file it shares bytes with
    (maybe a project file) is never written. A link in its place is removed itself first (never
    followed); a real folder only with `replace_dir` (else IsADirectoryError)."""
    path = str(path)
    if is_reparse_point(path):
        safe_rmtree(path)
    elif os.path.isdir(path):
        if not replace_dir:
            raise IsADirectoryError(f"{path} is a folder")
        safe_rmtree(path)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".sc.tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            if durable:
                fh.flush()
                os.fsync(fh.fileno())
        store_mod._replace_with_retry(tmp, Path(path))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


# ---- the shadow ---------------------------------------------------------------------------------------
class Shadow:
    """The private git of one project and the copies of its items (spec 2, 3). Every method that runs git
    or writes takes the ProjectLock; store updates happen inside it (lock order ProjectLock -> queue)."""

    def __init__(self, project_dir: str | os.PathLike, root: str | os.PathLike | None,
                 base_dir: str | os.PathLike, stop_event: threading.Event | None = None) -> None:
        self.project_dir = Path(project_dir)
        # (2026-10-28, F1) the long form (_long): project.json keeps the root as typed (the key never
        # changes), but a typed 8.3 short root made every task "cwd-outside" (its cwd is the long form)
        self.root = _long(store_mod.project_root(root)) if root else None
        # (2026-10-29, F1 fix 2) the typed form, for the system note only (the folder the owner and Claude's
        # own sessions in P use, so its memory slug): realpath also follows a junction/symlink root
        self.typed_root = store_mod.project_root(root) if root else None
        self.base_dir = Path(base_dir)
        self.stop_event = stop_event
        self.git_dir = self.project_dir / SHADOW_DIR
        self.sc_dir = self.git_dir / "sc"
        self.hooks_dir = self.project_dir / NOHOOKS_DIR
        self.home_dir = self.project_dir / HOME_DIR
        self.backups_dir = self.project_dir / BACKUPS_DIR
        self.journal_dir = self.project_dir / JOURNAL_DIR
        self.lock = ProjectLock(self.project_dir, stop_event)
        self.hash8 = self.project_dir.name.rsplit("-", 1)[-1]
        self.wt_root = wt_base(self.base_dir) / self.hash8
        self._exe: str | None = None
        self.last_stats: dict | None = None

    # -- git plumbing ---------------------------------------------------------------------------------
    @property
    def exe(self) -> str:
        if self._exe is None:
            self._exe = find_git()
        return self._exe

    def _git(self, args: list[str], *, work_tree=None, index=None, timeout: float = TIMEOUT_S,
             input: bytes | None = None) -> tuple[int, bytes, bytes]:
        return _run_git(self.exe, self.git_dir, args, hooks_dir=str(self.hooks_dir), home=str(self.home_dir),
                        work_tree=work_tree, index=index, timeout=timeout, input=input,
                        stop_event=self.stop_event)

    def _gitc(self, args: list[str], kind: str = "failed", **kw) -> bytes:
        rc, out, err = self._git(args, **kw)
        if rc != 0:
            raise WorktreeError(kind, f"git {args[0]} exit {rc}: {err.decode('utf-8', 'replace').strip()[:400]}")
        return out

    def _ref(self, ref: str) -> str | None:
        rc, out, _err = self._git(["rev-parse", "--verify", "-q", ref + "^{commit}"])
        return out.decode().strip() if rc == 0 and out.strip() else None

    def _tree(self, rev: str) -> str:
        return self._gitc(["rev-parse", "--verify", "-q", rev + "^{tree}"], kind="corrupt").decode().strip()

    def _set_ref(self, ref: str, sha: str) -> None:
        self._gitc(["update-ref", ref, sha])

    def _del_refs(self, item_id: str) -> None:
        for kind in ("base", "ckpt", "p"):
            self._git(["update-ref", "-d", f"refs/sc/{kind}/{item_id}"])

    def _entry_oid(self, commit: str, path: str) -> str | None:
        rc, out, _err = self._git(["rev-parse", "--verify", "-q", f"{commit}:{path}"])
        return out.decode().strip() if rc == 0 and out.strip() else None

    def _diff_tree(self, a: str, b: str) -> list[dict]:
        """`diff-tree -r -z --no-renames a b`: [{src_mode, dst_mode, src, dst, status, path}]."""
        out = self._gitc(["diff-tree", "-r", "-z", "--no-renames", a, b], kind="corrupt")
        parts = out.split(b"\0")
        res, i = [], 0
        while i + 1 < len(parts):
            head = parts[i].decode("utf-8", "replace")
            if not head.startswith(":"):
                i += 1
                continue
            src_mode, dst_mode, src, dst, status = head[1:].split(" ")[:5]
            res.append({"src_mode": src_mode, "dst_mode": dst_mode, "src": src, "dst": dst,
                        "status": status[:1], "path": parts[i + 1].decode("utf-8")})
            i += 2
        return res

    def _cat_blobs(self, oids: list[str]) -> dict[str, bytes]:
        """Blob contents for oids, one `cat-file --batch`."""
        want = [o for o in dict.fromkeys(oids) if o]
        if not want:
            return {}
        out = self._gitc(["cat-file", "--batch"], kind="corrupt", input=("\n".join(want) + "\n").encode(),
                         timeout=ADD_TIMEOUT_S)
        blobs: dict[str, bytes] = {}
        pos = 0
        for oid in want:
            nl = out.index(b"\n", pos)
            head = out[pos:nl].split()
            pos = nl + 1
            if len(head) < 3 or head[1] == b"missing":
                raise WorktreeError("corrupt", f"object {oid} missing from the shadow")
            size = int(head[2])
            blobs[oid] = out[pos:pos + size]
            pos += size + 1
        return blobs

    def _blob_sizes(self, oids: list[str]) -> dict[str, int]:
        want = [o for o in dict.fromkeys(oids) if o]
        if not want:
            return {}
        out = self._gitc(["cat-file", "--batch-check"], kind="corrupt", input=("\n".join(want) + "\n").encode())
        sizes = {}
        for line in out.decode().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] != "missing":
                sizes[parts[0]] = int(parts[2])
        return sizes

    # -- G itself -------------------------------------------------------------------------------------
    def config_text(self) -> str:
        """(2026-10-27, review) G\\config exactly: what `init --bare` writes here plus SHADOW_CONFIG and
        core.hooksPath. A copy is a linked worktree, so a task's own `git config` in it writes G\\config;
        anything else found there (a remote, a promisor, diff.external, include.path, core.worktree, ...)
        is dropped by _guard_shadow."""
        sections: dict[str, list[tuple[str, str]]] = {"core": [("repositoryformatversion", "0"), ("bare", "true")]}
        if sys.platform == "win32":
            sections["core"].append(("ignorecase", "true"))
        for key, value in SHADOW_CONFIG:
            sec, name = key.split(".", 1)
            sections.setdefault(sec, []).append((name, value))
        sections["core"].append(("hooksPath", '"' + str(self.hooks_dir).replace("\\", "/") + '"'))
        lines: list[str] = []
        for sec, pairs in sections.items():
            lines.append(f"[{sec}]")
            lines += [f"\t{k} = {v}" for k, v in pairs]
        return "\n".join(lines) + "\n"

    def _guard_shadow(self) -> None:
        """(2026-10-27, review) Before our git trusts G (under the ProjectLock): G\\config is restored to
        config_text() by an atomic replace when it differs; files planted in the empty hooks dir are moved
        aside (shadow-nohooks.planted-<stamp>, kept); G\\objects\\info\\alternates (we never make one) is
        refused as corrupt."""
        want = self.config_text().encode("utf-8")
        cfg = self.git_dir / "config"
        try:
            have = cfg.read_bytes()
        except OSError:
            have = None
        if have != want:
            if have is not None:
                log.warning("the private shadow git's config %s was changed (by a task's own git?); restored", cfg)
            _replace_bytes(cfg, want)
        try:
            planted = os.listdir(self.hooks_dir)
        except OSError:
            planted = []
        if planted or is_reparse_point(self.hooks_dir):
            aside = self.project_dir / f"{NOHOOKS_DIR}.planted-{_stamp()}"
            log.warning("files were planted in %s (%s); moved to %s", self.hooks_dir, ", ".join(planted[:5]), aside)
            try:
                os.rename(self.hooks_dir, aside)
            except OSError as exc:
                raise WorktreeError("corrupt", f"cannot move planted files out of {self.hooks_dir}: {exc}") from None
            self.hooks_dir.mkdir(parents=True, exist_ok=True)
        for name in ("alternates", "http-alternates"):
            if os.path.lexists(self.git_dir / "objects" / "info" / name):
                raise WorktreeError("corrupt", f"the private shadow git has objects/info/{name} (never ours); "
                                               "it is not used until that file is removed")

    def _ensure_repo(self) -> None:
        """(spec 2.1) Make G when missing: `init --bare`, our config, info/attributes, the empty hooks dir
        and HOME. Idempotent and cheap once made; every call re-checks G (_guard_shadow)."""
        self.hooks_dir.mkdir(parents=True, exist_ok=True)
        self.home_dir.mkdir(parents=True, exist_ok=True)
        empty = self.home_dir / "empty.gitconfig"
        if not empty.exists():
            empty.write_bytes(b"")
        marker = self.sc_dir / "shadow.json"
        if not (self.git_dir / "HEAD").is_file() or not marker.is_file():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            self._gitc(["init", "--bare", "-q", "--template="])
            _replace_bytes(self.git_dir / "config", self.config_text().encode("utf-8"))
            (self.sc_dir / "wtidx").mkdir(parents=True, exist_ok=True)
            store_mod.write_text_atomic(marker, json.dumps({"v": 1, "created_at": _now_iso()}) + "\n")
        self._guard_shadow()
        info = self.git_dir / "info"
        info.mkdir(parents=True, exist_ok=True)
        attrs = info / "attributes"
        try:
            same = attrs.read_bytes() == GIT_ATTRIBUTES.encode()
        except OSError:
            same = False
        if not same:
            attrs.write_bytes(GIT_ATTRIBUTES.encode())
        (self.sc_dir / "wtidx").mkdir(parents=True, exist_ok=True)

    def exclude_text(self) -> str:
        """(spec 2.3) G\\info\\exclude: the project's own .git\\info\\exclude (read only) first, then
        DEFAULT_EXCLUDE (only without a root .gitignore), the owner's worktree.json patterns (no `!`) and
        ALWAYS_EXCLUDE last, so nothing earlier can negate them. Folder patterns are written without the
        trailing slash so a junction (seen as a link, not a folder) is excluded too."""
        lines = ["# Sleeping Clanker shadow excludes (generated; edit worktree.json in the project data dir)"]
        g = project_git_dir(self.root) if self.root else None
        if g:
            with contextlib.suppress(OSError):
                lines += Path(g, "info", "exclude").read_text(encoding="utf-8", errors="replace").splitlines()
        if self.root and not os.path.isfile(os.path.join(self.root, ".gitignore")):
            lines += list(DEFAULT_EXCLUDE)
        extra = (_read_json(self.project_dir / WORKTREE_JSON) or {}).get("exclude")
        if isinstance(extra, list):
            lines += [p.strip() for p in extra if isinstance(p, str) and p.strip() and not p.strip().startswith("!")
                      and "\n" not in p and "\r" not in p]
        lines += [p.rstrip("/") for p in ALWAYS_EXCLUDE + OWN_EXCLUDE]
        return "\n".join(lines) + "\n"

    def _ensure_excludes(self) -> None:
        text = self.exclude_text()
        gi = b""
        if self.root:
            with contextlib.suppress(OSError):
                gi = Path(self.root, ".gitignore").read_bytes()
        sha = hashlib.sha1(text.encode("utf-8") + b"\0" + gi).hexdigest()
        sha_file = self.sc_dir / "exclude.sha"
        try:
            old = sha_file.read_text(encoding="ascii").strip()
        except OSError:
            old = ""
        if old == sha and (self.git_dir / "info" / "exclude").is_file():
            return
        (self.git_dir / "info").mkdir(parents=True, exist_ok=True)
        (self.git_dir / "info" / "exclude").write_bytes(text.encode("utf-8"))
        # the snapshot index keeps tracking files that are excluded now: start it over
        with contextlib.suppress(FileNotFoundError):
            (self.sc_dir / "snap.index").unlink()
        self.sc_dir.mkdir(parents=True, exist_ok=True)
        sha_file.write_text(sha + "\n", encoding="ascii")

    def _lane_items(self) -> list[tuple[str, Path, list[dict]]]:
        """(lane, dir, items) of every lane of the project, read only."""
        out = []
        for lane, d in store_mod.list_lanes(self.project_dir):
            try:
                items = store_mod.Store(d).load()
            except (store_mod.StoreError, OSError):
                items = []
            out.append((lane, d, items))
        return out

    def _active_copies(self) -> list[str]:
        return [it["id"] for _l, _d, items in self._lane_items() for it in items
                if (it.get("worktree") or {}).get("state") in ("creating", "active", "removing", "missing")]

    def availability(self) -> str | None:
        """(spec 2.7, 2.8) None when copies can be made, else the runner-wide code: no-root, git-missing,
        git-old, shadow-broken. A broken G with no active copy is renamed shadow.broken-<stamp> (kept,
        never deleted) and a fresh one is made later."""
        if not self.root:
            return "no-root"
        try:
            self.exe
        except WorktreeError as exc:
            return exc.code or "git-missing"
        if (self.git_dir / "HEAD").exists():
            rc, _o, _e = self._git(["rev-parse", "--is-bare-repository"])
            if rc != 0:
                with self.lock.hold("repair"):
                    if self._active_copies():
                        return "shadow-broken"
                    broken = self.project_dir / f"shadow.broken-{_stamp()}"
                    log.error("the private shadow git %s is damaged; renamed to %s (kept)", self.git_dir, broken)
                    try:
                        os.replace(self.git_dir, broken)
                    except OSError as exc:
                        log.error("could not rename the damaged shadow: %s", exc)
                        return "shadow-broken"
        return None

    # -- gates --------------------------------------------------------------------------------------
    def _slow_recent(self) -> bool:
        data = _read_json(self.sc_dir / "slow.json") or {}
        at = data.get("at")
        return isinstance(at, (int, float)) and time.time() - at < SLOW_KEEP_S

    def _excluded_names(self) -> set[str]:
        names = {".git"} | {p.rstrip("/").lower() for p in ALWAYS_EXCLUDE if "/" not in p.rstrip("/")
                            and "*" not in p}
        if self.root and not os.path.isfile(os.path.join(self.root, ".gitignore")):
            names |= {p.rstrip("/").lower() for p in DEFAULT_EXCLUDE if "*" not in p}
        return names

    def _top_links(self) -> list[str]:
        """The reparse points of P's non-excluded folders (git paths; empty = none; truthy as before).
        (2026-10-29, delta L2) At ANY depth now (_links_scan), not only the top two levels."""
        return self._links_scan()[0]

    def _links_scan(self) -> tuple[list[str], bool]:
        """(2026-10-29, delta L2) (links, capped): every reparse point (junction, symlink) in P that `add -A`
        could read through, at any depth. A bounded walk: one directory listing per folder (no per-file stat:
        Windows fills st_file_attributes from the listing), never into a reparse point, an _excluded_names
        folder (.git, LINK_DIRS, ...), .claude/worktrees or a folder P's own simple ignore lines drop
        (_simple_ignores of each .gitignore, P's .git\\info\\exclude, worktree.json); capped = it stopped after
        LINK_SCAN_MAX entries or PATHS_MAX links, or (fix round 6) at a folder it could not list that still
        exists (the gate then refuses the copy).
        (2026-10-29, delta L2 fix round 2) A folder holding a `.git` entry is walked like any other (only that
        entry is skipped, by name): git walks it unless the entry is a VALID repo (not an empty or junk .git
        folder, nor a .git file naming a missing gitdir: a stale worktree or submodule), and a valid nested repo
        is refused anyway (nested-repo, snapshot_stats). Cost: a nested repo's files count toward LINK_SCAN_MAX.
        (2026-10-29, delta L2 fix round 1) git lets a LOWER .gitignore's `!` line bring back a folder an upper
        .gitignore dropped, and any .gitignore outranks info/exclude (P's own, worktree.json and the app's
        DEFAULT/ALWAYS_EXCLUDE all live in G\\info\\exclude). So a folder whose .gitignore has a `!` line (any
        name case and a UTF-8 BOM skipped, as git reads it on Windows) drops every inherited prune name and every
        skip name but .git and the secret names (pathspec-excluded on every `add`: no negation brings them back).
        Cost: such a subtree is walked in full (a negated node_modules too), still within LINK_SCAN_MAX."""
        skip = self._excluded_names() | {".claude/worktrees"}   # names, and the one path (a child, never a name)
        hard = {".git"} | {p.lower() for p in SECRET_PATTERNS if "*" not in p}
        top_names: list[str] = []
        top_here: list[str] = []
        g = project_git_dir(self.root) if self.root else None
        extra = (_read_json(self.project_dir / WORKTREE_JSON) or {}).get("exclude")
        texts = ["\n".join(p.strip() for p in extra if isinstance(p, str) and not p.strip().startswith("!")
                           and "\n" not in p and "\r" not in p) if isinstance(extra, list) else ""]
        if g:
            with contextlib.suppress(OSError):   # (fix round 5) the very lines exclude_text writes into G
                texts.append("\n".join(Path(g, "info", "exclude").read_text(encoding="utf-8",
                                                                             errors="replace").splitlines()))
        for text in texts:
            n, h = _simple_ignores(text)
            top_names += n
            top_here += h
        out: list[str] = []
        seen = 0
        stack: list[tuple[str, list[str], set[str]]] = [("", top_names, skip)]
        while stack:
            rel, inherited, skip = stack.pop()
            # (2026-10-29, delta L2 fix round 6) never skip a folder git can still walk: a long path is listed in
            # the \\?\ form (_list_form); a folder gone since its parent was listed (git cannot see it either)
            # is skipped; any other listing error stops the walk as capped (no copy, snapshot() refuses)
            d = _list_form(os.path.join(self.root, *rel.split("/")) if rel else self.root)
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except FileNotFoundError:
                try:
                    os.lstat(d)
                except FileNotFoundError:
                    continue
                except OSError:
                    pass
                return out, True
            except OSError:
                return out, True
            names, here = list(inherited), list(top_here if not rel else [])
            for gi in [e for e in entries if e.name.lower() == ".gitignore" and e.is_file(follow_symlinks=False)]:
                try:   # (fix round 5) the raw bytes as git reads them: a lone CR is no line break
                    big = gi.stat(follow_symlinks=False).st_size > IGNORE_READ_MAX
                    text = "!" if big else Path(gi.path).read_bytes().decode("utf-8-sig", "replace")
                except OSError:
                    continue
                if any(ln.startswith("!") for ln in text.splitlines()):
                    names, here, skip = [], [], hard   # a negation may bring back what ANY upper rule dropped
                elif gi.name == ".gitignore":
                    n, h = _simple_ignores(text)
                    names += n
                    here += h
            for e in entries:
                seen += 1
                if seen > LINK_SCAN_MAX:
                    return out, True
                if e.name.lower() in skip:
                    continue
                child = f"{rel}/{e.name}" if rel else e.name
                try:
                    attrs = getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0)
                except OSError:
                    attrs = 0
                if attrs & REPARSE_POINT or e.is_symlink():
                    out.append(child)
                    if len(out) >= PATHS_MAX:
                        return out, True
                    continue
                if not e.is_dir(follow_symlinks=False) or child.lower() in skip \
                        or _ignored_name(e.name, names + here):
                    continue
                stack.append((child, names, skip))
        return out, False

    def snapshot_stats(self) -> dict:
        """Counts of the last snapshot index: files, bytes (os.stat sizes), the longest path, nested repos
        (160000 entries) and links (120000 entries or reparse points near the top of P)."""
        out = self._gitc(["ls-files", "-s", "-z"], work_tree=self.root, index=self.sc_dir / "snap.index",
                         timeout=ADD_TIMEOUT_S)
        files = nested = links = longest = size = 0
        for rec in out.split(b"\0"):
            if not rec:
                continue
            head, _tab, path_b = rec.partition(b"\t")
            mode = head.split(b" ")[0]
            path = path_b.decode("utf-8", "replace")
            files += 1
            longest = max(longest, len(path))
            if mode == b"160000":
                nested += 1
            elif mode == b"120000":
                links += 1
            else:
                with contextlib.suppress(OSError):
                    size += os.stat(_target(self.root, path)).st_size
        stats = {"files": files, "bytes": size, "longest": longest, "nested": nested,
                 "links": links + (1 if self._top_links() else 0)}
        self.last_stats = stats
        return stats

    def item_gate(self, item: dict, stats: dict | None = None) -> str | None:
        """(spec 2.7) None when the item may get a copy, else a GATE_CODES code (the item then runs
        "alone" in the project folder). `stats` (snapshot_stats) adds the size checks."""
        code = self.availability()
        if code:
            return code
        w = str(self.worktree_path(item))
        if _is_unc(self.root) or _is_unc(w):
            return "unc"
        if not os.path.isdir(self.root):
            return "root-missing"
        if not _is_under(item.get("cwd") or self.root, self.root):
            return "cwd-outside"
        if _is_under(w, self.root) or _is_under(self.root, w) \
                or wt_root_denied(w, _deny_rules(item.get("profile"))):
            return "wt-root-denied"
        if self._granted_over(w, item.get("profile")):
            return "wt-root-granted"
        if self._slow_recent():
            return "slow-listing"
        links, capped = self._links_scan()
        if links:
            # (2026-10-27, review) before any snapshot: `add -A` reads through a junction, so the copy is
            # refused before the target's files (outside P) could reach G; (2026-10-29, delta L2) at any depth
            return "link-in-tree"
        if capped:
            return "too-many-files"   # (2026-10-29, delta L2) too many entries to look for links in
        if stats is None:
            return None
        if stats["files"] > MAX_FILES:
            return "too-many-files"
        if stats["bytes"] > MAX_BYTES:
            return "too-big"
        free = _free_bytes(w)
        if free is not None and free < 2 * stats["bytes"]:
            return "low-disk"
        if len(w) + 1 + stats["longest"] >= MAX_PATH:
            return "long-path"
        if stats["nested"]:
            return "nested-repo"
        if stats["links"]:
            return "link-in-tree"
        return None

    def _granted_over(self, w: str | os.PathLike, profile: str | None) -> bool:
        """(2026-10-29, delta C-M1) True when a user extra folder or Edit/Write allow rule of the Claude settings
        (profiles.user_dirs_ancestors, the same inputs user_dirs_deny reads) holds the copy W but not P: in place
        that folder is write-denied, in the copy it cannot be (an ancestor of the task folder), so the copy
        would have fewer write denies than the same task in place. `full` has no denies at all: never. No
        folder name is logged."""
        from . import profiles
        if (profile or profiles.DEFAULT_PROFILE) == profiles.FULL:
            return False
        try:
            grants = profiles.user_dirs_ancestors(str(w), os.environ)
        except Exception:  # noqa: BLE001 - fail closed: a grant we cannot read could be one
            log.warning("the Claude user settings could not be checked for a copy; the task runs alone")
            return True
        return any(not _is_under(self.root, g) for g in grants)

    def worktree_path(self, item: dict) -> Path:
        rec = item.get("worktree") or {}
        return Path(rec["path"]) if rec.get("path") else self.wt_root / _check_id(item["id"])

    # -- snapshot (spec 2.2) ------------------------------------------------------------------------
    def snapshot(self) -> str:
        """A parentless commit of P's current files (excludes applied); the user's .git is never written.
        (2026-10-27, review) A reparse point near the top of P is never walked (an exclude pathspec), and
        refs/sc/snap keeps the blobs G\\sc\\snap.index caches reachable, so gc never prunes them."""
        with self.lock.hold("snapshot"):
            self._ensure_repo()
            self._ensure_excludes()
            idx = self.sc_dir / "snap.index"
            first = not idx.exists()
            # (2026-10-29, delta L2 fix round 4) a capped walk may have missed a link: never snapshot P then
            # (a merge waits as merge-error, let-claude fails); every link the walk found is left out
            found, capped = self._links_scan()
            if capped:
                raise WorktreeError("busy", "too many entries or links in the project folder to look for links")
            links = [f":(exclude,literal){p}" for p in found]
            t0 = time.monotonic()
            for again in (False, True):
                rc, _out, err = self._git(["add", "-A", "--", ".", *ADD_PATHSPECS, *links], work_tree=self.root,
                                          index=idx, timeout=ADD_TIMEOUT_S)
                if rc != 0:
                    raise WorktreeError("busy", f"snapshot of {self.root} failed (a file locked by another "
                                                f"program?): {err.decode('utf-8', 'replace').strip()[:300]}")
                if first and time.monotonic() - t0 > MAX_LIST_S:
                    store_mod.write_text_atomic(self.sc_dir / "slow.json", json.dumps({"at": time.time()}))
                rc, out, err = self._git(["write-tree"], index=idx)
                if rc == 0:
                    break
                if again:
                    raise WorktreeError("corrupt", f"git write-tree exit {rc}: "
                                                   f"{err.decode('utf-8', 'replace').strip()[:300]}")
                # a blob the index caches is gone (pruned by gc): start the index over once
                log.warning("the snapshot index lost an object (%s); rebuilding it",
                            err.decode("utf-8", "replace").strip()[:200])
                with contextlib.suppress(FileNotFoundError):
                    idx.unlink()
            tree = out.decode().strip()
            sha = self._gitc(["commit-tree", tree, "-m", f"snapshot {_now_iso()}"]).decode().strip()
            self._set_ref("refs/sc/snap", sha)
            return sha

    # -- create / reuse (spec 2.5, 2.6) ----------------------------------------------------------------
    def _load(self, store, item_id: str) -> dict | None:
        return next((i for i in store.load() if i.get("id") == item_id), None)

    def _set_worktree(self, store, item_id: str, rec: dict | None) -> None:
        def fn(it: dict) -> None:
            it["worktree"] = rec
        store.mutate(item_id, fn)

    def _set_wt_state(self, store, item_id: str, state: str) -> None:
        def fn(it: dict) -> None:
            rec = dict(it.get("worktree") or {})
            if rec:
                rec["state"] = state
                it["worktree"] = rec
        with contextlib.suppress(KeyError):
            store.mutate(item_id, fn)

    def _owned_copy(self, w: Path) -> bool:
        """Only a folder under the copies root, or one whose .git file names THIS shadow, is ever deleted
        as a copy; never the project folder or anything around it."""
        if self.root and (_is_under(w, self.root) or _is_under(self.root, w)):
            return False
        if _is_under(w, self.wt_root) and not _same_path(w, self.wt_root):
            return True
        try:
            text = (w / ".git").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return False
        return text.startswith("gitdir:") and _is_under(text[7:].strip(), self.git_dir / "worktrees")

    def _admin(self, item_id: str) -> Path:
        return self.git_dir / "worktrees" / _check_id(item_id)

    def _idx(self, item_id: str) -> Path:
        return self.sc_dir / "wtidx" / f"{_check_id(item_id)}.index"

    def _keep_aside(self, w: Path, why: str | None = "could not be saved as a patch") -> Path | None:
        """(2026-10-27, review) Rename a copy folder whose work could not be saved as a patch to
        <W>.kept-<stamp> next to it: never deleted by us, listed by leftovers() (reason "kept"). None when
        it cannot be moved (a file in it is open). (2026-10-29, delta M1 fix round 4) `why` None: no log line
        here, the caller logs the real reason (the M1 note)."""
        kept = w.with_name(f"{w.name}.kept-{_stamp()}")
        try:
            os.rename(w, kept)
        except OSError as exc:
            log.error("could not move the copy %s aside: %s", w, exc)
            return None
        if why:
            log.warning("the copy %s %s; kept at %s", w, why, kept)
        return kept

    def create(self, store, item_id: str) -> dict:
        """(spec 2.5) A new copy at the item's path (its old one when it had a record). Raises
        WorktreeError (kind gate with .code for a gate); on failure the folder is removed again and the
        record says "removed"."""
        _check_id(item_id)
        with self.lock.hold("create"):
            item = self._load(store, item_id)
            if item is None:
                raise WorktreeError("failed", f"no item {item_id}")
            prev = item.get("worktree") or {}
            if prev.get("state") in ("active", "missing"):
                return self.recreate(store, item_id)   # its work is checkpointed and saved first
            again = prev.get("state") in ("removed", "creating", "removing", "missing") and bool(
                item.get("session_id"))
            return self._create(store, item, int(prev.get("recreated") or 0) + (1 if again else 0))

    def _create(self, store, item: dict, recreated: int, notes: list[str] | None = None) -> dict:
        item_id = item["id"]
        if not _ID_RE.match(item_id):
            raise WorktreeError("failed", f"unsafe item id {item_id!r}")
        code = self.item_gate(item)
        if code:
            raise WorktreeError("gate", f"runs alone: {code}", code=code)
        w = self.worktree_path(item)
        rec = {"state": "creating", "path": str(w), "root": self.root, "created_at": _now_iso(),
               "recreated": recreated}
        self._set_worktree(store, item_id, rec)
        crashpoint("after-create-record")
        try:
            base = self.snapshot()
            stats = self.snapshot_stats()
            code = self.item_gate(item, stats)
            if code:
                raise WorktreeError("gate", f"runs alone: {code}", code=code)
            self._set_ref(f"refs/sc/base/{item_id}", base)
            if os.path.lexists(w):
                if not self._owned_copy(w) or not safe_rmtree(w):
                    raise WorktreeError("busy", f"the old copy folder {w} could not be removed")
            safe_rmtree(self._admin(item_id))
            self._git(["worktree", "prune"])
            with contextlib.suppress(FileNotFoundError):
                (self._idx(item_id)).unlink()
            w.parent.mkdir(parents=True, exist_ok=True)
            self._gitc(["worktree", "add", "--detach", "--lock", str(w), base], timeout=ADD_TIMEOUT_S)
            crashpoint("after-worktree-add")
            if not self._admin(item_id).is_dir():
                raise WorktreeError("failed", f"git named the copy's admin folder differently than {item_id}")
            rel = _rel_under(item.get("cwd") or self.root, self.root)   # (2026-10-28, F1) 8.3 cwd or root
            rel = "." if rel in (".", "") else rel
            if rel != "." and not os.path.isdir(os.path.join(w, rel)) and os.path.isdir(os.path.join(self.root, rel)):
                # (2026-10-29, delta H1) the task's folder is in P but not in the copy (ignored or excluded, or
                # holding nothing a copy keeps): it would work in an empty folder whose output no merge sees
                self._git(["update-ref", "-d", f"refs/sc/base/{item_id}"])
                raise WorktreeError("gate", "runs alone: cwd-excluded", code="cwd-excluded")
            with contextlib.suppress(OSError):
                shutil.copyfile(self._admin(item_id) / "index", self._idx(item_id))
            notes = list(notes or [])
            links = self._make_links(w, notes)
            copied = self._copy_files(w)
            pythonpath, hooks = self._pythonpath(w)
            os.makedirs(os.path.join(w, rel), exist_ok=True)
            rec = {"state": "active", "path": str(w), "root": self.root, "rel": rel, "base": base,
                   "branch": project_branch(self.root), "created_at": rec["created_at"], "recreated": recreated,
                   "links": links, "copied": copied, "pythonpath": pythonpath, "pth_hooks": hooks,
                   "not_copied": self._not_copied(w), "notes": notes}
            rec["system_note"] = system_note(dict(item, worktree=rec), self)
            self._set_worktree(store, item_id, rec)
            log.info("item %s: private copy %s made (%d files)", item_id, w, stats["files"])
            return rec
        except Exception as exc:
            if os.path.lexists(w) and self._owned_copy(w):
                safe_rmtree(w)
            safe_rmtree(self._admin(item_id))
            with contextlib.suppress(WorktreeError):
                self._git(["worktree", "prune"])
            self._set_wt_state(store, item_id, "removed")
            if isinstance(exc, WorktreeError):
                raise
            raise WorktreeError("failed", f"copy for {item_id} failed: {exc}") from exc

    def _make_links(self, w: Path, notes: list[str]) -> list[dict]:
        """(spec 2.5 step 4) Junctions to P's .venv/venv/node_modules (and <d>\\node_modules one level
        down), only to a real folder (not a reparse point) and only where W has nothing yet."""
        pairs = [(name, os.path.join(self.root, name)) for name in LINK_DIRS]
        with contextlib.suppress(OSError), os.scandir(self.root) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and e.name.lower() not in self._excluded_names():
                    pairs.append((f"{e.name}/node_modules", os.path.join(e.path, "node_modules")))
        links = []
        for rel, target in pairs:
            if not os.path.isdir(target) or is_reparse_point(target):
                continue
            link = os.path.join(w, *rel.split("/"))
            if os.path.lexists(link) or not os.path.isdir(os.path.dirname(link)):
                continue
            try:
                create_junction(link, target)
                links.append({"link": rel, "target": target})
            except OSError as exc:
                notes.append(f"not linked: {rel} ({exc})")
                log.warning("could not link %s -> %s: %s", link, target, exc)
        return links

    def _copy_files(self, w: Path) -> list[str]:
        out = []
        for rel in COPY_FILES:
            src, dst = _target(self.root, rel), os.path.join(w, *rel.split("/"))
            if os.path.isfile(src) and not os.path.lexists(dst):
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    out.append(rel)
                except OSError as exc:
                    log.warning("could not copy %s into the copy: %s", rel, exc)
        return out

    def _pythonpath(self, w: Path) -> tuple[str | None, list[str]]:
        """(spec 2.5 step 6, probe P7) Lines of P's venv *.pth files that are absolute paths under P map to
        the same path under W; import-hook lines cannot be mapped and are named."""
        paths: list[str] = []
        hooks: list[str] = []
        for venv in (".venv", "venv"):
            site = Path(self.root, venv, "Lib", "site-packages")
            try:
                pths = sorted(site.glob("*.pth"))
            except OSError:
                continue
            for pth in pths:
                try:
                    lines = pth.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if s.startswith("import"):
                        if pth.name not in hooks:
                            hooks.append(pth.name)
                        continue
                    if os.path.isabs(s) and _is_under(s, self.root):
                        mapped = os.path.join(str(w), _rel_under(s, self.root))   # (2026-10-28, F1)
                        if mapped not in paths:
                            paths.append(mapped)
        return (os.pathsep.join(paths) if paths else None), hooks

    def _not_copied(self, w: Path) -> list[str]:
        try:
            have = {n.lower() for n in os.listdir(w)}
            names = sorted(os.listdir(self.root), key=str.lower)
        except OSError:
            return []
        return [n for n in names if n.lower() not in have and n.lower() != ".git"][:NOT_COPIED_MAX]

    def verify(self, item: dict) -> str:
        """(spec 2.6, 7.3) "ok", "repaired" (W\\.git or the admin gitdir file restored) or "missing" (W or
        G\\worktrees\\<id> gone: recreate at the same path)."""
        rec = item.get("worktree") or {}
        if not rec.get("path"):
            return "missing"
        w, admin = Path(rec["path"]), self._admin(item["id"])
        with self.lock.hold("verify"):
            if not w.is_dir() or not admin.is_dir():
                return "missing"
            repaired = False
            dotgit = w / ".git"
            want = f"gitdir: {str(admin).replace(os.sep, '/')}\n"
            current = None
            if dotgit.is_file():
                with contextlib.suppress(OSError):
                    current = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            ok = bool(current and current.startswith("gitdir:") and _same_path(current[7:].strip(), admin)
                      and not is_reparse_point(dotgit) and os.lstat(dotgit).st_nlink <= 1)
            if not ok:
                # (2026-10-27, review) a new directory entry: a link (symlink, hard link, junction) in its
                # place is replaced itself, the file it points at is never written
                _replace_bytes(dotgit, want.encode("utf-8"), replace_dir=True)
                repaired = True
                log.warning("item %s: restored %s", item["id"], dotgit)
            back = admin / "gitdir"
            try:
                back_ok = _same_path(back.read_text(encoding="utf-8").strip(), dotgit) \
                    and not is_reparse_point(back) and os.lstat(back).st_nlink <= 1
            except OSError:
                back_ok = False
            if not back_ok:
                _replace_bytes(back, (str(dotgit).replace(os.sep, "/") + "\n").encode("utf-8"))
                repaired = True
            return "repaired" if repaired else "ok"

    def recreate(self, store, item_id: str) -> dict:
        """(spec 2.6) A new copy at the SAME path from a new snapshot (recreated + 1); unmerged work in the
        old copy is checkpointed (when its folder is still there) and saved as a patch first."""
        _check_id(item_id)
        with self.lock.hold("recreate"):
            item = self._load(store, item_id)
            if item is None:
                raise WorktreeError("failed", f"no item {item_id}")
            rec = item.get("worktree") or {}
            w = self.worktree_path(item)
            notes: list[str] = []
            if w.is_dir() and not is_reparse_point(w) and self._owned_copy(w):
                # (2026-10-27, review, spec 2.6 "the patch is saved first") the old folder is deleted only
                # after its work is in a patch (or it has none); a failed checkpoint or patch, or a lost
                # base (G deleted), keeps the folder aside instead of deleting it
                try:
                    base = self._ref(f"refs/sc/base/{item_id}")
                    if not base:
                        raise WorktreeError("corrupt", f"no base snapshot for {item_id} in the shadow")
                    ckpt = self.checkpoint(item)
                    done = self.merge_final(item, ckpt)   # (2026-10-29, F1) not an earlier round's merge
                    if not done and self._tree(ckpt) != self._tree(base):
                        self.save_patch(item, "recreated")
                except WorktreeError as exc:
                    log.warning("item %s: its old copy could not be saved as a patch: %s", item_id, exc)
                    kept = self._keep_aside(w)
                    if kept is None:
                        raise WorktreeError("busy", f"the old copy {w} could not be saved or moved aside") from None
                    notes.append(f"the old copy was kept at {kept} (its changes could not be saved as a patch)")
                else:
                    # (2026-10-29, delta M1) files no patch can carry: the old copy is kept aside, never deleted
                    lost, kept_at, stay = self._set_aside_unmerged(item)
                    if stay and kept_at:
                        raise WorktreeError("busy", f"the old copy {w} could not be moved aside")
                    if lost:
                        notes.append(not_merged_text(lost, len(lost), kept_at))
            elif self.unmerged(item):
                self.save_patch(item, "recreated")
            if os.path.lexists(w) and (not self._owned_copy(w) or not safe_rmtree(w)):
                self._set_wt_state(store, item_id, "removing")
                raise WorktreeError("busy", f"the old copy folder {w} could not be removed")
            return self._create(store, item, int(rec.get("recreated") or 0) + 1, notes)

    def ensure_copy(self, store, item_id: str) -> dict:
        """(spec 1.4 step 1) The item's copy, ready to run in: verified and reused, recreated at the same
        path, or created. Raises WorktreeError (see create)."""
        _check_id(item_id)
        with self.lock.hold("prepare"):
            item = self._load(store, item_id)
            if item is None:
                raise WorktreeError("failed", f"no item {item_id}")
            state = (item.get("worktree") or {}).get("state")
            if state == "active":
                if self.verify(item) != "missing":
                    return (self._load(store, item_id) or item)["worktree"]
                self._set_wt_state(store, item_id, "missing")
                return self.recreate(store, item_id)
            if state == "missing":
                return self.recreate(store, item_id)
            return self.create(store, item_id)

    # -- checkpoint, patch, unmerged --------------------------------------------------------------------
    def checkpoint(self, item: dict) -> str:
        """(spec 1.4 step 4, 3.2 step 3) A commit of W's files on top of the copy's base, in our own index
        G\\sc\\wtidx\\<id>.index (Claude's own git use in W never disturbs it); refs/sc/ckpt/<id>."""
        item_id = _check_id(item["id"])
        rec = item.get("worktree") or {}
        w = rec.get("path")
        with self.lock.hold("checkpoint"):
            if not w or not os.path.isdir(w):
                raise WorktreeError("failed", f"the copy folder of {item_id} is missing")
            base = self._ref(f"refs/sc/base/{item_id}") or rec.get("base")
            if not base:
                raise WorktreeError("corrupt", f"no base snapshot for {item_id}")
            self._ensure_repo()
            self._ensure_excludes()
            idx = self._idx(item_id)
            if not idx.exists():
                self._gitc(["read-tree", base], index=idx)
            rc, _o, err = self._git(["add", "-A", "--", ".", *ADD_PATHSPECS], work_tree=w, index=idx,
                                    timeout=ADD_TIMEOUT_S)
            if rc != 0:
                # (2026-10-27, review) a nested git repo without a commit fails `add -A`: name it (a folder
                # holding .git whose path git's message names; the message text itself is never parsed)
                nested = [p for p in _nested_repos(w)
                          if f"'{p}/'".encode("utf-8") in err or f"'{p}'".encode("utf-8") in err]
                if nested:
                    raise WorktreeError("failed", f"the copy holds a nested git repo: {', '.join(nested[:5])}",
                                        code="nested-repo", paths=nested)
                raise WorktreeError("busy", f"checkpoint of {w} failed: {err.decode('utf-8', 'replace')[:300]}")
            tree = self._gitc(["write-tree"], index=idx).decode().strip()
            sha = self._gitc(["commit-tree", tree, "-p", base, "-m", f"checkpoint {_now_iso()}"]).decode().strip()
            self._set_ref(f"refs/sc/ckpt/{item_id}", sha)
            return sha

    def merge_final(self, item: dict, ckpt: str | None = None) -> bool:
        """(2026-10-29, F1 fix 1) merged/nothing is final for the copy's current work: _merge_final (merged
        after this copy was made) and, when the merge recorded the tree it took (merge.tree), the copy's
        checkpoint (`ckpt`, else refs/sc/ckpt/<id>) still has that tree. `merge --now` of a failed/blocked
        item keeps its copy, and a reply re-runs the item in that SAME copy (created_at unchanged): its
        later round is not merged. No checkpoint to compare, or any git error: not final."""
        if not _merge_final(item):
            return False
        want = (item.get("merge") or {}).get("tree")
        if not want:
            return True     # a merge recorded before merge.tree existed: the time rule only
        try:
            ckpt = ckpt or self._ref(f"refs/sc/ckpt/{_check_id(item['id'])}")
            return bool(ckpt) and self._tree(ckpt) == want
        except (WorktreeError, KeyError) as exc:
            log.warning("item %s: cannot tell whether its merge covers its copy (%s); assumed not",
                        item.get("id"), exc)
            return False

    def unmerged(self, item: dict) -> bool:
        """True when the copy's last checkpoint differs from its base and its merge did not end merged or
        nothing (its work exists nowhere else). (2026-10-28, F1; ROOT-CAUSE [U3 -> U1]) merged/nothing
        counts only for this copy (merge_final), and a git error fails closed: True (its work may be there)."""
        if self.merge_final(item):
            return False
        try:
            base = self._ref(f"refs/sc/base/{item['id']}")
            ckpt = self._ref(f"refs/sc/ckpt/{item['id']}")
            return bool(base and ckpt and self._tree(base) != self._tree(ckpt))
        except WorktreeError as exc:
            log.warning("item %s: cannot tell whether its copy holds unmerged work (%s); assumed it does",
                        item.get("id"), exc)
            return True

    def save_patch(self, item: dict, tag: str) -> Path:
        """`diff --binary <base> <checkpoint>` to D\\merge_backups\\<stamp>-<id8>-<tag>\\task.patch."""
        item_id = _check_id(item["id"])
        with self.lock.hold("patch"):
            if (self.git_dir / "HEAD").is_file():
                self._guard_shadow()
            base = self._ref(f"refs/sc/base/{item_id}") or (item.get("worktree") or {}).get("base")
            ckpt = self._ref(f"refs/sc/ckpt/{item_id}")
            if not ckpt and os.path.isdir(str(self.worktree_path(item))):
                ckpt = self.checkpoint(item)
            if not base or not ckpt:
                raise WorktreeError("failed", f"no snapshot to make a patch for {item_id}")
            out = self._gitc(["diff", *DIFF_OPTS, base, ckpt], timeout=ADD_TIMEOUT_S)
            d = self.backups_dir / f"{_stamp()}-{item_id[:8]}-{tag}"
            d.mkdir(parents=True, exist_ok=True)
            p = d / "task.patch"
            p.write_bytes(out)
            log.info("item %s: its changes were saved to %s", item_id, p)
            return p

    def not_merged(self, item: dict) -> list[str]:
        """(2026-10-29, delta M1) The files in the copy W that no merge or patch can carry back: not in the copy's
        checkpoint index because the APP's own excludes dropped them (the secret names and folders,
        DEFAULT_EXCLUDE without a root .gitignore, ALWAYS_EXCLUDE), while P's own ignore rules (each .gitignore
        in W, P's .git\\info\\exclude, worktree.json) keep them; QUIET_DROPS (tool caches, links) never count.
        One `ls-files --others` WITHOUT the standard excludes (G\\info\\exclude is not applied): names only,
        never file contents. Git paths, sorted; [] without a copy folder. A git error raises WorktreeError."""
        item_id = _check_id(item["id"])
        w = (item.get("worktree") or {}).get("path")
        if not w or not os.path.isdir(w) or is_reparse_point(w):
            return []
        with self.lock.hold("not-merged"):
            if not self._idx(item_id).exists():
                self.checkpoint(item)
            args = ["ls-files", "--others", "-z", "--exclude-per-directory=.gitignore"]
            g = project_git_dir(self.root) if self.root else None
            if g and os.path.isfile(os.path.join(g, "info", "exclude")):
                args.append(f"--exclude-from={os.path.join(g, 'info', 'exclude')}")
            extra = (_read_json(self.project_dir / WORKTREE_JSON) or {}).get("exclude")
            owner = [p.strip() for p in extra if isinstance(p, str) and p.strip() and not p.strip().startswith("!")
                     and "\n" not in p and "\r" not in p] if isinstance(extra, list) else []
            args += [f"--exclude={p}" for p in (*QUIET_DROPS, *owner)]
            out = self._gitc(args, work_tree=w, index=self._idx(item_id), timeout=ADD_TIMEOUT_S)
        return sorted({p.decode("utf-8", "replace") for p in out.split(b"\0") if p})

    def _set_aside_unmerged(self, item: dict) -> tuple[list[str], str | None, bool]:
        """(2026-10-29, delta M1) Before a finished copy is deleted: (not_merged paths, the folder it is kept in,
        keep it in place). Files no merge can carry move the copy aside (_keep_aside: never deleted by us,
        leftovers() lists it as "kept"); a copy that cannot be moved stays where it is (True). A failed check
        keeps the copy too (fail closed), with no paths to name."""
        w = self.worktree_path(item)
        err = None
        try:
            lost: list[str] | None = self.not_merged(item)
        except WorktreeError as exc:
            err, lost = exc, None
        if not lost and lost is not None:
            return [], None, False
        # (2026-10-29, delta M1 fix round 4) one log line with the real reason (the M1 note or the failed check),
        # never _keep_aside's "could not be saved as a patch"
        moved = self._keep_aside(w, None) if w.is_dir() and not is_reparse_point(w) and self._owned_copy(w) else None
        kept = str(moved or w)
        if lost:
            log.warning("item %s: %s", item.get("id"), not_merged_text(lost, len(lost), kept))
        else:
            log.warning("item %s: its copy could not be checked for files a merge cannot carry (%s); it is kept "
                        "at %s", item.get("id"), err, kept)
        return lost or [], kept, moved is None

    # -- remove (spec 2.9) ------------------------------------------------------------------------------
    def remove(self, store, item_id: str, item: dict | None = None) -> bool:
        """Delete the copy folder (safe_rmtree: never through a junction, never `worktree remove`), its
        admin folder, refs and index. False (record "removing") when the folder could not be deleted.
        `item`: the item as it was when the queue no longer holds it (remove)."""
        _check_id(item_id)
        with self.lock.hold("remove"):
            it = self._load(store, item_id) or item or {"id": item_id}
            w = self.worktree_path(it)
            if os.path.lexists(w):
                if not self._owned_copy(w):
                    log.error("item %s: %s is not a copy of this project; not deleted", item_id, w)
                    return False
                if not safe_rmtree(w):
                    self._set_wt_state(store, item_id, "removing")
                    return False
            safe_rmtree(self._admin(item_id))
            if (self.git_dir / "HEAD").exists():
                with contextlib.suppress(WorktreeError):
                    self._git(["worktree", "prune"])
                with contextlib.suppress(WorktreeError):
                    self._del_refs(item_id)
            with contextlib.suppress(FileNotFoundError):
                (self._idx(item_id)).unlink()
            self._set_wt_state(store, item_id, "removed")
            return True

    def leftovers(self) -> list[dict]:
        """(spec 2.10) Copy folders without a live record and records stuck removing/missing; only
        reported, never deleted: [{path, reason (orphan|kept|removing|missing), id, lane}]. "kept" = an
        old copy moved aside (<id>.kept-<stamp>) because its work could not be saved as a patch."""
        live: dict[str, tuple[str, dict]] = {}
        for lane, _d, items in self._lane_items():
            for it in items:
                rec = it.get("worktree") or {}
                if rec and rec.get("state") != "removed":
                    live[it["id"]] = (lane, rec)
        out = []
        with contextlib.suppress(OSError), os.scandir(self.wt_root) as it:
            for e in it:
                if e.name not in live:
                    name, kept, _stamp_part = e.name.partition(".kept-")
                    out.append({"path": e.path, "reason": "kept" if kept else "orphan",
                                "id": name if _ID_RE.match(name) else None, "lane": None})
        for item_id, (lane, rec) in live.items():
            if rec.get("state") in ("removing", "missing"):
                out.append({"path": rec.get("path"), "reason": rec["state"], "id": item_id, "lane": lane})
        return out

    def gc_if_due(self, now: float | None = None) -> bool:
        """(spec 2.11) `gc --quiet` (default prune expiry, never --prune=now) at most every GC_EVERY_S, only
        when the ProjectLock is free and no item of any lane of the project is running."""
        now = time.time() if now is None else now
        last = (_read_json(self.sc_dir / "last_gc.json") or {}).get("at")
        if isinstance(last, (int, float)) and now - last < GC_EVERY_S:
            return False
        if not (self.git_dir / "HEAD").exists():
            return False
        if any(it.get("status") == "running" for _l, _d, items in self._lane_items() for it in items):
            return False
        try:
            self.lock.acquire("gc", timeout=0)
        except WorktreeError:
            return False
        try:
            try:
                self._guard_shadow()   # (2026-10-27, review) never a planted gc.* / prune setting
            except WorktreeError as exc:
                log.error("shadow gc skipped: %s", exc)
                return False
            rc, _o, err = self._git(["gc", "--quiet"], timeout=GC_TIMEOUT_S)
            if rc != 0:
                log.warning("shadow gc failed: %s", err.decode("utf-8", "replace")[:300])
            store_mod.write_text_atomic(self.sc_dir / "last_gc.json", json.dumps({"at": now, "rc": rc}))
            return rc == 0
        finally:
            self.lock.release()

    # -- merge (spec 3.2) -----------------------------------------------------------------------------
    def _mutate_merge(self, store, item_id: str, fn) -> dict | None:
        def wrap(it: dict) -> None:
            m = dict(it.get("merge") or {})
            fn(it, m)
            it["merge"] = m
        try:
            return store.mutate(item_id, wrap)
        except KeyError:
            return None

    def merge(self, store, item_id: str, gen: int, *, take_task: bool = False, force: bool = False,
              keep_copy: bool = False) -> str:
        """(spec 3.2) Merge a finished copy into P: "merged", "nothing", "conflict", "waiting", "held", or
        "stale" (the job's gen is old or the item is gone: dropped). Runs under the ProjectLock; a conflict
        writes nothing. `take_task`: conflicted paths take the task's version (3.6). `force`: merge a
        cancelled/held item (`merge --now`). `keep_copy`: the copy stays afterwards (failed/blocked)."""
        _check_id(item_id)
        with self.lock.hold("merge"):
            self.recover_journals(store)
            item = self._load(store, item_id)
            if item is None:
                return "stale"
            m = item.get("merge") or {}
            if m.get("gen") != gen or m.get("state") not in MERGE_ACTIVE:
                return "stale"   # a newer job exists, or this one ended (a question, merged, ...)
            if _read_json(self._jpath(item_id)) is not None:
                # (2026-10-27, review) its last journal is not settled yet: never plan over it
                return self._wait(store, item_id, "changed-during-merge")
            if item.get("status") == "cancelled" and not force:
                self._mutate_merge(store, item_id, lambda it, mm: mm.update(state="held", wait=None))
                log.info("item %s: cancelled before its merge; changes held in its copy", item_id)
                return "held"
            self._mutate_merge(store, item_id, lambda it, mm: mm.update(state="merging", wait=None))
            try:
                return self._merge_locked(store, item, gen, take_task, keep_copy)
            except WorktreeError as exc:
                if (self.journal_dir / f"{item_id}.json").exists():
                    raise   # a journal in committing: recovery settles it
                return self._merge_fail(store, item_id, str(exc))

    def _merge_fail(self, store, item_id: str, detail: str) -> str:
        log.error("item %s: merge error: %s", item_id, detail)
        res = {"r": "waiting"}

        def fn(it, mm):
            mm["tries"] = int(mm.get("tries") or 0) + 1
            if mm["tries"] >= MERGE_TRIES:
                res["r"] = "conflict"
                self._set_conflict(it, mm, "content", [])
            else:
                mm.update(state="waiting", wait="merge-error")
        self._mutate_merge(store, item_id, fn)
        return res["r"]

    def _wait(self, store, item_id: str, code: str) -> str:
        self._mutate_merge(store, item_id, lambda it, mm: mm.update(state="waiting", wait=code))
        log.info("item %s: merge waits (%s)", item_id, code)
        return "waiting"

    def _set_conflict(self, it: dict, mm: dict, kind: str, paths: list[str], args: dict | None = None,
                      backup: str | None = None, preview: str | None = None) -> None:
        mm.update(state="conflict", kind=kind, paths=list(paths)[:PATHS_MAX], wait=None)
        if backup is not None:
            mm["backup"] = backup
        if preview is not None:
            mm["preview"] = preview
        it["merge"] = mm
        it["status"] = "blocked"
        it["finished_at"] = _now_iso()
        it["blocked_question"] = merge_question(it, kind, paths, args)
        log.warning("item %s: running -> blocked (its changes were not merged: %s %s)", it["id"], kind,
                    ", ".join(list(paths)[:5]))

    def _conflict(self, store, item_id: str, kind: str, paths: list[str], args: dict | None = None,
                  backup: str | None = None, preview: str | None = None) -> str:
        self._mutate_merge(store, item_id, lambda it, mm: self._set_conflict(it, mm, kind, paths, args, backup,
                                                                             preview))
        return "conflict"

    def _marker_paths(self, changes: list[dict]) -> list[str]:
        """(spec 3.2 step 5) Paths the task changed whose text still holds OUR conflict markers."""
        cand = [c for c in changes if c["status"] in ("A", "M") and c["dst_mode"].startswith("100")]
        sizes = self._blob_sizes([c["dst"] for c in cand])
        cand = [c for c in cand if sizes.get(c["dst"], MARKER_SCAN_MAX + 1) <= MARKER_SCAN_MAX]
        blobs = self._cat_blobs([c["dst"] for c in cand])
        return [c["path"] for c in cand if b"\0" not in blobs[c["dst"]][:8000]
                and _MARKER_RE.search(blobs[c["dst"]])]

    def _merge_tree(self, item_id: str, base: str) -> tuple[int, str | None, list[str], str]:
        rc, out, err = self._git(["merge-tree", "--write-tree", "-z", "--name-only", "--no-messages",
                                  f"--merge-base={base}", f"refs/sc/p/{item_id}", f"refs/sc/ckpt/{item_id}"],
                                 timeout=ADD_TIMEOUT_S)
        parts = [p.decode("utf-8") for p in out.split(b"\0") if p]
        if rc not in (0, 1) or not parts or not _OID_RE.match(parts[0]):
            return rc, None, [], err.decode("utf-8", "replace").strip()[:400]
        return rc, parts[0], list(dict.fromkeys(parts[1:])), ""

    def _merge_locked(self, store, item: dict, gen: int, take_task: bool, keep_copy: bool) -> str:
        item_id = item["id"]
        rec = item.get("worktree") or {}
        m = item.get("merge") or {}
        result_status = m.get("result_status") or "done"
        busy = project_busy(self.root)
        if busy:
            return self._wait(store, item_id, "repo-busy")
        branch_now = project_branch(self.root)
        if rec.get("branch") and branch_now and branch_now != rec["branch"] and branch_now != m.get("branch_ok"):
            return self._conflict(store, item_id, "branch-switched", [],
                                  {"from": rec["branch"], "to": branch_now})
        if (self.git_dir / "HEAD").is_file():
            self._guard_shadow()
        if rec.get("state") == "active" and self.verify(item) != "missing":
            try:
                theirs = self.checkpoint(item)
            except WorktreeError as exc:
                if exc.code == "nested-repo":   # (2026-10-27, review) a nested repo without a commit
                    return self._conflict(store, item_id, "nested-repo", exc.paths, {"copy": rec.get("path")})
                raise
        else:
            theirs = self._ref(f"refs/sc/ckpt/{item_id}")
            if not theirs:
                return self._merge_fail(store, item_id, "the copy folder and its checkpoint are both gone")
        base = self._ref(f"refs/sc/base/{item_id}") or rec.get("base")
        if not base:
            return self._merge_fail(store, item_id, "no base snapshot")
        theirs_tree = self._tree(theirs)   # (2026-10-29, F1 fix 1) kept as merge.tree when it ends
        if theirs_tree == self._tree(base):
            self._finish(store, item_id, "nothing", result_status, [], None, keep_copy, theirs_tree)
            return "nothing"
        task_changes = self._diff_tree(base, theirs)
        # (2026-10-27, review) a nested repo the task committed in is a gitlink (160000): its files exist
        # only in the copy, so it is a question, never skipped (the copy would be deleted after "merged")
        nested = [c["path"] for c in task_changes if "160000" in (c["src_mode"], c["dst_mode"])]
        if nested:
            return self._conflict(store, item_id, "nested-repo", nested, {"copy": rec.get("path")})
        marked = self._marker_paths(task_changes)
        if marked:
            return self._conflict(store, item_id, "markers", marked)
        # (2026-10-27) git for Windows reads THROUGH a junction in the copy (probe: W\lnk -> a folder
        # outside became lnk\o.txt): a change under or at a link the task made is a "link" conflict
        w = rec.get("path")
        if w and os.path.isdir(w):
            linked = [c["path"] for c in task_changes if _safe_rel(c["path"])
                      and (_link_on_path(w, c["path"]) or is_reparse_point(_target(w, c["path"])))]
            if linked:
                return self._conflict(store, item_id, "link", linked)
        ours = self.snapshot()
        self._set_ref(f"refs/sc/p/{item_id}", ours)
        rc, result, conflicted, err = self._merge_tree(item_id, base)
        if result is None:
            return self._merge_fail(store, item_id, f"merge-tree exit {rc}: {err}")
        if conflicted and not take_task:
            bdir = self._backup_dir(item_id)
            self._write_backup(bdir, item_id, base, ours, theirs, result, [], conflicted)
            preview = self._write_preview(bdir, result, conflicted)
            return self._conflict(store, item_id, "content", conflicted, backup=str(bdir), preview=preview)
        changes = self._diff_tree(ours, result)
        if take_task and conflicted:
            theirs_by = {c["path"]: c for c in task_changes}
            changes = [c for c in changes if c["path"] not in conflicted]
            for path in conflicted:
                t = theirs_by.get(path)
                if t is None:
                    # (2026-10-27, review) the task never changed this path (a rename/delete conflict: merge-tree
                    # detects renames): P's file stays; never a delete of the owner's newest file
                    continue
                ours_oid = self._entry_oid(ours, path)
                dst = None if t is None or t["status"] == "D" else t["dst"]
                if dst == ours_oid:
                    continue
                changes.append({"src_mode": "100644" if ours_oid else "000000",
                                "dst_mode": t["dst_mode"] if t and dst else "000000",
                                "src": ours_oid or "0" * 40, "dst": dst or "0" * 40,
                                "status": "D" if dst is None else ("A" if ours_oid is None else "M"), "path": path})
        plan, links = [], []
        for c in changes:
            if "160000" in (c["src_mode"], c["dst_mode"]):
                continue
            if c["status"] == "T" or "120000" in (c["src_mode"], c["dst_mode"]) or not _safe_rel(c["path"]) \
                    or _link_on_path(self.root, c["path"]):
                links.append(c["path"])
                continue
            if c["status"] == "D":
                plan.append({"path": c["path"], "op": "delete", "before": c["src"], "after": None, "src": None})
            else:
                plan.append({"path": c["path"], "op": "write", "before": None if c["status"] == "A" else c["src"],
                             "after": c["dst"], "src": c["dst"]})
        if links:
            return self._conflict(store, item_id, "link", links)
        # (2026-10-27, review) a file that becomes a folder (or the reverse) cannot be staged next to the
        # old one: a "link" conflict (a file-type change), not three merge errors
        def parents(path: str) -> list[str]:
            parts = path.split("/")
            return ["/".join(parts[:i]) for i in range(1, len(parts))]
        deleted = {p["path"] for p in plan if p["op"] == "delete"}
        deleted_dirs = {d for p in deleted for d in parents(p)}
        swapped = sorted(p["path"] for p in plan if p["op"] == "write"
                         and (p["path"] in deleted_dirs or any(d in deleted for d in parents(p["path"]))
                              or os.path.isdir(_target(self.root, p["path"]))))
        if swapped:
            return self._conflict(store, item_id, "link", swapped)
        adds = {p["path"].lower(): p["path"] for p in plan if p["before"] is None}
        dels = {p["path"].lower(): p["path"] for p in plan if p["op"] == "delete"}
        case = sorted({adds[k] for k in adds if k in dels and adds[k] != dels[k]} |
                      {dels[k] for k in dels if k in adds and adds[k] != dels[k]})
        if case:
            return self._conflict(store, item_id, "case", case)
        if not plan:
            self._finish(store, item_id, "merged", result_status, [], None, keep_copy, theirs_tree)
            return "merged"
        need = sum(self._blob_sizes([p["after"] for p in plan if p["after"]]).values())
        free = _free_bytes(self.root)
        if free is not None and free < 2 * need + DISK_MARGIN:
            return self._wait(store, item_id, "low-disk")
        bdir = self._backup_dir(item_id)
        changed_now = self._write_backup(bdir, item_id, base, ours, theirs, result, plan, [])
        if changed_now:
            return self._changed_during(store, item_id, changed_now)
        j = {"v": 1, "kind": "merge", "item": item_id, "store": str(store.data_dir), "gen": gen, "state": "planned",
             "dirs_made": [], "backup": str(bdir), "result_status": result_status, "keep_copy": keep_copy,
             "tree": theirs_tree,
             "files": [dict(p, tmp=f".sc-merge-{item_id[:8]}-{gen}-{n}.tmp") for n, p in enumerate(plan)]}
        return self._apply_journal(store, j)

    def _changed_during(self, store, item_id: str, paths: list[str], code: str = "changed-during-merge") -> str:
        """A target changed (or, code repo-busy, is read-only or locked) since the plan: nothing written,
        waiting; the MERGE_TRIES-th time a conflict of that kind."""
        res = {"r": "waiting"}

        def fn(it, mm):
            mm["tries"] = int(mm.get("tries") or 0) + 1
            if mm["tries"] >= MERGE_TRIES:
                res["r"] = "conflict"
                self._set_conflict(it, mm, code, paths)
            else:
                mm.update(state="waiting", wait=code)
        self._mutate_merge(store, item_id, fn)
        log.warning("item %s: merge waits (%s): %s", item_id, code, ", ".join(paths[:5]))
        return res["r"]

    def _backup_dir(self, item_id: str, tag: str | None = None) -> Path:
        d = self.backups_dir / (f"{_stamp()}-{item_id[:8]}" + (f"-{tag}" if tag else ""))
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_backup(self, bdir: Path, item_id: str, base: str, ours: str, theirs: str, result: str,
                      plan: list[dict], conflicted: list[str]) -> list[str]:
        """(spec 3.2 step 10) ours\\<path> = P's current bytes of every file to be changed or deleted (each
        checked against ours' blob), task.patch, merge.json. Returns the paths whose bytes no longer match
        (changed during the merge)."""
        bad = []
        for p in plan + [{"path": c, "before": "?"} for c in conflicted]:
            if p.get("before") is None:
                continue
            src = _target(self.root, p["path"])
            try:
                data = Path(src).read_bytes()
            except OSError:
                if p["before"] != "?":
                    bad.append(p["path"])
                continue
            if p["before"] != "?" and blob_sha(data) != p["before"]:
                bad.append(p["path"])
                continue
            dst = bdir / "ours" / Path(*p["path"].split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            _replace_bytes(dst, data, durable=True)   # (2026-10-27, review) fsynced before "committing"
        with contextlib.suppress(WorktreeError):
            (bdir / "task.patch").write_bytes(self._gitc(["diff", *DIFF_OPTS, base, theirs], timeout=ADD_TIMEOUT_S))
        info = {"item": item_id, "base": base, "ours": ours, "theirs": theirs, "result": result,
                "changes": [{k: p[k] for k in ("path", "op", "before", "after")} for p in plan],
                "conflicted": conflicted, "at": _now_iso()}
        (bdir / "merge.json").write_text(json.dumps(info, indent=1, ensure_ascii=False), encoding="utf-8")
        return bad

    def _write_preview(self, bdir: Path, result: str, paths: list[str]) -> str:
        """(spec 3.5) The result blobs (with markers) of the conflicted text paths, in D only."""
        pdir = bdir / "preview"
        oids = {p: self._entry_oid(result, p) for p in paths if _safe_rel(p)}
        blobs = self._cat_blobs([o for o in oids.values() if o])
        for path, oid in oids.items():
            data = blobs.get(oid) if oid else None
            if data is None or b"\0" in data[:8000]:
                continue
            dst = pdir / Path(*path.split("/"))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
        pdir.mkdir(parents=True, exist_ok=True)
        return str(pdir)

    # -- the journal (spec 3.2 step 11, 3.4) -----------------------------------------------------------
    def _jpath(self, item_id: str) -> Path:
        return self.journal_dir / f"{_check_id(item_id)}.json"

    def _tmp(self, f: dict) -> str:
        return os.path.join(os.path.dirname(_target(self.root, f["path"])), f["tmp"])

    def _after_bytes(self, files: list[dict]) -> dict[str, bytes]:
        """The new bytes of each write: a blob of G (merge) or a backup file (undo)."""
        out: dict[str, bytes] = {}
        git = [f["src"] for f in files if f["op"] == "write" and not str(f["src"]).startswith("file:")]
        blobs = self._cat_blobs(git)
        for f in files:
            if f["op"] != "write":
                continue
            src = str(f["src"])
            data = Path(src[5:]).read_bytes() if src.startswith("file:") else blobs[src]
            if blob_sha(data) != f["after"]:
                raise WorktreeError("corrupt", f"the new bytes of {f['path']} do not match their id")
            out[f["path"]] = data
        return out

    def _stage(self, j: dict, persist=None) -> None:
        """Every write's new bytes to its tmp name (fsynced). (2026-10-27, review) Each folder made is
        recorded first, `persist()` writes the journal durably, then os.mkdir: a crash mid-stage leaves no
        empty folder in P that recovery does not know about."""
        data = self._after_bytes(j["files"])
        for f in j["files"]:
            if f["op"] != "write":
                continue
            parent = os.path.dirname(_target(self.root, f["path"]))
            missing = []
            cur = parent
            while not os.path.isdir(cur) and _is_under(cur, self.root) and not _same_path(cur, self.root):
                missing.append(cur)
                cur = os.path.dirname(cur)
            for d in reversed(missing):
                j["dirs_made"].append(d)
                if persist is not None:
                    persist()
                os.mkdir(d)
                crashpoint("mid-stage")
            with open(self._tmp(f), "wb") as fh:
                fh.write(data[f["path"]])
                fh.flush()
                os.fsync(fh.fileno())

    def _back_tmp(self, f: dict) -> str:
        return os.path.join(os.path.dirname(_target(self.root, f["path"])), f["tmp"][:-4] + "-back.tmp")

    def _discard_temps(self, j: dict) -> None:
        for f in j.get("files") or []:
            for tmp in (self._tmp(f), self._back_tmp(f)):   # (2026-10-27, review) + rollback's temps
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
        for d in reversed(j.get("dirs_made") or []):
            with contextlib.suppress(OSError):
                os.rmdir(d)   # only when empty

    def _check(self, j: dict) -> tuple[list[str], list[str]]:
        """Every target still hashes to `before` (None = must not exist); read-only targets are busy."""
        changed, locked = [], []
        for f in j["files"]:
            t = _target(self.root, f["path"])
            if _hash_file(t) != f["before"]:
                changed.append(f["path"])
            elif f["before"] is not None and not os.access(t, os.W_OK):
                locked.append(f["path"])
        return changed, locked

    def _commit_files(self, j: dict) -> list[str]:
        """Deletes first, then writes; each re-checked right before. Returns the first path that no longer
        matches (nothing after it was touched), or []."""
        first = True
        order = [f for f in j["files"] if f["op"] == "delete"] + [f for f in j["files"] if f["op"] == "write"]
        for f in order:
            t = _target(self.root, f["path"])
            cur = _hash_file(t)
            if cur == f["after"]:
                continue   # done already (recovery)
            if cur != f["before"]:
                return [f["path"]]
            try:
                if f["op"] == "delete":
                    os.remove(t)
                else:
                    store_mod._replace_with_retry(self._tmp(f), Path(t))
            except OSError as exc:
                log.warning("could not write %s: %s", t, exc)
                return [f["path"]]
            if first:
                first = False
                crashpoint("mid-commit-1")
        return []

    def _old_bytes(self, bdir: Path, f: dict) -> bytes | None:
        """(2026-10-27, review) The bytes `before` names: the backup copy only when it hashes to it (a
        power loss can truncate it), else the blob from G; None when neither has them."""
        with contextlib.suppress(OSError):
            data = (bdir / "ours" / Path(*f["path"].split("/"))).read_bytes()
            if blob_sha(data) == f["before"]:
                return data
        with contextlib.suppress(WorktreeError, OSError, ValueError):
            data = self._cat_blobs([f["before"]]).get(f["before"])
            if data is not None and blob_sha(data) == f["before"]:
                return data
        return None

    def _rollback(self, j: dict) -> tuple[list[str], list[str]]:
        """Every file that now holds the new bytes gets its old bytes back (backup ours\\ copies; files the
        merge created are deleted); temps and the folders it made (when empty) go. (2026-10-27, review)
        Never wrong bytes: a file whose old bytes exist nowhere keeps the new ones (`lost`, logged); a file
        that cannot be written now (`failed`) keeps the journal for a later pass. Returns (lost, failed)."""
        bdir = Path(j["backup"])
        lost, failed = [], []
        for f in j["files"]:
            t = _target(self.root, f["path"])
            if _hash_file(t) != f["after"]:
                continue
            try:
                if f["before"] is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(t)
                    continue
                old = self._old_bytes(bdir, f)
                if old is None:
                    log.error("item %s: the old bytes of %s are in neither the backup nor the shadow; it keeps "
                              "the merged bytes", j.get("item"), f["path"])
                    lost.append(f["path"])
                    continue
                tmp = self._back_tmp(f)
                with open(tmp, "wb") as fh:
                    fh.write(old)
                    fh.flush()
                    os.fsync(fh.fileno())
                store_mod._replace_with_retry(tmp, Path(t))
            except OSError as exc:
                log.error("item %s: could not roll %s back yet: %s", j.get("item"), f["path"], exc)
                failed.append(f["path"])
        if not failed:
            self._discard_temps(j)
        return lost, failed

    def _apply_journal(self, store, j: dict) -> str:
        jpath = self._jpath(j["item"])
        _write_json_durable(jpath, j)
        crashpoint("after-planned")
        try:
            self._stage(j, persist=lambda: _write_json_durable(jpath, j))
        except (OSError, WorktreeError) as exc:
            self._discard_temps(j)
            jpath.unlink()
            if isinstance(exc, WorktreeError):
                raise
            raise WorktreeError("disk", f"could not stage the merge: {exc}") from exc
        j["state"] = "staged"
        _write_json_durable(jpath, j)
        crashpoint("after-staged")
        changed, locked = self._check(j)
        if changed or locked:
            self._discard_temps(j)
            jpath.unlink()
            if j["kind"] == "undo":
                return "refused"
            if changed:
                return self._changed_during(store, j["item"], changed)
            return self._changed_during(store, j["item"], locked, code="repo-busy")
        j["state"] = "committing"
        _write_json_durable(jpath, j)
        bad = self._commit_files(j)
        if bad:
            return self._settle_back(store, j, bad)
        j["state"] = "committed"
        _write_json_durable(jpath, j)
        crashpoint("after-committed")
        return self._phase3(store, j)

    def _settle_back(self, store, j: dict, bad: list[str]) -> str:
        lost, failed = self._rollback(j)
        if failed:
            # (2026-10-27, review) the journal stays for the next recovery pass; merge() waits for it
            if j["kind"] == "merge":
                self._mutate_merge(store, j["item"], lambda it, mm: mm.update(state="waiting",
                                                                              wait="changed-during-merge"))
            return "refused" if j["kind"] == "undo" else "waiting"
        self._jpath(j["item"]).unlink(missing_ok=True)
        if j["kind"] == "undo":
            log.warning("item %s: undo taken back; %s changed meanwhile", j["item"], ", ".join(bad))
            return "refused"
        self._conflict(store, j["item"], "changed-during-merge", list(dict.fromkeys(bad + lost)),
                       backup=j.get("backup"))
        return "conflict"

    def _phase3(self, store, j: dict) -> str:
        """(spec 3.3) The store update after the files are in place, then the copy goes; the journal is
        deleted last (re-running this is harmless)."""
        changed = [f["path"] for f in j["files"]]
        if j["kind"] == "undo":
            self._mutate_merge(store, j["item"], lambda it, mm: mm.update(state="undone", undone_at=_now_iso()))
            self._jpath(j["item"]).unlink(missing_ok=True)
            return "undone"
        self._finish(store, j["item"], "merged", j.get("result_status") or "done", changed, j.get("backup"),
                     bool(j.get("keep_copy")), j.get("tree") if isinstance(j.get("tree"), str) else None)
        self._jpath(j["item"]).unlink(missing_ok=True)
        return "merged"

    def _commit_subjects(self, item: dict) -> list[str]:
        """(spec 3.3) Subjects of commits Claude made in W (for the owner to see; never merged as commits)."""
        admin = self._admin(item["id"])
        try:
            head = (admin / "HEAD").read_text(encoding="utf-8").strip()
        except OSError:
            return []
        if head.startswith("ref:"):
            head = self._ref(head[4:].strip()) or ""
        base = (item.get("worktree") or {}).get("base")
        if not _OID_RE.match(head or "") or not base or head == base:
            return []
        rc, out, _e = self._git(["rev-list", "--max-count=10", "--format=%s", f"{base}..{head}"])
        if rc != 0:
            return []
        return [ln for ln in out.decode("utf-8", "replace").splitlines() if ln and not ln.startswith("commit ")]

    def _finish(self, store, item_id: str, state: str, result_status: str, changed: list[str],
                backup: str | None, keep_copy: bool, tree: str | None = None) -> None:
        """(2026-10-29, F1 fix 1) `tree`: the tree of the checkpoint this merge took (merge.tree), so a later
        round in the SAME kept copy (keep_copy) is not mistaken for merged work (Shadow.merge_final)."""
        item = self._load(store, item_id)
        commits = self._commit_subjects(item) if item else []
        lost: list[str] = []
        kept = None
        if not keep_copy and item:
            # (2026-10-29, delta M1; DECISION "done + note") files the app's excludes kept out of the merge
            lost, kept, keep_copy = self._set_aside_unmerged(item)

        def fn(it, mm):
            mm.update(state=state, changed=list(changed)[:PATHS_MAX], backup=backup, commits=commits, kind=None,
                      paths=None, wait=None, tries=0, at=_now_iso(), tree=tree)
            for k in ("not_merged", "not_merged_n", "kept"):
                mm.pop(k, None)
            if lost:
                mm.update(not_merged=lost[:PATHS_MAX], not_merged_n=len(lost), kept=kept)
            if it.get("status") != "cancelled":
                it["status"] = result_status
            q = it.get("blocked_question")
            if isinstance(q, dict) and q.get("kind") == "merge":
                it["blocked_question"] = None
        self._mutate_merge(store, item_id, fn)
        log.info("item %s: merge %s (%d file(s))", item_id, state, len(changed))
        if not keep_copy:
            self.remove(store, item_id)

    def recover_journals(self, store) -> int:
        """(spec 3.4) Settle every journal of the project left by a crash: planned/staged -> temps deleted,
        merge pending again; committing -> roll forward when every file is before or after, else roll
        back and block (changed-during-merge); committed -> phase 3. Returns how many were settled.
        (2026-10-27, review) Every lane's journals, not only this one's (all of it runs under the one
        ProjectLock, and a crashed lane's runner may never come back): the item update goes to the
        journal's own lane store. A journal that cannot be settled yet is kept for the next pass."""
        if not self.journal_dir.is_dir():
            return 0
        n = 0
        with self.lock.hold("recover"):
            lanes = {_norm(d): d for _l, d in store_mod.list_lanes(self.project_dir)}
            for jp in sorted(self.journal_dir.glob("*.json")):
                j = _read_json(jp)
                if not j or not isinstance(j.get("files"), list):
                    log.error("unreadable merge journal %s; left for the owner", jp)
                    continue
                st = store
                lane_dir = lanes.get(_norm(j["store"])) if isinstance(j.get("store"), str) else None
                if lane_dir is not None and _norm(lane_dir) != _norm(store.data_dir):
                    st = store_mod.Store(lane_dir)
                n += 1
                state = j.get("state")
                try:
                    if state in ("planned", "staged"):
                        self._discard_temps(j)
                        jp.unlink(missing_ok=True)
                        if j.get("kind") == "merge":
                            self._mutate_merge(st, j["item"], lambda it, mm: mm.update(state="pending", wait=None)
                                               if mm.get("state") in ("merging", "pending", "waiting") else None)
                    elif state == "committing":
                        self._settle_committing(st, j)
                    elif state == "committed":
                        self._phase3(st, j)
                except (OSError, WorktreeError) as exc:
                    log.error("item %s: its merge journal %s could not be settled yet (kept): %s", j.get("item"),
                              jp, exc)
                    continue
                log.warning("item %s: recovered a merge journal left in state %s", j.get("item"), state)
        return n

    def _settle_committing(self, store, j: dict) -> str:
        odd = [f["path"] for f in j["files"]
               if _hash_file(_target(self.root, f["path"])) not in (f["before"], f["after"])]
        if odd:
            return self._settle_back(store, j, odd)
        try:
            missing = [f for f in j["files"] if f["op"] == "write"
                       and _hash_file(_target(self.root, f["path"])) != f["after"]
                       and _hash_file(self._tmp(f)) != f["after"]]
            if missing:
                self._stage(dict(j, files=missing), persist=lambda: _write_json_durable(self._jpath(j["item"]), j))
            bad = self._commit_files(j)
        except (OSError, WorktreeError) as exc:
            log.error("item %s: could not finish its merge yet: %s", j["item"], exc)
            return "waiting"
        if bad:
            return self._settle_back(store, j, bad)
        j["state"] = "committed"
        _write_json_durable(self._jpath(j["item"]), j)
        return self._phase3(store, j)

    # -- let Claude fix it (spec 3.6) ---------------------------------------------------------------
    def merge_into_worktree(self, store, item_id: str) -> list[str]:
        """Write the 3-way result of P's newest files and the copy INTO the copy (conflict markers
        labelled refs/sc/p/<id> and refs/sc/ckpt/<id>); base := that snapshot of P. Returns the
        conflicted paths. P is not written."""
        _check_id(item_id)
        with self.lock.hold("let-claude"):
            item = self._load(store, item_id)
            if item is None:
                raise WorktreeError("failed", f"no item {item_id}")
            if self.verify(item) == "missing":
                raise WorktreeError("failed", f"the copy of {item_id} is missing")
            w = (item.get("worktree") or {})["path"]
            theirs = self.checkpoint(item)
            base = self._ref(f"refs/sc/base/{item_id}") or item["worktree"].get("base")
            ours = self.snapshot()
            self._set_ref(f"refs/sc/p/{item_id}", ours)
            rc, result, conflicted, err = self._merge_tree(item_id, base)
            if result is None:
                raise WorktreeError("failed", f"merge-tree exit {rc}: {err}")
            changes = [c for c in self._diff_tree(theirs, result)
                       if "160000" not in (c["src_mode"], c["dst_mode"]) and "120000" not in (c["src_mode"], c["dst_mode"])
                       and c["status"] != "T" and _safe_rel(c["path"]) and not _link_on_path(w, c["path"])]
            blobs = self._cat_blobs([c["dst"] for c in changes if c["status"] != "D"])
            for c in changes:
                t = _target(w, c["path"])
                if c["status"] == "D":
                    with contextlib.suppress(FileNotFoundError):
                        os.remove(t)
                    continue
                os.makedirs(os.path.dirname(t), exist_ok=True)
                _replace_bytes(t, blobs[c["dst"]])   # (2026-10-27, review) never through a link at t
            self._set_ref(f"refs/sc/base/{item_id}", ours)

            def fn(it, mm):
                rec = dict(it.get("worktree") or {})
                rec["base"] = ours
                it["worktree"] = rec
                mm.update(state="fixing", base=ours, wait=None, tries=0)
            self._mutate_merge(store, item_id, fn)
            return conflicted

    # -- undo (spec 3.6) ----------------------------------------------------------------------------
    def _later_merges(self, store, item_id: str, m: dict, paths: list[str]) -> list[dict]:
        """(2026-10-28, F1) The merges that block undoing this one: items of any lane of the project whose
        merge still stands (merged), ended after this one (merge.at) and changed one of `paths` (their
        backup's merge.json, else merge.changed). [{"id", "at", "paths"}], newest first."""
        try:
            mine = parse_iso(m.get("at"))
        except (TypeError, ValueError):
            mine = None
        want: set[str] = set(paths)
        found: dict[str, dict] = {}
        stores = [store] + [store_mod.Store(d) for _l, d in store_mod.list_lanes(self.project_dir)
                            if _norm(d) != _norm(store.data_dir)]
        for st in stores:
            try:
                items = st.load()
            except (OSError, ValueError, store_mod.StoreError):
                continue
            for it in items:
                om = it.get("merge") or {}
                oid = it.get("id")
                if oid == item_id or oid in found or not isinstance(oid, str) or om.get("state") != "merged":
                    continue
                try:
                    at = parse_iso(om.get("at"))
                except (TypeError, ValueError):
                    continue
                if at is None or (mine is not None and at <= mine):
                    continue
                info = _read_json(Path(om["backup"]) / "merge.json") if isinstance(om.get("backup"), str) else None
                changed = [c.get("path") for c in (info or {}).get("changes") or [] if isinstance(c, dict)]
                changed = changed or list(om.get("changed") or [])
                hit = sorted(want.intersection(p for p in changed if isinstance(p, str)))
                if hit:
                    found[oid] = {"id": oid, "at": om.get("at"), "paths": hit}
        return sorted(found.values(), key=lambda x: str(x["at"]), reverse=True)

    def undo(self, store, item_id: str, gen: int) -> dict:
        """Put back the bytes a merge replaced: allowed only while every changed file still holds the merged
        bytes. {"ok", "state", "paths"} (paths = files changed since, when refused)."""
        _check_id(item_id)
        with self.lock.hold("undo"):
            item = self._load(store, item_id)
            m = (item or {}).get("merge") or {}
            info = _read_json(Path(m.get("backup") or "") / "merge.json") if m.get("backup") else None
            if not item or m.get("state") != "merged" or not info:
                return {"ok": False, "state": m.get("state"), "paths": []}
            changes = info.get("changes") or []
            since = [c["path"] for c in changes if _hash_file(_target(self.root, c["path"])) != c.get("after")]
            if since:
                return {"ok": False, "state": "merged", "paths": since,
                        "later": self._later_merges(store, item_id, m, since)}
            bdir = self._backup_dir(item_id, "undo")
            files = []
            for n, c in enumerate(changes):
                if c.get("after") is not None:
                    dst = bdir / "ours" / Path(*c["path"].split("/"))
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    _replace_bytes(dst, Path(_target(self.root, c["path"])).read_bytes(), durable=True)
                f = {"path": c["path"], "before": c.get("after"), "after": c.get("before"),
                     "tmp": f".sc-merge-{item_id[:8]}-{gen}-u{n}.tmp"}
                if c.get("before") is None:
                    f.update(op="delete", src=None)
                else:
                    f.update(op="write", src="file:" + str(Path(m["backup"]) / "ours" / Path(*c["path"].split("/"))))
                files.append(f)
            j = {"v": 1, "kind": "undo", "item": item_id, "store": str(store.data_dir), "gen": gen,
                 "state": "planned", "dirs_made": [], "backup": str(bdir), "files": files}
            r = self._apply_journal(store, j)
            if r == "undone":
                self._mutate_merge(store, item_id, lambda it, mm: mm.update(gen=gen))
            return {"ok": r == "undone", "state": "undone" if r == "undone" else "merged", "paths": []}


def _free_bytes(path: str | os.PathLike) -> int | None:
    p = os.path.abspath(str(path))
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return None


# ---- the question and the actions (spec 3.5, 3.6) ---------------------------------------------------
ACTION_LABELS = {"let-claude": "Let Claude fix it (recommended)", "retry": "Try the merge again",
                 "take-task": "Take the task's version", "discard": "Discard the task's changes"}


def actions_for(kind: str) -> list[str]:
    if kind in ("content", "markers", "changed-during-merge"):
        return ["let-claude", "retry", "take-task", "discard"]
    if kind in ("link", "case"):
        return ["retry", "take-task", "discard"]
    return ["retry", "discard"]   # branch-switched, repo-busy, nested-repo


def _paths_text(paths: list[str]) -> str:
    shown = list(paths)[:QUESTION_PATHS_MAX]
    more = len(paths) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def not_merged_text(paths: list[str], n: int, kept: str | None) -> str:
    """(2026-10-29, delta M1) The note on files no merge could carry (Shadow.not_merged): paths only (<= 20 named
    + "and N more"), never file contents. The extension says the same in the UI language (format.js)."""
    shown = list(paths)[:QUESTION_PATHS_MAX]
    more = max(0, n - len(shown))
    one = n == 1
    return (f"{'1 file' if one else f'{n} files'} could not be merged back because copies leave "
            f"{'it' if one else 'them'} out: {', '.join(shown)}" + (f" and {more} more" if more else "")
            + f"; {'it is' if one else 'they are'} kept in the copy {kept or '?'}")


def not_merged_note(item: dict) -> str | None:
    """(2026-10-29, delta M1) not_merged_text of an item's merge.not_merged (CLI show/merge); None = none."""
    m = item.get("merge") if isinstance(item, dict) else None
    paths = [p for p in ((m or {}).get("not_merged") or []) if isinstance(p, str) and p]
    if not paths:
        return None
    n = m.get("not_merged_n")
    n = n if isinstance(n, int) and not isinstance(n, bool) and n >= len(paths) else len(paths)
    return not_merged_text(paths, n, m.get("kept") if isinstance(m.get("kept"), str) else None)


def merge_question(item: dict, kind: str, paths: list[str], args: dict | None = None) -> dict:
    """(spec 3.5) The blocked_question of a merge conflict: English text from the code (the extension
    translates by code), option labels, kind "merge", code, args {paths (<= 20), n, ...}, actions, gen."""
    paths = list(paths or [])
    args = dict(args or {})
    p = _paths_text(paths) or "(no file list)"
    texts = {
        "content": f"Finished, but its changes conflict with newer edits in the project: {p}. Nothing was written.",
        "markers": f"Its copy still holds conflict markers in: {p}. Nothing was written.",
        "link": f"Its changes include a link or a file-type change: {p}. Nothing was written.",
        "case": f"Its changes differ from the project only by upper/lower case in: {p}. Nothing was written.",
        "branch-switched": (f"The project switched from branch {args.get('from')} to {args.get('to')} while it ran. "
                            "Nothing was written."),
        "changed-during-merge": f"Files changed in the project while its changes were being written: {p}. "
                                "Nothing of it was kept.",
        # (2026-10-27, review) args.copy = the copy folder, which is kept (discard moves it aside)
        "nested-repo": (f"Its changes include a folder that is its own git repository: {p}. Git cannot merge that, "
                        f"so nothing was written. Copy it from {args.get('copy') or 'the private copy'} by hand "
                        "(or remove its .git folder there) and try again."),
        "repo-busy": (f"Finished, but not merged yet: these files are read-only or locked by another program: {p}."
                      if paths else "Finished, but not merged yet: the project's git is in the middle of a merge, "
                                    "rebase, cherry-pick or revert."),
    }
    acts = actions_for(kind)
    return {"text": texts.get(kind, f"Its changes could not be merged ({kind}). Nothing was written."),
            "options": [ACTION_LABELS[a] for a in acts], "kind": "merge", "code": kind,
            "args": dict(args, paths=paths[:QUESTION_PATHS_MAX], n=len(paths)), "actions": acts,
            "gen": (item.get("merge") or {}).get("gen")}


def _merge_final(item: dict) -> bool:
    """(2026-10-28, F1; ROOT-CAUSE [U3 -> U1]) merge.state merged/nothing ends the work of the item's CURRENT
    copy only when that merge ended after the copy was made (merge.at newer than worktree.created_at): a
    reply re-runs the item in a new copy (spec 4.6) while the earlier round's state stays. A missing or
    unreadable time is not final."""
    m = item.get("merge") or {}
    if m.get("state") not in ("merged", "nothing"):
        return False
    try:
        at = parse_iso(m.get("at"))
        made = parse_iso((item.get("worktree") or {}).get("created_at"))
    except (TypeError, ValueError):
        return False
    return at is not None and made is not None and at > made


def _undo_refusal(paths: list[str], later: list[dict], backup: str | None) -> str:
    """(2026-10-28, F1; coordinator decision) Why `merge <id> --undo` is refused and what to do. It stays
    refused: putting back the bytes from before this merge would drop the work of every later merge."""
    one = len(paths) == 1
    files = "the file" if one else "the files"
    text = "changed since the merge: " + _paths_text(paths)
    if not paths:
        return text
    if later:
        names = "; ".join(f"{x['id']} ({_paths_text(x['paths'])})" for x in later)
        text += (f". A later merge changed {'it' if one else 'them'} too: {names}. Undo the later merge first "
                 f"(newest first: merge <id> --undo), or edit {files} by hand")
    else:
        text += f". {'It was' if one else 'They were'} edited after the merge: edit {files} by hand"
    if backup:
        text += f" (the bytes from before this merge are in {os.path.join(backup, 'ours')})"
    return text


def _result(ok: bool, state, code, reply_text=None, **extra) -> dict:
    return dict({"ok": ok, "state": state, "code": code, "reply_text": reply_text}, **extra)


def merge_action(store, shadow: Shadow, item_id: str, action: str, gen: int | None, note: str | None = None) -> dict:
    """(spec 3.6) One owner action on a merge: {"ok", "state" (merge.state after), "code", "reply_text",
    "detail"?, "paths"?}. Refused (ok False) with code gen-mismatch, not-allowed, no-item, no-copy, busy
    or changed-since. Every action checks gen against merge.gen (now/undo only when gen is given), runs
    under the ProjectLock and bumps merge.gen."""
    if action not in MERGE_ACTIONS:
        return _result(False, None, "not-allowed", detail=f"unknown action {action!r}")
    if not isinstance(item_id, str) or not _ID_RE.match(item_id):
        return _result(False, None, "no-item", detail=f"unsafe item id {item_id!r}")
    try:
        with shadow.lock.hold(f"merge-{action}"):
            shadow.recover_journals(store)   # (2026-10-28, F1) a crashed merge/undo is settled first
            item = shadow._load(store, item_id)
            if item is None:
                return _result(False, None, "no-item")
            m = item.get("merge") or {}
            state, kind = m.get("state"), m.get("kind")
            if os.path.lexists(shadow._jpath(item_id)):
                # (2026-10-28, F1) its journal could not be settled yet: no action may write over it
                return _result(False, state, "busy", detail=f"its last merge or undo is not settled yet "
                                                            f"({shadow._jpath(item_id)}); try again later")
            if (gen is not None or action in QUESTION_ACTIONS) and gen != m.get("gen"):
                return _result(False, state, "gen-mismatch", detail="the question changed")
            new_gen = int(m.get("gen") or 0) + 1
            status = item.get("status")
            if action in QUESTION_ACTIONS:
                allowed = state == "conflict" and action in actions_for(kind or "content")
                if action in ("retry", "discard") and state == "waiting":
                    allowed = True
                if not allowed:
                    return _result(False, state, "not-allowed")
            if action == "let-claude":
                paths = shadow.merge_into_worktree(store, item_id) or list(m.get("paths") or [])
                shadow._mutate_merge(store, item_id, lambda it, mm: mm.update(gen=new_gen))
                text = ("Your copy now holds the project's newest files. Resolve every conflict marker "
                        "(<<<<<<< refs/sc/p/... / >>>>>>> refs/sc/ckpt/...) in: "
                        f"{_paths_text(paths) or 'the files named before'}. Keep both intents.")
                if note:
                    text += " " + note.strip()
                return _result(True, "fixing", "fixing", text)
            if action == "retry":
                branch = project_branch(shadow.root) if kind == "branch-switched" else None

                def fn(it, mm):
                    mm.update(state="pending", gen=new_gen, tries=0, wait=None, kind=None, paths=None)
                    if branch:
                        mm["branch_ok"] = branch
                    it["status"] = "running"
                    it["blocked_question"] = None
                shadow._mutate_merge(store, item_id, fn)
                return _result(True, "pending", "pending")
            if action == "take-task":
                shadow._mutate_merge(store, item_id, lambda it, mm: mm.update(state="pending", gen=new_gen))
                r = shadow.merge(store, item_id, new_gen, take_task=True)
                after = (shadow._load(store, item_id) or {}).get("merge") or {}
                return _result(True, after.get("state"), r)
            if action == "discard":
                patch = kept = None
                w = shadow.worktree_path(item)
                if (item.get("worktree") or {}).get("state") == "active" or shadow._ref(f"refs/sc/ckpt/{item_id}"):
                    try:
                        patch = str(shadow.save_patch(item, "discarded"))
                    except WorktreeError as exc:
                        log.warning("item %s: its changes could not be saved as a patch: %s", item_id, exc)
                # (2026-10-27, review) a copy whose changes are not all in a patch (none was made, or a nested
                # repo the patch only names by its commit id) is moved aside and kept, never deleted
                # (2026-10-29, delta M1) + files the app's excludes keep out of every patch (not_merged; a failed
                # check counts as some)
                lost: list[str] | None = []
                if w.is_dir() and not is_reparse_point(w) and shadow._owned_copy(w):
                    try:
                        lost = shadow.not_merged(item)
                    except WorktreeError as exc:
                        log.warning("item %s: its copy could not be checked for files a patch cannot carry (%s)",
                                    item_id, exc)
                        lost = None
                if ((patch is None or kind == "nested-repo" or lost != []) and w.is_dir() and not is_reparse_point(w)
                        and shadow._owned_copy(w)):
                    # (2026-10-29, delta M1 fix round 4) a copy kept only for the M1 check: its note is the log line
                    why = "could not be saved as a patch" if patch is None or kind == "nested-repo" else None
                    moved = shadow._keep_aside(w, why)
                    if moved is None:
                        return _result(False, state, "no-patch", detail=f"its changes could not be saved and its "
                                                                         f"copy {w} could not be moved aside")
                    kept = str(moved)
                    if lost:
                        log.warning("item %s: %s", item_id, not_merged_text(lost, len(lost), kept))
                    elif why is None:
                        log.warning("item %s: its copy is kept at %s", item_id, kept)
                gone = shadow.remove(store, item_id)

                def fd(it, mm):
                    mm.update(state="discarded", gen=new_gen, patch=patch, wait=None)
                    for k in ("not_merged", "not_merged_n", "kept"):
                        mm.pop(k, None)
                    if lost:
                        mm.update(not_merged=lost[:PATHS_MAX], not_merged_n=len(lost), kept=kept)
                    it["status"] = "cancelled"
                    it["blocked_question"] = None
                shadow._mutate_merge(store, item_id, fd)
                return _result(True, "discarded", "discarded", None, patch=patch, removed=gone, kept=kept)
            if action == "now":
                ok = state == "held" or (status in ("cancelled", "failed", "blocked")
                                         and state not in ("conflict", "fixing", *MERGE_ACTIVE)
                                         and shadow.unmerged(item))
                if not ok:
                    return _result(False, state, "not-allowed")
                keep = status in ("failed", "blocked")
                shadow._mutate_merge(store, item_id, lambda it, mm: mm.update(state="pending", gen=new_gen,
                                                                              result_status=status))
                r = shadow.merge(store, item_id, new_gen, force=True, keep_copy=keep)
                after = (shadow._load(store, item_id) or {}).get("merge") or {}
                return _result(True, after.get("state"), r)
            # undo
            if state != "merged" or not m.get("backup"):
                return _result(False, state, "not-allowed")
            r = shadow.undo(store, item_id, new_gen)
            if not r["ok"]:
                later = r.get("later") or []
                return _result(False, r["state"], "changed-since", paths=r["paths"], later=later,
                               detail=_undo_refusal(r["paths"], later, m.get("backup")))
            return _result(True, "undone", "undone")
    except WorktreeError as exc:
        return _result(False, None, exc.kind if exc.kind in ("busy", "stopped") else "no-copy"
                       if exc.kind == "failed" else exc.kind, detail=str(exc))


# ---- attempts in a copy (spec 4) ----------------------------------------------------------------------
def _active(item: dict) -> dict | None:
    rec = item.get("worktree")
    return rec if isinstance(rec, dict) and rec.get("state") == "active" and rec.get("path") else None


def effective_cwd(item: dict) -> str:
    """(spec 4.1) W\\<rel> while the copy is active, else the item's own cwd.
    (2026-10-29, delta C-L2) W in its long form (_long; a record made before wt_base was long-form)."""
    rec = _active(item)
    if rec is None:
        return item["cwd"]
    rel = rec.get("rel") or "."
    w = _long(rec["path"])
    return w if rel == "." else os.path.join(w, rel)


def _memory_path(root: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", root)
    return Path(os.path.expanduser("~"), ".claude", "projects", slug, "memory", "MEMORY.md")


def system_note(item: dict, shadow: Shadow | None) -> str:
    """(spec 4.3) The English note sent with --append-system-prompt to an attempt in a copy, <= NOTE_MAX
    characters: where it works, never to write the project folder, what is not copied, what is linked,
    the PYTHONPATH, that commits are not merged as commits; + recreated, memory and .pth hook lines."""
    rec = item.get("worktree") or {}
    w = rec.get("path") or ""
    # (2026-10-29, F1 fix 2) the root as typed (a junction/symlink root is named as the owner knows it);
    # its memory first (the slug Claude's own sessions in P use), then the long form's (an 8.3 typed root)
    long_root = shadow.root if shadow is not None else None
    root = (getattr(shadow, "typed_root", None) or long_root) or rec.get("root") or ""
    not_copied = list(rec.get("not_copied") or [])[:NOT_COPIED_MAX]
    links = [x.get("link") for x in rec.get("links") or [] if isinstance(x, dict)]
    hooks = list(rec.get("pth_hooks") or [])
    mems = [_memory_path(r) for r in dict.fromkeys((root, long_root)) if r]
    mem = next((m for m in mems if m.is_file()), None)

    def build(nc: list[str], lk: list[str], hk: list[str]) -> str:
        parts = [f"You are working in a private copy of the project: {w}. The real project folder is {root}: "
                 "never write there; Sleeping Clanker merges your changes back when you finish.",
                 "Files the project ignores are not copied and not merged back"
                 + (f" (not here: {', '.join(nc)})." if nc else ".")]
        if lk:
            parts.append(f"Linked from the project, shared with it: {', '.join(lk)}.")
        if rec.get("pythonpath"):
            parts.append(f"Python imports of the project come from this copy (PYTHONPATH={rec['pythonpath']}).")
        parts.append("This copy has a private git: commits you make here are not merged as commits, only the "
                     "file changes are; say in your final message what you would commit.")
        if int(rec.get("recreated") or 0) > 0:
            parts.append("This copy was recreated from the project's current files.")
        if mem is not None and mem.is_file():
            parts.append(f"Project memory: {mem}.")
        if hk:
            parts.append(f"These .pth import hooks still load from the project folder: {', '.join(hk)}.")
        return " ".join(parts)

    text = build(not_copied, links, hooks)
    while len(text) > NOTE_MAX and (not_copied or links or hooks):
        for lst in (not_copied, links, hooks):
            if lst:
                lst.pop()
                break
        text = build(not_copied, links, hooks)
    return text[:NOTE_MAX]


def attempt_env_patch(item: dict, env: dict | None = None) -> dict:
    """(spec 4.4) {"set": {...}, "drop": [...]} for an attempt in a copy: PYTHONPATH -> the copy's paths
    (prepended to the parent's), and every GIT_* / VSCODE_GIT_* variable dropped (Claude's own git in W
    must use W\\.git, never an inherited GIT_DIR). Nothing for an item without an active copy."""
    rec = _active(item)
    if rec is None:
        return {"set": {}, "drop": []}
    env = os.environ if env is None else env
    drop = sorted(k for k in env if k.upper().startswith(("GIT_", "VSCODE_GIT_")))
    patch: dict[str, str] = {}
    if rec.get("pythonpath"):
        old = env.get("PYTHONPATH")
        patch["PYTHONPATH"] = rec["pythonpath"] + (os.pathsep + old if old else "")
    return {"set": patch, "drop": drop}


def _map_text(value, pairs: list[tuple[re.Pattern, str]]):
    if isinstance(value, str):
        for rx, repl in pairs:
            value = rx.sub(lambda _m, r=repl: r, value)
        return value
    if isinstance(value, dict):
        return {k: _map_text(v, pairs) for k, v in value.items()}
    if isinstance(value, list):
        return [_map_text(v, pairs) for v in value]
    return value


def outcome_to_project(out, item: dict):
    """(spec 4.5) The outcome with copy paths in its denials mapped to the project folder (both slash
    forms, any case), for denial blocks and summaries; the task log keeps the raw paths."""
    rec = item.get("worktree") or {}
    w, root = rec.get("path"), rec.get("root")
    if not w or not root or not getattr(out, "denials", None):
        return out
    pairs = []
    for sep in ("\\", "/"):
        pairs.append((re.compile(re.escape(w.replace("\\", sep)), re.I), root.replace("\\", sep)))
    return dataclasses.replace(out, denials=_map_text(list(out.denials), pairs))

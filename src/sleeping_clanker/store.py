"""Queue storage, data directory and runner lock (2026-10-13, spec section 4).

- queue.json is written atomically (temp file in the same dir + os.replace).
- Saves made by the runner keep the previous version as queue.json.bak.
- A short-lived mutex file (queue.json.lock) serialises read-modify-write cycles, so a CLI `add`
  while the runner is active cannot be lost (2026-10-13: not in the spec, prevents lost updates).
- runner.lock holds {pid, started_at, run_id}; a live PID refuses a second runner, a dead PID is
  taken over (and logged).
- (2026-10-26, runner identity) runner.lock also holds `create_time`: the runner process's creation
  time from GetProcessTimes as a POSIX epoch float (UTC seconds; null when it cannot be read). Windows
  reuses PIDs, so "the PID is alive" is not "the runner is alive": lock_owner_alive() also requires the
  live process to be the one that wrote the lock (see there). A dead or reused PID is a stale lock.
- stop.request asks a live runner to stop gracefully (2026-10-13, SPEC_VSCODE section 2).
- (2026-10-13, SPEC_GOALS section 1) Items carry kind/attachments/rounds/max_rounds/max_hours/
  goal_started_at/paused_from; old queue.json files without them load unchanged (readers use .get).
- (2026-10-13, SPEC_PROJECTS section 1) One full data dir per project: <base>/projects/<slug>-<hash8>,
  chosen by project_data_dir(); everything else in this module works per dir as before.
- (2026-10-15, SPEC_V03 section 2.1) Named queues ("lanes"): the project dir is the `default` lane;
  any other lane is a full store dir at <project dir>/lanes/<name>, chosen by lane_data_dir(). As with
  projects, only the dir choice is new; nothing per-dir changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import __version__
from .clock import iso, parse_iso
from .goals import (DEFAULT_FRESH_AT_CTX, DEFAULT_FRESH_EVERY, DEFAULT_MAX_HOURS, DEFAULT_MAX_ROUNDS,
                    KIND_GOAL)
from .log import get_logger, get_run_id
from .procutil import pid_alive, process_create_time
from .profiles import normalise_agents

log = get_logger(__name__)

# (2026-10-26, runner identity) a lock's create_time must match the live process's creation time within
# this many seconds (it is the same GetProcessTimes value; the slack only absorbs float rounding)
CREATE_TIME_TOLERANCE_S = 1.0
# (2026-10-26, runner identity) an older core's lock has no create_time: its runner existed before it
# wrote started_at, so a process created more than this after started_at cannot be that runner
STARTED_AT_SLACK_S = 5.0
_unreadable_logged: set[int] = set()


def lock_identity(info: dict | None, alive: Callable[[int], bool] = pid_alive) -> tuple[bool, str]:
    """(2026-10-26, runner identity) (alive, why) for a runner.lock's contents: alive only when its PID
    is a live process AND that process is the runner that wrote the lock.

    - the lock has a numeric `create_time`: the live process's creation time must match it (within
      CREATE_TIME_TOLERANCE_S);
    - an older core's lock (no usable `create_time`): the live process must have been created no later
      than `started_at` + STARTED_AT_SLACK_S (a process created after the lock was written is a reused
      PID); an unparseable started_at cannot be judged, so the PID check alone decides (as before);
    - the creation time cannot be read (access denied, not Windows): the PID check alone decides (as
      before), logged at debug once per PID and process."""
    if not info:
        return False, "no runner.lock" if info is None else "runner.lock unreadable or without a pid"
    pid = info.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False, "runner.lock has no valid pid"
    if not alive(pid):
        return False, f"pid {pid} is not running"
    created = process_create_time(pid)
    if created is None:
        if sys.platform == "win32" and pid not in _unreadable_logged:
            _unreadable_logged.add(pid)
            log.debug("runner.lock pid %s: its creation time cannot be read (access denied?); "
                      "judged by the PID alone", pid)
        return True, f"pid {pid} is running (identity not checkable)"
    recorded = info.get("create_time")
    if isinstance(recorded, (int, float)) and not isinstance(recorded, bool):
        if abs(created - recorded) <= CREATE_TIME_TOLERANCE_S:
            return True, f"pid {pid} is the runner"
        return False, (f"pid {pid} is another process (created {_epoch_iso(created)}, the runner "
                       f"was created {_epoch_iso(recorded)})")
    try:
        started = parse_iso(info.get("started_at") if isinstance(info.get("started_at"), str) else None)
    except ValueError:
        started = None
    if started is None:
        return True, f"pid {pid} is running (started_at unknown)"
    if created <= started.timestamp() + STARTED_AT_SLACK_S:
        return True, f"pid {pid} is the runner (created before its lock)"
    return False, (f"pid {pid} is another process (created {_epoch_iso(created)}, after the lock's "
                   f"started_at {info.get('started_at')})")


def lock_owner_alive(info: dict | None, alive: Callable[[int], bool] = pid_alive) -> bool:
    """(2026-10-26, runner identity) True when the lock's runner is alive (see lock_identity)."""
    return lock_identity(info, alive)[0]


def _epoch_iso(t: float) -> str:
    try:
        return iso(datetime.fromtimestamp(t, timezone.utc))
    except (OverflowError, OSError, ValueError):
        return repr(t)

ENV_HOME = "CLAUDE_AUTOPILOT_HOME"
# (2026-10-15, SPEC_V03 5.1) `blocked`: terminal, Claude needs the owner (never started, never blocks)
STATUSES = ("pending", "running", "waiting", "done", "failed", "cancelled", "paused", "blocked")
RESULT_MAX = 4000
# (2026-10-21, v049 dontask_hold) a queue with this file in its data dir is on hold: no runner starts
# for it without `run --ignore-hold`, and a live runner exits after its current item
HOLD_FILE = "hold.json"
# (2026-10-26, limit_continue) the hold `run --on-limit ask` puts on when a usage limit parks an item:
# hold.json gets "reason" and "resets_at" next to "since", so an older reader still sees a plain hold
HOLD_REASON_LIMIT = "limit"
# (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.2/1.9/6.1) per lane, next to queue.json: the live "tasks at
# once" number, the parallel runner's heartbeat, the shared usage-limit gate folder
TASKS_AT_ONCE_FILE = "tasks_at_once.json"
PARALLEL_FILE = "parallel.json"
LIMIT_GATE_DIR = "limit_gate"
TASKS_AT_ONCE_MAX = 5


class StoreError(Exception):
    pass


class LockHeld(StoreError):
    pass


def default_data_dir(explicit: str | os.PathLike | None = None, env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    if explicit:
        return Path(explicit).resolve()
    if env.get(ENV_HOME):
        return Path(env[ENV_HOME]).resolve()
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "claud_autopilot"


PROJECTS_DIRNAME = "projects"
PROJECT_FILE = "project.json"
_SLUG_BAD = re.compile(r"[^A-Za-z0-9._-]")


def project_root(root: str | os.PathLike) -> str:
    r"""The canonical root string: os.path.abspath without a `\\?\` prefix or trailing separators.

    (2026-10-13, spec amendment M10) Never Path.resolve(): a mapped/subst drive, a junction or a
    deleted folder would resolve differently over time and the same project would get a new dir.
    """
    text = os.fspath(root)
    # (2026-10-26, improve_core) `//?/` is the same prefix, and `\\?\UNC\srv\share` is `\\srv\share`:
    # stripping only a literal `\\?\` kept `//?/C:/x` as a second key for `C:\x`, and turned the UNC form
    # into `UNC\srv\...`, which abspath made relative to the current folder.
    head = text[:8].replace("/", "\\")
    if head.upper() == "\\\\?\\UNC\\":
        text = "\\\\" + text[8:]
    elif head.startswith("\\\\?\\"):
        text = text[4:]
    text = os.path.abspath(text)
    stripped = text.rstrip("\\/")
    if not stripped or stripped.endswith(":"):  # keep a drive root as "A:\"
        return stripped + os.sep if stripped else text
    return stripped


def project_key(root: str | os.PathLike) -> str:
    """The hash input: os.path.normcase(project_root(root)) (lower case, backslashes on Windows)."""
    return os.path.normcase(project_root(root))


def project_dir_name(root: str | os.PathLike) -> str:
    """`<slug>-<hash8>` (2026-10-13, SPEC_PROJECTS section 1 + amendment M10)."""
    canon = project_root(root)
    name = os.path.basename(canon.rstrip("\\/"))
    slug = _SLUG_BAD.sub("_", name)[:40] or "root"  # a drive root has no base name (review L14)
    hash8 = hashlib.sha1(project_key(canon).encode("utf-8")).hexdigest()[:8]
    return f"{slug}-{hash8}"


def _existing_project_dir(base: Path, canon: str) -> Path | None:
    """(2026-10-13) The hash is case-insensitive but the slug keeps the folder's case, so `a:\\x\\Foo`
    and `A:\\X\\foo` would give `foo-<h>` and `Foo-<h>`. An existing `*-<hash8>` dir whose
    project.json names the same root (by key) is reused, so every variant maps to ONE dir."""
    parent = base / PROJECTS_DIRNAME
    name = project_dir_name(canon)
    exact = parent / name
    if exact.is_dir():
        return exact
    hash8 = name.rsplit("-", 1)[1]
    key = project_key(canon)
    try:
        candidates = sorted(parent.glob(f"*-{hash8}"))
    except OSError:
        return None
    for d in candidates:
        root = read_project_meta(d).get("root")
        if d.is_dir() and isinstance(root, str) and project_key(root) == key:
            return d
    return None


def project_data_dir(base: Path, root: str | os.PathLike, now: datetime | None = None,
                     create: bool = True) -> Path:
    """<base>/projects/<slug>-<hash8>. With `create`, makes it and its project.json.

    (2026-10-13) The root does not have to exist: a project whose folder was deleted keeps a
    readable, removable queue. Read-only commands pass create=False (amendment M11), so merely
    looking at a folder never adds a project. A project.json naming a different root (a 32-bit hash
    collision, review L15) raises StoreError.
    """
    canon = project_root(root)
    d = _existing_project_dir(Path(base), canon) or Path(base) / PROJECTS_DIRNAME / project_dir_name(canon)
    meta = d / PROJECT_FILE
    existing = read_project_meta(d).get("root")
    if isinstance(existing, str) and project_key(existing) != project_key(canon):
        raise StoreError(f"project dir {d} belongs to {existing}, not {canon} (hash collision); "
                         "refusing to mix their queues")
    if create:
        d.mkdir(parents=True, exist_ok=True)
        if not meta.exists():
            info = {"root": canon, "created_at": iso(now or datetime.now(timezone.utc))}
            # (2026-10-26, improve_core) a unique temp name + the replace retry (two first `add`s at once)
            write_text_atomic(meta, json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    return d


# ---- named queues ("lanes") (2026-10-15, docs/SPEC_V03.md section 2.1) -----------------------------
LANES_DIRNAME = "lanes"
REMOVED_LANES_DIRNAME = ".removed"
DEFAULT_LANE = "default"
LANE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def valid_lane_name(name: object) -> bool:
    """`^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`; `default` (any case) is valid and means the project dir."""
    return isinstance(name, str) and bool(LANE_RE.match(name))


def is_default_lane(name: str | None) -> bool:
    return name is None or name.lower() == DEFAULT_LANE


def _lane_dirs(project_dir: Path) -> list[Path]:
    """The existing non-default lane dirs (valid names only; `.removed` and stray files are skipped)."""
    parent = Path(project_dir) / LANES_DIRNAME
    try:
        entries = list(parent.iterdir())
    except OSError:
        return []
    return [d for d in entries if d.is_dir() and valid_lane_name(d.name) and not is_default_lane(d.name)]


def lane_data_dir(project_dir: Path, lane: str | None) -> tuple[str, Path]:
    """(canonical name, dir) of a lane. `default` is the project dir itself, unchanged since v0.2;
    any other lane is `<project dir>/lanes/<name>`, a full store dir. Names are unique without regard
    to case: an existing dir whose name matches case-insensitively is reused, with its own spelling
    (2026-10-15). Nothing is created here; `Store.ensure()` makes the dir on the first write."""
    if is_default_lane(lane):
        return DEFAULT_LANE, Path(project_dir)
    if not valid_lane_name(lane):
        raise StoreError(f"invalid queue name {lane!r} (letters, digits, '_' and '-', at most 32)")
    want = str(lane).lower()
    for d in _lane_dirs(project_dir):
        if d.name.lower() == want:
            return d.name, d
    return str(lane), Path(project_dir) / LANES_DIRNAME / str(lane)


def list_lanes(project_dir: Path) -> list[tuple[str, Path]]:
    """`default` first, then every lane dir sorted case-insensitively. A lane exists once its dir
    does (the first `add`, or a `run`, creates it)."""
    others = sorted(_lane_dirs(project_dir), key=lambda d: (d.name.lower(), d.name))
    return [(DEFAULT_LANE, Path(project_dir))] + [(d.name, d) for d in others]


def has_any_queue(project_dir: Path) -> bool:
    """True if the project dir or any of its lanes holds a queue.json."""
    return any((d / "queue.json").is_file() for _name, d in list_lanes(project_dir))


def read_project_meta(project_dir: Path) -> dict:
    try:
        data = json.loads((Path(project_dir) / PROJECT_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def project_dir_of(store: "Store") -> Path | None:
    """(2026-10-27, tasks at once) The project data dir of a lane store: its own dir when it holds
    project.json (the default lane), <project>/lanes/<name> -> <project>; None for a store that is not
    a project store (a plain --data-dir)."""
    lane = Path(store.data_dir)
    if (lane / PROJECT_FILE).is_file():
        return lane
    if lane.parent.name == LANES_DIRNAME and (lane.parent.parent / PROJECT_FILE).is_file():
        return lane.parent.parent
    return None


def base_dir_of(project_dir: str | os.PathLike) -> Path:
    """(2026-10-27) The base data dir of a project data dir (<base>/projects/<slug>-<hash8>)."""
    return Path(project_dir).parent.parent


def next_seq(items: list[dict]) -> int:
    """(2026-10-13, fix B2) Monotonic add-order number: max existing seq + 1 (legacy items lack it)."""
    return max((i["seq"] for i in items if isinstance(i.get("seq"), int)), default=0) + 1


def queue_order_key(item: dict) -> tuple:
    """Strict add order: legacy items (no seq) first by created_at, then by seq."""
    seq = item.get("seq")
    has_seq = isinstance(seq, int)
    return (1 if has_seq else 0, seq if has_seq else 0, item.get("created_at") or "", item["id"])


# ---- subtasks (2026-10-21, additive `parent` field) -------------------------------------------
def subtasks_of(items: list[dict], item_id: str) -> list[dict]:
    """The subtasks of one main task, in add order."""
    return sorted((i for i in items if i.get("parent") == item_id), key=queue_order_key)


def held_by_parent(item: dict, by_id: dict[str, dict]) -> bool:
    """A subtask waits while its main task is not `done`. A removed main task holds nothing."""
    parent = by_id.get(item.get("parent") or "")
    return parent is not None and parent.get("status") != "done"


def run_order_key(item: dict, by_id: dict[str, dict]) -> tuple:
    """Queue order with each subtask right after its main task (then the subtasks in add order)."""
    parent = by_id.get(item.get("parent") or "")
    if parent is None:
        return (queue_order_key(item), 0, queue_order_key(item))
    return (queue_order_key(parent), 1, queue_order_key(item))


# ---- move (2026-10-26, owner task 07254297 "drag in priority") ---------------------------------
def order_peers(items: list[dict], item: dict, by_id: dict[str, dict]) -> list[dict]:
    """The items `item` is ranked against in run_order_key, in queue order (itself included): every
    main task for a main task (a removed main task's subtask counts as one), else the subtasks of
    its own main task."""
    parent = by_id.get(item.get("parent") or "")
    return sorted((i for i in items if by_id.get(i.get("parent") or "") is parent), key=queue_order_key)


def move_seq(items: list[dict], target: dict, index: int) -> bool:
    """Put `target` at `index` among its peers (order_peers without itself) by reassigning seq only,
    the mechanism of `first` and `reply`: to the front, a seq below every other one (as `first`); to the
    end, above every other one (as `reply`); between two peers, a free seq between them, else every
    item from there on moves up by the same step, so no other item changes its place. Items without a
    seq (legacy) first get one below the rest, in their present order. Returns False, changing
    nothing, when the target is already at `index`."""
    by_id = {i["id"]: i for i in items}
    peers = order_peers(items, target, by_id)
    others = [p for p in peers if p is not target]
    index = max(0, min(index, len(others)))
    if next(n for n, p in enumerate(peers) if p is target) == index:
        return False
    legacy = sorted((i for i in items if not isinstance(i.get("seq"), int)), key=queue_order_key)
    low = min((i["seq"] for i in items if isinstance(i.get("seq"), int)), default=1)
    for n, it in enumerate(legacy):
        it["seq"] = low - len(legacy) + n
    rest = sorted((i for i in items if i is not target), key=queue_order_key)
    if index == 0:
        target["seq"] = min(i["seq"] for i in rest) - 1
    elif index == len(others):
        target["seq"] = max(i["seq"] for i in rest) + 1
    else:
        pos = next(n for n, i in enumerate(rest) if i is others[index])
        lo, hi = rest[pos - 1]["seq"], rest[pos]["seq"]
        if hi - lo < 2:
            for it in rest[pos:]:
                it["seq"] += lo + 2 - hi
        target["seq"] = lo + 1
    return True


def with_defaults(item: dict) -> dict:
    """A copy with the v0.2 fields filled in, for `--json` output of items from an older queue.json
    (2026-10-13, SPEC_GOALS section 1). The stored item is not changed."""
    out = dict(item)
    out.setdefault("kind", "task")
    out.setdefault("attachments", [])
    out.setdefault("rounds", 0)
    out.setdefault("max_rounds", None)
    out.setdefault("max_hours", None)
    out.setdefault("goal_started_at", None)
    out.setdefault("paused_from", None)
    out.setdefault("no_marker_rounds", 0)
    out.setdefault("total_cost_usd", None)
    out.setdefault("replies", [])  # (2026-10-15, SPEC_V03 5.2)
    out.setdefault("pending_reply", None)
    # (2026-10-17, 0.4.1, additive) R3 blocked question, R5 fresh sessions, R1 model/version facts
    out.setdefault("blocked_question", None)
    out.setdefault("handoff", None)
    out.setdefault("prior_sessions", [])
    out.setdefault("fresh_at_ctx", None)
    out.setdefault("fresh_every", None)
    # (2026-10-20, additive) how the last attempt with an init event was billed
    out.setdefault("billing", "unknown")
    out.setdefault("api_key_source", None)
    # (2026-10-20, 0.4.4 logfix C, additive) blocks in a row with the same question and profile
    out.setdefault("repeat_block", 0)
    # (2026-10-21, subtasks, additive) the main task's id, and its label for the subtask's prompt
    out.setdefault("parent", None)
    out.setdefault("parent_label", None)
    # (2026-10-21, task model, additive) the task's own model; None = the run's model
    out.setdefault("model", None)
    # (2026-10-21, subagents, additive) how many subagents the task may start; None = the profile decides
    out.setdefault("max_agents", None)
    # (2026-10-21, add panel, additive, docs/SPEC_V049_ADD_PANEL.md) the task's own provider and effort;
    # None = the run's, as `model`
    out.setdefault("provider", None)
    out.setdefault("effort", None)
    # (2026-10-26, modes, additive) `ultracode`: the attempt passes --settings {"ultracode":true};
    # `autorun`: the extension answers the task's option questions itself (the core only keeps it)
    out.setdefault("ultracode", False)
    out.setdefault("autorun", False)
    # (2026-10-27, tasks at once, additive) order (`after`), run in place, the private copy, the merge
    out.setdefault("after", None)
    out.setdefault("after_source", None)
    out.setdefault("in_place", False)
    out.setdefault("worktree", None)
    out.setdefault("merge", None)
    return out


PARENT_LABEL_MAX = 200


def parent_label(parent: dict) -> str:
    """(2026-10-21, subtasks) The main task's name, else its prompt's first 200 chars (whitespace
    collapsed): stored on the subtask, so its prompt names the main task without a lookup."""
    text = parent.get("name") or " ".join((parent.get("prompt") or "").split())
    return text[:PARENT_LABEL_MAX] + ("…" if len(text) > PARENT_LABEL_MAX else "")


def new_item(prompt: str, cwd: str, name: str | None, profile: str, now: datetime,
             existing_ids: set[str], seq: int | None = None, kind: str = "task",
             attachments: list[str] | None = None, max_rounds: int | None = None,
             max_hours: float | None = None, fresh_at_ctx: int | None = None,
             fresh_every: int | None = None, parent: dict | None = None,
             model: str | None = None, max_agents: int | None = None,
             provider: str | None = None, effort: str | None = None) -> dict:
    """A new pending item. For a goal, missing caps default to 50 rounds / 72 hours, and the
    fresh-session settings to 100000 / 0 (2026-10-17, 0.4.1 R5). `parent` (2026-10-21) makes it a
    subtask of that main task; `model` (2026-10-21) is its own model (None = the run's);
    `max_agents` (2026-10-21) its subagent limit (None = the profile decides, 0 = none, -1 = no limit);
    `provider`/`effort` (2026-10-21, add panel) its own provider name and effort (None = the run's)."""
    goal = kind == KIND_GOAL
    if goal:
        max_rounds = DEFAULT_MAX_ROUNDS if max_rounds is None else max_rounds
        max_hours = DEFAULT_MAX_HOURS if max_hours is None else max_hours
        fresh_at_ctx = DEFAULT_FRESH_AT_CTX if fresh_at_ctx is None else fresh_at_ctx
        fresh_every = DEFAULT_FRESH_EVERY if fresh_every is None else fresh_every
    while True:
        item_id = secrets.token_hex(4)
        if item_id not in existing_ids:
            break
    return {
        "id": item_id,
        "seq": seq,
        "prompt": prompt,
        "cwd": cwd,
        "name": name,
        "status": "pending",
        "session_id": None,
        "attempts": 0,
        "limit_waits": 0,
        "created_at": iso(now),
        "started_at": None,
        "finished_at": None,
        "wait_until": None,
        "result": None,
        "error": None,
        "cost_usd": None,
        "num_turns": None,
        "profile": profile,
        "permission_denials": [],
        "kind": kind,
        "attachments": list(attachments or []),
        "rounds": 0,
        "max_rounds": max_rounds if goal else None,
        "max_hours": max_hours if goal else None,
        "goal_started_at": None,
        "paused_from": None,
        "no_marker_rounds": 0,
        "total_cost_usd": None,
        "replies": [],
        "pending_reply": None,
        "blocked_question": None,
        "handoff": None,
        "prior_sessions": [],
        "fresh_at_ctx": fresh_at_ctx if goal else None,
        "fresh_every": fresh_every if goal else None,
        "billing": "unknown",
        "api_key_source": None,
        "repeat_block": 0,
        "parent": parent["id"] if parent else None,
        "parent_label": parent_label(parent) if parent else None,
        "model": model or None,
        "max_agents": normalise_agents(max_agents),
        "provider": provider or None,
        "effort": effort or None,
        # (2026-10-26, modes) off; `add --ultracode/--autorun on` sets them after new_item
        "ultracode": False,
        "autorun": False,
        # (2026-10-27, tasks at once) the same five as with_defaults, so queue.json == list --json
        "after": None,
        "after_source": None,
        "in_place": False,
        "worktree": None,
        "merge": None,
    }


REPLACE_DELAYS_S = (0.05, 0.1, 0.2, 0.4, 0.5, 0.75)  # ~2 s in total
# (2026-10-26, improve_core) the queue mutex is judged by its holder's pid only once it is this old: the
# holder writes the pid right after creating the file, and a transaction takes milliseconds
MUTEX_PID_AFTER_S = 1.0
# (2026-10-26, improve_core) an empty or invalid runner.lock younger than this may still be being written
LOCK_SETTLE_S = 2.0


def _replace_with_retry(src: str, dst: Path) -> None:
    """os.replace, retried on PermissionError with backoff for about 2 s (2026-10-13, amendment M6).

    On Windows a reader that has queue.json open (the 1 Hz status probe, a CLI call, the extension's
    poller) makes os.replace fail with WinError 5 for a moment.
    """
    for delay in (*REPLACE_DELAYS_S, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            if delay is None:
                raise
            log.debug("os.replace(%s) refused (%s); retrying in %.2fs", dst.name, exc, delay)
            time.sleep(delay)  # real sleep on purpose: a sub-second file-sharing retry, not a wait


def _ends_by(until: str, resets_at) -> bool:
    """(2026-10-27, integrate: hold kinds compose) True when a timed hold's `until` (ISO) is at or after a
    usage limit's `resets_at` (ISO): the hold already covers the limit. False when either cannot be read."""
    try:
        return isinstance(resets_at, str) and parse_iso(until) >= parse_iso(resets_at)
    except ValueError:
        return False


def write_text_atomic(path: str | os.PathLike, text: str) -> None:
    """(2026-10-26, improve_core) Write a small state file atomically: a UNIQUE temp file in its folder,
    then _replace_with_retry. The fixed `<name>.tmp` let two writers clobber each other's temp file, and a
    bare os.replace failed while a reader had the target open (Windows)."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def read_text_retry(path: str | os.PathLike) -> str:
    """(2026-10-26, improve_core) Path.read_text (utf-8), retried on PermissionError like Store._read:
    a read during another process's os.replace of the file fails for a moment on Windows."""
    for delay in (*REPLACE_DELAYS_S, None):
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)  # real sleep on purpose: a sub-second file-sharing retry, not a wait
    raise AssertionError("unreachable")


def _hold_until(data) -> datetime | None:
    """(2026-10-26, usage forecast) The `until` of a timed hold file, or None (none, or unreadable)."""
    value = data.get("until") if isinstance(data, dict) else None
    if not isinstance(value, str):
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.queue_path = self.data_dir / "queue.json"
        self.backup_path = self.data_dir / "queue.json.bak"
        self.mutex_path = self.data_dir / "queue.json.lock"
        self.lock_path = self.data_dir / "runner.lock"
        self.stop_path = self.data_dir / "stop.request"
        # (2026-10-21, v049 dontask_hold) the queue's hold flag, next to queue.json (never in a repo)
        self.hold_path = self.data_dir / HOLD_FILE
        self.runs_dir = self.data_dir / "runs"
        self.prompts_dir = self.data_dir / "prompts"
        # (2026-10-26, improve_core) attempt outcomes the queue could not take yet (loop.record_outcome)
        self.pending_outcomes_dir = self.data_dir / "pending_outcomes"

    def ensure(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ---- queue file -------------------------------------------------------------------------
    def _read(self, path: Path) -> list[dict]:
        # (2026-10-26, improve_core) On Windows open() fails with PermissionError for a moment while
        # another process os.replace()s the file (measured: ~3% of reads under a busy writer). Retry it
        # like _replace_with_retry, so a busy queue is not reported corrupt or served from the old .bak.
        for delay in (*REPLACE_DELAYS_S, None):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                break
            except PermissionError:
                if delay is None:
                    raise
                time.sleep(delay)  # real sleep on purpose: a sub-second file-sharing retry, not a wait
        if not isinstance(data, list):
            raise ValueError(f"{path.name} does not hold a list")
        return data

    def load(self) -> list[dict]:
        if not self.queue_path.exists():
            return []
        try:
            return self._read(self.queue_path)
        except (OSError, ValueError) as exc:
            log.error("queue.json unreadable (%s); trying %s", exc, self.backup_path.name)
            if self.backup_path.exists():
                try:
                    items = self._read(self.backup_path)
                    log.warning("recovered %d items from %s", len(items), self.backup_path.name)
                    return items
                except (OSError, ValueError) as exc2:
                    log.error("backup also unreadable: %s", exc2)
            # (2026-10-26, improve_core) name the cause: "corrupt" alone hid a locked or unreadable file
            raise StoreError(f"queue file {self.queue_path} is corrupt and no valid backup exists "
                             f"({exc})") from exc

    def save(self, items: list[dict], backup: bool = False) -> None:
        """Atomic write: temp file in the same directory, fsync, os.replace."""
        self.ensure()
        if backup and self.queue_path.exists():
            try:
                shutil.copy2(self.queue_path, self.backup_path)
            except OSError as exc:
                log.error("could not write %s: %s", self.backup_path.name, exc)
        fd, tmp = tempfile.mkstemp(prefix="queue.json.", suffix=".tmp", dir=self.data_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(items, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            _replace_with_retry(tmp, self.queue_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def _mutex(self, timeout: float = 10.0, stale_after: float = 30.0) -> Iterator[None]:
        self.ensure()
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(self.mutex_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                break
            except PermissionError as exc:
                # (2026-10-26, improve_core) Windows: while the last holder deletes the mutex file (delete
                # pending), O_EXCL gets EACCES, not EEXIST. Wait like for a busy mutex; this used to kill
                # the transaction (an `add`, or the runner itself). The deadline keeps the real cause.
                if time.monotonic() > deadline:
                    raise StoreError(f"cannot create the queue mutex {self.mutex_path}: {exc}") from exc
                time.sleep(0.05)  # real sleep on purpose: microsecond-scale file mutex, not a wait
                continue
            except FileExistsError:
                try:
                    age = time.time() - self.mutex_path.stat().st_mtime
                except FileNotFoundError:
                    continue  # released meanwhile: try again at once
                except OSError as exc:
                    # (2026-10-26, improve_core) any other stat error was a `continue` with no sleep and no
                    # deadline: a lasting one spun a core forever. Wait like for a busy mutex instead.
                    if time.monotonic() > deadline:
                        raise StoreError(f"queue is busy ({self.mutex_path} cannot be checked: {exc})") from exc
                    time.sleep(0.05)  # real sleep on purpose: microsecond-scale file mutex, not a wait
                    continue
                # (2026-10-26, improve_core) a holder that died inside its transaction (taskkill, a CLI
                # timeout) made every transaction fail "queue is busy" for 30 s and so killed the runner:
                # a mutex over MUTEX_PID_AFTER_S old whose pid is not running is stale at once
                dead = age > MUTEX_PID_AFTER_S and self._mutex_holder_dead()
                if age > stale_after or dead:
                    log.warning("removing stale queue mutex (age %.0fs%s)", age,
                                ", its process is gone" if dead else "")
                    with contextlib.suppress(OSError):
                        self.mutex_path.unlink()
                    continue
                if time.monotonic() > deadline:
                    raise StoreError(f"queue is busy ({self.mutex_path} held > {timeout}s)")
                time.sleep(0.05)  # real sleep on purpose: microsecond-scale file mutex, not a wait
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                self.mutex_path.unlink()

    def _mutex_holder_dead(self) -> bool:
        """(2026-10-26, improve_core) True when queue.json.lock names a pid that is not running. An
        unreadable or empty file, or a live (or reused) pid, is not judged dead: the age rule decides."""
        try:
            pid = int(self.mutex_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return False
        return not pid_alive(pid)

    @contextlib.contextmanager
    def transaction(self, backup: bool = False) -> Iterator[list[dict]]:
        """Load, let the caller mutate the list in place, save atomically on normal exit."""
        with self._mutex():
            items = self.load()
            yield items
            self.save(items, backup=backup)

    def find(self, items: list[dict], ref: str) -> dict:
        exact = [i for i in items if i["id"] == ref]
        if exact:
            return exact[0]
        matches = [i for i in items if i["id"].startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"no item with id {ref!r}")
        raise KeyError(f"id prefix {ref!r} is ambiguous ({len(matches)} items)")

    def update(self, item_id: str, backup: bool = True, **fields) -> dict:
        with self.transaction(backup=backup) as items:
            item = self.find(items, item_id)
            item.update(fields)
            return dict(item)

    def mutate(self, item_id: str, fn: Callable[[dict], None], backup: bool = True) -> dict:
        with self.transaction(backup=backup) as items:
            item = self.find(items, item_id)
            fn(item)
            return dict(item)

    def run_dir(self, item_id: str) -> Path:
        return self.runs_dir / item_id

    # ---- runner lock ------------------------------------------------------------------------
    def read_lock(self) -> dict | None:
        try:
            with open(self.lock_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("runner.lock unreadable (%s); treating it as stale", exc)
            return {}

    def _read_lock_settled(self) -> dict | None:
        """(2026-10-26, improve_core) read_lock() for a decision to REMOVE the lock. A runner creates
        runner.lock (O_EXCL) and only then writes its JSON, so a racing reader saw it empty and took it
        over (measured: two runners holding one queue). An empty or invalid lock younger than
        LOCK_SETTLE_S is re-read until its writer is done; an older one is stale at once, as before."""
        deadline = time.monotonic() + LOCK_SETTLE_S
        while True:
            try:
                text = self.lock_path.read_text(encoding="utf-8")
                age = time.time() - self.lock_path.stat().st_mtime
            except FileNotFoundError:
                return None
            except OSError:
                text, age = "", 0.0  # being created or deleted right now: look again
            try:
                if isinstance(json.loads(text), dict):
                    break
            except ValueError:
                pass
            if age > LOCK_SETTLE_S or time.monotonic() > deadline:
                break
            time.sleep(0.05)  # real sleep on purpose: the writer needs microseconds
        return self.read_lock()

    def runner_alive(self, alive: Callable[[int], bool] = pid_alive) -> dict | None:
        """The lock's info when its runner is alive (2026-10-26: the same process, lock_identity), else
        None. Read-only: a stale lock is left for `run` (takes it over) or `stop` (removes it)."""
        info = self.read_lock()
        if info and lock_owner_alive(info, alive):
            return info
        return None

    def remove_stale_lock(self, alive: Callable[[int], bool] = pid_alive) -> dict | None:
        """(2026-10-26, runner identity) Delete runner.lock when its runner is not alive (a dead or a
        reused PID); returns the removed lock's info ({} for an unreadable one), else None. Never signals
        any process. The lock is judged again right before the unlink, so a runner that took the lock
        meanwhile keeps it (the same read-then-unlink window as acquire_lock's takeover)."""
        info = self._read_lock_settled()  # (2026-10-26) never an empty lock still being written
        if info is None:
            return None
        ok, why = lock_identity(info, alive)
        if ok:
            return None
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            return None
        except OSError as exc:
            log.error("could not remove the stale runner.lock (pid %s): %s", info.get("pid"), exc)
            return None
        log.warning("removed a stale runner.lock (pid %s, started %s): %s", info.get("pid"),
                    info.get("started_at"), why)
        return info

    def acquire_lock(self, now: datetime | None = None, alive: Callable[[int], bool] = pid_alive) -> dict:
        self.ensure()
        info = {
            "pid": os.getpid(),
            "started_at": iso(now or datetime.now(timezone.utc)),
            "run_id": get_run_id(),
            # (2026-10-20, 0.4.4 logfix D, additive) the core version of this runner; an old lock has none
            "version": __version__,
            # (2026-10-26, runner identity, additive) this process's creation time (epoch seconds, UTC;
            # null when unreadable): with the pid it names this runner, so a reused PID is not "alive"
            "create_time": process_create_time(os.getpid()),
        }
        for _ in range(2):
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                old = self._read_lock_settled()  # (2026-10-26) never an empty lock still being written
                pid = old.get("pid") if old else None
                ok, why = lock_identity(old, alive)
                if ok:
                    raise LockHeld(
                        f"another runner is active (pid {pid}, started {old.get('started_at')}); "
                        f"lock file: {self.lock_path}"
                    )
                log.warning("taking over stale runner lock (pid %s, started %s): %s", pid,
                            (old or {}).get("started_at"), why)
                with contextlib.suppress(FileNotFoundError):
                    self.lock_path.unlink()
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(info, fh)
            log.info("runner lock acquired: %s", self.lock_path)
            return info
        raise LockHeld(f"could not acquire {self.lock_path} (another runner raced us)")

    # ---- graceful stop request (2026-10-13, SPEC_VSCODE section 2) -----------------------------
    def request_stop(self, now: datetime | None = None) -> None:
        self.ensure()
        info = {"requested_by_pid": os.getpid(),
                "requested_at": iso(now or datetime.now(timezone.utc))}
        write_text_atomic(self.stop_path, json.dumps(info))  # (2026-10-26) unique temp name + retry

    def stop_requested(self) -> bool:
        return self.stop_path.exists()

    # ---- (2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 2) the queue's hold flag ---------------
    def read_hold(self, now: datetime | None = None) -> dict | None:
        """None when the queue is not on hold, else {"since": ISO time or None}. Fails CLOSED: a hold
        file that cannot be read still holds the queue (only `hold off` removes it).
        (2026-10-26, usage forecast) a TIMED hold (`hold on --until`, "queue for after the reset") is
        {"since", "until"} before `until` and no hold at all from then on (`now`, default the wall
        clock); an `until` that cannot be read is ignored, so that hold stays (fails closed)."""
        try:
            with open(self.hold_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            log.warning("%s unreadable (%s); the queue stays on hold until `hold off`", self.hold_path.name, exc)
            return {"since": None}
        since = data.get("since") if isinstance(data, dict) else None
        held = {"since": since if isinstance(since, str) else None}
        until = _hold_until(data)
        if until is not None:
            if until <= (now or datetime.now(timezone.utc)):
                return None
            held["until"] = iso(until)
        # (2026-10-26, limit_continue) a usage-limit hold also says why and when the limit resets
        if isinstance(data, dict) and data.get("reason") == HOLD_REASON_LIMIT:
            held["reason"] = HOLD_REASON_LIMIT
            held["resets_at"] = data.get("resets_at") if isinstance(data.get("resets_at"), str) else None
            # (2026-10-27, integrate: limit_continue review LOW-2) a limit hold put over the owner's own hold
            # ("Start anyway") names it, so the limit's answer can give it back (release_limit_hold); additive
            if isinstance(data.get("owner_since"), str):
                held["owner_since"] = data["owner_since"]
                # (2026-10-27, integrate: hold kinds compose) the owner hold it replaced was a TIMED one
                if isinstance(data.get("owner_until"), str):
                    held["owner_until"] = data["owner_until"]
        return held

    def hold_key(self, now: datetime | None = None) -> tuple | None:
        """(2026-10-22, v049 tester LOW-9) None when the queue is not on hold, else what identifies THIS
        hold: its `since` and the file's mtime and size. A hold put on again, or a hold file written anew
        (`hold on` over an unreadable one), has another key, so `run --ignore-hold` can tell the hold it
        started past from a later one."""
        held = self.read_hold(now)
        if held is None:
            return None
        try:
            st = self.hold_path.stat()
            return (held.get("since"), st.st_mtime_ns, st.st_size)
        except OSError:
            return (held.get("since"), None, None)

    def set_hold(self, now: datetime | None = None, until: datetime | None = None, reason: str | None = None,
                 resets_at: str | None = None, over_owner: bool = False) -> dict:
        """Put the queue on hold (kept as it is when it already is); returns the hold.
        (2026-10-22, v049 tester LOW-9) an UNREADABLE hold file (no valid `since`) is written anew with a
        fresh since: `hold on` then always leaves a real hold, which a runner started past the unreadable
        one with --ignore-hold sees as a new hold. A folder in its place is kept (it still holds).
        (2026-10-26, limit_continue) `reason`/`resets_at`: a usage-limit hold (HOLD_REASON_LIMIT); a hold
        that is already on is kept as it is, so the owner's own hold is never turned into a limit hold.
        (2026-10-27, limit_continue fix MED-2) but a NEW usage limit over an old LIMIT hold (a runner started
        past it with --ignore-hold) writes it anew: a fresh since and the new reset, so the extension asks again.
        (2026-10-27, limit_continue fix round 2) `over_owner`: the runner was started past THIS owner hold
        ("Start anyway"); a usage limit then writes a limit hold in its place (the owner's since kept as
        `owner_since`), else no one is asked and "Release hold" starts nothing.
        (2026-10-26, usage forecast) `until`: a timed hold (see read_hold); a hold already on is kept as
        it is, an expired timed hold is written anew.
        (2026-10-27, fix LOW) a timed hold already on with an EARLIER until is extended to the new until
        (its since kept): "queue for after the reset" of the 7-day window must not end at the 5-hour one.
        (2026-10-27, fix round 2 MED) a plain hold (until None) over a timed one makes it indefinite (since
        kept, until dropped): an explicit hold must not end by itself at the usage reset.
        (2026-10-27, integrate: hold kinds compose) timed / owner / limit holds:
        - `until` only applies to a hold without a reason: `hold on [--until]` over a limit hold keeps it.
        - a usage limit over a TIMED hold that ends before the reset (or a reset that cannot be read) writes
          a limit hold in its place and keeps the timed hold as `owner_since` + `owner_until`, so the
          answer (release_limit_hold) gives it back; a timed hold that ends at or after the reset is kept:
          the owner already queued for after the reset (loop.limit_hold then waits it out).
        - the hold file is written with write_text_atomic (unique temp name + _replace_with_retry)."""
        held = self.read_hold(now)
        timed = held is not None and bool(held.get("until")) and not held.get("reason")
        over = reason == HOLD_REASON_LIMIT and held is not None and not self.hold_path.is_dir() and (
            held.get("reason") == HOLD_REASON_LIMIT or over_owner
            or (timed and not _ends_by(held["until"], resets_at)))
        extend = False
        if reason is None and timed and held.get("since"):
            # usage forecast: extend a live timed hold to a later until, or make it indefinite
            extend = until is None or parse_iso(held["until"]) < until
        if held is not None and (held.get("since") or self.hold_path.is_dir()) and not over and not extend:
            return held
        self.ensure()
        info = {"since": held["since"] if extend else iso(now or datetime.now(timezone.utc)),
                "by_pid": os.getpid()}
        if until is not None and reason is None:
            info["until"] = iso(until)
        if reason:
            info.update(reason=reason, resets_at=resets_at)
        if over and held.get("reason") != HOLD_REASON_LIMIT:
            info["owner_since"] = held.get("since")
            if held.get("until"):
                info["owner_until"] = held["until"]
        elif over and held.get("owner_since"):
            # (2026-10-27, integrate: review LOW-2) a new limit over a limit hold that replaced the owner's
            # hold keeps naming that owner hold, so the answer can still give it back
            info["owner_since"] = held["owner_since"]
            if held.get("owner_until"):
                info["owner_until"] = held["owner_until"]
        write_text_atomic(self.hold_path, json.dumps(info))  # (2026-10-26) unique temp name + retry
        return {k: info[k] for k in ("since", "until", "reason", "resets_at") if k in info}

    def release_limit_hold(self, now: datetime | None = None) -> dict | None:
        """(2026-10-27, integrate: limit_continue review LOW-2) `hold limit-off`: release only the usage-limit
        part of the hold and return the hold left in place (None = no hold).
        - A limit hold put over the owner's own hold ("Start anyway"; `owner_since`): that owner hold comes
          back with its own since. The extension then starts the runner past it (--ignore-hold), as "Start
          anyway" did, and the next automatic start skips the queue again.
        - (2026-10-27, integrate: hold kinds compose) that owner hold was TIMED (`owner_until`): it comes back
          with its until, or not at all once that until has passed (`now`, default the wall clock).
        - A plain limit hold is removed (as `hold off`).
        - Any other hold (the owner's own, or an unreadable one) is kept as it is: an answer given late, or
          to a hold already replaced, never releases the owner's hold."""
        held = self.read_hold(now)
        if held is None or held.get("reason") != HOLD_REASON_LIMIT:
            return held
        owner = held.get("owner_since")
        owner_until = _hold_until({"until": held.get("owner_until")}) if held.get("owner_until") else None
        if not owner or (owner_until is not None and owner_until <= (now or datetime.now(timezone.utc))):
            self.clear_hold()
            return None
        info = {"since": owner, "by_pid": os.getpid()}
        if owner_until is not None:
            info["until"] = iso(owner_until)
        write_text_atomic(self.hold_path, json.dumps(info))
        return {k: info[k] for k in ("since", "until") if k in info}

    def clear_hold(self) -> bool:
        """Release the hold; True if the queue was on hold.
        (2026-10-21, v049 tester fix LOW-8) a hold.json that cannot be removed (a directory, a locked
        file) is a StoreError naming the path; the queue stays on hold."""
        if self.hold_path.is_dir():
            raise StoreError(f"{self.hold_path} is a folder, not a hold file; the queue stays on hold "
                             "until that folder is removed by hand")
        try:
            self.hold_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StoreError(f"could not remove {self.hold_path} ({exc.strerror or exc}); the queue "
                             "stays on hold") from None

    # ---- tasks at once (2026-10-27, SPEC_V05_PARALLEL 1.2, 1.9) -----------------------------------------
    def read_tasks_at_once(self) -> int | None:
        """The lane's live "tasks at once" number, clamped to 1..TASKS_AT_ONCE_MAX; None when the file is
        missing or does not hold an integer (then the run's own setting applies)."""
        try:
            data = json.loads(read_text_retry(self.data_dir / TASKS_AT_ONCE_FILE))
        except (OSError, ValueError):
            return None
        n = data.get("n") if isinstance(data, dict) else None
        if not isinstance(n, int) or isinstance(n, bool):
            return None
        return max(1, min(TASKS_AT_ONCE_MAX, n))

    def write_tasks_at_once(self, n: int, now: datetime | None = None) -> int:
        """Store the number (clamped to 1..TASKS_AT_ONCE_MAX); returns what was written."""
        if not isinstance(n, int) or isinstance(n, bool):
            raise StoreError(f"tasks at once must be a whole number, not {n!r}")
        n = max(1, min(TASKS_AT_ONCE_MAX, n))
        self.ensure()
        write_text_atomic(self.data_dir / TASKS_AT_ONCE_FILE,
                          json.dumps({"v": 1, "n": n, "at": iso(now or datetime.now(timezone.utc))}))
        return n

    def clear_tasks_at_once(self) -> bool:
        """Delete the file; True if it existed."""
        try:
            (self.data_dir / TASKS_AT_ONCE_FILE).unlink()
            return True
        except FileNotFoundError:
            return False

    def read_parallel(self) -> dict | None:
        """The parallel runner's heartbeat; None when missing, torn or not an object."""
        try:
            data = json.loads(read_text_retry(self.data_dir / PARALLEL_FILE))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def write_parallel(self, data: dict) -> None:
        self.ensure()
        write_text_atomic(self.data_dir / PARALLEL_FILE, json.dumps(data, ensure_ascii=False))

    def project_dir_of(self) -> Path | None:
        return project_dir_of(self)

    def clear_stop_request(self) -> bool:
        """Delete stop.request; True if it existed. Errors other than 'missing' are logged."""
        try:
            self.stop_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            log.error("could not delete %s: %s", self.stop_path, exc)
            return False

    def release_lock(self) -> None:
        info = self.read_lock()
        if info is None:
            return
        if info.get("pid") != os.getpid():
            log.warning("runner.lock belongs to pid %s, not us; leaving it", info.get("pid"))
            return
        try:
            self.lock_path.unlink()
            log.info("runner lock released")
        except OSError as exc:
            log.error("could not remove runner.lock: %s", exc)

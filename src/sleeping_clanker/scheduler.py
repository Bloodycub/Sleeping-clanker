"""Tasks at once: the parallel runner of one lane (2026-10-27, SPEC_V05_PARALLEL sections 1, 3.1, 3.7, 5, 6).

One runner process per lane as before; inside it, while "tasks at once" N > 1 or an item still has a private
copy or a pending merge (parallel_wanted), a coordinator (the main thread), one Worker thread per running item
(at most N) and one Merger thread.

- The coordinator ticks every TICK_S: it never runs git, never takes the ProjectLock and never blocks longer
  than a tick. It reads the queue once per tick and publishes {id: status} for the workers' interrupt checks,
  releases due usage-limit waits, applies the limit gate / ramp / forecast cap, starts workers with
  order.pick, writes <lane>\\parallel.json and drives KeepAwake (which works per thread).
- A worker prepares the item's copy (Shadow.ensure_copy, under the ProjectLock), starts it (loop._start_item:
  attempts + 1 only after a good prepare), runs loop.run_one_attempt in the copy, checkpoints the copy after
  every outcome and records the outcome (loop.record_outcome; a done copy item gets merge "pending").
- The merger runs Shadow.merge jobs FIFO (deferred while an "alone" item runs, a waiting merge is retried every
  MERGE_RETRY_S) and Shadow.gc_if_due when it has nothing to do.

run_parallel returns BACK_TO_SERIAL when N is 1 again and nothing of a copy is left: run_loop then goes on
serially in the same process (N=1 without copy state is the old serial loop, unchanged).
"""

from __future__ import annotations

import collections
import contextlib
import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import loop as _loop
from . import order, providers, usage, worktree
from . import store as store_mod
from .clock import Clock, iso, parse_iso
from .log import get_logger
from .procutil import process_create_time
from .runner import LIMIT, StopRequested

log = get_logger(__name__)


class _BackToSerial:
    def __repr__(self) -> str:
        return "BACK_TO_SERIAL"


# run_parallel's "hand the lane back to the serial loop" result (an object, never an exit code)
BACK_TO_SERIAL = _BackToSerial()

# parallel.json "why" codes: why fewer than N items run (SPEC_V05_PARALLEL 1.9)
WHY_CODES = ("limit-gate", "ramp", "forecast", "alone-barrier", "waits-after", "family", "goal", "hold",
             "prepare-backoff", "nothing-pending")
REASON_CODES = worktree.RUNNER_GATES   # parallel.json "reason" in mode in-place

TICK_S = 1.0                 # coordinator tick (real time)
BEAT_EVERY_S = 15.0          # parallel.json is rewritten at least this often
STOP_JOIN_S = 10.0           # a stop joins every thread for at most this long in total
RAMP_S = 90.0                # the canary runs this long without a limit before N comes back (6.3)
PREPARE_BACKOFF_S = 60.0     # a busy prepare is tried again after this long (1.4)
PREPARE_TRIES = 3            # busy prepares before the item runs alone / recreates before it blocks
TRANSIENT_KINDS = ("busy", "timeout", "lock")
MERGE_DEFER_S = 5.0          # a merge job waits this long while an "alone" item runs (1.5)
MERGE_RETRY_S = 30.0         # a waiting merge is tried again after this long
MERGE_JOB_TRIES = 3          # a merge job that raises this often in a row asks the owner (kind content)
MERGE_WAIT_MAX_S = 600.0     # --until-empty waits this long for waiting merges, then asks (3.7)
FORECAST_FRESH_S = 1800.0    # a five_hour sample older than this is not used (6.4)
FORECAST_CACHE_S = 15.0      # usage_samples.jsonl is read at most this often
LIVE_STATES = ("creating", "active", "removing", "missing")
RECREATE_QUESTION = ("Its private copy could not be recreated ({kind}). Reply to try again, or run it in the "
                     "project folder with `edit --in-place`.")


# ---- live N and dispatch (1.1, 1.2) ----------------------------------------------------------------------
def live_n(store, opts) -> int:
    """The lane's "tasks at once": <lane>\\tasks_at_once.json, else the run's opts.tasks_at_once; clamped to
    1..store.TASKS_AT_ONCE_MAX."""
    n = store.read_tasks_at_once()
    if n is None:
        n = getattr(opts, "tasks_at_once", 1)
    if not isinstance(n, int) or isinstance(n, bool):
        n = 1
    return max(1, min(store_mod.TASKS_AT_ONCE_MAX, n))


def parallel_wanted(store, items: list[dict], opts) -> bool:
    """(1.1) True when the lane needs the parallel runner: N > 1, or any item still has copy state (a live
    worktree record, a merge pending/waiting/merging, or an unfinished item with a record not removed), so
    lowering N to 1 never strands a copy or a merge."""
    if live_n(store, opts) > 1:
        return True
    for it in items:
        rec = it.get("worktree")
        state = rec.get("state") if isinstance(rec, dict) else None
        if state in LIVE_STATES:
            return True
        if (it.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE:
            return True
        if it.get("status") not in ("done", "cancelled") and isinstance(rec, dict) and rec and state != "removed":
            return True
        # (2026-10-27, fix round 1) a removed copy whose session lives on (a reply to a merged task): spec 4.6
        # recreates W at the same path so --resume finds it; order.item_mode's "copy" branch for the same item
        if (it.get("status") not in ("done", "cancelled") and isinstance(rec, dict) and rec.get("path")
                and state == "removed" and it.get("session_id")):
            return True
    return False


def run_parallel(store, opts, claude_cmd: list[str], clock: Clock, awake, should_stop: Callable[[], bool] | None,
                 *, ignored_hold: tuple | None = None,
                 claude_resolver: Callable[[], list[str]] | None = None) -> int | _BackToSerial:
    """(1.1) Run the lane with the coordinator until the queue is done (--until-empty), a hold stops it, a stop
    request (StopRequested propagates after every thread stopped, so run_loop deletes stop.request and exits
    0), or BACK_TO_SERIAL. `ignored_hold` and `claude_resolver` are run_loop's."""
    coord = Coordinator(store, opts, claude_cmd, clock, awake, should_stop, ignored_hold=ignored_hold,
                        claude_resolver=claude_resolver)
    return coord.run()


# ---- limit gate files (6.1) -------------------------------------------------------------------------------
def _gate_path(base_dir, hash8: str, lane: str) -> Path:
    return Path(base_dir) / store_mod.LIMIT_GATE_DIR / f"{hash8}-{lane}.json"


def _epoch(value) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def write_limit_gate(base_dir, hash8: str, lane: str, reset_at) -> None:
    """This runner's own gate file <base>\\limit_gate\\<hash8>-<lane>.json {"v", "reset_at", "at", "pid"}: no
    runner of this account starts a new task before reset_at. Each runner writes only its own file."""
    reset = _epoch(reset_at)
    if base_dir is None or reset is None:
        return
    path = _gate_path(base_dir, hash8, lane)
    path.parent.mkdir(parents=True, exist_ok=True)
    store_mod.write_text_atomic(path, json.dumps({"v": 1, "reset_at": reset, "at": time.time(),
                                                  "pid": os.getpid()}))


def clear_limit_gate(base_dir, hash8: str, lane: str) -> bool:
    """Delete this runner's own gate file; True when one was there."""
    if base_dir is None:
        return False
    try:
        _gate_path(base_dir, hash8, lane).unlink()
        return True
    except OSError:
        return False


def _read_gate(path: Path) -> float | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _epoch(data.get("reset_at")) if isinstance(data, dict) else None


def limit_gate_until(base_dir, now: float) -> float | None:
    """The latest reset_at in the future over every runner's gate file; None = no gate (past ones are
    ignored, unreadable ones too)."""
    if base_dir is None:
        return None
    try:
        files = list((Path(base_dir) / store_mod.LIMIT_GATE_DIR).glob("*.json"))
    except OSError:
        return None
    resets = [r for r in (_read_gate(f) for f in files) if r is not None and r > now]
    return max(resets) if resets else None


# ---- forecast cap (6.4) and ramp (6.3) ---------------------------------------------------------------------
def forecast_cap(n: int, base_dir, now: float) -> int:
    """At most n tasks at once by the latest fresh five_hour usage sample: utilization < 0.80 -> n, < 0.90 ->
    ceil(n / 2), else 1. A sample older than FORECAST_FRESH_S, one whose window has reset, or none -> n."""
    if base_dir is None or n <= 1:
        return max(1, n)
    try:
        s = usage.summary(Path(base_dir), now)
    except Exception as exc:   # never stop the runner over the samples file
        log.debug("forecast cap: usage samples unreadable: %s", exc)
        return n
    latest = ((s or {}).get("latest") or {}).get("five_hour")
    if not isinstance(latest, dict):
        return n
    t, u = latest.get("t"), latest.get("utilization")
    if not isinstance(t, (int, float)) or not isinstance(u, (int, float)):
        return n
    if now - t > FORECAST_FRESH_S or (latest.get("resets_at") or 0) <= now:
        return n
    if u < 0.80:
        return n
    if u < 0.90:
        return max(1, math.ceil(n / 2))
    return 1


class Ramp:
    """(6.3) After a limit gate releases only one task starts (the canary); N comes back once the canary has
    run RAMP_S without a limit, or its attempt ended with another kind. A limit of the canary keeps the ramp
    armed (its wait re-arms the gate). `now` is epoch seconds."""

    def __init__(self, now: Callable[[], float] = time.time, hold_s: float | None = None) -> None:
        self._now = now
        self.hold_s = RAMP_S if hold_s is None else hold_s
        self.armed = False
        self.canary: str | None = None
        self.canary_t: float | None = None

    def release(self) -> None:
        self.armed, self.canary, self.canary_t = True, None, None
        log.info("usage-limit gate released: one task first (the canary), then up to tasks at once")

    def on_start(self, item_id: str) -> None:
        if self.armed and self.canary is None:
            self.canary, self.canary_t = item_id, self._now()

    def on_outcome(self, item_id: str, kind: str) -> None:
        if not self.armed or item_id != self.canary:
            return
        if kind == LIMIT:
            self.canary, self.canary_t = None, None
            return
        self.armed, self.canary, self.canary_t = False, None, None

    def cap(self, n: int, now: float) -> int:
        if not self.armed:
            return n
        if self.canary_t is not None and now - self.canary_t >= self.hold_s:
            self.armed, self.canary, self.canary_t = False, None, None
            return n
        return min(n, 1)


# ---- helpers -----------------------------------------------------------------------------------------------
def _lane_name(store) -> str:
    proj = store_mod.project_dir_of(store)
    if proj is not None and os.path.normcase(os.path.abspath(proj)) == os.path.normcase(
            os.path.abspath(store.data_dir)):
        return store_mod.DEFAULT_LANE
    return Path(store.data_dir).name or store_mod.DEFAULT_LANE


def _hash8(store) -> str:
    proj = store_mod.project_dir_of(store)
    return proj.name.rsplit("-", 1)[-1] if proj is not None else "local"


def _subscription(item: dict, opts) -> bool:
    """The attempt ran on the subscription (the rule usage.recorder follows): its own provider, else the run's."""
    name = ((item.get("provider") or "").strip() or (getattr(opts, "provider", None) or "")
            or providers.DEFAULT_PROVIDER).strip().lower()
    return name == providers.DEFAULT_PROVIDER


def _find(items: list[dict], item_id: str) -> dict | None:
    return next((i for i in items if i.get("id") == item_id), None)


# ---- worker (1.4) -------------------------------------------------------------------------------------------
class Worker(threading.Thread):
    """One running item: prepare (copy mode) -> start -> attempt -> checkpoint -> outcome. Every store or
    shadow step runs here, never on the coordinator."""

    def __init__(self, coord: "Coordinator", item_id: str, mode: str, recreating: bool = False,
                 note: str | None = None, claude_cmd: list[str] | None = None) -> None:
        super().__init__(name=f"sc-worker-{item_id[:8]}", daemon=True)
        self.coord = coord
        # (2026-10-27) the command resolved for THIS start: a later start's resolve never changes it
        self.claude_cmd = list(claude_cmd if claude_cmd is not None else coord.claude_cmd)
        self.item_id = item_id
        self.mode = mode
        self.recreating = recreating
        self.note = note
        self.phase = "prepare"
        self.since = iso(coord.clock.now())

    def run(self) -> None:
        try:
            self._work()
        except BaseException as exc:   # never a silent death: the item is put back by the next runner
            log.exception("item %s: worker ended with %s", self.item_id, type(exc).__name__)
        finally:
            self.coord.wake.set()

    def _interrupt(self) -> str | None:
        status = self.coord.status.get(self.item_id)
        return status if status in _loop.INTERRUPT_STATUSES else None

    def _prepare(self) -> bool:
        c = self.coord
        try:
            if c.shadow is None:
                raise worktree.WorktreeError("gate", "no project root", code="no-root")
            c.shadow.ensure_copy(c.store, self.item_id)
            return True
        except worktree.WorktreeError as exc:
            log.warning("item %s: its private copy could not be made (%s%s): %s", self.item_id, exc.kind,
                        f" {exc.code}" if exc.code else "", exc.detail)
            c.events.put(("prepare-failed", self.item_id, exc.kind, exc.code, self.recreating))
        except Exception:
            log.exception("item %s: preparing its private copy failed", self.item_id)
            c.events.put(("prepare-failed", self.item_id, "failed", None, self.recreating))
        return False

    def _checkpoint(self) -> None:
        """(1.4 step 4) refs/sc/ckpt/<id> after every outcome, so a crash, limit or stop never loses the copy's
        state; uses the quiet shadow (a stop does not abort it)."""
        c = self.coord
        if self.mode != "copy" or c.quiet_shadow is None:
            return
        try:
            item = _find(c.store.load(), self.item_id)
            if item and (item.get("worktree") or {}).get("state") == "active":
                c.quiet_shadow.checkpoint(item)
        except Exception as exc:   # a failed checkpoint never loses the outcome: W itself is kept
            log.warning("item %s: checkpoint of its copy failed: %s", self.item_id, exc)

    def _work(self) -> None:
        c = self.coord
        if self.mode == "copy" and not self._prepare():
            return
        try:
            item = _loop._start_item(c.store, self.item_id, c.clock)
        except Exception as exc:
            log.error("item %s: could not be started: %s", self.item_id, exc)
            return
        if item is None:
            return
        c.events.put(("started", self.item_id))
        self.phase, self.since = "attempt", iso(c.clock.now())
        cwd = env_patch = note = None
        if self.mode == "copy" and (item.get("worktree") or {}).get("state") == "active":
            cwd = worktree.effective_cwd(item)
            env_patch = worktree.attempt_env_patch(item)
            note = (item.get("worktree") or {}).get("system_note")
        try:
            out = _loop.run_one_attempt(c.store, item, c.opts, self.claude_cmd, c.clock, cwd=cwd, env_patch=env_patch,
                                        system_note=note, should_stop=c.stop_event.is_set,
                                        should_interrupt=self._interrupt)
        except StopRequested:
            # (2026-10-27, fix round 1) pending first: the checkpoint may wait up to LOCK_WAIT_S for the
            # ProjectLock and outlast STOP_JOIN_S; W is kept either way and nothing starts after a stop
            _loop._back_to_pending(c.store, self.item_id, "stop requested; session kept")
            self._checkpoint()
            return
        self._checkpoint()
        if self.mode == "copy":
            out = worktree.outcome_to_project(out, item)
        if out.kind == LIMIT and out.reset_at is not None and _subscription(item, c.opts):
            try:
                write_limit_gate(c.gate_base, c.hash8, c.lane, out.reset_at)
            except OSError as exc:
                log.warning("limit gate file not written: %s", exc)
        ok = _loop.record_outcome(c.store, self.item_id, out, c.opts, c.clock)
        if not ok:
            c.unsaved.put((self.item_id, out))
        else:
            try:
                now_item = _find(c.store.load(), self.item_id)
            except Exception:
                now_item = None
            if now_item and (now_item.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE:
                c.merger.enqueue(self.item_id)
        c.events.put(("outcome", self.item_id, out.kind, ok))


# ---- merger (1.5, 3.7) ---------------------------------------------------------------------------------------
class Merger(threading.Thread):
    """One per runner: Shadow.recover_journals and the availability check at start, then merge jobs FIFO."""

    def __init__(self, coord: "Coordinator") -> None:
        super().__init__(name="sc-merger", daemon=True)
        self.coord = coord
        self.cv = threading.Condition()
        self.jobs: "collections.OrderedDict[str, float]" = collections.OrderedDict()
        self.waiting: set[str] = set()
        self.current: str | None = None
        self.fails: collections.Counter = collections.Counter()
        self.ready = threading.Event()
        self._halt = threading.Event()
        self._give_up = threading.Event()

    # -- the coordinator's side
    def enqueue(self, item_id: str, delay: float = 0.0) -> None:
        if item_id in self.coord.unsettled:   # (2026-10-27, fix round 1) left for the next runner
            # (2026-10-28, F2) unless its journal was settled since (every Shadow.merge runs recover_journals
            # first, e.g. an owner's `merge --retry`): then its pending merge is this runner's job again
            if self._journal_open(item_id):
                return
            self.coord.unsettled.discard(item_id)
        with self.cv:
            if item_id == self.current or item_id in self.jobs:
                return
            self.jobs[item_id] = time.monotonic() + delay
            self.cv.notify_all()

    def idle(self, ignore_waiting: bool = False) -> bool:
        with self.cv:
            if self.current is not None:
                return False
            if ignore_waiting:
                return all(i in self.waiting for i in self.jobs)
            return not self.jobs

    def stop(self) -> None:
        self._halt.set()
        with self.cv:
            self.cv.notify_all()

    def give_up_waiting(self) -> None:
        self._give_up.set()
        with self.cv:
            self.cv.notify_all()

    # -- the thread
    def run(self) -> None:
        try:
            self._startup()
        except Exception:
            log.exception("merger start failed")
        finally:
            self.ready.set()
            self.coord.wake.set()
        while not self._halt.is_set():
            if self._give_up.is_set():
                self._give_up.clear()
                self._ask_waiting()
            job = self._take()
            if job is None:
                continue
            try:
                self._run_job(job)
            except BaseException as exc:
                log.exception("item %s: merge job failed (%s)", job, type(exc).__name__)
                self._job_failed(job)
            finally:
                with self.cv:
                    self.current = None
                self.coord.wake.set()
            if self.idle() and not self._halt.is_set():
                self._gc()

    def _startup(self) -> None:
        c = self.coord
        sh = c.quiet_shadow
        if sh is None:
            c.set_availability("no-root")
            return
        try:
            code = sh.availability()
        except Exception as exc:
            log.error("private copies unavailable: %s", exc)
            code = "shadow-broken"
        c.set_availability(code)
        if code in (None, "shadow-broken"):
            try:
                n = sh.recover_journals(c.store)
                if n:
                    log.info("settled %d merge journal(s) from an earlier run", n)
            except Exception as exc:
                log.error("merge journals could not be settled: %s", exc)
        for it in c.store.load():
            if (it.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE:
                self.enqueue(it["id"])

    def _take(self) -> str | None:
        with self.cv:
            if self._halt.is_set() or self._give_up.is_set():
                return None
            now = time.monotonic()
            for item_id, due in self.jobs.items():
                if due <= now:
                    del self.jobs[item_id]
                    self.waiting.discard(item_id)
                    self.current = item_id
                    return item_id
            nxt = min(self.jobs.values(), default=now + 1.0)
            self.cv.wait(timeout=max(0.05, min(1.0, nxt - now)))
            return None

    def _requeue(self, item_id: str, delay: float, waiting: bool = False) -> None:
        with self.cv:
            self.jobs.pop(item_id, None)
            self.jobs[item_id] = time.monotonic() + delay
            if waiting:
                self.waiting.add(item_id)
            self.cv.notify_all()

    def _run_job(self, item_id: str) -> None:
        c = self.coord
        if c.alone_now:
            self._requeue(item_id, MERGE_DEFER_S)
            return
        item = _find(c.store.load(), item_id)
        m = (item or {}).get("merge") or {}
        if item is None or m.get("state") not in worktree.MERGE_ACTIVE or c.quiet_shadow is None:
            return
        res = c.quiet_shadow.merge(c.store, item_id, m.get("gen"))
        self.fails.pop(item_id, None)
        log.info("item %s: merge %s", item_id, res)
        if res == "waiting":
            self._requeue(item_id, MERGE_RETRY_S, waiting=True)

    def _job_failed(self, item_id: str) -> None:
        self.fails[item_id] += 1
        if self.fails[item_id] >= MERGE_JOB_TRIES:
            self.fails.pop(item_id, None)
            self._ask(item_id, "content")
            return
        self._requeue(item_id, MERGE_RETRY_S, waiting=True)

    def _ask(self, item_id: str, kind: str) -> None:
        """Turn an unfinished merge into a merge question (status blocked, nothing written): kind repo-busy
        for a merge still waiting when --until-empty would exit (3.7), content after repeated job errors."""
        c = self.coord
        if self._journal_open(item_id):
            # (2026-10-27, fix round 1) P may be half-merged (a rollback failed): never "not merged yet" +
            # discard over it; the next runner's recover_journals settles it, this one leaves it alone
            log.error("item %s: a merge journal is not settled yet; it stays waiting for the next runner", item_id)
            c.unsettled.add(item_id)
            return

        def fn(it: dict) -> None:
            m = dict(it.get("merge") or {})
            if m.get("state") not in worktree.MERGE_ACTIVE:
                return
            wait = m.get("wait")
            m.update(state="conflict", kind=kind, paths=[], wait=None)
            it["merge"] = m
            it["status"] = "blocked"
            it["finished_at"] = iso(c.clock.now())
            it["blocked_question"] = worktree.merge_question(it, kind, [], {"wait": wait} if wait else None)
            log.warning("item %s: running -> blocked (its changes are not merged: %s)", it["id"], kind)
        try:
            with c.quiet_shadow.lock.hold("merge"):
                c.store.mutate(item_id, fn)
        except Exception as exc:
            log.error("item %s: could not ask about its merge: %s", item_id, exc)

    def _journal_open(self, item_id: str) -> bool:
        """(2026-10-27, fix round 1) The item's merge journal D\\merge_active\\<id>.json is still there."""
        sh = self.coord.quiet_shadow
        if sh is None:
            return False
        try:
            return sh._jpath(item_id).exists()
        except Exception as exc:   # unknown: ask as before
            log.debug("item %s: merge journal not checked: %s", item_id, exc)
            return False

    def _ask_waiting(self) -> None:
        with self.cv:
            ids = list(self.jobs)
            for i in ids:
                self.jobs.pop(i, None)
            self.waiting.clear()
        for item_id in ids:
            log.warning("item %s: its merge is still waiting after %.0f min; the runner exits and asks",
                        item_id, MERGE_WAIT_MAX_S / 60)
            self._ask(item_id, "repo-busy")
        self.coord.wake.set()

    def _gc(self) -> None:
        sh = self.coord.quiet_shadow
        if sh is None or self.coord.reason is not None:
            return
        try:
            sh.gc_if_due()
        except Exception as exc:
            log.warning("shadow gc failed: %s", exc)


# ---- coordinator (1.3) ---------------------------------------------------------------------------------------
class Coordinator:
    """The main thread of a parallel run; see the module docstring. Runs no git and takes no ProjectLock."""

    def __init__(self, store, opts, claude_cmd: list[str], clock: Clock, awake, should_stop, *,
                 ignored_hold: tuple | None = None, claude_resolver: Callable[[], list[str]] | None = None) -> None:
        self.store, self.opts, self.claude_cmd, self.clock = store, opts, claude_cmd, clock
        self.awake = awake
        self.should_stop = should_stop
        self.ignored_hold = ignored_hold
        self.claude_resolver = claude_resolver
        self.tries = _loop.STORE_RETRIES if opts.forever else 1
        self.stop_event = threading.Event()
        self.lane = _lane_name(store)
        self.hash8 = _hash8(store)
        proj = store_mod.project_dir_of(store)
        self.gate_base = store_mod.base_dir_of(proj) if proj is not None else getattr(opts, "usage_dir", None)
        # workers' prepares stop with the runner; checkpoints and merges finish their step (the journal covers
        # a kill): two Shadow objects of the one project share its ProjectLock
        self.shadow = worktree.shadow_for(store, self.stop_event)
        self.quiet_shadow = worktree.shadow_for(store)
        self.status: dict[str, str | None] = {}
        self.workers: dict[str, Worker] = {}
        self.alone_now = False
        self.events: queue.Queue = queue.Queue()
        self.unsaved: queue.Queue = queue.Queue()
        self.wake = threading.Event()
        self.ramp = Ramp(lambda: self.clock.now().timestamp())
        self.backoff: dict[str, datetime] = {}
        self.alone_ids: set[str] = set()
        self.notes: dict[str, str] = {}
        self.fails: collections.Counter = collections.Counter()
        self.reason: str | None = None
        self.copy_ok = False
        self.merger = Merger(self)
        self.hold_exit = False
        self.ask_windows: set = set()
        self.gate_on = False
        self.n = 1
        self.n_eff = 1
        self.why: dict | None = None
        self._beat_key: str | None = None
        self._beat_t = float("-inf")
        self._exit_wait_since: float | None = None
        self._forecast = (float("-inf"), 0, 0)
        self._shut = False
        self._tick_fails = 0                # (2026-10-27, fix round 1) store errors of ticks in a row
        self.unsettled: set[str] = set()    # (2026-10-27, fix round 1) merge journals left for the next runner

    # -- called from the merger thread
    def set_availability(self, code: str | None) -> None:
        self.reason = code
        self.copy_ok = code is None
        if code:
            log.warning("tasks at once: no private copies (%s); items run one at a time in the project folder", code)

    @property
    def mode(self) -> str:
        return "in-place" if self.reason else "copies"

    # -- the loop
    def run(self) -> int | _BackToSerial:
        log.info("parallel runner: lane %s, tasks at once %d", self.lane, live_n(self.store, self.opts))
        self.merger.start()
        try:
            while True:
                try:
                    rc = self.tick()
                except (store_mod.StoreError, OSError) as exc:
                    # (2026-10-27, fix round 1) a queue read/write error of one tick (a busy mutex, an antivirus
                    # holding queue.json) never kills the running attempts: keep the last published status
                    # and tick again; only an idle runner, or STORE_RETRIES failed ticks in a row, gives up
                    busy = bool(self.workers) or not self.merger.idle()
                    self._tick_fails += 1
                    if not busy or self._tick_fails >= _loop.STORE_RETRIES:
                        raise
                    log.error("queue store error in a coordinator tick (%d/%d in a row): %s: %s; the running "
                              "tasks go on", self._tick_fails, _loop.STORE_RETRIES, type(exc).__name__, exc)
                    self.wake.wait(TICK_S)
                    self.wake.clear()
                    continue
                self._tick_fails = 0
                if rc is not None:
                    return rc
                self.wake.wait(TICK_S)
                self.wake.clear()
        finally:
            self.shutdown()

    def tick(self):
        clock, store = self.clock, self.store
        _loop._check_stop(self.should_stop)
        now = clock.now()
        self._reap()
        self._drain_events(now)
        # c. outcomes the queue could not take, then the kept ones (as run_loop)
        limits = self._retry_unsaved()
        _loop._retry_store(lambda: _loop.apply_pending_outcomes(store, self.opts, clock, limits),
                           "applying kept outcomes", clock, self.should_stop, self.tries)
        for item_id in limits:
            self._ask_hold(item_id)
        # b. holds: a timed hold is waited out, an owner/limit hold stops new starts and exits when idle
        timed = _loop.hold_until(store, self.ignored_hold, now)
        if timed is None and not self.hold_exit and _loop.hold_stops(store, self.ignored_hold, now):
            self.hold_exit = True
        # d. one read per tick, published for the workers
        items = _loop._retry_store(store.load, "reading the queue", clock, self.should_stop, self.tries)
        self.status = {i["id"]: i.get("status") for i in items}
        # e. due usage-limit waits go back to pending
        if any(i.get("status") == "waiting" and (parse_iso(i.get("wait_until")) or now) <= now for i in items):
            _loop._retry_store(lambda: _loop.release_due_waiting(store, now), "releasing due usage-limit waits",
                               clock, self.should_stop, self.tries)
            items = _loop._retry_store(store.load, "reading the queue", clock, self.should_stop, self.tries)
            self.status = {i["id"]: i.get("status") for i in items}
        # (2026-10-28, F2) every tick, each pending/waiting/merging merge that is not a merger job yet (enqueue
        # skips those): an owner's `merge --retry` / `--now` whose own lock wait timed out stays pending, and a
        # merge an earlier run left; neither may be stranded until the next runner
        for it in items:
            if (it.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE and self.merger.ready.is_set():
                self.merger.enqueue(it["id"])
        # f. the limit gate
        ts = now.timestamp()
        limit_until = self._limit_until(items, now)
        gate = "hold" if (self.hold_exit or timed is not None) else ("limit-gate" if limit_until else None)
        if self.gate_on and not limit_until:
            self.ramp.release()
        self.gate_on = bool(limit_until)
        # g. how many may run
        self.n = live_n(store, self.opts)
        ramp_cap = self.ramp.cap(self.n, ts)
        fc = self._forecast_cap(self.n, ts)
        self.n_eff = max(1, min(self.n, ramp_cap, fc))
        # h. starts
        if self.merger.ready.is_set() and gate is None:
            free = self.n_eff - len(self.workers)
            while free > 0:
                ctx = self._ctx(items, now, gate)
                item = order.pick(items, ctx)
                if item is None:
                    break
                self._start(item, ctx)
                free -= 1
        # KeepAwake (per thread: only here)
        if self.workers or not self.merger.idle() or limit_until or timed is not None:
            self.awake.on("tasks are running or waiting")
        else:
            self.awake.off("queue idle")
        # i. heartbeat
        self.why = self._why(items, now, gate, limit_until, ramp_cap, fc)
        self._beat(items, now)
        # j. exit
        return self._exit(items, now, timed)

    # -- pieces of a tick
    def _reap(self) -> None:
        for item_id, w in list(self.workers.items()):
            if not w.is_alive():
                w.join(0)
                del self.workers[item_id]
        self.alone_now = any(w.mode == "alone" for w in self.workers.values())

    def _drain_events(self, now: datetime) -> None:
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return
            if ev[0] == "started":
                self.ramp.on_start(ev[1])
            elif ev[0] == "outcome":
                _kind, item_id, kind, ok = ev
                self.ramp.on_outcome(item_id, kind)
                if kind == LIMIT and ok:
                    self._ask_hold(item_id)
            elif ev[0] == "prepare-failed":
                self._prepare_failed(ev[1], ev[2], ev[3], ev[4], now)

    def _prepare_failed(self, item_id: str, kind: str, code: str | None, recreating: bool, now: datetime) -> None:
        if kind == "stopped":
            return
        if code in worktree.RUNNER_GATES:
            self.set_availability(code)
            return
        if recreating and kind in TRANSIENT_KINDS:
            # (2026-10-27, fix round 1) a busy ProjectLock (another copy, a merge, the weekly gc) is not a
            # failed recreate: try again later, never block the item over it
            self.backoff[item_id] = now + timedelta(seconds=PREPARE_BACKOFF_S)
            return
        self.fails[item_id] += 1
        if recreating:
            if self.fails[item_id] >= PREPARE_TRIES:
                self.fails.pop(item_id, None)
                self._block_recreate(item_id, code or kind)
            else:
                self.backoff[item_id] = now + timedelta(seconds=PREPARE_BACKOFF_S)
            return
        if kind in TRANSIENT_KINDS and self.fails[item_id] < PREPARE_TRIES:
            self.backoff[item_id] = now + timedelta(seconds=PREPARE_BACKOFF_S)
            return
        self.fails.pop(item_id, None)
        self.backoff.pop(item_id, None)
        self.alone_ids.add(item_id)
        self.notes[item_id] = f"could not make a copy: {code or kind}"
        log.warning("item %s: runs alone in the project folder this run (%s)", item_id, self.notes[item_id])

    def _block_recreate(self, item_id: str, kind: str) -> None:
        text = RECREATE_QUESTION.format(kind=kind)

        def fn(it: dict) -> None:
            if it.get("status") != "pending":
                return
            it["blocked_question"] = {"text": text, "options": []}
            it["error"] = f"blocked: {text}"
            it["finished_at"] = iso(self.clock.now())
            _loop.transition(it, "blocked", text)
        try:
            self.store.mutate(item_id, fn)
        except Exception as exc:
            log.error("item %s: could not block it: %s", item_id, exc)

    def _retry_unsaved(self) -> list[str]:
        limits: list[str] = []
        still = []
        while True:
            try:
                item_id, out = self.unsaved.get_nowait()
            except queue.Empty:
                break
            if not _loop.record_outcome(self.store, item_id, out, self.opts, self.clock):
                still.append((item_id, out))
            else:
                if out.kind == LIMIT:
                    limits.append(item_id)
                self._enqueue_if_merging(item_id)
        for pair in still:
            self.unsaved.put(pair)
        return limits

    def _enqueue_if_merging(self, item_id: str) -> None:
        try:
            it = _find(self.store.load(), item_id)
        except Exception:
            return
        if it and (it.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE:
            self.merger.enqueue(item_id)

    def _ask_hold(self, item_id: str) -> None:
        """(6.2) --on-limit ask: the first limit of a reset window puts the queue on a limit hold
        (loop.limit_hold); no new starts, the runner exits when idle (merges go on)."""
        if self.opts.on_limit != _loop.ON_LIMIT_ASK:
            return
        try:
            it = _find(self.store.load(), item_id)
        except Exception:
            it = None
        window = (it or {}).get("wait_until")
        if window in self.ask_windows:
            return
        self.ask_windows.add(window)
        if _loop.limit_hold(self.store, item_id, self.clock, self.ignored_hold):
            self.hold_exit = True

    def _limit_until(self, items: list[dict], now: datetime) -> float | None:
        """(6.1) The end of the limit gate: a `waiting` item of the lane with a future wait_until, or any
        runner's gate file; this runner's own past file is deleted."""
        ts = now.timestamp()
        until = [w.timestamp() for w in (parse_iso(i.get("wait_until")) for i in items if i.get("status") == "waiting")
                 if w is not None and w > now]
        files = limit_gate_until(self.gate_base, ts)
        if files is not None:
            until.append(files)
        if self.gate_base is not None:
            own = _read_gate(_gate_path(self.gate_base, self.hash8, self.lane))
            if own is not None and own <= ts:
                clear_limit_gate(self.gate_base, self.hash8, self.lane)
        return max(until) if until else None

    def _forecast_cap(self, n: int, ts: float) -> int:
        at, cached_n, cap = self._forecast
        if cached_n == n and ts - at < FORECAST_CACHE_S and ts >= at:
            return cap
        cap = forecast_cap(n, getattr(self.opts, "usage_dir", None), ts)
        self._forecast = (ts, n, cap)
        return cap

    def _ctx(self, items: list[dict], now: datetime, gate: str | None) -> order.PickContext:
        """(2026-10-27) ctx.n_eff is the LIVE N: order.item_mode's "n_eff 1 -> alone" means "tasks at once is 1".
        A ramp or forecast cap of 1 only limits how many start (free = self.n_eff - workers, in tick): the
        canary must run in a copy, else it would run alone and nothing could start beside it until it ENDS
        (6.3; "ramp until the canary ends" is rejected)."""
        merging = {i["id"] for i in items if (i.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE}
        return order.PickContext(running=set(self.workers), merging=merging, alone_running=self.alone_now,
                                 merge_due=not self.merger.idle(ignore_waiting=True), n_eff=self.n, now=now,
                                 copy_ok=self.copy_ok, alone_ids=set(self.alone_ids), backoff=dict(self.backoff),
                                 gate=gate)

    def _start(self, item: dict, ctx: order.PickContext) -> None:
        mode = order.item_mode(item, ctx)
        if self.claude_resolver is not None:
            self.claude_cmd = self.claude_resolver()   # (0.4.1) before the item starts, as run_loop
        rec = item.get("worktree")
        recreating = isinstance(rec, dict) and bool(rec.get("path")) and (
            rec.get("state") != "removed" or bool(item.get("session_id")))
        w = Worker(self, item["id"], mode, recreating=recreating, note=self.notes.get(item["id"]),
                   claude_cmd=self.claude_cmd)
        self.workers[item["id"]] = w
        self.alone_now = self.alone_now or mode == "alone"
        log.info("item %s: starting (%s)", item["id"], mode)
        w.start()

    def _why(self, items: list[dict], now: datetime, gate: str | None, limit_until: float | None,
             ramp_cap: int, fc: int) -> dict | None:
        if len(self.workers) >= self.n:
            return None
        if gate == "hold":
            return {"code": "hold", "args": {}}
        if gate == "limit-gate":
            return {"code": "limit-gate",
                    "args": {"until": iso(datetime.fromtimestamp(limit_until, timezone.utc)) if limit_until else None}}
        if self.n_eff < self.n and len(self.workers) >= self.n_eff:
            return {"code": "ramp" if ramp_cap < self.n else "forecast", "args": {"cap": self.n_eff}}
        ctx = self._ctx(items, now, None)
        for it in _loop.runnable_pending(items):
            if it["id"] in self.workers:
                continue
            block = order.start_block(it, items, ctx)
            if block and block["code"] in WHY_CODES:
                args = dict(block.get("args") or {})
                if block["code"] == "alone-barrier" and args.get("id8"):
                    # (A12) why an item runs alone (a gate code or failed prepares), when this run knows it
                    alone = next((i["id"] for i in items if i["id"].startswith(args["id8"])), None)
                    if alone and self.notes.get(alone):
                        args["reason"] = self.notes[alone]
                return {"code": block["code"], "args": args}
        if not any(i.get("status") == "pending" and i["id"] not in self.workers for i in items):
            return {"code": "nothing-pending", "args": {}}
        return None

    def _beat(self, items: list[dict], now: datetime) -> None:
        slots = []
        for w in list(self.workers.values()):
            slot = {"item": w.item_id, "phase": w.phase, "mode": "shared" if w.mode == "shared" else w.mode,
                    "since": w.since}
            if w.note:
                slot["note"] = w.note
            slots.append(slot)
        merging = [{"item": i["id"], "state": (i.get("merge") or {}).get("state"),
                    "since": (i.get("merge") or {}).get("at")}
                   for i in items if (i.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE]
        data = {"v": 1, "pid": os.getpid(), "create_time": process_create_time(os.getpid()), "n": self.n,
                "n_eff": self.n_eff, "mode": self.mode, "slots": slots, "merging": merging, "why": self.why,
                "reason": self.reason}
        key = json.dumps(data, sort_keys=True, default=str)
        t = time.monotonic()
        if key == self._beat_key and t - self._beat_t < BEAT_EVERY_S:
            return
        data["beat"] = iso(now)
        try:
            self.store.write_parallel(data)
            self._beat_key, self._beat_t = key, t
        except OSError as exc:
            log.warning("parallel.json not written: %s", exc)

    def _exit(self, items: list[dict], now: datetime, timed) -> int | _BackToSerial | None:
        if not self.merger.ready.is_set() or self.workers:
            self._exit_wait_since = None
            return None
        # (2026-10-27, fix round 1) an unsettled merge journal is the next runner's: it never keeps this one
        active = [i for i in items if (i.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE
                  and i["id"] not in self.unsettled]
        if self.hold_exit:
            # (2026-10-27, fix round 1) waiting merges get the 3.7 wait and question before a hold exit too
            if self.merger.idle():
                log.info("queue on hold: no task running, merges done; the runner exits")
                return _loop.EXIT_OK
            self._wait_merges(active)
            return None
        if (live_n(self.store, self.opts) == 1 and self.merger.idle()
                and not parallel_wanted(self.store, items, self.opts)):
            log.info("tasks at once is 1 and no private copy is left: back to the serial runner")
            return BACK_TO_SERIAL
        if self.opts.forever or timed is not None:
            return None
        if any(i.get("status") == "waiting" and (parse_iso(i.get("wait_until")) or now) > now for i in items):
            return None
        ctx = self._ctx(items, now, None)
        ctx.backoff = {}
        if order.pick(items, ctx) is not None:
            return None
        if not active and self.merger.idle():
            log.info("queue empty; exiting (--until-empty)")
            return _loop.EXIT_OK
        self._wait_merges(active)
        return None

    def _wait_merges(self, active: list[dict]) -> None:
        """(3.7) Only waiting merges are left: wait up to MERGE_WAIT_MAX_S, then they become questions.
        (2026-10-29, integrate round 9; NOTE [F2]) the wait ends as soon as a merge is not waiting (an owner's
        retry the merger runs, a new pending merge), so a merge that waits again gets a new full wait."""
        if self.merger.idle(ignore_waiting=True) and all(
                (i.get("merge") or {}).get("state") == "waiting" for i in active):
            t = time.monotonic()
            if self._exit_wait_since is None:
                self._exit_wait_since = t
                log.info("queue done; %d merge(s) still waiting; waiting up to %.0f min", len(active),
                         MERGE_WAIT_MAX_S / 60)
            elif t - self._exit_wait_since >= MERGE_WAIT_MAX_S:
                self._exit_wait_since = None
                self.merger.give_up_waiting()
        else:
            self._exit_wait_since = None

    def shutdown(self) -> None:
        """(1.7) Every thread stops: running attempts are killed and their items go back to pending (sessions
        and copies kept), the merger finishes the step it is in; at most STOP_JOIN_S in total."""
        if self._shut:
            return
        self._shut = True
        self.stop_event.set()
        self.merger.stop()
        deadline = time.monotonic() + STOP_JOIN_S
        for w in list(self.workers.values()):
            w.join(max(0.0, deadline - time.monotonic()))
        if self.merger.is_alive():
            self.merger.join(max(0.0, deadline - time.monotonic()))
        for item_id, w in self.workers.items():
            if w.is_alive():
                log.warning("item %s: its worker did not stop within %.0fs; the next runner recovers it", item_id,
                            STOP_JOIN_S)
        while True:
            try:
                item_id, out = self.unsaved.get_nowait()
            except queue.Empty:
                break
            if not _loop.record_outcome(self.store, item_id, out, self.opts, self.clock):
                log.error("item %s: outcome %s is lost; the item stays `running` until a later runner's crash "
                          "recovery", item_id, out.kind)
        self.awake.off("parallel runner stops")
        with contextlib.suppress(OSError):
            (Path(self.store.data_dir) / store_mod.PARALLEL_FILE).unlink()

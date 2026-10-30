"""The run loop (2026-10-13, spec section 8).

Queue order is strict creation order and a usage limit blocks the whole queue (it is account-wide):
while any item is `waiting` with a future wait_until, the loop sleeps until the earliest one instead
of starting newer pending items (2026-10-13: reconciles 8.1/8.2 with the queue-order rule; see
DECISIONS.md).

Graceful stop (2026-10-13, SPEC_VSCODE section 2): `autopilot stop` writes <data>/stop.request. The
loop checks it at least once per second while sleeping and while a child runs; on a request it kills
the child tree, puts the item back to `pending` (session kept), releases the lock, deletes
stop.request and exits 0.

Goals and pause (2026-10-13, docs/SPEC_GOALS.md section 3): a successful goal round goes back to
`pending` with its original seq (it keeps its place in line) until a marker or a cap ends it. A
`paused` item is never started and does not block the queue. While a child runs, an item-status
probe re-reads the item at most once per second (real clock); `paused`/`cancelled` kills the child
tree and the status stays as the user left it.

Blocked and Reply (2026-10-15, docs/SPEC_V03.md 5.1/5.2): a task's DONE result goes through
apply_task_result (TASK_BLOCKED, or denials without TASK_DONE, give the terminal status `blocked`).
An owner's reply is moved from `pending_reply` to the internal `reply_in_flight` when its attempt
starts; it is dropped once Claude answered in that attempt and put back otherwise (stop, crash,
interrupt or a limit before any answer), so a reply is never lost.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import providers, usage
from .clock import Clock, iso, parse_iso
from .denials import block_text, blocking, hint_text, summarise
from .goals import (BG_KILLED, BG_REDO, BG_REDO_MAX, BG_REDO_PROMPT, FRESH_ROUND, FRESH_RESUME,
                    LAST_MESSAGE_MAX, STATUS_BLOCKED, apply_goal_round, apply_task_result, is_goal,
                    rounds_cap_text, start_fresh, wants_fresh_resume)
from . import limit_handoff
from .limits import RESET_MARGIN
from .log import get_logger
from .profiles import FULL, SHELL_DEV, load_allow_list
from .runner import (DEFAULT_EFFORT, DEFAULT_MAX_BUDGET_USD, DEFAULT_MODEL, DONE, ENV_EFFORT, ENV_MODEL,
                     FAILED, INTERRUPTED, LIMIT, SUBTYPE_BUDGET_PREFIX, SUBTYPE_MAX_TURNS, Outcome,
                     StopRequested, build_argv, check_extra_args, default_args, option_value,
                     prompt_arg_for, redact_argv, run_attempt, usage_text, valid_task_model, without_flag)
from .runner import item_settings  # (2026-10-26, modes)
from .profiles import ultracode_refusal  # (2026-10-27, modes fix HIGH)
from .runner import data_root_of, key_file_paths, project_instructions  # (2026-10-27, SEC-H1/M7 review)
from .store import (HOLD_REASON_LIMIT, RESULT_MAX, LockHeld, Store, held_by_parent, queue_order_key,
                    run_order_key)
from .store import StoreError, write_text_atomic  # (2026-10-26, improve_core)
from .runner import with_system_note  # (2026-10-29, C-L1) moved to runner: build_argv joins the note
from .worktree import MERGE_ACTIVE  # (2026-10-27, tasks at once) merge states that keep an item `running`
from .profiles import user_dirs_deny  # (2026-10-29, integrate round 8: SEC-H2 in the dry run)

log = get_logger(__name__)

# (2026-10-26, improve_core) `run --forever` retries a failing store step this many times in a row, waiting
# STORE_RETRY_S between tries (~2 min), before it gives up and exits 1 as it used to at the first error
STORE_RETRIES = 8
STORE_RETRY_S = 15.0
_PENDING_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # an item id that is safe as a file name

SLEEP_CHUNK_S = 60.0
WAIT_LOG_EVERY_S = 600.0
IDLE_POLL_S = 60.0
STATUS_PROBE_S = 1.0  # (2026-10-13, SPEC_GOALS 3.3) item re-read interval while a child runs
INTERRUPT_STATUSES = ("paused", "cancelled")
# (2026-10-21, security review L6) set by `pause` on a RUNNING item (an ISO time) and cleared when
# the runner has stopped that attempt; `reply` refuses a paused item while it is set and a runner is
# alive (older than PAUSE_UNRELEASED_MAX_S = stale, e.g. a runner that died).
PAUSE_UNRELEASED = "pause_unreleased"
PAUSE_UNRELEASED_MAX_S = 120
EXIT_OK, EXIT_FAIL, EXIT_INTERRUPTED = 0, 1, 130


# (2026-10-17, 0.4.1 R7, DECISIONS.md 0.4.1) turn caps: 100 per task attempt (was 200; the most
# seen in real runs was 14) and 40 per goal round (a round that hits it still counts, amendment M4).
DEFAULT_TASK_MAX_TURNS = 100
DEFAULT_GOAL_MAX_TURNS = 40


@dataclass
class RunOptions:
    forever: bool = False
    start_at: str | None = None  # "HH:MM" local time
    max_turns: int | None = DEFAULT_TASK_MAX_TURNS  # per task attempt
    item_timeout_s: float = 180 * 60
    max_limit_waits: int = 10
    poll_min: float = 30.0
    dry_run: bool = False
    extra_args: list[str] = field(default_factory=list)
    # (2026-10-17, 0.4.1 R1/R7) passed on every attempt unless extra_args set the same flag;
    # None / "" / 0 passes nothing
    goal_max_turns: int | None = DEFAULT_GOAL_MAX_TURNS  # per goal round
    model: str | None = DEFAULT_MODEL
    effort: str | None = DEFAULT_EFFORT
    max_budget_usd: float | None = DEFAULT_MAX_BUDGET_USD
    # (2026-10-20, 0.4.4 logfix A) the project's shell_allow.json (re-read before every shell-dev attempt)
    allow_file: Path | None = None
    # (2026-10-20, 0.4.7 providers) the provider NAME of this run, for the log line only: the key is
    # already in os.environ (cli._provider_selection) and must never reach a RunOptions repr
    provider: str | None = None
    # (2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) the NAMES of the variables the run's provider
    # put into os.environ (an item with its own provider gets them taken out of ITS attempt's env), and
    # how to read providers.env again for such an item. Neither puts a key value into a repr.
    provider_env: tuple[str, ...] = ()
    key_loader: Callable[[], dict] | None = field(default=None, repr=False, compare=False)
    # (2026-10-21, v049 dontask_hold) `run --ignore-hold`: the hold that is on when the runner starts is
    # ignored for its life; a hold put on (again) later still makes it exit after its current item
    ignore_hold: bool = False
    # (2026-10-26, limit_continue) `run --on-limit auto|ask`: auto = wait out a usage limit and resume
    # (as before); ask = park the item, put the queue on a limit hold and exit (the owner continues it)
    on_limit: str = "auto"
    # (2026-10-26, usage forecast) where usage_samples.jsonl goes (the BASE data dir); None = not recorded
    usage_dir: Path | None = None
    # (2026-10-27, SEC-M6 review) `run --env-file`: a PATH only (its keys are redacted from the run logs and
    # its Read is denied to tasks, runner.run_attempt); None = the default key files only
    env_file: Path | None = None
    # (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.2) the run's "tasks at once" when <lane>\tasks_at_once.json is
    # missing or invalid; 1 = the serial loop (scheduler.parallel_wanted decides per pass)
    tasks_at_once: int = 1


ON_LIMIT_AUTO, ON_LIMIT_ASK = "auto", "ask"
ON_LIMIT_CHOICES = (ON_LIMIT_AUTO, ON_LIMIT_ASK)


def shell_allow_for(item: dict, opts: RunOptions) -> list[str] | None:
    """(2026-10-20, 0.4.4 logfix A) The allow-list a shell-dev attempt gets; None for every other profile."""
    if (item.get("profile") or "safe") != SHELL_DEV:
        return None
    allow = load_allow_list(opts.allow_file)
    if not allow:
        log.warning("item %s: profile shell-dev with an empty allow-list (%s); it runs like shell-read. "
                    "Add commands with `autopilot shell-allow add \"<command>\"`", item.get("id"), opts.allow_file)
    return allow


def run_defaults(opts: RunOptions) -> list[str]:
    return default_args(opts.model, opts.effort, opts.max_budget_usd)


@dataclass(frozen=True)
class AttemptChoice:
    """(2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) What one attempt of an item runs on.
    `env` is the attempt's own child env (it may hold a key: never in a repr), None = the runner's."""

    provider: str
    model: str | None
    effort: str | None
    own_provider: bool = False      # the item's provider is not the run's: `env` is its own
    drop_model: bool = False        # a --claude-arg --model / --effort extra yields to this choice
    drop_effort: bool = False
    env: dict | None = field(default=None, repr=False, compare=False)
    env_set: tuple[str, ...] = ()   # NAMES only, for the log
    env_removed: tuple[str, ...] = ()
    endpoint: str = ""              # redacted (providers.redact_url)


_cost_warned: set[str] = set()


class InvalidStoredModel(ValueError):
    """(2026-10-22, v049 tester LOW-13) The item's stored model is not a model id (queue.json edited by
    hand): its attempt fails with this message, the queue goes on."""


def _checked_model(item: dict) -> str | None:
    """(2026-10-22, v049 tester LOW-13) The item's own model, checked again at run with the rule `add`
    and `edit` use (runner.valid_task_model: letters, digits and . _ : / [ ] -, at most 100 characters,
    not led by '-'). Any id of that shape stays valid, listed or not (0.4.9: one model list, old ids
    accepted)."""
    model = item.get("model") or None
    if model is None:
        return None
    if not isinstance(model, str) or not valid_task_model(model):
        shown = repr(model)
        shown = shown if len(shown) <= 60 else shown[:57] + "..."
        raise InvalidStoredModel(f"model {shown} is not a valid model id (letters, digits and . _ : / [ ] -, "
                                 "at most 100 characters, not led by '-'); fix it with "
                                 f"`autopilot edit {item.get('id')} --model <id>` (or --model= for the run's)")
    return model


def attempt_choice(item: dict, opts: RunOptions, environ: dict | None = None) -> AttemptChoice:
    """(2026-10-21, add panel) The provider, model and effort of one attempt of `item`, each by
    item > run > env > default.

    Without its own provider (or with the run's) this is 0.4.8: the item's model/effort, else the run's
    (`run --model/--effort`, which already folded in $CLAUDE_AUTOPILOT_MODEL/_EFFORT and the defaults).
    With ANOTHER provider the run's model and effort were chosen for the run's provider, so they do not
    carry over: the model is the item's, else (the subscription) $CLAUDE_AUTOPILOT_MODEL, else that
    provider's <PREFIX>_MODEL / built-in default; the effort is the item's, else the run's (or the env /
    default when the run's provider takes none). Its env is a copy of the runner's with the run's
    provider taken out and the item's put in (providers.attempt_env), so nothing leaks to the next item.
    An effort for a provider without `supports_effort` is dropped (logged). Raises ProviderError when
    the item's provider cannot be used (unknown, or providers.env lacks what it needs)."""
    environ = os.environ if environ is None else environ
    run_name = (opts.provider or providers.DEFAULT_PROVIDER).strip().lower()
    own = (item.get("provider") or "").strip().lower() or None
    name = own or run_name
    known = providers.provider(name)
    model = _checked_model(item)   # (2026-10-22, v049 tester LOW-13) a hand-edited id is checked again
    effort = item.get("effort") or None
    if effort and not known.supports_effort:
        log.warning("item %s: effort %s dropped: provider %s takes no --effort (an Anthropic-model feature)",
                    item.get("id"), effort, name)
        effort = None
    if name == run_name:
        return AttemptChoice(provider=name, model=model or opts.model,
                             effort=effort or opts.effort, drop_model=bool(model), drop_effort=bool(effort))
    sel = providers.resolve(name, opts.key_loader() if opts.key_loader else {}, model)
    if not model:
        model = ((environ.get(ENV_MODEL) or "").strip() if sel.is_default else "") or sel.model or None
    if not known.supports_effort:
        effort = None
    elif not effort:
        if opts.effort or providers.provider(run_name).supports_effort:
            effort = opts.effort
        else:   # the run's provider takes no effort, so the run has none to pass on: env, else default
            effort = (environ[ENV_EFFORT] if ENV_EFFORT in environ else DEFAULT_EFFORT).strip() or None
    env, was_set, removed = providers.attempt_env(environ, opts.provider_env, sel)
    return AttemptChoice(provider=name, model=model, effort=effort, own_provider=True, drop_model=True,
                         drop_effort=True, env=env, env_set=tuple(was_set), env_removed=tuple(removed),
                         endpoint=providers.redact_url(sel.base_url))


def log_choice(item: dict, choice: AttemptChoice, opts: RunOptions) -> None:
    """(2026-10-21, add panel) One line per attempt that runs on the item's own provider: NAMES only."""
    if not choice.own_provider:
        return
    log.info("item %s: provider %s (the item's own; the run's is %s): model=%s effort=%s endpoint=%s; "
             "child env sets %s, removes %s", item.get("id"), choice.provider,
             opts.provider or providers.DEFAULT_PROVIDER, choice.model or "-", choice.effort or "-",
             choice.endpoint or "(provider default)", ", ".join(choice.env_set) or "-",
             ", ".join(choice.env_removed) or "-")
    if choice.provider != providers.DEFAULT_PROVIDER and choice.provider not in _cost_warned:
        _cost_warned.add(choice.provider)
        log.warning("provider %s: claude.exe prices cost_usd and --max-budget-usd at Anthropic rates, so "
                    "the recorded cost of a %s attempt is an estimate at the wrong rates, not a bill",
                    choice.provider, choice.provider)


def attempt_args(item: dict, opts: RunOptions,
                 choice: AttemptChoice | None = None) -> tuple[list[str], list[str]]:
    """(2026-10-21, task model) The extras and defaults of one attempt of `item`. A task's own model
    (`add/edit --model`) wins over the run's `--model` and a `--claude-arg --model` extra: it is the
    narrower choice. Without one this is the run's (extras, defaults) as before.
    (2026-10-21, add panel) the same for its own effort and provider (attempt_choice); an item on
    another provider than the run's drops the run's --model/--effort extras too."""
    choice = choice or attempt_choice(item, opts)
    if not (choice.drop_model or choice.drop_effort):
        return list(opts.extra_args), default_args(choice.model, choice.effort, opts.max_budget_usd)
    extras = list(opts.extra_args)
    check_extra_args(extras)  # clean before a --model/--effort pair is dropped from it
    if choice.drop_model:
        extras = without_flag(extras, "--model")
    if choice.drop_effort:
        extras = without_flag(extras, "--effort")
    return extras, default_args(choice.model, choice.effort, opts.max_budget_usd)


def turns_for(item: dict, opts: RunOptions) -> int | None:
    return opts.goal_max_turns if is_goal(item) else opts.max_turns


def budget_cap(opts: RunOptions) -> str:
    """The effective --max-budget-usd value for messages (a user extra wins over the default)."""
    value = option_value(list(opts.extra_args), "--max-budget-usd")
    if value is None and opts.max_budget_usd:
        value = default_args(None, None, opts.max_budget_usd)[1]
    return f"${value}" if value is not None else "?"


# (2026-10-20, 0.4.4 logfix G) errors.log (WARNING and up) shows every item that needs the owner
_TRANSITION_LEVELS = {"failed": logging.ERROR, STATUS_BLOCKED: logging.WARNING}


def transition(item: dict, new: str, reason: str) -> None:
    log.log(_TRANSITION_LEVELS.get(new, logging.INFO), "item %s: %s -> %s (%s)", item["id"],
            item.get("status"), new, reason)
    item["status"] = new


# ---- (2026-10-20, 0.4.4 logfix C) the same question again -------------------------------------------------
def block_key(item: dict) -> str | None:
    """The normalised question of a blocked item (blocked_question.text, else its error text)."""
    q = item.get("blocked_question")
    raw = (q.get("text") if isinstance(q, dict) else None) or item.get("error") or ""
    key = " ".join(re.sub(r"[^\w\s]", " ", str(raw).lower()).split())
    key = key[len("blocked "):] if key.startswith("blocked ") else key
    return key or None


def note_repeat_block(item: dict) -> None:
    """After an attempt's outcome: a block with the same question AND the same profile as the
    previous block counts up `repeat_block` (logged as a WARNING); any other block, or a done/failed
    outcome or a finished goal round, sets it back to 0. No retry is added."""
    if item.get("status") != STATUS_BLOCKED:
        item["repeat_block"] = 0
        item["last_block_key"] = None
        item["last_block_profile"] = None
        return
    key = block_key(item)
    profile = item.get("profile") or "safe"
    if key and key == item.get("last_block_key") and profile == item.get("last_block_profile"):
        item["repeat_block"] = int(item.get("repeat_block") or 0) + 1
        log.warning("item %s: blocked again with the same question as the last attempt (profile %s "
                    "unchanged, repeat %d); retrying as is will ask it again: change the profile or "
                    "answer it", item["id"], profile, item["repeat_block"])
    else:
        item["repeat_block"] = 0
    item["last_block_key"] = key
    item["last_block_profile"] = profile


_SHELL_NEED_RE = re.compile(r"\b(shell|bash|powershell|terminal|commands?|command line|komen\w*|komento\w*|git|pytest|npm)\b", re.I)
# (2026-10-21, v049 dontask_hold) characters of a failed attempt's error kept in a denial block's text
FAILED_ERROR_MAX = 300


def attempt_key_provider(item: dict, opts: RunOptions | None) -> str | None:
    """(2026-10-21, v049 merge) The provider the item's attempt ran on (its own, else the run's, as
    attempt_choice) when that provider puts an API key in the child env; None for the subscription."""
    name = ((item.get("provider") or "").strip() or ((opts.provider if opts else None) or "")
            or providers.DEFAULT_PROVIDER).strip().lower()
    try:
        return name if providers.needs_key(name) else None
    except providers.ProviderError:
        return None


def allow_list_of(opts: RunOptions | None) -> list[str] | None:
    """(2026-10-22, v049 tester LOW-11) The project's shell-dev allow-list for the denial block; None when
    the run has none (an empty file reads as [])."""
    return load_allow_list(opts.allow_file) if opts is not None and opts.allow_file else None


def record_denial_block(item: dict, out: Outcome, opts: RunOptions | None = None) -> None:
    """(2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 1) A blocked item whose attempt had
    permission denials (hook notes excluded) gets `denial_block`: what was denied, the profile to retry
    with, the allow-list entries to add and the `hint` text (denials.summarise / hint_text). Any other
    outcome drops it. Nothing is granted, changed or retried here: the owner decides.
    (2026-10-21, v049 merge) an attempt on a key provider (add panel) is never told to retry with a
    shell profile: `run` refuses that pair (attempt_key_provider).
    (2026-10-22, v049 tester LOW-11) an entry already on the project's allow-list is not suggested again."""
    block = summarise(out.denials, item.get("profile"), item.get("cwd"), attempt_key_provider(item, opts),
                      allow_list_of(opts)) if item.get("status") == STATUS_BLOCKED else None
    if block:
        item["denial_block"] = {**block, "hint": hint_text(block, item["id"])}
    else:
        item.pop("denial_block", None)


def _shell_hint(item: dict, out: Outcome, opts: RunOptions | None = None) -> None:
    """(2026-10-20, 0.4.4 logfix A) A block that needs a command points to shell-dev and its allow-list
    (2026-10-21, owner) and to `full`, which runs any command.
    (2026-10-21, v049 dontask_hold) A block with a `denial_block` logs its exact fix instead.
    (2026-10-21, v049 tester fix LOW-1) No shell-dev / full hint for an attempt on a key provider:
    `retry --profile shell-dev|full` is refused for it (attempt_key_provider)."""
    block = item.get("denial_block") if item.get("status") == STATUS_BLOCKED else None
    if block:
        log.warning("item %s: denied %s; fix: %s", item["id"],
                    block_text(block).removeprefix("permission denied for "), block.get("hint"))
        return
    if item.get("status") != STATUS_BLOCKED or (item.get("profile") or "safe") in (SHELL_DEV, FULL):
        return
    if attempt_key_provider(item, opts):
        return
    denied_shell = any(d.get("tool_name") in ("Bash", "PowerShell") for d in blocking(out.denials))
    if denied_shell or _SHELL_NEED_RE.search(str(item.get("error") or "")):
        log.warning("item %s: it may need a command: `retry %s --profile shell-dev` runs the commands on "
                    "this project's allow-list (`shell-allow list|add`; an empty list = shell-read), "
                    "`--profile full` runs any command with no permission checks",
                    item["id"], item["id"])


def _check_stop(should_stop: Callable[[], bool] | None) -> None:
    if should_stop is not None and should_stop():
        raise StopRequested()


def sleep_until(target: datetime, clock: Clock, what: str,
                should_stop: Callable[[], bool] | None = None) -> None:
    """Sleep in chunks of <= 60 s (<= clock.stop_poll_s with `should_stop`), logging every 10 min."""
    chunk_max = SLEEP_CHUNK_S if should_stop is None else _stop_chunk(clock)
    last_log = None
    while True:
        _check_stop(should_stop)
        remaining = (target - clock.now()).total_seconds()
        if remaining <= 0:
            return
        if last_log is None or last_log - remaining >= WAIT_LOG_EVERY_S:
            log.info("waiting for %s: %s remaining (until %s)", what,
                     timedelta(seconds=int(remaining)), iso(target))
            last_log = remaining
        clock.sleep(min(chunk_max, remaining))


def _stop_chunk(clock: Clock) -> float:
    """(2026-10-13) The real Clock sets stop_poll_s = 1 s, so stop.request is seen within a second.
    An injected test clock without it keeps 60 s chunks: no real time passes there, and 1 s chunks
    made a fake day-long wait 86400 iterations (suite 12 s -> 30 s)."""
    return min(SLEEP_CHUNK_S, float(getattr(clock, "stop_poll_s", SLEEP_CHUNK_S)))


def _idle_sleep(seconds: float, clock: Clock, should_stop: Callable[[], bool] | None) -> None:
    left = seconds
    chunk = _stop_chunk(clock)
    while left > 0:
        _check_stop(should_stop)
        step = min(chunk, left)
        clock.sleep(step)
        left -= step
    _check_stop(should_stop)


def _retry_store(fn: Callable, what: str, clock: Clock, should_stop: Callable[[], bool] | None, tries: int):
    """(2026-10-26, improve_core) fn() with `run --forever`'s patience for the queue store. A StoreError or
    OSError (a busy or stale mutex, a locked or corrupt queue.json, a full disk) is logged with its cause
    and fn is tried again after STORE_RETRY_S, up to `tries` failures in a row; the last one propagates
    as before (the runner exits 1). The runner used to end at the first one."""
    for n in range(1, tries + 1):
        try:
            return fn()
        except (StoreError, OSError) as exc:
            if n >= tries:
                if tries > 1:
                    log.error("%s failed %d times in a row (%s: %s); the runner gives up", what, n,
                              type(exc).__name__, exc)
                raise
            log.error("%s failed (%d/%d in a row): %s: %s; trying again in %.0fs", what, n, tries,
                      type(exc).__name__, exc, STORE_RETRY_S)
            _idle_sleep(STORE_RETRY_S, clock, should_stop)


def next_local_time(hhmm: str, clock: Clock) -> datetime:
    hour, minute = (int(x) for x in hhmm.split(":", 1))
    tz = clock.local_tz()
    now_local = clock.now().astimezone(tz)
    cand = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now_local:
        cand += timedelta(days=1)
    return cand


def _created_key(item: dict):
    return queue_order_key(item)


def runnable_pending(items: list[dict]) -> list[dict]:
    """(2026-10-21, subtasks) The pending items that may start, in run order: a subtask waits until
    its main task is done, then runs right after it (store.run_order_key)."""
    by_id = {i["id"]: i for i in items}
    return sorted((i for i in items if i.get("status") == "pending" and not held_by_parent(i, by_id)),
                  key=lambda i: run_order_key(i, by_id))


def recover_crashed(store: Store) -> int:
    count = 0
    kept = pending_outcome_ids(store)  # (2026-10-26) those stay `running` until their outcome is applied
    with store.transaction(backup=True) as items:
        for it in items:
            it.pop(PAUSE_UNRELEASED, None)  # (security review L6) no runner owns anything at start
            # (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.6) a finished copy item whose merge is pending, waiting or
            # merging stays `running`: its outcome is applied and the merger picks it up; any other one goes
            # back to pending as before (its copy is kept and reused)
            if (it.get("merge") or {}).get("state") in MERGE_ACTIVE:
                continue
            if it.get("status") == "running" and it.get("id") not in kept:
                if is_goal(it):
                    it["goal_continue"] = False  # a crashed round resumes with the resume prompt
                settle_reply(it, answered=False)
                transition(it, "pending", "crash recovery: was running at start; session kept")
                count += 1
    return count


def settle_reply(item: dict, answered: bool) -> None:
    """(2026-10-15, SPEC_V03 5.2) End of an attempt that carried an owner's reply: drop it once
    Claude answered, otherwise put it back as `pending_reply` for the next attempt."""
    reply = item.pop("reply_in_flight", None)
    if not reply:
        return
    if answered:
        log.info("item %s: the owner's reply reached the session", item["id"])
        return
    if not item.get("pending_reply"):
        item["pending_reply"] = reply
    log.warning("item %s: attempt ended before Claude answered; the owner's reply is kept for the "
                "next attempt", item["id"])


def record_attempt_facts(item: dict, out: Outcome, now) -> None:
    """(2026-10-17, 0.4.1 R1/R6) What the next attempt needs to know about this one: the model and
    CLI version from the init event, the last main-thread context, when it ended and (unless it was
    cut by a usage limit) the tail of its final message. A new session clears `fresh`."""
    prev_session = item.get("session_id")  # (2026-10-27, integrate: review LOW-4) before this attempt's
    if out.session_id:
        item["session_id"] = out.session_id
    if item.get("session_id"):
        item.pop("fresh", None)
    if out.model:
        item["model_used"] = out.model
    if out.claude_code_version:
        item["claude_code_version"] = out.claude_code_version
    if out.last_ctx is not None:
        item["last_ctx"] = out.last_ctx
    if out.cache_tokens:
        # (2026-10-26, task 5c5e438d) prompt-cache use summed over every attempt (as total_cost_usd is)
        # and the share of it read from the cache, for the UI; absent until an attempt reported usage
        old = item.get("cache_tokens") if isinstance(item.get("cache_tokens"), dict) else {}
        sums = {k: (old.get(k) if isinstance(old.get(k), int) and not isinstance(old.get(k), bool) else 0)
                + v for k, v in out.cache_tokens.items()}
        item["cache_tokens"] = sums
        total = sum(sums.values())
        item["cache_read_share"] = round(sums.get("cache_read", 0) / total, 4) if total else None
    # (2026-10-20) how the attempt was billed (runner.billing_for); an attempt without an init event
    # keeps the previous attempt's values
    if out.api_key_source is not None:
        item["api_key_source"] = out.api_key_source
        item["billing"] = out.billing
    # (2026-10-27, limit_continue fix LOW-6) a LIMIT attempt without a real main-thread message (e.g. a
    # resume rejected at once) wrote no cache: R6's gap still counts from the last real attempt
    if out.kind != LIMIT or out.last_ctx is not None or not item.get("last_ended_at"):
        item["last_ended_at"] = iso(now)
    if out.kind != LIMIT and out.result_text:
        item["last_message"] = out.result_text[-LAST_MESSAGE_MAX:]
    elif out.kind == LIMIT and out.handoff:
        # (2026-10-26, limit_continue) a limit has no final message: the run log's handoff fills it
        prev = item.get("last_message") if isinstance(item.get("last_message"), str) else ""
        same = not out.session_id or out.session_id == prev_session
        item["last_message"] = limit_last_message(out.handoff, prev if same else "")


HANDOFF_BEFORE = "\n\nThe attempt before it ended with:\n"
HANDOFF_BEFORE_MIN = 200  # less room than this for the earlier text: the handoff alone


def limit_last_message(handoff: str, prev: str) -> str:
    """(2026-10-27, integrate: limit_continue review LOW-4) `last_message` after a LIMIT attempt: its
    handoff, then as much of the end of the previous `last_message` of the SAME session as still fits in
    LAST_MESSAGE_MAX (`prev` = '' for a new session or none). Before this, a resumed attempt cut again
    after a little work replaced a richer handoff or final message with its short handoff. That text is
    what a fresh session after a long wait gets (R6)."""
    handoff = handoff[-LAST_MESSAGE_MAX:]
    prev = prev.strip()
    if not prev:
        return handoff
    if prev.startswith(handoff.strip()):  # this handoff is already there: nothing new to add
        return prev[:LAST_MESSAGE_MAX]
    room = LAST_MESSAGE_MAX - len(handoff) - len(HANDOFF_BEFORE)
    if room < HANDOFF_BEFORE_MIN:
        return handoff
    tail = prev if len(prev) <= room else "…" + prev[-(room - 1):]
    return handoff + HANDOFF_BEFORE + tail


def apply_outcome(item: dict, out: Outcome, opts: RunOptions, clock: Clock) -> None:
    now = clock.now()
    item.pop(PAUSE_UNRELEASED, None)  # (security review L6) the runner has let go of the attempt
    settle_reply(item, out.answered)
    record_attempt_facts(item, out, now)
    if out.cost_usd is not None:
        item["cost_usd"] = out.cost_usd
        # (2026-10-13, review L5) cost_usd is the last attempt; total_cost_usd sums every attempt.
        item["total_cost_usd"] = round(float(item.get("total_cost_usd") or 0) + float(out.cost_usd), 6)
    if out.num_turns is not None:
        item["num_turns"] = out.num_turns
    if out.result_text is not None:
        item["result"] = out.result_text[:RESULT_MAX]
    if out.denials:
        # (2026-10-21, v049 dontask_hold) + `kind` (denials.annotate): "hook" = a note, never a block
        item["permission_denials"] = [
            {"tool_name": d.get("tool_name"), "tool_input": str(d.get("tool_input"))[:300],
             **({"kind": d["kind"]} if d.get("kind") else {})}
            for d in out.denials if isinstance(d, dict)
        ]
    if item.get("status") == "cancelled":
        log.info("item %s was cancelled during its attempt; keeping 'cancelled'", item["id"])
        item["finished_at"] = iso(now)
        return
    # (2026-10-13, amendment M5) Paused after the last status probe but before the outcome was
    # saved: the outcome is applied as usual first (a goal counts its round from the FULL result).
    # A final result (done/failed) wins over the pause; anything that would run again stays paused.
    was_paused = item.get("status") == "paused"
    if was_paused:
        item["status"] = "running"
    if is_goal(item):
        item["goal_continue"] = False
    max_turns_round = out.kind == FAILED and is_goal(item) and out.subtype == SUBTYPE_MAX_TURNS
    # (2026-10-17, 0.4.1 R7) a task that hits the turn cap, or any item that hits the budget cap,
    # is `blocked`, so the owner can reply "continue" (the session is kept and resumes).
    cap_why = None
    if out.kind == FAILED and str(out.subtype or "").startswith(SUBTYPE_BUDGET_PREFIX):
        cap_why = f"hit the budget cap ({budget_cap(opts)})"
    elif out.kind == FAILED and not is_goal(item) and out.subtype == SUBTYPE_MAX_TURNS:
        cap_why = f"hit the turn cap ({opts.max_turns})"
    # (2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 1) a TASK attempt that failed after a
    # permission denial (hooks excluded) needs the owner: blocked, naming what was denied
    denied = summarise(out.denials, item.get("profile"), item.get("cwd"), allow=allow_list_of(opts)) \
        if out.kind == FAILED and not is_goal(item) else None
    # (2026-10-21, v049 dontask_hold addendum) the redo instruction has reached the session
    if out.answered or out.kind == DONE:
        item.pop(BG_REDO, None)
    if cap_why:
        item["error"] = f"blocked: {cap_why}" + (f"; {block_text(denied)}" if denied else "")
        item["blocked_question"] = None
        item["wait_until"] = None
        item["finished_at"] = iso(now)
        transition(item, STATUS_BLOCKED, f"{cap_why}; reply to continue the session; "
                                         f"turns={out.num_turns}, {usage_text(out.billing, out.cost_usd)}")
    elif out.kind == DONE and out.bg_killed:
        apply_bg_killed(item, out, now)
    elif is_goal(item) and (out.kind == DONE or max_turns_round):
        # Amendment M1: the marker comes from out.result_text, never the truncated item["result"].
        # Amendment M4: error_max_turns is a finished round without a marker, not a failed goal.
        new, reason = apply_goal_round(item, out.result_text, now, max_turns=max_turns_round,
                                       last_ctx=out.last_ctx)
        transition(item, new, f"goal round {item['rounds']}/{rounds_cap_text(item.get('max_rounds'))}: {reason}; "
                              f"turns={out.num_turns}, {usage_text(out.billing, out.cost_usd)}")
    elif out.kind == DONE:
        # (2026-10-15, SPEC_V03 5.1) the task marker / denials decide done vs blocked (FULL result)
        new, why = apply_task_result(item, out.result_text, out.denials, now)
        transition(item, new, f"{why}; turns={out.num_turns}, {usage_text(out.billing, out.cost_usd)}")
    elif out.kind == LIMIT:
        if out.bg_killed and item.get("session_id"):
            # (2026-10-21, v049 tester fix LOW-6) the limit ended a run whose background tasks were
            # killed: the resume after the wait carries the redo instruction (never a plain resume)
            item[BG_KILLED] = int(item.get(BG_KILLED) or 0) + 1
            item[BG_REDO] = True
            log.warning("item %s: %s; the resume after the usage-limit wait asks to redo it in the "
                        "foreground", item["id"], out.bg_killed)
        item["limit_waits"] = int(item.get("limit_waits") or 0) + 1
        item["error"] = out.error
        if item["limit_waits"] > opts.max_limit_waits:
            item["wait_until"] = None
            item["finished_at"] = iso(now)
            item["error"] = f"limit waits exceeded ({item['limit_waits']} > {opts.max_limit_waits}); {out.error}"
            transition(item, "failed", "max-limit-waits cap reached")
        else:
            wait_until = out.reset_at + RESET_MARGIN
            item["wait_until"] = iso(wait_until)
            transition(item, "waiting",
                       f"usage limit; reset {iso(out.reset_at)} via {out.reset_source}; "
                       f"resume at {iso(wait_until)} (wait {item['limit_waits']}/{opts.max_limit_waits})")
    elif denied:
        ended = " ".join(str(out.error or "failed").split())[:FAILED_ERROR_MAX]
        item["error"] = f"blocked: {block_text(denied)} (the attempt ended: {ended})"
        item["blocked_question"] = None
        item["wait_until"] = None
        item["finished_at"] = iso(now)
        transition(item, STATUS_BLOCKED, f"{block_text(denied)}; the attempt ended: {ended}")
    else:
        item["error"] = out.error
        item["finished_at"] = iso(now)
        transition(item, "failed", out.error or "failed")
    if out.kind == DONE and not out.bg_killed:
        item.pop(BG_KILLED, None)   # a clean finish ends a run of killed-background attempts
    if out.kind != LIMIT:  # (2026-10-20, 0.4.4 logfix C) a usage-limit wait says nothing about the question
        note_repeat_block(item)
    record_denial_block(item, out, opts)
    _shell_hint(item, out, opts)
    if was_paused:
        if item["status"] in ("pending", "waiting"):
            item["paused_from"] = item["status"]
            transition(item, "paused", "the user paused it during this attempt; outcome recorded")
        else:
            item["paused_from"] = None
            log.info("item %s: paused during its attempt, but its outcome %r is final", item["id"],
                     item["status"])


def outcome_applier(item: dict, out: Outcome, opts: RunOptions, clock: Clock) -> None:
    """(2026-10-27, tasks at once, SPEC_V05_PARALLEL 3.1) Phase 1 of a merge: apply an attempt's outcome to the item
    (apply_interrupt / apply_outcome, as before). When that makes the item `done` while it has an active
    private copy, its changes are not in the project yet: merge = pending (gen + 1, result_status done) and
    the status stays `running` until the merger has merged them. A goal merges once, at its final done (a
    round goes back to pending). Every other outcome keeps the copy and starts no merge. Used by
    record_outcome and apply_pending_outcomes; an item without a copy gets exactly the old outcome."""
    if out.kind == INTERRUPTED:
        apply_interrupt(item, out, clock.now())
        return
    apply_outcome(item, out, opts, clock)
    rec = item.get("worktree")
    if item.get("status") != "done" or not isinstance(rec, dict) or rec.get("state") != "active":
        return
    merge = dict(item.get("merge") or {})
    merge.update(state="pending", result_status="done", gen=int(merge.get("gen") or 0) + 1,
                 at=iso(clock.now()), wait=None, tries=0)
    item["merge"] = merge
    transition(item, "running", "finished; merging its changes")


def apply_bg_killed(item: dict, out: Outcome, now) -> None:
    """(2026-10-21, v049 dontask_hold addendum, SPEC_V049_DONTASK_HOLD 3) A finished attempt whose
    background tasks claude.exe killed when the run ended is never `done` (nor a counted goal round):
    the first one resumes the SAME session once with BG_REDO_PROMPT; the next one in a row blocks."""
    n = int(item.get(BG_KILLED) or 0) + 1
    item[BG_KILLED] = n
    item["wait_until"] = None
    if n <= BG_REDO_MAX and item.get("session_id"):
        item[BG_REDO] = True
        item["error"] = None
        log.warning("item %s: %s; resuming session %s once to redo that work in the foreground",
                    item["id"], out.bg_killed, item.get("session_id"))
        transition(item, "pending", f"{out.bg_killed}; resuming the same session once: \"{BG_REDO_PROMPT}\"; "
                                    f"turns={out.num_turns}, {usage_text(out.billing, out.cost_usd)}")
        return
    again = " again, after the resume that asked to redo it in the foreground" if n > 1 else ""
    item["error"] = f"blocked: {out.bg_killed}{again}"[:FAILED_ERROR_MAX + 200]
    item["blocked_question"] = None
    item["finished_at"] = iso(now)
    log.warning("item %s: %s%s; blocked", item["id"], out.bg_killed, again)
    transition(item, STATUS_BLOCKED, f"background tasks killed at the end of the run{again}; reply to "
                                     f"continue the session; turns={out.num_turns}, "
                                     f"{usage_text(out.billing, out.cost_usd)}")


def apply_interrupt(item: dict, out: Outcome, now=None) -> None:
    """(2026-10-13, SPEC_GOALS 3.3) Paused/cancelled while running: keep the user's status and the
    session; nothing is counted as a failure."""
    item.pop(PAUSE_UNRELEASED, None)  # (security review L6) the attempt is stopped: a reply may go now
    if now is not None:
        record_attempt_facts(item, out, now)
    elif out.session_id:
        item["session_id"] = out.session_id
    settle_reply(item, out.answered)
    if is_goal(item):
        item["goal_continue"] = False
    if item.get("status") == "running":  # cannot normally happen; never leave an item stuck
        transition(item, "pending", "interrupted attempt, status was still running; session kept")
        return
    log.info("item %s: attempt interrupted; status stays %r, session %s kept", item["id"],
             item.get("status"), item.get("session_id"))


def status_probe(store: Store, item_id: str, interval_s: float = STATUS_PROBE_S,
                 monotonic: Callable[[], float] = time.monotonic) -> Callable[[], str | None]:
    """A should_interrupt callable: re-reads the item at most once per `interval_s` of REAL time and
    returns its status when it became paused/cancelled, else None. Read errors never interrupt."""
    last = [float("-inf")]

    def probe() -> str | None:
        t = monotonic()
        if t - last[0] < interval_s:
            return None
        last[0] = t
        try:
            status = store.find(store.load(), item_id).get("status")
        except KeyError:
            return None  # removed during its attempt: handled when the outcome is applied (fix B6)
        except Exception as exc:  # a half-written file must not kill the attempt
            log.warning("item %s: status probe could not read the queue: %s", item_id, exc)
            return None
        return status if status in INTERRUPT_STATUSES else None

    return probe


def dry_run(store: Store, opts: RunOptions, claude_cmd: list[str], out=print) -> int:
    items = sorted((i for i in store.load() if i.get("status") in ("pending", "waiting", "running")),
                   key=_created_key)
    if not items:
        out("(queue has no runnable items)")
    for it in items:
        prompt_arg = prompt_arg_for(it, store.prompts_dir, write=False)
        try:
            extras, defaults = attempt_args(it, opts)
        except (providers.ProviderError, InvalidStoredModel) as exc:   # (2026-10-21, add panel) names only
            out(f"{it['id']} [{it['status']}] cwd={it['cwd']}")
            out(f"  refused: {exc}")
            continue
        why = (ultracode_refusal(it.get("profile") or "safe", it.get("max_agents"))   # (2026-10-27, modes fix HIGH)
               if item_settings(it).get("ultracode") else None)
        if why:
            out(f"{it['id']} [{it['status']}] cwd={it['cwd']}")
            out(f"  refused: {why}")
            continue
        # (2026-10-29, integrate round 8, DECISION [F3] residual 1) the Edit/Write denies of the user's extra
        # folders, as run_attempt adds them (not for full); printed as `<extra folder>` like its log line
        user_rules = ([] if (it.get("profile") or "safe") == FULL else user_dirs_deny(it["cwd"], os.environ))
        argv = build_argv(claude_cmd, prompt_arg, it.get("profile") or "safe", turns_for(it, opts),
                          it.get("session_id"), extras, defaults, shell_allow_for(it, opts),
                          settings=item_settings(it),   # (2026-10-26, modes) as run_attempt
                          # (2026-10-27, SEC-H1/M7 review) the CLAUDE.md block and the data-dir denies, as
                          # run_attempt builds them, and the item's subagent limit
                          max_agents=it.get("max_agents"), project_instructions=project_instructions(it["cwd"]),
                          data_root=data_root_of(store.runs_dir), prompts_dir=store.prompts_dir,
                          key_files=key_file_paths(store.runs_dir, os.environ, opts.env_file),
                          user_dir_rules=user_rules)
        out(f"{it['id']} [{it['status']}] cwd={it['cwd']}")
        out("  " + " ".join(_quote(a) for a in redact_argv(argv, prompt_arg, user_rules)))
    return EXIT_OK


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def run_loop(store: Store, opts: RunOptions, claude_cmd: list[str], clock: Clock | None = None,
             keep_awake: bool = True, claude_resolver: Callable[[], list[str]] | None = None) -> int:
    """`claude_resolver` (2026-10-19, 0.4.1), when given, is asked for the claude command before
    every attempt (it re-resolves a vanished claude.exe); a ClaudePathError from it propagates
    before the item is started, so nothing is left `running`."""
    from .keepawake import KeepAwake

    clock = clock or Clock()
    lock_t0 = time.time()
    try:
        store.acquire_lock(clock.now())
    except LockHeld as exc:
        log.error("%s", exc)
        return EXIT_FAIL
    _drop_stale_stop_request(store, lock_t0)
    should_stop = store.stop_requested
    current: dict | None = None
    stopped = False
    # (2026-10-26, improve_core) store steps are retried in --forever (_retry_store); outcomes that neither
    # the queue nor a kept-outcome file took are retried from memory on every pass
    tries = STORE_RETRIES if opts.forever else 1
    unsaved: list[tuple[str, Outcome]] = []
    # (2026-10-20, 0.4.4 logfix F) on while an attempt runs or a usage-limit wait is active, off while idle
    awake = KeepAwake(enabled=keep_awake)
    ignored_hold: tuple | None = None  # (2026-10-27, integrate) set first thing below; the finally reads it
    try:
        # (2026-10-22, v049 tester LOW-9) the key of the hold in place at start (Store.hold_key), not only
        # its since: an unreadable hold has since None, and a later `hold on` must still count as new
        ignored_hold = store.hold_key(clock.now()) if opts.ignore_hold else None  # (2026-10-27) the run's clock
        if ignored_hold is not None:
            log.warning("queue is on hold (since %s); started anyway (--ignore-hold)", ignored_hold[0])
        # (2026-10-26, improve_core) kept outcomes first: crash recovery must not make their items pending
        limits: list[str] = []  # (2026-10-27, integrate) replayed usage limits: the ask hold below
        _retry_store(lambda: apply_pending_outcomes(store, opts, clock, limits), "applying kept outcomes", clock,
                     should_stop, tries)
        n = _retry_store(lambda: recover_crashed(store), "crash recovery", clock, should_stop, tries)
        if n:
            log.info("recovered %d crashed item(s)", n)
        if _limit_hold_after_replay(store, opts, clock, ignored_hold, limits):
            return EXIT_OK
        if opts.start_at:
            target = next_local_time(opts.start_at, clock)
            sleep_until(target, clock, f"--start-at {opts.start_at}", should_stop)
        hold_logged = None  # (2026-10-27) the timed hold's until last logged
        while True:
            _check_stop(should_stop)
            current = None
            # (2026-10-26, usage forecast) a timed hold ("queue for after the reset") is waited out
            until = hold_until(store, ignored_hold, clock.now())
            if until is not None:
                awake.on("waiting for the end of a timed hold")
                # (2026-10-27, usage forecast fix MED-1) wait <= 60 s at a time and read the hold again,
                # so `hold off` (Release hold) ends the wait at once, not at the old `until`
                if until != hold_logged:
                    log.info("queue on a timed hold until %s; waiting (hold off ends it)", iso(until))
                    hold_logged = until
                _idle_sleep(min(IDLE_POLL_S, max(0.0, (until - clock.now()).total_seconds())), clock,
                            should_stop)
                continue
            if hold_stops(store, ignored_hold, clock.now()):
                return EXIT_OK
            # (2026-10-26, improve_core) outcomes the queue could not take earlier come first
            limits = []
            still: list[tuple[str, Outcome]] = []
            for i, o in unsaved:
                if not record_outcome(store, i, o, opts, clock):
                    still.append((i, o))
                elif o.kind == LIMIT:
                    limits.append(i)
            unsaved[:] = still
            _retry_store(lambda: apply_pending_outcomes(store, opts, clock, limits), "applying kept outcomes", clock,
                         should_stop, tries)
            # (2026-10-27, integrate: limit_continue) a usage limit saved late holds the queue as one saved at once
            if _limit_hold_after_replay(store, opts, clock, ignored_hold, limits):
                return EXIT_OK
            items = _retry_store(store.load, "reading the queue", clock, should_stop, tries)
            # (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.1) more than one at once, or copy/merge state left: the
            # parallel coordinator runs this lane until it hands back (N 1 and nothing of a copy left)
            from . import scheduler
            if scheduler.parallel_wanted(store, items, opts):
                rc = scheduler.run_parallel(store, opts, claude_cmd, clock, awake, should_stop,
                                            ignored_hold=ignored_hold, claude_resolver=claude_resolver)
                if rc is not scheduler.BACK_TO_SERIAL:
                    return rc
                continue
            now = clock.now()
            waiting = sorted((i for i in items if i.get("status") == "waiting"),
                             key=lambda i: (parse_iso(i.get("wait_until")) or now, _created_key(i)))
            if waiting:
                first = waiting[0]
                due = parse_iso(first.get("wait_until")) or now
                if due > now:
                    # (2026-10-20, 0.4.4 logfix F, DECISIONS.md) kept awake: the runner must wake at reset
                    awake.on("waiting for a usage-limit reset")
                    sleep_until(due, clock, f"usage-limit reset (item {first['id']})", should_stop)
                    continue  # re-read the queue: it may have changed while we slept
                # (2026-10-26) a function, for _retry_store; (2026-10-27, tasks at once) release_due_waiting
                _retry_store(lambda now=now: release_due_waiting(store, now), "releasing due usage-limit waits",
                             clock, should_stop, tries)
                continue
            pending = runnable_pending(items)
            if not pending:
                if not opts.forever:
                    held = sum(1 for i in items if i.get("status") == "pending")
                    if held:
                        log.info("queue empty; %d subtask(s) wait for their main task to be done; "
                                 "exiting (--until-empty)", held)
                    else:
                        log.info("queue empty; exiting (--until-empty)")
                    return EXIT_OK
                awake.off("queue idle")
                log.debug("queue idle; polling again in %.0fs", IDLE_POLL_S)
                _idle_sleep(IDLE_POLL_S, clock, should_stop)
                continue
            if claude_resolver is not None:
                claude_cmd = claude_resolver()
            first_id = pending[0]["id"]
            current = _retry_store(lambda: _start_item(store, first_id, clock), f"starting item {first_id}",
                                   clock, should_stop, tries)
            if current is None:
                continue
            awake.on("an attempt is running")
            # (2026-10-27, tasks at once) the attempt body is run_one_attempt (the parallel workers call it too)
            out = run_one_attempt(store, current, opts, claude_cmd, clock, should_stop=should_stop)
            if not record_outcome(store, current["id"], out, opts, clock):
                unsaved.append((current["id"], out))  # (2026-10-26) retried from the next pass on
            elif out.kind == LIMIT and opts.on_limit == ON_LIMIT_ASK and limit_hold(store, current["id"], clock, ignored_hold):
                return EXIT_OK
            current = None
    except StopRequested:
        stopped = True
        log.warning("stop requested (%s): stopping runner", store.stop_path)
        if current is not None:
            _back_to_pending(store, current["id"], "stop requested; session kept")
        return EXIT_OK
    except KeyboardInterrupt:
        log.warning("Ctrl+C: stopping runner")
        if current is not None:
            _back_to_pending(store, current["id"], "interrupted by Ctrl+C; session kept")
        return EXIT_INTERRUPTED
    finally:
        awake.off("runner exits")
        for item_id, kept_out in unsaved:  # (2026-10-26, improve_core) a last try before the lock goes
            if not record_outcome(store, item_id, kept_out, opts, clock):
                log.error("item %s: outcome %s is lost; the item stays `running` until a later runner's "
                          "crash recovery", item_id, kept_out.kind)
            elif kept_out.kind == LIMIT:  # (2026-10-27, integrate) saved at the last try: the ask hold too
                _limit_hold_after_replay(store, opts, clock, ignored_hold, [item_id])
        store.release_lock()
        if stopped and store.clear_stop_request():
            log.info("stop.request deleted; runner exits 0")


def _patched_env(base: dict | None, env_patch: dict | None) -> dict | None:
    """(2026-10-27, tasks at once, SPEC_V05_PARALLEL 4.4) worktree.attempt_env_patch applied to the attempt's base env
    (None = the runner's os.environ); no patch (or an empty one) leaves `base` as it is."""
    if not env_patch or not (env_patch.get("set") or env_patch.get("drop")):
        return base
    env = dict(os.environ if base is None else base)
    for name in env_patch.get("drop") or ():
        env.pop(name, None)
    env.update(env_patch.get("set") or {})
    return env


def run_one_attempt(store: Store, item: dict, opts: RunOptions, claude_cmd: list[str], clock: Clock, *,
                    cwd: str | None = None, env_patch: dict | None = None, system_note: str | None = None,
                    should_stop: Callable[[], bool] | None = None,
                    should_interrupt: Callable[[], str | None] | None = None) -> Outcome:
    """(2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.4 step 3) One attempt of a started item; the body run_loop had
    inline, unchanged for it (no cwd, env patch or note: the same argv and env). The parallel workers pass
    `cwd` (the copy's folder: runner.run_attempt reads item["cwd"] only), `env_patch`
    (worktree.attempt_env_patch), `system_note` (worktree.system_note, joined to the one
    --append-system-prompt value) and their own `should_stop` / `should_interrupt` (default: the queue
    status probe). Never raises for a failed build/launch: that is a FAILED outcome. StopRequested and
    KeyboardInterrupt propagate as before."""
    try:
        # (2026-10-21, add panel) the item's own provider/model/effort, and its own child env
        choice = attempt_choice(item, opts)
        extras, defaults = attempt_args(item, opts, choice)
        log_choice(item, choice, opts)
        run_item = item if cwd is None else dict(item, cwd=cwd)
        return run_attempt(
            run_item, claude_cmd=claude_cmd, prompts_dir=store.prompts_dir,
            runs_dir=store.runs_dir, max_turns=turns_for(item, opts),
            item_timeout_s=opts.item_timeout_s, poll_min=opts.poll_min, clock=clock,
            on_session=lambda sid, _id=item["id"]: _persist_session(store, _id, sid),
            extra_args=extras, should_stop=should_stop,
            should_interrupt=should_interrupt if should_interrupt is not None else status_probe(store, item["id"]),
            default_args=defaults, shell_allow=shell_allow_for(item, opts),
            env=_patched_env(choice.env, env_patch),
            # (2026-10-27, fix LOW) only the subscription's windows: another provider is
            # another account/key, its samples would mix into usage_samples.jsonl
            on_rate_limit=(usage.recorder(opts.usage_dir, lambda: clock.now().timestamp())
                           if choice.provider == providers.DEFAULT_PROVIDER else None),
            env_file=opts.env_file,   # (2026-10-27, SEC-M6 review)
            # (2026-10-29, C-L1) the note is free text: build_argv joins it, never scanned as an option
            system_note=system_note,
        )
    except providers.ProviderError as exc:
        # (2026-10-21, add panel) its provider cannot be used: fail THIS item, names only
        log.error("item %s: %s", item["id"], exc)
        return Outcome(FAILED, error=f"provider: {exc}")
    except InvalidStoredModel as exc:
        # (2026-10-22, v049 tester LOW-13) a malformed stored model: fail THIS attempt, clearly
        log.error("item %s: %s", item["id"], exc)
        return Outcome(FAILED, error=str(exc))
    except Exception as exc:
        # (2026-10-13, fix B1b) Building/launching blew up: fail THIS item, keep the
        # queue moving. An exception must never leave an item stuck in `running`.
        log.exception("item %s: attempt could not run", item["id"])
        return Outcome(FAILED, error=f"attempt could not run: {type(exc).__name__}: {exc}")


def release_due_waiting(store: Store, now: datetime) -> None:
    """(2026-10-27, tasks at once) Every `waiting` item whose wait_until is due (or unreadable) goes back to
    pending to resume its session: run_loop's former inline _release_due, unchanged; the parallel
    coordinator calls it too."""
    with store.transaction(backup=True) as tx:
        for it in tx:
            if it.get("status") == "waiting":
                wu = parse_iso(it.get("wait_until"))
                if wu is None or wu <= now:
                    it["wait_until"] = None
                    transition(it, "pending", "limit reset time reached; will resume session")


def _limit_hold_after_replay(store: Store, opts: RunOptions, clock: Clock, ignored: tuple | None,
                             ids: list[str]) -> bool:
    """(2026-10-27, integrate: limit_continue) A usage-limit outcome saved late gets the same `--on-limit
    ask` hold as one saved at once. Late means a kept outcome replayed from pending_outcomes, or an outcome
    retried from memory. The first of `ids` that is now `waiting` holds the queue (limit_hold): one hold,
    so one question. True = the queue is held and the runner exits. With `--on-limit auto` the item just
    waits out its limit, as before."""
    if opts.on_limit != ON_LIMIT_ASK or not ids:
        return False
    return any(limit_hold(store, i, clock, ignored) for i in ids)


def hold_until(store: Store, ignored: tuple | None, now: datetime) -> datetime | None:
    """(2026-10-26, usage forecast) The end of the queue's TIMED hold (`hold on --until`), when one is on
    and it is not the hold this runner was started past with --ignore-hold; the runner waits until then
    instead of exiting. None = no timed hold (an untimed one is hold_stops' business)."""
    try:
        held = store.read_hold(now)
        if held is None or not held.get("until") or (ignored is not None and store.hold_key(now) == ignored):
            return None
        return parse_iso(held["until"])
    except Exception as exc:  # never kill the runner over the check
        log.warning("timed hold check failed: %s", exc)
        return None


def hold_stops(store: Store, ignored: tuple | None, now: datetime | None = None) -> bool:
    """(2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 2) True when the queue is on hold (and it
    is not the hold this runner was started past with --ignore-hold): the runner takes no next item and
    exits. Checked before every next item, so `hold on` never stops an attempt that is running.
    (2026-10-22, v049 tester LOW-9) `ignored` is that hold's Store.hold_key(): a hold that appears or
    changes later (another since, or the file written anew) stops the runner."""
    try:
        key = store.hold_key(now)
    except Exception as exc:  # never kill the runner over the check
        log.warning("hold check failed: %s", exc)
        return False
    if key is None or (ignored is not None and key == ignored):
        return False
    log.warning("queue is on hold (since %s): the runner takes no next item and exits; "
                "`autopilot hold off` releases it", key[0] or "?")
    return True


def limit_hold(store: Store, item_id: str, clock: Clock, ignored: tuple | None = None) -> bool:
    """(2026-10-26, limit_continue) `run --on-limit ask`: a usage limit parked `item_id` (status
    `waiting`, wait_until kept, session kept). Put the queue on a limit hold (reason + reset time in
    hold.json; a hold already on is kept as it is) and tell the caller to exit: after the reset nothing
    restarts by itself; `hold off` and a new runner resume the same session. False = the item is not
    waiting (e.g. the max-limit-waits cap failed it): the runner goes on as before.
    (2026-10-27, limit_continue fix round 2) `ignored`: the key of the owner hold this runner was started
    past (--ignore-hold, "Start anyway"). That hold becomes the limit hold (Store.set_hold over_owner), so
    the extension asks and "Release hold" restarts the queue; a hold put on during the run is kept.
    (2026-10-27, integrate: hold kinds compose) a TIMED hold put on during the run that ends at or after the
    reset ("Queue for after the reset") is the owner's answer already: it is kept and False is returned, so
    the runner waits it out (hold_until) and then resumes the item. One that ends before the reset becomes
    the limit hold (Store.set_hold), which gives it back on the answer."""
    try:
        now = clock.now()
        item = next((i for i in store.load() if i.get("id") == item_id), None)
        if not item or item.get("status") != "waiting":
            return False
        held = store.set_hold(now, reason=HOLD_REASON_LIMIT, resets_at=item.get("wait_until"),
                              over_owner=ignored is not None and store.hold_key(now) == ignored)
    except Exception as exc:  # never kill the runner over the hold: it then waits out the limit as `auto`
        log.error("item %s: could not put the queue on a usage-limit hold (%s); waiting out the limit", item_id, exc)
        return False
    if held.get("until") and held.get("reason") != HOLD_REASON_LIMIT:
        log.warning("item %s: usage limit; --on-limit ask: the queue's timed hold (until %s) already ends after "
                    "the reset (%s); the runner waits for it, then resumes the same session", item_id,
                    held["until"], item.get("wait_until"))
        return False
    log.warning("item %s: usage limit; --on-limit ask: queue on hold (%s, resume at %s); the runner exits. "
                "`hold off` and `run` resume the same session", item_id,
                "usage limit" if held.get("reason") == HOLD_REASON_LIMIT else f"already held since {held.get('since')}",
                item.get("wait_until"))
    return True


def record_outcome(store: Store, item_id: str, out: Outcome, opts: RunOptions, clock: Clock) -> bool:
    """Save an attempt's outcome. (2026-10-13, amendment M6) Any error is logged and the save is
    retried once.
    (2026-10-26, improve_core) If that fails too, the outcome is kept in a file in the data dir
    (save_pending_outcome) and applied on the next pass or start (apply_pending_outcomes); the item stays
    `running` meanwhile, so no runner starts it again. It used to go back to `pending`, which re-ran a
    finished task. False only when neither the queue nor that file took the outcome (the caller keeps it)."""
    if out.kind == LIMIT and out.handoff is None:
        # (2026-10-26, limit_continue) read before the store lock is taken: a run log can be megabytes
        try:
            out.handoff = limit_handoff.from_run_log(out.log_path)
        except Exception:  # (2026-10-27, fix LOW-3) e.g. RecursionError on a deeply nested line: never stuck
            log.exception("item %s: could not build the handoff from %s", item_id, out.log_path)
            out.handoff = None

    def fn(it: dict) -> None:   # (2026-10-27, tasks at once) + the merge rule of a copy item (SPEC_V05_PARALLEL 3.1)
        outcome_applier(it, out, opts, clock)
    for attempt in (1, 2):
        try:
            store.mutate(item_id, fn)
            return True
        except KeyError:
            log.warning("item %s was removed during its attempt; outcome %s dropped", item_id, out.kind)
            return True
        except Exception as exc:
            log.error("item %s: could not save outcome %s (try %d/2): %s: %s", item_id, out.kind,
                      attempt, type(exc).__name__, exc)
    return save_pending_outcome(store, item_id, out, clock.now())


def pending_outcome_ids(store: Store) -> set[str]:
    """(2026-10-26, improve_core) The ids of the items with a kept outcome (save_pending_outcome)."""
    try:
        return {p.stem for p in store.pending_outcomes_dir.glob("*.json")}
    except OSError:
        return set()


def save_pending_outcome(store: Store, item_id: str, out: Outcome, now: datetime) -> bool:
    """(2026-10-26, improve_core) Keep an outcome the queue would not take as
    <data dir>/pending_outcomes/<item id>.json (next to queue.json, never anywhere else; the same content
    queue.json gets). True when the file is written."""
    data = {f.name: getattr(out, f.name) for f in dataclasses.fields(Outcome)}
    data["reset_at"] = iso(out.reset_at) if isinstance(out.reset_at, datetime) else None
    data["log_path"] = str(out.log_path) if out.log_path else None
    path = store.pending_outcomes_dir / f"{item_id}.json"
    try:
        if not _PENDING_ID_RE.match(item_id):
            raise ValueError(f"item id {item_id!r} is not usable as a file name")
        store.pending_outcomes_dir.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps({"item_id": item_id, "saved_at": iso(now), "outcome": data},
                                           ensure_ascii=False))
    except (OSError, TypeError, ValueError) as exc:
        log.error("item %s: outcome %s could not be kept in %s either: %s: %s", item_id, out.kind,
                  store.pending_outcomes_dir, type(exc).__name__, exc)
        return False
    log.warning("item %s: outcome %s kept in %s; it is applied once the queue can be saved, and the item "
                "stays running meanwhile, so it is not started again", item_id, out.kind, path)
    return True


def _outcome_from(data: dict) -> Outcome:
    known = {f.name for f in dataclasses.fields(Outcome)}
    kw = {k: v for k, v in data.items() if k in known}
    kw["reset_at"] = parse_iso(kw.get("reset_at")) if isinstance(kw.get("reset_at"), str) else None
    kw["log_path"] = Path(kw["log_path"]) if isinstance(kw.get("log_path"), str) else None
    return Outcome(**kw)


def apply_pending_outcomes(store: Store, opts: RunOptions, clock: Clock, limits: list[str] | None = None) -> int:
    """(2026-10-26, improve_core) Apply the kept outcomes (save_pending_outcome); returns how many were
    applied. Each goes only to its item while that is still `running` (the attempt it belongs to): an item
    the owner changed or removed meanwhile keeps its state and the outcome is dropped (logged). A store
    error propagates with every file left in place, for the caller's retry (_retry_store).
    (2026-10-27, integrate: limit_continue) `limits` collects the ids whose applied outcome was a usage
    limit, so the runner can put the ask hold on for it (_limit_hold_after_replay)."""
    applied = 0
    for path in sorted(store.pending_outcomes_dir.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
            item_id, out = rec["item_id"], _outcome_from(rec["outcome"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.error("kept outcome %s is unreadable (%s: %s); moved aside as .bad", path, type(exc).__name__,
                      exc)
            with contextlib.suppress(OSError):
                os.replace(path, path.with_suffix(".bad"))
            continue
        seen: dict = {}

        def fn(it: dict, out=out) -> None:   # (2026-10-27, tasks at once) + the merge rule (SPEC_V05_PARALLEL 3.1)
            outcome_applier(it, out, opts, clock)

        def only_running(it: dict, fn=fn, seen=seen) -> None:
            seen["status"] = it.get("status")
            # (2026-10-27, tasks at once, SPEC_V05_PARALLEL 3.1) a `running` item whose merge is pending, waiting or
            # merging had its outcome applied already (it waits for the merger): never apply one twice
            if (it.get("merge") or {}).get("state") in MERGE_ACTIVE:
                seen["merging"] = True
                return
            if it.get("status") == "running":
                fn(it)
        try:
            store.mutate(item_id, only_running)
        except KeyError:
            log.warning("item %s was removed; its kept outcome %s is dropped", item_id, out.kind)
        except (StoreError, OSError):
            raise
        except Exception as exc:  # a bug applying it: never kill the runner, never retry it every pass
            log.error("item %s: kept outcome %s could not be applied (%s: %s); moved aside as .bad", item_id,
                      out.kind, type(exc).__name__, exc)
            with contextlib.suppress(OSError):
                os.replace(path, path.with_suffix(".bad"))
            continue
        else:
            if seen.get("merging"):
                log.warning("item %s: its outcome is applied already and its merge is pending; the kept outcome "
                            "%s is dropped", item_id, out.kind)
            elif seen.get("status") == "running":
                applied += 1
                log.info("item %s: kept outcome %s applied", item_id, out.kind)
                if limits is not None and out.kind == LIMIT and item_id not in limits:
                    limits.append(item_id)
            else:
                log.warning("item %s is %s now, not running; its kept outcome %s is dropped", item_id,
                            seen.get("status"), out.kind)
        with contextlib.suppress(OSError):
            path.unlink()
    return applied


def _drop_stale_stop_request(store: Store, lock_t0: float) -> None:
    """(2026-10-13) A stop.request older than our lock belongs to a runner that died before it saw
    it; a newer one (written between lock and here) is honoured by the loop."""
    try:
        mtime = store.stop_path.stat().st_mtime
    except FileNotFoundError:
        return
    except OSError as exc:
        log.error("cannot stat %s: %s", store.stop_path, exc)
        return
    if mtime < lock_t0 - 1.0:  # 1 s slack for coarse file-time resolution
        log.warning("removing stale %s from an earlier runner", store.stop_path.name)
        store.clear_stop_request()


def _back_to_pending(store: Store, item_id: str, reason: str) -> None:
    def _go(it: dict) -> None:
        it.pop(PAUSE_UNRELEASED, None)  # (security review L6) this runner lets go of the attempt
        if it.get("status") == "running":
            if is_goal(it):
                it["goal_continue"] = False  # a stopped round resumes with the resume prompt
            settle_reply(it, answered=False)  # (2026-10-15) unknown whether it was read: keep it
            transition(it, "pending", reason)

    try:
        store.mutate(item_id, _go)
    except Exception as exc:  # must still release the lock
        log.error("could not reset item %s to pending: %s", item_id, exc)


def _start_item(store: Store, item_id: str, clock: Clock) -> dict | None:
    def _go(it: dict) -> None:
        if it.get("status") != "pending":
            return
        it["attempts"] = int(it.get("attempts") or 0) + 1
        it["started_at"] = iso(clock.now())
        it["error"] = None
        if is_goal(it) and not it.get("goal_started_at"):
            it["goal_started_at"] = it["started_at"]  # never reset by retry (SPEC_GOALS section 1)
        # (2026-10-17, 0.4.1 R6) a reply/resume long after the last attempt, with a big context,
        # starts a new session (decided after the reply is moved in flight, below)
        go_fresh = wants_fresh_resume(it, clock.now())
        if it.get("pending_reply") and (it.get("session_id") or it.get("fresh")):
            # (2026-10-15, SPEC_V03 5.2) cleared once the attempt starts; settle_reply() puts it
            # back if Claude never answered. The reply entry records which attempt carried it.
            it["reply_in_flight"] = it["pending_reply"]
            it["pending_reply"] = None
            it.pop("reply_from", None)  # (2026-10-21, edit reply) sent: no state to go back to
            for rep_ in reversed(it.get("replies") or []):
                if isinstance(rep_, dict) and rep_.get("attempt") is None:
                    rep_["attempt"] = it["attempts"]
                    break
        if go_fresh:
            mode = FRESH_ROUND if (is_goal(it) and it.get("goal_continue")) else FRESH_RESUME
            log.info("item %s: last attempt ended %s with context %s; starting a new session (%s); "
                     "session %s kept in prior_sessions", it["id"], it.get("last_ended_at"),
                     it.get("last_ctx"), mode, it.get("session_id"))
            start_fresh(it, mode)
        transition(it, "running", f"attempt {it['attempts']}"
                   + (f", resuming session {it['session_id']}" if it.get("session_id") else ""))

    try:
        item = store.mutate(item_id, _go)
    except KeyError:
        # (2026-10-13, fix B6) Removed between load and start: skip it, do not kill the runner.
        log.warning("item %s vanished from the queue before it could start; skipping", item_id)
        return None
    return item if item.get("status") == "running" else None


def _persist_session(store: Store, item_id: str, sid: str) -> None:
    try:
        store.update(item_id, session_id=sid)
        log.info("item %s: session_id=%s persisted", item_id, sid)
    except Exception as exc:  # never kill the attempt over bookkeeping; the result carries it too
        log.error("item %s: could not persist session_id %s: %s", item_id, sid, exc)

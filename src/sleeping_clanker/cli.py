"""`autopilot` command line (2026-10-13, spec section 5). Exit codes: 0 OK, 1 failure, 2 usage.

(2026-10-13, SPEC_VSCODE section 2) Machine-readable `--json` for add/list/show/status and a graceful
`stop` for the VS Code extension. JSON is printed ASCII-escaped, so it survives any console code
page when a parent process reads stdout through a pipe.

(2026-10-13, SPEC_GOALS section 2) `add --goal/--max-rounds/--max-hours/--attach`, `--profile plan`,
`pause` and `resume`.

(2026-10-13, SPEC_PROJECTS section 2) `--project DIR` is a common option, so it works both before
and after the subcommand (`autopilot --project X list` == `autopilot list --project X`). It selects
<base>/projects/<slug>-<hash8>; without it the base dir (legacy global queue) is used. With
`--project`, `--data-dir` / CLAUDE_AUTOPILOT_HOME name the BASE dir (spec amendment H1). Only `add`
and `run` create a project dir; read-only commands never do (amendment M11). New commands:
`projects`, `where`, `migrate-global`.

(2026-10-15, SPEC_V03 section 2.2) Named queues ("lanes"): the common option `--lane NAME` picks a
lane of the selected project (or of the base dir); `default` is the project dir itself. `add` and
`run` create a missing lane; read-only commands never do. New commands: `lanes` and `lane-remove`;
`projects --json` rows gain `lanes`, and `where --json` gains `lane`.

(2026-10-15, SPEC_V03 section 5.2) `reply ID (--text TEXT | --file PATH) [--profile NAME]` answers an
item that has a session (blocked/done/failed/cancelled): the reply is new work, so the item goes to
the END of the queue (a new seq) and its next attempt resumes the session with the reply.
(2026-10-21, reply to paused) A paused item with a session takes a reply too: the reply un-pauses it
(pending, or waiting while its usage-limit wait is still ahead); one with a reply queued is refused.

(2026-10-21, edit) `edit ID [--text TEXT | --file PATH] [--name NAME] [--profile NAME]` changes an
item in place (status and place in the queue kept). The prompt only before the item's first session;
a started item gets new instructions with `reply`.

(2026-10-21, start now) `first ID [--json]` moves a pending item to the front of its queue (a seq
below every other item's): the runner starts it next, after the attempt that is running now. A
subtask moves with its main task.

(2026-10-26, owner task 07254297 "drag in priority") `move ID (--top | --bottom | --before ID | --after
ID) [--json]` puts a not-done item (not running, done or cancelled) at another place in its queue, by
seq as `first` does: the runner follows the new order at its next pick. A main task moves with its
subtasks; a subtask moves only among its main task's subtasks.

(2026-10-21, v049 dontask_hold, docs/SPEC_V049_DONTASK_HOLD.md) `hold on|off|status` holds one queue
(hold.json in its data dir): `run` refuses it with exit 3 unless `--ignore-hold`, and a live runner
exits after its current item. `status`, `list` and the `lanes` rows show it. A blocked item's
`denial_block` (what was denied, the owner's fix) is printed by `show` and under its `list` row.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__, providers, report, usage
from .version import describe as describe_version  # (2026-10-26) full version + build stamp
from .claude_path import ClaudePathError, ClaudeResolver, check_not_shim
from .clock import Clock, iso, parse_iso
from .denials import block_text, hint_text
from .denials import CONTROLS_RE, strip_controls  # (2026-10-27, SEC-L17)
from .log import get_logger, setup_logging, shutdown_logging
from .log import ROOT_NAME  # (2026-10-27, SEC-L17) the console handler gets the control filter
from .goals import (BG_REDO, FRESH_AT_CTX_LIMIT, FRESH_EVERY_LIMIT, KIND_GOAL, KIND_TASK, MAX_ATTACHMENTS,
                    MAX_HOURS_LIMIT, MAX_ROUNDS_LIMIT, UNLIMITED_ROUNDS, is_goal, rounds_cap_text)
from .loop import (DEFAULT_GOAL_MAX_TURNS, DEFAULT_TASK_MAX_TURNS, PAUSE_UNRELEASED, PAUSE_UNRELEASED_MAX_S,
                   RunOptions, dry_run, run_loop, runnable_pending, transition)
from .loop import ON_LIMIT_AUTO, ON_LIMIT_CHOICES  # (2026-10-26, limit_continue)
from .profiles import (AGENTS_TEXTS, FULL, NO_SHELL_PROFILES, PROFILES, DEFAULT_PROFILE, SHELL_ALLOW_MAX,
                       AllowListError,
                       agents_label, allow_file, load_allow_list, normalise_allow_entry,
                       parse_agents_text, save_allow_list)
from .profiles import ultracode_refusal  # (2026-10-27, modes fix HIGH)
from .runner import (DEFAULT_EFFORT, DEFAULT_MAX_BUDGET_USD, DEFAULT_MODEL, EFFORTS, ENV_EFFORT,
                     ENV_MODEL, BILLING_API, ForbiddenFlagError, check_extra_args, usage_text,
                     valid_task_model)
from .store import (DEFAULT_LANE, LANES_DIRNAME, PROJECTS_DIRNAME, REMOVED_LANES_DIRNAME, STATUSES,
                    LockHeld, Store, StoreError, default_data_dir, has_any_queue, held_by_parent,
                    is_default_lane, lane_data_dir, list_lanes, new_item, next_seq, parent_label, project_data_dir,
                    project_key, project_root, queue_order_key, read_project_meta, run_order_key, subtasks_of,
                    valid_lane_name, with_defaults)
from .store import move_seq, order_peers, run_order_key  # (2026-10-26, move)
from .store import HOLD_REASON_LIMIT  # (2026-10-27, integrate: limit_continue review LOW-2) hold limit-off
# (2026-10-27, "Tasks at once" U3) the parallel runner's files, order rules and private copies
from . import order, worktree
from .runner import CLAUDE_MD as RUN_CLAUDE_MD
from .store import TASKS_AT_ONCE_MAX, lock_identity

log = get_logger(__name__)

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
EXIT_HELD = 3  # (2026-10-21, v049 dontask_hold) `run` refused: the queue is on hold
CREATING_COMMANDS = ("add", "run")  # the only commands that create a project dir (amendment M11)
# (2026-10-15) Commands that work on a whole project (all its lanes) or on the base queue refuse --lane.
# (2026-10-20, 0.4.7 providers) `providers` reads the project's .env, which is shared by its lanes.
LANELESS_COMMANDS = ("lanes", "lane-remove", "projects", "migrate-global", "shell-allow", "providers")
# (2026-10-20, 0.4.4 logfix G) read-only commands the extension polls: their start/stop lines were 96-99% of
# app.log, so they are logged only with -v
QUIET_COMMANDS = ("lanes", "status", "list", "show", "where", "projects", "worktrees")
ACTIVE_STATUSES = ("pending", "running", "waiting", "paused")  # lane-remove refuses these
RENAME_DELAYS_S = (0.05, 0.1, 0.2, 0.4, 0.5, 0.75)  # ~2 s, as store._replace_with_retry
REPLY_STATUSES = ("blocked", "done", "failed", "cancelled", "paused")  # (2026-10-15, SPEC_V03 5.2)
# (2026-10-21, edit reply) the fields `reply` changes and `edit --drop-reply` puts back
# ((2026-10-21, reply to paused) + paused_from and wait_until)
REPLY_FROM_KEYS = ("status", "seq", "error", "finished_at", "blocked_question", "paused_from", "wait_until")
REPLY_WITHDRAWN_ERROR = "blocked: the owner's reply was withdrawn; reply again or retry"
# (2026-10-17, 0.4.1 R8) a CLAUDE.md over this size is worth a warning at `add`: Claude loads it
# into every new session (the 126 KB one in token_v04 tripled the starting context).
CLAUDE_MD_WARN_BYTES = 40 * 1024
# (2026-10-27, U3: SEC-H1 follow-up) runs pass `--setting-sources user` and re-supply only <task folder>\CLAUDE.md
# (runner.project_instructions): CLAUDE.local.md, .claude/CLAUDE.md and parent folders' files are never read,
# so only that one file is worth a size warning
CLAUDE_MD_NAMES = (RUN_CLAUDE_MD,)
# (2026-10-27, SEC-H1 add-on) a folder's own claude.exe settings: runs pass --setting-sources user, so
# their hooks, env (ANTHROPIC_BASE_URL ...) and permissions never apply; `add` says so in one line
PROJECT_SETTINGS_NAMES = ("settings.json", "settings.local.json")
PROJECT_SETTINGS_WARNING = ("{0} holds .claude/{1}: its hooks/env are ignored by Sleeping Clanker runs "
                            "(they load your user settings only)")
# (2026-10-27, SEC-L24) a data dir inside the task folder (or the reverse) lets a task edit its own queue
# (queue.json, shell_allow.json): `add` and `run` refuse it
NESTED_DATA_DIR = ("the task folder {0} and the data dir {1} nest (one holds the other), so a task could "
                   "edit its own queue; pick another folder or move the data dir (--data-dir or "
                   "$CLAUDE_AUTOPILOT_HOME)")
# the statuses `run` may still start (done, failed and cancelled items never run again by themselves)
NESTED_CHECK_STATUSES = ("pending", "running", "waiting", "paused", "blocked")
# (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.6/3.8) `merge` flags; a reply never runs a destructive one
MERGE_FLAGS = ("let-claude", "retry", "take-task", "discard", "now", "undo")
REPLY_MERGE_ACTIONS = ("let-claude", "retry")
# (2026-10-27 autorun; 2026-10-29, integrate round 9 autopick) `reply/merge --by`: an automatic answer
REPLY_AUTHORS = ("autorun", "autopick")
OPTION_REPLY_RE = re.compile(r"^\s*option\s+(\d+)\s*:\s*(.*?)\s*$", re.I | re.S)
PAUSE_MERGING = "its changes are being merged; wait, or cancel it"
CANCEL_MERGE_HELD = "its merge is held: `merge --now` or `merge --discard`"
IN_PLACE_REFUSED = "merge or discard its copy first"


class UsageError(Exception):
    pass


def _hhmm(value: str) -> str:
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("expected HH:MM (24-hour local time)")
    return value


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def _round_cap(value: str) -> int:
    n = int(value)
    # (2026-10-13, tester fix 1) 1..500; (2026-10-26, owner) -1 = unlimited, until stopped
    if not (1 <= n <= MAX_ROUNDS_LIMIT or n == UNLIMITED_ROUNDS):
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_ROUNDS_LIMIT}, or -1 for unlimited")
    return n


def _int_range(limit: int):
    def parse(value: str) -> int:
        n = int(value)
        if not 0 <= n <= limit:
            raise argparse.ArgumentTypeError(f"must be a whole number from 0 to {limit} (0 = off)")
        return n
    return parse


def _tasks_at_once(value: str) -> int:
    """(2026-10-27, "Tasks at once" U3) 1..TASKS_AT_ONCE_MAX, else a usage error (exit 2)."""
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a whole number: {value!r}") from None
    if not 1 <= n <= TASKS_AT_ONCE_MAX:
        raise argparse.ArgumentTypeError(f"must be 1 to {TASKS_AT_ONCE_MAX}")
    return n


def _order_flags(sp: argparse.ArgumentParser) -> None:
    """(2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 5.1) add/edit: what the task waits for and where it runs."""
    aft = sp.add_mutually_exclusive_group()
    aft.add_argument("--after", default=None, metavar="ID",
                     help="start only after this task of the same queue is done and merged (tasks at once > 1)")
    aft.add_argument("--after-previous", action="store_true",
                     help="start only after the last unfinished task of the queue (in run order)")
    aft.add_argument("--no-after", action="store_true", help="wait for no other task")
    inp = sp.add_mutually_exclusive_group()
    inp.add_argument("--in-place", dest="in_place", action="store_const", const=True, default=None,
                     help="run in the project folder itself, alone (no private copy)")
    inp.add_argument("--no-in-place", dest="in_place", action="store_const", const=False,
                     help="run in a private copy when tasks at once > 1 (the default)")


def _budget(value: str) -> float:
    b = float(value)
    if not (b >= 0 and b < float("inf")):  # also refuses nan
        raise argparse.ArgumentTypeError("must be a dollar amount >= 0 (0 = no budget cap)")
    return b


def _effort(value: str) -> str:
    if value and value not in EFFORTS:
        raise argparse.ArgumentTypeError(f"must be one of {', '.join(EFFORTS)} (or empty: do not pass)")
    return value


# (2026-10-26, modes) `--ultracode` / `--autorun`: on or off
ON_OFF = {"on": True, "off": False}


def _on_off(value: str) -> bool:
    v = value.strip().lower()
    if v not in ON_OFF:
        raise argparse.ArgumentTypeError("must be on or off")
    return ON_OFF[v]


def _ultracode_follows(item: dict, how: str) -> bool:
    """(2026-10-27, modes fix HIGH) After a profile or subagent-limit change: an ultracode task that can no
    longer get the Workflow tool (profiles.ultracode_refusal) gets ultracode off, logged; never a flag
    nobody acts on. True = it was turned off."""
    if item.get("ultracode") is not True:
        return False
    why = ultracode_refusal(item.get("profile") or DEFAULT_PROFILE, item.get("max_agents"))
    if not why:
        return False
    log.info("item %s: ultracode off (%s): %s", item["id"], how, why)
    item["ultracode"] = False
    return True


def _task_model(value: str) -> str:
    """(2026-10-21, task model) A task's own model id; "" = none (the run's model)."""
    value = value.strip()
    if value and not valid_task_model(value):
        raise argparse.ArgumentTypeError("expected a model id such as claude-sonnet-5-5 or opus (letters, "
                                         "digits and . _ : / [ ] -, at most 100 chars, not led by '-')")
    return value


def _task_provider(value: str) -> str:
    """(2026-10-21, add panel) A task's own provider: one of `providers`' names; "" = none (the run's)."""
    value = value.strip().lower()
    if value and value not in providers.PROVIDER_NAMES:
        raise argparse.ArgumentTypeError(f"must be one of {', '.join(providers.PROVIDER_NAMES)} "
                                         "(or empty: the run's provider)")
    return value


def _check_run_choice(item_id: str | None, provider: str | None, effort: str | None, profile: str) -> None:
    """(2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) The rules `run` applies, checked when a task's
    own provider/effort/profile is set (add, edit; the runner checks again per attempt): an effort only
    for a provider that takes one, and a provider with an API key never with a shell profile or `full`
    (runner.provider_key_refusal). Names only, never a key."""
    if not provider:
        return
    known = providers.provider(provider)
    what = f"item {item_id}: " if item_id else ""
    if effort and not known.supports_effort:
        raise UsageError(f"{what}provider {provider} takes no --effort (an Anthropic-model feature); "
                         "give --effort= (empty) or another provider")
    if providers.needs_key(provider) and profile not in NO_SHELL_PROFILES:
        raise UsageError(f"{what}profile {profile} refused with provider {provider}: it puts an API key in "
                         "the child env, and a shell profile could print it (`Get-ChildItem env:`). Use "
                         f"profile {', '.join(NO_SHELL_PROFILES)}, or provider {providers.DEFAULT_PROVIDER}.")


def _check_env_provider(args, profile: str) -> None:
    """(2026-10-22, v049 tester LOW-3) A task with no provider of its own runs on the provider `run` picks
    without --provider: $CLAUDE_AUTOPILOT_PROVIDER, else providers.env's (the `selected` of `providers
    --json`). When that one has an API key, a shell profile or `full` is refused here, at add time, as
    `run` would refuse it per attempt (runner.provider_key_refusal). Names only, never a key."""
    if profile in NO_SHELL_PROFILES:
        return
    _path, values, _candidates = _load_keys(args)
    name = providers.select_name(None, values)
    if name not in providers.PROVIDER_NAMES or not providers.needs_key(name):
        return   # an unknown name fails at run with its own error
    raise UsageError(f"profile {profile} refused: the task has no provider of its own, so it runs on the "
                     f"runner's provider {name} (${providers.ENV_PROVIDER} or providers.env), which puts an "
                     "API key in the child env, and a shell profile could print it (`Get-ChildItem env:`). Use "
                     f"profile {', '.join(NO_SHELL_PROFILES)}, or give the task "
                     f"--provider {providers.DEFAULT_PROVIDER}.")


def _agents_cap(value: str) -> str:
    """(2026-10-21, subagents) `--max-agents`: validated here, turned into the field by the handler
    (profiles.parse_agents_text), so "default" and a missing flag stay distinguishable."""
    try:
        parse_agents_text(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    return value


def _hours_cap(value: str) -> float:
    h = float(value)
    if not 0 < h <= MAX_HOURS_LIMIT:  # also refuses nan and inf (tester fix 1)
        raise argparse.ArgumentTypeError(f"must be a number > 0 and <= {MAX_HOURS_LIMIT:g}")
    return h


def _hold_until_arg(value: str):
    """(2026-10-26, usage forecast) `hold on --until`: an ISO 8601 time (no zone = UTC)."""
    try:
        dt = parse_iso(value.strip())
    except ValueError:
        dt = None
    if dt is None:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 time: {value!r}")
    return dt


def build_parser() -> argparse.ArgumentParser:
    # Common options work both before and after the subcommand (SUPPRESS keeps the outer value).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help="data directory")
    common.add_argument("--claude", default=argparse.SUPPRESS, help="path to claude.exe")
    common.add_argument("--project", default=argparse.SUPPRESS, metavar="DIR",
                        help="use this project folder's own queue (<base>/projects/<slug>-<hash8>)")
    common.add_argument("--lane", default=argparse.SUPPRESS, metavar="NAME",
                        help="a named queue of the project (default: `default`, the project dir)")
    common.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                        help="DEBUG output on the console")

    p = argparse.ArgumentParser(prog="autopilot", parents=[common],
                                description="Run queued Claude Code tasks unattended.")
    p.add_argument("--version", action="version", version=f"%(prog)s {describe_version(__version__)}")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", parents=[common], help="queue a prompt")
    src = a.add_mutually_exclusive_group(required=True)
    src.add_argument("prompt", nargs="?", help="the prompt text")
    src.add_argument("--file", help="read the prompt from a UTF-8 file")
    a.add_argument("--cwd", default=None,
                   help="directory the task runs in (default: the --project folder, else .)")
    a.add_argument("--name", help="optional label")
    a.add_argument("--profile", choices=sorted(PROFILES), default=None,
                   help=f"permission profile (default {DEFAULT_PROFILE}; with --parent the main task's)")
    # (2026-10-21, subtasks) runs after the main task is done; cwd and profile default to the main task's
    a.add_argument("--parent", default=None, metavar="ID",
                   help="add a subtask under this main task (same queue; a subtask's id means its main task)")
    # (2026-10-21, task model) wins over `run --model` and a --claude-arg --model extra for this task
    a.add_argument("--model", type=_task_model, default=None,
                   help="this task's own model, e.g. claude-sonnet-5-5 (default: the run's model; with "
                        "--parent the main task's)")
    # (2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) win over `run --provider/--effort` for this task
    a.add_argument("--provider", type=_task_provider, default=None, metavar="NAME",
                   help="this task's own model provider (`providers` lists them; default: the run's; with "
                        "--parent the main task's)")
    a.add_argument("--effort", type=_effort, default=None, metavar="LEVEL",
                   help=f"this task's own effort: {', '.join(EFFORTS)} (default: the run's; with --parent "
                        "the main task's)")
    # (2026-10-21, subagents, docs/SPEC_V049_AGENTS.md) how many subagents this task may start
    a.add_argument("--max-agents", type=_agents_cap, default=None, metavar="N",
                   help=f"subagents this task may start: {AGENTS_TEXTS} (default: default, i.e. the "
                        "profile decides; only `full` offers subagents by itself)")
    # (2026-10-26, modes) per task; a missing flag = off (with --parent: the main task's)
    a.add_argument("--ultracode", type=_on_off, default=None, metavar="on|off",
                   help='pass --settings {"ultracode":true} to claude and offer the Workflow tool: standing '
                        "workflow orchestration, many agents and more usage; needs the full profile, subagents "
                        "and a model that supports it (default off)")
    a.add_argument("--autorun", type=_on_off, default=None, metavar="on|off",
                   help="the VS Code extension answers this task's option questions with the best option "
                        "itself (default off; the core only keeps the flag)")
    a.add_argument("--json", action="store_true", help='print {"id": ...} instead of the bare id')
    a.add_argument("--goal", action="store_true",
                   help="a goal: resumed round after round until Claude reports GOAL_COMPLETE")
    a.add_argument("--max-rounds", type=_round_cap, default=None, help="goal cap (default 50, -1 = unlimited; --goal only)")
    a.add_argument("--max-hours", type=_hours_cap, default=None, help="goal cap (default 72; --goal only)")
    a.add_argument("--attach", action="append", default=[], metavar="PATH",
                   help="file or folder to read first (repeatable; must exist)")
    a.add_argument("--fresh-at-ctx", type=_int_range(FRESH_AT_CTX_LIMIT), default=None, metavar="N",
                   help="goal: start the next round in a new session when the context reached N "
                        "tokens (default 100000; 0 = off; --goal only)")
    a.add_argument("--fresh-every", type=_int_range(FRESH_EVERY_LIMIT), default=None, metavar="N",
                   help="goal: start a new session every N rounds (default 0 = off; --goal only)")
    _order_flags(a)   # (2026-10-27, "Tasks at once" U3)

    ls = sub.add_parser("list", parents=[common], help="list queue items")
    ls.add_argument("--all", action="store_true", help="include done and cancelled items")
    ls.add_argument("--json", action="store_true", help="print a JSON array of queue items")

    for name, hlp in (("show", "show one item"), ("remove", "delete an item"),
                      ("cancel", "cancel an item"), ("retry", "set an item back to pending"),
                      ("pause", "pause an item (a running one is stopped within ~1 s)"),
                      ("resume", "resume a paused item")):
        sp = sub.add_parser(name, parents=[common], help=hlp)
        if name == "remove":
            # (2026-10-21, tester round 2 L-5) several ids = one transaction: all go, or none does
            sp.add_argument("id", nargs="+", help="one or more item ids (all are removed, or none)")
        else:
            sp.add_argument("id")
        if name == "show":
            sp.add_argument("--json", action="store_true", help="print the item as one JSON object")
        if name == "retry":  # (2026-10-13, review L3) raise a goal's caps when retrying it
            sp.add_argument("--max-rounds", type=_round_cap, default=None, help="new goal round cap (-1 = unlimited)")
            sp.add_argument("--max-hours", type=_hours_cap, default=None, help="new goal hours cap")
            # (2026-10-20, 0.4.4 logfix B) the same semantics as `reply --profile`
            sp.add_argument("--profile", choices=sorted(PROFILES), default=None,
                            help="switch the item's profile for the next attempt")

    # (2026-10-21, start now) the Task view's "Start now"
    fp = sub.add_parser("first", parents=[common],
                        help="move a pending item to the front of its queue (the runner starts it next)")
    fp.add_argument("id")
    fp.add_argument("--json", action="store_true", help='print {"id", "moved", "ahead"}')

    # (2026-10-26, owner task 07254297) the Queue view's drag and drop, Move to Top / Move to Bottom
    mv = sub.add_parser("move", parents=[common],
                        help="move a not-done item to another place in its queue (the runner follows the new order)")
    mv.add_argument("id")
    mvw = mv.add_mutually_exclusive_group(required=True)
    mvw.add_argument("--top", action="store_true", help="to the front of the queue")
    mvw.add_argument("--bottom", action="store_true", help="to the end of the queue")
    mvw.add_argument("--before", metavar="ID", help="right before this item")
    mvw.add_argument("--after", metavar="ID", help="right after this item (and its subtasks)")
    mv.add_argument("--json", action="store_true", help='print {"id", "moved", "ahead"}')

    rp = sub.add_parser("reply", parents=[common],
                        help="answer a blocked/finished/paused item; it resumes the same session (end of queue)")
    rp.add_argument("id")
    rsrc = rp.add_mutually_exclusive_group(required=True)
    rsrc.add_argument("--text", help="the reply text")
    rsrc.add_argument("--file", help="read the reply from a UTF-8 file")
    rp.add_argument("--profile", choices=sorted(PROFILES), default=None,
                    help="switch the item's profile for the next attempt")
    # (2026-10-27, modes fix round 2) who answered: the VS Code Task view shows such a reply as an
    # "Autorun picked" note instead of the owner's reply; absent = the owner (as before)
    # (2026-10-29, integrate round 9) + "autopick": an Auto-pick best answer; an automatic answer (either) is not
    # the owner's use of the queue (the extension's idle FULL ACCESS check, lib/fullaccess.js lastOwnerUse)
    rp.add_argument("--by", choices=REPLY_AUTHORS, default=None,
                    help="record the reply as an automatic answer: the task's Autorun or Auto-pick best "
                         "(stored as replies[i].by)")
    rp.add_argument("--json", action="store_true", help='print {"id", "status", "seq", "replies"}')

    # (2026-10-21, edit) change a queued item in place
    ed = sub.add_parser("edit", parents=[common],
                        help="change an item's prompt (before it has started), its reply that was not "
                             "sent yet, name, profile, provider, model, effort or subagent limit")
    ed.add_argument("id")
    esrc = ed.add_mutually_exclusive_group()
    esrc.add_argument("--text", default=None, help="the new prompt text")
    esrc.add_argument("--file", default=None, help="read the new prompt from a UTF-8 file")
    # (2026-10-21, edit reply) the owner's reply while Claude has not got it yet
    ersrc = ed.add_mutually_exclusive_group()
    ersrc.add_argument("--reply-text", default=None, help="the new text of the reply that was not sent yet")
    ersrc.add_argument("--reply-file", default=None, help="read that new reply text from a UTF-8 file")
    ersrc.add_argument("--drop-reply", action="store_true",
                       help="withdraw the reply that was not sent yet; the item goes back to how it was")
    # (2026-10-26, edit paused) a paused task that has started: the new prompt starts it over
    ed.add_argument("--restart", action="store_true",
                    help="with --text/--file on a paused task that has started: drop its session (kept in "
                         "prior_sessions) so it starts over with the new text when resumed")
    ed.add_argument("--name", default=None, help='the new label ("" = no label)')
    ed.add_argument("--profile", choices=sorted(PROFILES), default=None,
                    help="the permission profile for the next attempt")
    ed.add_argument("--model", type=_task_model, default=None,
                    help='the model for the next attempt ("" = the run\'s model)')
    # (2026-10-21, add panel) the same as --model: from the next attempt, "" = the run's
    ed.add_argument("--provider", type=_task_provider, default=None, metavar="NAME",
                    help='the model provider for the next attempt ("" = the run\'s provider)')
    ed.add_argument("--effort", type=_effort, default=None, metavar="LEVEL",
                    help=f'the effort for the next attempt: {", ".join(EFFORTS)} ("" = the run\'s)')
    # (2026-10-21, subagents) takes effect from the next attempt, like --profile and --model
    ed.add_argument("--max-agents", type=_agents_cap, default=None, metavar="N",
                    help=f"subagents this task may start from its next attempt: {AGENTS_TEXTS}")
    # (2026-10-26, modes) ultracode from the next attempt; autorun at once (also while it runs)
    ed.add_argument("--ultracode", type=_on_off, default=None, metavar="on|off",
                    help='pass --settings {"ultracode":true} and offer the Workflow tool from the next attempt '
                         "(on; needs the full profile) or nothing (off)")
    ed.add_argument("--autorun", type=_on_off, default=None, metavar="on|off",
                    help="the extension answers this task's option questions itself (on) or not (off); "
                         "the only change allowed while the task runs")
    _order_flags(ed)   # (2026-10-27, "Tasks at once" U3)
    ed.add_argument("--json", action="store_true", help='print {"id", "changed"}')

    r = sub.add_parser("run", parents=[common], help="run the queue")
    mode = r.add_mutually_exclusive_group()
    mode.add_argument("--until-empty", action="store_true", help="exit when the queue is empty (default)")
    mode.add_argument("--forever", action="store_true", help="keep polling the queue")
    r.add_argument("--start-at", type=_hhmm, help="wait until this local time (HH:MM) first")
    r.add_argument("--max-turns", type=_positive_int, default=DEFAULT_TASK_MAX_TURNS,
                   help=f"turn cap per task attempt (default {DEFAULT_TASK_MAX_TURNS}); hitting it "
                        "blocks the task")
    r.add_argument("--goal-max-turns", type=_positive_int, default=DEFAULT_GOAL_MAX_TURNS,
                   help=f"turn cap per goal round (default {DEFAULT_GOAL_MAX_TURNS})")
    r.add_argument("--model", default=None,
                   help=f"model for every attempt (default ${ENV_MODEL}, else the --provider "
                        f"default, {DEFAULT_MODEL} for claude; empty = do not pass)")
    r.add_argument("--effort", type=_effort, default=None,
                   help=f"effort for every attempt (default ${ENV_EFFORT} or {DEFAULT_EFFORT}; "
                        "empty = do not pass)")
    r.add_argument("--max-budget-usd", type=_budget, default=None,
                   help=f"dollar cap per attempt (default {DEFAULT_MAX_BUDGET_USD:g}; 0 = off); "
                        "hitting it blocks the item")
    # (2026-10-20, 0.4.7 providers, docs/SPEC_V047_PROVIDERS.md) which model provider pays for the
    # run, and the .env file its API key comes from
    r.add_argument("--provider", choices=providers.PROVIDER_NAMES, default=None,
                   help=f"model provider for every attempt (default ${providers.ENV_PROVIDER} or the "
                        f"providers.env value or {providers.DEFAULT_PROVIDER}); `providers` lists them")
    r.add_argument("--env-file", default=None, metavar="PATH",
                   help=f"the KEY=VALUE file with the API keys (default ${providers.ENV_FILE_VAR}, "
                        "else providers.env in the project data dir, then in the base data dir; "
                        "a repo .env is never read)")
    r.add_argument("--item-timeout", type=float, default=180.0, help="minutes per attempt (default 180)")
    r.add_argument("--max-limit-waits", type=_positive_int, default=10)
    r.add_argument("--poll-min", type=float, default=30.0,
                   help="minutes to wait when a limit gives no reset time (default 30)")
    r.add_argument("--dry-run", action="store_true", help="print the argv per item; execute nothing")
    r.add_argument("--claude-arg", action="append", default=[], metavar="ARG",
                   help="extra argument for claude (repeatable; permission bypass is refused)")
    # (2026-10-21, v049 dontask_hold) a queue on hold refuses `run` (exit 3) unless this is given
    r.add_argument("--ignore-hold", action="store_true",
                   help="start even though the queue is on hold (`hold status`); a hold put on later "
                        "still makes the runner exit after its current item")
    # (2026-10-26, limit_continue) what a usage limit does: auto = wait for the reset and resume (as
    # before); ask = park the item, hold the queue (reason "limit") and exit; `hold off` + `run` resume
    r.add_argument("--on-limit", choices=ON_LIMIT_CHOICES, default=ON_LIMIT_AUTO,
                   help="auto (default): wait out a usage limit and resume; ask: put the queue on hold "
                        "(reason limit, with the reset time) and exit; `hold off` then `run` resume it")
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 1.2) written to the lane's tasks_at_once.json at start;
    # a `run` without it deletes that file (no stale number from an old session)
    r.add_argument("--tasks-at-once", type=_tasks_at_once, default=None, metavar="N",
                   help=f"run up to N tasks of this queue at the same time (1-{TASKS_AT_ONCE_MAX}; default 1), "
                        "each in its own private copy of the project, merged back when it finishes")

    # (2026-10-27, "Tasks at once" U3) change the live number of a running (or the next) runner
    ta = sub.add_parser("tasks-at-once", parents=[common],
                        help="set how many tasks of this queue run at the same time (a live runner applies it "
                             "at its next pick; without a runner, the next `run` uses its own --tasks-at-once)")
    ta.add_argument("n", type=_tasks_at_once, metavar="N", help=f"1 to {TASKS_AT_ONCE_MAX}")
    ta.add_argument("--json", action="store_true",
                    help='print {"lane", "tasks_at_once", "runner_alive", "applies"}')

    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.6) the owner's answer to a merge (conflict) question
    me = sub.add_parser("merge", parents=[common],
                        help="act on a task's merge: let Claude fix a conflict, retry, take the task's version, "
                             "discard its changes, merge now or undo a merge")
    me.add_argument("id")
    mact = me.add_mutually_exclusive_group(required=True)
    for flag, hlp in (("let-claude", "write the project's newest files with conflict markers into the task's "
                                     "copy and resume the task to resolve them"),
                      ("retry", "try the merge again (a new plan)"),
                      ("take-task", "the task's version wins in the conflicted files (a backup is kept)"),
                      ("discard", "drop the task's changes (saved as a patch first) and cancel it"),
                      ("now", "merge a held, cancelled, failed or blocked task's copy now"),
                      ("undo", "put back the files a merge replaced (only while they are unchanged since)")):
        mact.add_argument(f"--{flag}", dest="merge_action", action="store_const", const=flag, help=hlp)
    me.add_argument("--note", default=None, metavar="TEXT", help="with --let-claude: added to Claude's instructions")
    me.add_argument("--gen", type=int, default=None, metavar="G",
                    help="the question's generation (refused when the question changed meanwhile)")
    # (2026-10-29, integrate round 9) an automatic let-claude answer is stored as one, like `reply --by`
    me.add_argument("--by", choices=REPLY_AUTHORS, default=None,
                    help="with --let-claude: record its reply as an automatic answer (Autorun or Auto-pick best)")
    me.add_argument("--json", action="store_true",
                    help='print {"id", "action", "ok", "state", "code", "merge"}')

    # (2026-10-27, "Tasks at once" U3) the project's private copies and leftovers (read-only)
    wt = sub.add_parser("worktrees", parents=[common], help="list the project's private task copies and leftovers")
    wt.add_argument("--json", action="store_true",
                    help='print {"project", "shadow", "copies", "leftovers"}')

    # (2026-10-21, v049 dontask_hold, docs/SPEC_V049_DONTASK_HOLD.md 2) the queue's hold flag
    hd = sub.add_parser("hold", parents=[common],
                        help="hold a queue: no runner starts for it (a live one exits after its current item)")
    # (2026-10-27, integrate: limit_continue review LOW-2) limit-off: release only a usage-limit hold; the
    # owner's own hold it replaced ("Start anyway") comes back, any other hold stays (Store.release_limit_hold)
    hd.add_argument("action", choices=("on", "off", "status", "limit-off"))
    hd.add_argument("--json", action="store_true",
                    help='print {"hold", "since", "runner_alive", "runner_pid", "data_dir", "until"}')
    # (2026-10-26, usage forecast) "queue for after the reset": a runner waits until then instead of exiting
    hd.add_argument("--until", type=_hold_until_arg, default=None, metavar="ISO",
                    help="with `on`: a timed hold that ends at this time (ISO 8601, within 8 days); a "
                         "runner waits for it instead of exiting")

    # (2026-10-20, 0.4.4 logfix A) the shell-dev allow-list of the project (in its data dir, never the repo)
    sa = sub.add_parser("shell-allow", parents=[common],
                        help="the commands the shell-dev profile may run in this project")
    sa.add_argument("action", choices=("list", "add", "remove"))
    sa.add_argument("command", nargs="?", default=None, metavar="PREFIX",
                    help='a command prefix, e.g. "git merge" or "pytest" (add/remove)')
    sa.add_argument("--json", action="store_true", help='print {"file", "commands"}')

    # (2026-10-20, 0.4.7 providers) what the .env file offers; key VALUES are never printed
    pv = sub.add_parser("providers", parents=[common],
                        help="the model providers, their models and which .env key each one needs")
    pv.add_argument("--env-file", default=None, metavar="PATH", help="read this KEY=VALUE file instead")
    pv.add_argument("--json", action="store_true",
                    help='print {"env_file", "searched", "selected", "providers"}')

    # (2026-10-21, docs/SPEC_V048_REPORT.md) a GitHub issue draft with the secrets taken out; never sent
    rpt = sub.add_parser("report", parents=[common],
                         help="write a redacted GitHub issue draft and print its prefilled new-issue link")
    rpt.add_argument("--title", default=None, help="the issue title (default: from the newest ERROR line)")
    rdesc = rpt.add_mutually_exclusive_group()
    rdesc.add_argument("--text", default=None, help="what happened, in your words")
    rdesc.add_argument("--file", default=None, help="read what happened from a UTF-8 file")
    rpt.add_argument("--item", default=None, metavar="ID",
                     help="add this item's status and error (never its prompt or output)")
    rpt.add_argument("--lines", type=_int_range(report.MAX_LOG_LINES), default=report.DEFAULT_LOG_LINES,
                     help=f"errors.log lines to include (default {report.DEFAULT_LOG_LINES}; 0 = none)")
    rpt.add_argument("--repo", default=None, metavar="OWNER/NAME",
                     help=f"the GitHub repo (default ${report.ENV_REPO} or {report.DEFAULT_REPO})")
    rpt.add_argument("--env-file", default=None, metavar="PATH",
                     help="the key file whose values are taken out (default: as `run`)")
    rpt.add_argument("--open", action="store_true",
                     help="open the link in the browser; nothing is posted until you press Submit there")
    rpt.add_argument("--json", action="store_true",
                     help='print {"file", "url", "title", "repo", "redactions", "log_lines", "url_log_lines"}')

    st = sub.add_parser("status", parents=[common], help="counts, runner liveness, next wait")
    st.add_argument("--json", action="store_true",
                    help="print {counts, runner_alive, runner_pid, next_wait_until, data_dir}")
    sub.add_parser("stop", parents=[common], help="ask a live runner to stop gracefully")

    pj = sub.add_parser("projects", parents=[common], help="list the per-project queues")
    pj.add_argument("--json", action="store_true", help="print a JSON array of projects")
    pj.add_argument("--items", action="store_true",
                    help="include every project's items (one call for the all-projects view)")
    wh = sub.add_parser("where", parents=[common], help="print the resolved data dir")
    wh.add_argument("--json", action="store_true", help='print {"data_dir", "project_root", "lane"}')
    ln = sub.add_parser("lanes", parents=[common], help="list the named queues of the project")
    ln.add_argument("--json", action="store_true", help="print a JSON array of lanes")
    ln.add_argument("--items", action="store_true", help="include every lane's items (one call per poll)")
    lr = sub.add_parser("lane-remove", parents=[common],
                        help="move an empty named queue to lanes/.removed (a backup, not a delete)")
    lr.add_argument("name")
    lr.add_argument("--json", action="store_true", help='print {"removed", "backup"}')
    mg = sub.add_parser("migrate-global", parents=[common],
                        help="move the base (global) queue's items into their projects' queues")
    mg.add_argument("--json", action="store_true", help="print the result as JSON")
    mg.add_argument("--dry-run", action="store_true", help="show what would move; change nothing")
    mg.add_argument("--for", dest="for_root", metavar="ROOT", default=None,
                    help="also report how many items would go to this project folder")
    return p


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=True)


def _print_line(s: str) -> None:
    """(2026-10-26, improve_core) The default `out`: one line to stdout. A stdout that cannot encode a
    character (a cp1252 pipe under `python -I`, which ignores PYTHONIOENCODING) gets it as a backslash
    escape instead of a UnicodeEncodeError that kills `show`/`list`. A utf-8 stdout (the extension's)
    encodes everything, so it never takes the fallback."""
    try:
        print(s, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(s.encode(enc, "backslashreplace").decode(enc, "replace"), flush=True)


def main(argv: list[str] | None = None, *, claude_cmd: list[str] | None = None,
         clock: Clock | None = None, out=None) -> int:
    """Entry point. `claude_cmd` and `clock` are injection points for tests."""
    out = out or _print_line
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: 2 on usage errors, 0 for --help/--version
        return int(exc.code or 0)
    explicit = getattr(args, "data_dir", None)
    project = getattr(args, "project", None)
    usage = None
    if project is not None and args.cmd == "migrate-global":
        usage = "migrate-global works on the shared (base) queue; drop --project"
    elif project is not None and not str(project).strip():
        usage = "--project needs a folder"
    lane_arg = getattr(args, "lane", None)
    if usage is None and lane_arg is not None:
        if args.cmd in LANELESS_COMMANDS:
            usage = f"{args.cmd} works on every queue of a project; drop --lane"
        elif not valid_lane_name(lane_arg):
            usage = (f"invalid --lane {lane_arg!r}: 1 to 32 letters, digits, '_' or '-', "
                     "starting with a letter or digit")
    if usage:
        sys.stderr.write(f"autopilot: {usage}\n")
        return EXIT_USAGE
    base_dir = default_data_dir(explicit)  # with --project this is the BASE dir (amendment H1)
    args.project_root = project_root(project) if project else None
    if args.cmd == "add":
        # (2026-10-13, tester fix 4) validate before the project dir is created: a rejected
        # `add --project` must leave nothing behind.
        try:
            args.prepared = _prepare_add(args)
            # (2026-10-27, SEC-L24) before any project dir is made: the base dir holds every queue.
            # (S8 fix r1) a subtask without --cwd runs in its main task's folder, not this provisional
            # one (the process cwd): _cmd_add checks that final folder
            if args.cwd or not getattr(args, "parent", None):
                _refuse_nested(args.prepared[1], base_dir)
        except UsageError as exc:
            sys.stderr.write(f"autopilot: {exc}\n")
            return EXIT_USAGE
    # (2026-10-21, v049 dontask_hold) `hold on` may hold a queue before anything was added to it
    create = args.cmd in CREATING_COMMANDS or (args.cmd == "shell-allow" and args.action == "add") \
        or (args.cmd == "hold" and args.action == "on")
    try:
        project_dir = project_data_dir(base_dir, project, (clock or Clock()).now(),
                                       create=create) if project else base_dir
        # (2026-10-15, SPEC_V03 2.1) the lane picks the store dir inside the project dir; `add` and
        # `run` create a missing lane, read-only commands never do (the M11 rule, per lane).
        args.lane, data_dir = lane_data_dir(project_dir, lane_arg)
        if create and args.lane != DEFAULT_LANE:
            data_dir.mkdir(parents=True, exist_ok=True)
    except (OSError, StoreError) as exc:
        sys.stderr.write(f"autopilot: project data dir: {exc}\n")
        return EXIT_FAIL
    args.base_dir = base_dir
    args.project_dir = project_dir
    verbose = getattr(args, "verbose", False)
    # (2026-10-13, amendment M11) A project dir that does not exist yet gets no logs/ either:
    # read-only commands log to the console only until `add` or `run` creates the dir.
    # (2026-10-15) The same holds for a lane dir; the legacy base dir keeps logging as before.
    legacy_base = project is None and args.lane == DEFAULT_LANE
    log_dir = data_dir if (legacy_base or data_dir.is_dir()) else None
    try:
        run_id = setup_logging(log_dir, verbose)
        guard_console()   # (2026-10-27, SEC-L17)
    except Exception as exc:  # logging must not block the command itself
        sys.stderr.write(f"logging setup failed: {exc}\n")
        run_id = "-"
    quiet = args.cmd in QUIET_COMMANDS and not verbose
    if not quiet:
        log.debug("start: version=%s cmd=%s pid=%s data_dir=%s run_id=%s", __version__, args.cmd,
                  os.getpid(), data_dir, run_id)
    store = Store(data_dir)
    try:
        return _dispatch(args, store, claude_cmd, clock or Clock(), out)
    except UsageError as exc:
        log.error("usage: %s", exc)
        return EXIT_USAGE
    except _Abort:
        return EXIT_FAIL  # already logged with its reason
    except (StoreError, ClaudePathError, ForbiddenFlagError, KeyError, OSError) as exc:
        log.error("%s", exc)
        return EXIT_FAIL
    finally:
        if not quiet:
            log.debug("stop: cmd=%s", args.cmd)
        shutdown_logging()


def _dispatch(args, store: Store, claude_cmd, clock: Clock, out) -> int:
    cmd = args.cmd
    if cmd == "add":
        return _cmd_add(args, store, clock, out)
    if cmd == "list":
        return _cmd_list(args, store, out)
    if cmd == "show":
        item = with_defaults(store.find(store.load(), args.id))
        if args.json:
            out(_json(item))
            return EXIT_OK
        # (2026-10-27, S8 fix r1, SEC-L17) JSON escapes C0 but not DEL/C1 (\x9b CSI): escaped, not dropped
        out(_escape_controls(json.dumps(_show_text_fields(item), indent=2, ensure_ascii=False)))
        out(usage_text(item.get("billing"), item.get("total_cost_usd")))
        for line in denial_lines(item):  # (2026-10-21, v049 dontask_hold)
            out(line)
        note = worktree.not_merged_note(item)   # (2026-10-29, delta M1)
        if note:
            out(note)
        out(f"run logs: {store.run_dir(item['id'])}")
        return EXIT_OK
    if cmd == "remove":
        return _cmd_remove(args, store, out)
    if cmd == "cancel":
        return _cmd_cancel(args, store, out)
    if cmd == "retry":
        return _cmd_retry(args, store, out, clock)
    if cmd == "reply":
        return _cmd_reply(args, store, clock, out)
    if cmd == "edit":
        return _cmd_edit(args, store, out, clock)
    if cmd == "first":
        return _cmd_first(args, store, out)
    if cmd == "move":
        return _cmd_move(args, store, out)
    if cmd == "pause":
        return _cmd_pause(args, store, clock, out)
    if cmd == "resume":
        return _cmd_resume(args, store, clock, out)
    if cmd == "projects":
        return _cmd_projects(args, out)
    if cmd == "where":
        return _cmd_where(args, store, out)
    if cmd == "lanes":
        return _cmd_lanes(args, store, out)
    if cmd == "lane-remove":
        return _cmd_lane_remove(args, store, clock, out)
    if cmd == "migrate-global":
        return _cmd_migrate(args, store, clock, out)
    if cmd == "status":
        return _cmd_status(args, store, out)
    if cmd == "shell-allow":
        return _cmd_shell_allow(args, out)
    if cmd == "providers":
        return _cmd_providers(args, out)
    if cmd == "report":
        return _cmd_report(args, store, clock, out)
    if cmd == "stop":
        return _cmd_stop(store, clock, out)
    if cmd == "hold":
        return _cmd_hold(args, store, clock, out)
    if cmd == "run":
        return _cmd_run(args, store, claude_cmd, clock, out)
    if cmd == "tasks-at-once":   # (2026-10-27, "Tasks at once" U3)
        return _cmd_tasks_at_once(args, store, clock, out)
    if cmd == "merge":
        return _cmd_merge(args, store, clock, out)
    if cmd == "worktrees":
        return _cmd_worktrees(args, store, out)
    raise UsageError(f"unknown command {cmd}")


def _show_text_fields(item: dict) -> dict:
    """(2026-10-20) `show` text output: the cost fields only when the run was billed to the API (a
    subscription run's total_cost_usd is an estimate nobody pays). `show --json` keeps them."""
    if item.get("billing") == BILLING_API:
        return item
    return {k: v for k, v in item.items() if k not in ("cost_usd", "total_cost_usd")}


def _prepare_add(args) -> tuple[str, Path, list[str]]:
    """(prompt, cwd, attachments) for `add`, or UsageError. Touches no data dir."""
    if args.file:
        try:
            prompt = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --file {args.file}: {exc}") from exc
    else:
        prompt = args.prompt
    if not prompt or not prompt.strip():
        raise UsageError("the prompt is empty")
    if not args.goal and (args.max_rounds is not None or args.max_hours is not None):
        raise UsageError("--max-rounds and --max-hours are only valid with --goal")
    if not args.goal and (args.fresh_at_ctx is not None or args.fresh_every is not None):
        raise UsageError("--fresh-at-ctx and --fresh-every are only valid with --goal")
    cwd = Path(args.cwd or args.project_root or ".").resolve()
    if not cwd.is_dir():
        raise UsageError(f"--cwd {cwd} is not an existing directory")
    attachments: list[str] = []
    for raw in args.attach:
        path = Path(raw).resolve() if raw else None
        if path is None or not path.exists():
            raise UsageError(f"--attach {raw!r} does not exist (a file or folder is required)")
        if str(path) not in attachments:
            attachments.append(str(path))
    if len(attachments) > MAX_ATTACHMENTS:
        raise UsageError(f"at most {MAX_ATTACHMENTS} attachments per item (got {len(attachments)}); "
                         "attach their folder instead")
    return prompt, cwd, attachments


def _cmd_add(args, store: Store, clock: Clock, out) -> int:
    prompt, cwd, attachments = getattr(args, "prepared", None) or _prepare_add(args)
    kind = KIND_GOAL if args.goal else KIND_TASK
    profile = args.profile or DEFAULT_PROFILE
    model = getattr(args, "model", None) or None
    # (2026-10-21, add panel) the task's own provider and effort; None = the run's
    provider = getattr(args, "provider", None) or None
    effort = getattr(args, "effort", None) or None
    # (2026-10-21, subagents) None = the flag was not given = the profile decides
    raw_agents = getattr(args, "max_agents", None)
    max_agents = None if raw_agents is None else parse_agents_text(raw_agents)
    # (2026-10-26, modes) None = not given: off, or the main task's for a subtask
    ultracode = getattr(args, "ultracode", None)
    autorun = getattr(args, "autorun", None)
    with store.transaction() as items:
        parent = None
        if getattr(args, "parent", None):
            # (2026-10-21, subtasks) one level only: a subtask's id means its main task
            parent = store.find(items, args.parent)
            if parent.get("parent"):
                main = next((i for i in items if i["id"] == parent["parent"]), None)
                if main is None:
                    # (2026-10-21, tester round 2 L-5) its main task was removed: making the orphan a
                    # parent would chain two levels deep
                    raise UsageError(f"item {parent['id']} is a subtask whose main task "
                                     f"{parent['parent']} was removed; it cannot be a parent")
                parent = main
            # (2026-10-21, security review M2) `full` is opt-in per item: a subtask never inherits it;
            # it gets the safe default unless `--profile full` is given for it explicitly
            inherited = parent.get("profile") or DEFAULT_PROFILE
            if inherited == FULL and not args.profile:
                log.info("item %s is %s; its new subtask gets %s (full is never inherited)",
                         parent["id"], FULL, DEFAULT_PROFILE)
                inherited = DEFAULT_PROFILE
            profile = args.profile or inherited
            model = model or parent.get("model") or None
            # (2026-10-21, add panel) provider and effort are inherited the way the model is
            provider = provider or parent.get("provider") or None
            if not effort and parent.get("effort"):
                if provider and not providers.provider(provider).supports_effort:
                    log.info("item %s: effort %s not inherited: provider %s takes none", parent["id"],
                             parent["effort"], provider)
                else:
                    effort = parent["effort"]
            # (2026-10-21, subagents) a subtask inherits the main task's limit unless it was given one
            if raw_agents is None:
                max_agents = parent.get("max_agents")
            if ultracode is None:
                ultracode = parent.get("ultracode") is True
            if autorun is None:
                autorun = parent.get("autorun") is True
            if not args.cwd and Path(parent.get("cwd") or "").is_dir():
                cwd = Path(parent["cwd"])
        # (2026-10-27, modes fix HIGH) ultracode only where the Workflow tool can be offered: an explicit
        # `on` is refused, an inherited one is dropped (a subtask never inherits `full`)
        why = ultracode_refusal(profile, max_agents) if ultracode else None
        if why and getattr(args, "ultracode", None) is True:
            raise UsageError(why)
        if why:
            log.info("item %s: ultracode not inherited by its subtask: %s", parent["id"], why)
            ultracode = False
        _check_run_choice(None, provider, effort, profile)
        after = _add_after(args, store, items)   # (2026-10-27, "Tasks at once" U3) before the item exists
        # (2026-10-27, SEC-L24) the final folder (a subtask's may be its main task's)
        _refuse_nested(cwd, getattr(args, "base_dir", None) or store.data_dir, store.data_dir)
        if not provider:   # (2026-10-22, v049 tester LOW-3) the effective provider: the runner's
            _check_env_provider(args, profile)
        item = new_item(prompt, str(cwd), args.name, profile, clock.now(),
                        {i["id"] for i in items}, seq=next_seq(items), kind=kind,
                        attachments=attachments, max_rounds=args.max_rounds, max_hours=args.max_hours,
                        fresh_at_ctx=args.fresh_at_ctx, fresh_every=args.fresh_every, parent=parent,
                        model=model, max_agents=max_agents, provider=provider, effort=effort)
        item["ultracode"] = ultracode is True   # (2026-10-26, modes)
        item["autorun"] = autorun is True
        if after:   # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 5.1)
            item.update(after=after, after_source="owner")
        if getattr(args, "in_place", None):
            item["in_place"] = True
        items.append(item)
    log.info("item %s: added (%s, profile=%s, provider=%s, model=%s, effort=%s, subagents=%s, cwd=%s, %d chars, "
             "%d attachment(s)%s%s)",
             item["id"], kind, profile, provider or "(run's)", model or "(run's)", effort or "(run's)",
             agents_label(item["max_agents"]), cwd,
             len(prompt), len(attachments),
             f", max_rounds={item['max_rounds']}, max_hours={item['max_hours']}" if args.goal else "",
             f", subtask of {item['parent']}" if item["parent"] else "")
    for flag in ("ultracode", "autorun"):   # (2026-10-26, modes) only when on
        if item[flag]:
            log.info("item %s: %s on", item["id"], flag)
    if item.get("after") or item.get("in_place"):   # (2026-10-27, "Tasks at once" U3)
        log.info("item %s: after=%s in_place=%s", item["id"], item.get("after"), item.get("in_place") is True)
    warnings = claude_md_warnings(cwd) + project_settings_warnings(cwd)
    for text in warnings:
        # one line on stderr (the console log handler) and in app.log
        log.warning("item %s: warning: %s", item["id"], text)
    if args.json:
        # (2026-10-17, 0.4.1 R8) `warnings` only when there are any: {"id"} stays the plain answer
        out(_json({"id": item["id"], "warnings": warnings} if warnings else {"id": item["id"]}))
    else:
        out(item["id"])
    return EXIT_OK


def claude_md_warnings(cwd: Path) -> list[str]:
    """(2026-10-17, 0.4.1 R8) One line per CLAUDE.md over 40 KB that a run in `cwd` loads, naming its size.
    (2026-10-27, U3: SEC-H1 follow-up) Runs load only <cwd>\\CLAUDE.md (runner.project_instructions re-supplies
    it; `--setting-sources user` keeps claude.exe from reading CLAUDE.local.md, .claude/CLAUDE.md and the parent
    folders' files), and not one that resolves outside the folder: so only that file is checked."""
    found: list[str] = []
    cwd = Path(cwd)
    for name in CLAUDE_MD_NAMES:
        path = cwd / name
        try:
            if not path.is_file() or not path.resolve().is_relative_to(cwd.resolve()):
                continue
            size = path.stat().st_size
        except OSError:
            continue
        if size > CLAUDE_MD_WARN_BYTES:
            found.append(f"{path} is {size // 1024} KB (over {CLAUDE_MD_WARN_BYTES // 1024} KB); "
                         "Claude re-reads it at the start of every run (each attempt and goal "
                         "round), so it adds tokens to each one, and only its first "
                         f"{CLAUDE_MD_WARN_BYTES // 1024} KB are passed")
    return found


def project_settings_warnings(cwd: Path) -> list[str]:
    """(2026-10-27, SEC-H1 add-on) One line when the task folder holds .claude/settings*.json: runs load
    the user settings only (--setting-sources user), so its hooks and env never apply."""
    try:
        names = sorted(p.name for p in (Path(cwd) / ".claude").glob("settings*.json") if p.is_file())
    except OSError:
        return []
    if not names:
        return []
    return [PROJECT_SETTINGS_WARNING.format(cwd, " and .claude/".join(names))]


def _norm_dir(path) -> str:
    """(2026-10-27, SEC-L24) `path` resolved, case-folded on Windows, with one trailing separator."""
    try:
        full = Path(path).resolve()
    except (OSError, RuntimeError):
        full = Path(os.path.abspath(path))
    text = str(full)
    # (2026-10-27, S8 fix r2) resolve keeps a \\?\ (or \\.\) prefix: `\\?\C:\x` is `C:\x`, `\\?\UNC\srv\share`
    # is `\\srv\share`
    if text[:8].upper() in ("\\\\?\\UNC\\", "\\\\.\\UNC\\"):
        text = "\\\\" + text[8:]
    elif text[:4] in ("\\\\?\\", "\\\\.\\"):
        text = text[4:]
    return os.path.normcase(text).rstrip("\\/") + os.sep


def _dir_ids(path) -> tuple[tuple | None, set]:
    """(2026-10-27, S8 fix r2, SEC-L24) (the volume + file id of `path` or None, the ids of it and every folder
    above it that exists). One folder has one id whatever its spelling: C:\\x, \\\\?\\C:\\x,
    an admin share of this PC (\\\\<pc>\\C$\\x) or an 8.3 name all give the same (st_dev, st_ino)."""
    try:
        full = str(Path(path).resolve())
    except (OSError, RuntimeError):
        full = os.path.abspath(path)
    own, chain = None, set()
    first = True
    while True:
        try:
            st = os.stat(full)
            ident = (st.st_dev, st.st_ino) if st.st_ino else None
        except (OSError, ValueError):
            ident = None
        if ident is not None:
            chain.add(ident)
            if first:
                own = ident
        first = False
        up = os.path.dirname(full)
        if up == full:
            return own, chain
        full = up


def nested_dirs(a, b) -> bool:
    """(2026-10-27, SEC-L24) `a` and `b` are one folder, or one holds the other: by the normalised path, and
    (S8 fix r2) by the folders' file ids, so another spelling of the same folder (\\\\?\\C:\\..., an admin share
    \\\\<pc>\\C$\\...) is caught too."""
    x, y = _norm_dir(a), _norm_dir(b)
    if x.startswith(y) or y.startswith(x):
        return True
    (own_a, chain_a), (own_b, chain_b) = _dir_ids(a), _dir_ids(b)
    return (own_a is not None and own_a in chain_b) or (own_b is not None and own_b in chain_a)


def _refuse_nested(cwd, *data_dirs) -> None:
    """(2026-10-27, SEC-L24) UsageError when the task folder `cwd` and a data dir nest."""
    for d in data_dirs:
        if d is not None and nested_dirs(cwd, d):
            raise UsageError(NESTED_DATA_DIR.format(cwd, d))


def _refuse_nested_item(item: dict, args, store: Store) -> None:
    """(2026-10-27, S8 fix r1, SEC-L24) `retry`/`reply`: UsageError (nothing saved) when a queued item's folder
    and a data dir nest. `run` checks only at its start, so a finished item from before the fix put back to
    pending under a live runner would run unchecked."""
    cwd = item.get("cwd")
    if isinstance(cwd, str) and cwd:
        _refuse_nested(cwd, getattr(args, "base_dir", None), store.data_dir)


def _run_folder_checks(args, store: Store) -> list[str]:
    """(2026-10-27, SEC-L24 + SEC-H1 add-on) `run`: the refusals for the queued items whose folder and the
    data dir nest (an item added before `add` refused it), and one warning per folder that holds
    .claude/settings*.json."""
    refusals: list[str] = []
    seen: set[str] = set()
    for it in store.load():
        cwd = it.get("cwd")
        if it.get("status") not in NESTED_CHECK_STATUSES or not isinstance(cwd, str) or not cwd:
            continue
        for d in (getattr(args, "base_dir", None) or store.data_dir, store.data_dir):
            if nested_dirs(cwd, d):
                refusals.append(f"item {it.get('id')}: " + NESTED_DATA_DIR.format(cwd, d))
                break
        key = _norm_dir(cwd)
        if key not in seen:
            seen.add(key)
            for text in project_settings_warnings(Path(cwd)):
                log.warning("run: %s", text)
    return refusals


class _ConsoleText(logging.Formatter):
    """(2026-10-27, SEC-L17) The console handler's formatter, with every C0/C1 control character but \\t and
    \\n taken out of the formatted line: a model-chosen text (a denied command, a path) logged at INFO must
    not move the cursor, recolour or rewrite what the owner sees. A Formatter, not a Filter: a Filter can
    only change the record itself, which the file handlers (app.log, errors.log) format too."""

    def __init__(self, inner: logging.Formatter | None) -> None:
        super().__init__()
        self.inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return strip_controls(self.inner.format(record))


def guard_console() -> None:
    """(2026-10-27, SEC-L17) Put _ConsoleText on the console handler(s) setup_logging made (never a file one)."""
    for h in logging.getLogger(ROOT_NAME).handlers:
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) \
                and not isinstance(h.formatter, _ConsoleText):
            h.setFormatter(_ConsoleText(h.formatter))


def _escape_controls(text: str) -> str:
    """(2026-10-27, S8 fix r4, SEC-L17) every CONTROLS_RE character of `text` as a visible \\uXXXX escape (never
    dropped: `show` and `run --dry-run` print what is stored or passed, a prompt included)."""
    return CONTROLS_RE.sub(lambda m: f"\\u{ord(m.group()):04x}", str(text))


def _label(item: dict) -> str:
    text = item.get("name") or " ".join((item.get("prompt") or "").split())
    # (2026-10-27, S8 fix r3, SEC-L17) a subtask's prompt can be model-written: no control reaches `list`
    return strip_controls(text)[:60]


def _marks(item: dict) -> str:
    """Human-output marks: `[goal r3/50]`, `[plan]`, `[2 attachments]`."""
    marks = []
    if is_goal(item):
        cap = item.get("max_rounds")
        marks.append(f"[goal r{int(item.get('rounds') or 0)}/{rounds_cap_text(cap) if cap is not None else '-'}]")
    if item.get("profile") == "plan":
        marks.append("[plan]")
    n = len(item.get("attachments") or [])
    if n:
        marks.append(f"[{n} attachment{'s' if n != 1 else ''}]")
    if item.get("parent"):  # (2026-10-21, subtasks)
        marks.append(f"[subtask of {item['parent']}]")
    return (" ".join(marks) + " ") if marks else ""


def denial_lines(item: dict) -> list[str]:
    """(2026-10-21, v049 dontask_hold) A blocked item's denial block in plain text: what was denied
    and the owner's fix (`show`, and `list` under the item's row)."""
    block = item.get("denial_block")
    if item.get("status") != "blocked" or not isinstance(block, dict):
        return []
    # (2026-10-27, SEC-L17) no control character reaches the console (a block stored before the fix too)
    return [strip_controls(f"denied: {block_text(block).removeprefix('permission denied for ')}"),
            strip_controls(f"fix: {block.get('hint') or hint_text(block, item.get('id', '<id>'))}")]


HOLD_LIST_LINE = "queue on hold since {0}: no runner starts for it (`autopilot hold off` releases it)"


def _in_run_order(items: list[dict]) -> list[dict]:
    """(2026-10-26, owner "if add new task should go on bottom of que. and do from olderst que to
    newest one") `list` and `lanes`/`projects --items` (the Queue view) give the items in RUN order
    (store.run_order_key: seq, each subtask right after its main task), not file order: a replied or
    `first`-moved item keeps its file spot while its seq moves, so the view showed it misplaced."""
    by_id = {i["id"]: i for i in items}
    return sorted(items, key=lambda i: run_order_key(i, by_id))


def _cmd_list(args, store: Store, out) -> int:
    every = _in_run_order(store.load())
    items = every
    if not args.all:
        items = [i for i in items if i.get("status") not in ("done", "cancelled")]
    if args.json:
        # (2026-10-27, "Tasks at once" U3) + phase and waits on a parallel lane
        out(_json(_json_items(store, items, every)))
        return EXIT_OK
    held = store.read_hold()  # (2026-10-21, v049 dontask_hold)
    if held is not None:
        out(HOLD_LIST_LINE.format(held.get("since") or "?"))
    out(f"{'ID':<10} {'STATUS':<10} {'ATT':>3}  NAME/PROMPT")
    for it in items:
        out(f"{it['id']:<10} {it.get('status', '?'):<10} {it.get('attempts', 0):>3}  "
            f"{_marks(it)}{_label(it)}")
        for line in denial_lines(it):
            out(f"{'':<27}{line}")
    return EXIT_OK


def _remove_targets(store: Store, ids: list[str]) -> list[dict]:
    """The items `remove` would take (with subtasks), or _Abort for a running one (checked again inside the
    transaction)."""
    items = store.load()
    out: list[dict] = []
    for one in ids:
        item = store.find(items, one)
        for it in (item, *subtasks_of(items, item["id"])):
            if it.get("status") == "running":
                log.error("item %s is running; cancel it or stop the runner first", it["id"])
                raise _Abort()
            if all(o["id"] != it["id"] for o in out):
                out.append(it)
    return out


def _cmd_remove(args, store: Store, out) -> int:
    ids = args.id if isinstance(args.id, list) else [args.id]
    done: list[tuple[dict, list[dict]]] = []
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.10) a copy's unmerged work is saved as a patch FIRST (outside
    # the queue mutex: lock order ProjectLock -> queue); a copy whose work could not be saved is kept on disk
    shadow, copies = _shadow_copy_items(store)
    saved: dict[str, tuple[bool, str | None]] = {}
    if copies:
        for it in _remove_targets(store, ids):
            if _has_copy(it):
                saved[it["id"]] = _save_copy_work(shadow, it, "removed")
    with store.transaction() as items:
        # (2026-10-21, tester round 2 L-5) every id is checked before anything is removed, and one
        # failure (unknown id, a running item or subtask) aborts the whole transaction: all or nothing
        seen: set[str] = set()
        for one in ids:
            item = store.find(items, one)
            if item["id"] in seen:
                continue
            if item.get("status") == "running":
                log.error("item %s is running; cancel it or stop the runner first", item["id"])
                raise _Abort()
            # (2026-10-21, subtasks) removing a main task removes its subtasks too
            subs = [s for s in subtasks_of(items, item["id"]) if s["id"] not in seen]
            for sub in subs:
                if sub.get("status") == "running":
                    log.error("subtask %s of item %s is running; cancel it or stop the runner first",
                              sub["id"], item["id"])
                    raise _Abort()
            seen.update([item["id"], *(s["id"] for s in subs)])
            done.append((item, subs))
        for item, subs in done:
            for it in (item, *subs):
                # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 5.2) its dependents wait for its own `after`
                order.inherit_after(items, it["id"])
                items.remove(it)
    for item, subs in done:
        log.info("item %s: removed (was %s)%s", item["id"], item.get("status"),
                 f", with {len(subs)} subtask(s): {', '.join(s['id'] for s in subs)}" if subs else "")
        out(f"removed {item['id']}" + (f" and {len(subs)} subtask(s)" if subs else ""))
        for it in (item, *subs):   # (2026-10-27, "Tasks at once" U3) then its copy goes
            if shadow is None or not _has_copy(it):
                continue
            ok, patch = saved.get(it["id"], (False, None))
            if patch:
                out(f"  changes of {it['id']} saved to {patch}")
            if not ok:
                out(f"  copy of {it['id']} kept (its changes could not be saved): {shadow.worktree_path(it)}")
                continue
            try:
                gone = shadow.remove(store, it["id"], item=it)
            except worktree.WorktreeError as exc:
                log.warning("item %s: its copy was not deleted: %s", it["id"], exc)
                gone = False
            if not gone:
                out(f"  copy of {it['id']} could not be deleted (a leftover in `worktrees`): "
                    f"{shadow.worktree_path(it)}")
    return EXIT_OK


class _Abort(StoreError):
    """Abort a transaction without saving; already logged."""


def _cmd_cancel(args, store: Store, out) -> int:
    with store.transaction() as items:
        item = store.find(items, args.id)
        if item.get("status") in ("done", "cancelled"):
            log.error("item %s is already %s", item["id"], item["status"])
            raise _Abort()
        if item.get("status") == "running":
            log.warning("item %s is running; the runner stops its attempt within ~1 s", item["id"])
        # (2026-10-21, subtasks) cancelling a main task cancels its unfinished subtasks too
        subs = [s for s in subtasks_of(items, item["id"]) if s.get("status") not in ("done", "cancelled")]
        for it in (item, *subs):
            if it is not item and it.get("status") == "running":
                log.warning("subtask %s is running; the runner stops its attempt within ~1 s", it["id"])
            log.info("item %s: %s -> cancelled (user cancel%s)", it["id"], it.get("status"),
                     f" of main task {item['id']}" if it is not item else "")
            it["status"] = "cancelled"
            it["wait_until"] = None
            it["paused_from"] = None
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.9) a merge not yet writing is held (the merger sees
    # cancelled)
    held = [it["id"] for it in (item, *subs) if (it.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE]
    out(f"cancelled {item['id']}" + (f" and {len(subs)} subtask(s)" if subs else "")
        + (f"; {CANCEL_MERGE_HELD}" if held else ""))
    if held:
        log.info("item(s) %s: cancelled while merging; %s", ", ".join(held), CANCEL_MERGE_HELD)
    return EXIT_OK


def _merge_question(item: dict) -> dict | None:
    """(2026-10-27, "Tasks at once" U3) The blocked item's merge question, else None."""
    q = item.get("blocked_question")
    return q if item.get("status") == "blocked" and isinstance(q, dict) and q.get("kind") == "merge" else None


def _cmd_retry(args, store: Store, out, clock: Clock | None = None) -> int:
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.8) `retry` of an item blocked on a merge = `merge --retry`
    first = store.find(store.load(), args.id)
    q = _merge_question(first)
    if q is not None:
        if args.profile or args.max_rounds is not None or args.max_hours is not None:
            log.info("item %s: a merge retry runs no attempt; --profile/--max-rounds/--max-hours ignored", first["id"])
        res = _merge_run(args, store, clock or Clock(), first, "retry", q.get("gen"))
        if res["ok"]:
            out(f"merge retry {first['id']}: {res['code']}")
        return EXIT_OK if res["ok"] else EXIT_FAIL
    with store.transaction() as items:
        item = store.find(items, args.id)
        if item.get("status") == "running":
            log.error("item %s is running; nothing to retry", item["id"])
            raise _Abort()
        # (2026-10-13, SPEC_GOALS section 2) A goal keeps rounds and goal_started_at: retry continues it.
        if (args.max_rounds is not None or args.max_hours is not None) and not is_goal(item):
            log.error("item %s is a task; --max-rounds/--max-hours are for goals", item["id"])
            raise _Abort()
        if args.profile:
            # (2026-10-21, v049 merge) "Retry with <profile>" (the denial card, the profile picker) obeys
            # the add/edit rule: a task on its own key provider never gets a shell profile or full
            _check_run_choice(item["id"], item.get("provider"), None, args.profile)
            if not item.get("provider"):   # (2026-10-22, re-test R4) the runner's provider, as at add
                _check_env_provider(args, args.profile)
        _refuse_nested_item(item, args, store)   # (2026-10-27, S8 fix r1, SEC-L24)
        # (2026-10-21, v049 tester fix LOW-12) logged only once every check passed
        log.info("item %s: %s -> pending (user retry; session %s kept)", item["id"],
                 item.get("status"), item.get("session_id"))
        item.update(status="pending", error=None, wait_until=None, finished_at=None, limit_waits=0,
                    paused_from=None, blocked_question=None)
        if args.profile and args.profile != item.get("profile"):
            log.info("item %s: profile %s -> %s (retry)", item["id"], item.get("profile"), args.profile)
            item["profile"] = args.profile
            _ultracode_follows(item, "retry")   # (2026-10-27, modes fix HIGH)
        if is_goal(item):
            item["goal_continue"] = False
            item["no_marker_rounds"] = 0
            if args.max_rounds is not None:
                item["max_rounds"] = args.max_rounds
            if args.max_hours is not None:
                item["max_hours"] = args.max_hours
    out(f"retry queued {item['id']}")
    return EXIT_OK


def _cmd_first(args, store: Store, out) -> int:
    """(2026-10-21, start now) Move a pending item to the front of its queue: its seq goes below every
    other item's, so the runner starts it next (it never stops the attempt that is running now). A
    subtask runs right after its main task (store.run_order_key), so the main task's seq moves."""
    with store.transaction() as items:
        item = store.find(items, args.id)
        if item.get("status") != "pending":
            log.error("item %s is %s; only a pending item can be moved to the front", item["id"],
                      item.get("status"))
            raise _Abort()
        by_id = {i["id"]: i for i in items}
        if held_by_parent(item, by_id):
            log.error("subtask %s waits for its main task %s; start that one first", item["id"],
                      item.get("parent"))
            raise _Abort()
        target = by_id.get(item.get("parent") or "") or item
        order = runnable_pending(items)
        moved = bool(order) and order[0] is not item
        if moved:
            old = target.get("seq")
            target["seq"] = min((i["seq"] for i in items if isinstance(i.get("seq"), int)), default=1) - 1
            log.info("item %s: moved to the front of the queue (seq %s -> %d%s)", item["id"], old,
                     target["seq"], f", with its main task {target['id']}" if target is not item else "")
        ahead = [i["id"] for i in runnable_pending(items)].index(item["id"])
    if args.json:
        out(_json({"id": item["id"], "moved": moved, "ahead": ahead}))
    elif moved:
        out(f"moved {item['id']} to the front of the queue")
    else:
        out(f"{item['id']} is already first in the queue")
    return EXIT_OK


MOVE_REFUSED = ("running", "done", "cancelled")


def _cmd_move(args, store: Store, out) -> int:
    """(2026-10-26, owner task 07254297 "drag in priority") Move a not-done item to another place in
    its queue, inside the store transaction: only seqs change (store.move_seq, the mechanism of
    `first`), so the runner follows the new order at its next pick; the attempt that is running now
    is never stopped. A main task moves with its subtasks (they run right after it) and goes before or
    after a subtask's whole family; a subtask moves only among its main task's subtasks."""
    with store.transaction() as items:
        item = store.find(items, args.id)
        if item.get("status") in MOVE_REFUSED:
            log.error("item %s is %s; only a task that is not running, done or cancelled can be moved",
                      item["id"], item.get("status"))
            raise _Abort()
        by_id = {i["id"]: i for i in items}
        main = by_id.get(item.get("parent") or "")
        peers = order_peers(items, item, by_id)
        others = [p for p in peers if p is not item]
        ref = args.before if args.before is not None else args.after
        if ref is not None and not ref.strip():
            raise UsageError("move: --before/--after need an item id")
        if ref is None:
            index = 0 if args.top else len(others)
            where = "to the top" if args.top else "to the bottom"
        else:
            anchor = store.find(items, ref)
            if main is None:
                anchor = by_id.get(anchor.get("parent") or "") or anchor  # a subtask's family
            elif anchor.get("parent") != main["id"]:
                log.error("subtask %s always runs right after its main task %s; it can only move among "
                          "that task's subtasks", item["id"], main["id"])
                raise _Abort()
            if anchor is not item and not any(p is anchor for p in others):
                log.error("item %s cannot be placed next to %s: they are not in the same list", item["id"],
                          anchor["id"])
                raise _Abort()
            index = (next(n for n, p in enumerate(peers) if p is item) if anchor is item
                     else others.index(anchor) + (1 if args.after else 0))
            where = f"{'after' if args.after else 'before'} {anchor['id']}"
        old = item.get("seq")
        moved = move_seq(items, item, index)
        if moved:
            log.info("item %s: moved %s (seq %s -> %d)", item["id"], where, old, item["seq"])
        key = run_order_key(item, by_id)
        ahead = sum(1 for i in items if i is not item and i.get("status") == "pending"
                    and run_order_key(i, by_id) < key)
    if args.json:
        out(_json({"id": item["id"], "moved": moved, "ahead": ahead}))
    elif moved:
        out(f"moved {item['id']} {where}")
    else:
        out(f"{item['id']} is already there")
    return EXIT_OK


def _reply_text(args) -> str:
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --file {args.file}: {exc}") from exc
    else:
        text = args.text
    if not text or not text.strip():
        raise UsageError("the reply is empty")
    return text


def _pause_unreleased(item: dict, store: Store, now) -> bool:
    """(2026-10-21, security review L6) True while a live runner still owns the attempt this pause
    interrupted: the pause mark is set, recent, and the queue's runner is alive."""
    mark = parse_iso(item.get(PAUSE_UNRELEASED)) if isinstance(item.get(PAUSE_UNRELEASED), str) else None
    if mark is None or (now - mark).total_seconds() > PAUSE_UNRELEASED_MAX_S:
        return False
    return store.runner_alive() is not None


def _cmd_reply(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-15, SPEC_V03 5.2) Queue the owner's reply; the runner resumes the session with it.
    (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.8) A reply to a merge question maps to a merge action."""
    text = _reply_text(args)
    first = store.find(store.load(), args.id)
    q = _merge_question(first)
    if q is not None:
        return _reply_merge(args, store, clock, out, first, q, text)
    res = _queue_reply(store, first["id"], text, clock, args=args, profile=args.profile, by=getattr(args, "by", None))
    if args.json:
        out(_json(res))
    else:
        out(f"reply queued {res['id']}")
    return EXIT_OK


def _reply_refusals(item: dict, args, store: Store, profile: str | None) -> None:
    """(2026-10-27, U3 fix r1) The read-only refusals of a reply (_Abort / UsageError, nothing written): also
    run by `merge --let-claude` BEFORE merge_action writes the conflict markers and bumps the gen."""
    status = item.get("status")
    if status not in REPLY_STATUSES:
        log.error("item %s is %s; reply works on %s items", item["id"], status,
                  "/".join(REPLY_STATUSES))
        raise _Abort()
    if not item.get("session_id"):
        log.error("item %s has no session to reply to (it never started); use %s", item["id"],
                  "resume or edit" if status == "paused" else "retry")
        raise _Abort()
    if profile:
        # (2026-10-21, v049 tester fix LOW-2) refused here, as `retry --profile` is, not at run time
        _check_run_choice(item["id"], item.get("provider"), None, profile)
        if not item.get("provider"):   # (2026-10-22, re-test R4) the runner's provider, as at add
            _check_env_provider(args, profile)
    _refuse_nested_item(item, args, store)   # (2026-10-27, S8 fix r1, SEC-L24)


def _queue_reply(store: Store, item_id: str, text: str, clock: Clock, *, args=None, profile: str | None = None,
                 by: str | None = None, merge_reply: bool = False) -> dict:
    """(2026-10-27, "Tasks at once" U3) The body of `reply`, also used by `merge --let-claude`: the reply is new
    work, re-queued at the END; {"id", "status", "seq", "replies"}. A merge question is answered only through
    the merge mapping (`merge_reply`), never as a plain reply."""
    now = clock.now()
    with store.transaction() as items:
        item = store.find(items, item_id)
        if _merge_question(item) is not None and not merge_reply:
            log.error("item %s is blocked on a merge question; answer it with reply again or `merge`", item["id"])
            raise _Abort()
        status = item.get("status")
        _reply_refusals(item, args, store, profile)
        # (2026-10-21, reply to paused) the reply un-pauses it, as resume would: a usage-limit wait
        # still ahead is kept (waiting); one reply at a time (edit changes the queued one)
        new = "pending"
        if status == "paused":
            # (2026-10-21, security review L6) paused while running: until the runner has stopped that
            # attempt (it clears the mark), a reply could turn the item back to pending before the
            # runner's probe saw "paused"; the attempt would then finish it and the reply be lost
            if _pause_unreleased(item, store, now):
                log.error("item %s was paused while running and the runner is still stopping that "
                          "attempt; nothing was queued, try again in a moment", item["id"])
                raise _Abort()
            item.pop(PAUSE_UNRELEASED, None)
            if item.get("pending_reply"):
                log.error("item %s already has a reply queued; edit or withdraw it, or resume", item["id"])
                raise _Abort()
            wu = parse_iso(item.get("wait_until"))
            if item.get("paused_from") == "waiting" and wu is not None and wu > now:
                new = "waiting"
        replies = item.get("replies") if isinstance(item.get("replies"), list) else []
        replies.append({"at": iso(now), "text": text, **({"by": by} if by else {})})
        # (2026-10-21, edit reply) what `edit --drop-reply` restores while the reply is not sent yet
        item["reply_from"] = {k: item.get(k) for k in REPLY_FROM_KEYS}
        # A reply is new work: re-queued at the END (a new seq), not in its old place.
        seq = next_seq(items)
        # (2026-10-17, 0.4.1 R3) the question is answered: clear it
        item.update(replies=replies, pending_reply=text, status=new, seq=seq, limit_waits=0,
                    error=None, wait_until=item.get("wait_until") if new == "waiting" else None,
                    finished_at=None, paused_from=None, blocked_question=None)
        if profile and profile != item.get("profile"):
            log.info("item %s: profile %s -> %s (reply)", item["id"], item.get("profile"), profile)
            item["profile"] = profile
            _ultracode_follows(item, "reply")   # (2026-10-27, modes fix HIGH)
        if is_goal(item):
            item["goal_continue"] = False
            item["no_marker_rounds"] = 0
        log.info("item %s: %s -> %s (%s reply #%d, %d chars; session %s kept; seq %d)",
                 item["id"], status, new, by or "owner", len(replies), len(text),
                 item["session_id"], seq)
    return {"id": item["id"], "status": new, "seq": seq, "replies": len(replies)}


def _edit_prompt(args) -> str | None:
    if args.file:
        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --file {args.file}: {exc}") from exc
    elif args.text is not None:
        text = args.text
    else:
        return None
    if not text.strip():
        raise UsageError("the prompt is empty")
    return text


def _edit_reply(args) -> str | None:
    if args.reply_file:
        try:
            text = Path(args.reply_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --reply-file {args.reply_file}: {exc}") from exc
    elif args.reply_text is not None:
        text = args.reply_text
    else:
        return None
    if not text.strip():
        raise UsageError("the reply is empty; --drop-reply withdraws it")
    return text


def _pending_reply_entry(item: dict) -> dict | None:
    """The `replies` entry of the reply not sent yet: the last one with its text (an attempt that
    ended before Claude answered put it back), else the last one no attempt carried."""
    replies = [r for r in (item.get("replies") or []) if isinstance(r, dict)]
    for r in reversed(replies):
        if r.get("text") == item.get("pending_reply"):
            return r
    return next((r for r in reversed(replies) if r.get("attempt") is None), None)


def _withdraw_reply(item: dict) -> None:
    """(2026-10-21, edit reply) Drop the reply not sent yet. A pending/paused item goes back to the
    state `reply` found it in (reply_from); without that record it is blocked, so nothing runs a bare
    "continue". A waiting/failed item (an attempt ended before Claude answered) keeps its state."""
    entry = _pending_reply_entry(item)
    if entry is not None:
        item["replies"] = [r for r in item["replies"] if r is not entry]
    item["pending_reply"] = None
    snap = item.pop("reply_from", None)
    # (2026-10-21, reply to paused) a reply to a paused item still in its usage-limit wait left it
    # waiting; no attempt has carried it yet (reply_from is dropped when one does): back to paused
    from_paused = isinstance(snap, dict) and snap.get("status") == "paused"
    if item.get("status") not in ("pending", "paused") and not (from_paused and item.get("status") == "waiting"):
        return
    item["paused_from"] = None
    if isinstance(snap, dict) and snap.get("status") in REPLY_STATUSES:
        for k in REPLY_FROM_KEYS:
            if k != "status" and not (k == "seq" and snap.get(k) is None):
                item[k] = snap.get(k)
        transition(item, snap["status"], "owner withdrew the reply")
    else:
        item["error"] = REPLY_WITHDRAWN_ERROR
        transition(item, "blocked", "owner withdrew the reply")


def _cmd_edit(args, store: Store, out, clock: Clock | None = None) -> int:
    """(2026-10-21, edit) Change an item's prompt, name, profile, model or subagent limit in place: status, seq and session
    are kept. The prompt only before the first session (Claude already has the old one; a started item
    gets new instructions with `reply`). A main task's new label goes to its subtasks' parent_label.
    (2026-10-21, edit reply) `--reply-text/--reply-file` change, `--drop-reply` withdraws the owner's
    reply while it is still `pending_reply` (not running: then Claude has it)."""
    prompt = _edit_prompt(args)
    reply = _edit_reply(args)
    model = getattr(args, "model", None)
    provider = getattr(args, "provider", None)   # (2026-10-21, add panel) None = not given, "" = the run's
    effort = getattr(args, "effort", None)
    raw_agents = getattr(args, "max_agents", None)
    ultracode = getattr(args, "ultracode", None)   # (2026-10-26, modes) None = not given
    autorun = getattr(args, "autorun", None)
    # (2026-10-27, "Tasks at once" U3) --after/--after-previous/--no-after and --in-place/--no-in-place
    order_given = bool(getattr(args, "after", None) or getattr(args, "after_previous", False)
                       or getattr(args, "no_after", False))
    in_place = getattr(args, "in_place", None)
    only_autorun = autorun is not None and (prompt is None and reply is None and not args.drop_reply
                                            and args.name is None and args.profile is None and model is None
                                            and provider is None and effort is None and raw_agents is None
                                            and ultracode is None and not order_given and in_place is None)
    if (prompt is None and reply is None and not args.drop_reply and args.name is None and args.profile is None
            and model is None and provider is None and effort is None and raw_agents is None
            and ultracode is None and autorun is None and not order_given and in_place is None):
        raise UsageError("nothing to change: give --text, --file, --reply-text, --reply-file, --drop-reply, "
                         "--name, --profile, --model, --provider, --effort, --max-agents, --ultracode, --autorun, "
                         "--after, --after-previous, --no-after, --in-place or --no-in-place")
    if prompt is not None and (reply is not None or args.drop_reply):
        raise UsageError("change the prompt or the reply, not both")
    with store.transaction() as items:
        item = store.find(items, args.id)
        # (2026-10-26, modes) autorun alone is allowed while it runs: the attempt does not read it
        if item.get("status") == "running" and not only_autorun:
            log.error("item %s is running; pause or cancel it first, then edit it", item["id"])
            raise _Abort()
        # (2026-10-21, add panel) the provider/effort/profile the item would have: the run's rules first,
        # so a refused edit changes nothing at all
        if args.profile or provider is not None or effort is not None:
            _check_run_choice(item["id"], item.get("provider") if provider is None else (provider or None),
                              item.get("effort") if effort is None else (effort or None),
                              args.profile or item.get("profile") or DEFAULT_PROFILE)
            # (2026-10-22, re-test R4) no provider of its own after the edit: the runner's, as at add
            if not (item.get("provider") if provider is None else (provider or None)):
                _check_env_provider(args, args.profile or item.get("profile") or DEFAULT_PROFILE)
        # (2026-10-27, modes fix HIGH) `--ultracode on` only where the Workflow tool can be offered
        why = (ultracode_refusal(args.profile or item.get("profile") or DEFAULT_PROFILE,
                                 item.get("max_agents") if raw_agents is None else parse_agents_text(raw_agents))
               if ultracode is True else None)
        if why:
            raise UsageError(f"item {item['id']}: {why}")
        changed: list[str] = []
        if reply is not None or args.drop_reply:
            if not item.get("pending_reply"):
                log.error("item %s has no reply waiting to be sent (Claude already has the replies it "
                          "shows); send a new one with reply", item["id"])
                raise _Abort()
            if args.drop_reply:
                log.info("item %s: the owner's reply withdrawn (%d chars)", item["id"], len(item["pending_reply"]))
                _withdraw_reply(item)
                changed.append("reply-withdrawn")
            elif reply != item["pending_reply"]:
                log.info("item %s: reply edited (%d -> %d chars)", item["id"], len(item["pending_reply"]), len(reply))
                entry = _pending_reply_entry(item)
                if entry is not None:
                    entry.update(text=reply, attempt=None)
                    entry.pop("by", None)   # (2026-10-27, modes fix round 2) the owner's words now
                item["pending_reply"] = reply
                changed.append("reply")
        restart = getattr(args, "restart", False)
        if restart:
            _check_restart(item, prompt)
            # (2026-10-26, edit paused) as reply: the runner still stopping that attempt could write
            # the old session back over the restart
            if _pause_unreleased(item, store, (clock or Clock()).now()):
                log.error("item %s was paused while running and the runner is still stopping that "
                          "attempt; nothing was changed, try again in a moment", item["id"])
                raise _Abort()
            item.pop(PAUSE_UNRELEASED, None)
        if prompt is not None and prompt != item.get("prompt"):
            if item.get("session_id") and restart:
                # (2026-10-26, edit paused) the old session is kept in prior_sessions, never resumed
                log.info("item %s: prompt edited while paused; session %s dropped (kept in prior_sessions), "
                         "the task starts over when resumed", item["id"], item["session_id"])
                prior = item.get("prior_sessions") if isinstance(item.get("prior_sessions"), list) else []
                prior.append(item["session_id"])
                item["prior_sessions"] = prior
                item["session_id"] = None
                item["blocked_question"] = None
                item["fresh"] = None           # the new prompt itself, not a carried-over context
                item.pop(BG_REDO, None)
                changed.append("restart")
            elif item.get("session_id"):
                log.error("item %s has already started (session %s), so its prompt is sent; give new "
                          "instructions with reply", item["id"], item["session_id"])
                raise _Abort()
            log.info("item %s: prompt edited (%d -> %d chars)", item["id"], len(item.get("prompt") or ""),
                     len(prompt))
            item["prompt"] = prompt
            changed.append("prompt")
        if args.name is not None:
            name = args.name.strip() or None
            if name != item.get("name"):
                log.info("item %s: name edited", item["id"])
                item["name"] = name
                changed.append("name")
        if args.profile and args.profile != item.get("profile"):
            log.info("item %s: profile %s -> %s (edit)", item["id"], item.get("profile"), args.profile)
            item["profile"] = args.profile
            changed.append("profile")
        # (2026-10-21, task model) the next attempt runs it; "" goes back to the run's model
        if model is not None and (model or None) != item.get("model"):
            log.info("item %s: model %s -> %s (edit)", item["id"], item.get("model") or "(run's)",
                     model or "(run's)")
            item["model"] = model or None
            changed.append("model")
        # (2026-10-21, add panel) the same for the provider and the effort
        if provider is not None and (provider or None) != item.get("provider"):
            log.info("item %s: provider %s -> %s (edit)", item["id"], item.get("provider") or "(run's)",
                     provider or "(run's)")
            item["provider"] = provider or None
            changed.append("provider")
        if effort is not None and (effort or None) != item.get("effort"):
            log.info("item %s: effort %s -> %s (edit)", item["id"], item.get("effort") or "(run's)",
                     effort or "(run's)")
            item["effort"] = effort or None
            changed.append("effort")
        # (2026-10-21, subagents) the next attempt's argv offers (or denies) the Agent tool
        if raw_agents is not None:
            agents = parse_agents_text(raw_agents)
            if agents != item.get("max_agents"):
                log.info("item %s: subagents %s -> %s (edit)", item["id"],
                         agents_label(item.get("max_agents")), agents_label(agents))
                item["max_agents"] = agents
                changed.append("max-agents")
        # (2026-10-26, modes) ultracode from the next attempt's argv; autorun for the next question
        for flag, value in (("ultracode", ultracode), ("autorun", autorun)):
            if value is not None and value != (item.get(flag) is True):
                log.info("item %s: %s %s (edit)", item["id"], flag, "on" if value else "off")
                item[flag] = value
                changed.append(flag)
        if _ultracode_follows(item, "edit"):   # (2026-10-27, modes fix HIGH) a new profile or limit
            changed.append("ultracode")
        # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 5.1/5.8) what it waits for; a cycle is refused
        if order_given:
            after = _edit_after(args, store, items, item)
            if order.after_cycle(items, item["id"], after):
                log.error("item %s: waiting for %s would close a cycle (it waits, directly or not, for this "
                          "item); nothing was changed", item["id"], after)
                raise _Abort()
            if after != item.get("after"):
                log.info("item %s: after %s -> %s (edit)", item["id"], item.get("after"), after)
                item["after"] = after
                item["after_source"] = "owner" if after else None
                changed.append("after")
        if in_place is not None and in_place != (item.get("in_place") is True):
            rec = item.get("worktree")
            if in_place and isinstance(rec, dict) and rec.get("state") not in (None, "removed"):
                log.error("item %s has a private copy (%s); %s", item["id"], rec.get("state"), IN_PLACE_REFUSED)
                raise _Abort()
            log.info("item %s: in_place %s (edit)", item["id"], "on" if in_place else "off")
            item["in_place"] = in_place
            changed.append("in-place")
        if "prompt" in changed or "name" in changed:
            for sub in subtasks_of(items, item["id"]):
                sub["parent_label"] = parent_label(item)
    if args.json:
        out(_json({"id": item["id"], "changed": changed}))
    else:
        out(f"edited {item['id']} ({', '.join(changed)})" if changed else f"nothing changed in {item['id']}")
    return EXIT_OK


def _check_restart(item: dict, prompt) -> None:
    """(2026-10-26, edit paused) `edit --restart` only gives a paused task (not a goal) a new prompt;
    a reply Claude has not got yet is edited or withdrawn first."""
    if prompt is None:
        raise UsageError("--restart needs the new prompt: give --text or --file")
    if item.get("status") != "paused":
        log.error("item %s is %s; only a paused task starts over with a new prompt (pause it first)",
                  item["id"], item.get("status"))
        raise _Abort()
    if item.get("kind") == KIND_GOAL:
        log.error("item %s is a goal; --restart is for tasks", item["id"])
        raise _Abort()
    if item.get("pending_reply"):
        log.error("item %s has a reply waiting to be sent; edit or withdraw that reply first", item["id"])
        raise _Abort()


def _cmd_pause(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-13, SPEC_GOALS section 2) pending/waiting keep session and wait_until; a running item
    becomes paused (paused_from=pending) and the runner's status probe kills its child within ~1 s."""
    with store.transaction() as items:
        item = store.find(items, args.id)
        status = item.get("status")
        # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 1.8) a finished task whose changes are being merged
        if (item.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE:
            log.error("item %s: %s", item["id"], PAUSE_MERGING)
            raise _Abort()
        if status in ("pending", "waiting"):
            item["paused_from"] = status
        elif status == "running":
            item["paused_from"] = "pending"
            # (2026-10-21, security review L6) the runner still owns it until its probe stops the
            # attempt; loop.apply_interrupt/apply_outcome clear this, and reply waits for that
            item[PAUSE_UNRELEASED] = iso(clock.now())
            log.info("item %s is running; the runner stops its attempt within ~1 s (session kept)",
                     item["id"])
        else:
            log.error("item %s is %s; only pending, waiting or running items can be paused",
                      item["id"], status)
            raise _Abort()
        transition(item, "paused", "user pause")
    out(f"paused {item['id']}")
    return EXIT_OK


def _cmd_resume(args, store: Store, clock: Clock, out) -> int:
    with store.transaction() as items:
        item = store.find(items, args.id)
        if item.get("status") != "paused":
            log.error("item %s is %s, not paused; nothing to resume", item["id"], item.get("status"))
            raise _Abort()
        new = item.get("paused_from") if item.get("paused_from") in ("pending", "waiting") else "pending"
        if new == "waiting":
            wu = parse_iso(item.get("wait_until"))
            if wu is None or wu <= clock.now():
                new = "pending"
                item["wait_until"] = None
        item["paused_from"] = None
        transition(item, new, "user resume")
    out(f"resumed {item['id']} ({new})")
    return EXIT_OK


def _cmd_status(args, store: Store, out) -> int:
    items = store.load()
    counts = Counter(i.get("status") for i in items)
    if args.json:
        runner = store.runner_alive()
        waits = sorted(filter(None, (parse_iso(i.get("wait_until")) for i in items
                                     if i.get("status") == "waiting")))
        out(_json({
            "counts": {s: counts.get(s, 0) for s in STATUSES},
            "runner_alive": runner is not None,
            "runner_pid": runner["pid"] if runner else None,
            "runner_version": _runner_version(runner),
            "next_wait_until": iso(waits[0]) if waits else None,
            "data_dir": str(store.data_dir),
            "hold": store.read_hold(),  # (2026-10-21, v049 dontask_hold) null or {"since"}
            "usage": usage.summary(args.base_dir, time.time()),  # (2026-10-26, usage forecast) or null
        }))
        return EXIT_OK
    out("counts: " + ", ".join(f"{s}={counts.get(s, 0)}" for s in STATUSES))
    runner = store.runner_alive()
    out(f"runner: alive (pid {runner['pid']}, since {runner.get('started_at')})" if runner
        else "runner: not running")
    waits = sorted(filter(None, (parse_iso(i.get("wait_until")) for i in items
                                 if i.get("status") == "waiting")))
    out(f"next wait_until: {iso(waits[0]) if waits else '-'}")
    held = store.read_hold()
    out(f"hold: on (since {held.get('since') or '?'})" if held is not None else "hold: off")
    return EXIT_OK


def _cmd_hold(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 2) `hold on|off|status` of one queue
    (--project/--lane). `on` never stops a live runner: it finishes its current item, then exits."""
    until = getattr(args, "until", None)
    if until is not None and args.action != "on":
        raise UsageError("--until goes with `hold on`")
    if until is not None and not clock.now() < until <= clock.now() + HOLD_UNTIL_MAX:
        raise UsageError("--until must be in the future, at most 8 days from now")
    if args.action == "on":
        was = store.read_hold(clock.now())
        held = store.set_hold(clock.now(), until)
        log.info("hold on: %s (since %s)%s", store.data_dir, held.get("since"),
                 "" if not was else " (already on)" if was.get("until") == held.get("until")
                 else " (made indefinite)" if not held.get("until")  # (2026-10-27, fix round 2 MED)
                 else f" (extended until {held.get('until')})")  # (2026-10-27, fix LOW) Store.set_hold
    elif args.action == "off":
        if store.clear_hold():
            log.info("hold off: %s", store.data_dir)
        held = None
    elif args.action == "limit-off":
        was = store.read_hold(clock.now())
        held = store.release_limit_hold(clock.now())
        if was is not None and was.get("reason") != HOLD_REASON_LIMIT:
            log.info("hold limit-off: %s is not on a usage-limit hold; its hold (since %s) stays", store.data_dir,
                     was.get("since"))
        elif held is not None:
            log.info("hold limit-off: %s: usage-limit hold released; the owner's hold (since %s)%s is back",
                     store.data_dir, held.get("since"),
                     f" until {held['until']}" if held.get("until") else "")  # (2026-10-27, integrate)
        elif was is not None:
            log.info("hold limit-off: %s: usage-limit hold released", store.data_dir)
    else:
        held = store.read_hold()
    runner = store.runner_alive()
    if args.json:
        out(_json({"hold": held is not None, "since": held.get("since") if held else None,
                   "runner_alive": runner is not None, "runner_pid": runner["pid"] if runner else None,
                   "data_dir": str(store.data_dir),
                   "until": held.get("until") if held else None}))  # (2026-10-26, usage forecast)
        return EXIT_OK
    if held is None:
        out("hold: off" + (" (released)" if args.action == "off" else ""))
        return EXIT_OK
    if held.get("until"):  # (2026-10-26, usage forecast)
        out(f"hold: on (since {held.get('since') or '?'}) until {held['until']}; a runner waits for it")
        return EXIT_OK
    out(f"hold: on (since {held.get('since') or '?'}); no runner starts for this queue "
        "(`run` refuses without --ignore-hold)")
    if runner is not None:
        out(f"the live runner (pid {runner['pid']}) is not stopped: it finishes its current item, then exits")
    return EXIT_OK


def _cmd_stop(store: Store, clock: Clock, out) -> int:
    """(2026-10-13, SPEC_VSCODE section 2) Ask a live runner to stop; it deletes stop.request."""
    runner = store.runner_alive()
    if runner is None:
        if store.clear_stop_request():
            log.warning("removed a leftover %s (no runner alive)", store.stop_path.name)
        # (2026-10-26, runner identity) a stale lock (dead or reused PID) is removed, never signalled:
        # its PID may belong to an unrelated process now (observed: chrome.exe)
        stale = store.remove_stale_lock()
        log.info("stop: no runner alive")
        if stale is not None:
            out(f"runner not running (stale runner.lock of pid {stale.get('pid', '?')} removed); "
                "nothing to stop")
        else:
            out("no runner alive; nothing to stop")
        return EXIT_OK
    store.request_stop(clock.now())
    log.info("stop: requested for runner pid %s (%s)", runner["pid"], store.stop_path)
    out(f"stop requested for runner pid {runner['pid']}")
    return EXIT_OK


def _runner_version(runner: dict | None) -> str | None:
    """(2026-10-20, 0.4.4 logfix D) the live runner's core version; "" for an older lock (no field)."""
    if runner is None:
        return None
    v = runner.get("version")
    return v if isinstance(v, str) else ""


def _cmd_shell_allow(args, out) -> int:
    """(2026-10-20, 0.4.4 logfix A) list/add/remove the project's shell-dev allow-list (shell_allow.json in
    the project's data dir). A rejected entry exits 2 with the reason."""
    path = allow_file(args.project_dir)
    entries = load_allow_list(path)
    entry = ""
    if args.action in ("add", "remove"):
        if args.command is None:
            raise UsageError(f'shell-allow {args.action} needs a command, e.g. "git merge"')
        try:
            entry = normalise_allow_entry(args.command)
        except AllowListError as exc:
            if args.action == "add":
                raise UsageError(str(exc)) from None
            entry = " ".join(str(args.command).split())
        low = [e.lower() for e in entries]
        if args.action == "add":
            if entry.lower() in low:
                log.info("shell-allow: %r is already on the list (%s)", entry, path)
            elif len(entries) >= SHELL_ALLOW_MAX:
                raise UsageError(f"the allow-list is full ({SHELL_ALLOW_MAX} commands)")
            else:
                entries.append(entry)
                save_allow_list(path, entries)
                log.info("shell-allow: added %r (%s)", entry, path)
        else:
            if entry.lower() not in low:
                log.error("shell-allow: %r is not on the list (%s)", entry, path)
                raise _Abort()
            entries = [e for e in entries if e.lower() != entry.lower()]
            save_allow_list(path, entries)
            log.info("shell-allow: removed %r (%s)", entry, path)
    if args.json:
        out(_json({"file": str(path), "commands": entries}))
    elif args.action == "list":
        out(f"shell-dev allow-list ({path}):")
        for e in entries:
            out(f"  {e}")
        if not entries:
            out("  (empty: shell-dev runs like shell-read)")
    else:
        done = "added" if args.action == "add" else "removed"
        out(f"{done} {entry!r}; {len(entries)} command(s) allowed")
    return EXIT_OK


def _load_keys(args) -> tuple[Path | None, dict, list[Path]]:
    """(2026-10-20, 0.4.7 providers) The .env in use, its values and every path searched."""
    try:
        return providers.load_keys(getattr(args, "env_file", None), args.project_dir,
                                   base_dir=getattr(args, "base_dir", None))
    except providers.ProviderError as exc:
        raise UsageError(str(exc)) from None


def _key_loader(args):
    """(2026-10-21, add panel) Reads providers.env again for an item on its own provider (a missing
    --env-file is a ProviderError then: that item fails, the queue goes on)."""
    def load() -> dict:
        return providers.load_keys(getattr(args, "env_file", None), args.project_dir,
                                   base_dir=getattr(args, "base_dir", None))[1]
    return load


def _env_file_line(path: Path | None, candidates: list[Path]) -> str:
    if path:
        return f"env file: {path}"
    return "env file: none found (searched: " + ", ".join(str(c) for c in candidates) + ")"


def _cmd_providers(args, out) -> int:
    """(2026-10-20, 0.4.7 providers) The model providers, their models and the .env variable each one
    needs. Key VALUES are never printed: only the variable name and set/missing."""
    path, values, candidates = _load_keys(args)
    rows = providers.describe(values)
    selected = providers.select_name(None, values)
    if args.json:
        out(_json({"env_file": str(path) if path else None,
                   "searched": [str(c) for c in candidates],
                   "selected": selected, "providers": rows}))
        return EXIT_OK
    out(_env_file_line(path, candidates))
    out(f"used unless `run --provider NAME` says otherwise: {selected}")
    for r in rows:
        state = "ready" if r["ready"] else "needs " + ", ".join(r["missing"]) + " in providers.env"
        if r.get("refused"):   # (2026-10-26, network audit MEDIUM) a base URL resolve() refuses
            state = f"refused: {r['refused']}"
        out(f"  {r['name']:<11} {state}")
        out(f"              {r['label']}")
        models = f" (of {', '.join(r['models'])})" if len(r["models"]) > 1 else ""
        endpoint = f"; endpoint {r['base_url']}" if r["base_url"] else ""
        out(f"              model {r['model'] or '(set it in providers.env)'}{models}{endpoint}")
        if r["key_var"]:
            out(f"              key {r['key_var']}: {'set' if r['key_set'] else 'missing'}")
        if r["note"]:
            out(f"              note: {r['note']}")
    return EXIT_OK


def _repo_label(repo: str, flag: str | None) -> str:
    """(2026-10-27, SEC-L10) `github.com/OWNER/NAME`, plus where it came from when it is not the default."""
    if repo == report.DEFAULT_REPO:
        return f"github.com/{repo}"
    origin = "--repo" if flag else f"${report.ENV_REPO}"
    return f"github.com/{repo} (from {origin}; the default is github.com/{report.DEFAULT_REPO})"


def _cmd_report(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-21, docs/SPEC_V048_REPORT.md) A redacted issue draft in <data dir>/reports/ and its
    prefilled github.com new-issue link. Nothing is posted: the owner reviews the page and submits it."""
    description = ""
    if args.file:
        try:
            description = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read --file {args.file}: {exc}") from exc
    elif args.text:
        description = args.text
    try:
        repo = report.resolve_repo(args.repo)
    except report.ReportError as exc:
        raise UsageError(str(exc)) from None
    _path, values, _candidates = _load_keys(args)
    items = store.load()
    item = with_defaults(store.find(items, args.item)) if args.item else None
    status = _counts_and_runner(store)
    # (amendment M11) a project with no queue yet gets no data dir: the draft goes to the base dir
    folder = store.data_dir if store.data_dir.is_dir() else args.base_dir
    try:
        rep = report.make_report(data_dir=folder, lane=args.lane, counts=status["counts"],
                                 runner_alive=status["runner_alive"], key_values=values, repo=repo,
                                 now_stamp=clock.now().strftime("%Y%m%dT%H%M%SZ"), title=args.title,
                                 description=description, item=item, lines=args.lines,
                                 project_root=args.project_root)
    except report.ReportError as exc:
        log.error("report: %s", exc)
        return EXIT_FAIL
    log.info("report: draft %s (%d value(s) redacted, %d log line(s))", rep.file, rep.redactions,
             rep.log_lines)
    if args.json:
        out(_json({"file": str(rep.file), "url": rep.url, "title": rep.title, "repo": rep.repo,
                   "redactions": rep.redactions, "log_lines": rep.log_lines,
                   "url_log_lines": rep.url_log_lines}))
    else:
        out(f"draft: {rep.file}")
        out(f"repo: {_repo_label(repo, args.repo)}")   # (2026-10-27, SEC-L10) where the link goes
        # (2026-10-27, S8 fix r2, SEC-L17) the title comes from an errors.log row: no ESC/BEL on the console
        out(f"title: {strip_controls(report.stdout_safe(rep.title))}")
        out(f"redacted: {rep.redactions} value(s); prompts, task results and run logs are not included "
            "(errors.log lines and the item error can quote short, redacted pieces of Claude's text)")
        if rep.url_log_lines < rep.log_lines:
            out(f"the link carries {rep.url_log_lines} of {rep.log_lines} log lines; attach the draft "
                "for the rest")
        out("read the draft, then open this link; nothing is posted until you press Submit on GitHub:")
        out(rep.url)
    if args.open:
        # (2026-10-27, SEC-L10) the browser page names no repo before it loads: the console does (stderr, so
        # `--json --open` says it too), as a warning when it is not the default repo
        (log.info if repo == report.DEFAULT_REPO else log.warning)(
            "report: opening a new-issue page on %s in the browser", _repo_label(repo, args.repo))
        import webbrowser
        if not webbrowser.open(rep.url):
            log.warning("report: could not open a browser; open the link above yourself")
    return EXIT_OK


def _counts_and_runner(store: Store) -> dict:
    items = store.load()
    counts = Counter(i.get("status") for i in items)
    runner = store.runner_alive()
    waits = sorted(filter(None, (parse_iso(i.get("wait_until")) for i in items
                                 if i.get("status") == "waiting")))
    return {"counts": {s: counts.get(s, 0) for s in STATUSES}, "runner_alive": runner is not None,
            "runner_pid": runner["pid"] if runner else None, "runner_version": _runner_version(runner),
            "next_wait_until": iso(waits[0]) if waits else None,
            "hold": store.read_hold()}  # (2026-10-21, v049 dontask_hold) null or {"since"}


def _lane_row(name: str, d: Path, with_items: bool) -> dict:
    """(2026-10-15, SPEC_V03 2.2) One lane: name, counts, runner, data_dir [, items]."""
    row = {"name": name, "data_dir": str(d), "error": None}
    st = Store(d)
    # (2026-10-27, "Tasks at once" U3) the lane's live number and its runner's parallel.json (null unless alive)
    row.update(tasks_at_once=st.read_tasks_at_once() or 1, parallel=_live_parallel(st))
    try:
        row.update(_counts_and_runner(st))
        if with_items:
            every = _in_run_order(st.load())
            row["items"] = _json_items(st, every, every)
    except StoreError as exc:
        log.error("queue %s in %s: %s", name, d, exc)
        row.update(counts={s: 0 for s in STATUSES}, runner_alive=False, runner_pid=None,
                   next_wait_until=None, error=str(exc), hold=Store(d).read_hold())
        if with_items:
            row["items"] = []
    return row


def _lane_rows(project_dir: Path, with_items: bool) -> list[dict]:
    return [_lane_row(name, d, with_items) for name, d in list_lanes(project_dir)]


def _project_row(d: Path, root: str | None, shared: bool, with_items: bool) -> dict:
    """(2026-10-15) `lanes` lists every lane; the top-level counts/items stay the SUM over the lanes
    (compatibility), runner_alive is true when any lane's runner is alive, runner_pid is the first
    live one (the default lane first) and next_wait_until the earliest over all lanes."""
    row = {"name": "(shared queue)" if shared else (Path(root).name if root else d.name),
           "data_dir": str(d), "root": root, "shared": shared,
           "root_exists": bool(root) and Path(root).is_dir(), "error": None}
    lanes = _lane_rows(d, with_items)
    row["counts"] = {s: sum(ln["counts"].get(s, 0) for ln in lanes) for s in STATUSES}
    alive = [ln for ln in lanes if ln["runner_alive"]]
    row["runner_alive"] = bool(alive)
    row["runner_pid"] = alive[0]["runner_pid"] if alive else None
    waits = [(parse_iso(w), w) for w in (ln["next_wait_until"] for ln in lanes) if w]
    waits = [x for x in waits if x[0] is not None]
    row["next_wait_until"] = min(waits)[1] if waits else None
    errors = [ln["error"] if ln["name"] == DEFAULT_LANE else f"queue {ln['name']}: {ln['error']}"
              for ln in lanes if ln["error"]]
    row["error"] = "; ".join(errors) or None
    if with_items:
        row["items"] = [it for ln in lanes for it in ln["items"]]
    row["lanes"] = lanes
    return row


def _cmd_projects(args, out) -> int:
    """(2026-10-13, SPEC_PROJECTS section 2 + amendments M10/M11) Every <base>/projects/* dir that
    holds a queue.json, with its data_dir and root; `--items` adds every item, so the extension's
    all-projects view is ONE process per poll. The base (shared) queue is listed too when it has
    items or a live runner (review L18), with `shared: true` and `root: null`."""
    base = Path(args.base_dir)
    root_dir = base / PROJECTS_DIRNAME
    rows = []
    dirs = sorted((d for d in root_dir.iterdir() if d.is_dir() and has_any_queue(d)),
                  key=lambda d: d.name.lower()) if root_dir.is_dir() else []
    for d in dirs:
        root = read_project_meta(d).get("root")
        rows.append(_project_row(d, root if isinstance(root, str) else None, False, args.items))
    if has_any_queue(base) or any(Store(ld).runner_alive() for _n, ld in list_lanes(base)):
        shared = _project_row(base, None, True, args.items)
        if sum(shared["counts"].values()) or shared["runner_alive"]:
            rows.append(shared)
    if args.json:
        out(_json(rows))
        return EXIT_OK
    if not rows:
        out(f"no projects under {root_dir}")
    for r in rows:
        c = r["counts"]
        busy = ", ".join(f"{s}={c[s]}" for s in STATUSES if c.get(s))
        out(f"{r['name']:<30} {'runner alive' if r['runner_alive'] else 'idle':<12} {busy or 'empty'}")
        out(f"    root: {r['root'] or '-'}{'' if r['root_exists'] or r['shared'] else '  (missing)'}")
        if len(r["lanes"]) > 1:
            out(f"    queues: {', '.join(ln['name'] for ln in r['lanes'])}")
    return EXIT_OK


def _cmd_where(args, store: Store, out) -> int:
    root = args.project_root  # canonical (amendment M10); the dir may not exist yet (M11)
    if args.json:
        out(_json({"data_dir": str(store.data_dir), "project_root": root, "lane": args.lane}))
    else:
        out(str(store.data_dir))
    return EXIT_OK


def _cmd_lanes(args, store: Store, out) -> int:
    """(2026-10-15, SPEC_V03 2.2) The lanes of the selected project (or the base dir), `default`
    first. Read-only: a project or lane that does not exist is listed empty and nothing is created.
    `--items` adds every lane's items, so the extension's this-project poll is ONE process."""
    rows = _lane_rows(store.data_dir, args.items)
    if args.json:
        if rows:  # (2026-10-26, usage forecast) the account's usage, on the first row only (not per queue)
            rows[0]["usage"] = usage.summary(args.base_dir, time.time())
        out(_json(rows))
        return EXIT_OK
    for r in rows:
        c = r["counts"]
        busy = ", ".join(f"{s}={c[s]}" for s in STATUSES if c.get(s))
        runner = f"runner alive (pid {r['runner_pid']})" if r["runner_alive"] else "idle"
        out(f"{r['name']:<20} {runner:<24} {busy or 'empty'}")
        out(f"    data: {r['data_dir']}")
    return EXIT_OK


def _rename_with_retry(src: Path, dst: Path) -> None:
    """os.rename, retried on PermissionError for about 2 s: on Windows a reader holding a file in the
    lane dir open (a status poll's log, a queue read) refuses the rename for a moment."""
    for delay in (*RENAME_DELAYS_S, None):
        try:
            os.rename(src, dst)
            return
        except PermissionError as exc:
            if delay is None:
                raise
            log.debug("rename %s refused (%s); retrying in %.2fs", src, exc, delay)
            time.sleep(delay)  # real sleep on purpose: a sub-second file-sharing retry


def _cmd_lane_remove(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-15, SPEC_V03 2.2) Move an empty lane to <project dir>/lanes/.removed/<name>-<stamp>/.

    - Refused (exit 1) while the lane has pending, running, waiting or paused items, or a live
      runner; `default` and an invalid name are usage errors (exit 2); a missing lane is exit 1.
    - The check and the move happen under the lane's queue mutex, so an `add` that raced us either
      landed before the check (and the remove is refused) or waits and then recreates the lane.
    - The dir is MOVED, never deleted (the global back-up-first rule).
    """
    name = args.name
    if not valid_lane_name(name):
        raise UsageError(f"invalid queue name {name!r}")
    if is_default_lane(name):
        raise UsageError("the default queue cannot be removed")
    canon, lane_dir = lane_data_dir(store.data_dir, name)
    if not lane_dir.is_dir():
        log.error("no queue named %r in %s", name, store.data_dir)
        raise _Abort()
    lane = Store(lane_dir)
    runner = lane.runner_alive()
    if runner:
        log.error("queue %s has a live runner (pid %s); stop it first", canon, runner["pid"])
        raise _Abort()
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 3.10) the lane's copies are kept (they become leftovers);
    # their unmerged work is saved as patches first, outside the queue mutex (lock order ProjectLock -> queue)
    active = [i for i in lane.load() if i.get("status") in ACTIVE_STATUSES]
    if active:
        log.error("queue %s still has %d pending, running, waiting or paused item(s); "
                  "remove or finish them first", canon, len(active))
        raise _Abort()
    shadow, copies = _shadow_copy_items(lane)
    patches = [p for p in (_save_copy_work(shadow, it, "lane-removed")[1] for it in copies) if p]
    kept = [str(shadow.worktree_path(it)) for it in copies]
    stamp = clock.now().strftime("%Y%m%dT%H%M%SZ")
    removed = store.data_dir / LANES_DIRNAME / REMOVED_LANES_DIRNAME
    dest = removed / f"{canon}-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = removed / f"{canon}-{stamp}-{n}"
    with lane._mutex():
        active = [i for i in lane.load() if i.get("status") in ACTIVE_STATUSES]
        if active:
            log.error("queue %s still has %d pending, running, waiting or paused item(s); "
                      "remove or finish them first", canon, len(active))
            raise _Abort()
        if lane.runner_alive():
            log.error("queue %s: a runner started meanwhile; stop it first", canon)
            raise _Abort()
        removed.mkdir(parents=True, exist_ok=True)
        _rename_with_retry(lane_dir, dest)
    with contextlib.suppress(OSError):  # the mutex file moved with the dir
        (dest / lane.mutex_path.name).unlink()
    log.info("queue %s: moved %s to %s", canon, lane_dir, dest)
    for path in kept:
        log.info("queue %s: its copy %s is kept (a leftover in `worktrees`)", canon, path)
    if args.json:   # (2026-10-27, "Tasks at once" U3) + patches and leftovers only when it had copies
        out(_json({"removed": canon, "backup": str(dest), **({"patches": patches, "leftovers": kept} if kept else {})}))
    else:
        out(f"removed queue {canon} (backup: {dest})")
        for p in patches:
            out(f"  changes saved to {p}")
        for path in kept:
            out(f"  copy kept (a leftover in `worktrees`): {path}")
    return EXIT_OK


def _cmd_migrate(args, store: Store, clock: Clock, out) -> int:
    """(2026-10-13, SPEC_PROJECTS section 2 + amendments M7/M8/M9) Move the base queue's items into
    their projects' queues, each routed by its EXACT cwd: project_data_dir(base, item.cwd).

    - The base runner.lock is taken for the whole move (a held lock = exit 1), and the base queue
      mutex is held throughout, so no base runner can start an item that is being moved.
    - queue.json is backed up to queue.json.premigrate-<timestamp> first; runs/<id>/ is COPIED.
    - The project queue is written before the item leaves the base queue, and an id already in the
      target is not added twice, so an interrupted or repeated migration moves nothing twice.
    - `--for ROOT` adds `for_root`: how many items would go to that folder (paths are compared
      here in the core, never in the extension).
    """
    base = store.data_dir
    plan: list[tuple[str, str]] = []
    skipped: list[str] = []
    for it in sorted(store.load(), key=queue_order_key):
        cwd = it.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            skipped.append(it["id"])
            log.warning("item %s has no cwd; it stays in the shared queue", it.get("id"))
            continue
        if _nests_base(it, base):
            skipped.append(it["id"])
            log.warning("item %s stays in the shared queue: %s", it.get("id"), NESTED_DATA_DIR.format(cwd, base))
            continue
        plan.append((it["id"], cwd))
    projects: dict[str, int] = {}
    for _id, cwd in plan:
        key = project_root(cwd)
        projects[key] = projects.get(key, 0) + 1
    summary: dict = {"moved": 0, "would_move": len(plan), "projects": projects, "skipped": skipped,
                     "backup": None, "dry_run": bool(args.dry_run)}
    if args.for_root:
        want = project_key(args.for_root)
        summary["for_root"] = sum(1 for _id, cwd in plan if project_key(cwd) == want)
    runner = store.runner_alive()
    summary["runner_alive"] = runner is not None
    if args.dry_run or not plan:
        _migrate_report(args, summary, out)
        return EXIT_OK
    try:
        store.acquire_lock(clock.now())
    except LockHeld as exc:
        log.error("migrate-global: %s; stop the shared runner first (`autopilot stop`)", exc)
        raise _Abort() from None
    try:
        _migrate_locked(store, base, clock, summary)
    finally:
        store.release_lock()
    _migrate_report(args, summary, out)
    return EXIT_OK


def _nests_base(it: dict, base: Path) -> bool:
    """(2026-10-27, S8 fix r3, SEC-L24) migrate-global: `it` may still run and its folder and the base data dir
    nest (queued before `add` refused it). It stays in the shared queue, where `run` refuses it at start: a live
    project runner would run it unchecked (every project dir lies in `base`)."""
    cwd = it.get("cwd")
    return (it.get("status") in NESTED_CHECK_STATUSES and isinstance(cwd, str) and bool(cwd.strip())
            and nested_dirs(cwd, base))


def _migrate_locked(store: Store, base: Path, clock: Clock, summary: dict) -> None:
    stamp = clock.now().strftime("%Y%m%dT%H%M%S%fZ")
    backup = base / f"queue.json.premigrate-{stamp}"
    with store.transaction(backup=True) as items:
        shutil.copy2(store.queue_path, backup)  # global rule: back up before anything is removed
        summary["backup"] = str(backup)
        log.info("migrate-global: backed up %s to %s", store.queue_path.name, backup.name)
        for it in sorted(list(items), key=queue_order_key):
            cwd = it.get("cwd")
            if not isinstance(cwd, str) or not cwd.strip() or _nests_base(it, base):
                continue
            pdir = project_data_dir(base, cwd, clock.now())
            pstore = Store(pdir)
            with pstore.transaction() as target:
                if any(t.get("id") == it["id"] for t in target):
                    log.info("migrate-global: item %s already in %s; not added twice", it["id"], pdir.name)
                else:
                    moved = dict(it)
                    moved["seq"] = next_seq(target)
                    if moved.get("status") == "running":
                        if is_goal(moved):
                            moved["goal_continue"] = False
                        transition(moved, "pending", "migrate-global: was running, no runner alive; "
                                                     "session kept")
                    target.append(moved)
            src_runs = store.run_dir(it["id"])
            if src_runs.is_dir():
                shutil.copytree(src_runs, pstore.run_dir(it["id"]), dirs_exist_ok=True)
            items.remove(it)
            summary["moved"] += 1
            log.info("migrate-global: item %s -> %s (%s)", it["id"], pdir, cwd)


def _migrate_report(args, summary: dict, out) -> None:
    if args.json:
        out(_json(summary))
        return
    if summary["dry_run"]:
        out(f"would move {summary['would_move']} item(s)")
    else:
        out(f"moved {summary['moved']} item(s)")
    for root, count in summary["projects"].items():
        out(f"  {count} -> {root}")
    if summary["backup"]:
        out(f"backup: {summary['backup']}")
    if summary["skipped"]:
        out(f"left in the shared queue (no cwd): {', '.join(summary['skipped'])}")


def _setting(arg: str | None, env_name: str, default: str) -> str | None:
    """(2026-10-17, 0.4.1 R1) `run --x` > the env var > the default; an empty value = do not pass."""
    if arg is not None:
        value = arg
    elif env_name in os.environ:
        value = os.environ[env_name]
    else:
        value = default
    return value.strip() or None


def _provider_selection(args):
    """(2026-10-20, 0.4.7 providers) The provider this run uses, with its key already in os.environ
    (runner.child_env copies it into the claude.exe child). The default provider `claude` changes
    nothing at all, so a subscription run is exactly as it was.

    Nothing here logs or prints a key: only variable names and the endpoint.
    """
    path, values, candidates = _load_keys(args)
    name = providers.select_name(getattr(args, "provider", None), values)
    try:
        sel = providers.resolve(name, values, args.model)
    except providers.ProviderError as exc:
        raise UsageError(f"{exc} [{_env_file_line(path, candidates)}]") from None
    if sel.is_default:
        log.debug("provider %s (subscription, no env change); %s", sel.provider_name,
                  _env_file_line(path, candidates))
        return sel
    was_set, removed = providers.apply(sel)
    log.info("provider %s: model=%s endpoint=%s key from %s (%s); child env sets %s, removes %s",
             # (2026-10-21, 0.4.4 tester fixes LOW-4) no user:pass@ or ?query in the log
             sel.provider_name, sel.model, providers.redact_url(sel.base_url) or "(provider default)",
             sel.key_var or "(no key needed)", _env_file_line(path, candidates),
             ", ".join(was_set) or "-", ", ".join(removed) or "-")
    log.warning("provider %s: claude.exe prices cost_usd and --max-budget-usd at Anthropic rates, so "
                "the recorded cost of a %s run is an estimate at the wrong rates, not a bill",
                sel.provider_name, sel.provider_name)
    return sel


def _provider_model(args, sel) -> str | None:
    """(2026-10-20, 0.4.7 providers) The model every attempt gets.

    For the default provider this is 0.4.1 behaviour: `--model` > $CLAUDE_AUTOPILOT_MODEL > the
    default. A chosen provider IGNORES $CLAUDE_AUTOPILOT_MODEL (logged): that variable usually holds
    an Anthropic id, and sending `claude-opus-5-5` to DeepSeek fails every attempt. Its own model
    comes from `--model` or `<PREFIX>_MODEL` in the .env, both of which resolve() already applied.
    """
    if sel.is_default or args.model is not None:
        model = _setting(args.model, ENV_MODEL, sel.model)
    else:
        model = sel.model or None
        if os.environ.get(ENV_MODEL):
            log.warning("provider %s: ignoring $%s=%s and using %s; set <PROVIDER>_MODEL in providers.env "
                        "or pass `run --model` to change it", sel.provider_name, ENV_MODEL,
                        os.environ[ENV_MODEL], model)
    known = providers.provider(sel.provider_name)
    serves_claude = any(m.startswith("claude-") for m in known.models)
    if model and not sel.is_default and not serves_claude and model.startswith("claude-"):
        log.warning("provider %s: model %s is an Anthropic model id and %s is not Anthropic; unless "
                    "it is a gateway that proxies Anthropic, every attempt will be rejected",
                    sel.provider_name, model, sel.provider_name)
    return model


HOLD_UNTIL_MAX = timedelta(days=8)  # (2026-10-26, usage forecast) the 7-day window's reset + a margin
HOLD_REFUSAL = ("queue is on hold (since {0}): not starting a runner. `autopilot hold off` releases the "
                "hold; `run --ignore-hold` starts one anyway")


def _cmd_run(args, store: Store, claude_cmd, clock: Clock, out) -> int:
    # (2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 2) the real guard for every caller that
    # does not know the flag (a script, an extension window whose JS predates it): refused before any
    # provider key is read or any item is touched. A dry run executes nothing and is allowed.
    held = store.read_hold(clock.now())
    # (2026-10-26, usage forecast) a timed hold is not refused: the runner waits until it ends
    if held is not None and not held.get("until") and not args.ignore_hold and not args.dry_run:
        log.error(HOLD_REFUSAL.format(held.get("since") or "?"))
        return EXIT_HELD
    # (2026-10-27, SEC-L24) a queued item whose folder and the data dir nest is refused before anything runs
    # (a dry run executes nothing: it only says so)
    refusals = _run_folder_checks(args, store)
    for text in refusals:
        (log.warning if args.dry_run else log.error)("run: %s", text)
    if refusals and not args.dry_run:
        return EXIT_USAGE
    sel = _provider_selection(args)
    # (2026-10-20, 0.4.7 providers) --effort is an Anthropic-model feature; a third-party provider
    # gets it only when the owner asked for it explicitly (flag or env var).
    effort = _setting(args.effort, ENV_EFFORT, DEFAULT_EFFORT if sel.supports_effort else "")
    if effort and effort not in EFFORTS:
        raise UsageError(f"{ENV_EFFORT}={effort!r}: must be one of {', '.join(EFFORTS)} (or empty)")
    budget = DEFAULT_MAX_BUDGET_USD if args.max_budget_usd is None else args.max_budget_usd
    opts = RunOptions(
        forever=args.forever, start_at=args.start_at, max_turns=args.max_turns,
        item_timeout_s=args.item_timeout * 60.0, max_limit_waits=args.max_limit_waits,
        poll_min=args.poll_min, dry_run=args.dry_run, extra_args=list(args.claude_arg),
        goal_max_turns=args.goal_max_turns, model=_provider_model(args, sel),
        effort=effort, max_budget_usd=budget or None,
        allow_file=allow_file(args.project_dir),  # (2026-10-20, 0.4.4 logfix A)
        provider=sel.provider_name,  # the NAME only: RunOptions is logged, a key never is
        # (2026-10-21, add panel) for items with their own provider: what the run set (names), and
        # providers.env read again per such attempt (the values never enter RunOptions)
        provider_env=tuple(sorted(sel.env)), key_loader=_key_loader(args),
        ignore_hold=args.ignore_hold,  # (2026-10-21, v049 dontask_hold)
        on_limit=args.on_limit,  # (2026-10-26, limit_continue)
        usage_dir=args.base_dir,  # (2026-10-26, usage forecast) the account's samples: one file, the base dir
        # (2026-10-27, SEC-M6 review) its path only: every key in it is redacted from the run logs
        env_file=Path(args.env_file).resolve() if getattr(args, "env_file", None) else None,
    )
    check_extra_args(opts.extra_args)  # refuse up front instead of failing every item (fix B4)
    resolver = None
    if claude_cmd is None:
        # (2026-10-19, 0.4.1) re-resolved before each attempt if the file vanishes (VS Code update)
        resolver = ClaudeResolver(getattr(args, "claude", None))
        claude_cmd = resolver.cmd
    else:
        for part in claude_cmd:
            check_not_shim(part)
    log.info("run: version=%s data_dir=%s claude=%s pid=%s opts=%s", describe_version(__version__), store.data_dir,
             claude_cmd, os.getpid(), opts)
    if opts.dry_run:
        # (2026-10-27, S8 fix r4, SEC-L17) each item's argv holds its prompt (a subtask's is model-written)
        return dry_run(store, opts, claude_cmd, out=lambda line: out(_escape_controls(line)))
    # (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 1.2) the live number: written with the flag, deleted without
    # it (no stale number from an old session); never while another runner owns this queue (it keeps its own)
    if store.runner_alive() is None:
        if args.tasks_at_once is not None:
            store.write_tasks_at_once(args.tasks_at_once, clock.now())
            log.info("run: tasks at once %d", args.tasks_at_once)
        elif store.clear_tasks_at_once():
            log.info("run: tasks at once back to 1 (no --tasks-at-once)")
    rc = run_loop(store, opts, claude_cmd, clock, claude_resolver=resolver)
    log.info("run finished with exit code %s", rc)
    return rc


# ---- (2026-10-27, "Tasks at once" U3, SPEC_V05_PARALLEL 1.2, 1.9, 3.6-3.10, 5, U3) -----------------------------
def _add_after(args, store: Store, items: list[dict]) -> str | None:
    """`add --after ID` (a prefix; same queue, else KeyError = exit 1) or `--after-previous` (the last item
    in run order that is not done/cancelled now: order.resolve_previous; none -> None)."""
    if getattr(args, "after", None):
        return store.find(items, args.after)["id"]
    if getattr(args, "after_previous", False):
        return order.resolve_previous(items)
    return None


def _edit_after(args, store: Store, items: list[dict], item: dict) -> str | None:
    """`edit`: the new `after` of `item`. --after-previous = the last unfinished item BEFORE it in run order."""
    if args.no_after:
        return None
    if args.after:
        return store.find(items, args.after)["id"]
    by_id = {i["id"]: i for i in items}
    mine = run_order_key(item, by_id)
    live = [i for i in items if i["id"] != item["id"] and i.get("status") not in ("done", "cancelled")
            and run_order_key(i, by_id) < mine]
    return max(live, key=lambda i: run_order_key(i, by_id))["id"] if live else None


def _live_parallel(store: Store) -> dict | None:
    """The lane's parallel.json while the runner that wrote it is alive (store.lock_identity on its pid and
    create_time), else None (SPEC_V05_PARALLEL 1.9: readers trust it only then)."""
    data = store.read_parallel()
    if not isinstance(data, dict):
        return None
    try:
        alive, _why = lock_identity({"pid": data.get("pid"), "create_time": data.get("create_time")})
    except (OSError, ValueError, TypeError):
        return None
    return data if alive else None


def _slots(pj: dict | None) -> list[dict]:
    raw = (pj or {}).get("slots")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _parallel_lane(store: Store, items: list[dict], pj: dict | None) -> bool:
    """True when the lane runs (or will run) the parallel way: a live parallel runner, tasks at once > 1, or
    any item with a copy record or merge state. Otherwise the lane is serial and `list --json` stays exactly
    queue.json (N=1 identical: no phase/waits keys)."""
    if pj is not None or (store.read_tasks_at_once() or 1) > 1:
        return True
    return any(it.get("worktree") or it.get("merge") for it in items)


def _pick_context(store: Store, items: list[dict], pj: dict | None) -> order.PickContext:
    """What the scheduler would know, from parallel.json (a live runner) or the queue alone."""
    merging = {i["id"] for i in items if (i.get("merge") or {}).get("state") in worktree.MERGE_ACTIVE}
    gate = "hold" if store.read_hold() is not None else None
    if pj is None:
        return order.PickContext(running={i["id"] for i in items if i.get("status") == "running"},
                                 merging=merging, n_eff=store.read_tasks_at_once() or 1, gate=gate)
    why = pj.get("why") if isinstance(pj.get("why"), dict) else {}
    if why.get("code") in order.GATES:
        gate = why["code"]
    n_eff = pj.get("n_eff") if isinstance(pj.get("n_eff"), int) and not isinstance(pj.get("n_eff"), bool) else 1
    merging |= {m.get("item") for m in pj.get("merging") or [] if isinstance(m, dict) and m.get("item")}
    return order.PickContext(running={s.get("item") for s in _slots(pj) if s.get("item")}, merging=merging,
                             alone_running=any(s.get("mode") == "alone" for s in _slots(pj)),
                             n_eff=max(1, n_eff), copy_ok=pj.get("mode") != "in-place", gate=gate)


def _phase(item: dict, pj: dict | None) -> str:
    """(SPEC_V05_PARALLEL U3, 8.10) queued | preparing | working | merging | merge-waiting | needs-you | <status>.
    needs-you = blocked on a merge question (any other blocked item keeps "blocked")."""
    status = item.get("status")
    state = (item.get("merge") or {}).get("state")
    if status == "pending":
        return "queued"
    if status == "running":
        if state in ("pending", "merging"):
            return "merging"
        if state == "waiting":
            return "merge-waiting"
        slot = next((s for s in _slots(pj) if s.get("item") == item.get("id")), None)
        return "preparing" if slot and slot.get("phase") == "prepare" else "working"
    q = item.get("blocked_question")
    if status == "blocked" and isinstance(q, dict) and q.get("kind") == "merge":
        return "needs-you"
    return str(status)


def _json_items(store: Store, shown: list[dict], every: list[dict]) -> list[dict]:
    """`list --json` / `lanes --items` rows: with_defaults, plus "phase" and "waits" (order.start_block over
    EVERY item of the lane; null unless the item is pending) when the lane is parallel."""
    rows = [with_defaults(i) for i in shown]
    pj = _live_parallel(store)
    if not _parallel_lane(store, every, pj):
        return rows
    ctx = _pick_context(store, every, pj)
    for row, it in zip(rows, shown):
        row["phase"] = _phase(it, pj)
        row["waits"] = order.start_block(it, every, ctx) if it.get("status") == "pending" else None
    return rows


def _cmd_tasks_at_once(args, store: Store, clock: Clock, out) -> int:
    """`tasks-at-once N`: written at once (n=1 too); a live runner applies it at its next pick. Without a runner
    the next `run` sets its own number: `run --tasks-at-once N` writes N, a plain `run` deletes the file (1), so
    the JSON's "next-run" means "the next run's own --tasks-at-once" (the VS Code extension passes its setting)."""
    if not store.data_dir.is_dir():
        log.error("no queue in %s yet; add a task first", store.data_dir)
        return EXIT_FAIL
    n = store.write_tasks_at_once(args.n, clock.now())
    alive = store.runner_alive() is not None
    applies = "next-pick" if alive else "next-run"
    # (2026-10-29, integrate round 9; D5 tester residual) no runner: "from the next run" was not true, because a
    # plain `run` deletes tasks_at_once.json (spec 1.2); the text says what the next run really uses
    log.info("queue %s: tasks at once %d (%s)", args.lane, n,
             "applies at the runner's next pick" if alive else "no runner: the next run uses its own --tasks-at-once")
    if args.json:
        out(_json({"lane": args.lane, "tasks_at_once": n, "runner_alive": alive, "applies": applies}))
    elif alive:
        out(f"tasks at once: {n} (the runner applies it at its next pick)")
    else:
        out(f"tasks at once: {n} (no runner now: the next run uses its own number: `run --tasks-at-once {n}`; "
            "a plain `run` sets it back to 1; the VS Code extension passes its Tasks At Once setting)")
    return EXIT_OK


def _merge_of(store: Store, item_id: str) -> dict | None:
    with contextlib.suppress(KeyError):
        return store.find(store.load(), item_id).get("merge")
    return None


def _merge_run(args, store: Store, clock: Clock, item: dict, action: str, gen: int | None,
               note: str | None = None, *, profile: str | None = None, by: str | None = None) -> dict:
    """(SPEC_V05_PARALLEL 3.6) Run one merge action through worktree.merge_action; `let-claude` then queues its
    reply_text as a normal reply (_queue_reply), `retry` merges at once (a lock wait that times out leaves it
    pending), `discard` checkpoints the copy first. {"id", "action", "ok", "state", "code", "merge", ...}."""
    item_id = item["id"]
    if gen is None and action in worktree.QUESTION_ACTIONS:   # a hand-typed command: the current question
        gen = (item.get("merge") or {}).get("gen")
    shadow = worktree.shadow_for(store)
    if shadow is None:
        res = {"ok": False, "state": None, "code": "no-copy", "reply_text": None,
               "detail": "this queue is not a project's queue, so it has no private copies"}
    else:
        if action == "let-claude":
            # (2026-10-27, U3 fix r1) the reply's refusals run BEFORE merge_action writes markers into the
            # copy and bumps the gen: a refused reply must not leave the item "fixing" with a stale question
            _reply_refusals(item, args, store, None)
        res = _checkpoint_before_discard(shadow, item, gen) if action == "discard" else None
        if res is None:
            res = worktree.merge_action(store, shadow, item_id, action, gen, note=note)
    reply = None
    # (2026-10-27, U3 fix r1; 2026-10-29 integrate round 8, INTEGRATION [F2]) the retry merges at once, even with
    # a live runner; if its lock wait times out the merge stays pending and the live runner's next coordinator
    # tick enqueues it (scheduler.py: every tick enqueues each pending/waiting/merging merge, spec 1.3).
    # merge() is gen-checked and serialised on the ProjectLock, so a duplicate job of the merger with the same
    # gen ends "stale".
    if res.get("ok") and action == "retry":
        new_gen = (_merge_of(store, item_id) or {}).get("gen")
        try:
            code = shadow.merge(store, item_id, new_gen)
        except worktree.WorktreeError as exc:
            log.warning("item %s: merge not run now (%s); the runner's merger or the next run merges it",
                        item_id, exc)
            code = "pending"
        res = dict(res, code=code, state=(_merge_of(store, item_id) or {}).get("state"))
    if res.get("ok") and action == "let-claude":
        reply = _queue_reply(store, item_id, res.get("reply_text") or "", clock, args=args, profile=profile,
                             by=by, merge_reply=True)
    if res.get("ok"):
        log.info("item %s: merge %s -> %s (%s)", item_id, action, res.get("state"), res.get("code"))
    else:
        log.error("item %s: merge %s refused: %s%s", item_id, action, res.get("code"),
                  f" ({res['detail']})" if res.get("detail") else "")
    result = {"id": item_id, "action": action, "ok": bool(res.get("ok")), "state": res.get("state"),
              "code": res.get("code"), "merge": _merge_of(store, item_id)}
    # (2026-10-29, integrate round 8, INTEGRATION [F1 -> cli]) + `later`: a refused undo's later merges
    # [{id, at, paths}] (worktree.merge_action), newest first
    for key in ("detail", "paths", "patch", "removed", "kept", "later"):
        if key in res:
            result[key] = res[key]
    if reply is not None:
        result["reply"] = reply
    return result


def _checkpoint_before_discard(shadow: worktree.Shadow, item: dict, gen: int | None) -> dict | None:
    """(2026-10-27, U3 fix r1, SPEC_V05_PARALLEL 3.6 + 8.6) `discard` saves its patch from the copy's last checkpoint:
    take a fresh one first, so what changed in the copy since the merge (the card's "Open copy") is in it.
    None = go on; else the refusal (nothing discarded). A nested-repo copy is moved aside by merge_action
    whatever its patch holds, so its failed checkpoint does not stop the discard."""
    m = item.get("merge") or {}
    if gen != m.get("gen") or m.get("state") not in ("conflict", "waiting"):
        return None   # merge_action refuses it without touching the copy
    w = shadow.worktree_path(item)
    if not w.is_dir() or worktree.is_reparse_point(w):
        return None
    try:
        shadow.checkpoint(item)
    except worktree.WorktreeError as exc:
        if m.get("kind") == "nested-repo":
            return None
        log.warning("item %s: its copy could not be checkpointed (%s); nothing was discarded", item["id"], exc)
        return {"ok": False, "state": m.get("state"), "code": "busy", "reply_text": None,
                "detail": "its copy could not be checkpointed; nothing was discarded"}
    return None


def _cmd_merge(args, store: Store, clock: Clock, out) -> int:
    """`merge ID --<action> [--note] [--gen] [--json]`: 0 = the action ran, 1 = refused, 2 = usage."""
    action = args.merge_action
    if args.note is not None and action != "let-claude":
        raise UsageError("--note goes with --let-claude only")
    by = getattr(args, "by", None)
    if by is not None and action != "let-claude":
        raise UsageError("--by goes with --let-claude only")
    item = store.find(store.load(), args.id)
    res = _merge_run(args, store, clock, item, action, args.gen, args.note, by=by)
    if args.json:
        out(_json(res))
    elif res["ok"]:
        out(f"merge {action} {item['id']}: {res['code']}" + (f" (patch: {res['patch']})" if res.get("patch") else ""))
        note = worktree.not_merged_note({"merge": res.get("merge")})   # (2026-10-29, delta M1)
        if note:
            out(note)
    return EXIT_OK if res["ok"] else EXIT_FAIL


def _label_key(text) -> str:
    text = text.get("label") if isinstance(text, dict) else text
    text = re.sub(r"\(recommended\)", "", str(text or ""), flags=re.I)
    return " ".join(text.replace("…", " ").split()).lower()


def _merge_reply_action(q: dict, text: str) -> str | None:
    """(SPEC_V05_PARALLEL 3.8) The merge action a reply names: a bare action word, or "Option <n>: <label>" (by its
    label, else its number); None = free text."""
    actions = [a for a in (q.get("actions") or []) if a in worktree.MERGE_ACTIONS]
    word = text.strip().lower().lstrip("-")
    if word in MERGE_FLAGS:
        return word
    m = OPTION_REPLY_RE.match(text)
    if m is None:
        return None
    options = list(q.get("options") or [])
    label = _label_key(m.group(2))
    keys = [_label_key(o) for o in options]
    if label and label in keys and keys.index(label) < len(actions):
        return actions[keys.index(label)]
    by_label = {_label_key(v): k for k, v in worktree.ACTION_LABELS.items()}
    if label in by_label:
        return by_label[label]
    n = int(m.group(1))
    idx = next((k for k, o in enumerate(options) if isinstance(o, dict) and o.get("n") == n), n - 1)
    return actions[idx] if 0 <= idx < len(actions) else None


def _reply_merge(args, store: Store, clock: Clock, out, item: dict, q: dict, text: str) -> int:
    """(SPEC_V05_PARALLEL 3.8) A reply to a merge question: let-claude or retry run; take-task, discard, now and undo
    never run through a reply (exit 2, the `merge` command to use); free text = let-claude with it as the note."""
    action = _merge_reply_action(q, text)
    note = None
    if action is None:
        action, note = "let-claude", text
    if action not in REPLY_MERGE_ACTIONS:
        raise UsageError(f"item {item['id']}: {action} is never done through a reply; use "
                         f"`autopilot merge {item['id']} --{action}`")
    if action not in (q.get("actions") or []):
        offered = " or ".join(f"--{a}" for a in (q.get("actions") or [])) or "--discard"
        raise UsageError(f"item {item['id']}: this merge question does not offer {action}; use "
                         f"`autopilot merge {item['id']} {offered}`")
    profile = args.profile if action == "let-claude" else None
    if args.profile and profile is None:
        log.info("item %s: a merge retry runs no attempt; --profile ignored", item["id"])
    if profile:   # refused before the copy is touched, as a plain reply refuses it
        _check_run_choice(item["id"], item.get("provider"), None, profile)
        if not item.get("provider"):
            _check_env_provider(args, profile)
    res = _merge_run(args, store, clock, item, action, q.get("gen"), note, profile=profile,
                     by=getattr(args, "by", None))
    reply = res.get("reply") or {}
    if args.json:
        out(_json({"id": item["id"], "status": reply.get("status", (store.find(store.load(), item["id"])).get("status")),
                   "seq": reply.get("seq", item.get("seq")), "replies": reply.get("replies", len(item.get("replies") or [])),
                   "action": action, "ok": res["ok"], "state": res["state"], "code": res["code"]}))
    elif res["ok"]:
        out(f"reply queued {item['id']}" if action == "let-claude" else f"merge retry {item['id']}: {res['code']}")
    return EXIT_OK if res["ok"] else EXIT_FAIL


def _shadow_copy_items(store: Store) -> tuple[worktree.Shadow | None, list[dict]]:
    shadow = worktree.shadow_for(store)
    return shadow, ([] if shadow is None else [i for i in store.load() if _has_copy(i)])


def _has_copy(item: dict) -> bool:
    rec = item.get("worktree")
    return isinstance(rec, dict) and rec.get("state") not in (None, "removed")


def _save_copy_work(shadow: worktree.Shadow, item: dict, tag: str) -> tuple[bool, str | None]:
    """(SPEC_V05_PARALLEL 3.10) Before a copy may go: checkpoint it and, when it holds work that was never merged,
    save that as D\\merge_backups\\<stamp>-<id8>-<tag>\\task.patch. (ok, patch path): ok False = its work could
    not be saved, so the copy folder must be kept."""
    # (2026-10-27, U3 fix r1) decided by comparing the copy's checkpoint with its base, never by merge.state
    # (Shadow.unmerged): a "merged" left over from an earlier round (a reply re-ran the item in a new copy,
    # spec 4.6) must not let new work go unsaved. Any git error keeps the copy (fail closed).
    w = shadow.worktree_path(item)
    sc_base, sc_ckpt = f"refs/sc/base/{item['id']}", f"refs/sc/ckpt/{item['id']}"
    lost: list[str] = []
    try:
        if w.is_dir() and not worktree.is_reparse_point(w):
            ckpt = shadow.checkpoint(item)
            base = shadow._ref(sc_base) or (item.get("worktree") or {}).get("base")
            if not base:
                raise worktree.WorktreeError("corrupt", f"no base snapshot for {item['id']}")
            lost = shadow.not_merged(item)   # (2026-10-29, delta M1) files no patch can carry: keep the copy
        else:
            ckpt, base = shadow._ref(sc_ckpt), shadow._ref(sc_base)
        differs = bool(base and ckpt and shadow._tree(base) != shadow._tree(ckpt))
    except worktree.WorktreeError as exc:
        log.warning("item %s: its copy could not be checked for unsaved work (%s); the copy is kept",
                    item["id"], exc)
        return False, None
    if lost:
        log.warning("item %s: %s", item["id"], worktree.not_merged_text(lost, len(lost), str(w)))
    if not differs:
        return not lost, None
    try:
        return not lost, str(shadow.save_patch(item, tag))
    except (worktree.WorktreeError, OSError) as exc:
        log.warning("item %s: its changes could not be saved as a patch (%s); the copy is kept", item["id"], exc)
        return False, None


def _cmd_worktrees(args, store: Store, out) -> int:
    """`worktrees [--json]`: the project's private copies (every lane) and leftovers; read-only."""
    shadow = worktree.shadow_for(store)
    if shadow is None:
        res = {"project": None, "shadow": None, "copies": [], "leftovers": []}
    else:
        try:
            worktree.find_git()
            git_ok = True
        except worktree.WorktreeError:
            git_ok = False
        last = (worktree._read_json(shadow.sc_dir / "last_gc.json") or {}).get("at")
        last_gc = iso(datetime.fromtimestamp(last, timezone.utc)) if isinstance(last, (int, float)) else None
        copies = [{"id": it["id"], "lane": lane, "path": (it.get("worktree") or {}).get("path"),
                   "state": (it.get("worktree") or {}).get("state"), "merge_state": (it.get("merge") or {}).get("state")}
                  for lane, _d, items in shadow._lane_items() for it in items if _has_copy(it)]
        res = {"project": str(shadow.root) if shadow.root else None,
               "shadow": {"path": str(shadow.git_dir), "last_gc": last_gc,
                          "ok": git_ok and (not shadow.git_dir.exists() or (shadow.git_dir / "HEAD").is_file())},
               "copies": copies, "leftovers": shadow.leftovers()}
    if args.json:
        out(_json(res))
        return EXIT_OK
    if res["shadow"] is None:
        out("not a project queue: no private copies")
        return EXIT_OK
    out(f"project: {res['project']}")
    out(f"shadow: {res['shadow']['path']} ({'ok' if res['shadow']['ok'] else 'not usable'}; "
        f"last gc {res['shadow']['last_gc'] or '-'})")
    for c in res["copies"]:
        out(f"copy {c['id']} [{c['lane']}] {c['state']} merge={c['merge_state'] or '-'}: {c['path']}")
    for lo in res["leftovers"]:
        out(f"leftover ({lo['reason']}): {lo['path']}")
    if not res["copies"] and not res["leftovers"]:
        out("no copies")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

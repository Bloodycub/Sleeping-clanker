"""One attempt of one queue item against `claude -p` (2026-10-13, spec section 7).

build_argv() is the ONLY place a claude argv is assembled. It refuses permission-bypass options from
any source and permission/tool-surface options in user extras; the prompt text is not scanned. The one
exception (2026-10-21, owner) is the `full` profile's own --dangerously-skip-permissions token.

Event shapes verified against claude.exe 2.1.278 (2026-10-13 probes):
  {"type":"system","subtype":"init","session_id":...,"permissionMode":...}
  {"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning"|"rejected"|...,
     "resetsAt":<epoch s>,"rateLimitType":...}}
  {"type":"system","subtype":"permission_denied","tool_name":...,"decision_reason_type":...}
  {"type":"result","subtype":"success"|"error_*","is_error":bool,"result":str,
     "total_cost_usd":float,"num_turns":int,"session_id":...,"permission_denials":[...],
     "terminal_reason":"completed"|...}
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .claude_path import GIT_BASH_ENV, find_git_bash
from .clock import Clock
from .denials import annotate, is_hook_text, tool_result_text
from .goals import (BG_REDO, BG_REDO_PROMPT, FRESH_ROUND, first_round_prompt, fresh_resume_prompt, fresh_round_prompt,
                    goal_resume_prompt, is_goal, needs_restate_file, next_round_prompt, reply_prompt,
                    task_resume_prompt)
from .limits import compute_reset, text_says_limit
from .log import get_logger
from .profiles import (AGENT_SPAWN_TOOLS, AGENTS_UNLIMITED, FULL, FULL_BYPASS_FLAG,
                       NO_SHELL_PROFILES, normalise_agents, profile_args)
from .profiles import WORKFLOW_TOOL, ultracode_refusal, with_ultracode  # (2026-10-27, modes fix HIGH)
from .profiles import with_data_dir_deny  # (2026-10-27, SEC-M7/L24)
from .profiles import user_dirs_deny, with_user_dirs_deny  # (2026-10-28, SEC-H2)
from .providers import key_vars_in, scrub_child_env
from .providers import ENV_FILE_NAME, env_file_candidates, is_secret_env_name, load_env_file  # (2026-10-27, SEC-M6)
from .providers import KEY_VARS_MARKER  # (2026-10-27, SEC-M6 review 2)

log = get_logger(__name__)

PROMPT_INLINE_MAX = 8000
RESUME_PROMPT = (
    "Continue the task where you left off. If it is already complete, reply with a short summary "
    "of the outcome."
)
FORBIDDEN_FLAGS = ("--dangerously-skip-permissions", "--allow-dangerously-skip-permissions")
FORBIDDEN_VALUES = ("bypasspermissions",)
# (2026-10-13, fix B4 v2) User extras (--claude-arg) are an ALLOWLIST, not a denylist: a denylist
# kept missing flags that change permissions, tools, hooks or the session (--agents, --bare, -w,
# --continue, --input-format, ...). Every allowed option takes exactly one value.
ALLOWED_EXTRA_FLAGS = (
    "--model", "--fallback-model", "--effort", "--max-budget-usd", "--append-system-prompt",
    "--name", "-n",
)

# (2026-10-17, 0.4.1 R1/R7, DECISIONS.md 0.4.1) Defaults passed on every attempt unless the run's
# --claude-arg extras already set the same flag (extras win). The model was never set before, so runs
# used the settings alias ("opus" -> claude-opus-5 on CLI 2.1.278), 24-29% dearer than Opus 5.5.
# `run --model/--effort/--max-budget-usd` and the env vars below override them; an empty value
# (or a budget of 0) passes nothing. All three flags verified in `claude --help` (2.1.278).
DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "high"
EFFORTS = ("low", "medium", "high", "xhigh", "max")  # `claude --help`: --effort <level>
DEFAULT_MAX_BUDGET_USD = 10.0
ENV_MODEL = "CLAUDE_AUTOPILOT_MODEL"
ENV_EFFORT = "CLAUDE_AUTOPILOT_EFFORT"
# Result subtypes, from the result schema inside claude.exe 2.1.278/2.1.296: error_during_execution,
# error_max_turns, error_max_budget_usd, error_max_structured_output_retries.
SUBTYPE_MAX_TURNS = "error_max_turns"
SUBTYPE_BUDGET_PREFIX = "error_max_budget"

DONE, LIMIT, FAILED = "DONE", "LIMIT", "FAILED"
# (2026-10-13, SPEC_GOALS 3.3) The item was paused or cancelled while its child ran: the tree was
# killed and the loop leaves the status exactly as the user set it (not a failure, session kept).
INTERRUPTED = "INTERRUPTED"


class ForbiddenFlagError(ValueError):
    pass


# (2026-10-27, SEC-H1) claude.exe -p skips the workspace-trust dialog and, without this flag, loads the task
# folder's PROJECT and LOCAL settings (.claude/settings.json, .claude/settings.local.json): a repo's hooks ran
# and its env block (ANTHROPIC_BASE_URL) redirected the session under every profile (security_final probe
# verify_exec_injection_settings_probe, claude.exe 2.1.310). `claude --help`: "--setting-sources <sources>
# Comma-separated list of setting sources to load (user, project, local)". build_argv adds it to EVERY argv,
# `full` included; check_options() refuses it from anywhere else.
SETTING_SOURCES_FLAG = "--setting-sources"
SETTING_SOURCES_ARGS = (SETTING_SOURCES_FLAG, "user")


class StopRequested(BaseException):
    """(2026-10-13, SPEC_VSCODE section 2) A graceful stop was requested via stop.request.

    BaseException on purpose, like KeyboardInterrupt: the loop's per-item `except Exception` must not
    turn a stop into a failed item.
    """


def check_options(tokens: list[str]) -> None:
    """Raise if any OPTION token would bypass permission checks.

    (2026-10-13, fix B1) Only option tokens are inspected (profile args, extras, --resume value);
    the positional prompt is free text and may legitimately mention e.g. bypassPermissions.
    """
    for tok in tokens:
        low = str(tok).strip().lower()
        flag = low.split("=", 1)[0]
        if flag in FORBIDDEN_FLAGS:
            raise ForbiddenFlagError(f"refusing forbidden flag {tok!r}")
        if flag == SETTING_SOURCES_FLAG:   # (2026-10-27, SEC-H1) build_argv adds the only one
            raise ForbiddenFlagError(f"refusing {tok!r}: {SETTING_SOURCES_FLAG} is set by build_argv only")
        if any(v in low for v in FORBIDDEN_VALUES):
            raise ForbiddenFlagError(f"refusing forbidden permission value in {tok!r}")


def check_extra_args(extras: list[str]) -> None:
    """Accept only ALLOWED_EXTRA_FLAGS as `--flag value` or `--flag=value`; refuse anything else."""
    allowed = ", ".join(ALLOWED_EXTRA_FLAGS)

    def refuse(why: str) -> ForbiddenFlagError:
        return ForbiddenFlagError(f"refusing --claude-arg {why}; allowed options: {allowed}")

    toks = [str(t) for t in extras]
    i = 0
    while i < len(toks):
        tok = toks[i]
        flag, eq, _value = tok.partition("=")
        if flag not in ALLOWED_EXTRA_FLAGS:
            raise refuse(f"{tok!r}")
        if not eq:
            # Space form: the next token is the value; a dash-led value is ambiguous to the CLI
            # parser (could be read as an option), so it must use the --flag=value form.
            if i + 1 >= len(toks) or toks[i + 1].startswith("-"):
                raise refuse(f"{tok!r} without a value (use {flag}=VALUE for values starting with '-')")
            i += 1
        i += 1
    check_options(toks)  # defence in depth: bypass guard also scans the allowed values


SYSTEM_PROMPT_FLAG = "--append-system-prompt"
# (2026-10-26, modes) ONE `--settings <json>` per attempt, built only here (`claude --help` 2.1.310:
# "--settings <file-or-json>"); it is not in ALLOWED_EXTRA_FLAGS, so no extra can add a second one.
SETTINGS_FLAG = "--settings"


def item_settings(item: dict) -> dict:
    """(2026-10-26, modes) The session settings of an attempt of `item`, merged into one object:
    {"ultracode": true} while its `ultracode` is on (claude-code-settings.schema.json 2.1.310: "Enable
    ultracode for the session ... typically provided via --settings"). {} = no --settings at all."""
    out: dict = {}
    if item.get("ultracode") is True:
        out["ultracode"] = True
    return out


def default_args(model: str | None, effort: str | None, max_budget_usd: float | None) -> list[str]:
    """(2026-10-17, 0.4.1 R1/R7) `--model X --effort Y --max-budget-usd Z`, each only when set."""
    out: list[str] = []
    if model:
        out += ["--model", model]
    if effort:
        out += ["--effort", effort]
    if max_budget_usd:
        out += ["--max-budget-usd", _num(max_budget_usd)]
    return out


def _num(value: float) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else str(f)


def option_value(tokens: list[str], flag: str) -> str | None:
    """The value of `flag` in a check_extra_args()-clean token list (`--f v` or `--f=v`); last wins."""
    found = None
    i = 0
    while i < len(tokens):
        name, eq, value = str(tokens[i]).partition("=")
        if name == flag:
            if eq:
                found = value
            elif i + 1 < len(tokens):
                found = str(tokens[i + 1])
                i += 1
        i += 1
    return found


def without_flag(tokens: list[str], flag: str) -> list[str]:
    """(2026-10-21, task model) `tokens` without each `flag value` / `flag=value`; the tokens must be
    check_extra_args()-clean (a space-form value never starts with '-')."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        name, eq, _value = str(tokens[i]).partition("=")
        if name == flag:
            i += 1 if eq else 2
            continue
        out.append(tokens[i])
        i += 1
    return out


# (2026-10-21, task model) a task's own model id: letters, digits and . _ : / [ ] - (e.g.
# `claude-sonnet-5-5`, `opus`, `claude-opus-5-5[1m]`, `deepseek/v3`), never led by '-'
TASK_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,99}")


def valid_task_model(value: str) -> bool:
    return bool(TASK_MODEL_RE.fullmatch(value))


def _without_overridden(defaults: list[str], extras: list[str]) -> list[str]:
    """Drop each `--flag value` pair of `defaults` whose flag the user's extras already set."""
    out: list[str] = []
    for i in range(0, len(defaults) - 1, 2):
        if option_value(extras, defaults[i]) is None:
            out += defaults[i:i + 2]
    return out


def _merge_system_prompt(options: list[str], extras: list[str]) -> list[str]:
    """(2026-10-15, 0.3.1) A profile may carry its own --append-system-prompt (shell-read). The argv
    then holds ONE such option: the profile's text, a blank line, then each user extra's text, so
    a user's prompt can neither replace nor drop the profile's. Returns the extras to append; with
    no profile prompt the extras pass unchanged. `extras` must already be check_extra_args()-clean
    (so a space-form value never starts with '-' and the scan below is unambiguous)."""
    if SYSTEM_PROMPT_FLAG not in options:
        return extras
    rest: list[str] = []
    texts: list[str] = []
    i = 0
    while i < len(extras):
        flag, eq, value = extras[i].partition("=")
        if flag == SYSTEM_PROMPT_FLAG:
            texts.append(value if eq else extras[i + 1])
            i += 1 if eq else 2
            continue
        rest.append(extras[i])
        i += 1
    if texts:
        at = options.index(SYSTEM_PROMPT_FLAG) + 1
        options[at] = "\n\n".join([options[at], *texts])
    return rest


def with_system_note(extras: list[str], note: str | None) -> list[str]:
    """(2026-10-27, tasks at once, SPEC_V05_PARALLEL 4.3) `extras` with the copy's system note in their ONE
    --append-system-prompt value: appended after a blank line to the user's own value when there is one,
    else a new `--append-system-prompt=<note>` (never a second flag; build_argv then merges it into the
    profile's/CLAUDE.md value). `extras` are check_extra_args-clean: `--flag value` or `--flag=value`.
    (2026-10-29, C-L1) moved here from loop.py: build_argv joins the note itself, after the extras were checked."""
    if not note:
        return list(extras)
    out = [str(t) for t in extras]
    i = 0
    while i < len(out):
        flag, eq, value = out[i].partition("=")
        if flag == SYSTEM_PROMPT_FLAG:
            if eq:
                out[i] = f"{flag}={value}\n\n{note}"
            else:
                out[i + 1] = f"{out[i + 1]}\n\n{note}"
            return out
        i += 1 if eq else 2
    return [*out, f"{SYSTEM_PROMPT_FLAG}={note}"]


# (2026-10-27, SEC-H1) `--setting-sources user` also stops claude.exe loading the folder's CLAUDE.md (probe
# verify_exec_injection_claudemd_probe: default=the token, user only=NONE). build_argv re-supplies
# <cwd>\CLAUDE.md inside the ONE --append-system-prompt value, labelled as repo content. Only that file:
# CLAUDE.local.md, .claude/CLAUDE.md, parent folders' CLAUDE.md and @imports are not read.
CLAUDE_MD = "CLAUDE.md"
CLAUDE_MD_MAX_BYTES = 40 * 1024
PROJECT_INSTRUCTIONS_LABEL = "Project instructions from CLAUDE.md (repo content):"
PROJECT_INSTRUCTIONS_END = "(end of CLAUDE.md)"
PROJECT_INSTRUCTIONS_CUT = "[CLAUDE.md cut here: too long for the command line]"
# Windows CreateProcess takes at most 32767 UTF-16 units for the whole command line: a CLAUDE.md that would
# push the argv over this many characters (cmdline_chars) is cut to fit, or left out.
ARGV_CHARS_MAX = 30000
_ARGV_SLACK = 200


def cmdline_chars(argv: list[str]) -> int:
    """(2026-10-27, SEC-H1) An upper bound of the Windows command-line length of `argv` in UTF-16 units (what
    CreateProcess counts; (2026-10-27, SEC-H1 review 3) a character outside the BMP is two): each argument
    plus a space and two quotes, plus one per double quote or backslash that quoting may escape."""
    return sum(len(a.encode("utf-16-le", "surrogatepass")) // 2 + 3 + a.count('"') + a.count("\\")
               for a in map(str, argv))


def project_instructions(cwd: str | os.PathLike) -> str | None:
    """(2026-10-27, SEC-H1) The text of <cwd>\\CLAUDE.md for build_argv: UTF-8 (strict; a BOM dropped), at
    most CLAUDE_MD_MAX_BYTES (a longer file is cut at a character boundary). None when there is none, it is
    empty, it resolves outside the folder (a link) or it is not UTF-8 (logged, never its text)."""
    root = Path(cwd)
    path = root / CLAUDE_MD
    try:
        if not path.is_file():
            return None
        if not path.resolve().is_relative_to(root.resolve()):
            log.warning("%s resolves outside the task folder: not passed to the task", path)
            return None
        with open(path, "rb") as fh:
            raw = fh.read(CLAUDE_MD_MAX_BYTES + 1)
    except OSError as exc:
        log.warning("%s unreadable (%s): not passed to the task", path, exc)
        return None
    cut = len(raw) > CLAUDE_MD_MAX_BYTES
    raw = raw[:CLAUDE_MD_MAX_BYTES]
    text = None
    for drop in range(4 if cut else 1):   # a cut may split one UTF-8 character (at most 3 bytes)
        try:
            text = raw[:len(raw) - drop].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        log.warning("%s is not UTF-8: not passed to the task", path)
        return None
    text = text.removeprefix("﻿")
    if "\x00" in text:   # (2026-10-27, SEC-H1 review) an argv element with a NUL makes Popen raise ValueError
        log.warning("%s holds NUL bytes: dropped", path)
        text = text.replace("\x00", "")
    if not text.strip():
        return None
    if cut:
        log.warning("%s is over %d bytes: only its first %d bytes are passed to the task", path,
                    CLAUDE_MD_MAX_BYTES, CLAUDE_MD_MAX_BYTES)
    return text


def _instructions_block(text: str, cut: bool) -> str:
    return "\n".join([PROJECT_INSTRUCTIONS_LABEL, text, *([PROJECT_INSTRUCTIONS_CUT] if cut else []),
                      PROJECT_INSTRUCTIONS_END])


def check_setting_sources(argv: list[str]) -> None:
    """(2026-10-27, SEC-H1) Raise unless the options of `argv` (before the first "--") carry exactly one
    `--setting-sources user`."""
    opts = [str(a) for a in (argv[:argv.index("--")] if "--" in argv else argv)]
    found = [i for i, a in enumerate(opts) if a.split("=", 1)[0].lower() == SETTING_SOURCES_FLAG]
    if len(found) != 1 or opts[found[0]] != SETTING_SOURCES_FLAG or opts[found[0] + 1:found[0] + 2] != ["user"]:
        raise ForbiddenFlagError(f"refusing an argv without exactly one `{SETTING_SOURCES_FLAG} user`: the "
                                 "task folder's .claude/settings*.json (hooks, env) would load")


def prompt_for(item: dict, prompt_file: str | None = None, reply_file: str | None = None) -> str:
    """The prompt text of the next attempt (2026-10-13, SPEC_GOALS 3.1 + amendments M2/M3).

    - no session yet: the user prompt (a goal is wrapped, attachments are appended); with
      `prompt_file`, the user prompt is replaced by a pointer to that file;
    - a goal right after a round that goes on: the next-round prompt (restates the goal);
    - any other resume (limit wait, stop, pause, crash): RESUME_PROMPT; for a goal it also restates
      the goal and the end-of-message protocol.
    - (2026-10-15, SPEC_V03 5.1/5.2) a task's prompts all end with the TASK_DONE/TASK_BLOCKED line;
      an owner's reply (`reply_in_flight` while running, `pending_reply` before) wins over all of
      the above: `Owner's reply:\n<text>\n\n<protocol>`.
    - (2026-10-17, 0.4.1 R5/R6) no session but `fresh` set: a new session that carries the context
      (a goal round with the PROGRESS handoff, or a reply/resume with the last final message).
    """
    # (2026-10-21, v049 dontask_hold addendum) the one resume after claude.exe killed background tasks
    resume = BG_REDO_PROMPT if item.get(BG_REDO) else RESUME_PROMPT
    if item.get("session_id"):
        reply = _reply_text(item)
        if reply is not None:
            return reply_prompt(item, reply, reply_file)
        if item.get(BG_REDO):
            return goal_resume_prompt(resume, item, prompt_file) if is_goal(item) else task_resume_prompt(resume)
        if is_goal(item):
            if item.get("goal_continue"):
                return next_round_prompt(item, prompt_file)
            return goal_resume_prompt(RESUME_PROMPT, item, prompt_file)
        return task_resume_prompt(RESUME_PROMPT)
    fresh = item.get("fresh")
    if fresh == FRESH_ROUND and is_goal(item):
        return fresh_round_prompt(item, prompt_file)
    if fresh:
        return fresh_resume_prompt(item, resume, prompt_file, _reply_text(item), reply_file)
    return first_round_prompt(item, prompt_file)


def _reply_text(item: dict) -> str | None:
    reply = item.get("reply_in_flight") or item.get("pending_reply")
    return reply if isinstance(reply, str) and reply else None


def prompt_arg_for(item: dict, prompts_dir: Path, write: bool = True) -> str:
    """The inline prompt argument. Only the RAW user prompt ever goes to <data>/prompts/<id>.md; the
    goal wrapper, protocol and attachments stay inline, and the 8000-char limit is measured on the
    final inline text (2026-10-13, amendment M2)."""
    path = (Path(prompts_dir) / f"{item['id']}.md").resolve()
    reply_path = (Path(prompts_dir) / f"{item['id']}.reply.md").resolve()
    reply = _reply_text(item) if (item.get("session_id") or item.get("fresh")) else None

    def write_prompt() -> str:
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["prompt"], encoding="utf-8")
            log.info("item %s: prompt is %d chars; written to %s", item["id"], len(item["prompt"]), path)
        return str(path)

    def write_reply() -> str:
        if write:
            reply_path.parent.mkdir(parents=True, exist_ok=True)
            reply_path.write_text(reply, encoding="utf-8")
            log.info("item %s: reply is %d chars; written to %s", item["id"], len(reply), reply_path)
        return str(reply_path)

    if not item.get("session_id") and item.get("fresh"):
        # (2026-10-17, 0.4.1 R5/R6) a new session that carries the context: the raw prompt goes to
        # the file when the goal is long or the inline text is too long, then the reply if needed.
        pfile = write_prompt() if needs_restate_file(item) else None
        text = prompt_for(item, pfile)
        if len(text) <= PROMPT_INLINE_MAX:
            return text
        pfile = pfile or write_prompt()
        text = prompt_for(item, pfile)
        if len(text) <= PROMPT_INLINE_MAX or reply is None:
            return text
        return prompt_for(item, pfile, write_reply())
    if reply is not None:
        # (2026-10-15, SPEC_V03 5.2) a reply over 8000 chars goes through the long-prompt file;
        # prompt_for() is the single source of the inline reply prompt.
        text = prompt_for(item)
        if len(text) <= PROMPT_INLINE_MAX:
            return text
        return prompt_for(item, None, write_reply())
    if item.get("session_id"):
        if not needs_restate_file(item):
            return prompt_for(item)
    else:
        text = prompt_for(item)
        if len(text) <= PROMPT_INLINE_MAX:
            return text
    return prompt_for(item, write_prompt())


def build_argv(
    claude_cmd: list[str],
    prompt_arg: str,
    profile: str,
    max_turns: int | None,
    session_id: str | None = None,
    extra_args: list[str] | None = None,
    default_args: list[str] | None = None,
    shell_allow: list[str] | None = None,
    max_agents: object = None,
    settings: dict | None = None,
    project_instructions: str | None = None,
    data_root: str | os.PathLike | None = None,
    _instructions_cut: bool = False,
    prompts_dir: str | os.PathLike | None = None,
    key_files: list | tuple = (),
    user_dir_rules: list | tuple = (),
    system_note: str | None = None,
) -> list[str]:
    # (2026-10-29, C-L1) `system_note`: a copy's app-built note (worktree.system_note), free text like the CLAUDE.md
    # block: joined into the ONE --append-system-prompt value below, never scanned as an option; None = no copy
    # (2026-10-27, SEC-H1) `project_instructions`: the task folder's CLAUDE.md text (project_instructions()),
    # which `--setting-sources user` no longer lets claude.exe load itself; None = no CLAUDE.md block
    # (2026-10-13, fix B5) The prompt goes LAST, after "--": it can never be parsed as an option
    # (even when it starts with '-') and no variadic tool flag can swallow it. Verified against
    # claude.exe 2.1.278: `... -- "--verbose is not a flag here. Reply with OK"` -> result "OK".
    # (2026-10-20, 0.4.4 logfix A) `shell_allow`: the project's shell-dev allow-list (other profiles ignore it)
    # (2026-10-21, subagents) `max_agents`: the item's subagent limit; None = the profile's own argv
    options = profile_args(profile, max_turns, shell_allow, max_agents)
    # (2026-10-27, SEC-M7/L24) `data_root`: the run's BASE data dir (data_root_of), denied to the task
    # (2026-10-27, SEC-M7 review) + Read of the other queues' prompts dirs (`prompts_dir` = the run's own, kept
    # readable) and of the `key_files` (key_file_paths) no .env rule covers
    options = with_data_dir_deny(options, data_root, prompts_dir, key_files)
    # (2026-10-28, SEC-H2) `user_dir_rules`: the Edit/Write denies of the user's extra folders (user_dirs_deny of
    # the task folder); every profile but `full`
    options = with_user_dirs_deny(options, profile, user_dir_rules)
    # (2026-10-27, modes fix HIGH) ultracode works only with the Workflow tool: `full` gets it, anything
    # else is refused (never a --settings flag nobody acts on)
    if settings and settings.get("ultracode"):
        refusal = ultracode_refusal(profile, max_agents)
        if refusal:
            raise ForbiddenFlagError(refusal)
        options = with_ultracode(options)
    extras = list(extra_args or [])
    check_extra_args(extras)
    # (2026-10-17, 0.4.1 R1/R7) the run's defaults (--model/--effort/--max-budget-usd), each
    # dropped when the user's extras set the same flag: the user's choice wins.
    defaults = list(default_args or [])
    check_extra_args(defaults)
    options += _without_overridden(defaults, extras)
    # (2026-10-27, SEC-H1) the CLAUDE.md block joins the profile's system prompt (or starts the one value),
    # so the user's own --append-system-prompt extras are merged into that same single value below
    block = _instructions_block(project_instructions, _instructions_cut) if project_instructions else None
    if block is not None:
        if SYSTEM_PROMPT_FLAG in options:
            at = options.index(SYSTEM_PROMPT_FLAG) + 1
            options[at] = f"{options[at]}\n\n{block}"
        else:
            options += [SYSTEM_PROMPT_FLAG, block]
    # (2026-10-29, C-L1) the copy note joins as loop.py joined it before, but after check_extra_args
    options += _merge_system_prompt(options, with_system_note(extras, system_note))
    # (2026-10-26, modes) `settings` (item_settings): one compact JSON object, or no flag at all
    if settings:
        if SETTINGS_FLAG in options:
            raise ForbiddenFlagError(f"refusing a second {SETTINGS_FLAG}")
        options += [SETTINGS_FLAG, json.dumps(settings, separators=(",", ":"), sort_keys=True)]
    if session_id:
        options += ["--resume", session_id]
    # (2026-10-21, owner) `full` carries the bypass flag on purpose: drop exactly that one token of its
    # own before the guard. Extras and defaults were checked above, so any second bypass is still refused.
    guarded = list(options)
    if profile == FULL:
        guarded.remove(FULL_BYPASS_FLAG)
    if block is not None:   # (2026-10-27, SEC-H1) repo text, like the prompt: free text, not an option
        guarded = [t.replace(block, "") for t in guarded]
    if system_note:   # (2026-10-29, C-L1) a copy's app-built note: free text, not an option (extras were checked)
        guarded = [t.replace(system_note, "") for t in guarded]
    check_options(guarded)
    argv = [*claude_cmd, "-p", "--output-format", "stream-json", "--verbose", *options, *SETTING_SOURCES_ARGS,
            "--", prompt_arg]
    check_setting_sources(argv)
    if block is not None:
        over = cmdline_chars(argv) - ARGV_CHARS_MAX
        if over > 0:   # (2026-10-27, SEC-H1) cut CLAUDE.md to fit the command line, or leave it out
            keep = len(project_instructions) - over - _ARGV_SLACK
            log.warning("CLAUDE.md: %s to fit the %d-character command line",
                        f"cut to {keep} characters" if keep > 0 else "left out", ARGV_CHARS_MAX)
            return build_argv(claude_cmd, prompt_arg, profile, max_turns, session_id, extra_args, default_args,
                              shell_allow, max_agents, settings,
                              project_instructions[:keep] if keep > 0 else None, data_root=data_root,
                              _instructions_cut=True, prompts_dir=prompts_dir, key_files=key_files,
                              user_dir_rules=user_dir_rules, system_note=system_note)
    # (2026-10-29, integrate round 8) still too long with no CLAUDE.md block left (many long extra folders in the
    # user's settings): leave out the LAST extra folders' denies, whole Edit+Write pairs, until it fits; one
    # warning with counts only (no folder name). CLAUDE.md is always cut first (above). When even no rule at all
    # would not make it fit (e.g. a huge prompt), every rule stays: leaving them out would only lose protection.
    rules = list(dict.fromkeys(user_dir_rules)) if profile != FULL else []
    over = cmdline_chars(argv) - ARGV_CHARS_MAX if rules else 0
    costs = [cmdline_chars([r]) - 2 for r in rules]   # each rule and its comma in the --disallowedTools value
    if 0 < over <= sum(costs):
        keep, freed = len(rules), 0
        while freed < over:
            step = 2 if keep % 2 == 0 else 1
            freed += sum(costs[keep - step:keep])
            keep -= step
        log.warning("the extra folders of your Claude settings: %d of %d deny rules left out to fit the "
                    "%d-character command line (those folders stay writable)", len(rules) - keep, len(rules),
                    ARGV_CHARS_MAX)
        return build_argv(claude_cmd, prompt_arg, profile, max_turns, session_id, extra_args, default_args,
                          shell_allow, max_agents, settings, project_instructions, data_root=data_root,
                          _instructions_cut=_instructions_cut, prompts_dir=prompts_dir, key_files=key_files,
                          user_dir_rules=rules[:keep], system_note=system_note)
    return argv


def redact_argv(argv: list[str], prompt_arg: str, hidden: list | tuple = ()) -> list[str]:
    """(2026-10-28, SEC-H2) + each deny rule in `hidden` (the user's extra folders, user_dirs_deny) is shown
    as `Edit(<extra folder>/**)`: no folder name of the user's settings reaches a log line."""
    short = prompt_arg if len(prompt_arg) <= 80 else prompt_arg[:77] + "..."
    out = [short if a == prompt_arg else _short_instructions(a) for a in argv]
    if hidden:
        rules = set(hidden)
        out = [",".join(f"{p.split('(', 1)[0]}(<extra folder>/**)" if p in rules else p for p in a.split(","))
               if a != short else a for a in out]
    return out


def _short_instructions(arg: str) -> str:
    """(2026-10-27, SEC-H1) `arg` with a CLAUDE.md block's text replaced by its length (log lines)."""
    start = arg.find(PROJECT_INSTRUCTIONS_LABEL)
    end = arg.rfind(PROJECT_INSTRUCTIONS_END)
    if start < 0 or end < start:
        return arg
    body = len(arg[start + len(PROJECT_INSTRUCTIONS_LABEL):end])
    return f"{arg[:start]}{PROJECT_INSTRUCTIONS_LABEL} [{body} characters] {arg[end:]}"


# (2026-10-21, v049 tester fix LOW-7) a key the child prints itself (an error echoing its own auth
# header, a debug line) is replaced before any of it is stored: the stream log, the stderr file, the
# app/errors log lines and the item's fields all come from the redacted bytes
SECRET_REDACTED = b"[REDACTED]"
SECRET_MIN_LEN = 8   # a value this short (or shorter) is not matched: too likely to be ordinary text


def attempt_secrets(env: dict, key_files: list[Path] | None = None) -> list[bytes]:
    """The VALUES of the provider key variables in this attempt's env (providers.key_vars_in), as
    bytes, longest first; only values longer than SECRET_MIN_LEN. Never logged.

    (2026-10-27, SEC-M6) Plus every value (_key_file_secret: all but model ids) of every file in `key_files`
    (key_file_paths: each providers.env candidate), and a URL's password: a shell profile, or `full`, could
    print providers.env itself while its env holds no key. (2026-10-27, SEC-L21) Each value also in its JSON-escaped forms (a stream-json line holds
    `\\\\` for a backslash). Known limit: a value split, re-encoded (base64) or otherwise changed survives."""
    vals = set()
    for name in key_vars_in(env):
        raw = env.get(name) or ""
        vals.update(v for v in (raw, raw.strip()) if len(v) > SECRET_MIN_LEN)
    for path in key_files or ():
        for name, raw in [*load_env_file(path).items(), *_skipped_key_lines(path)]:
            if _key_file_secret(name, raw):
                vals.update(v for v in (raw, raw.strip(), *_url_password(raw)) if len(v) > SECRET_MIN_LEN)
    vals |= {json.dumps(v, ensure_ascii=a)[1:-1] for v in vals for a in (True, False)}
    return sorted({v.encode("utf-8") for v in vals}, key=len, reverse=True)


# (2026-10-27, SEC-M6) a key-shaped name (report.py's rule, broader than providers.is_secret_env_name)
_KEY_FILE_SECRET_NAME_RE = re.compile(r"(?i)KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|CREDENTIAL|AUTH|_PAT$|DSN|"
                                      r"WEBHOOK|DATABASE_URL|CONNECTION_STRING")
_URL_USERINFO_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://([^/?#@]*)@")


def _key_file_secret(name: str, value: str) -> bool:
    """(2026-10-27, SEC-M6 review 2) Every non-empty key-file value is a secret, whatever its shape (a word, a
    number, a URL), except a model id (`<PREFIX>_MODEL`, ANTHROPIC_DEFAULT_*_MODEL: claude.exe's own init
    event and usage name it) and the provider key-names marker, unless the name is key-shaped. A value of
    SECRET_MIN_LEN characters or fewer is still not matched (attempt_secrets)."""
    if is_secret_env_name(name) or _KEY_FILE_SECRET_NAME_RE.search(name):
        return bool(value.strip())
    upper = name.upper()
    return bool(value.strip()) and not (upper.endswith("_MODEL") or upper == KEY_VARS_MARKER)


_SKIPPED_KEY_NAME_RE = re.compile(r"[^\s=#]+")


def _skipped_key_lines(path: str | os.PathLike) -> list[tuple[str, str]]:
    """(2026-10-27, SEC-M6 review 4) The `NAME=VALUE` lines load_env_file skips: a commented-out key
    (`# OLD_API_KEY=...`) and a name it does not accept (`MY-KEY=...`) are still in the file, so a task that
    prints it shows them. The value as written and without one pair of quotes; a NAME must be one word."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, ValueError):
        return []
    out = []
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        if line[:7].lower() == "export ":
            line = line[7:].lstrip()
        name, eq, value = line.partition("=")
        if eq and _SKIPPED_KEY_NAME_RE.fullmatch(name.strip()):
            value = value.strip()
            out.append((name.strip(), value))
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                out.append((name.strip(), value[1:-1]))
    return out


def _url_password(value: str) -> tuple[str, ...]:
    """(2026-10-27, SEC-M6 review 2) The password of a `scheme://user:password@host` value (and the whole
    `user:password`), so a copy of it alone is redacted too; () when there is none."""
    m = _URL_USERINFO_RE.match(value.strip())
    if not m or ":" not in m.group(1):
        return ()
    return (m.group(1), m.group(1).split(":", 1)[1])


def data_root_of(runs_dir: str | os.PathLike) -> Path:
    """(2026-10-27, SEC-M6/M7) The BASE data dir above a store's runs dir: <base>/runs,
    <base>/projects/<slug>-<hash8>/runs or .../lanes/<lane>/runs. A step up is taken only when the dir
    names fit the store's own forms (a lane name, a `<slug>-<hash8>` project dir), so a base kept in a
    folder that happens to be called `projects` is not mistaken for a project dir (its parent would be
    write-denied). Known limit: a base kept in a folder called `lanes` under a lane-valid name is."""
    from .store import LANES_DIRNAME, PROJECTS_DIRNAME, valid_lane_name   # lazy, like profiles' store imports
    d = Path(runs_dir).parent
    if d.parent.name == LANES_DIRNAME and valid_lane_name(d.name):
        d = d.parent.parent
    if d.parent.name == PROJECTS_DIRNAME and _PROJECT_DIR_NAME_RE.fullmatch(d.name):
        d = d.parent.parent
    return d


_PROJECT_DIR_NAME_RE = re.compile(r".+-[0-9a-f]{8}")   # store.project_dir_name: `<slug>-<hash8>`


def key_file_paths(runs_dir: str | os.PathLike, env: dict,
                   env_file: str | os.PathLike | None = None) -> list[Path]:
    """(2026-10-27, SEC-M6) Every providers.env the autopilot may read for this run: $CLAUDE_AUTOPILOT_ENV_FILE,
    the project's and the base data dir's (providers.env_file_candidates) and a lane's own data dir.
    (2026-10-27, SEC-M6 review) `env_file`: the run's `--env-file` (the caller passes it), first."""
    from .store import LANES_DIRNAME, valid_lane_name
    data_dir = Path(runs_dir).parent
    base = data_root_of(runs_dir)
    lane = data_dir.parent.name == LANES_DIRNAME and valid_lane_name(data_dir.name)
    project = data_dir.parent.parent if lane else data_dir
    paths = env_file_candidates(env_file, project, env, base)
    paths.append(data_dir / ENV_FILE_NAME)
    return list(dict.fromkeys(paths))


def redact_secrets(data: bytes, secrets: list[bytes]) -> bytes:
    """`data` with every exact occurrence of a secret replaced by SECRET_REDACTED."""
    for s in secrets:
        if s in data:
            data = data.replace(s, SECRET_REDACTED)
    return data


@dataclass
class Outcome:
    kind: str
    exit_code: int | None = None
    session_id: str | None = None
    result_text: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    error: str | None = None
    reset_at: object = None  # datetime | None
    reset_source: str | None = None
    denials: list = field(default_factory=list)
    elapsed_ms: int = 0
    log_path: Path | None = None
    subtype: str | None = None  # result.subtype (2026-10-13: a goal treats error_max_turns as a round)
    # (2026-10-15, SPEC_V03 5.2) Claude answered in this attempt (an assistant event without an error,
    # or a non-error result), so an owner's reply it carried has reached the session; otherwise (a
    # crash, a stop, a usage limit before any answer) the reply is put back.
    answered: bool = False
    # (2026-10-17, 0.4.1) from the init event, and the last main-thread context (R1, R5/R6)
    model: str | None = None
    claude_code_version: str | None = None
    last_ctx: int | None = None
    # (2026-10-26, task 5c5e438d) the attempt's prompt-cache use (cache_totals); None = no usage seen
    cache_tokens: dict | None = None
    # (2026-10-20) init.apiKeySource and the billing it implies (billing_for); None = no init event
    api_key_source: str | None = None
    billing: str | None = None
    # (2026-10-21, v049 dontask_hold addendum, SPEC_V049_DONTASK_HOLD 3) claude.exe killed background
    # tasks when the run ended (bg_killed_reason): what was lost, else None. Never recorded as done.
    bg_killed: str | None = None
    # (2026-10-26, limit_continue) a LIMIT outcome: the handoff built from its run log (limit_handoff)
    handoff: str | None = None


# (2026-10-21, v049 dontask_hold addendum) `claude -p` waits for background tasks (agents, background
# shell commands) after the final message, then kills them. Real stderr of item 86735ff0 (claude.exe
# 2.1.x): "Background tasks still running after 600s; terminating. Set
# CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely." Matched loosely (number, wording around it).
BG_KILLED_RE = re.compile(r"background\s+tasks?\b[^\n]{0,80}?\bstill\s+running\b[^\n]{0,80}?\bterminat", re.I)
# The wait's ceiling (ms) for the child, unless the parent env sets a positive one (never 0 = unbounded).
# The CLI's own default is 600 s, which killed a research agent in the evidence run. Raised to at most
# BG_WAIT_CEILING_MAX_S, and never more than half the attempt's timeout (the wait starts only after the
# final message, so the rest of the attempt keeps room to work; the runner's timeout still bounds all).
BG_WAIT_ENV = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"
BG_WAIT_CEILING_MAX_S = 1800
BG_WAIT_CEILING_MIN_S = 600


def bg_wait_ceiling_ms(item_timeout_s: float) -> int:
    """The CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS this runner gives a child: min(30 min, timeout / 2), but
    never under the CLI's own 600 s default."""
    return int(max(BG_WAIT_CEILING_MIN_S, min(BG_WAIT_CEILING_MAX_S, item_timeout_s / 2)) * 1000)


def with_bg_wait_ceiling(env: dict, item_timeout_s: float) -> dict:
    """`env` with BG_WAIT_ENV set: a positive value from the parent env is kept; unset, empty, 0 (the
    CLI's "wait indefinitely") or junk is replaced by bg_wait_ceiling_ms (logged)."""
    cur = (env.get(BG_WAIT_ENV) or "").strip()
    # (2026-10-21, v049 tester fix LOW-5) ASCII digits only: str.isdigit() takes '²', which int() refuses
    if cur.isascii() and cur.isdigit() and int(cur) > 0:
        return env
    if cur:
        log.warning("%s=%r in the parent env is not a positive number of ms (0 = wait forever); using %d",
                    BG_WAIT_ENV, cur, bg_wait_ceiling_ms(item_timeout_s))
    return {**env, BG_WAIT_ENV: str(bg_wait_ceiling_ms(item_timeout_s))}


class _AttemptState:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.result: dict | None = None
        self.limit_reasons: list[str] = []
        self.limit_texts: list[str] = []
        self.resets_at = None
        self.denials: list = []
        self.non_json: list[str] = []
        self.answered = False
        # (2026-10-17, 0.4.1 R1/R5) from the init event, and the context (input + cache read + cache
        # write tokens) of the last main-thread assistant message (parent_tool_use_id is null)
        self.model: str | None = None
        self.cc_version: str | None = None
        self.last_ctx: int | None = None
        self.api_key_source: str | None = None
        # (2026-10-26, task 5c5e438d) per API call (message id): its call_usage() (cache_totals)
        self.cache_calls: dict = {}
        # (2026-10-21, subagents) how many subagents this attempt started (Agent/Task tool_use blocks)
        self.agent_calls = 0
        # (2026-10-21, v049 dontask_hold) per tool_use_id: the permission_denied event's reason, the
        # calls a PreToolUse hook stopped (a note, never a block), and the input of the calls a denial
        # could name (only used when no result event lists the denials)
        self.denial_reasons: dict = {}
        self.denied_tools: dict = {}
        self.denied_order: list = []
        self.hook_ids: set = set()
        self.tool_inputs: dict = {}
        # (2026-10-21, v049 dontask_hold addendum) the background tasks the stream announced
        # (system/task_started is_backgrounded) and the ones it reported killed (task_updated
        # patch.status "killed"), with the event number; a kill after the last main-thread assistant
        # message is the CLI's end-of-run kill (the model can no longer have asked for it)
        self.seq = 0
        self.last_main_assistant = -1
        self.bg_tasks: dict = {}
        self.bg_kills: list = []
        # (2026-10-26, usage forecast) called with each rate_limit_info (usage.recorder; None = not recorded)
        self.on_rate_limit: Callable[[dict], None] | None = None

    def end_of_run_kills(self) -> list[str]:
        """The descriptions of the background tasks killed after the model's last message."""
        return [self.bg_tasks[tid] for tid, n in self.bg_kills
                if tid in self.bg_tasks and n > self.last_main_assistant]

    def event_denials(self) -> list[dict]:
        """The denials the stream's permission_denied events announced (an attempt without a result)."""
        out = []
        for tid in self.denied_order:
            name, inp = self.tool_inputs.get(tid, (None, None))
            out.append({"tool_name": name or self.denied_tools.get(tid), "tool_use_id": tid,
                        "tool_input": inp})
        return out


# (2026-10-21, v049 dontask_hold) the tool inputs kept per attempt for event_denials (oldest dropped)
TOOL_INPUTS_MAX = 200
_DENIABLE_TOOLS = ("Bash", "PowerShell", "Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Monitor",
                   "WebFetch", "WebSearch", "Glob", "Grep")


def _note_tool_calls(ev: dict, st: _AttemptState) -> None:
    """Remember the input of the tool_use blocks a denial could name (command/path fields only)."""
    msg = ev.get("message")
    parts = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(parts, list):
        return
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("name") in _DENIABLE_TOOLS:
            inp = p.get("input") if isinstance(p.get("input"), dict) else {}
            keep = {k: str(v)[:300] for k, v in inp.items()
                    if k in ("command", "file_path", "notebook_path", "path", "url", "pattern")}
            st.tool_inputs[p.get("id")] = (p.get("name"), keep)
            if len(st.tool_inputs) > TOOL_INPUTS_MAX:
                st.tool_inputs.pop(next(iter(st.tool_inputs)))


def _note_tool_results(ev: dict, st: _AttemptState) -> None:
    """A tool_result error that is a PreToolUse hook's denial marks its call as a hook note."""
    msg = ev.get("message")
    parts = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(parts, list):
        return
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "tool_result" and is_hook_text(tool_result_text(p)):
            st.hook_ids.add(p.get("tool_use_id"))


# (2026-10-20) Billing. init.apiKeySource is, per the schema in claude.exe 2.1.304: "ANTHROPIC_API_KEY"
# (env var), "apiKeyHelper" (helper command), "/login managed key" (Console API key from /login) or
# "none" (no API key: claude.ai OAuth login, a bearer token or a third-party cloud provider); the
# legacy "user"/"project"/"org"/"temporary"/"oauth" are never emitted by current CLIs. Every real
# subscription run log shows "none". The result's total_cost_usd is an API-equivalent estimate, which
# a subscription run is not billed.
BILLING_SUBSCRIPTION, BILLING_API, BILLING_UNKNOWN = "subscription", "api", "unknown"
# env vars that route a "none" source to a billed provider (Bedrock/Vertex/Foundry, a gateway token)
PROVIDER_ENV = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
                "ANTHROPIC_AUTH_TOKEN")
API_BILLING_ENV = ("ANTHROPIC_API_KEY", *PROVIDER_ENV)


def _env_on(env: dict | None, name: str) -> bool:
    value = (env or {}).get(name)
    return bool(value) and str(value).strip().lower() not in ("0", "false", "no", "off")


def billing_for(api_key_source: str | None, env: dict | None = None) -> str:
    """'api' | 'subscription' | 'unknown' for an attempt's init.apiKeySource (None = no init event)."""
    if not isinstance(api_key_source, str) or not api_key_source:
        return BILLING_UNKNOWN
    if api_key_source != "none":
        return BILLING_API
    if any(_env_on(env, name) for name in PROVIDER_ENV):
        return BILLING_API
    return BILLING_SUBSCRIPTION


def usage_text(billing: str | None, cost_usd) -> str:
    """The cost part of a log/show line: `cost_usd=3.01 (api)` only for API billing, otherwise
    `usage=subscription` / `usage=unknown`. The stored cost value is never changed."""
    if billing == BILLING_API:
        return f"cost_usd={cost_usd} (api)"
    return f"usage={billing or BILLING_UNKNOWN}"


_billing_lock = threading.Lock()
_billing_warned: dict = {}


def _reset_billing_warning() -> None:
    """Tests only: forget the once-per-runner API-billing warning."""
    with _billing_lock:
        _billing_warned.clear()


def warn_api_billing_env(env: dict) -> bool:
    """Log a WARNING once per runner process when the child env routes claude to a billed API
    (ANTHROPIC_API_KEY, Bedrock/Vertex/Foundry, a gateway token). The env is not changed: stripping
    the variable is the owner's choice. Returns True when this call logged it."""
    names = [name for name in API_BILLING_ENV if _env_on(env, name)]
    if not names:
        return False
    with _billing_lock:
        if _billing_warned.get("done"):
            return False
        _billing_warned["done"] = True
    log.warning("billing: %s is set in the env passed to claude; runs will be billed to the API key, "
                "not the subscription", ", ".join(names))
    return True


def _assistant_text(ev: dict) -> str:
    msg = ev.get("message") or {}
    parts = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(parts, list):
        return " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return str(parts or "")


def agent_calls(ev: dict) -> int:
    """(2026-10-21, subagents) How many subagents one assistant event starts: its `tool_use` content
    blocks whose name is a spawn tool (Agent, or the older Task). The count is the only check on the
    LIMIT that exists — claude.exe has no flag for it (profiles.with_agents)."""
    msg = ev.get("message")
    parts = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(parts, list):
        return 0
    # (2026-10-27, modes fix HIGH) a Workflow call (ultracode, `full` only) starts agents too: it counts as
    # one here, a floor (one call may start several)
    return sum(1 for p in parts
               if isinstance(p, dict) and p.get("type") == "tool_use"
               and (p.get("name") in AGENT_SPAWN_TOOLS or p.get("name") == WORKFLOW_TOOL))


def over_agent_limit(started: int, limit: int | None) -> str | None:
    """The warning text when `started` subagents is more than `limit` allows, else None."""
    if limit is None or limit == AGENTS_UNLIMITED or started <= limit:
        return None
    allowed = "no subagents" if limit == 0 else f"at most {limit}"
    return f"started {started} subagent(s); its limit is {allowed}"


def context_tokens(ev: dict) -> int | None:
    """input + cache_read + cache_creation tokens of an assistant event's usage, or None."""
    msg = ev.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
    return total


# (2026-10-26, task 5c5e438d) prompt-cache use: the item's `cache_tokens` names and the usage fields
CACHE_TOKEN_KEYS = (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
                    ("cache_read", "cache_read_input_tokens"))


def call_usage(ev: dict) -> tuple | None:
    """(input, cache write, cache read) tokens of an assistant event's usage (junk counts as 0), or None."""
    msg = ev.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return None
    vals = (usage.get(key) for _, key in CACHE_TOKEN_KEYS)
    return tuple(v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0 for v in vals)


def cache_totals(calls: dict) -> dict | None:
    """An attempt's prompt-cache use, each CACHE_TOKEN_KEYS field summed over its API calls (subagents
    included), or None without a call. `calls` holds one call_usage() per message id: the content blocks
    of one call arrive as separate events that repeat its usage (real 2.1.304 logs)."""
    if not calls:
        return None
    return {name: sum(c[i] for c in calls.values()) for i, (name, _) in enumerate(CACHE_TOKEN_KEYS)}


def _handle_event(ev: dict, st: _AttemptState, item_id: str, on_session: Callable[[str], None]) -> None:
    etype, sub = ev.get("type"), ev.get("subtype")
    st.seq += 1
    log.debug("item %s event type=%s subtype=%s", item_id, etype, sub)
    sid = ev.get("session_id")
    if etype == "system" and sub == "init":
        # (2026-10-17, 0.4.1 R1) field names verified in real 2.1.278 run logs
        if isinstance(ev.get("model"), str):
            st.model = ev["model"]
        if isinstance(ev.get("claude_code_version"), str):
            st.cc_version = ev["claude_code_version"]
        # (2026-10-20) field name verified in real 2.1.304 run logs ("apiKeySource":"none")
        if isinstance(ev.get("apiKeySource"), str):
            st.api_key_source = ev["apiKeySource"]
    if etype == "system" and sub == "init" and sid and sid != st.session_id:
        st.session_id = sid
        on_session(sid)  # persisted immediately so a crash can still resume
    elif sid and not st.session_id:
        st.session_id = sid
        on_session(sid)
    if etype == "rate_limit_event":
        info = ev.get("rate_limit_info") or {}
        log.debug("item %s rate_limit_info status=%s type=%s", item_id, info.get("status"),
                  info.get("rateLimitType"))
        if info.get("status") == "rejected":
            st.limit_reasons.append("rate_limit_event rejected")
            if info.get("resetsAt") not in (None, ""):
                st.resets_at = info.get("resetsAt")
        if st.on_rate_limit is not None and isinstance(info, dict):
            st.on_rate_limit(info)  # (2026-10-26, usage forecast) a usage sample; never raises
    elif etype == "assistant" and ev.get("error"):
        text = _assistant_text(ev)
        if ev.get("error") == "rate_limit":
            st.limit_reasons.append("assistant error=rate_limit")
        st.limit_texts.append(text)
        log.warning("item %s assistant error=%s: %s", item_id, ev.get("error"), text[:300])
    elif etype == "system" and sub == "permission_denied":
        log.warning("item %s permission denied: tool=%s reason=%s", item_id, ev.get("tool_name"),
                    ev.get("decision_reason_type"))
        tid = ev.get("tool_use_id")
        if tid is not None:   # (2026-10-21, v049 dontask_hold) "mode" = don't ask mode
            if tid not in st.denial_reasons:
                st.denied_order.append(tid)
            st.denial_reasons[tid] = str(ev.get("decision_reason_type") or "denied")
            st.denied_tools[tid] = ev.get("tool_name")
    elif etype == "system" and sub == "task_started" and ev.get("is_backgrounded") is True:
        # (2026-10-21, v049 dontask_hold addendum) field names verified in the 86735ff0 run log
        tid = ev.get("task_id")
        if isinstance(tid, str) and len(st.bg_tasks) < TOOL_INPUTS_MAX:
            st.bg_tasks[tid] = str(ev.get("description") or ev.get("task_type") or tid)[:120]
    elif etype == "system" and sub == "task_updated":
        patch = ev.get("patch") if isinstance(ev.get("patch"), dict) else {}
        if patch.get("status") == "killed" and isinstance(ev.get("task_id"), str):
            st.bg_kills.append((ev["task_id"], st.seq))
    elif etype == "result":
        st.result = ev
    elif etype == "user":
        _note_tool_results(ev, st)   # (2026-10-21, v049 dontask_hold) hook denials are notes
    if etype == "assistant":
        st.agent_calls += agent_calls(ev)   # (2026-10-21, subagents) also a subagent's own agents
        _note_tool_calls(ev, st)
        usage = call_usage(ev)   # (2026-10-26, task 5c5e438d) an event without a message id counts alone
        if usage is not None:
            mid = ev["message"].get("id")
            st.cache_calls[mid if isinstance(mid, str) and mid else f"#{st.seq}"] = usage
    if etype == "assistant" and ev.get("parent_tool_use_id") is None:
        st.last_main_assistant = st.seq
        ctx = context_tokens(ev)
        # (2026-10-26, limit_continue) a usage 0 is the CLI's own "<synthetic>" limit message, never a real
        # context: it made last_ctx 0 after every limit, so the cold-cache fresh session (R6) never started
        if ctx:
            st.last_ctx = ctx
    if (etype == "assistant" and not ev.get("error")) or (etype == "result" and not ev.get("is_error")):
        st.answered = True


# (2026-10-20, hotfix) Git Bash for the child's hooks, located once per runner process: claude.exe
# 2.1.304 blocks every tool whose bash-style hook it had to run in PowerShell (claude_path.py).
_git_bash_lock = threading.Lock()
_git_bash: dict = {}   # {"path": str | None} once located; "logged_existing" once logged
# (2026-10-26, network audit) claude.exe's own switch for its non-essential traffic (child_env)
NONESSENTIAL_TRAFFIC_ENV = "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"


def _reset_git_bash_cache() -> None:
    """Tests only: forget the located Git Bash and the once-only log lines."""
    with _git_bash_lock:
        _git_bash.clear()


# (2026-10-27, SEC-L19) settings every git process under claude.exe gets ABOVE the repo's own .git/config
# (GIT_CONFIG_COUNT/KEY_n/VALUE_n, git's command-line scope): shell-read's "read-only" git status/diff/log/
# show must not run a program that a repo's config names (core.fsmonitor, a pager, gpg.program through
# log.showSignature), and no hook runs (hooksPath = the null device). diff.external cannot be switched off
# this way (git 2.43: an empty value makes `git diff` fail with "cannot spawn"), nor can a textconv or diff
# driver (`diff.<name>.textconv` / `.command`) that .git/config defines: (2026-10-27, SEC-L19 review 3)
# shell-read's exact `git diff` forms carry --no-ext-diff --no-textconv (profiles.SHELL_READ_GIT_EXACT).
# Known limit: a clean filter (`filter.<name>.clean`) that .git/config defines still runs in `git status`
# and `git diff` (verified with git 2.43; git has no switch for it); only the .git/ write denies
# (profiles.GIT_DIR_DENY) keep a task from planting any of these.
GIT_SAFE_CONFIG = (("core.fsmonitor", "false"), ("core.hooksPath", os.devnull), ("core.pager", "cat"),
                   ("log.showSignature", "false"))


def with_git_safe_config(env: dict) -> None:
    """(2026-10-27, SEC-L19) Append GIT_SAFE_CONFIG to `env`'s GIT_CONFIG_* entries, in place (a parent's own
    entries are kept, ours come later and win; an unreadable count is replaced)."""
    try:
        n = max(0, int((env.get("GIT_CONFIG_COUNT") or "0").strip()))
    except ValueError:
        n = 0
    for i, (key, value) in enumerate(GIT_SAFE_CONFIG, n):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_CONFIG_COUNT"] = str(n + len(GIT_SAFE_CONFIG))


_GIT_CONFIG_ENTRY_RE = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+")


def drop_broken_git_config(env: dict) -> bool:
    """(2026-10-27, integrate S1 x S6) A parent GIT_CONFIG_* set with a hole (a KEY_i or VALUE_i below its
    count missing, e.g. a credential VALUE that providers.scrub_child_env dropped) makes every git fail
    ("missing config key/value"): drop that set, in place (names only are logged). True when dropped."""
    try:
        n = max(0, int((env.get("GIT_CONFIG_COUNT") or "0").strip()))
    except ValueError:
        return False   # with_git_safe_config replaces an unreadable count
    if all(f"GIT_CONFIG_KEY_{i}" in env and f"GIT_CONFIG_VALUE_{i}" in env for i in range(n)):
        return False
    for k in [k for k in env if _GIT_CONFIG_ENTRY_RE.fullmatch(k.upper())]:
        del env[k]
    env.pop("GIT_CONFIG_COUNT", None)
    log.debug("child env: the parent's GIT_CONFIG_* entries were incomplete after the scrub; dropped")
    return True


def child_env(base: dict | None = None, git_safe: bool = True) -> dict[str, str]:
    """The env for a claude.exe child: `base` (default os.environ) plus CLAUDE_CODE_GIT_BASH_PATH when
    the parent did not set it. A parent value is never overridden, even one naming a missing file
    (that is only logged). An empty value counts as unset.

    (2026-10-26, network audit) Also CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 (no auto-update check,
    telemetry or error reporting from the child) unless the parent env sets it: an explicit value, even
    "0", is the owner's choice and is kept; an empty one counts as unset.

    (2026-10-27, SEC-L19) `git_safe`: + GIT_SAFE_CONFIG (with_git_safe_config); run_attempt leaves it off for
    `full`, whose task may run any command anyway (its commits keep the repo's hooks)."""
    env = dict(os.environ if base is None else base)
    # (2026-10-26, security L2) no stray secret-named variable or TLS/Node switch (providers.scrub_child_env)
    dropped = scrub_child_env(env)
    if dropped:
        log.debug("child env: dropped %s", ", ".join(dropped))   # names only, never values
    # (2026-10-27, integrate S1 x S6) after the scrub, so the safe git config itself is never scrubbed
    drop_broken_git_config(env)
    if git_safe:
        with_git_safe_config(env)
    if not env.get(NONESSENTIAL_TRAFFIC_ENV):
        env[NONESSENTIAL_TRAFFIC_ENV] = "1"
    existing = env.get(GIT_BASH_ENV)
    with _git_bash_lock:
        if existing:
            if not _git_bash.get("logged_existing"):
                _git_bash["logged_existing"] = True
                if os.path.isfile(existing):
                    log.info("git bash: %s already set by the parent env: %s (kept)", GIT_BASH_ENV, existing)
                else:
                    log.warning("git bash: %s is set to a missing file: %s (kept, not overridden); hooks "
                                "that need bash may fail under claude.exe >=2.1.304", GIT_BASH_ENV, existing)
            return env
        if "path" not in _git_bash:
            found = find_git_bash(env)
            _git_bash["path"] = found[1] if found else None
            if found:
                log.info("git bash: %s=%s (found via %s)", GIT_BASH_ENV, found[1], found[0])
            else:
                log.warning("git bash: not found; %s left unset; hooks in ~/.claude/settings.json that "
                            "need bash will fail under claude.exe >=2.1.304", GIT_BASH_ENV)
        if _git_bash["path"]:
            env[GIT_BASH_ENV] = _git_bash["path"]
    return env


def provider_key_refusal(profile: str, env: dict) -> str | None:
    """(2026-10-21, 0.4.4 tester fixes MED-4) Why an attempt with `profile` must not start with `env`,
    else None. A provider key in the env of claude.exe is in the env of its PowerShell tool too:
    shell-read/shell-dev allow `Get-ChildItem env:` and `Get-Content env:ANTHROPIC_AUTH_TOKEN` (literal
    arguments to an allowed read cmdlet), which would print the key into the transcript, so a shell
    profile never runs with a key in its env. (2026-10-27, SEC-M6) This checks the ENV only: providers.env
    stays on disk while a shell profile (or `full`) runs on any provider. Its Read is denied
    (profiles.ENV_FILE_DENY) and every key in every providers.env candidate is redacted from the run logs
    (attempt_secrets, exact and JSON-escaped bytes only). (apiKeyHelper keeps the key out of the env, probe
    in DECISIONS.md, but its source would still be a readable file.) Names only, never values."""
    if profile in NO_SHELL_PROFILES:
        return None
    names = key_vars_in(env)
    if not names:
        return None
    # (2026-10-26, security L2) any secret-named variable left in the child env counts (key_vars_in)
    return (f"profile {profile} refused: the child env holds an API key or token "
            f"({', '.join(names)}), and a shell profile could print it (`Get-ChildItem env:`). Run this "
            "item with profile safe, edits or plan, or with the default provider `claude` and without "
            "that variable.")


def run_attempt(
    item: dict,
    *,
    claude_cmd: list[str],
    prompts_dir: Path,
    runs_dir: Path,
    max_turns: int | None,
    item_timeout_s: float,
    poll_min: float,
    clock: Clock,
    on_session: Callable[[str], None],
    extra_args: list[str] | None = None,
    on_start: Callable[[subprocess.Popen], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    should_interrupt: Callable[[], str | None] | None = None,
    default_args: list[str] | None = None,
    shell_allow: list[str] | None = None,
    env: dict | None = None,
    on_rate_limit: Callable[[dict], None] | None = None,
    env_file: str | os.PathLike | None = None,
    system_note: str | None = None,
) -> Outcome:
    """Run one attempt; never raises for child failures. KeyboardInterrupt kills the tree, re-raises.

    `should_stop` is polled every <= 0.25 s while the child runs; when it returns True the tree is
    killed and StopRequested is raised (2026-10-13, SPEC_VSCODE section 2).

    `should_interrupt` is polled just as often (it rate-limits itself); when it returns a status
    (`paused`/`cancelled`) the tree is killed and an INTERRUPTED outcome is RETURNED, not raised,
    so the runner goes on with the next item (2026-10-13, SPEC_GOALS 3.3).

    `env` (2026-10-21, add panel) is the base env of THIS attempt (an item on its own provider,
    loop.attempt_choice); None = the runner's os.environ, as before. The key refusal and the billing
    read the env the child really gets.

    `on_rate_limit` (2026-10-26, usage forecast) gets every rate_limit_event's rate_limit_info as it
    arrives (loop.run_loop passes usage.recorder); None = nothing is recorded.

    `env_file` (2026-10-27, SEC-M6 review) is the run's `--env-file` (the caller passes it): every key in it
    is redacted from the logs and, unless a .env rule covers its name, the task may not Read it.

    `system_note` (2026-10-29, C-L1) is a copy's note (worktree.system_note) for build_argv; None = no copy.
    """
    from .procutil import kill_tree

    item_id = item["id"]
    attempt = int(item.get("attempts") or 1)
    # (2026-10-27, modes fix HIGH) an ultracode item that cannot get the Workflow tool fails clearly
    refusal = (ultracode_refusal(item.get("profile") or "safe", item.get("max_agents"))
               if item_settings(item).get("ultracode") else None)
    if refusal:
        log.error("item %s: %s", item_id, refusal)
        return Outcome(FAILED, error=refusal)
    prompt_arg = prompt_arg_for(item, prompts_dir)
    # (2026-10-28, SEC-H2) the user's extra folders, from the settings file the child's env names (not for full)
    user_rules = ([] if (item.get("profile") or "safe") == FULL
                  else user_dirs_deny(item["cwd"], os.environ if env is None else env))
    argv = build_argv(claude_cmd, prompt_arg, item.get("profile") or "safe", max_turns,
                      item.get("session_id"), extra_args, default_args, shell_allow,
                      item.get("max_agents"), item_settings(item),
                      project_instructions(item["cwd"]),   # (2026-10-27, SEC-H1)
                      data_root=data_root_of(runs_dir),    # (2026-10-27, SEC-M7/L24)
                      prompts_dir=prompts_dir,             # (2026-10-27, SEC-M7 review) the rest are denied
                      key_files=key_file_paths(runs_dir, os.environ if env is None else env, env_file),
                      user_dir_rules=user_rules, system_note=system_note)   # (2026-10-29, C-L1)
    run_dir = Path(runs_dir) / item_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / f"{attempt}.jsonl"
    err_path = run_dir / f"{attempt}.stderr.txt"

    creationflags = 0
    popen_kw: dict = {}
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kw["start_new_session"] = True
    # (2026-10-21, v049 dontask_hold addendum) a bounded, longer wait for background tasks at the end;
    # (2026-10-21, add panel) on top of the attempt's own env (an item's own provider)
    env = with_bg_wait_ceiling(child_env(env, git_safe=(item.get("profile") or "safe") != FULL), item_timeout_s)
    refusal = provider_key_refusal(item.get("profile") or "safe", env)
    if refusal:   # (2026-10-21, 0.4.4 tester fixes MED-4) before any child exists
        log.error("item %s: %s", item_id, refusal)
        return Outcome(FAILED, error=refusal, log_path=log_path)
    # (2026-10-21, v049 tester fix LOW-7) values, never logged; (2026-10-27, SEC-M6) + the providers.env files
    # (2026-10-27, SEC-M6 review) + the run's --env-file (`env_file`): its other keys are in no env
    secrets = attempt_secrets(env, key_file_paths(runs_dir, env, env_file))
    log.info("item %s attempt %d: spawn cwd=%s argv=%s", item_id, attempt, item["cwd"],
             redact_argv(argv, prompt_arg, user_rules))
    warn_api_billing_env(env)  # (2026-10-20) once per runner; the env is passed on unchanged
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv, cwd=item["cwd"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, creationflags=creationflags, env=env, **popen_kw,
        )
    except OSError as exc:
        log.error("item %s: could not start claude: %s", item_id, exc)
        return Outcome(FAILED, error=f"spawn failed: {exc}", log_path=log_path)
    if on_start:
        on_start(proc)

    lines: "queue.Queue[bytes | None]" = queue.Queue()
    stderr_chunks: list[bytes] = []

    def _pump_stdout() -> None:
        try:
            for raw in iter(proc.stdout.readline, b""):
                lines.put(raw)
        except (OSError, ValueError) as exc:
            log.warning("item %s stdout reader stopped: %s", item_id, exc)
        finally:
            lines.put(None)

    def _pump_stderr() -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                stderr_chunks.append(raw)
        except (OSError, ValueError) as exc:
            log.warning("item %s stderr reader stopped: %s", item_id, exc)

    t_out = threading.Thread(target=_pump_stdout, daemon=True)
    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_out.start()
    t_err.start()

    st = _AttemptState()
    st.on_rate_limit = on_rate_limit
    timed_out = False
    interrupted: str | None = None
    deadline = t0 + item_timeout_s
    try:
        with open(log_path, "ab") as jl:
            while True:
                if should_stop is not None and should_stop():
                    log.warning("item %s: stop requested; killing process tree pid=%s", item_id, proc.pid)
                    raise StopRequested()
                if should_interrupt is not None:
                    interrupted = should_interrupt()
                    if interrupted:
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    raw = lines.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if raw is None:
                    break
                raw = redact_secrets(raw, secrets)
                jl.write(raw if raw.endswith(b"\n") else raw + b"\n")
                # (2026-10-15, SPEC_V03 1.1) flushed after EVERY line, before it is parsed, so the
                # extension's live log sees each event as it arrives (tested by mutation).
                jl.flush()
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except ValueError:
                    log.warning("item %s non-JSON stdout line kept: %s", item_id, text[:200])
                    st.non_json.append(text)
                    continue
                if isinstance(ev, dict):
                    _handle_event(ev, st, item_id, on_session)
                else:
                    log.warning("item %s JSON line is not an object: %s", item_id, text[:200])
        if interrupted:
            log.warning("item %s: status is now %r; killing process tree pid=%s", item_id,
                        interrupted, proc.pid)
            kill_tree(proc)
        elif timed_out:
            log.error("item %s: timeout after %.0fs; killing process tree pid=%s", item_id,
                      item_timeout_s, proc.pid)
            kill_tree(proc)
        else:
            try:
                proc.wait(timeout=max(5.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.error("item %s: stdout closed but pid=%s did not exit; killing tree", item_id, proc.pid)
                kill_tree(proc)
    except BaseException as exc:  # Ctrl+C or a bug here: never leave the child tree running
        log.warning("item %s: attempt aborted (%s); killing process tree pid=%s", item_id,
                    type(exc).__name__, proc.pid)
        kill_tree(proc)
        raise
    finally:
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        stderr_bytes = redact_secrets(b"".join(stderr_chunks), secrets)
        try:
            err_path.write_bytes(stderr_bytes)
        except OSError as exc:
            log.error("item %s: could not write %s: %s", item_id, err_path, exc)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    exit_code = proc.returncode
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    log.info("item %s attempt %d: exit=%s elapsed_ms=%d log=%s", item_id, attempt, exit_code,
             elapsed_ms, log_path)
    if stderr_text.strip():
        log.debug("item %s stderr: %s", item_id, stderr_text.strip()[:2000])
    _check_model(item_id, option_value(argv[:argv.index("--")], "--model"), st)
    # (2026-10-21, subagents) the limit's count is a request in the system prompt, not a claude.exe
    # flag: say so in the log when an attempt went over it (the run itself is not touched)
    if st.agent_calls:
        limit = normalise_agents(item.get("max_agents"))
        over = over_agent_limit(st.agent_calls, limit)
        if over:
            log.warning("item %s: %s (profiles.with_agents states the limit in the system prompt; "
                        "claude.exe has no flag for the count)", item_id, over)
        else:
            log.info("item %s: started %d subagent(s)", item_id, st.agent_calls)
    billing = billing_for(st.api_key_source, env)
    cache = cache_totals(st.cache_calls)
    if cache:   # (2026-10-26, task 5c5e438d)
        log.info("item %s attempt %d: prompt cache: read %d, written %d, uncached input %d tokens",
                 item_id, attempt, cache["cache_read"], cache["cache_write"], cache["input"])
    if interrupted:
        res = st.result or {}
        return Outcome(INTERRUPTED, exit_code=exit_code, session_id=res.get("session_id") or st.session_id,
                       answered=st.answered,
                       error=f"interrupted: item {interrupted} while running", elapsed_ms=elapsed_ms,
                       log_path=log_path, model=st.model, claude_code_version=st.cc_version,
                       last_ctx=st.last_ctx, api_key_source=st.api_key_source, billing=billing,
                       cache_tokens=cache)

    outcome = classify(st, exit_code, stderr_text, timed_out, item_id, clock, poll_min,
                       elapsed_ms=elapsed_ms, log_path=log_path)
    outcome.cache_tokens = cache
    outcome.answered = st.answered
    outcome.model, outcome.claude_code_version, outcome.last_ctx = st.model, st.cc_version, st.last_ctx
    outcome.api_key_source, outcome.billing = st.api_key_source, billing
    return outcome


def _check_model(item_id: str, requested: str | None, st: _AttemptState) -> None:
    """(2026-10-17, 0.4.1 R1) Log the CLI version and model; warn when the model differs from the
    requested full model name (an alias such as `opus` is not compared)."""
    if st.model or st.cc_version:
        log.info("item %s: claude_code_version=%s model=%s apiKeySource=%s", item_id, st.cc_version,
                 st.model, st.api_key_source)
    if (requested and requested.startswith("claude-") and st.model
            and not st.model.startswith(requested)):
        log.warning("item %s: requested model %s but the session ran %s (claude_code_version %s)",
                    item_id, requested, st.model, st.cc_version)


def bg_killed_reason(st: _AttemptState, stderr_text: str) -> str | None:
    """(2026-10-21, v049 dontask_hold addendum) Why this attempt's background work was lost, else None:
    claude.exe's "Background tasks still running after Ns; terminating" (stderr, or a non-JSON stdout
    line), or a background task the stream reported killed after the model's last message."""
    said = None
    for line in [*str(stderr_text or "").splitlines(), *st.non_json]:
        if BG_KILLED_RE.search(line):
            said = re.split(r"(?<=\.)\s+", line.strip())[0][:200]   # not the "set it to 0" advice
            break
    killed = st.end_of_run_kills()
    if not said and not killed:
        return None
    what = ("background task(s) " + ", ".join(f'"{k}"' for k in killed[:3])
            + (f" (+{len(killed) - 3} more)" if len(killed) > 3 else "")) if killed else "background task(s)"
    return (f"claude.exe killed {what} when the run ended, so that work is lost"
            + (f' (claude.exe: "{said}")' if said else ""))


def classify(
    st: _AttemptState,
    exit_code: int | None,
    stderr_text: str,
    timed_out: bool,
    item_id: str,
    clock: Clock,
    poll_min: float,
    elapsed_ms: int = 0,
    log_path: Path | None = None,
) -> Outcome:
    res = st.result or {}
    is_error = bool(res.get("is_error"))
    subtype = res.get("subtype")
    result_text = res.get("result") if isinstance(res.get("result"), str) else None
    # (2026-10-21, v049 dontask_hold) each denial gets its `kind` (denials.annotate): "hook" (a note,
    # never a block), "mode" (don't ask mode), "asyncAgent", ...; without a result event the stream's
    # permission_denied events stand in for result.permission_denials
    raw = (res.get("permission_denials") or []) if st.result is not None else st.event_denials()
    denials = annotate(raw, st.denial_reasons, st.hook_ids)
    if denials:
        log.warning("item %s: %d permission denial(s): %s", item_id, len(denials),
                    [f"{d.get('tool_name')} ({d.get('kind')})" for d in denials])
    out = Outcome(
        FAILED, exit_code=exit_code, session_id=res.get("session_id") or st.session_id, subtype=subtype,
        result_text=result_text, cost_usd=res.get("total_cost_usd", res.get("cost_usd")),
        num_turns=res.get("num_turns"), denials=denials, elapsed_ms=elapsed_ms, log_path=log_path,
    )
    out.bg_killed = bg_killed_reason(st, stderr_text)
    if out.bg_killed:
        log.warning("item %s: %s", item_id, out.bg_killed)

    reasons = list(st.limit_reasons)
    if res.get("terminal_reason") == "blocking_limit":
        reasons.append("result.terminal_reason=blocking_limit")
    texts = list(st.limit_texts)
    # (2026-10-13) Result text is only scanned when the result is an error: a successful task whose
    # answer merely talks about "usage limit reached" must not be parked as LIMIT (see DECISIONS.md).
    if result_text and (is_error or (subtype and subtype != "success")):
        texts.append(result_text)
        if text_says_limit(result_text):
            reasons.append("result text")
    if text_says_limit(stderr_text):
        reasons.append("stderr text")
        texts.append(stderr_text)
    for line in st.non_json:
        if text_says_limit(line):
            reasons.append("stdout text")
            texts.append(line)

    if reasons and not timed_out:
        now = clock.now()
        reset_at, source = compute_reset(st.resets_at, texts, now, clock.local_tz(), poll_min)
        out.kind, out.reset_at, out.reset_source = LIMIT, reset_at, source
        out.error = "usage limit: " + ", ".join(reasons)
        log.warning("item %s: LIMIT (%s); resets %s via %s", item_id, ", ".join(reasons),
                    reset_at.isoformat(), source)
        return out
    if timed_out:
        out.error = "timeout"
        return out
    if st.result is not None:
        if subtype == "success" and not is_error:
            out.kind = DONE
            return out
        out.error = f"result subtype={subtype} is_error={is_error}: {(result_text or '')[:500]}"
        return out
    tail = stderr_text.strip()[-2000:]
    if exit_code not in (0, None):
        out.error = f"exit code {exit_code} without a result event; stderr: {tail}"
    else:
        out.error = f"no result event (exit code {exit_code}); stderr: {tail}"
    return out

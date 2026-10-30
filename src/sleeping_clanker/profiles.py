"""Permission profiles (2026-10-13, spec section 6).

Tool lists are always ONE comma-joined value: --tools/--allowedTools/--disallowedTools are variadic
and would otherwise swallow following positionals.

(2026-10-13) Deviation verified against claude.exe 2.1.278 (see DECISIONS.md): with
`--permission-mode dontAsk` alone, Write is denied ("running in don't ask mode"), so the spec's
`safe` profile could not edit anything. `--allowedTools Read,Glob,Grep,Edit(./**),Write(./**)`
pre-approves edits inside the task cwd only; a write to ../ was still denied in the probe.
`edits` also pre-approves Read/Glob/Grep so a long-prompt file outside cwd stays readable.

(2026-10-13) Both profiles add --strict-mcp-config: without it the init event listed the claude.ai
Gmail/Docs MCP tools (incl. send_message); with it the real init tools list was exactly
[Edit, Glob, Grep, Read, Write] and mcp_servers was [].

(2026-10-13, SPEC_GOALS section 4) `plan`: Claude's plan mode with read-only built-ins only. Verified
against claude.exe 2.1.278 from an empty scratch dir ("Make a 3-step plan to add a README. Do not
write files."): exit 0, result subtype success, the plan came back as result.result, init tools were
exactly [Glob, Grep, Read] and permissionMode was plan. ExitPlanMode is not in the tool list, so the
run ends on plain text, never on a denied ExitPlanMode; an attempted Write was refused as "not
available" and no file was written. See DECISIONS.md.

(2026-10-15, SPEC_V03 section 5.3) `shell-read`: `safe`'s file access plus a read-only shell command
allowlist. Rule syntax verified against claude.exe 2.1.278 with real probes (DECISIONS.md 0.3.0):
`PowerShell(ls:*)` and `PowerShell(git status *)` both allow the command (with arguments), while a
compound `ls && rm x.txt`, `Get-ChildItem; Remove-Item x.txt` and `Get-ChildItem x.txt | Remove-Item`
are denied as a whole (x.txt survived). On this Windows install the Bash tool is not offered at all
(init tools had PowerShell only), so the Bash rules are kept for a machine where it is.
Deviations from the spec's list, for the "no deleting" promise: `git branch` is only `--list` and
`--show-current` (plain `git branch -D x` deletes); `rg` is left out (`rg --pre=CMD` runs a command);
Edit/Write into `.git/` is disallowed (a hooks/config edit would make `git status` run code).

(2026-10-15, 0.3.1 security fix, DECISIONS.md 0.3.1) The 0.3.0 prefix rules were unsafe: `git log`,
`git diff` and `git show` take `--output=<path>` (verified: `git log -1 --output=out.txt` RAN under the
0.3.0 argv and wrote out.txt), which can overwrite any file, `.git/config` included, and so run code;
`git remote -v:*` matched `git remote -v add ...`; `where` is PowerShell's alias of Where-Object,
whose script block runs code. Now, safe by construction:
- every git rule is an EXACT command (no `:*`), and so are `python --version`, `node --version`,
  `Get-Location`; the Bash rules are exact too (the Bash tool is unprobed: it does not exist here);
- prefix (`:*`) rules remain only for PowerShell read-only cmdlets/aliases and findstr, none of
  which has an output or exec parameter; claude.exe denied `Get-ChildItem ([IO.File]::Delete('x.txt'))`
  (x.txt survived), `$(...)`, `@(...)`, a script-block argument in a pipeline of two allowed
  cmdlets, and `findstr (...)`: all denied before running (DECISIONS.md 0.3.1);
- `where`, `which`, `head`, `tail`, `wc`, `grep` are gone (`where` runs code; the rest do not exist
  in PowerShell on this machine);
- the profile appends SHELL_READ_SYSTEM_PROMPT (the exact list; anything else is denied, report
  TASK_BLOCKED). A user's own `--append-system-prompt` extra is merged after it (runner.build_argv).
UNC paths (two leading backslashes, then host and share): Windows would open an SMB connection to that
host, which can leak the account's NTLM hash. (2026-10-27, refuted perm_creds-10) claude.exe itself refuses
them: 2.1.289, 2.1.304 and 2.1.310 check for a UNC path in their read, shell and path checks ("UNC path
detected (defense-in-depth check)"), and under dontAsk that ask is a refusal (security_final
perm_creds_verify_claudeexe_unc2.txt). The shell profiles' system prompt forbids them as well.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

DEFAULT_PROFILE = "safe"

# (2026-10-20, 0.4.4 logfix E) Every tool that can run a command, start an agent or schedule work. A
# profile WITHOUT a shell (safe, edits, plan) offers none of them through --tools (a fixed list) AND
# denies them all through --disallowedTools, so a missing or widened --tools (an old core, a
# Claude Code that adds a tool) still cannot open a shell. Finding (errlog_v05 #2): under the 0.4.0
# argv (no --tools) `Monitor {"command": "echo ...; pwd"}` RAN in an `edits` task.
COMMAND_TOOLS = (
    "Bash", "PowerShell", "BashOutput", "KillShell", "Monitor", "Agent", "Task", "TaskOutput",
    "TaskStop", "Workflow", "CronCreate", "CronDelete", "RemoteTrigger", "SendMessage", "Skill",
    "SlashCommand", "EnterWorktree", "ExitWorktree", "ToolSearch", "WebFetch", "WebSearch",
    # (2026-10-21, 0.4.4 tester fixes LOW-3) schedulers, task lists, notebook edits and LSP too
    "CronList", "ScheduleWakeup", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "NotebookEdit",
    "LSP",
)
# (2026-10-21, 0.4.4 tester fixes MED-3/MED-4) no tool may read or write a .env (a repo's keys) or the
# autopilot's own key file. Every profile denies these (deny rules beat allow rules). The `./` rules
# are the same syntax as the probed .git/ rules; `//**/providers.env` (any drive, any folder) is the
# documented absolute form. (2026-10-27, S1 probe, claude.exe 2.1.310, safe argv with the run's data dir) a
# Read of a fake <data dir>\providers.env, <data dir>\queue.json, a project's queue.json and the missing
# ~/.cargo/credentials.toml were all denied while <project>\prompts\p.md stayed readable; under shell-read
# `Get-Content <abs>\providers.env` and `Get-Content <dir>\provider?.env` were denied too.
# (2026-10-27, SEC-M6) The env of a shell profile never holds a provider key (runner.provider_key_refusal),
# but providers.env stays on disk while it runs: these rules deny Read of it, and every key in every
# providers.env candidate is redacted from the run logs (runner.attempt_secrets), exact bytes only.
# (2026-10-21, LOW-A) + `*.env` (secrets.env, prod.env), as build.py's fallback already denies.
KEY_FILE_NAME = "providers.env"   # == providers.ENV_FILE_NAME (a test checks it)
# (2026-10-26, security M3) the user's credential stores, in Claude Code's documented home-relative
# form `~/path`, and the run logs (transcripts) of every project in the DEFAULT data dir
# (%LOCALAPPDATA% = ~/AppData/Local); a data dir moved by CLAUDE_AUTOPILOT_HOME is not covered.
# (2026-10-27, SEC-M7) + Claude Code's own account file, transcripts and backups, the PowerShell history,
# browser profiles, Windows Credentials and DPAPI keys, VS Code's secret storage and more tool logins; the
# data dir (default and moved) is denied by data_dir_deny() below.
# (2026-10-27, SEC-M7 review) of ~/.claude/projects only the transcripts (`<slug>/<session>.jsonl`, subagents'
# `agent-*.jsonl`): claude.exe saves a large tool output as `<slug>/<session>/tool-results/*.txt` and the task
# must Read it back, and a project's auto-memory (`<slug>/memory/*.md`) stays the task's own.
CREDENTIAL_PATHS = ("~/.claude/.credentials.json", "~/.ssh/**", "~/.aws/**", "~/.npmrc",
                    "~/.git-credentials", "~/.docker/config.json", "~/.config/gh/**", "~/.azure/**",
                    "~/.kube/config", "~/AppData/Local/claud_autopilot/**/runs/**",
                    "~/.claude.json", "~/.claude/projects/**/*.jsonl", "~/.claude/backups/**",
                    "~/AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/**",
                    "~/AppData/Roaming/Mozilla/Firefox/Profiles/**", "~/AppData/Local/Google/Chrome/User Data/**",
                    "~/AppData/Local/Microsoft/Edge/User Data/**", "~/AppData/Roaming/Microsoft/Credentials/**",
                    "~/AppData/Local/Microsoft/Credentials/**", "~/AppData/Roaming/Microsoft/Protect/**",
                    "~/AppData/Roaming/Code/User/globalStorage/**", "~/.pypirc", "~/.netrc", "~/_netrc",
                    "~/.gnupg/**", "~/.config/gcloud/**", "~/AppData/Roaming/gcloud/**",
                    "~/.cargo/credentials.toml", "~/.cargo/credentials", "~/.m2/settings.xml",
                    "~/.gradle/gradle.properties", "~/AppData/Roaming/GitHub CLI/**")
# (2026-10-27, SEC-M7) a .env anywhere (a sibling repo, a parent folder, the home folder), not only in ./
ANY_ENV_PATHS = ("//**/.env", "//**/.env.*", "//**/*.env")


# ---- (2026-10-27, SEC-M7 + SEC-L24) the autopilot's data dirs ------------------------------------------------
# No task may write into a data dir (queue.json, shell_allow.json: a task could queue or allow work for
# itself) or read its queues, run logs, logs, reports and allow-lists (other projects' prompts and results).
# prompts/** stays readable: a long prompt or reply is handed to the task as a file there
# (runner.prompt_arg_for). The default dir is denied in the `~/` form below; build_argv adds the `//` form of
# the run's own data dir (CLAUDE_AUTOPILOT_HOME / --data-dir) through with_data_dir_deny().
DATA_DIR_HOME = "~/AppData/Local/claud_autopilot"
DATA_DIR_READ_DENY = ("**/queue.json", "**/queue.json.*", "**/runs/**", "**/logs/**", "**/reports/**",
                      "**/pending_outcomes/**", f"**/{KEY_FILE_NAME}", "**/shell_allow.json",
                      "**/usage_samples.jsonl")
# a path with one of these cannot be written as a rule (a comma or `)` ends it; the rest are glob syntax):
# rule_root writes each as `*`
_RULE_PATH_BAD = re.compile(r"[,()*?\[\]{}]")


def data_dir_deny(root: str) -> list[str]:
    """(2026-10-27, SEC-M7/L24) The deny rules of a data dir given as a rule path (`~/...` or `//...`):
    no Edit/Write anywhere in it, no Read of DATA_DIR_READ_DENY."""
    root = root.rstrip("/")
    return [f"Edit({root}/**)", f"Write({root}/**)", *(f"Read({root}/{p})" for p in DATA_DIR_READ_DENY)]


def rule_root(path: str | os.PathLike, resolve: bool = True) -> str | None:
    """(2026-10-27, SEC-M7/L24) An absolute folder as a rule path: claude.exe matches paths in POSIX form
    (`C:\\x` is `/c/x`, read from claude.exe 2.1.310) and `//` starts an absolute rule, so `C:\\x` becomes
    `//` + `c/x`. None for a UNC path. (2026-10-28, SEC-H2) `resolve=False`: the path as written, made
    absolute (links not followed)."""
    raw = str(path)
    if raw.startswith(("\\\\", "//")):   # UNC: never resolved (that would open the share)
        return None
    try:
        text = str(Path(raw).resolve() if resolve else Path(os.path.abspath(raw))).replace("\\", "/")
    except OSError:
        return None
    m = re.match(r"^([A-Za-z]):(/|$)", text)
    if m:
        text = f"/{m.group(1).lower()}{text[2:]}"
    if text.startswith("//") or not text.startswith("/"):
        return None
    # (2026-10-27, SEC-M7 review 2) a character a rule cannot hold (`Data (2)`, `a,b`, `x[1]`) becomes the
    # `*` wildcard: the rule then matches a little more (fail-closed) instead of the whole deny being dropped
    return "/" + _RULE_PATH_BAD.sub("*", text).rstrip("/")


ENV_FILE_DENY = ",".join([*(
    f"{tool}({pat})" for tool in ("Read", "Edit", "Write")
    for pat in ("./.env", "./.env.*", "./**/.env", "./**/.env.*", "./*.env", "./**/*.env",
                f"./**/{KEY_FILE_NAME}", f"//**/{KEY_FILE_NAME}", *CREDENTIAL_PATHS, *ANY_ENV_PATHS)),
    *data_dir_deny(DATA_DIR_HOME)])
# (2026-10-27, SEC-L12) no profile without a shell may write repo config that runs code later: .git/
# (hooks, core.fsmonitor), .claude/ (settings hooks) or .vscode/ (tasks.json runOn folderOpen). claude.exe
# 2.1.310 already refused these writes itself (security_final verify_exec_injection_safe_write_probe);
# these rules are defence in depth. The shell profiles add the same (SHELL_READ_DISALLOWED).
# (2026-10-27, tasks at once, SPEC_V05_PARALLEL 7.1) + Edit/Write(./.git): in a private copy .git is a FILE (the
# gitdir pointer into the shadow); Shadow.verify repairs it before every attempt and merge anyway
GIT_DIR_DENY = "Edit(./.git/**),Write(./.git/**),Edit(./**/.git/**),Write(./**/.git/**),Edit(./.git),Write(./.git)"
# (2026-10-27, SEC-L12 review 2) + ./**/.vscode/**: a nested folder opened on its own reads its .vscode too
CONFIG_DIR_DENY = ",".join(f"{tool}({pat})" for pat in ("./.claude/**", "./**/.claude/**", "./.vscode/**",
                                                        "./**/.vscode/**")
                           for tool in ("Edit", "Write"))
# (2026-10-27, SEC-M7 review 2) claude.exe 2.1.310 matches a rule against the path as the task writes it and
# does not expand 8.3 short names: a Read of the queue file's short-name form (`LONGNA~1` for the user
# folder, `QUEUE~1.JSO` for queue.json) passed every data-dir rule
# (probe, NOTES "S1 R2 PROBE"). So no profile with permission checks may Read/Edit/Write a path with a
# `~<digit>` segment (the short-name form); `[0-9]` is honoured (same probe). Not in `full`: its shell reads
# any file anyway and %TEMP% is often an 8.3 path. The autopilot resolves every task folder (long form).
SHORT_NAME_PATHS = ("//**/*~[0-9]*", "//**/*~[0-9]*/**")
# (2026-10-27, SEC-M7 review 5) the same holds for an NTFS stream suffix: Read of `<data>\queue.json::$DATA`
# and `<proj>\.env::$DATA` returned the file past every name rule, and `<dir>::$INDEX_ALLOCATION\<file>`
# (also `:$I30:$INDEX_ALLOCATION`) opens a file inside a folder under another folder name (live probe, NOTES
# "S1 R2 TEST LIVE"). A Windows file name cannot hold `:` and the drive's colon is gone in the POSIX form
# (/c/...), so no profile with permission checks may Read/Edit/Write a path with a `:` in any segment.
STREAM_PATHS = ("//**/*:*", "//**/*:*/**")
SHORT_NAME_DENY = ",".join(f"{tool}({pat})" for pat in SHORT_NAME_PATHS + STREAM_PATHS
                           for tool in ("Read", "Edit", "Write"))
NO_SHELL_DISALLOWED = ",".join([*COMMAND_TOOLS, GIT_DIR_DENY, CONFIG_DIR_DENY, ENV_FILE_DENY, SHORT_NAME_DENY])
# the tools a no-shell profile may offer at all (tests check every --tools value against it)
NO_SHELL_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
NO_SHELL_PROFILES = ("safe", "edits", "plan")

# (2026-10-15, 0.3.1) EXACT commands: matched as a whole, never as a prefix. Each is safe as written.
# (2026-10-27, SEC-L19 review 3) the two patch-printing diffs carry --no-ext-diff --no-textconv: a repo's
# .git/config can name a diff.external, diff.<driver>.command or .textconv program that a plain `git diff`
# runs (verified with git 2.43; the --stat forms and log/show --stat ran none). An exact rule cannot be
# extended with `--ext-diff`.
SHELL_READ_GIT_EXACT = (
    "git status", "git status --short", "git diff --no-ext-diff --no-textconv", "git diff --stat",
    "git diff --cached --no-ext-diff --no-textconv",
    "git log --oneline -20", "git log --oneline -50", "git log -5 --stat", "git show --stat HEAD",
    "git branch --list", "git branch --show-current", "git remote -v", "git rev-parse HEAD",
    "git ls-files",
)
SHELL_READ_OTHER_EXACT = ("python --version", "node --version")
SHELL_READ_PS_EXACT = ("Get-Location",)
# (2026-10-15, 0.3.1) PowerShell read-only cmdlets/aliases that may take arguments (`:*`). None has an
# output, write or exec parameter; never add `where` (Where-Object runs a script block).
SHELL_READ_PS_PREFIX = ("Get-ChildItem", "Get-Content", "Select-String", "Test-Path", "Get-Item",
                        "ls", "dir", "cat", "type", "findstr")
SHELL_READ_EXACT = SHELL_READ_GIT_EXACT + SHELL_READ_OTHER_EXACT


def _shell_read_allowed() -> str:
    rules = ["Read", "Glob", "Grep", "Edit(./**)", "Write(./**)"]
    rules += [f"Bash({c})" for c in SHELL_READ_EXACT]
    rules += [f"PowerShell({c})" for c in SHELL_READ_EXACT + SHELL_READ_PS_EXACT]
    rules += [f"PowerShell({c}:*)" for c in SHELL_READ_PS_PREFIX]
    return ",".join(rules)


def _code(cmds: tuple[str, ...]) -> str:
    return ", ".join(f"`{c}`" for c in cmds)


# (2026-10-15, 0.3.1) tells Claude the exact forms up front so it does not burn turns on denials
SHELL_READ_SYSTEM_PROMPT = (
    "Permission profile shell-read. Shell commands run through the PowerShell tool, one command per "
    "call, and only these are allowed:\n"
    "1. Exactly as written, with no extra arguments: "
    + _code(SHELL_READ_EXACT + SHELL_READ_PS_EXACT) + ".\n"
    "2. With plain literal arguments (paths, patterns, switches such as -Recurse): "
    + _code(SHELL_READ_PS_PREFIX) + ". No parentheses, `$`, `@`, braces, pipes, `;`, `&&`, "
    r"redirection, or UNC (\\host\share) paths." "\n"
    "Every other command, and every other form of these commands, will be denied. Use the Read, "
    "Glob and Grep tools to read and search files. If the task needs a command that is not allowed, "
    "do not look for a workaround: stop and report TASK_BLOCKED (GOAL_BLOCKED in a goal), naming "
    "the command you need."
)

# (2026-10-20, 0.4.4 logfix A, probe 2) The Bash
# tool is offered since the Git Bash hotfix, and claude.exe 2.1.304 ALLOWED `git --version > b3.txt`
# under `Bash(git --version:*)`: the matcher ignores an output redirection, so an allowed Bash command
# could overwrite any file (.git/config, the allow-list file). `&&`, `&`, `;`, backticks and `$()` were
# denied. PowerShell denied `>` (0.3.0 probe 4) and `|`, `;`, `$()` (0.4.4 logfix probe 1). So the shell
# profiles offer the PowerShell tool only and deny Bash; the Bash rules stay as dead letters.
SHELL_TOOLS = "Read,Edit,Write,Glob,Grep,PowerShell"
# (2026-10-27, SEC-L12) GIT_DIR_DENY is defined above, next to CONFIG_DIR_DENY
# every command-capable tool but PowerShell (Bash included, see above)
_SHELL_TOOL_DENY = ",".join(t for t in COMMAND_TOOLS if t != "PowerShell")
SHELL_READ_DISALLOWED = f"{GIT_DIR_DENY},{_SHELL_TOOL_DENY},{ENV_FILE_DENY},{CONFIG_DIR_DENY},{SHORT_NAME_DENY}"

PROFILES: dict[str, list[str]] = {
    "safe": [
        "--permission-mode", "dontAsk",
        "--tools", "Read,Edit,Write,Glob,Grep",
        "--allowedTools", "Read,Glob,Grep,Edit(./**),Write(./**)",
        "--disallowedTools", NO_SHELL_DISALLOWED,  # (2026-10-20, 0.4.4 logfix E) defense in depth
        "--permission-prompts", "none",
        "--strict-mcp-config",
    ],
    "edits": [
        "--permission-mode", "acceptEdits",
        # (2026-10-17, 0.4.1 R2) a fixed tool list: without it 24 tools were offered (+14.3k tokens
        # per message) and Claude started background agents/Monitor that died with the run.
        "--tools", "Read,Edit,Write,Glob,Grep",
        # (2026-10-20, 0.4.4 logfix E) was "Bash,PowerShell,WebFetch,WebSearch": Monitor & co. were missing
        "--disallowedTools", NO_SHELL_DISALLOWED,
        "--allowedTools", "Read,Glob,Grep",
        "--permission-prompts", "none",
        "--strict-mcp-config",
    ],
    "plan": [
        "--permission-mode", "plan",
        "--tools", "Read,Glob,Grep",
        "--allowedTools", "Read,Glob,Grep",
        "--disallowedTools", NO_SHELL_DISALLOWED,  # (2026-10-20, 0.4.4 logfix E) defense in depth
        "--permission-prompts", "none",
        "--strict-mcp-config",
    ],
    "shell-read": [
        "--permission-mode", "dontAsk",
        "--tools", SHELL_TOOLS,  # (2026-10-20, 0.4.4 logfix A) no Bash: see SHELL_TOOLS
        "--allowedTools", _shell_read_allowed(),
        # (2026-10-15, 0.3.2) also nested .git dirs (old-style submodules): `git status` recurses into them.
        # (2026-10-20, 0.4.4 logfix A/E) + Bash and every other command-capable tool
        "--disallowedTools", SHELL_READ_DISALLOWED,
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--append-system-prompt", SHELL_READ_SYSTEM_PROMPT,
    ],
}



# ---- (2026-10-20, 0.4.4 logfix A) `shell-dev`: shell-read plus the project's own allow-list ----------------
SHELL_DEV = "shell-dev"
# The allow-list lives in the autopilot data dir of the project (next to queue.json), NEVER in the
# repo: a task that can edit repo files must not be able to grant itself commands.
SHELL_ALLOW_FILE = "shell_allow.json"
SHELL_ALLOW_MAX = 50
SHELL_ALLOW_ENTRY_MAX = 120

# HARD DENY, always, even when the allow-list has the command (claude.exe applies deny rules
# before allow rules). Why each group:
# - git: these publish work (push), destroy uncommitted work or history (reset --hard, clean,
#   checkout --, branch -D, rebase, filter-*), or change what later git commands run (config,
#   remote, -c: hooksPath/alias/credential helpers run arbitrary programs);
# - delete: recursive or whole-volume deletes cannot be undone (Edit/Write never delete);
# - privilege: execution policy, the registry, services, scheduled tasks and runas change the
#   machine, not the project;
# - fetching: downloads and remote shells move data in or out of the machine;
# - installing/publishing: changes the machine or publishes the owner's work under their name;
# - exec: an interpreter or launcher given a command string runs ANY command, so it would turn one
#   allowed prefix into all of them.
SHELL_DEV_DENY = (
    # git
    "git push", "git reset --hard", "git clean", "git checkout --", "git branch -D",
    "git branch --delete", "git rebase", "git filter-branch", "git filter-repo", "git config",
    "git remote", "git -c", "git submodule foreach",
    # delete
    "rm -r", "rm -rf", "rm -fr", "rm -R", "rm --recursive", "Remove-Item -Recurse", "del /s",
    "rd /s", "rmdir /s", "format", "Format-Volume",
    # privilege
    "Set-ExecutionPolicy", "reg", "sc", "schtasks", "runas",
    # fetching
    "curl", "wget", "Invoke-WebRequest", "iwr", "Invoke-RestMethod", "irm", "Start-BitsTransfer",
    "bitsadmin", "certutil", "ssh", "scp", "sftp",
    # installing / publishing
    "npm publish", "vsce publish", "pip install", "pip3 install", "python -m pip", "py -m pip",
    "npm install", "npm i", "npm ci", "npx", "winget", "choco", "Install-Module", "Install-Package",
    "code --install-extension",
    # exec
    "Invoke-Expression", "iex", "Start-Process", "cmd", "powershell", "pwsh", "bash", "sh", "wsl",
    "python -c", "py -c", "node -e", "node --eval",
    # (2026-10-21, 0.4.4 tester fixes MED-2) aliases and spellings the tester got through
    "git restore", "git switch", "git stash", "git worktree", "git -C", "git -P", "git --no-pager",
    "git --git-dir", "git --work-tree", "git --exec-path", "erase", "ri", "rm", "del", "rd",
    "Remove-Item", "npm exec", "npm x", "pnpm dlx", "pnpm exec", "yarn dlx", "bunx", "pnpx", "uvx",
    "saps", "start", "Invoke-Command", "icm", "Start-Job", "sajb", "Start-ThreadJob", "Invoke-Item",
    "ii", "python3 -m pip", "python3 -c", "node -p", "node --print",
)

# ---- (2026-10-21, 0.4.4 tester fixes MED-2) what an allow-list entry may be ------------------------------
# An entry becomes a PREFIX rule (`PowerShell(<entry>:*)`), so ANY text may follow it: an entry is judged by
# what it lets through, not by what it names. Every word below is compared lower-case unless it says so.
# 1. DENIED_PROGRAMS: refused as the first word whatever follows (`rm build` lets `rm build -r -fo` run),
#    aliases included (erase, ri, saps, start, icm, ii, ...).
# 2. git: the second word must be a SUBCOMMAND, never an option (`git --no-pager`, `git -P`, `git -C dir`,
#    `git --git-dir=.git` let ANY subcommand follow, push included), and not one in GIT_DENIED_SUBCOMMANDS
#    (each has a destructive form reachable by appending words: `git checkout main -- .`).
# 3. package managers / launchers (npm, pnpm, yarn, bun, pip, uv, ...): a subcommand is required, it must not
#    be an option or in their deny set (install, exec, publish, dlx, ...); `<tool> run <cmd>` launchers check
#    <cmd> as an entry of its own.
# 4. interpreters: must name a script or a module before anything else could follow, and no code flag may come
#    first (python: any short-option cluster with `c`, CASE-SENSITIVE so `-E` stays fine; node: -e/-p/--eval/
#    --print; `-m` of a module runner such as pip, runpy, pdb, cProfile, timeit, http). Other interpreters must
#    start with a script (no option at all).
# 5. denied_by(): no prefix relation with SHELL_DEV_DENY (these also become claude.exe deny rules).
# The checks run when the owner adds an entry (`shell-allow add`, exit 2 with the reason) AND every time the
# file is loaded (load_allow_list drops a failing entry: a hand edit only takes commands away).
DENIED_PROGRAMS = frozenset({
    # delete (Remove-Item and every alias; Edit/Write never delete)
    "rm", "del", "erase", "rd", "ri", "rmdir", "remove-item", "rimraf", "format", "format-volume",
    "diskpart", "clear-recyclebin",
    # privilege / machine state
    "set-executionpolicy", "reg", "regedit", "sc", "schtasks", "runas", "net", "netsh", "bcdedit",
    "takeown", "icacls", "set-acl", "new-service", "set-service", "remove-service",
    "register-scheduledtask", "unregister-scheduledtask", "new-itemproperty", "set-itemproperty",
    "remove-itemproperty", "stop-computer", "restart-computer", "shutdown",
    # fetching / remote
    "curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "start-bitstransfer",
    "bitsadmin", "certutil", "ssh", "scp", "sftp", "ftp", "tftp", "telnet", "nc", "ncat",
    "send-mailmessage",
    # installing / publishing
    "winget", "choco", "scoop", "install-module", "install-package", "install-script", "msiexec",
    "npx", "pnpx", "bunx", "uvx", "pipx", "code", "code-insiders",
    # exec: anything that runs a command string, a script block or another program
    "invoke-expression", "iex", "invoke-command", "icm", "start-job", "sajb", "start-threadjob",
    "start-process", "saps", "start", "invoke-item", "ii", "cmd", "powershell", "pwsh", "bash", "sh",
    "wsl", "wscript", "cscript", "mshta", "rundll32", "regsvr32", "add-type", "foreach-object", "foreach",
    "where-object", "where", "invoke-wmimethod", "invoke-cimmethod", "forfiles", "at", "env", "xargs",
    ".",
    # (2026-10-27, SEC-M5) network and cloud CLIs that act with stored logins (a gist, a bucket, a push)
    "gh", "hub", "glab", "aws", "az", "gcloud", "gsutil", "bq", "docker", "docker-compose", "podman",
    "kubectl", "helm", "terraform", "tofu", "pulumi", "heroku", "vercel", "netlify", "firebase", "flyctl",
    "fly", "doctl", "wrangler", "railway", "supabase", "rclone", "azcopy", "s3cmd", "ngrok",
    "cloudflared", "tailscale", "git-lfs",
    # DNS, ping and connection tools (a lookup of a crafted name sends data out)
    "nslookup", "resolve-dnsname", "ping", "test-netconnection", "tnc", "test-connection", "tracert",
    "traceroute", "pathping", "dig", "host", "whois", "route",
    # open a URL, a file or a program (`explorer <url>`, Start-Process' kin)
    "explorer", "rundll", "hh", "msedge", "chrome", "firefox", "open", "xdg-open",
    # file copiers, movers, writers, linkers and archivers: they write OUTSIDE the folder (.git/config, the
    # data dir, ~/.claude/settings.json, a UNC share), where Edit/Write(./**) cannot
    "copy-item", "cpi", "copy", "cp", "xcopy", "robocopy", "move-item", "mi", "move", "mv", "rename-item",
    "rni", "ren", "rename", "set-content", "add-content", "ac", "out-file", "new-item", "ni", "mkdir", "md",
    "set-item", "si", "clear-content", "clc", "tee-object", "tee", "export-csv", "export-clixml",
    "mklink", "new-symlink", "fsutil", "tar", "expand-archive", "compress-archive", "7z", "7za", "zip",
    "unzip", "makecab", "expand", "extrac32", "esentutl",
    # the autopilot and Claude Code themselves (`autopilot add --profile full`, `claude -p ...`)
    "autopilot", "sleeping-clanker", "sleeping_clanker", "claud-autopilot", "claud_autopilot", "claude",
    "sleeping-claud", "sleeping_claud",   # (2026-10-29, Clanker rename) the names before the rename
    # (2026-10-27, SEC-M5 review) the aliases and kin of the writers and registry cmdlets above (`sp` is
    # Set-ItemProperty, `epcsv` Export-Csv), a transcript to any path, module/help downloads and publishing,
    # twine (~/.pypirc), rsync, certreq -Post, and launchers that run any program (like `start`)
    "sp", "cpp", "mp", "clp", "rp", "rnp", "copy-itemproperty", "move-itemproperty", "clear-itemproperty",
    "rename-itemproperty", "epcsv", "epal", "export-alias", "start-transcript", "save-module", "save-script",
    "save-help", "update-help", "find-module", "find-script", "publish-module", "publish-script",
    "update-module", "twine", "rsync", "certreq", "iexplore", "mstsc", "wt", "conhost",
    # (2026-10-27, SEC-M5 review 2) aliases of cmdlets whose verb _PS_DENIED_VERBS refuses (New-PSDrive,
    # New-PSSession, Enter-PSSession, Import-Module, Set-Clipboard, Invoke-History, Stop-Process ...), remote
    # WMI/CIM queries, the clipboard, Windows tools that write machine state or reach another machine, and
    # database/remote clients that act with stored logins
    "ndr", "mount", "rdr", "nsn", "etsn", "exsn", "cnsn", "dnsn", "rcsn", "rsn", "ipsn", "epsn", "gsn",
    "ipmo", "nmo", "rmo", "ipal", "nal", "sal", "set", "sv", "nv", "rv", "clv", "scb", "gcb", "clip",
    "ihy", "r", "kill", "spps", "sasv", "spsv", "spjb", "rjb", "rcjb", "sujb", "rujb", "sbp", "rbp", "trcm",
    "iwmi", "swmi", "rwmi", "gwmi", "gcim", "asnp", "npssc", "lp", "ogv", "shcm", "ise", "cli",
    "get-ciminstance", "get-cimclass", "get-cimassociatedinstance", "get-cimsession", "get-wmiobject",
    "get-wsmaninstance", "get-pssession", "get-clipboard", "get-credential", "measure-command",
    "test-wsman", "test-computersecurechannel", "out-printer", "out-gridview", "write-eventlog",
    "subst", "setx", "wmic", "winrm", "winrs", "psexec", "psexec64", "cmdkey", "vaultcmd", "mountvol",
    "openssl", "gpg", "hg", "svn", "fossil", "bzr", "nuget", "psql", "mysql", "sqlcmd", "mongo", "mongosh",
    "redis-cli",
})
# (2026-10-27, SEC-M5 review 2) A PowerShell `<verb>-<noun>` cmdlet whose verb changes state, writes a file,
# reaches another machine or runs code is refused whatever its noun (New-Object, New-ModuleManifest,
# Export-PfxCertificate, Enter-PSSession, Register-PSRepository, Set-AuthenticodeSignature, Import-Module ...);
# a blocklist of names could never be complete. _PS_VERB_OK: test/build runners kept usable (like pytest,
# they run the code they are pointed at; the owner allows them knowingly) and pipeline/location cmdlets that
# change nothing outside the session. Out-* and Write-* are not verbs here (Out-Null, Write-Output are common
# pipe ends): Out-File/Out-Printer/Out-GridView and Write-EventLog are named instead.
_PS_DENIED_VERBS = frozenset({
    "add", "approve", "backup", "block", "checkpoint", "clear", "close", "compress", "connect", "copy", "debug",
    "deny", "deploy", "disable", "disconnect", "dismount", "edit", "enable", "enter", "exit", "expand",
    "export", "find", "grant", "import", "initialize", "install", "invoke", "lock", "mount", "move", "new",
    "open", "protect", "publish", "push", "receive", "register", "remove", "rename", "repair",
    "request", "reset", "restart", "restore", "resume", "revoke", "save", "send", "set", "start", "stop",
    "submit", "suspend", "switch", "sync", "trace", "unblock", "uninstall", "unlock", "unprotect",
    "unpublish", "unregister", "update", "use",
})
_PS_VERB_OK = frozenset({"invoke-pester", "invoke-build", "invoke-psake", "invoke-scriptanalyzer",
                         "start-sleep", "clear-host", "set-location", "push-location", "set-strictmode"})
_PS_CMDLET_RE = re.compile(r"^([a-z]+)-[a-z0-9]")
# (2026-10-27, SEC-M5 review 4) cmdlets that run a script block they are given (a calculated property
# `@{e={...}}`, -Property {...}, -FilterScript, -Process): every `*-Object` cmdlet (ForEach-, Where-, Select-,
# Sort-, Group-, Compare-, Measure-, Tee-, New-), every `Format-*` cmdlet, ConvertTo-Html, and their aliases
_PS_SCRIPT_BLOCK_RE = re.compile(r"^([a-z]+-object|format-[a-z0-9]+|convertto-html)$")
_PS_SCRIPT_BLOCK_ALIASES = frozenset({"select", "sort", "group", "compare", "diff", "measure", "ft", "fl", "fw",
                                      "fc", "fhx"})
# (2026-10-27, SEC-M5 review 4) A program that ships with Windows (a file in the Windows folder, System32,
# SysWOW64 or their Wbem/PowerShell/OpenSSH folders) is refused whatever its name, unless it is one of these
# read-only tools: every review found more of them that copy files (replace, print), change ACLs or keys
# (cacls, cipher) or reach another machine (finger, w32tm, nbtstat, nltest), and Windows ships hundreds.
# py/pyw live there too: they are judged as python first. Known limit: a program elsewhere on PATH (Git's
# usr\bin, a tools folder) is judged by its name only.
_WINDOWS_TOOL_DIRS = ("System32", "SysWOW64", "", "System32/Wbem", "System32/WindowsPowerShell/v1.0",
                      "System32/OpenSSH")
_WINDOWS_TOOL_OK = frozenset({"findstr", "find", "more", "tree", "whoami", "hostname", "chcp", "timeout", "comp"})
# (2026-10-27, SEC-M5 review 4) git runs any `git-<name>` program on PATH and any alias for an unknown
# subcommand, and every review found more built-in subcommands that write any path, read any file into the
# repo or reach the network (interpret-trailers --in-place, commit-graph/multi-pack-index --object-dir,
# hash-object -w, notes add -F). So the subcommand must be one of these (any case, like the deny check):
# work in the folder's own repository and read-only queries. GIT_DENIED_SUBCOMMANDS still names the reason
# for the known dangerous ones. Known limit: commit/merge/tag -F read a file named after them.
GIT_ALLOWED_SUBCOMMANDS = frozenset({
    "status", "add", "commit", "merge", "mv", "cherry-pick", "revert", "tag",
    "describe", "rev-parse", "ls-files", "ls-tree", "cat-file", "merge-base", "show-ref", "for-each-ref",
    "name-rev", "count-objects", "fsck", "check-ignore", "check-attr", "check-ref-format", "show-branch",
    "cherry", "version", "var", "stripspace", "patch-id",
})
GIT_DENIED_SUBCOMMANDS = frozenset({
    "push", "reset", "clean", "checkout", "restore", "switch", "branch", "rebase", "filter-branch",
    "filter-repo", "config", "remote", "submodule", "stash", "worktree", "gc", "prune", "reflog",
    "update-ref", "rm", "bisect", "send-email", "credential", "credential-manager", "credential-cache",
    "credential-store", "daemon", "instaweb", "maintenance", "hook",
    # --output/-o write any path (0.3.1: `git log --output=.git/config`); shell-read has their exact forms
    "log", "diff", "show", "whatchanged", "format-patch", "archive", "bundle",
    # run a command (`grep -O<cmd>`, `difftool -x`, `--upload-pack=<cmd>`) or reach the network (fetching)
    "grep", "difftool", "mergetool", "fetch", "pull", "clone", "ls-remote", "upload-pack",
    "receive-pack", "shell", "svn", "p4",
    # (2026-10-27, SEC-M5 review) push/fetch plumbing and remote helpers (`git lfs push`: git-lfs itself is
    # denied above), `apply --unsafe-paths` (writes outside the work tree), `init <any path> --template`
    "lfs", "send-pack", "http-push", "fetch-pack", "http-fetch", "remote-http", "remote-https", "remote-ext",
    "remote-fd", "apply", "init",
    # (2026-10-27, SEC-M5 review 2) a browser/URL opener (`help -w`, web--browse), network (imap-send,
    # request-pull runs ls-remote, subtree push, annex, the cvs/arch bridges), writers that take any output
    # path (checkout-index --prefix, bugreport/diagnose -o, merge-file <any file>), more --output diff
    # commands, and the GUIs (git gui can push). Every `credential-*` and `remote-*` helper: GIT_DENIED_PREFIXES
    "web--browse", "help", "imap-send", "checkout-index", "bugreport", "diagnose", "merge-file",
    "range-diff", "diff-files", "diff-index", "diff-tree", "request-pull", "subtree", "annex", "http-backend",
    "upload-archive", "cvsimport", "cvsexportcommit", "cvsserver", "archimport", "quiltimport", "gui",
    "citool", "gui--askpass",
    # (2026-10-27, SEC-M5 review 3) more writers that take any path (verified with git 2.43: `rev-list`,
    # `shortlog`, `blame` and `annotate --output=<file>`, `fast-export`/`fast-import --export-marks=<file>`,
    # `mailsplit -o<dir>`, `mailinfo <msg> <patch>`, `pack-objects <base-name>`, `index-pack -o <file>`) and
    # `merge-index <program>`, which runs the program it is given
    "fast-import", "fast-export", "rev-list", "shortlog", "blame", "annotate", "mailsplit", "mailinfo",
    "pack-objects", "index-pack", "merge-index",
})
GIT_DENIED_PREFIXES = ("credential", "remote-")
# a subcommand is required; these ones are refused (installs, downloads+runs, publishes, config)
_NPM_DENY = {"install", "i", "in", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal",
             "isntall", "add", "ci", "clean-install", "ic", "install-clean", "isntall-clean",
             "install-test", "it", "install-ci-test", "cit", "exec", "x", "dlx", "publish", "unpublish",
             "update", "up", "upgrade", "udpate", "link", "ln", "uninstall", "remove", "rm", "r", "un",
             "unlink", "deprecate", "dist-tag", "owner", "access", "adduser", "login", "logout", "token",
             "config", "c", "set", "explore", "init", "create", "global", "npm", "pm", "rebuild"}
PACKAGE_MANAGERS: dict[str, frozenset] = {
    "npm": frozenset(_NPM_DENY), "pnpm": frozenset(_NPM_DENY), "yarn": frozenset(_NPM_DENY),
    "bun": frozenset(_NPM_DENY),
    "pip": frozenset({"install", "uninstall", "download", "wheel", "config"}),
    "pip3": frozenset({"install", "uninstall", "download", "wheel", "config"}),
    "uv": frozenset({"pip", "add", "remove", "tool", "python", "self", "sync", "init", "venv"}),
    "poetry": frozenset({"add", "remove", "install", "update", "self", "publish", "source", "config", "sync"}),
    "pipenv": frozenset({"install", "uninstall", "update", "sync", "shell"}),
    "pdm": frozenset({"add", "remove", "install", "update", "sync", "publish", "self", "config"}),
    "hatch": frozenset({"publish", "env", "python", "self", "config", "shell"}),
    "conda": frozenset({"install", "remove", "uninstall", "update", "upgrade", "create", "env", "config"}),
    "gem": frozenset({"install", "uninstall", "update", "push", "owner", "yank", "signin"}),
    "cargo": frozenset({"install", "uninstall", "publish", "yank", "owner", "login", "logout"}),
    "dotnet": frozenset({"tool", "add", "remove", "nuget", "workload", "new", "sdk", "exec"}),
    "deno": frozenset({"install", "uninstall", "eval", "upgrade", "publish", "repl", "run", "x", "add",
                       "remove", "serve", "jupyter", "compile"}),
    "vsce": frozenset({"publish", "unpublish", "login", "logout", "delete-publisher", "create-publisher"}),
}
# `<tool> run <cmd>` runs <cmd>: <cmd> is checked as an entry of its own
LAUNCHERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "conda": "run"}
# (2026-10-27, SEC-M5 review 4) + pyw (the windowed py launcher), free-threaded (`python3.13t`) and debug
# (`python_d`) builds, pypyw
_PYTHON_RE = re.compile(r"^(python|pythonw|pypy|pypyw)(\d+(\.\d+)*)?t?(_d)?$|^pyw?$")
# (2026-10-27, SEC-M5 review 4) any other name of the Python family (ipython, bpython, ptpython, py3, a
# python-config) is a shell or launcher whose options this check does not know: refused
_PYTHON_FAMILY_RE = re.compile(r"python|^pypy|^pyw?(\d|$)")
_NODE_NAMES = {"node", "nodejs"}
# script-taking interpreters with an eval flag (-e, -r, -E ...): the first word after them must be a script
_SCRIPT_INTERPRETERS = {"ruby", "perl", "php", "lua", "rscript", "r", "julia", "tclsh", "java", "jshell",
                        "groovy", "kotlin", "scala", "osascript", "awk", "gawk", "sed"}
# `python -m <one of these>` installs, serves, or runs another module/script given after it
DENIED_PY_MODULES = frozenset({"pip", "pip3", "ensurepip", "pipx", "uv", "poetry", "runpy", "pdb",
                               "cprofile", "profile", "trace", "timeit", "http", "webbrowser", "pydoc",
                               "smtpd", "idlelib", "code", "venv", "site",
                               # (2026-10-27, SEC-M5) the autopilot itself (`-m sleeping_clanker add --profile
                               # full`), a REPL, network clients, and modules that write any path
                               "sleeping_clanker", "claud_autopilot", "autopilot", "asyncio", "urllib",
                               "sleeping_claud",   # (2026-10-29, Clanker rename) the name before the rename
                               "ftplib", "smtplib", "telnetlib", "xmlrpc", "wsgiref", "zipfile", "tarfile",
                               "gzip", "zipapp", "json", "sqlite3",
                               "twine",    # (2026-10-27, SEC-M5 review) uploads with ~/.pypirc
                               # (2026-10-27, SEC-M5 review 3) the rest of python 3.11's stdlib whose
                               # `-m` entry reaches a host or a web page (imaplib, poplib, nntplib connect to
                               # the host given; antigravity opens a URL), writes a path it is given (lib2to3
                               # -w, uu -d, aifc, pickletools -o, fileinput -i), runs code or a command
                               # (pickle loads a file, mailcap os.system, inspect imports any module, test
                               # -u network), or prints secrets (netrc: ~/.netrc logins; cgi: the environment)
                               "imaplib", "poplib", "nntplib", "antigravity", "lib2to3", "uu", "aifc",
                               "pickletools", "fileinput", "pickle", "mailcap", "inspect", "test", "netrc",
                               "cgi"})
# (2026-10-27, SEC-M5 review 2) python's long options without an argument (they print and exit)
_PY_LONG_FLAGS = frozenset({"--version", "--help", "--help-env", "--help-xoptions", "--help-all"})
_NODE_CODE = {"-e", "--eval", "-p", "--print", "-i", "--interactive", "-"}
_NODE_ARG_OPTS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-C", "--conditions"}
# A literal command prefix: letters, digits, space and . _ \ / : = + -. No quotes, `$`, `;`, `&`,
# `|`, `<`, `>`, backticks, `*`, parentheses or commas (a comma or `)` would end the permission rule
# and start a new one inside --allowedTools).
_ENTRY_RE = re.compile(r"^[A-Za-z0-9.][A-Za-z0-9 ._\\/:=+-]*$")


class AllowListError(ValueError):
    pass


def _exe_name(word: str) -> str:
    """`.venv\\Scripts\\python.exe` -> `python` (lower case, no folder, no .exe/.cmd/.bat).

    (2026-10-27, SEC-M5 review 2) Fail-closed for Windows spellings of the same program: trailing dots and
    spaces (Windows drops them: `gh.exe.`), a drive-relative `C:gh.exe`, an NTFS stream suffix (`gh.exe:x`),
    and the .com/.ps1 forms (npm installs `npx.ps1` next to npx.cmd), repeated until nothing changes."""
    exe = re.split(r"[\\/]", word)[-1].lower()
    exe = re.sub(r"^[a-z]:", "", exe).split(":")[0] or exe
    while True:
        name = exe.rstrip(". ") or exe   # `.` (dot-sourcing) stays `.`
        if name.endswith((".exe", ".cmd", ".bat", ".com", ".ps1")):
            name = name[:-4]
        if name == exe or not name:
            return exe
        exe = name


def _words(cmd: str) -> list[str]:
    """The words compared with SHELL_DEV_DENY: the program by name (lower case), the rest lower case,
    except after git, whose options are case-sensitive (`-c` sets config, `-C` changes folder:
    0.4.4 tester fixes LOW-5)."""
    w = cmd.split()
    if w:
        w[0] = _exe_name(w[0])
        if w[0] != "git":
            w[1:] = [x.lower() for x in w[1:]]
    return w


def denied_by(entry: str) -> str | None:
    """The hard-deny prefix that `entry` runs or would cover, else None. An entry that STARTS with
    a denied command is that command; an entry that is a word-prefix of one (`git` of `git push`,
    `rm` of `rm -r`) would allow it through other spellings (`git -C . push`, `rm x -r`). The first
    word is compared by its program name (`.venv\\Scripts\\python -m pip` is `python -m pip`)."""
    w = _words(entry)
    for d in SHELL_DEV_DENY:
        dw = _words(d)
        n = min(len(w), len(dw))
        if w[:n] == dw[:n]:
            return d
    return None


def _python_problem(rest: list[str]) -> str | None:
    """Why `python <rest>` could run code it does not name, else None (a script or a module comes
    first). Case-sensitive: python's `-c` runs code, `-E`/`-B`/`-X` do not."""
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "-":
            return "`-` reads code from stdin"
        if t.startswith("--"):
            # (2026-10-27, SEC-M5 review 2) `--check-hash-based-pycs MODE` takes the next word: it was read as
            # the script, so `-m sleeping_clanker` / `-c` could follow. Any other long option is refused (python
            # has no other that is needed before a script, and an unknown one could hide the same trick).
            if t == "--check-hash-based-pycs":
                i += 2
                continue
            if t in _PY_LONG_FLAGS:
                i += 1
                continue
            return f"`{t}` is not a python option this check knows: name the script or `-m <module>` first"
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            for j, ch in enumerate(letters):
                if ch == "c":
                    return f"`{t}` is a code flag (python -c runs any code)"
                if ch == "m":
                    mod = letters[j + 1:] or (rest[i + 1] if i + 1 < len(rest) else "")
                    if not mod:
                        return "`-m` without a module would let any module follow"
                    if mod.split(".")[0].lower() in DENIED_PY_MODULES:
                        return f"`-m {mod}` installs, serves or runs other code"
                    return None
                if ch in "XW":   # -X opt / -W arg: the rest (or the next word) is the argument
                    if j + 1 == len(letters):
                        i += 1
                    break
            i += 1
            continue
        return None   # a script path
    return "it names no script or module, so `-c <code>` could follow"


def _node_problem(rest: list[str]) -> str | None:
    """Why `node <rest>` could run code it does not name, else None (a script comes first)."""
    i = 0
    while i < len(rest):
        t = rest[i]
        name = t.split("=", 1)[0]
        if name in _NODE_CODE or (t.startswith("-") and not t.startswith("--") and
                                  any(ch in "ep" for ch in t[1:])):
            return f"`{t}` evaluates code"
        if t.startswith("-"):
            if name in _NODE_ARG_OPTS and "=" not in t:
                i += 1
            i += 1
            continue
        return None
    return "it names no script, so `-e <code>` could follow"


def entry_problem(entry: str, _depth: int = 0) -> str | None:
    """(2026-10-21, 0.4.4 tester fixes MED-2) Why `entry` (already cleaned) is unsafe as a prefix rule,
    else None. The rules are listed above DENIED_PROGRAMS."""
    words = entry.split()
    if not words:
        return "the command is empty"
    exe = _exe_name(words[0])
    rest = words[1:]
    if exe in DENIED_PROGRAMS:
        return f"`{exe}` is always denied, whatever follows it"
    verb = _PS_CMDLET_RE.match(exe)   # (2026-10-27, SEC-M5 review 2)
    if verb and verb.group(1) in _PS_DENIED_VERBS and exe not in _PS_VERB_OK:
        return (f"`{exe}`: a `{verb.group(1)}-` cmdlet changes state, writes files, reaches another machine or "
                "runs code, and is always denied")
    if _PS_SCRIPT_BLOCK_RE.match(exe) or exe in _PS_SCRIPT_BLOCK_ALIASES:   # (2026-10-27, SEC-M5 review 4)
        return f"`{exe}` runs a script block given to it (any code) and is always denied"
    if exe.startswith("git-") or exe in ("gitk", "scalar"):   # git's own helpers run as programs (git-gui)
        return f"`{exe}` is a git helper program: allow `git <subcommand>` instead"
    if exe == "git":
        if not rest:
            return "`git` alone would allow every git command; name the subcommand (`git merge`)"
        sub = rest[0]
        if sub.startswith("-"):
            return (f"`git {sub}` is a global option: any subcommand (push included) could follow it; "
                    "start with the subcommand")
        if sub.lower() in GIT_DENIED_SUBCOMMANDS or sub.lower().startswith(GIT_DENIED_PREFIXES):
            return (f"`git {sub}` has a destructive, config-changing, file-writing, command-running or "
                    "network form and is always denied")
        if sub.lower() not in GIT_ALLOWED_SUBCOMMANDS:   # (2026-10-27, SEC-M5 review 4)
            return (f"`git {sub}` is not one of the git subcommands that only work in the folder's own repository "
                    f"or only read it ({', '.join(sorted(GIT_ALLOWED_SUBCOMMANDS))})")
    if exe in PACKAGE_MANAGERS:
        if not rest:
            return f"`{exe}` alone would allow every {exe} command; name the subcommand (`{exe} test`)"
        sub = rest[0]
        if sub.startswith("-"):
            return f"`{exe} {sub}` is an option: any subcommand could follow it; start with the subcommand"
        if sub.lower() in PACKAGE_MANAGERS[exe]:
            return f"`{exe} {sub}` installs, downloads, publishes or runs other code and is always denied"
        # (2026-10-22, v049 re-test R5) `npm run` with no script would allow every script in package.json
        if exe in ("npm", "pnpm", "yarn", "bun") and sub.lower() in ("run", "run-script", "rum", "urn") and (
                len(rest) < 2 or rest[1].startswith("-")):
            return f"`{exe} {sub}` alone would allow every script; name the script (`{exe} {sub} build`)"
        if LAUNCHERS.get(exe) == sub.lower():
            inner = " ".join(rest[1:])
            if not inner or inner.startswith("-"):
                return f"`{exe} {sub}` must name the command it runs, with no option before it"
            if _depth > 2:
                return "too many nested launchers"
            why = entry_problem(inner, _depth + 1)
            return f"`{exe} {sub}` runs `{inner}`: {why}" if why else None
    if _PYTHON_RE.match(exe):
        return _python_problem(rest)
    if _PYTHON_FAMILY_RE.search(exe):   # (2026-10-27, SEC-M5 review 4)
        return f"`{exe}` is a Python shell or launcher: allow `python <script>` or `python -m <module>` instead"
    if exe in _NODE_NAMES:
        return _node_problem(rest)
    if exe in _SCRIPT_INTERPRETERS and (not rest or rest[0].startswith("-")):
        return f"`{exe}` must be followed by a script (an option first could evaluate code)"
    if exe not in _WINDOWS_TOOL_OK and _windows_tool(exe):   # (2026-10-27, SEC-M5 review 4)
        return (f"`{exe}` is a program that ships with Windows; only the read-only ones "
                f"({', '.join(sorted(_WINDOWS_TOOL_OK))}) may be allowed")
    return None


def _windows_tool(exe: str) -> bool:
    """(2026-10-27, SEC-M5 review 4) Whether `exe` (a program name, lower case) is a file in the Windows folder
    or one of _WINDOWS_TOOL_DIRS under it (the folder itself is looked at, not PATH)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_+-]*", exe):
        return False
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return any(os.path.isfile(os.path.join(root, d, exe + ext)) for d in _WINDOWS_TOOL_DIRS for ext in (".exe", ".com"))


def normalise_allow_entry(raw: object) -> str:
    """One allow-list entry, cleaned (single spaces), or AllowListError naming the reason."""
    if not isinstance(raw, str):
        raise AllowListError("an allowed command must be text")
    entry = " ".join(raw.split())
    if not entry:
        raise AllowListError("the command is empty")
    if len(entry) > SHELL_ALLOW_ENTRY_MAX:
        raise AllowListError(f"the command is longer than {SHELL_ALLOW_ENTRY_MAX} characters")
    if not _ENTRY_RE.match(entry) or entry.endswith(":"):
        raise AllowListError(
            "only a literal command prefix is allowed: letters, digits, spaces and . _ \\ / : = + - "
            "(no quotes, $, ;, &, |, <, >, backticks, *, parentheses or commas)")
    # (2026-10-21, 0.4.4 tester fixes MED-2) the structural rules first (they name the clearer reason)
    why = entry_problem(entry)
    if why is not None:
        raise AllowListError(f"{entry!r} refused: {why} (profiles.py)")
    deny = denied_by(entry)
    if deny is not None:
        raise AllowListError(f"{entry!r} runs or covers {deny!r}, which is always denied (profiles.py)")
    return entry


def allow_file(project_dir: Path) -> Path:
    return Path(project_dir) / SHELL_ALLOW_FILE


def load_allow_list(path: Path | None) -> list[str]:
    """The valid entries of `path` (missing file = []). Fails CLOSED: an unreadable file or an invalid
    entry is logged and left out, so a broken list only takes commands away."""
    if path is None:
        return []
    from .store import read_text_retry  # (2026-10-26) lazy: store imports this module
    try:
        # (2026-10-26, improve_core) retried: a read during `shell-allow add`'s replace failed with EACCES
        # and an attempt starting right then ran without the owner's commands
        data = json.loads(read_text_retry(path))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("shell-dev: %s unreadable (%s); using an empty allow-list", path, exc)
        return []
    raw = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        log.warning("shell-dev: %s has no \"commands\" list; using an empty allow-list", path)
        return []
    out: list[str] = []
    for r in raw:
        try:
            e = normalise_allow_entry(r)
        except AllowListError as exc:
            log.warning("shell-dev: ignoring allow-list entry %r in %s: %s", r, path, exc)
            continue
        if e.lower() not in (x.lower() for x in out):
            out.append(e)
    return out[:SHELL_ALLOW_MAX]


def save_allow_list(path: Path, entries: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from .store import write_text_atomic  # (2026-10-26) lazy: store imports this module
    # (2026-10-26, improve_core) a unique temp name + the replace retry, not a fixed `.tmp` + bare os.replace
    write_text_atomic(path, json.dumps({"commands": entries}, indent=2, ensure_ascii=False) + "\n")


def _rule_bodies(cmds, tools=("PowerShell",)) -> list[str]:
    """Each command as an exact rule and a prefix rule (`cmd` and `cmd:*`) for each of `tools`."""
    out: list[str] = []
    for c in cmds:
        for tool in tools:
            out += [f"{tool}({c})", f"{tool}({c}:*)"]
    return out


# (2026-10-21, shell-dev PowerShell rule syntax, DECISIONS.md Unreleased) claude.exe 2.1.304's PowerShell
# check names an external program by its LAST path part when the path is relative with backslashes and
# no leading `.\` (`.venv\Scripts\python` -> `python`), then allows a PREFIX rule for that program only if
# the name equals the command's first word: `python` != `.venv\scripts\python`, so `PowerShell(.venv\
# Scripts\python -m pytest:*)` never matches anything with arguments (probes 1-2, PROBES.txt). A head with
# a leading `.\` (or `..\`), with forward slashes, or with a drive letter keeps its full name and matches.
# So a relative path head is emitted in the spellings that match, with and without `.exe`; the spelling
# as given is kept too (its EXACT rule still matches the command with no extra arguments). Only the
# head's separators, `.\` and `.exe` change, so the program (_exe_name) and MED-2's checks are the same.
def _dot_head(head: str) -> str | None:
    r"""A relative path head as `.\` + backslashes (`.venv/Scripts/python` -> `.\.venv\Scripts\python`);
    None for a plain program name or a drive path, which match as written."""
    if not re.search(r"[\\/]", head) or re.match(r"^[A-Za-z]:", head):
        return None
    bs = head.replace("/", "\\")
    return bs if bs.startswith((".\\", "..\\")) else ".\\" + bs


def allow_forms(entry: str) -> list[str]:
    """`entry` and every spelling of it that claude.exe can match as a PowerShell rule (see above)."""
    head, sep, rest = entry.partition(" ")
    dot = _dot_head(head)
    if dot is None:
        return [entry]
    heads: list[str] = []
    fwd = head.replace("\\", "/")
    for h in (head, dot, fwd[2:] if fwd.startswith("./") else fwd):
        base = re.split(r"[\\/]", h)[-1]
        alt = h[:-4] if base.lower().endswith(".exe") else (h + ".exe" if "." not in base else None)
        heads += [h] + ([alt] if alt else [])
    return [f"{h}{sep}{rest}" for h in dict.fromkeys(heads)]


def _allow_rules(allow: list[str]) -> list[str]:
    return list(dict.fromkeys(_rule_bodies([f for e in allow for f in allow_forms(e)])))


def _prompt_form(entry: str) -> str:
    r"""The spelling the system prompt shows: a relative path head with a leading `.\` (it matches)."""
    head, sep, rest = entry.partition(" ")
    dot = _dot_head(head)
    return entry if dot is None else f"{dot}{sep}{rest}"


# shell-dev: shell-read's denials (Bash, agents, monitors, schedulers, web, .git/) plus the hard-deny
# commands, as PowerShell AND Bash rules (the Bash ones only matter if Bash is ever offered again)
SHELL_DEV_DISALLOWED = ",".join([SHELL_READ_DISALLOWED, *_rule_bodies(SHELL_DEV_DENY, ("PowerShell", "Bash"))])


def shell_dev_system_prompt(allow: list[str]) -> str:
    return (
        "Permission profile shell-dev. Shell commands run through the PowerShell tool, one "
        "command per call, and only these are allowed:\n"
        "1. Exactly as written: " + _code(SHELL_READ_EXACT + SHELL_READ_PS_EXACT) + ".\n"
        "2. With plain literal arguments: " + _code(SHELL_READ_PS_PREFIX) + ".\n"
        "3. The owner's allow-list for this project, with plain literal arguments: "
        + _code(tuple(_prompt_form(e) for e in allow)) + ".\n"
        # (2026-10-21, shell-dev rule syntax) a relative path without the leading `.\` is always denied
        r"Write a program path as listed, starting with `.\` (or use forward slashes)." "\n"
        "No parentheses, `$`, `@`, braces, pipes, `;`, `&&`, backticks, redirection, or UNC "
        r"(\\host\share) paths. Pushing, history rewrites, recursive deletes, installs, downloads and "
        "admin tools are always denied, even if listed. Use the Read, Glob and Grep tools to read and "
        "search files. If the task needs a command that is not allowed, do not look for a workaround: "
        "stop and report TASK_BLOCKED (GOAL_BLOCKED in a goal), naming the exact command, so the owner "
        "can add it with `autopilot shell-allow add`."
    )


def shell_dev_args(allow: list[str]) -> list[str]:
    """(2026-10-20, 0.4.4 logfix A) shell-read plus `allow`. An EMPTY list is exactly shell-read's argv."""
    if not allow:
        return list(PROFILES["shell-read"])
    return [
        "--permission-mode", "dontAsk",
        "--tools", SHELL_TOOLS,
        "--allowedTools", ",".join([_shell_read_allowed(), *_allow_rules(allow)]),
        "--disallowedTools", SHELL_DEV_DISALLOWED,
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--append-system-prompt", shell_dev_system_prompt(allow),
    ]


# the static entry (argparse choices, docs) is the empty-list form; profile_args() fills the list in
PROFILES[SHELL_DEV] = shell_dev_args([])


# ---- (2026-10-21, owner) `full`: any command, no permission checks -------------------------------------
# Owner (task 347aa828): "this should work over night, meaning full access; yes it's not safe, but if
# users want to then it can do it." Opt-in per item only: never a default, never chosen by a picker or
# Auto-pick. The bypass flag is allowed ONLY as this profile's own token (runner.build_argv); --claude-arg
# extras and any other profile are still refused by the bypass guard. --tools stays a fixed list
# (0.4.1 R2: unlisted background tools died with the run); Agent is in it so a task can start a tester.
# The ENV_FILE_DENY rules are kept for the record only: a shell reads a .env anyway, and a provider key
# never reaches this profile (runner.provider_key_refusal refuses every profile with a shell).
FULL = "full"
FULL_BYPASS_FLAG = "--dangerously-skip-permissions"
FULL_TOOLS = "Read,Edit,Write,Glob,Grep,Bash,PowerShell,Agent,WebFetch,WebSearch"
PROFILES[FULL] = [
    FULL_BYPASS_FLAG,
    "--tools", FULL_TOOLS,
    "--disallowedTools", ENV_FILE_DENY,
    "--strict-mcp-config",
]


# ---- (2026-10-21, subagents, docs/SPEC_V049_AGENTS.md) the per-task subagent limit --------------------
# How many subagents one task may start. It is the ITEM's own field (`max_agents`), never a global
# default, and it means:
#   None = the profile decides, i.e. 0.4.6 behaviour (`full` offers Agent; every other profile denies it),
#   0    = none: the spawn tools are taken out of --tools and added to --disallowedTools,
#   n>0  = the spawn tools are offered and pre-approved, and the system prompt states the cap of n,
#   AGENTS_UNLIMITED = offered, with no number in the prompt.
# What is enforced where:
# - HARD (claude.exe): a tool that is not in --tools is not offered at all (0.4.1 R2), and a
#   --disallowedTools rule beats every allow rule (0.4.4 logfix E). So None and 0 cannot start one.
# - SOFT (the prompt): the COUNT. claude.exe 2.1.304 has no "at most n subagents" flag, so n is stated
#   in the system prompt only. The runner counts the spawn calls of each attempt and logs a warning
#   when the task went over its limit (runner.agent_calls); nothing kills a run for it.
# Allowing Agent does NOT widen what the work may do: a subagent inherits the session's deny list
# (DECISIONS.md 0.4.2/0.4.4: Bash denied "in this session and in its subagents", probed), so it can
# open no shell, reach no web and read no .env that the task itself could not. It only changes how
# much work runs in parallel. Background agents stay unsupported (0.4.1 R2: they die with the run):
# TaskOutput/TaskStop are never allowed and the prompt asks for foreground subagents.
# NOT probed on this machine: whether `full` (--dangerously-skip-permissions) still honours
# --disallowedTools. With limit 0 the spawn tools are left out of its --tools as well, which is the
# mechanism 0.4.1 R2 did verify.
AGENT_SPAWN_TOOLS = ("Agent", "Task")   # the tool that starts a subagent; `Task` is its older name
AGENTS_UNLIMITED = -1
AGENTS_MAX = 20
SYSTEM_PROMPT_FLAG = "--append-system-prompt"   # == runner.SYSTEM_PROMPT_FLAG (a test checks it)


def normalise_agents(value: object) -> int | None:
    """An item's `max_agents` as None (the profile decides) / 0 / 1..AGENTS_MAX / AGENTS_UNLIMITED.
    Anything else (a bool, a string, a negative other than -1) is None: a broken value never widens."""
    if value is None or isinstance(value, bool):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n == AGENTS_UNLIMITED:
        return AGENTS_UNLIMITED
    if n < 0:
        return None
    return min(n, AGENTS_MAX)


AGENTS_TEXT_DEFAULT = ("", "default", "profile")
AGENTS_TEXT_NONE = ("none", "no", "off", "0")
AGENTS_TEXT_UNLIMITED = ("unlimited", "no-limit", "max")
AGENTS_TEXTS = "default, none, unlimited or a number 1-%d" % AGENTS_MAX


def parse_agents_text(text: str) -> int | None:
    """(2026-10-21, subagents) A `--max-agents` value as the item's field: "default" (or "") = None =
    the profile decides, "none"/"0" = 0, "unlimited" = AGENTS_UNLIMITED, "1".."20" = that many.
    Raises ValueError with the allowed forms for anything else."""
    t = (text or "").strip().lower()
    if t in AGENTS_TEXT_DEFAULT:
        return None
    if t in AGENTS_TEXT_NONE:
        return 0
    if t in AGENTS_TEXT_UNLIMITED:
        return AGENTS_UNLIMITED
    try:
        n = int(t)
    except ValueError:
        raise ValueError(f"expected {AGENTS_TEXTS}") from None
    if not 1 <= n <= AGENTS_MAX:
        raise ValueError(f"expected {AGENTS_TEXTS}")
    return n


def agents_label(limit: int | None) -> str:
    """How a limit reads in a log line or a list: "the profile's default" / "none" / "3" / "unlimited"."""
    if limit is None:
        return "the profile's default"
    if limit == 0:
        return "none"
    if limit == AGENTS_UNLIMITED:
        return "unlimited"
    return str(limit)


def agents_prompt(limit: int) -> str:
    """The system-prompt paragraph a task with subagents gets. `limit` is 1..AGENTS_MAX or UNLIMITED."""
    how_many = ("as many subagents as the work needs" if limit == AGENTS_UNLIMITED
                else f"at most {limit} subagent{'' if limit == 1 else 's'}")
    return (
        f"Subagents: you may start {how_many} with the Agent tool in this run (the owner's limit for "
        "this task). Run each one in the foreground and wait for its result: a background agent is "
        "killed when this run ends. A subagent gets this session's permissions, deny list included, so "
        "it cannot run a command, reach the web or read a .env that you could not. Keep to the limit; "
        "if the work needs more, finish what you can and say so in your report."
    )


def _value_index(args: list[str], flag: str) -> int | None:
    """The index of `flag`'s value in `args`, or None when the flag is absent (or has no value)."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return i + 1
    return None


def _set_tools(args: list[str], flag: str, tools: tuple[str, ...], present: bool,
               create: bool = False) -> None:
    """In place: make each of `tools` present in (or absent from) `flag`'s comma-joined value; other
    entries keep their order. A missing flag is added only with `create` (the deny list, which has to
    exist for a denial to apply); every profile of PROFILES already carries all three flags."""
    at = _value_index(args, flag)
    if at is None:
        if present and create:
            args += [flag, ",".join(tools)]
        return
    parts = [p for p in args[at].split(",") if p and p not in tools]
    if present:
        parts += list(tools)
    args[at] = ",".join(parts)


def with_agents(args: list[str], limit: int | None) -> list[str]:
    """A copy of a profile's argv with the subagent spawn tools allowed (`limit` 1+/UNLIMITED) or
    denied (`limit` 0). `None` returns the profile's own argv unchanged."""
    if limit is None:
        return list(args)
    out = list(args)
    allow = limit != 0
    _set_tools(out, "--tools", AGENT_SPAWN_TOOLS, allow)
    _set_tools(out, "--allowedTools", AGENT_SPAWN_TOOLS, allow)
    _set_tools(out, "--disallowedTools", AGENT_SPAWN_TOOLS, not allow, create=True)
    if allow:
        at = _value_index(out, SYSTEM_PROMPT_FLAG)
        if at is None:
            out += [SYSTEM_PROMPT_FLAG, agents_prompt(limit)]
        else:
            out[at] = f"{out[at]}\n\n{agents_prompt(limit)}"
    return out


# ---- (2026-10-27, modes fix HIGH) Ultracode needs the Workflow tool ----------------------------------
# claude.exe 2.1.310 with --settings {"ultracode":true} only adds a reminder to use the Workflow tool on
# every substantive task. A session without Workflow in --tools cannot orchestrate anything, so the flag
# would do nothing. Only `full` gets Workflow (it starts agents and runs scripts). A no-shell profile, and
# shell-read/shell-dev, never get it: Workflow stays in their deny list (COMMAND_TOOLS). Workflow starts
# agents, so a task with no subagents (limit 0) cannot have it either. NOT probed: whether 2.1.310 offers
# the Workflow tool without a separate "workflows enabled" setting.
WORKFLOW_TOOL = "Workflow"


def ultracode_refusal(profile: str | None, max_agents: object = None) -> str | None:
    """Why a task with `profile` and subagent limit `max_agents` cannot have ultracode, else None."""
    if (profile or DEFAULT_PROFILE) != FULL:
        return (f"ultracode needs the full profile (profile {profile or DEFAULT_PROFILE} offers no Workflow "
                "tool, and no other profile ever gets one)")
    if normalise_agents(max_agents) == 0:
        return "ultracode needs subagents: this task's subagent limit is none (the Workflow tool starts agents)"
    return None


def with_ultracode(args: list[str]) -> list[str]:
    """A copy of a `full` argv that offers the Workflow tool (in --tools, out of --disallowedTools)."""
    out = list(args)
    _set_tools(out, "--tools", (WORKFLOW_TOOL,), True)
    _set_tools(out, "--disallowedTools", (WORKFLOW_TOOL,), False)
    return out


# (2026-10-27, SEC-M7 review) every queue's prompts dir under a base data dir (store layout; a removed lane
# is kept under lanes/.removed/<lane>-<stamp>); at most PROMPTS_DENY_MAX of them become rules (argv length)
PROMPTS_DIR_GLOBS = ("prompts", "lanes/*/prompts", "lanes/.removed/*/prompts", "projects/*/prompts",
                     "projects/*/lanes/*/prompts", "projects/*/lanes/.removed/*/prompts")
PROMPTS_DENY_MAX = 64


def _path_key(path: str | os.PathLike) -> str:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(path))


def other_prompts_dirs(data_root: str | os.PathLike, own_prompts: str | os.PathLike | None) -> list[Path]:
    """(2026-10-27, SEC-M7 review) The prompts dirs of every queue under `data_root` except `own_prompts` (the
    run's own: its task's long prompt / reply file is there): other projects' and lanes' prompts and replies."""
    own = _path_key(own_prompts) if own_prompts is not None else None
    found = {_path_key(d): d for pattern in PROMPTS_DIR_GLOBS for d in Path(data_root).glob(pattern) if d.is_dir()}
    return [d for key, d in sorted(found.items()) if key != own]


def with_data_dir_deny(args: list[str], data_root: str | os.PathLike | None,
                       own_prompts: str | os.PathLike | None = None,
                       key_files: list | tuple = ()) -> list[str]:
    """(2026-10-27, SEC-M7/L24) A copy of a profile's argv whose --disallowedTools also denies the data dir
    `data_root` in the absolute `//` form (data_dir_deny). None or a UNC path (logged) returns the argv
    unchanged; the `~/` default-dir rules in ENV_FILE_DENY still apply. (2026-10-27, SEC-M7 review 2) A path
    with `(`, `,` ... is denied too: rule_root writes those characters as `*`.

    (2026-10-27, SEC-M7 review) + Read of every OTHER queue's prompts dir (other_prompts_dirs; `own_prompts`
    stays readable), and Read/Edit/Write of each of `key_files` whose name no ENV_FILE_DENY rule matches
    (`run --env-file keys.txt`, $CLAUDE_AUTOPILOT_ENV_FILE)."""
    rules: list[str] = []
    root = rule_root(data_root) if data_root is not None else None
    if data_root is not None and root is None:
        log.warning("data dir %s (a UNC path, or one that cannot be resolved) cannot be written as a "
                    "permission rule: only the default data dir is denied to tasks", data_root)
    if root is not None:
        rules += data_dir_deny(root)
        others = [r for r in map(rule_root, other_prompts_dirs(data_root, own_prompts)) if r is not None]
        if len(others) > PROMPTS_DENY_MAX:
            log.warning("%d other queues' prompts dirs under %s: only the first %d are denied to tasks",
                        len(others), data_root, PROMPTS_DENY_MAX)
        rules += [f"Read({r}/**)" for r in others[:PROMPTS_DENY_MAX]]
    for path in key_files:
        name = Path(path).name
        if name == KEY_FILE_NAME or name == ".env" or name.startswith(".env.") or name.endswith(".env"):
            continue   # ENV_FILE_DENY already denies these names anywhere (case-sensitive: KEYS.ENV gets a rule)
        rule = rule_root(path)
        if rule is not None:
            rules += [f"{tool}({rule})" for tool in ("Read", "Edit", "Write")]
    out = list(args)
    if rules:
        _set_tools(out, "--disallowedTools", tuple(dict.fromkeys(rules)), True, create=True)
    return out


# ---- (2026-10-28, SEC-H2 owner decision "Pidä + rajaa kansiot") the user's own extra folders ----------------
# Runs keep loading the user's own Claude settings (`--setting-sources user`: their hooks keep working), and
# claude.exe makes each `permissions.additionalDirectories` entry there a working folder of the task, where
# `edits` (acceptEdits) writes freely and the Edit/Write allow rules of the other profiles apply (security_final
# SEC-H2). So every profile below `full` denies Edit/Write in each such folder that is not the task folder: a
# task writes only to its own folder (a deny rule beats an allow rule and acceptEdits). Only that one key of the
# file is read (strict JSON, a BOM dropped), the file is never written and no folder name is logged (counts
# only). The 8.3 and stream spellings of a denied folder are denied anyway (SHORT_NAME_DENY), and claude.exe
# matches a rule whatever the case (S1 R2.2 probe; F3 probe).
# (2026-10-29, integrate round 9, F3 residual 3: the same intent) an Edit(...)/Write(...) ALLOW rule of that file
# with an absolute path outside the task folder is denied the same way (its folder; the path itself when it has
# no glob): the reader also takes the Edit(/Write( entries of `permissions.allow`, nothing else of it.
USER_SETTINGS_FILE: str | os.PathLike | None = None   # None = claude.exe's own; tests/conftest.py sets a temp file
USER_DIRS_DENY_MAX = 64
USER_DIRS_ANCESTOR_WARNING = ("an extra folder from your Claude settings contains the task folder; "
                              "it stays writable")
USER_ALLOW_ANCESTOR_WARNING = ("an Edit/Write allow rule of your Claude settings covers the task folder's parent; "
                               "it stays as it is")   # (2026-10-29, integrate round 9, F3 residual 3)
_USER_DIRS_WARNED: set[str] = set()   # one warning of each kind per run
_ABS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_TRAIL_GLOB_RE = re.compile(r"(?:[\\/]\*\*)+[\\/]*$")   # `<dir>/**` names the folder <dir> (claude.exe trims it too)
_ALLOW_RULE_RE = re.compile(r"(?:Edit|Write)\((.*)\)", re.S)   # (round 9, F3 residual 3) the allow entries read
_GLOB_SEG_RE = re.compile(r"[*?\[\]{}]")


def _user_dirs_warn(kind: str, msg: str, *args) -> None:
    if kind not in _USER_DIRS_WARNED:
        _USER_DIRS_WARNED.add(kind)
        log.warning(msg, *args)


def _home(env) -> str:
    """The home folder claude.exe sees: USERPROFILE (Windows) or HOME of its env."""
    return env.get("USERPROFILE" if os.name == "nt" else "HOME") or str(Path.home())


def user_settings_path(env=None, task_dir: str | os.PathLike | None = None) -> Path:
    """(2026-10-28, SEC-H2) The settings file claude.exe loads for the `user` source: $CLAUDE_CONFIG_DIR/
    settings.json, else ~/.claude/settings.json, of `env` (the child's; default os.environ); a relative
    CLAUDE_CONFIG_DIR is the child's, i.e. under `task_dir`. USER_SETTINGS_FILE overrides both (tests)."""
    if USER_SETTINGS_FILE is not None:
        return Path(USER_SETTINGS_FILE)
    env = os.environ if env is None else env
    conf = env.get("CLAUDE_CONFIG_DIR") or ""
    base = Path(conf) if conf else Path(_home(env)) / ".claude"
    if not base.is_absolute() and task_dir is not None:
        base = Path(task_dir) / base
    return base / "settings.json"


def _not_json(name: str):
    raise ValueError(f"{name} is not JSON")


def user_extra_dirs(env=None, task_dir: str | os.PathLike | None = None) -> list:
    """(2026-10-28, SEC-H2) `permissions.additionalDirectories` of the user settings (user_settings_path), the
    entries unchecked. [] and one warning per run when the file is missing, unreadable or not strict JSON, or
    the key is malformed. A blank file is `{}`, as claude.exe 2.1.310 loads it."""
    return _user_permissions(env, task_dir)[0]


def user_allow_paths(env=None, task_dir: str | os.PathLike | None = None) -> list[str]:
    """(2026-10-29, integrate round 9, F3 residual 3) The path text of each `Edit(<path>)` / `Write(<path>)`
    entry of `permissions.allow` of the same file, unchecked; no other entry is kept. [] as user_extra_dirs."""
    return _user_permissions(env, task_dir)[1]


def _user_permissions(env=None, task_dir: str | os.PathLike | None = None) -> tuple[list, list[str]]:
    """(2026-10-28, SEC-H2; round 9) The one read of the user settings file: (additionalDirectories entries,
    Edit/Write allow paths). Each key is checked on its own: a malformed one is [] with one warning per run."""
    unread = "no extra folders or Edit/Write allow rules from it are denied to tasks"
    try:
        text = user_settings_path(env, task_dir).read_bytes().decode("utf-8-sig")
    except FileNotFoundError:
        _user_dirs_warn("missing", "your Claude user settings file (settings.json) was not found: %s", unread)
        return [], []
    except (OSError, UnicodeDecodeError) as exc:
        _user_dirs_warn("unreadable", "your Claude user settings file (settings.json) could not be read (%s): %s",
                        type(exc).__name__, unread)
        return [], []
    if not text.strip():
        return [], []
    try:
        data = json.loads(text, parse_constant=_not_json)
    except (ValueError, RecursionError) as exc:
        _user_dirs_warn("unreadable", "your Claude user settings file (settings.json) could not be read (%s): %s",
                        type(exc).__name__, unread)
        return [], []
    perms = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(perms, (dict, type(None))):
        _user_dirs_warn("malformed", "permissions.additionalDirectories of your Claude user settings is not a "
                                     "list: no extra folders from it are denied to tasks")
        return [], []
    dirs = (perms or {}).get("additionalDirectories")
    if not isinstance(dirs, (list, type(None))):
        _user_dirs_warn("malformed", "permissions.additionalDirectories of your Claude user settings is not a "
                                     "list: no extra folders from it are denied to tasks")
        dirs = None
    allow = (perms or {}).get("allow")
    if not isinstance(allow, (list, type(None))):
        _user_dirs_warn("malformed-allow", "permissions.allow of your Claude user settings is not a list: no "
                                           "Edit/Write allow rules from it are denied to tasks")
        allow = None
    paths = []
    for entry in allow or []:
        m = _ALLOW_RULE_RE.fullmatch(entry.strip()) if isinstance(entry, str) else None
        if m:
            paths.append(m.group(1).strip())
    return dirs or [], paths


def _allow_rule_start(pattern: str, home: str) -> tuple[str, str, bool]:
    """(2026-10-29, integrate round 9, F3 residual 3) Where an Edit/Write allow rule's path starts: (kind,
    native path, exact). Claude Code's absolute forms are `//<drive>/...` (drive C is `//c/`) and `~/...`; a drive
    path (`C:\\x`, `C:/x`) counts as absolute too (fail-closed). The path is the part before the first segment
    with a glob; `exact` = the pattern had none. kind "relative" (`x`, `./x`, `/x`: the task folder's or the
    settings file's own, not denied), "root" (`//**`: every drive, so it holds the task folder), "unc" (an
    absolute path on no drive, `//srv/share`: cannot be denied) or "path"."""
    p = pattern.strip()
    if p == "~" or p.startswith(("~/", "~\\")):
        text = home.replace("\\", "/").rstrip("/") + "/" + p[2:]
    elif p.startswith("//"):
        rest = p[2:].replace("\\", "/")
        first = rest.split("/", 1)[0]
        if os.name != "nt":
            text = "/" + rest
        elif re.fullmatch(r"[A-Za-z]", first):
            text = first + ":/" + rest[2:]
        elif not first or _GLOB_SEG_RE.search(first):
            return "root", "", False
        else:
            return "unc", "", False
    elif os.name == "nt" and _ABS_DRIVE_RE.match(p):
        text = p.replace("\\", "/")
    else:
        return "relative", "", False
    segs = text.split("/")
    static = []
    for seg in segs:
        if _GLOB_SEG_RE.search(seg):
            break
        static.append(seg)
    path = "/".join(static)
    if re.fullmatch(r"[A-Za-z]:", path) or not path:
        path += "/"   # a drive root (`//c/**`), or `/` itself (POSIX `//**`)
    return "path", path, len(static) == len(segs)


def _path_forms(path: str) -> list[str]:
    """`path` absolute as written and resolved (a link followed), each normcase'd."""
    forms = [os.path.normcase(os.path.abspath(path))]
    try:
        forms.append(os.path.normcase(str(Path(path).resolve())))
    except OSError:
        pass
    return list(dict.fromkeys(forms))


def _within(child: str, parent: str) -> bool:
    """normcase'd absolute paths: `child` is `parent` or a path inside it."""
    head = parent.rstrip("\\/")
    return child == parent or child.startswith(head + os.sep)


def user_dirs_deny(task_dir: str | os.PathLike, env=None) -> list[str]:
    """(2026-10-28, SEC-H2) Edit and Write deny rules (`<rule_root>/**`) for every extra folder of the user
    settings (user_extra_dirs) but the task's own. `~` and `~/...` are the home folder (claude.exe expands
    them). Skipped: an entry that is not an absolute path (relative, drive-relative, UNC, not text; one
    warning per run counts them), the task folder `task_dir` itself and a folder inside it (the task's own),
    and an ANCESTOR of the task folder, which cannot be denied without the task folder
    (USER_DIRS_ANCESTOR_WARNING, once per run). A folder is denied as written and resolved (a link);
    USER_DIRS_DENY_MAX folders at most. No folder name is ever logged.
    (2026-10-29, integrate round 9, F3 residual 3) Then each Edit/Write allow rule of the same file
    (user_allow_paths) with an absolute path (_allow_rule_start) outside the task folder: its folder is denied
    (`<root>/**`), and the path itself too when the rule has no glob (a file). The same skips (inside the task
    folder; an ancestor, `//**` included, with USER_ALLOW_ANCESTOR_WARNING once; a UNC path counted in one
    warning) and the same USER_DIRS_DENY_MAX, shared with the folders. Every rule comes as an Edit+Write pair
    (build_argv's fit)."""
    env = os.environ if env is None else env
    entries, allow = _user_permissions(env, task_dir)
    if not entries and not allow:
        return []
    home = _home(env)
    task_forms = _path_forms(str(task_dir))
    rules: list[str] = []
    skipped = unc = 0
    denied = [0]

    def deny(raw: str, exact: bool = False, allowed: bool = False) -> bool:
        """Deny `raw` (absolute, native) unless the task's own or an ancestor; False once the cap is reached."""
        forms = _path_forms(raw)
        if any(_within(f, t) for f in forms for t in task_forms):
            return True   # the task folder or a path inside it: the task's own
        if any(_within(t, f) for f in forms for t in task_forms):
            _user_dirs_warn(*(("allow-ancestor", USER_ALLOW_ANCESTOR_WARNING) if allowed
                              else ("ancestor", USER_DIRS_ANCESTOR_WARNING)))
            return True
        if denied[0] >= USER_DIRS_DENY_MAX:
            _user_dirs_warn("max", "more than %d extra folders or Edit/Write allow paths in your Claude settings: "
                                   "only the first %d are denied to tasks", USER_DIRS_DENY_MAX, USER_DIRS_DENY_MAX)
            return False
        denied[0] += 1
        for resolve in (False, True):
            root = rule_root(raw, resolve)
            for suffix in (("", "/**") if exact else ("/**",)) if root is not None else ():
                for tool in ("Edit", "Write"):
                    if f"{tool}({root}{suffix})" not in rules:
                        rules.append(f"{tool}({root}{suffix})")
        return True

    going = True
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            skipped += 1
            continue
        raw = home + entry[1:] if entry == "~" or entry.startswith(("~/", "~\\")) else entry
        raw = _TRAIL_GLOB_RE.sub("/", raw)
        if raw.startswith(("\\\\", "//")) or not (_ABS_DRIVE_RE.match(raw) if os.name == "nt"
                                                  else raw.startswith("/")):
            skipped += 1
            continue
        going = deny(raw)
        if not going:
            break
    for pattern in allow if going else []:
        kind, raw, exact = _allow_rule_start(pattern, home)
        if kind == "root":
            _user_dirs_warn("allow-ancestor", USER_ALLOW_ANCESTOR_WARNING)
        elif kind == "unc":
            unc += 1
        elif kind == "path" and not deny(raw, exact, allowed=True):
            break
    if skipped:
        _user_dirs_warn("skipped", "%d extra folder(s) in your Claude settings are not absolute paths "
                                   "(relative, UNC or not text): they are not denied to tasks", skipped)
    if unc:
        _user_dirs_warn("allow-unc", "%d Edit/Write allow rule(s) in your Claude settings name a path on no "
                                     "drive (UNC): they are not denied to tasks", unc)
    return rules


def user_dirs_ancestors(task_dir: str | os.PathLike, env=None) -> list[str]:
    """(2026-10-29, delta C-M1) The user grants user_dirs_deny(task_dir) skips as an ANCESTOR of `task_dir`: each
    extra folder (user_extra_dirs) and each Edit/Write allow path (user_allow_paths, its folder part) that holds
    the task folder but is not inside it, as an absolute native path (as written; `~` expanded). A `//**` rule
    holds every folder (and so the project too): not listed. Read only, from the same one read of the settings
    file; no folder name is logged. worktree's copy gate (wt-root-granted) compares them with the project."""
    env = os.environ if env is None else env
    entries, allow = _user_permissions(env, task_dir)
    if not entries and not allow:
        return []
    home = _home(env)
    task_forms = _path_forms(str(task_dir))
    raws: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        raw = home + entry[1:] if entry == "~" or entry.startswith(("~/", "~\\")) else entry
        raws.append(_TRAIL_GLOB_RE.sub("/", raw))
    for pattern in allow:
        kind, raw, _exact = _allow_rule_start(pattern, home)
        if kind == "path":
            raws.append(raw)
    out: list[str] = []
    for raw in raws:
        if raw.startswith(("\\\\", "//")) or not (_ABS_DRIVE_RE.match(raw) if os.name == "nt"
                                                  else raw.startswith("/")):
            continue
        forms = _path_forms(raw)
        if any(_within(f, t) for f in forms for t in task_forms):
            continue   # the task folder or a path inside it: the task's own
        if any(_within(t, f) for f in forms for t in task_forms) and raw not in out:
            out.append(raw)
    return out


def with_user_dirs_deny(args: list[str], profile: str, rules: list | tuple) -> list[str]:
    """(2026-10-28, SEC-H2) A copy of a profile's argv whose --disallowedTools also holds `rules`
    (user_dirs_deny); unchanged for `full` (no permission checks there) and for no rules."""
    out = list(args)
    if rules and profile != FULL:
        _set_tools(out, "--disallowedTools", tuple(dict.fromkeys(rules)), True, create=True)
    return out


def agents_phrase(limit: int | None) -> str:
    """(2026-10-21, subagents) What the prompt's "Tools" phrase adds; '' for None (unchanged)."""
    if limit is None:
        return ""
    if limit == 0:
        return "; no subagents"
    if limit == AGENTS_UNLIMITED:
        return "; subagents allowed (no limit)"
    return f"; at most {limit} subagent{'' if limit == 1 else 's'}"


# (2026-10-17, 0.4.1 R4) the tool phrase of the "Headless run" line in an attempt's first prompt
TOOL_PHRASES: dict[str, str] = {
    "safe": "Read, Glob, Grep, Edit/Write inside the folder; no shell, no web",
    "edits": "Read, Glob, Grep, Edit, Write; no shell, no web",
    "plan": "Read, Glob, Grep; read-only",
    # (2026-10-19, 0.4.2 N2) the commands are allowed as PowerShell(...) and Bash(...) alike
    "shell-read": ("Read, Glob, Grep, Edit/Write inside the folder, plus the read-only "
                   "commands listed in the system prompt; no web"),
    # (2026-10-20, 0.4.4 logfix A) with an empty allow-list the system prompt lists the read-only ones only
    "shell-dev": ("Read, Glob, Grep, Edit/Write inside the folder, plus the read-only commands and the "
                  "owner's allowed commands listed in the system prompt; no web"),
    # (2026-10-21, owner) full access
    "full": ("Read, Glob, Grep, Edit, Write, any shell command (Bash, PowerShell), subagents and web; "
             "no permission checks"),
}


class ProfileError(ValueError):
    pass


def profile_args(name: str, max_turns: int | None, shell_allow: list[str] | None = None,
                 max_agents: object = None) -> list[str]:
    """`shell_allow`: the project's allow-list (load_allow_list); used by shell-dev only.
    (2026-10-21, subagents) `max_agents`: the item's own subagent limit; None = the profile's own argv."""
    if name == SHELL_DEV:
        args = shell_dev_args([normalise_allow_entry(e) for e in (shell_allow or [])])
    else:
        try:
            args = list(PROFILES[name])
        except KeyError:
            raise ProfileError(f"unknown profile {name!r}; choose one of {sorted(PROFILES)}") from None
    args = with_agents(args, normalise_agents(max_agents))
    if max_turns is not None:
        # (2026-10-13) --max-turns is absent from `claude --help` 2.1.278 but the real CLI accepted it.
        args += ["--max-turns", str(int(max_turns))]
    return args

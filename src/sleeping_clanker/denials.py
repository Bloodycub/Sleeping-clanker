"""Permission denials of one attempt: which ones block a task, and what the owner can do about them
(2026-10-21, v049, docs/SPEC_V049_DONTASK_HOLD.md section 1).

claude.exe lists every tool call it refused in `result.permission_denials` ({tool_name, tool_use_id,
tool_input}). Real run logs (claude.exe 2.1.278) hold three kinds:
- a don't-ask denial: a `system/permission_denied` event with decision_reason_type "mode" and the
  tool_result "Permission to use X has been denied because Claude Code is running in don't ask mode";
- a subagent's denial: decision_reason_type "asyncAgent" ("no approval surface in this session");
- a PreToolUse HOOK that stopped the call (e.g. the owner's read_guard): NO permission_denied event, only
  the tool_result "PreToolUse:Read hook error: ...". It is still listed in permission_denials.
A hook denial is a note (0.4.4 logfix H): the model reads a slice and goes on. It never blocks a task.
Every other denial is a permission denial and does.

Nothing here grants anything: the block only names what was denied and suggests the owner's fix (a
profile to retry with, the allow-list entry to add). The owner decides (Retry with profile / Reply).
"""

from __future__ import annotations

import re
from pathlib import PurePath, PureWindowsPath

from .profiles import (_PYTHON_RE, DENIED_PROGRAMS, FULL, KEY_FILE_NAME, SHELL_DEV, SHELL_READ_EXACT,
                       SHELL_READ_PS_EXACT, SHELL_READ_PS_PREFIX, AllowListError, _dot_head, _exe_name,
                       _prompt_form, _words, allow_forms, denied_by, normalise_allow_entry)

# the tool_result text of a PreToolUse hook that stopped a call (== transcript.js HOOK_DENIAL_RE)
HOOK_DENIAL_RE = re.compile(r"^(?:Error:\s*)?PreToolUse:\w+ hook (?:error|denied|blocked)\b")
KIND_HOOK = "hook"
SHOWN_MAX = 3            # denials named in the block (the rest is "+N more")
WHAT_MAX = 80            # characters of a command head or path in the block text
ALLOW_MAX = 3            # allow-list entries suggested at once
SHELL_TOOL_NAMES = ("Bash", "PowerShell")
PATH_TOOL_NAMES = ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit")
EDIT_TOOL_NAMES = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# what a shell profile denies in ANY command: one command per call (0.3.0/0.4.4 probes)
_OPERATOR_RE = re.compile(r"\||;|&&|>|<|`|\$\(")
# profile breadth for "retry with": only a WIDER profile than the item's own is suggested
_RANK = {"plan": 0, "safe": 0, "edits": 1, "shell-read": 2, SHELL_DEV: 3, FULL: 4}
FIX_HARD = "hard"
# (2026-10-22, v049 tester LOW-11) the notes of a shell denial
OPERATOR_NOTE = "one command per call: `|`, `;`, `&&`, `>` and backticks are denied in every profile"
BASH_NOTE = "the shell profiles run commands through PowerShell (Bash is never offered)"
SPLICE_NOTE = ("a backtick or `$(` splices text into this command, so no profile or allow-list entry fits it "
               "as written")
# npm's spellings of `run-script`: `npm run build` keeps the script name (`npm run` would allow every script)
_RUN_SCRIPT = frozenset({"run", "run-script", "rum", "urn"})
_SCRIPT_RUNNERS = ("npm", "pnpm", "yarn", "bun")
# (2026-10-27, SEC-L17) C0/C1 control characters (ESC, BEL, CSI ...) but \t and \n: a model-chosen command
# or path carrying them would rewrite what `list`/`show` print on the owner's console; (2026-10-27, S8 fix r3)
# and the bidi embeddings, overrides and isolates (U+202A-U+202E, U+2066-U+2069): they reorder the text after
# them where a terminal or the Task view honours them
CONTROLS_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
# (2026-10-27, SEC-M5) what a denial block may SUGGEST (the owner adds it with one Enter): a test runner,
# build tool or package script WITH its subcommand, module or script. Never a program alone (`gh`, `aws`,
# `explorer`: one word allows all it does, the network with stored credentials included), another tool,
# or the autopilot itself (`python -m sleeping_clanker add --profile full`). The validator
# (profiles.normalise_allow_entry) still decides what the owner may add by hand; this only narrows the offer.
_SCRIPT_SUBCOMMANDS = frozenset({"test", "t", "tst", "start", "stop", "restart", "build", "lint", "typecheck",
                                 "check", "compile", "format", *_RUN_SCRIPT})
_SUGGEST_SUBCOMMANDS: dict[str, frozenset | None] = {
    # git: local subcommands only (the validator already refuses push/fetch/pull/clone/remote ...);
    # (2026-10-27, S8 fix r1) never `apply`: `git apply --unsafe-paths --directory=C:/x` writes outside the folder
    "git": frozenset({"add", "commit", "merge", "cherry-pick", "revert", "tag", "mv", "am", "status",
                      "ls-files", "rev-parse", "describe", "blame", "shortlog"}),
    "npm": _SCRIPT_SUBCOMMANDS, "pnpm": _SCRIPT_SUBCOMMANDS, "yarn": _SCRIPT_SUBCOMMANDS, "bun": _SCRIPT_SUBCOMMANDS,
    "uv": frozenset({"run", "build"}), "poetry": frozenset({"run", "build"}), "pipenv": frozenset({"run"}),
    "pdm": frozenset({"run", "build"}), "hatch": frozenset({"run", "build", "test"}), "conda": frozenset({"run"}),
    "cargo": frozenset({"build", "test", "check", "clippy", "fmt", "run", "bench", "doc"}),
    "dotnet": frozenset({"build", "test", "run", "format", "clean", "pack"}),
    "deno": frozenset({"test", "task", "check", "lint", "fmt", "bench"}),
    "go": frozenset({"build", "test", "vet", "fmt", "run"}),
    "mvn": frozenset({"test", "compile", "package", "verify", "clean", "validate"}),
    "gradle": frozenset({"test", "build", "check", "assemble", "clean"}),
    "vsce": frozenset({"package", "ls"}),
    "make": None,   # any target: make is the build runner
}
# what `<launcher> run <program>` may start, and the modules `python -m <module>` may run
_SUGGEST_RUN_PROGRAMS = frozenset({"pytest", "tox", "nox", "mypy", "ruff", "flake8", "pylint", "black", "isort",
                                   "coverage", "pyright", "bandit"})
_SUGGEST_PY_MODULES = _SUGGEST_RUN_PROGRAMS | frozenset({"unittest", "build", "compileall", "py_compile",
                                                         "doctest", "pyflakes", "pycodestyle", "pydocstyle"})
# (2026-10-29, Clanker rename) the old package names stay: an older core may still be installed and runnable
_AUTOPILOT_NAMES = frozenset({"sleeping_clanker", "sleeping_claud", "claud_autopilot"})
# (2026-10-27, S8 fix r2) the autopilot named anywhere in a denied command: its packages (`-m sleeping_clanker`,
# `src\sleeping_clanker\__main__.py`, `-msleeping_clanker.cli`) or its `autopilot` console script
_AUTOPILOT_WORD_RE = re.compile(r"(?i)sleeping_cla(?:nker|ud)|claud_autopilot|(?<![\w.-])autopilot(?:\.exe)?(?![\w.-])")
# (2026-10-27, S8 fix r3) the package names once every quote, escape, glob and `${...}` mark is gone
# (`sleeping''_clanker`, `sleeping?clanker`, `${X:-sleeping}_clanker`); words stay apart (`Sleeping Clanker` is not it)
_AUTOPILOT_SQUASHED_RE = re.compile(r"(?i)sleeping_?cla(?:nker|ud)|claud_?autopilot")
_SHELL_QUOTES_RE = re.compile(r"[\"'`]")
_SHELL_VARS_RE = re.compile(r"\$\{[^}]*\}|\$\w+")
_WRAPPER_PROGRAMS = frozenset({"coverage"})   # the suggestable programs that start the program named after them
_UNC_WORD_RE = re.compile(r"(?:^|=)[\\/]{2}")   # a UNC path (\\host\share, //host/share), alone or after `=`
_ANSI_C_RE = re.compile(r"\$'((?:[^'\\]|\\.)*)'", re.S)
_ANSI_C_ESC_RE = re.compile(r"\\(?:x([0-9A-Fa-f]{1,2})|([0-7]{1,3})|u([0-9A-Fa-f]{1,4})|U([0-9A-Fa-f]{1,8})|(.))",
                            re.S)
NOT_SUGGESTED_NOTE = ("`{0}` is not suggested as an allow-list entry: only a test runner, build tool or package "
                      "script with its subcommand, module or script is (`npm test`, `python -m pytest`), never a "
                      "program alone or the autopilot itself")
# (2026-10-27, S8 fix r4, SEC-M5) what PowerShell evaluates in a native command's arguments outside quotes
_BUILT_MARKS = frozenset("$(){}@")
BUILT_NOTE = ("`{0}` is not suggested as an allow-list entry: a word of this command is built when it runs "
              "(`(...)`, `$...`, `@...`, `{{...}}`, `--%`), so what it starts cannot be read from it")


# ---- reading the stream ------------------------------------------------------------------------
def tool_result_text(block: dict) -> str:
    """The text of a tool_result content block (a string, or a list of {type: text, text})."""
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return ""


def is_hook_text(text: str) -> bool:
    return bool(HOOK_DENIAL_RE.match(str(text or "").strip()))


def annotate(denials: list, reasons: dict, hook_ids: set) -> list[dict]:
    """`denials` (result.permission_denials, or the events' list) with a `kind` each: "hook" when its
    tool_result was a PreToolUse hook's, else the permission_denied event's decision_reason_type
    ("mode", "asyncAgent", ...), else "permission" (no event seen; still a permission denial)."""
    out = []
    for d in denials or []:
        if not isinstance(d, dict):
            continue
        tid = d.get("tool_use_id")
        kind = KIND_HOOK if tid in hook_ids else (reasons.get(tid) or d.get("kind") or "permission")
        out.append({**d, "kind": kind})
    return out


def blocking(denials: list | None) -> list[dict]:
    """The denials that block a task: every one but a hook's."""
    return [d for d in denials or [] if isinstance(d, dict) and d.get("kind") != KIND_HOOK]


# ---- what was denied ---------------------------------------------------------------------------
def _input(d: dict) -> dict:
    inp = d.get("tool_input")
    return inp if isinstance(inp, dict) else {}


def command_of(d: dict) -> str | None:
    cmd = _input(d).get("command")
    return cmd if isinstance(cmd, str) and cmd.strip() else None


def path_of(d: dict) -> str | None:
    inp = _input(d)
    for key in ("file_path", "notebook_path", "path"):
        if isinstance(inp.get(key), str) and inp[key].strip():
            return inp[key].strip()
    return None


def first_command(cmd: str) -> str:
    """The first command of a command line: its first line, cut before the first operator."""
    line = cmd.strip().splitlines()[0] if cmd.strip() else ""
    return _OPERATOR_RE.split(line, maxsplit=1)[0].strip()


def _group_end(s: str, i: int, open_ch: str, close_ch: str) -> int:
    """The index just past the `close_ch` that closes the group opened at s[i] (nesting counted, quotes
    not); len(s) when it is never closed."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == open_ch:
            depth += 1
        elif s[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(s)


def commands_of(cmd: str) -> list[dict]:
    """(2026-10-22, v049 tester LOW-11) The commands of a command line, in order, each
    {"text", "piped", "spliced"}: a new command starts after `;`, `&&`, `||`, a newline or a trailing `&`
    (piped False) and after `|` (piped True: a pipeline stage). Operators inside quotes, a `$(...)` /
    `@(...)` subexpression or a `{...}` script block do not split. A redirection (`>`, `2>&1`, `<`) and its
    target are left out of the command. `spliced`: the command holds a backtick (PowerShell's escape; a
    backtick at a line end continues the command on the next line) or a `$(`/`@(` subexpression, so its
    words are not the command that runs. Empty commands are skipped."""
    s = cmd or ""
    out: list[dict] = []
    cur: list[str] = []
    st = {"piped": False, "spliced": False, "redirect": False}

    def text() -> str:
        return "".join(cur)

    def add(chunk: str) -> None:
        if not st["redirect"]:
            cur.append(chunk)

    def flush(next_piped: bool) -> None:
        words = " ".join(text().split())
        if words:
            out.append({"text": words, "piped": st["piped"], "spliced": st["spliced"]})
            st["piped"] = next_piped
        elif next_piped:
            st["piped"] = True
        cur.clear()
        st["spliced"] = st["redirect"] = False

    q = None
    i, n = 0, len(s)
    while i < n:
        ch, two = s[i], s[i:i + 2]
        if q == "'":                      # single quotes: all literal
            if ch == "'":
                q = None
            add(ch)
            i += 1
            continue
        if ch == "`":                     # the escape character, also inside "..."
            st["spliced"] = True
            nxt = "\r\n" if s[i + 1:i + 3] == "\r\n" else s[i + 1:i + 2]
            add(" " if nxt in ("\n", "\r\n") else ch + nxt)   # at a line end: the command goes on
            i += 1 + len(nxt)
            continue
        if two in ("$(", "@("):           # a subexpression, also inside "...": its output is spliced in
            st["spliced"] = True
            j = _group_end(s, i + 1, "(", ")")
            add(s[i:j])
            i = j
            continue
        if q == '"':
            if ch == '"':
                q = None
            add(ch)
            i += 1
            continue
        if two in ('@"', "@'") and re.match(r"[ \t]*\r?\n", s[i + 2:]):   # (re-test R3b) blanks may end the line
            # (2026-10-22, v049 re-test R3) a here-string: its body, up to the `"@` / `'@` that starts a line,
            # is one literal argument of its command, never commands of its own (quotes inside do not count)
            close = "\n" + two[1] + "@"
            j = s.find(close, i + 2)
            j = n if j < 0 else j + len(close)
            if two == '@"' and re.search(r"`|\$\(", s[i + 2:j]):
                st["spliced"] = True      # an expandable here-string splices its `$(...)` and escapes
            add(s[i:j])
            i = j
            continue
        if ch in "'\"":
            q = ch
            add(ch)
        elif ch == "{":                   # a script block stays in its command
            j = _group_end(s, i, "{", "}")
            add(s[i:j])
            i = j
            continue
        elif two in ("&&", "||"):
            flush(False)
            i += 2
            continue
        elif ch == "|":
            flush(True)
        elif ch == ";" or ch == "\n":
            if ch == ";" or text().strip():   # an empty line, or a newline after `|`, starts nothing
                flush(False)
        elif ch == "&" and text().strip() and not st["redirect"]:
            flush(False)                  # PowerShell 7's background `&`; a LEADING `&` is the call operator
        elif ch in "<>":
            if not st["redirect"]:        # a stream number glued to it (`2>`, `*>`) is part of it
                tail = text()
                if tail[-1:] in tuple("0123456789*") and tail[-2:-1] in ("", " ", "\t"):
                    cur[:] = [tail[:-1]]
            st["redirect"] = True
        else:
            add(ch)
        i += 1
    flush(False)
    return out


def strip_controls(text: str) -> str:
    """(2026-10-27, SEC-L17) `text` without C0/C1 control characters (\\t and \\n are kept) or (S8 fix r3) bidi
    embeddings, overrides and isolates."""
    return CONTROLS_RE.sub("", str(text))


def _clip(text: str, n: int = WHAT_MAX) -> str:
    # (2026-10-27, SEC-L17) whitespace first (a newline still splits two words), then the controls go
    text = " ".join(w for w in (strip_controls(w) for w in str(text).split()) if w)
    return text if len(text) <= n else text[:n - 3] + "..."


def what_of(d: dict) -> str | None:
    """The command head or the path the denial names (None when its input has neither)."""
    cmd = command_of(d)
    if cmd:
        return _clip(first_command(cmd) or cmd)
    path = path_of(d)
    if path:
        return _clip(path)
    inp = _input(d)
    for key in ("url", "pattern", "description"):
        if isinstance(inp.get(key), str) and inp[key].strip():
            return _clip(inp[key])
    return None


# ---- the owner's fix ---------------------------------------------------------------------------
def is_env_path(path: str) -> bool:
    """A path ENV_FILE_DENY covers in every profile: .env, .env.*, *.env and providers.env."""
    name = PureWindowsPath(path).name.lower() if "\\" in path else PurePath(path).name.lower()
    return name == ".env" or name.startswith(".env.") or name.endswith(".env") or name == KEY_FILE_NAME


def _shell_read_allows(cmd: str) -> bool:
    """True when shell-read runs `cmd` as written: one of its exact commands, or a read cmdlet with
    plain arguments (the same forms SHELL_READ_SYSTEM_PROMPT lists)."""
    if _OPERATOR_RE.search(cmd) or re.search(r"[$@(){}]", cmd):
        return False
    norm = " ".join(cmd.split())
    if norm in SHELL_READ_EXACT or norm in SHELL_READ_PS_EXACT:
        return True
    head = norm.split(" ", 1)[0].lower()
    return head in (c.lower() for c in SHELL_READ_PS_PREFIX)


def _shell_read_forms(cmd: str) -> list[str]:
    """(2026-10-27, S8 fix r4) shell-read's exact commands that `cmd` (two words or more, plain) is the start of:
    `git diff` -> `git diff --no-ext-diff --no-textconv`, `git diff --stat`, ... (the forms profiles lists)."""
    if _OPERATOR_RE.search(cmd) or re.search(r"[$@(){}]", cmd):
        return []
    tokens = _tokens(cmd)
    if len(tokens) < 2 or any(quoted for _w, quoted in tokens):
        return []
    head = " ".join([_exe_name(tokens[0][0]), *(w for w, _q in tokens[1:])]).lower() + " "
    return [f for f in SHELL_READ_EXACT if f.lower().startswith(head)]


def _forms_note(cmd: str, forms: list[str]) -> str:
    names = [f"`{f}`" for f in forms]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]
    return f"shell-read runs `{' '.join(cmd.split())}` only as exactly {listed}"


_WORD_OK = re.compile(r"^[A-Za-z0-9._\\/:=+-]+$")


def _tokens(line: str) -> list[tuple[str, bool]]:
    """(2026-10-21, v049 tester fix MED-4) `line` split into words the way PowerShell reads plain
    arguments: whitespace splits outside quotes; '...' and "..." group (spaces included) and the quotes
    go. Each word comes with True when any part of it was quoted. A leading PowerShell call operator
    (`&` alone, or glued: `&"C:\\x\\git.exe"`) is dropped."""
    out: list[tuple[str, bool]] = []
    cur: list[str] = []
    quoted = started = False
    q = None
    for ch in line:
        if q:
            if ch == q:
                q = None
            else:
                cur.append(ch)
        elif ch in "'\"":
            q, quoted, started = ch, True, True
        elif ch.isspace():
            if started:
                out.append(("".join(cur), quoted))
            cur, quoted, started = [], False, False
        else:
            cur.append(ch)
            started = True
    if started:
        out.append(("".join(cur), quoted))
    if out and out[0][0] == "&" and not out[0][1]:
        out = out[1:]
    elif out and out[0][0].startswith("&") and not line.lstrip().startswith(("'", '"')):
        out[0] = (out[0][0][1:], out[0][1])
    return out


def _is_absolute(prog: str) -> bool:
    """A program named by an absolute path (`C:\\...`, `\\...`, `/...`, `C:x`): never an allow entry."""
    p = PureWindowsPath(prog)
    return bool(p.drive or p.root) or prog.startswith("/")


def allow_candidate(cmd: str) -> str | None:
    """(2026-10-27, SEC-M5) The allow-list entry a denial block may SUGGEST for `cmd`: _candidate's entry
    when it is a known test/build command with its subcommand, module or script (_suggestable), else None
    (`pytest -q` -> None: one word; `gh gist create x` -> None; `python -m sleeping_clanker add` -> None)."""
    # (2026-10-27, S8 fix r2) a command that names the autopilot anywhere gets no offer: a wrapper's prefix
    # (`python -m coverage`, `uv run coverage`) cut from `... coverage run -m sleeping_clanker add` allows it
    if _may_run_autopilot(cmd):
        return None
    entry = _candidate(cmd)
    return entry if _suggestable(entry) else None


def _ansi_c_char(m: re.Match) -> str:
    hexa, octal, u4, u8, other = m.groups()
    if other is not None:
        return other
    try:
        return chr(int(octal, 8) if octal else int(hexa or u4 or u8, 16))
    except (ValueError, OverflowError):
        return ""


def names_autopilot(cmd: str | None) -> bool:
    """(2026-10-27, S8 fix r3, SEC-M5) `cmd` names the autopilot (_AUTOPILOT_WORD_RE) as written OR as the shell
    reads it: quotes and escapes (`sleeping''_clanker`, `"sleeping"_clanker`, `sleeping\\_clanker`, `auto''pilot.exe`)
    and bash's $'\\x5f' are undone first, and the package names are also looked for with every quote, escape,
    glob and variable mark gone (`sleeping?clanker`, `sleeping${X}_clanker`). A heuristic for what is SUGGESTED, so
    it errs towards no offer; S1's profile denies are the guard."""
    raw = str(cmd or "")
    decoded = _ANSI_C_RE.sub(lambda m: _ANSI_C_ESC_RE.sub(_ansi_c_char, m.group(1)), raw)
    unquoted = _SHELL_QUOTES_RE.sub("", decoded)
    no_vars = _SHELL_VARS_RE.sub("", unquoted)
    for text in (raw, decoded, unquoted, unquoted.replace("\\", ""), no_vars, no_vars.replace("\\", "")):
        if _AUTOPILOT_WORD_RE.search(text):
            return True
    return any(_AUTOPILOT_SQUASHED_RE.search(re.sub(r"[^\w\s]", "", text)) for text in (decoded, no_vars))


def _wrapper_unread(cmd: str) -> bool:
    """(2026-10-27, S8 fix r3, SEC-M5) `cmd` runs a wrapper (`coverage`, as `python -m coverage`, `uv run
    coverage`, `-mcoverage`) and a word after it does not read literally (quoted, `$...`, `(...)`, a glob): what
    the wrapper starts is then unknown (`-m ('sleeping' + '_clanker')` in PowerShell), so its entry is not offered."""
    tokens = _tokens(first_command(str(cmd or "")))
    for i, (word, _quoted) in enumerate(tokens):
        if _exe_name(word[2:] if word.startswith("-m") else word).split(".")[0] in _WRAPPER_PROGRAMS:
            return any(quoted or not _WORD_OK.match(w) for w, quoted in tokens[i + 1:])
    return False


def _built_at_run(cmd: str) -> bool:
    """(2026-10-27, S8 fix r4, SEC-M5) a word of `cmd` (its first command) is built when PowerShell runs it: outside
    quotes `(...)`, `$...`, `@...`, `{...}` or a stop-parsing `--%` (its `%VAR%` expand), and `$` inside double
    quotes. What it starts is then unknown whatever program it names (`python -m pytest -p ('sleeping' +
    '_clanker.__main__')` loads the autopilot as a pytest plugin). Bash's own expansions (globs) are not looked for:
    a shell profile runs every command through PowerShell, which never globs a native command's arguments."""
    line = first_command(str(cmd or ""))
    q = None
    for ch in line:
        if q == "'":
            q = None if ch == "'" else q
        elif q == '"':
            if ch == "$":
                return True
            q = None if ch == '"' else q
        elif ch in "'\"":
            q = ch
        elif ch in _BUILT_MARKS:
            return True
    return any(w == "--%" and not quoted for w, quoted in _tokens(line))


def _may_run_autopilot(cmd: str) -> bool:
    """(2026-10-27, S8 fix r3, SEC-M5) no entry and no wider retry for `cmd`: it names the autopilot, or a wrapper
    in it starts something its words do not show, or (S8 fix r4) a word of it is built when it runs."""
    return names_autopilot(cmd) or _wrapper_unread(cmd) or _built_at_run(cmd)


def _suggestable(entry: str | None) -> bool:
    """(2026-10-27, SEC-M5) `entry` (an allow_candidate prefix) names a program of a known test/build family
    WITH its subcommand, module or script, and not the autopilot: two words at least."""
    words = (entry or "").split()
    if len(words) < 2:
        return False
    if any(_UNC_WORD_RE.search(w) for w in words):
        return False   # (2026-10-27, S8 fix r3) `node \\host\s\x.js`: claude.exe refuses a UNC path itself
    exe = _exe_name(words[0])
    rest = words[1:]
    if _PYTHON_RE.match(exe):
        # (2026-10-27, S8 fix r1) the module found the way python reads the options (`-W -m pytest` and
        # `-Xm pytest` run a script FILE named pytest: -W/-X take the next value), not by a trailing m
        module = _python_module(rest)
        if module is not None:
            return module.split(".")[0].lower() in _SUGGEST_PY_MODULES
        target = rest[-1]
        parts = {p.lower() for p in re.split(r"[\\/]", target)}
        return not parts & _AUTOPILOT_NAMES and target.lower().endswith(".py")
    if exe in ("node", "nodejs"):
        return True   # `node build.js`, `node --test dir`: a script (or the test runner) is named
    if exe not in _SUGGEST_SUBCOMMANDS:
        return False
    allowed = _SUGGEST_SUBCOMMANDS[exe]
    sub = rest[0].lower()
    if allowed is not None and sub not in allowed:
        return False
    if sub == "run" and exe in ("uv", "poetry", "pipenv", "pdm", "hatch", "conda"):
        return len(rest) > 1 and _exe_name(rest[1]) in _SUGGEST_RUN_PROGRAMS
    return True


def _candidate(cmd: str) -> str | None:
    """The allow-list entry that would let `cmd` (its first command) run in shell-dev: the program and
    its subcommand / module / script (`git merge`, `npm test`, `python -m pytest`, `node build.js`),
    or the program alone (`pytest`). None when no literal prefix can be cut from it.
    (2026-10-21, v049 tester fix MED-4) The words are unquoted first; a QUOTED or ABSOLUTE program
    (`& "C:\\Program Files\\Git\\cmd\\git.exe" ...`) gives no entry: a prefix rule never matches it as
    written. `& .venv\\Scripts\\python -m x` (call operator, relative path) still gives one."""
    tokens = _tokens(first_command(cmd))   # the PowerShell call operator `&` is dropped
    if not tokens or tokens[0][1] or _is_absolute(tokens[0][0]):
        return None
    words = []
    for w, _quoted in tokens:
        if not w or not _WORD_OK.match(w):
            break
        words.append(w)
    if not words:
        return None
    exe = _exe_name(words[0])
    rest = words[1:]
    n = 1
    if _PYTHON_RE.match(exe):
        # (2026-10-22, v049 tester LOW-11) options with a value (`-X utf8`, `-W ignore`) are skipped the
        # way profiles._python_problem reads them: `python -X utf8 -m pytest` -> `python -X utf8 -m pytest`
        n = 1 + _python_entry_len(rest)
        if n > len(words):
            return None   # the module / script is not a literal word: no prefix can be cut
    elif exe in ("node", "nodejs"):
        k = next((i for i, w in enumerate(rest) if not w.startswith("-")), None)
        n = 2 + k if k is not None else 1
    elif rest and not rest[0].startswith("-") and exe in (
            "git", "npm", "pnpm", "yarn", "bun", "pip", "pip3", "uv", "poetry", "pipenv", "pdm", "hatch",
            "conda", "gem", "cargo", "dotnet", "deno", "vsce", "go", "make", "mvn", "gradle"):
        n = 3 if exe in ("uv", "poetry", "pipenv", "pdm", "hatch", "conda") and rest[0] == "run" and len(rest) > 1 else 2
        # (2026-10-22, v049 tester LOW-11) `npm run build` keeps its script: `npm run` allows every script
        if exe in _SCRIPT_RUNNERS and rest[0].lower() in _RUN_SCRIPT:
            if len(rest) < 2 or rest[1].startswith("-"):
                return None   # (re-test R5) no script named: no entry (`npm run` alone is refused)
            n = 3
    return " ".join(words[:n]) or None


def _python_entry_len(rest: list[str]) -> int:
    """(2026-10-22, v049 tester LOW-11) How many words after `python` its allow entry keeps: the options up
    to and including the module (`-m mod`, `-mmod`) or the script, an option's value included (`-X utf8`,
    `-W ignore`, read as profiles._python_problem reads them). A code flag ends it (`python -c`, refused);
    0 = it names neither (the entry is `python` alone, refused)."""
    i = 0
    while i < len(rest):
        t = rest[i]
        if t == "-":
            return 0
        if t.startswith("--"):
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            letters = t[1:]
            for j, ch in enumerate(letters):
                if ch == "c":
                    return i + 1
                if ch == "m":
                    return i + 1 if letters[j + 1:] else i + 2
                if ch in "XW":
                    if j + 1 == len(letters):
                        i += 1
                    break
            i += 1
            continue
        return i + 1
    return 0


def _python_module(rest: list[str]) -> str | None:
    """(2026-10-27, S8 fix r1) The module `python <rest>` runs (`-m mod`, `-mmod`, `-bm mod`), walking the
    options as _python_entry_len does: -X/-W take a value (the next word when the letter is last), a code
    flag (`-c`) or the first word that is not an option (a script) gives None."""
    i = 0
    while i < len(rest):
        t = rest[i]
        if t.startswith("--"):
            i += 1
            continue
        if not (t.startswith("-") and len(t) > 1):
            return None
        letters = t[1:]
        for j, ch in enumerate(letters):
            if ch == "m":
                glued = letters[j + 1:]
                if glued:
                    return glued
                return rest[i + 1] if i + 1 < len(rest) else None
            if ch == "c":
                return None
            if ch in "XW":
                if j + 1 == len(letters):
                    i += 1
                break
        i += 1
    return None


def _hard_reason(cmd: str, entry: str | None) -> str | None:
    """Why `cmd` is denied in every profile with permission checks, else None: it runs a SHELL_DEV_DENY
    command or an always-denied program (profiles.DENIED_PROGRAMS), or its allow-list `entry` is refused.
    (2026-10-21, v049 tester fix MED-4) The words are unquoted first and the program is compared by its
    name, so `& "C:\\Program Files\\Git\\cmd\\git.exe" push` is `git push`."""
    tokens = [w for w, _quoted in _tokens(first_command(cmd))]
    if not tokens:
        return None
    exe = _exe_name(tokens[0])
    first = " ".join([exe, *tokens[1:]]).strip()
    deny = denied_by(first) if first else None
    if deny is not None and _words(first)[:len(_words(deny))] == _words(deny):
        return f"`{deny}` is always denied"
    if exe in DENIED_PROGRAMS:
        return f"`{exe}` is always denied"
    if entry:
        try:
            normalise_allow_entry(entry)
        except AllowListError as exc:
            return str(exc)
    return None


def _join(*notes: str | None) -> str | None:
    return "; ".join(n for n in notes if n) or None


def _covered(text: str, allow: list[str]) -> str | None:
    """(2026-10-22, v049 tester LOW-11) The allow-list entry that already covers the command `text` (its
    spelling with a leading `.\\` for a relative program path, profiles._prompt_form), compared without
    case against every rule spelling of the entry (profiles.allow_forms); None when none does."""
    cmd = _prompt_form(" ".join(re.sub(r"^&\s*", "", text).split())).lower()
    for entry in allow or []:
        for form in allow_forms(entry):
            f = form.lower()
            if cmd == f or cmd.startswith(f + " "):
                return entry
    return None


def _dot_note(text: str) -> str | None:
    """(2026-10-22, v049 tester LOW-11) claude.exe matches no prefix rule for a relative program path written
    with backslashes and no leading `.\\` when arguments follow (profiles.allow_forms): the note names the
    spelling that matches."""
    tokens = _tokens(first_command(text))
    if len(tokens) < 2 or tokens[0][1]:
        return None
    head = tokens[0][0]
    if "\\" not in head or "/" in head or _is_absolute(head) or head.startswith((".\\", "..\\")):
        return None
    return f"a relative program path matches only with a leading `.\\`: the command must start `{_dot_head(head)}`"


def _judge(text: str, profile: str, allow: list[str] | None, spliced: bool) -> dict:
    """(2026-10-22, v049 tester LOW-11) {fix, allow, note, allowed} for ONE command of a shell denial under
    `profile` (`allow` = the project's allow-list, None = not known). `allowed`: `profile` already runs it
    as written (only its company in the line, or the tool, was denied), so no entry is suggested for it."""
    if spliced:
        # (2026-10-22, v049 re-test LOW-T2) a backtick or `$(` splices text into a word, so the words before
        # it are not the command that runs (`g`it push` is not `g`): no profile or allow entry fits as written
        hard = _hard_reason(text, None)
        return {"fix": FIX_HARD if hard else None, "allow": None, "note": hard or SPLICE_NOTE, "allowed": False}
    # (2026-10-21, v049 tester fix MED-3) shell-read's own forms first: `git diff`, `git remote -v`,
    # `python --version` run there as written, though none is a valid allow-list entry
    if _shell_read_allows(text):
        if _RANK.get(profile, 0) < _RANK["shell-read"]:
            return {"fix": "shell-read", "allow": None, "note": None, "allowed": False}
        return {"fix": None, "allow": None, "note": f"profile {profile} already allows this command",
                "allowed": True}
    # (2026-10-27, S8 fix r4) the start of shell-read's exact forms (`git diff`, `git diff --cached`, `git log`):
    # the hint names those forms instead of "not allowed in any profile"
    forms = _shell_read_forms(text)
    if forms:
        fix = "shell-read" if _RANK.get(profile, 0) < _RANK["shell-read"] else None
        return {"fix": fix, "allow": None, "note": _forms_note(text, forms), "allowed": False}
    # (2026-10-27, SEC-M5) the validator judges the whole prefix (_candidate); only a _suggestable one is offered
    entry = _candidate(text)
    hard = _hard_reason(text, entry)
    if hard:
        return {"fix": FIX_HARD, "allow": None, "note": hard, "allowed": False}
    if entry is None:
        prog = (_tokens(first_command(text)) or [("", False)])[0]
        note = ("a quoted or absolute program path is never an allow-list entry; run it by a relative path"
                if prog[0] and (prog[1] or _is_absolute(prog[0])) else "no allow-list entry can cover this command")
        return {"fix": None, "allow": None, "note": note, "allowed": False}
    dot = _dot_note(text)
    listed = _covered(text, allow) if allow else None
    if listed is not None:
        # (2026-10-22, v049 tester LOW-11) already on the allow-list: never suggested again
        if profile == SHELL_DEV:
            return {"fix": None, "allow": None, "allowed": not dot,
                    "note": dot or f"`{_prompt_form(listed)}` is already on this project's allow-list"}
        if not _may_run_autopilot(text):   # (2026-10-27, S8 fix r2, r3) no wider retry for the autopilot
            return {"fix": SHELL_DEV, "allow": None, "note": dot, "allowed": False}
    # (2026-10-27, S8 fix r2) nor an entry cut from a command that names it (`python -m coverage run -m
    # sleeping_clanker add`: the `python -m coverage` prefix would allow exactly that)
    if not _suggestable(entry) or _may_run_autopilot(text):
        note = BUILT_NOTE if _built_at_run(text) else NOT_SUGGESTED_NOTE   # (2026-10-27, S8 fix r4)
        return {"fix": None, "allow": None, "note": note.format(entry), "allowed": False}
    # the spelling that matches (a relative program path with a leading `.\`, as 0.4.8's rules write it)
    return {"fix": SHELL_DEV, "allow": _prompt_form(entry), "note": dot, "allowed": False}


def _shell_rows(cmd: str, tool: str, profile: str, allow: list[str] | None) -> list[tuple[str, dict]]:
    """(2026-10-22, v049 tester LOW-11) (what, fix) for each DENIED command of a shell denial: every command of
    every line is judged (commands_of). A command after `;`, `&&`, a newline ... gets its own row unless the
    profile already runs it; a pipeline stage (after `|`) only when it is hard-denied (`| sh`, `| Remove-Item`):
    pipes are denied in every profile anyway, so a stage never gets an entry (`npm test | Out-Null` keeps
    `npm test`). When no command is denied (only the operators or the tool were), the first one is shown.
    The line's own note (operators, Bash) goes on the first row."""
    cmds = commands_of(cmd) or [{"text": cmd.strip(), "piped": False, "spliced": False}]
    multi = len([ln for ln in cmd.splitlines() if ln.strip()]) > 1
    line_note = OPERATOR_NOTE if (_OPERATOR_RE.search(cmd) or multi) else (BASH_NOTE if tool == "Bash" else None)
    judged = [(c, _judge(c["text"], profile, allow, c["spliced"])) for c in cmds]
    rows = [(c["text"], f) for c, f in judged
            if not f["allowed"] and (not c["piped"] or f["fix"] == FIX_HARD)]
    if not rows:
        rows = [(judged[0][0]["text"], judged[0][1])]
    out = []
    for k, (text, f) in enumerate(rows):
        note = f["note"] if f["fix"] == FIX_HARD else _join(line_note if k == 0 else None, f["note"])
        out.append((_clip(text), {"fix": f["fix"], "allow": f["allow"], "note": note}))
    return out


def _rows_for(d: dict, profile: str, cwd: str | None, allow: list[str] | None = None) -> list[tuple[str | None, dict]]:
    """(what, {fix, allow, note}) rows of one blocking denial: one per denied command of a shell denial
    (_shell_rows), else one."""
    tool = str(d.get("tool_name") or "")
    profile = profile or "safe"
    cmd = command_of(d)
    if tool in SHELL_TOOL_NAMES and cmd:
        return _shell_rows(cmd, tool, profile, allow)
    path = path_of(d)
    if tool in PATH_TOOL_NAMES and path and is_env_path(path):
        fix = {"fix": FIX_HARD, "allow": None, "note": "key files (.env, *.env, providers.env) are denied in every profile"}
    elif tool in EDIT_TOOL_NAMES and path:
        if not _inside(path, cwd):
            # (2026-10-27, SEC-H2) runs load the user settings (--setting-sources user), so their
            # additionalDirectories widen where edits are allowed: the note no longer says "inside it only"
            # (2026-10-27, S8 fix r2) so do Edit/Write allow rules there and in managed settings
            # (2026-10-29, integrate round 8, DECISION [F3] residual 2) since SEC-H2's owner decision the user
            # settings' additionalDirectories are write-denied below full (profiles.user_dirs_deny), except one
            # that contains the task folder: the note no longer says edits are allowed there
            # (2026-10-29, integrate round 9, F3 residual 3) so are the absolute paths of the user settings' own
            # Edit/Write allow rules (profiles.user_dirs_deny): only managed settings still widen it everywhere
            fix = {"fix": None, "allow": None,
                   "note": "outside the task's folder; the profiles below full allow edits only there and "
                           "where Edit/Write allow rules of your user or managed settings allow them (every run "
                           "loads ~/.claude/settings.json); the extra folders of your user settings "
                           "(additionalDirectories) are write-denied, except one that contains the task folder, "
                           "and so are the absolute paths of its Edit/Write allow rules"}
        elif profile in ("safe", "plan"):
            fix = {"fix": "edits", "allow": None, "note": None}
        else:
            fix = {"fix": None, "allow": None, "note": None}
    else:
        fix = {"fix": None, "allow": None, "note": None}
    return [(what_of(d), fix)]


def fix_for(d: dict, profile: str, cwd: str | None, allow: list[str] | None = None) -> dict:
    """{fix, allow, note} for one blocking denial under `profile` (its first row, _rows_for):
    fix = "shell-read" | "shell-dev" | "edits" | "hard" | None; allow = the allow-list entry for shell-dev;
    note = a short reason when there is no fix or the command needs splitting."""
    return _rows_for(d, profile, cwd, allow)[0][1]


def _inside(path: str, cwd: str | None) -> bool:
    """`path` is the task folder or under it (a relative path counts as inside)."""
    p = PureWindowsPath(path)
    if not p.drive and not p.root:
        # (2026-10-27, S8 fix r4, SEC-H2) `~` and `~/...` are the home folder (claude.exe expands them); a leading
        # `$` or `%` names a variable: none of them is known to be inside
        if p.parts and p.parts[0][:1] in ("~", "$", "%"):
            return False
        return ".." not in p.parts
    if not cwd:
        return True
    # (2026-10-27, S8 fix r2) `..` folded first: `<task folder>\..\other\x.py` is not inside the task folder
    if ".." in p.parts:
        kept: list[str] = []
        for part in p.parts[1:]:
            if part != "..":
                kept.append(part)
            elif kept:
                kept.pop()
        p = PureWindowsPath(p.anchor, *kept)
    try:
        p.relative_to(PureWindowsPath(cwd))
        return True
    except ValueError:
        pass
    return str(p).lower().startswith(str(PureWindowsPath(cwd)).lower().rstrip("\\") + "\\")


def summarise(denials: list | None, profile: str | None, cwd: str | None,
              key_provider: str | None = None, allow: list[str] | None = None) -> dict | None:
    """The `denial_block` of a blocked item, or None when no denial blocks:
    {"denied": [{tool, what, fix, allow, note}] (deduplicated, at most SHOWN_MAX), "more": int,
     "profile": the profile to retry with (wider than the item's) or None, "allow": [entries]}.
    (2026-10-21, v049 merge) `key_provider`: the attempt ran on a provider with an API key, which `run`
    refuses with a shell profile (runner.provider_key_refusal), so no shell fix is suggested: the row
    says to change the task's provider instead (a Retry with shell-* would only fail).
    (2026-10-22, v049 tester LOW-11) a shell denial gives one row per denied command (_shell_rows), and
    `allow` (the project's shell-dev allow-list) keeps an entry already on it from being suggested again."""
    profile = profile or "safe"
    rows: list[dict] = []
    seen: set = set()
    for d in blocking(denials):
        tool = str(d.get("tool_name") or "a tool")
        for what, fix in _rows_for(d, profile, cwd, allow):
            key = (tool, (what or "").lower())
            if key in seen:
                continue
            seen.add(key)
            if key_provider and fix["fix"] in ("shell-read", SHELL_DEV):
                fix = {"fix": None, "allow": None,
                       "note": f"a shell profile is refused with provider {key_provider} (an API key is in its "
                               f"env); set the task's provider to claude to retry it with {fix['fix']}"}
            rows.append({"tool": tool, "what": what, **fix})
    if not rows:
        return None
    rows.sort(key=lambda r: r["tool"])   # stable: the order of appearance within one tool
    shown = rows[:SHOWN_MAX]
    best = None
    for r in rows:
        fix = r["fix"]
        if fix in _RANK and _RANK[fix] > _RANK.get(profile, 0) and (best is None or _RANK[fix] > _RANK[best]):
            best = fix
    allow = list(dict.fromkeys(r["allow"] for r in rows if r["allow"]))[:ALLOW_MAX]
    return {"denied": shown, "more": len(rows) - len(shown), "profile": best, "allow": allow}


def block_text(summary: dict | None) -> str:
    """`permission denied for PowerShell `git push`, Write `x.py` (+2 more)`: what the block names."""
    if not summary:
        return "permission denied for a tool"
    parts = [f"{r['tool']} `{r['what']}`" if r.get("what") else r["tool"] for r in summary["denied"]]
    more = f" (+{summary['more']} more)" if summary.get("more") else ""
    return "permission denied for " + ", ".join(parts) + more


def _quote(entry: str) -> str:
    return '"' + entry.replace('"', "") + '"'


def hint_text(summary: dict | None, item_id: str = "<id>") -> str:
    """The owner's fix in CLI terms. Never applied by the runner: the owner decides."""
    if not summary:
        return ""
    lines = []
    allow = summary.get("allow") or []
    profile = summary.get("profile")
    adds = " and ".join(f"`autopilot shell-allow add {_quote(e)}`" for e in allow)
    if profile == SHELL_DEV and allow:
        lines.append(f"retry with profile shell-dev after adding the command to this project's allow-list: "
                     f"{adds}, then `autopilot retry {item_id} --profile shell-dev`")
    elif profile:
        lines.append(f"retry with profile {profile}: `autopilot retry {item_id} --profile {profile}`")
    elif allow:
        lines.append(f"add the command to this project's allow-list: {adds}, then `autopilot retry {item_id}`")
    for r in summary["denied"]:
        what = f"{r['tool']} `{r['what']}`" if r.get("what") else r["tool"]
        if r.get("fix") == FIX_HARD:
            lines.append(f"{what} is not allowed in any profile ({r.get('note') or 'always denied'})")
        elif r.get("note"):
            lines.append(f"{what}: {r['note']}")
    if not lines:
        lines.append("no profile change would allow it; answer with Reply")
    return "; ".join(lines)

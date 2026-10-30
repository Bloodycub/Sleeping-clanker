"""Goals, attachments and their prompts (2026-10-13, docs/SPEC_GOALS.md sections 1 and 3).

Every prompt text a goal or an attachment adds lives here, so tests can import the exact strings.

A goal is an item with kind == "goal". Each successful attempt is one ROUND; the last marker line
of the round's result decides what happens next (GOAL_COMPLETE / GOAL_CONTINUE / GOAL_BLOCKED). A
continuing goal goes back to `pending` with its original seq, so it keeps its place in the queue.

Spec amendments from the independent review (2026-10-13, see DECISIONS.md):
- H2: every round resets `limit_waits`, so the limit-wait cap counts waits within one round only.
- H3: the marker line is normalised (trailing blank and code-fence lines skipped; `*`, `_`, `` ` ``,
  `#`, `>` and whitespace stripped around it; trailing `.`, `!`, `:`, `;` dropped) and then matched
  by prefix. Three rounds in a row without a marker fail the goal (`no_marker_rounds`).
- M2: the goal wrapper, the protocol and the attachment block always stay inline in the argv
  prompt; only the raw user prompt goes to the long-prompt file, and the inline text points to it.
- M3: later-round and resume prompts restate the goal (its first 1500 chars plus the prompt file
  path when it is longer) and the full protocol.
- M4: a goal round that ends in error_max_turns counts as a round (no marker) and goes back to
  pending; it does not fail the goal and does not count towards `no_marker_rounds`.
- M14: goals always carry both caps; a goal stored without them gets the defaults.

(2026-10-13) Internal field `goal_continue` (not in the spec table): True only while the last
applied outcome was a round that goes on. It picks the next-round prompt for the next attempt; a
usage-limit wait, a stop, a pause or a crash clears it, so those use the goal resume prompt.

(2026-10-15, SPEC_V03 section 5.1) Tasks get an end-of-message protocol too (TASK_DONE /
TASK_BLOCKED), parsed by the same normalising, case-insensitive marker parser. GOAL_BLOCKED and
TASK_BLOCKED give the new terminal status `blocked` (Claude needs the owner); it superseded
`failed` for GOAL_BLOCKED (SPEC_GOALS 3.2 and the 0.2.1 entries). (SPEC_V03 5.2) The owner's
reply prompt lives here as well.
"""

from __future__ import annotations

import re
from datetime import datetime

from .clock import iso, parse_iso
from .log import get_logger
from .denials import block_text, summarise
from .profiles import TOOL_PHRASES, agents_phrase, normalise_agents

log = get_logger(__name__)

KIND_TASK, KIND_GOAL = "task", "goal"
KINDS = (KIND_TASK, KIND_GOAL)
DEFAULT_MAX_ROUNDS = 50
DEFAULT_MAX_HOURS = 72.0
MAX_ROUNDS_LIMIT = 500       # (2026-10-13, tester fix 1) --max-rounds is an int in 1..500
UNLIMITED_ROUNDS = -1        # (2026-10-26, owner) --max-rounds -1 = no round cap: rounds go on until stopped
MAX_HOURS_LIMIT = 720.0      # (2026-10-13, tester fix 1) --max-hours is finite in (0, 720]
NO_MARKER_LIMIT = 3          # amendment H3
GOAL_RESTATE_MAX = 1500      # amendment M3 (resume prompts; above this the goal goes to a file)
NEXT_ROUND_RESTATE_MAX = 300  # (2026-10-17, 0.4.1 R5) same-session next rounds: the goal is in history
MAX_ATTACHMENTS = 50         # review L8: keeps the inline prompt well under the Windows argv limit

# (2026-10-17, 0.4.1 R5) fresh goal sessions. fresh_at_ctx: start the next round in a new session
# when the last main-thread message of the round had at least this many context tokens (input +
# cache read + cache write); 0 = off. fresh_every: a new session after this many rounds; 0 = off.
DEFAULT_FRESH_AT_CTX = 100_000
DEFAULT_FRESH_EVERY = 0
FRESH_AT_CTX_LIMIT = 10_000_000
FRESH_EVERY_LIMIT = MAX_ROUNDS_LIMIT
HANDOFF_MAX = 3000           # the stored PROGRESS block
HANDOFF_FALLBACK = 1500      # no PROGRESS block: the tail of the final message
LAST_MESSAGE_MAX = 3000      # R6: "Your previous attempt ended with:"
# (2026-10-17, 0.4.1 R6) A reply or resume more than 55 minutes after the previous attempt ended
# finds the prompt cache cold (the main thread writes the 1-hour cache; 55 min leaves margin), so
# resuming re-reads the whole context at the full input price. With at least 60k context that costs
# more than a new session carrying the task, the last message and the reply (token_v04 F5, R6).
RESUME_FRESH_AFTER_S = 55 * 60
RESUME_FRESH_MIN_CTX = 60_000
FRESH_ROUND, FRESH_RESUME = "round", "resume"

MARK_COMPLETE = "GOAL_COMPLETE"
MARK_CONTINUE = "GOAL_CONTINUE"
MARK_BLOCKED = "GOAL_BLOCKED"
MARK_TASK_DONE = "TASK_DONE"
MARK_TASK_BLOCKED = "TASK_BLOCKED"
STATUS_BLOCKED = "blocked"

# (2026-10-21, v049 dontask_hold addendum, SPEC_V049_DONTASK_HOLD 3) claude.exe killed the attempt's
# background tasks when the run ended: the item is resumed ONCE in the same session with this prompt
# (item[BG_REDO] set until Claude answered it); item[BG_KILLED] counts such attempts in a row, and the
# next one after the redo blocks the item.
BG_REDO = "bg_redo"
BG_KILLED = "bg_killed"
BG_REDO_MAX = 1
BG_REDO_PROMPT = ("Your background agents were killed when the run ended; redo that work in the "
                  "foreground and wait for it.")

# (2026-10-15, SPEC_V03 5.1) appended to EVERY task prompt, first attempt and resume alike.
# (2026-10-17, 0.4.1 R3) asks for ONE structured question (OPTION lines) when blocked.
TASK_PROTOCOL_LINE = (
    'End your FINAL message with "TASK_DONE" as its last line. If you cannot finish without the '
    'owner, stop as soon as you know it and end with ONE question instead: 2-4 lines '
    '"OPTION <n>: <short answer> - <what you will do then>" (best first, written '
    '"OPTION 1 (recommended): ..."), then the last line "TASK_BLOCKED: <the question>". Write the '
    'question and options in the language of the task text. AskUserQuestion is not available '
    'here; the owner replies by picking an option. '
    # (2026-10-21) the Task view's "What did you change?" button asks for the details on demand
    'Keep the final message short: the outcome in 1-3 lines, no file:line or per-function '
    'walkthrough unless the owner asks for one.'
)
# (2026-10-15, SPEC_V03 5.2) the prompt of the attempt that carries the owner's reply
REPLY_TEMPLATE = "Owner's reply:\n{text}\n\n{protocol}"
REPLY_FILE_REF = "(the reply is in the file {path}; read it first)"

# (2026-10-17, 0.4.1 R9) paths, not "read these before starting": directories were read whole
ATTACH_HEADER = ("Relevant paths (open only what the task needs; search directories with Glob/Grep, "
                 "do not read them whole):")

# (2026-10-17, 0.4.1 R5) PROGRESS block for fresh-session handoffs; R3 structured blocked question
# (2026-10-19, 0.4.2 L3) one explicit order: PROGRESS block, OPTION lines (blocked only), marker
PROTOCOL_BLOCK = """End your FINAL message in this order:
1. "PROGRESS:" and at most 15 lines: done (with file paths), next step, open risks. You may continue in a new session that sees only the goal and this block.
2. Only if blocked: 2-4 lines "OPTION <n>: <short answer> - <what you will do then>" (best first, written "OPTION 1 (recommended): ..."), in the language of the goal text.
3. Exactly one of these as the last line:
GOAL_COMPLETE              - the goal is fully achieved and verified
GOAL_CONTINUE              - more work remains; you will be resumed
GOAL_BLOCKED: <question>   - you cannot proceed without the owner
If blocked, stop as soon as you know it and ask ONE question. AskUserQuestion is not available here; the owner replies by picking an option."""

# (2026-10-17, 0.4.1 R4) added to the FIRST prompt of every new session (not to resumes)
# (2026-10-19, 0.4.2 N2) AskUserQuestion is named once, by the protocol that follows this line
HEADLESS_LINE = ("Headless run: this process ends after your final message; background agents, "
                 "wakeups and monitors are lost. Work in the foreground. "
                 # (2026-10-20, 0.4.4 logfix H) a whole-file Read of a big file is denied by the owner's hook
                 "Read large files in slices (offset/limit) or grep; never read a whole file over ~20 KB.")

GOAL_FIRST_TEMPLATE = """GOAL: {prompt}
{attachments}
Work toward this goal autonomously. You will be resumed in later rounds, so make concrete progress
each round.
{headless}
""" + PROTOCOL_BLOCK

GOAL_NEXT_TEMPLATE = (
    "Continue pursuing the GOAL from the start of this session. Round {n} of max {max_rounds}.\n"
    "GOAL: {goal}\n\n" + PROTOCOL_BLOCK
)

# (2026-10-17, 0.4.1 R5) a goal round in a NEW session (no --resume): the goal plus the handoff.
# Deviation from the report's text: the R4 headless line sits before the protocol (a new session).
GOAL_FRESH_TEMPLATE = (
    "GOAL: {goal}\n{attachments}\nRound {n} of max {m}, new session. Progress so far (from your "
    "previous round):\n{handoff}\n\n{headless}\n\n" + PROTOCOL_BLOCK
)
# (2026-10-17, 0.4.1 R6) a reply / resume in a NEW session
# (2026-10-19, 0.4.2 N3) the previous final message between explicit delimiter lines
PREVIOUS_ENDED = ("Your previous attempt ended with:\n--- previous final message ---\n{text}\n"
                  "--- end ---")
NO_PREVIOUS_MESSAGE = "(no final message was recorded)"
GOAL_HANDOFF_HEADER = "Progress so far (from your previous round):\n{handoff}"

# The existing task resume prompt is followed by this for a goal (limit wait, stop, pause, crash).
GOAL_RESUME_SUFFIX = "\nGOAL: {goal}\n\n" + PROTOCOL_BLOCK

TASK_FILE_REF = "Read the file {path} and carry out the task it describes."
# (2026-10-21, subtasks) before a subtask's own prompt; its main task ran first
SUBTASK_LINE = 'This is a subtask of the main task "{label}", which is already done. Do this subtask only.'
GOAL_FILE_REF = "(the full goal text is in the file {path}; read it first)"
GOAL_CUT_REF = "\n[... the goal continues; its full text is in the file {path}]"

_FENCE = re.compile(r"^(`{3,}|~{3,})[\w+-]*$")
# (2026-10-13, tester fix 2) Markers match case-insensitively, after normalisation.
# A trailing `_` wrapper (`_GOAL_BLOCKED_ - x`) is allowed; `GOAL_COMPLETED` / `GOAL_COMPLETE_X` are not.
_MARKER = re.compile(r"^(GOAL_COMPLETE|GOAL_CONTINUE|GOAL_BLOCKED)(?![A-Za-z0-9]|_+[A-Za-z0-9])(.*)$", re.S | re.I)
_TASK_MARKER = re.compile(r"^(TASK_DONE|TASK_BLOCKED)(?![A-Za-z0-9]|_+[A-Za-z0-9])(.*)$", re.S | re.I)
_WRAP = " \t*_`#>"
_REASON_SEP = ":-\u2013\u2014"  # separators between GOAL_BLOCKED and its reason


def is_goal(item: dict) -> bool:
    return item.get("kind") == KIND_GOAL


def caps(item: dict) -> tuple[int, float]:
    """(max_rounds, max_hours) with the defaults for a goal stored without them (amendment M14)."""
    try:
        rounds = int(item.get("max_rounds"))
        if not (1 <= rounds <= MAX_ROUNDS_LIMIT or rounds == UNLIMITED_ROUNDS):
            raise ValueError
    except (TypeError, ValueError):
        rounds = DEFAULT_MAX_ROUNDS
    try:
        hours = float(item.get("max_hours"))
        if not (0 < hours <= MAX_HOURS_LIMIT):
            raise ValueError
    except (TypeError, ValueError):
        hours = DEFAULT_MAX_HOURS
    return rounds, hours


def rounds_cap_text(max_rounds: int) -> str:
    """The round cap for prompts and logs: the number, or "unlimited" for -1."""
    return "unlimited" if max_rounds == UNLIMITED_ROUNDS else str(max_rounds)


def attachment_block(paths: list[str] | None) -> str:
    """The attachment block (no trailing newline), or "" without attachments."""
    paths = [p for p in (paths or []) if p]
    if not paths:
        return ""
    return "\n".join([ATTACH_HEADER, *(f"- {p}" for p in paths)])


def headless_line(profile: str | None, max_agents: object = None) -> str:
    """(2026-10-17, 0.4.1 R4) The "Headless run" line with the profile's tool phrase.
    (2026-10-21, subagents) `max_agents`: the item's own subagent limit, named in the same phrase."""
    tools = TOOL_PHRASES.get(profile or "safe")
    if not tools:
        return HEADLESS_LINE
    return f"{HEADLESS_LINE} Tools: {tools}{agents_phrase(normalise_agents(max_agents))}."


def subtask_prefix(item: dict) -> str:
    """(2026-10-21, subtasks) "" for a main task; for a subtask one line naming its main task (it
    only runs once the main task is done), then a blank line."""
    label = item.get("parent_label")
    return SUBTASK_LINE.format(label=label) + "\n\n" if item.get("parent") and label else ""


def _task_body(item: dict, prompt_file: str | None) -> str:
    block = attachment_block(item.get("attachments"))
    body = subtask_prefix(item) + (TASK_FILE_REF.format(path=prompt_file) if prompt_file else item["prompt"])
    return f"{body}\n\n{block}" if block else body


def first_round_prompt(item: dict, prompt_file: str | None = None) -> str:
    """The prompt of an item's first attempt. With `prompt_file`, the user prompt itself is replaced
    by a pointer to that file; the goal wrapper and attachments stay inline (amendment M2).
    (2026-10-17, 0.4.1 R4) It carries the headless line; resumes do not."""
    headless = headless_line(item.get("profile"), item.get("max_agents"))
    if is_goal(item):
        block = attachment_block(item.get("attachments"))
        body = subtask_prefix(item) + (GOAL_FILE_REF.format(path=prompt_file) if prompt_file else item["prompt"])
        return GOAL_FIRST_TEMPLATE.format(prompt=body, attachments=(block + "\n") if block else "",
                                          headless=headless)
    return f"{_task_body(item, prompt_file)}\n\n{headless}\n\n{TASK_PROTOCOL_LINE}"


def task_resume_prompt(resume_prompt: str) -> str:
    """(2026-10-15, SPEC_V03 5.1) A task's resume prompt restates the end-of-message protocol."""
    return f"{resume_prompt}\n\n{TASK_PROTOCOL_LINE}"


def protocol_for(item: dict) -> str:
    """The task or goal end-of-message protocol (the last part of a reply prompt)."""
    return PROTOCOL_BLOCK if is_goal(item) else TASK_PROTOCOL_LINE


def reply_prompt(item: dict, text: str, reply_file: str | None = None) -> str:
    """(2026-10-15, SPEC_V03 5.2) `Owner's reply:\n<text>\n\n<protocol>`; with `reply_file`, a long
    reply is replaced by a pointer to that file (the long-prompt mechanism)."""
    body = REPLY_FILE_REF.format(path=reply_file) if reply_file else text
    return REPLY_TEMPLATE.format(text=body, protocol=protocol_for(item))


def needs_restate_file(item: dict) -> bool:
    return is_goal(item) and len(item.get("prompt") or "") > GOAL_RESTATE_MAX


def restated_goal(item: dict, prompt_file: str | None = None) -> str:
    """The goal text for later prompts: in full, or its first 1500 chars + the file path (M3)."""
    text = item.get("prompt") or ""
    if len(text) <= GOAL_RESTATE_MAX:
        return text
    tail = GOAL_CUT_REF.format(path=prompt_file) if prompt_file else "\n[... the goal continues]"
    return text[:GOAL_RESTATE_MAX] + tail


def next_round_prompt(item: dict, prompt_file: str | None = None) -> str:
    """(2026-10-17, 0.4.1 R5) A same-session next round restates only the goal's first 300 chars:
    the full goal is already in the session's history."""
    n = int(item.get("rounds") or 0) + 1
    max_rounds, _hours = caps(item)
    text = item.get("prompt") or ""
    if len(text) > NEXT_ROUND_RESTATE_MAX:
        tail = (GOAL_CUT_REF.format(path=prompt_file) if prompt_file
                else "\n[... the goal continues; see the GOAL at the start of this session]")
        text = text[:NEXT_ROUND_RESTATE_MAX] + tail
    return GOAL_NEXT_TEMPLATE.format(n=n, max_rounds=rounds_cap_text(max_rounds), goal=text)


def goal_resume_prompt(resume_prompt: str, item: dict, prompt_file: str | None = None) -> str:
    return resume_prompt + GOAL_RESUME_SUFFIX.format(goal=restated_goal(item, prompt_file))


# ---- fresh sessions (2026-10-17, 0.4.1 R5/R6) ------------------------------------------------------
def _handoff_text(item: dict) -> str:
    return item.get("handoff") or "(no progress notes were recorded)"


def _goal_text(item: dict, prompt_file: str | None) -> str:
    return subtask_prefix(item) + (GOAL_FILE_REF.format(path=prompt_file) if prompt_file else item["prompt"])


def fresh_round_prompt(item: dict, prompt_file: str | None = None) -> str:
    """R5: a goal round in a new session: the full goal (or a pointer to its file), the
    attachments, the round line, the stored PROGRESS handoff, the headless line, the protocol."""
    n = int(item.get("rounds") or 0) + 1
    max_rounds, _hours = caps(item)
    block = attachment_block(item.get("attachments"))
    return GOAL_FRESH_TEMPLATE.format(goal=_goal_text(item, prompt_file),
                                      attachments=(block + "\n") if block else "", n=n,
                                      m=rounds_cap_text(max_rounds), handoff=_handoff_text(item),
                                      headless=headless_line(item.get("profile"), item.get("max_agents")))


def fresh_resume_prompt(item: dict, resume_prompt: str, prompt_file: str | None = None,
                        reply: str | None = None, reply_file: str | None = None) -> str:
    """R6: a reply or a resume in a new session: the original task (or goal + handoff), the headless
    line, `Your previous attempt ended with:` + the last final message, then the owner's reply block
    (which ends with the protocol) or the resume text + the protocol."""
    if is_goal(item):
        block = attachment_block(item.get("attachments"))
        context = (f"GOAL: {_goal_text(item, prompt_file)}\n" + (block + "\n" if block else "")
                   + GOAL_HANDOFF_HEADER.format(handoff=_handoff_text(item)))
    else:
        context = _task_body(item, prompt_file)
    parts = [context, headless_line(item.get("profile"), item.get("max_agents")),
             PREVIOUS_ENDED.format(text=item.get("last_message") or NO_PREVIOUS_MESSAGE)]
    if reply is not None or reply_file:
        parts.append(reply_prompt(item, reply or "", reply_file))
    else:
        parts.append(f"{resume_prompt}\n\n{protocol_for(item)}")
    return "\n\n".join(parts)


def start_fresh(item: dict, mode: str) -> None:
    """Drop the session so the next attempt runs without --resume; the old id goes to
    `prior_sessions`. `mode` (FRESH_ROUND / FRESH_RESUME) picks the prompt of that attempt."""
    sid = item.get("session_id")
    if sid:
        prior = item.get("prior_sessions") if isinstance(item.get("prior_sessions"), list) else []
        prior.append(sid)
        item["prior_sessions"] = prior
    item["session_id"] = None
    item["fresh"] = mode
    item["session_rounds"] = 0
    item["last_ctx"] = None


def _int_setting(item: dict, key: str, default: int) -> int:
    try:
        value = int(item.get(key))
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def wants_fresh_round(item: dict, last_ctx: int | None) -> str | None:
    """R5: why the NEXT round of a goal should start a new session, or None. `last_ctx` is the
    context of the last main-thread assistant message of the round that just ended."""
    at = _int_setting(item, "fresh_at_ctx", DEFAULT_FRESH_AT_CTX)
    every = _int_setting(item, "fresh_every", DEFAULT_FRESH_EVERY)
    if at and last_ctx is not None and last_ctx >= at:
        return f"context {last_ctx} >= fresh_at_ctx {at}"
    if every and int(item.get("session_rounds") or 0) >= every:
        return f"{item.get('session_rounds')} rounds in this session (fresh_every {every})"
    return None


def wants_fresh_resume(item: dict, now: datetime) -> bool:
    """R6: resume in a new session when the previous attempt ended more than RESUME_FRESH_AFTER_S
    ago (cold cache) and its last context was at least RESUME_FRESH_MIN_CTX tokens."""
    if not item.get("session_id"):
        return False
    ended = parse_iso(item.get("last_ended_at"))
    ctx = item.get("last_ctx")
    if ended is None or not isinstance(ctx, int):
        return False
    return ctx >= RESUME_FRESH_MIN_CTX and (now - ended).total_seconds() > RESUME_FRESH_AFTER_S


def _normalise(line: str) -> str:
    text = line
    while True:
        before = text
        text = text.strip(_WRAP).rstrip(".!:;").strip(_WRAP)
        if text == before:
            return text


def _last_marker(text: str | None, pattern: re.Pattern) -> tuple[str | None, str]:
    """(MARKER upper-cased, rest) from the last line that is neither blank nor a code fence
    (amendment H3), or (None, "") when that line holds no marker. Shared by goals and tasks."""
    for raw in reversed((text or "").splitlines()):
        stripped = raw.strip()
        if not stripped or _FENCE.match(stripped):
            continue
        m = pattern.match(_normalise(stripped))
        if not m:
            return None, ""
        return m.group(1).upper(), m.group(2)
    return None, ""


def _reason(rest: str) -> str:
    # (2026-10-13, tester fix 3) wrappers first, then separators, e.g. `**GOAL_BLOCKED**: x` -> x
    return _normalise(rest.lstrip(_WRAP + _REASON_SEP)) or "(no reason given)"


def parse_marker(text: str | None) -> tuple[str, str | None]:
    """Goal marker: ("complete" | "continue" | "blocked" | "none", reason)."""
    mark, rest = _last_marker(text, _MARKER)
    if mark == MARK_COMPLETE:
        return "complete", None
    if mark == MARK_CONTINUE:
        return "continue", None
    if mark == MARK_BLOCKED:
        return "blocked", _reason(rest)
    return "none", None


def parse_task_marker(text: str | None) -> tuple[str, str | None]:
    """(2026-10-15, SPEC_V03 5.1) Task marker: ("done" | "blocked" | "none", reason)."""
    mark, rest = _last_marker(text, _TASK_MARKER)
    if mark == MARK_TASK_DONE:
        return "done", None
    if mark == MARK_TASK_BLOCKED:
        return "blocked", _reason(rest)
    return "none", None


# ---- structured blocked question (2026-10-17, 0.4.1 R3) --------------------------------------------
_OPTION = re.compile(r"^OPTION\s+([1-4])\s*(\((recommended|suositeltu)\))?\s*[:.)\-–—]\s*(.+)$",
                     re.I | re.S)
# (2026-10-19, 0.4.2 L1) only LEADING list bullets, blockquote and heading markers are dropped
_LEAD = re.compile(r"^(?:[-+*•]\s+|\d+[.)]\s+|>\s*|#{1,6}\s+)")
# a markdown wrapper around the whole line, or around just the "OPTION <n> (tag):" head
_WRAPPERS = ("**", "__", "~~", "`", "*", "_")
_HEAD = r"OPTION\s+[1-4]\s*(?:\([^)\n]*\))?\s*[:.)\-–—]?"
_LABEL_SEPS = (" - ", " – ", " — ")
OPTIONS_MIN, OPTIONS_MAX, LABEL_MAX = 2, 4, 80


def _marker_index(lines: list[str]) -> int | None:
    """Index of the last line that is neither blank nor a code fence (the marker line, H3)."""
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped and not _FENCE.match(stripped):
            return i
    return None


def _unwrap(text: str) -> str:
    """(2026-10-19, 0.4.2 L1) Drop one symmetric markdown wrapper: around the whole line
    (`**OPTION 1: x**`, `` `OPTION 1: x` ``) or around the head only (`**OPTION 1:** x`,
    `**OPTION 1**: x`). Characters inside the text are never touched."""
    for w in _WRAPPERS:
        if not text.startswith(w):
            continue
        head = re.match(re.escape(w) + "(" + _HEAD + r")\s*" + re.escape(w), text, re.I)
        if head:
            return (head.group(1) + " " + text[head.end():].lstrip()).strip()
        inner = text[len(w):-len(w)] if len(text) > 2 * len(w) and text.endswith(w) else ""
        if inner and not inner[0].isspace() and not inner[-1].isspace():
            return inner
    return text


def _option_line(raw: str) -> re.Match | None:
    """Match one OPTION line after dropping leading bullets / `>` / `#` markers and a symmetric
    markdown wrapper (0.4.2 L1: never characters inside the text or at its end)."""
    text = raw.strip()
    while True:
        before = text
        text = _unwrap(_LEAD.sub("", text).strip())
        if text == before:
            break
    return _OPTION.match(text)


def parse_options(text: str | None) -> list[dict] | None:
    """The OPTION lines right above the last marker line, or None when they are not a valid set.

    Walks upward from the marker, skipping blank lines; stops at the first other line or a code
    fence. Valid: 2-4 options with unique numbers. `label` is the text before the first " - ",
    " – " or " — " (at most 80 chars), `detail` the rest; `recommended` is set on the first
    option tagged `(recommended)` / `(suositeltu)` only, never assumed for option 1."""
    lines = (text or "").splitlines()
    end = _marker_index(lines)
    if end is None:
        return None
    found: list[re.Match] = []
    for raw in reversed(lines[:end]):
        stripped = raw.strip()
        if not stripped:
            continue
        if _FENCE.match(stripped):
            break
        m = _option_line(stripped)
        if not m:
            break
        found.append(m)
    found.reverse()
    numbers = [int(m.group(1)) for m in found]
    if not (OPTIONS_MIN <= len(found) <= OPTIONS_MAX) or len(set(numbers)) != len(numbers):
        return None
    options: list[dict] = []
    tagged = False
    for m in found:
        body = m.group(4).strip()
        label, detail = body, ""
        cuts = [(body.find(sep), sep) for sep in _LABEL_SEPS if body.find(sep) > 0]
        if cuts:
            at, sep = min(cuts)
            label, detail = body[:at].strip(), body[at + len(sep):].strip()
        label, detail = _fit_label(label, detail)
        recommended = bool(m.group(2)) and not tagged
        tagged = tagged or recommended
        options.append({"n": int(m.group(1)), "label": label, "detail": detail,
                        "recommended": recommended})
    return options


def _fit_label(label: str, detail: str) -> tuple[str, str]:
    """(2026-10-26, owner d7f2e585 "why text isent full?") A label over LABEL_MAX was cut hard and
    the rest was lost (`... terminal shows (e`, from "(e.g. ...)"). Now it ends at a word with "…"
    (still at most LABEL_MAX chars) and the rest starts the detail, so the whole option is shown."""
    if len(label) <= LABEL_MAX:
        return label, detail
    cut = label.rfind(" ", 0, LABEL_MAX)
    if cut <= LABEL_MAX // 2:
        cut = LABEL_MAX - 1  # no word end in the second half: a hard cut, still marked
    rest = label[cut:].strip()
    return label[:cut].rstrip() + "…", "…" + rest + (" — " + detail if detail else "")


def blocked_question(text: str | None, reason: str) -> dict:
    """`{text, options}` for a blocked item; `options` is [] when the OPTION lines are missing or
    invalid (the owner then answers in free text, as before)."""
    return {"text": reason, "options": parse_options(text) or []}


# ---- PROGRESS handoff (2026-10-17, 0.4.1 R5) -------------------------------------------------------
_PROGRESS = re.compile(r"^PROGRESS\s*:\s*(.*)$", re.I | re.S)


def extract_handoff(text: str | None) -> str | None:
    """The last `PROGRESS:` block (up to the first OPTION line or the marker line, at most 3000
    chars), or the last 1500 chars of the final message when there is none; None for an empty
    message."""
    lines = (text or "").splitlines()
    end = _marker_index(lines)
    if end is None:
        return None
    body_lines = lines[:end] if (_last_marker(text, _MARKER)[0] or _last_marker(text, _TASK_MARKER)[0]) \
        else lines[:end + 1]
    for i in range(len(body_lines) - 1, -1, -1):
        m = _PROGRESS.match(body_lines[i].replace("*", "").strip(" \t#>_"))
        if m:
            first = m.group(1).strip()
            rest = body_lines[i + 1:]
            # (2026-10-19, 0.4.2 L3) the block stops at the first OPTION line: a fresh resume
            # already quotes the whole final message (PREVIOUS_ENDED), options included
            cut = next((k for k, line in enumerate(rest) if _option_line(line)), len(rest))
            block = "\n".join(([first] if first else []) + rest[:cut]).strip()
            if block:
                return block[:HANDOFF_MAX]
    tail = (text or "").strip()
    return tail[-HANDOFF_FALLBACK:] if tail else None


def apply_task_result(item: dict, result_text: str | None, denials: list, now: datetime) -> tuple[str, str]:
    """(2026-10-15, SPEC_V03 5.1) A task's DONE result: TASK_BLOCKED -> blocked; permission denials
    without TASK_DONE -> blocked; TASK_DONE or no marker -> done (a missing marker is logged).
    `result_text` must be the FULL result. Sets error/finished_at/wait_until; returns (status, why)."""
    item["wait_until"] = None
    item["finished_at"] = iso(now)
    item["blocked_question"] = None
    marker, reason = parse_task_marker(result_text)
    if marker == "blocked":
        item["error"] = f"blocked: {reason}"
        # (2026-10-17, 0.4.1 R3) the question and its OPTION lines, for one-click replies
        item["blocked_question"] = blocked_question(result_text, reason)
        return STATUS_BLOCKED, f"TASK_BLOCKED: {reason}"
    # (2026-10-21, v049 dontask_hold, SPEC_V049_DONTASK_HOLD 1) a hook denial is a note: only the
    # other denials block, and the block names them (tool + command head or path, at most 3)
    block = summarise(denials, item.get("profile"), item.get("cwd")) if marker != "done" else None
    if block:
        what = block_text(block)
        item["error"] = f"blocked: {what}"
        return STATUS_BLOCKED, f"{what} and no TASK_DONE marker"
    item["error"] = None
    # (2026-10-26, owner "failed or not complete tasks own subcategory") done without TASK_DONE: the
    # Task tree shows it under "Not completed", not "Completed" (`result` is cut, so the flag is kept)
    item["no_marker"] = marker == "none"
    if marker == "none":
        log.warning("item %s: no TASK_DONE/TASK_BLOCKED marker on the last line; counted as done",
                    item.get("id"))
        return "done", "result success (no task marker; counted as done)"
    return "done", "TASK_DONE"


def apply_goal_round(item: dict, result_text: str | None, now: datetime,
                     max_turns: bool = False, last_ctx: int | None = None) -> tuple[str, str]:
    """Apply one finished round (spec 3.2 + amendments H2, H3, M1, M4).

    `result_text` must be the FULL result, not the stored (truncated) one (amendment M1).
    Sets rounds/limit_waits/no_marker_rounds/error/finished_at; returns (new status, reason).
    (2026-10-17, 0.4.1) Also stores the PROGRESS handoff (R5), the blocked question (R3), and for a
    round that goes on decides whether the next round starts a new session (R5, `last_ctx`).
    """
    item["rounds"] = int(item.get("rounds") or 0) + 1
    rounds = item["rounds"]
    item["session_rounds"] = int(item.get("session_rounds") or 0) + 1
    item["blocked_question"] = None
    handoff = extract_handoff(result_text)
    if handoff:
        item["handoff"] = handoff
    elif max_turns:
        note = f"(round {rounds} hit the turn cap before writing PROGRESS)"
        item["handoff"] = ((item.get("handoff") or "") + "\n" + note).strip()[-HANDOFF_MAX:]
    item["limit_waits"] = 0
    item["wait_until"] = None
    item["goal_continue"] = False
    max_rounds, max_hours = caps(item)
    item["max_rounds"], item["max_hours"] = max_rounds, max_hours
    marker, reason = ("max_turns", None) if max_turns else parse_marker(result_text)

    def fail(error: str, why: str) -> tuple[str, str]:
        item["error"] = error
        item["finished_at"] = iso(now)
        return "failed", why

    if marker == "complete":
        item["no_marker_rounds"] = 0
        item["error"] = None
        item["finished_at"] = iso(now)
        return "done", f"GOAL_COMPLETE after round {rounds}"
    if marker == "blocked":
        # (2026-10-15, SPEC_V03 5.1) GOAL_BLOCKED is `blocked` (Claude needs the owner), no longer failed
        item["no_marker_rounds"] = 0
        item["error"] = f"blocked: {reason}"
        item["blocked_question"] = blocked_question(result_text, reason)
        item["finished_at"] = iso(now)
        return STATUS_BLOCKED, f"GOAL_BLOCKED after round {rounds}: {reason}"
    if marker == "continue":
        item["no_marker_rounds"] = 0
        why = "GOAL_CONTINUE"
    elif marker == "max_turns":
        why = "round ended at --max-turns (error_max_turns); counted as a round"
    else:
        item["no_marker_rounds"] = int(item.get("no_marker_rounds") or 0) + 1
        why = f"no goal marker on the last line ({item['no_marker_rounds']} in a row)"
        if item["no_marker_rounds"] >= NO_MARKER_LIMIT:
            return fail(f"no goal marker for {NO_MARKER_LIMIT} consecutive rounds", why)
    if max_rounds != UNLIMITED_ROUNDS and rounds >= max_rounds:
        return fail(f"goal cap: max_rounds {max_rounds} reached", f"{why}; max_rounds reached")
    started = parse_iso(item.get("goal_started_at"))
    # Review L1: max_hours only stops a goal that would go on; a COMPLETE after 72 h is still done.
    if started is not None and (now - started).total_seconds() >= max_hours * 3600:
        return fail(f"goal cap: max_hours {_num(max_hours)} reached", f"{why}; max_hours reached")
    item["error"] = None
    item["goal_continue"] = True
    fresh_why = wants_fresh_round(item, last_ctx)
    if fresh_why:
        log.info("item %s: the next round starts a new session (%s); session %s kept in prior_sessions",
                 item.get("id"), fresh_why, item.get("session_id"))
        start_fresh(item, FRESH_ROUND)
        return "pending", f"{why}; round {rounds} done, the next round starts a new session ({fresh_why})"
    return "pending", f"{why}; round {rounds} done, the next round resumes the session"


def _num(value) -> str:
    f = float(value)
    return str(int(f)) if f.is_integer() else str(f)

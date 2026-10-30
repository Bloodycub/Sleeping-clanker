"""(2026-10-26, limit_continue) A handoff for the attempt a usage limit cut short, built from its run
log (runs/<id>/<n>.jsonl) without any model call: a limit makes a summarising call impossible.

After a long limit wait the prompt cache is cold. With a big context the next attempt starts a new
session (goals.wants_fresh_resume, docs/SPEC_V041_TOKENS.md R6) whose prompt carries
`item["last_message"]` as "Your previous attempt ended with:". A limit attempt has no real final
message (its last one is the CLI's "You've hit your limit"), so this text takes its place: the files
the attempt changed, its last shell commands and the tail of its last messages. Read-only; a missing
or unreadable log gives None.
"""

from __future__ import annotations

import json
from pathlib import Path

from .log import get_logger

log = get_logger(__name__)

HANDOFF_MAX = 3000          # = goals.LAST_MESSAGE_MAX (the slot this text fills)
FILES_MAX = 30
COMMANDS_MAX = 5
COMMAND_CHARS = 200
TEXTS_MAX = 3
HEADER = ("(The usage limit cut this attempt short, so it has no final message. Built from its run log, "
          "without a model call.)")
_EDIT_TOOLS = {"Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path", "NotebookEdit": "notebook_path"}
_SHELL_TOOLS = ("Bash", "PowerShell")
SYNTHETIC_MODEL = "<synthetic>"  # the CLI's own "You've hit your limit" message, never the model's


def _main_assistant_parts(ev: dict) -> list:
    if ev.get("type") != "assistant" or ev.get("parent_tool_use_id") is not None:
        return []
    msg = ev.get("message")
    if not isinstance(msg, dict) or msg.get("model") == SYNTHETIC_MODEL:
        return []
    parts = msg.get("content")
    return [p for p in parts if isinstance(p, dict)] if isinstance(parts, list) else []


def from_run_log(path: Path | str | None, max_chars: int = HANDOFF_MAX) -> str | None:
    """The handoff text of one attempt's run log, or None (no log, or nothing worth handing off)."""
    if not path:
        return None
    files: list[str] = []
    commands: list[str] = []
    texts: list[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(ev, dict):
                    continue
                for p in _main_assistant_parts(ev):
                    if p.get("type") == "text" and str(p.get("text") or "").strip():
                        texts.append(str(p["text"]).strip())
                        del texts[:-TEXTS_MAX]
                    elif p.get("type") == "tool_use":
                        inp = p.get("input") if isinstance(p.get("input"), dict) else {}
                        key = _EDIT_TOOLS.get(str(p.get("name")))
                        if key and isinstance(inp.get(key), str) and inp[key] not in files:
                            files.append(inp[key])
                        elif p.get("name") in _SHELL_TOOLS and isinstance(inp.get("command"), str):
                            commands.append(" ".join(inp["command"].split())[:COMMAND_CHARS])
                            del commands[:-COMMANDS_MAX]
    except OSError as exc:
        log.warning("limit handoff: run log %s unreadable: %s", path, exc)
        return None
    if not (files or commands or texts):
        return None
    head = [HEADER]
    if files:
        shown = files[-FILES_MAX:]
        more = f" (+{len(files) - len(shown)} more)" if len(files) > len(shown) else ""
        head.append("Files you changed: " + ", ".join(shown) + more)
    if commands:
        head.append("Your last commands:\n" + "\n".join(f"- {c}" for c in commands))
    text = "\n".join(head)
    if texts:
        room = max_chars - len(text) - len("\nYour last messages:\n")
        tail = "\n\n".join(texts)
        if room > 1:
            text += "\nYour last messages:\n" + (tail if len(tail) <= room else "…" + tail[-(room - 1):])
    return text[:max_chars]

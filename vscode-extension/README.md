# Sleeping Clanker (VS Code)

Queue Claude Code tasks and goals from VS Code and let them run unattended. When a subscription
usage limit is hit, the runner waits for the reset and resumes the same Claude session.

The extension is a thin native UI over the bundled `sleeping_clanker` Python core (stdlib only). It
uses one locked-down webview for the Task view: no remote content, only its own script. It opens no
network listener and sends no telemetry.

## Requirements

- Windows, Python 3.11+ (`py -3` or `python`, or set `claudAutopilot.pythonPath`).
- Claude Code (`claude.exe`) installed and logged in.

## One queue per project

Each window shows and runs only its own project's queue: the window's first folder. Two windows on
two projects have two independent queues and runners. **Show All Projects** (the layers button in
the Queue title) groups every project's queue; this window's project comes first. Item actions work
on any project's items, and each project node has its own Start/Stop. A runner started for another
project is still a child of this window and stops when this window closes.

If the old shared queue holds tasks for this folder, the extension offers once to move them.

## Several queues per project (v0.3)

**New Queue…** (the Queue view's `…` menu, or the + Add menu) makes a second queue for the same
project, with its own runner. With two or more queues the view shows one node per queue (`Main` is
the original one) with its own ▶/■ and + buttons; adding then asks which queue (the last used comes
first). Start/Stop from the status bar or the palette asks for a queue, or `All queues`. Two queues
work in the same folder at the same time, so give them tasks on different files (a warning says so
once). **Remove Queue** (right-click an empty, stopped queue) moves its folder to `lanes\.removed`.

## Task view (0.4.0)

**Double-click** a task (or right-click → Open Task View, or the inline button on a running task) to
open its Task view beside the editor, laid out like the Claude Code and Codex chat panels:

- a header with the task name, a colored status pill, the profile, the queue, the folder and a
  **Raw log** link;
- your prompt (and every reply you sent) as a chat bubble on the right; long prompts fold after 12
  lines;
- Claude's answers rendered as Markdown: tables, bold, lists, `inline code`, code blocks;
- each tool call as one compact row (`Grep  TODO  in C:\code\…`, `Read  notes.md`)
  with a status dot and a short result (`14 lines`); click a row to see the result (up to 4 KB);
- red rows for permission denials, a yellow "Paused until HH:MM" row for usage limits, stderr folded;
- per attempt a divider (`Attempt 2 · time · model · session`) and a footer
  (`✓ success · 11 turns · $0.572 · completed`); the `TASK_DONE` / `TASK_BLOCKED` / `GOAL_…` line
  becomes a badge (✓ Done, ✗ Needs you: …, ↻ Continuing);
- while the task runs, a "Working…" row with the elapsed time; the view follows new output only when
  you are already at the bottom;
- a reply box at the bottom (Enter sends, Shift+Enter is a new line, with a profile dropdown). It works when the task has
  stopped (blocked, done, failed or cancelled) or is paused, and sends exactly like **Reply…**; a reply
  to a paused task un-pauses it (one queued reply at a time: **Edit reply…** changes it). The box grows when
  you resize it and the conversation keeps its last lines visible above it;
- 📎 **Add picture or file** at the left of the bottom bar: pick files (e.g. a screenshot of the
  error, a log); their paths are listed in your reply and Claude opens them (pictures too);
- **▶ Start now** on a pending task: it moves to the front of its queue and the runner starts (a busy
  runner finishes its current task first);
- (0.4.1) when Claude blocked with a question and numbered options (`OPTION 1 (recommended): …`
  lines above `TASK_BLOCKED: …`), the question and one button per option above the reply box: the
  recommended option first, as the primary button with a **Recommended** tag, each option's detail
  as a muted second line. A click sends `Option <n>: <label>` the same way as the reply box (an option
  that mentions a shell, git, PowerShell or a komento resumes with `shell-read` unless you picked a
  profile in the dropdown). **Other…** puts the cursor in the reply box. The OPTION lines are not
  repeated in Claude's answer while they are buttons.
- (0.4.3) an open Task view comes back after **Developer: Reload Window**. The view keeps only the
  task id, its project and queue (and your unsent reply). The extension checks them with the core
  and shows "This task is no longer available" when the task is gone.
- (2026-10-21) **ADHD mode**: pick it in the **View** dropdown next to **Raw log**. The view then shows
  only your messages, Claude's answers, the badges (✓ Done, ✗ Needs you), pauses and "Working…". Tool
  calls, attempt lines, interim messages, denials, CLI notes and stderr fold into
  `▸ N hidden steps` rows; a click opens one in place. A long question, badge or "blocked:" line
  shows its first sentence with `…`; a click (or the tooltip) shows the rest. The choice applies to
  every Task view, including new ones and after a restart. **Full chat** shows everything again.

The webview is locked down: a strict Content-Security-Policy, only the extension's own script (with a
fresh nonce), no remote content, no command links. Claude's text is escaped before the Markdown is
rendered, so HTML in an answer is shown as text. Links open in the browser only for `https://`;
(0.4.1) an `http://` link is shown as plain text with its address, and an image is shown as the text
`alt (image: url)` (nothing is loaded). Very long or deeply nested answers are rendered safely (deeper
than 20 levels, or beyond 20 000 characters on one line, the rest is plain text).

**Open Raw Log** (right-click, or **Raw log** in the view) opens the plain-text log as before: each
attempt's header, Claude's text, `▶` tool calls with `↳` results, `⛔` denials, `⏸` usage-limit
pauses, the result and any stderr. (0.4.0) The result line no longer repeats the final answer. Set
`claudAutopilot.taskView` to `text` to make the double-click open the raw log instead. Long runs show
the last 2 MB per attempt. A single click only selects the task.

## Needs you: blocked tasks and Reply (v0.3)

Claude ends every task with `TASK_DONE` or `TASK_BLOCKED: <what it needs>`. A blocked task (or one
that hit permission denials without finishing) gets a yellow question mark and **Needs you**; the
status bar shows `· N need you` on a warning background, and the Sleeping Clanker icon in the sidebar shows
the count as a badge. **Reply…** (inline on the task, right-click, or the goal bar)
sends your answer: type it and press Enter, or press Enter on an empty box to write a long reply in
an editor and click **Send reply**. You can switch the profile for the resumed session; when Claude
said it needs a shell, `shell-read` is offered first. The task resumes the same Claude session when
the runner reaches it. A task that needs ssh can only report blocked: no profile allows ssh.

(0.4.1) When Claude's question came with numbered options, **Reply…** (and the goal bar's Reply)
first shows a list of them, the recommended one first and marked ★ Recommended, plus **Write my own
reply…**, which opens the input box as before. Picking an option sends `Option <n>: <label>`; when
the option mentions a shell, git, PowerShell or a komento, `shell-read` is first in the profile list.

(0.4.4) **Auto-pick best** — the box next to the profile dropdown in a Task view — answers such a
question for you with the best (recommended) option, so a task that asks something at 02:00 keeps
going. It is **off by default**: turn it on per chat, or for every new chat with
`claudAutopilot.autoPickBestOption`. Each automatic answer is one note line in the chat
(`Auto-picked Option 1: …`) and one `>>` line in the output channel. It never widens the task's
permission profile (a manual click on a shell option still may), answers one question once, and
stops after five automatic answers in a row until you reply yourself. Nothing is answered
automatically unless that task's chat is open, and a free-text question (no options) always waits.

## Use

- **Queue view**: every task and goal with its status. Hover an item to see what is happening now
  and what happens next. Inline: Edit, Pause, Resume and Delete. Right-click: Show Details, Edit Task…, Retry,
  Cancel, Pause, Resume, Delete, Open Task View, Open Raw Log, Reply… and Open Run Log Folder. Deleting a running item
  cancels it first. A failed task shows a slashed circle; a blocked one a yellow question mark.
- **Status bar counts** (2026-10-21): right of the main item, coloured counts —
  green `$(pass) N` complete, yellow `$(question) N` need your answer, red `$(error) N` failed, blue
  `N` not done yet (pending, running, waiting or paused). A count of 0 is hidden; a click opens the
  queue view.
- **Edit Task…** (2026-10-21): change a task's text until it has started (pending, paused, waiting, or failed
  or cancelled before its first run). The text goes into an edit box at the top of the New Task panel
  (2026-10-26; no input box, no new editor): change it there and press **Save** (Ctrl+Enter) or **Cancel edit** (Esc). The task keeps its place in the queue. A started task has a session,
  so Claude already has its text: use **Reply…** instead.
- **Status groups**: each queue is sorted into **Questions** (a task waits for your answer; always on
  top), **Running**, **Not done yet** (pending, paused), **Errors** and **Complete** (done or cancelled, newest first). Click a group's arrow to hide or
  show it; Complete starts hidden. Finished rows show when they ended (`done 14:32`, `failed 10-20
  09:05`). This is display only: nothing is sent to Claude, so it costs no tokens. Setting
  `claudAutopilot.groupByStatus` (off = one flat list).
- **Delete**: the trash button on a row. A done or cancelled task goes at once; anything else (or a
  task with subtasks) asks first. The trash button on **Complete** or **Errors** deletes the whole
  group after one confirmation.
- **Subtasks**: the `+` on a task's row (or right-click → Add Subtask…) adds a small task under it,
  e.g. main task "cook" with "bake cookies" and "make ice cream". A subtask runs in the same folder
  with the same permissions, starts once the main task is done, and runs right after it, in the order
  you added them. Its prompt names the main task in one line. Deleting the main task deletes its
  subtasks; cancelling it cancels the unfinished ones. A `+` on a subtask adds to the same main task.
- **Add…** (the + button, the status bar menu, or "Add with these files…" in the Explorer):
  - Task: a one-shot task.
  - Goal: Claude keeps working, round after round, until it reports the goal complete or blocked,
    or a cap is hit (default 50 rounds / 72 hours).
  - Files and folders: attach files or folders (read-only context) to a new task or goal.
  - Plan mode: a read-only task that only makes a plan.
  - Selection / Current file.
- **Defaults for + (0.4.4)**: the rows at the bottom of the Queue view. Click a row to preselect
  what `+` adds (`Add`), the permission profile (`Permissions`), the queue (`Queue`, only with two
  or more queues) and the goal caps (`Goal caps`). The choice is saved in your user settings, so the
  next `+` uses it without asking — with Add = Goal, Permissions = edits and the caps preselected,
  `+` asks only for the goal text. Every row starts at `ask every time`; `Ask every time again` in
  the section's menu clears them. Right-click the section to hide the rows; `Sleeping Clanker:
  Defaults for +…` in the Command Palette still changes them.
- **Status bar**: `Sleeping Clanker: idle | running 1 · 3 pending | waiting until 15:45`. (0.3.3) When
  tasks are pending or waiting and no runner is running for their queue, it turns yellow:
  `Sleeping Clanker: N pending — runner stopped`, and its menu puts **Start runner** first. (0.4.4) While a
  task is running the text is green; yellow wins over green. While a goal is
  active, a second item shows `Pursuing goal: … · 2d 5h 21m 33s`; click it to Pause/Resume, Open
  Task View, Delete or Expand (Reply when the goal needs you).

Profiles:

- `safe` (default, recommended): may read files and edit files only inside this folder; no shell,
  no web.
- `edits`: may read and edit files; no shell commands, no web.
- `plan`: read-only: makes a plan, changes nothing.
- `shell-read` (v0.3.1, tightened in 0.3.2): like `safe`, plus a fixed list of read-only commands:
  exact git forms only (`git status`, `git diff --stat`, `git log --oneline -20`, ...) and the
  read-only cmdlets `Get-ChildItem`/`ls`/`dir`, `Get-Content`/`cat`/`type`, `Select-String`,
  `Test-Path`, `Get-Item`, `findstr`. Every other command (no `grep`, no `git blame`, no wildcard git
  forms) is denied; no deleting, no installs, no ssh, no web. The user guide lists the exact
  commands.

There is deliberately no setting for permission bypass.

## Settings

**Global settings (2026-10-21):** the gear in the Queue view's title bar opens **Language**,
**Defaults for +…**, four switches (**Start runners automatically**, **Split a prompt that lists
several tasks**, **Ask the model for each new task**, **Group the queue by status**; a pick flips it
in your user settings at once) and **All settings…** (the VS Code Settings editor, this extension only).

**Chat settings (2026-10-21):** the **⚙ Chat settings** button at the right end of a Task view's
bottom bar opens the chat box's own settings: **ADHD mode** (every chat), **Auto-pick best in this
chat**, **Auto-pick best in new chats** (`autoPickBestOption`), **Quick prompts…** (opens
`quickPrompts`), **"What did you change?" text…** (`changesQuestion`; empty = no button),
**Language** and **Global settings…** (the Queue view's gear menu). (2026-10-26) ADHD mode is also a
row in **Global settings** and the command **Toggle ADHD Mode** (Command Palette, Queue view **...** menu).

**Language:** `language` = `auto` (default: VS Code's display language when it is English, Russian or Finnish, else
English), `en`, `ru` (Русский) or `fi` (Suomi). Change it with Gear → Language, with
`Sleeping Clanker: Change UI Language…`, or with **Language** under **Other settings** in the New Task
panel (2026-10-26). All three save the same user setting. It is never an option of a task. The new
language applies at once to the Queue view, the status bar, the hovers, the New Task panel and open
Task views. The Settings page shows the choices by their own names. Command titles in menus and the
Settings page follow VS Code's own display language (`Configure Display Language`):
- **Russian:** VS Code has an official Russian language pack. If VS Code itself is not in Russian, one
  notice (only in the focused window) offers **Switch VS Code to Russian**. That opens VS Code's own display-language picker; VS Code
  installs the pack itself and asks to restart. **Don't ask again** stops the offer.
- **Finnish:** VS Code has no Finnish language pack. Menu command names and the Settings page stay in
  VS Code's language; a one-time notice says so.

Sleeping Clanker's own setting descriptions (except Language) and some notifications are still English.

`pythonPath`, `dataDir` (the base folder; each project gets its own folder inside it),
`defaultProfile`, `refreshSeconds`, `maxTurns` ((0.4.1) default 0 = the core's own caps, 100 turns
per task and 40 per goal round; a value above 0 is passed as `run --max-turns`), `itemTimeoutMinutes`, `maxLimitWaits`,
`autoStartRunner` (default on since 0.4.14; turn it off in the user settings; user settings only; starts the main queue plus every queue with
pending or waiting tasks when VS Code starts, and (0.3.3) the target queue's runner after you queue
a task or send a reply while it is not running; the **Always start automatically** button on the
"queued" message turns it on), `model`, `goalMaxRounds` (50), `goalMaxHours` (72) and (0.3.3)
`logPolling` (default off) and (0.4.0) `taskView` (`chat`, the default, or `text`: what a double-click
opens), all under `claudAutopilot.`.
(0.4.4) The Defaults for + rows write five more: `defaultKind` (`ask`, the default, or `task`,
`goal`, `plan`, `files`), `askProfile` (default on; off = every new task gets `defaultProfile`
without asking), `defaultLane` (empty = ask), `askGoalCaps` (default on; off = a new goal gets
`goalMaxRounds`/`goalMaxHours` without asking) and `showAddDefaults` (default on = show the rows).
(0.4.4) `autoPickBestOption` (default **off**; user settings only): what the **Auto-pick best** box of
a newly opened Task view starts with. A chat whose box you clicked keeps your choice over the
setting.
(2026-10-21) `askModel` (default **off**): + asks each new task's model (Runner default, Opus, Sonnet,
Haiku, Fable or any model id). **Change Model…** in a task's right-click menu (any status but running)
sets the model of its next attempt; a task's own model wins over `model` and shows in its hover.
(2026-10-21) `splitTasks` (default **on**): a New Task, Plan or Task-with-files prompt that lists
several tasks is queued as one task per item in the same queue (they run one after another). A list
is 2+ lines starting with `1.`/`1)` or `-`/`*` (bullets under other text only when that text says
"tasks"), or `1. … 2. …` on one line; each item needs 2+ words. The text around the list goes to
every part. Goals, selections and prompt files are never split. Off = one prompt, one task.
`changesQuestion`: every task prompt asks Claude for a short final message (the outcome in 1-3
lines, no file:line walkthrough). A done, failed or cancelled task's Task view shows a **What did you
change?** button. The button sends this setting as a reply (default: "What did you change? List each
file you modified as path:line, one line each on what and why."). Example:
`"claudAutopilot.changesQuestion": "List the changed files with their paths and the setting or option each change adds."`
Empty = no button.
`quickPrompts`: the quick-prompt buttons above a Task view's reply box, shown whenever the task can
take a reply (blocked, done, failed, cancelled or paused, with a session). A click sends that text as your
reply with the profile in the dropdown; **Shift+click** puts it in the reply box to edit first. Up to
8 texts, each up to 2000 characters (default: "Continue where you stopped.", "Run the tests and report
the pass and fail counts.", "Explain your last step in 1-3 lines."). Example:
`"claudAutopilot.quickPrompts": ["Continue where you stopped.", "Write a short summary for the changelog."]`
Empty list = no buttons.

The **Sleeping Clanker** output channel shows what matters: `>>` marks an event (task queued,
runner started / stopped / exited, `item 6f1b9503: pending -> running`), `!! ERROR` an error.
The background refresh calls (`lanes`, `where`, `projects`, ...) are logged only when they fail or
take over 5 s; turn on `claudAutopilot.logPolling` to log every one while debugging.

Runners started from a window are stopped gracefully when that window closes. Stop Runner asks the
runner to stop and, after 15 s, kills its process tree.

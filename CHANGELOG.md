# Changelog

Sleeping Clanker versions have four parts, MAJOR.MINOR.PATCH.BUILD:

- MAJOR: a breaking change (an incompatible queue format, setting or command). Resets the rest to 0.
- MINOR: a new feature or a significant enhancement. Resets PATCH and BUILD to 0.
- PATCH: bug fixes, security fixes and maintenance, nothing breaking. Resets BUILD to 0.
- BUILD: +1 for every new VSIX of the same MAJOR.MINOR.PATCH. A BUILD is never reused for a
  different VSIX.

The one source is `__version__` in `src/sleeping_clanker/__init__.py`, and only
`python vscode-extension/scripts/build.py --bump build|patch|minor|major` changes it.
VS Code accepts only three parts, so `vscode-extension/package.json` carries
MAJOR.MINOR.(PATCH × 1000 + BUILD), which `build.py` derives: 1.0.0.0 shows as 1.0.0 in the
Extensions view. Each build stamps its channel into the VSIX: `development` (built from uncommitted
changes), `testing` (built from a commit) or `production` (`build.py --release`: built from a
commit that has a dated entry here with a Verification line). `autopilot --version`, the `run` log,
`report` drafts and the extension's output log show the full version and that stamp.

Each entry has the version and its date (`unreleased` until it is released), what changed (new,
fixed, security, breaking) and a Verification line: the test counts and the review verdict of that
exact version. Versions before 1.0.0.0 (0.1.0 to 0.4.13) have no entries here.

## 1.0.1.0 - 2026-10-30

- Fixed: on a merge card, Try the merge again no longer says that the task goes back to the queue,
  and no longer starts the runner, when the conflict is still there. It now says that the task is
  still a merge conflict and that its merge card is back. Any other result of the retry is shown as
  it is, and the runner starts only when the merge has to wait for the project.
- Fixed: the tooltip of a conflict's Preview no longer calls the preview a read-only copy. It now
  says that it is a copy outside the project: edits to it are not used, and the project is not
  changed.
- Breaking: none.
- Verification: py_compile 108 files / 0 failed, node --check 102 / 0, pytest 3845 passed,
  node --test 800/800 (release, 2026-10-30). M5 (the VS Code UI of 1.0.0.0, independent tester):
  9 PASS, 1 FAIL (Try the merge again), fixed here; the fix's independent review: PASS.

## 1.0.0.0 - 2026-10-29

- Changed: the version scheme. 0.4.13 is followed by 1.0.0.0, with the four-part rules above.
- New: `autopilot --version`, the `run` log line and `report` drafts show the full version and, for
  a VSIX build, its channel, commit and build time. The extension's output log shows them when it
  starts.
- New: `build.py --bump PART` raises the version and updates package.json; `build.py --release`
  stamps a production build. The VSIX is named `sleeping-clanker-<MAJOR.MINOR.PATCH.BUILD>.vsix`.
- Breaking: none. VS Code sees 1.0.0, which is newer than 0.4.13, so the VSIX installs as an update.
- Changed: Renamed to Sleeping Clanker (was Sleeping Claud), with a new icon. The Python package is
  `sleeping_clanker` and the VSIX `sleeping-clanker-<version>.vsix`. The extension id, its settings
  and commands, the data folder and the environment variables keep their names, so an installed
  extension updates in place and keeps its queues and settings.

Tasks at once:

- New: several tasks of one queue can run at the same time. The setting Tasks At Once takes 1 to 5;
  the default 1 runs one task after another, as before. On the command line: `run --tasks-at-once N`,
  and `tasks-at-once N` for a runner that is already running.
- New: each of those tasks works in its own private copy of your project, made with the app's own
  private git store. Your project's .git (index, branches, stash, hooks) is never touched, nothing
  goes over the network, and folders that are not git repositories work too. Secret files such as
  .env are never copied; .venv and node_modules are shared with the project, not copied.
- New: a finished task's changes are merged back into your folder, byte for byte, with a backup of
  every file it replaces. If your folder changed in the same places, nothing is overwritten: the task
  asks you on a merge card with Let Claude fix it (recommended), Try the merge again, Take the task's
  version or Discard the task's changes, plus a preview of the conflict. Undo the merge puts
  the old files back while nothing changed them since. On the command line: `merge ID --let-claude`,
  `--retry`, `--take-task`, `--discard`, `--now` or `--undo`.
- New: New Task Start chooses whether a new task may start beside the running ones or only after the
  task added before it is finished and merged (`add --after ID`, `--after-previous`). Parts of a split
  task always run one after another.
- New: a task can run alone in the project folder instead (`--in-place`, or Runs in the New Task
  panel). Tasks run alone by themselves when a copy is not safe, for example without Git 2.40 (the
  Queue view then says why) or in a very large project.
- New: after a usage limit no new task starts until the reset; running tasks go on. After the reset
  one task starts first, and fewer start at once when the 5-hour window is nearly full.
- New: copies that no task uses any more are listed under Leftover copies in the Queue view (and by
  `worktrees`); they are never deleted by themselves.
- Fixed: a task whose folder is ignored by git, or left out of copies, now runs alone in the project
  folder instead of working in an empty folder of its copy, where its work was lost.
- Fixed: files a task wrote that a copy cannot merge back (for example .env.example, or bin/ in a
  project without a .gitignore) are no longer lost: the copy is kept aside and the task shows a note
  that names those files.
- Security: files in secret-named folders and links (junctions) anywhere in the project never enter
  the app's private git store; a project with such a link runs its tasks alone in the project folder.
- Security: a task in a copy never has fewer write limits than the same task in the project folder.

Queue view:

- New: drag a waiting task onto another one to run it before that one, or use Move to Top / Move to
  Bottom (on the command line: `move ID --top`, `--bottom`, `--before ID` or `--after ID`).
- New: Pause Queue and Resume Queue buttons next to Start/Stop Runner in the Queue view's title bar.
- New: a "Today" row on top counts this project's tasks done, pending, failed, running and waiting
  for you. Clicking it opens a statistics page for today and the last 7 days.
- New: a "Questions" group on top lists every task that waits for your answer.
- New: cancelled tasks have their own "Cancelled" group under Completed, with its own delete button.
- New: a running task shows a live line with what it is doing now.
- New: ADHD mode can be switched in the global settings and in the Queue view menu.
- Changed: the Queue view lists tasks in the order the runner will start them.
- Changed: the runner starts by itself when you add a task (on by default).

Task view and replies:

- New: you can write a reply while Claude is still working, as in the Claude chat.
- New: a ✎ pen in the reply box edits the task's text; a paused task's text can be edited too.
  Edit task… and Edit reply… now edit right in the reply box.
- Changed: clicking an option sends it at once; Shift+click or Alt+click puts it into the reply
  box so you can edit it first.
- Fixed: long option labels are no longer cut off in the middle of a word.
- New: the Working row shows the current step, the time so far and how much context the latest
  call used.
- New: a setting turns off the red colour of full-access tasks, and another one asks first before
  you reply to a full-access task after the queue was idle for a while (default 4 hours).
- New: pickers and input boxes have a close (X) button.

New task panel:

- New: Other settings holds every option for this one task: name, folder, attachments and, for a
  goal, the round and hour caps and when to start a fresh session. Chat settings moved there from
  the Task view.
- New: the pencil in the Queue view opens a task for editing in the New Task panel.
- New: two switches per task, also in Defaults for +. Autorun answers the task's questions with
  the best option by itself and shows what it picked. Ultracode lets Claude Code run workflows of
  many agents for the task (only with full access; it uses much more of your usage). On the
  command line: `--autorun on|off` and
  `--ultracode on|off` for `add` and `edit`.

Usage limits:

- New: a usage limit can ask you first instead of waiting by itself. With the setting Auto Continue
  After Limit off (the default) the task's queue is held and one notification asks: Continue after reset,
  Always, or Not now. The status bar counts down to the reset. On the command line:
  `run --on-limit ask` (the default stays `auto`).
- Changed: after a long limit wait a task goes on in a fresh session with a short summary of its
  work so far, built on your computer. That costs less than loading the whole old conversation.
- New: usage forecast. The status bar tooltip shows how full your 5-hour and 7-day usage windows
  are, when they reset, and how many queued tasks are likely to fit. Before you add, retry or reply
  near the limit, you can send anyway, queue the task for after the reset, or cancel (a setting
  turns this warning off).
- New: a queue can be held until a set time: `hold on --until TIME`; `hold limit-off` releases a
  usage-limit hold.

Language:

- New: choose the extension's language in its settings or in the New Task panel: Auto (VS Code's
  language), English, Russian or Finnish. Every button, message, setting description and Queue
  view text is translated; the logs stay in English.

Safety and security:

- Changed: running a chat code block by itself in full access is off by default. When you turn it
  on, each task asks once before its first such run and shows the whole command, and the command
  runs in a new visible terminal.
- Security: Claude cannot read or edit your saved logins (Claude, SSH, cloud, npm, git, Docker,
  GitHub CLI and Kubernetes credentials) or the run logs, in any permission mode, full access
  included.
- Security: claude.exe and git.exe are found only in full PATH folders, never in the current folder.
  Other API keys, tokens and passwords in your environment are no longer passed on to Claude's
  runs; only the chosen provider's key is.
- Security: a link in the Task view opens only after you confirm its full address.
- Security: more invisible characters are removed from code shown in the chat.
- Fixed: with Tasks at once, a task no longer fails before it starts when a folder or file name in its
  copy reads like a permission option; the note that tells Claude about its copy is now passed as
  text, and every option is still checked.
- Security: the public export and the VSIX check find more kinds of keys and personal paths (secrets
  given as command-line flags, named keys in code and JSON, home folders on Windows, Linux and macOS,
  private work folders), and a release build is refused when the owner's name cannot be checked.

Speed and reliability:

- Changed: the extension starts faster and its background checks start fewer processes.
- New: each run records how much of its input came from the prompt cache.
- Fixed: the runner no longer stops when the queue file is briefly locked by Windows or another
  program, and a finished task is never run again because its result could not be saved.
- Fixed: `show` and `list` no longer crash in a console that cannot show every character.
- Fixed: seven Task view and live-log errors when a log file was locked or changed while being read.
- Verification: py_compile 108 files / 0 failed, node --check 102 / 0, pytest 3845 passed,
  node --test 798/798 (integrator, 2026-10-29). Tasks at once acceptance: A1-A12 PASS, A13 M1-M3 PASS
  (M4 a follow-up, M5 with the release tester).

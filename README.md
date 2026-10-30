# Sleeping Clanker

![Sleeping Clanker](clanker.png)

Queue Claude Code tasks from VS Code and let them run while you sleep. When a subscription usage
limit is hit, Sleeping Clanker waits for the reset and resumes the same Claude session.

- One queue per project (or several named queues). Tasks in a queue run one at a time.
- Goals run several rounds until they are done or reach their limits.
- A task that needs your answer stops as **blocked**. You can reply to it later.
- A chat-style Task view shows each run: Claude's text, tool calls and permission denials.
- Permission profiles: `safe` (the default: read files, edit only inside the project, no shell),
  `edits`, `plan`, `shell-read`, `shell-dev` (your own allow-list of commands) and the opt-in `full`.
- Everything runs on your machine. The extension opens no network listener and sends no telemetry.

## Requirements

- Windows 10 or 11
- VS Code 1.90 or newer
- Python 3.11 or newer (`py -3` or `python` on PATH, or set `claudAutopilot.pythonPath`)
- [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude.exe`) installed and logged in

## Install

1. Download `sleeping-clanker-<version>.vsix` from the
   [latest release](https://github.com/Bloodycub/Sleeping-clanker/releases/latest).
2. In VS Code, open the Extensions view, click `…` at its top, choose **Install from VSIX…** and
   pick the file. Or, from a terminal: `code --install-extension sleeping-clanker-<version>.vsix`
3. Open the **Sleeping Clanker** view in the activity bar and press **+** to add a task.

The extension does not update itself yet. To update, install the newer VSIX the same way.

Versions have four parts, MAJOR.MINOR.PATCH.BUILD, starting at 1.0.0.0; [CHANGELOG.md](CHANGELOG.md)
says what each one changed. The VSIX file name, `python -m sleeping_clanker --version` and the
extension's output log show all four. VS Code accepts only three, so its Extensions view shows
MAJOR.MINOR.(PATCH × 1000 + BUILD): 1.0.0.0 as 1.0.0, and BUILD 7 of 1.0.2 as 1.0.2007.

Full guide: [vscode-extension/README.md](vscode-extension/README.md).

## Command line

The same core runs without VS Code:

```
pip install -e .
python -m sleeping_clanker add "Fix the failing test in tests/test_x.py" --cwd C:\code\repo
python -m sleeping_clanker list
python -m sleeping_clanker run          # until the queue is empty
python -m sleeping_clanker status
python -m sleeping_clanker --help
```

## Build from source

```
python vscode-extension\scripts\build.py
```

This copies the Python core into the extension and writes `dist\sleeping-clanker-<version>.vsix`.

## Reporting a problem

Run **Sleeping Clanker: Report an Issue on GitHub…** (or `python -m sleeping_clanker report`). It
writes a draft with keys, paths, user names and e-mail addresses taken out, and gives you a
prefilled GitHub issue page (the extension opens it; the CLI prints the link, `--open` opens it).
Nothing is posted until you read the draft and click **Submit new issue** yourself. Issues on this
repo are public.

## Credits and license

Inspired by [benbasha/Claude-Autopilot](https://github.com/benbasha/Claude-Autopilot) (MIT). This
is a separate implementation that drives `claude -p` with `--resume` instead of a terminal.

MIT. See [LICENSE](LICENSE).

Sleeping Clanker is not made by or affiliated with Anthropic.

// @ts-check
// Sleeping Clanker for VS Code: thin native UI over the bundled Python core (2026-10-13,
// docs/SPEC_VSCODE.md). TreeView + status bar + QuickPick + OutputChannel only: no webview, no
// network listener. All logic that does not need the vscode API lives in ./lib (tested by node:test).
// (2026-10-13, v0.2) Goals, the Add menu, pause/resume and the goal bar (docs/SPEC_GOALS.md 5); one
// queue per project, every CLI call carries `--project=<root>`, plus the "Show all projects" toggle
// (docs/SPEC_PROJECTS.md 3, 3.1); hovers that stay open and explain (docs/SPEC_HOVER.md).
// (2026-10-15, v0.3, docs/SPEC_V03.md) The live task log (a read-only virtual document beside the
// editor, opened by a double-click), named queues ("lanes", each with its own runner), the
// `blocked` status and Reply. Still no webview and no network listener.
// (2026-10-17, 0.4.0, docs/SPEC_V04_CHAT.md) ONE locked-down webview, the Task view (lib/taskview.js):
// a chat-style view of a task (markdown answers, tool rows, a composer). No remote content, only its
// own nonce'd script, no command URIs; the text log stays as Open Raw Log. Still no network listener.
// (2026-10-20, 0.4.5, docs/SPEC_V045_DEFAULTS.md) "Defaults for +": rows at the BOTTOM of the Queue
// view that preselect what + adds (task/goal/plan/files), the permission profile, the queue and the
// goal caps. A click on a row opens that row's picker; the choice is saved in the user settings, so
// every later + uses it without asking.
'use strict';

const vscode = require('vscode');
const fs = require('fs');
const path = require('path');
const argv = require('./lib/argv');
const parse = require('./lib/parse');
const format = require('./lib/format');
const view = require('./lib/view');
const proc = require('./lib/proc');
const { ClickTracker } = require('./lib/click');
// (2026-10-26) showQuickPick/showInputBox with a close button (X) in the title bar, lib/pickers.js
const pickers = require('./lib/pickers').forVscode(vscode);
const { ItemLog } = require('./lib/livelog');
const { ActivityCache } = require('./lib/activity');
const { TaskViews, VIEW_TYPE: TASK_VIEW_TYPE } = require('./lib/taskview');
const { Poller, pollIntervalMs, statusChanges, warningChanges } = require('./lib/poller');
const i18n = require('./lib/i18n');
const split = require('./lib/split');
const addpanel = require('./lib/addpanel'); // (2026-10-21) the "+ Add new task" panel, docs/SPEC_V049_ADD_PANEL.md
const stats = require('./lib/stats'); // (2026-10-26, owner task f60cec52) the Today counter and the statistics
const fullaccess = require('./lib/fullaccess'); // (2026-10-26, owner fa388b7b) the idle FULL ACCESS reminder
const move = require('./lib/move'); // (2026-10-26, owner task 07254297) drag a not-done task to a new place
const limithold = require('./lib/limithold'); // (2026-10-26, limit_continue) the usage-limit hold's question
const usage = require('./lib/usage'); // (2026-10-26) the usage-limit forecast and the warning before sending
const { t } = i18n; // (2026-10-21) the UI language; the English text is the key

const SECTION = 'claudAutopilot';
const VIEW_ID = 'claudAutopilot.queue';
const CONTAINER_COMMAND = 'workbench.view.extension.claudAutopilot';
const ALL_PROJECTS_KEY = 'claudAutopilot.allProjects';
const LAST_LANE_KEY = 'claudAutopilot.lastLane';
const PARALLEL_WARNING_OFF_KEY = 'claudAutopilot.parallelWarningOff';
// (2026-10-26, owner "Valinta + VS Code-kieli") globalState: "Don't ask again" for the Russian VS Code
// offer, and the one-time Finnish note (VS Code has no Finnish language pack)
const RU_LOCALE_OFFER_OFF_KEY = 'claudAutopilot.ruLocaleOfferOff';
const FI_LOCALE_NOTE_KEY = 'claudAutopilot.fiLocaleNoteShown';
const LOG_SCHEME = 'claud-autopilot-log';
const FULL_SCHEME = 'claud-autopilot-full'; // (2026-10-21, security review M5) the red FULL ACCESS badge on tree rows
// (2026-10-26, owner task f60cec52) the read-only statistics document the Today counter opens
const STATS_SCHEME = 'claud-autopilot-stats';
const STATS_PATH = '/Sleeping Clanker statistics.md';
const SETTINGS_QUERY = '@ext:bloodycub.claud-autopilot'; // (2026-10-21) the Settings editor, this extension only
// (2026-10-20, 0.4.4 logfix D/A) action labels
const RELOAD_WINDOW = 'Reload Window';
const EDIT_ALLOWED = 'Edit allowed commands…';
// (2026-10-21, SPEC_V048_REPORT; tester round 2 L-3) the report draft's NON-modal actions: the draft
// stays editable in its editor while the notification waits; dismissing it = Cancel
const REPORT_SEND = 'Send';
const REPORT_CANCEL = 'Cancel';
// (tester round 2 M-4) the split question's buttons; dismissed = Keep as one
const SPLIT_YES = 'Split';
const SPLIT_NO = 'Keep as one';
const RUN_FULL = 'Run with full access'; // (2026-10-21, security review B1) the only button that confirms full
// (2026-10-27, SEC-L22) both FULL ACCESS modals name the two risks: credentials and prompt injection
const FULL_RISKS = 'It can read every key, token and login this PC has (files, environment variables, signed-in tools) and send them anywhere. Text it reads (a web page, a file, an issue, a command\'s output) can steer it: prompt injection.';
// (2026-10-21, v049 tester fix LOW-4) confirmFull's question per English `what` ({0} = the task id); t() keys
/** @type {Readonly<Record<string, string>>} */
const FULL_QUESTIONS = Object.freeze({
  'this task': 'Sleeping Clanker: FULL ACCESS for this task?',
  'every new task (the default for +)': 'Sleeping Clanker: FULL ACCESS for every new task (the default for +)?',
  'the retry of {0}': 'Sleeping Clanker: FULL ACCESS for the retry of {0}?',
  'the reply to {0}': 'Sleeping Clanker: FULL ACCESS for the reply to {0}?',
});
const LOG_LANGUAGE = 'autopilot-log';
const LIVE_POLL_MS = 1000;
const LOG_CLOSE_GRACE_MS = 3000; // setTextDocumentLanguage closes and reopens the document
const STOP_TIMEOUT_MS = 15000;
const DEACTIVATE_TIMEOUT_MS = 5000;
const DELETE_RUNNING_WAIT_MS = 10000; // amendment M13: cancel, wait until it is no longer running
const GOAL_TICK_MS = 1000;
const PARALLEL_WARNING = 'Two queues edit the same folder at once; avoid giving them overlapping files.';
// (2026-10-17, 0.3.3) the buttons of the "queued" toast; QUIET marks the poller's read-only calls
const START_RUNNER = 'Start Runner';
const ALWAYS_START = 'Always start automatically';
const QUIET = Object.freeze({ quiet: true });
// ---- (2026-10-27, U4, SPEC_V05_PARALLEL 8.4-8.13) tasks at once ------------------------------------------
const PARALLEL_INTRO_KEY = 'claudAutopilot.parallelIntroShown'; // globalState: the one-time intro was shown
const PARALLEL_INTRO = 'Sleeping Clanker: up to {0} tasks of a queue now run at once. Each runs in its own private copy of the project and is merged back when it finishes. When a merge conflicts, the task asks you; "Let Claude fix it" is the recommended answer. Files like .env and .venv are not copied into the copies.';
/** Merge answers that replace files or throw work away: a modal asks first (never an automatic pick). */
const MERGE_CONFIRMS = Object.freeze({
  'take-task': "Sleeping Clanker: use the task's version of the conflicted files ({0})?",
  discard: "Sleeping Clanker: discard the task's changes ({0})?",
  undo: 'Sleeping Clanker: undo the merge of {0}?',
});
const MERGE_CONFIRM_DETAILS = Object.freeze({
  'take-task': "The project's newer changes in those files are replaced by the task's. A backup is kept; Undo the merge restores it.",
  discard: "Its private copy is removed (its changes are saved as a patch file first) and the task is cancelled.",
  undo: 'Every file the merge changed goes back to how it was before. It is refused when one of them changed since.',
});
/** `merge --json` refusals (exit 1) in words; an unknown code shows the core's own text. */
const MERGE_REFUSALS = Object.freeze({
  'gen-mismatch': 'the question changed meanwhile; look at the card again',
  'not-allowed': 'that answer is not offered for this task now',
  'no-item': 'the task is not in the queue any more',
  'no-copy': 'the task has no private copy any more',
  busy: 'the project is busy with another merge; try again in a moment',
  stopped: 'the runner is stopping; try again once it has stopped',
  'changed-since': 'files changed since the merge, so it cannot be undone',
  'no-patch': "the task's changes could not be saved as a patch, so nothing was discarded",
});
// (2026-10-29, F4) a typed reply to a merge question becomes a merge answer (cli.py _reply_merge): what it did, in words
const MERGE_REPLY_DONE = 'Sleeping Clanker: your reply answered the merge question of {0}: {1}. {2}';
const MERGE_REPLY_BACK = 'It goes back to the queue to fix the conflict.';
const MERGE_REPLY_REFUSED = 'your reply became the merge answer "{0}", which was refused: {1}';
/** A retry's merge result (worktree.Shadow.merge, or "pending" when the project lock was busy) by code. */
const MERGE_REPLY_RESULTS = Object.freeze({
  merged: 'The changes were merged into the project.',
  nothing: 'There was nothing to merge.',
  conflict: 'It still conflicts; the merge card is back.',
  waiting: 'The merge waits; the runner tries it again.',
  pending: 'The project was busy; the runner merges it later.',
  held: "The task's changes are held in its copy.",
  stale: 'The question had changed meanwhile; nothing was merged.',
});
const DIRTY_MERGED = 'Sleeping Clanker: a task changed {0} on disk while it has unsaved edits here; VS Code will ask on save.';
const LEFTOVERS_POLL_MS = 60 * 1000; // `worktrees --json` at most once a minute, and only while copies are in use
// (2026-10-21, v049 dontask_hold) the one button that starts a held queue's runner (modal)
const START_ANYWAY = 'Start anyway';
/** (2026-10-27, limit_continue fix round 2) the usage-limit holds already answered (workspaceState) */
const LIMIT_ASKED_KEY = 'claudAutopilot.limitAsked';
// (2026-10-26, usage forecast) the near-the-limit warning's buttons (Cancel is the modal's own)
const SEND_ANYWAY = 'Send anyway';
const AFTER_RESET = 'Queue for after the reset';

/**
 * @typedef {{command: string, args: string[]}} Python
 * @typedef {{child: import('child_process').ChildProcess, exited: Promise<void>, stopRequested: boolean,
 *   project: string | null, lane: string | null, dataDir: string, name: string, lockHeld?: boolean,
 *   startedAt?: number, onLimit?: string, inPlaceSeen?: boolean}} RunnerRecord
 *   (2026-10-15) keyed by the data dir the core resolved for (project, lane): one runner per queue
 */

/** @type {Autopilot | null} */
let instance = null;

/**
 * True if `exited` settles within `ms`; the timer is always cleared.
 * @param {Promise<void>} exited @param {number} ms
 * @returns {Promise<boolean>}
 */
function exitedWithin(exited, ms) {
  /** @type {any} */
  let timer;
  const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve(false), ms); });
  return Promise.race([exited.then(() => true), timeout]).finally(() => clearTimeout(timer));
}

/** @param {number} ms */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// (2026-10-20, 0.4.4 logfix I) how long a failed taskkill waits for the runner's exit before it is an error
const TASKKILL_EXIT_GRACE_MS = 5000;

/** (2026-10-15) The `--lane` value for a queue: null for `default`. @param {string | null | undefined} lane */
const laneArg = (lane) => (lane && lane.toLowerCase() !== argv.DEFAULT_LANE ? lane : null);

/**
 * Assign obj[key] only when the value differs: re-assigning a status bar tooltip closes an open
 * hover (SPEC_HOVER 1).
 * @param {any} obj @param {string} key @param {any} value
 */
function setIfChanged(obj, key, value) {
  if (obj[key] !== value) obj[key] = value;
}

class QueueProvider {
  constructor() {
    /** this window's project items (kept for commands and tests) @type {import('./lib/parse').Item[]} */
    this.items = [];
    /** @type {import('./lib/view').Node[]} */
    this.roots = [];
    /** (2026-10-20, 0.4.5) nodes BELOW the queue: the "Defaults for +" section @type {import('./lib/view').Node[]} */
    this.footer = [];
    /** (2026-10-26) nodes ABOVE the queue: the Today counter @type {import('./lib/view').Node[]} */
    this.header = [];
    this._fingerprint = '';
    this.fires = 0;
    /** (2026-10-21, scroll) node id -> the owner's own expand/collapse; it beats the default
     * @type {Map<string, boolean>} */
    this.open = new Map();
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
  }

  /**
   * (2026-10-21, scroll) The owner opened or closed a node. The tree re-renders every few seconds,
   * and `getTreeItem` hands VS Code a collapsible state every time; without this the state was
   * always the default, so a group the owner opened snapped shut under the cursor (and one they
   * closed re-opened) within seconds, and a long queue could not be scrolled or read.
   * @param {import('./lib/view').Node} node @param {boolean} open
   */
  setOpen(node, open) {
    if (node && node.render && node.render.id) this.open.set(node.render.id, open);
  }

  /**
   * (2026-10-13, SPEC_HOVER 1) Fire only when what is rendered changed; an unchanged poll must not
   * re-render the tree, or VS Code closes the hover the owner is reading.
   * @param {import('./lib/parse').Item[]} items @param {import('./lib/view').Node[]} roots
   * @param {import('./lib/view').Node[]} [footer] (0.4.5) the "Defaults for +" section, below the queue
   * @param {import('./lib/view').Node[]} [header] (2026-10-26) the Today counter, above the queue
   */
  setNodes(items, roots, footer = [], header = []) {
    this.items = items;
    this.roots = roots;
    this.footer = footer;
    this.header = header;
    this._pruneOpen([...header, ...roots, ...footer]);
    const fp = view.fingerprint([...header, ...roots, ...footer]);
    if (fp === this._fingerprint) return false;
    this._fingerprint = fp;
    this.fires += 1;
    this._emitter.fire(undefined);
    return true;
  }

  /**
   * (2026-10-21, tester round 2 L-6) Forget the open/closed state of nodes that are gone (a removed
   * item, an emptied group), so the map holds only ids the tree still shows.
   * @param {import('./lib/view').Node[]} nodes
   */
  _pruneOpen(nodes) {
    if (!this.open.size) return;
    /** @type {Set<string>} */
    const ids = new Set();
    const walk = (/** @type {any[]} */ list) => {
      for (const n of list || []) {
        if (n && n.render && n.render.id) ids.add(n.render.id);
        if (n && n.kind !== 'setting' && n.children) walk(n.children);
      }
    };
    walk(nodes);
    for (const id of [...this.open.keys()]) if (!ids.has(id)) this.open.delete(id);
  }

  /** @param {import('./lib/view').Node} node */
  getTreeItem(node) {
    const r = node.render;
    const C = vscode.TreeItemCollapsibleState;
    const leaf = view.isLeaf(node); // (2026-10-21) a main task with subtasks expands
    const own = this.open.get(r.id); // (2026-10-21, scroll) the owner's own toggle, else the default
    const state = leaf ? C.None : ((own === undefined ? r.expanded : own) ? C.Expanded : C.Collapsed);
    const ti = new vscode.TreeItem(r.label, state);
    ti.id = r.id;
    ti.description = r.description;
    ti.tooltip = r.tooltip;
    ti.iconPath = new vscode.ThemeIcon(r.icon.id, r.icon.color ? new vscode.ThemeColor(r.icon.color) : undefined);
    ti.contextValue = r.contextValue;
    // (2026-10-21, security review M5) a full task's row: the FileDecoration of FULL_SCHEME paints it red
    if (node.kind === 'item' && node.item && node.item.profile === argv.FULL_PROFILE && vscode.Uri && vscode.Uri.from) {
      ti.resourceUri = vscode.Uri.from({ scheme: FULL_SCHEME, path: `/${node.item.id}` });
    }
    // (2026-10-15, SPEC_V03 1.3) a single click only selects; two clicks within 500 ms open the live
    // log (Show Details stays in the context menu and the goal bar).
    if (node.kind === 'item') ti.command = { command: 'claudAutopilot.itemClick', title: 'Open Task View', arguments: [node] };
    // (2026-10-20, 0.4.5) a defaults row IS its dropdown: one click opens that row's picker
    else if (node.kind === 'setting') {
      ti.command = { command: 'claudAutopilot.setAddDefault', title: 'Change', arguments: [node.key] };
    } else if (node.kind === 'hint') {
      ti.command = { command: 'claudAutopilot.add', title: 'Add…', arguments: [] }; // (2026-10-21) the empty-queue row
    } else if (node.kind === 'counter') {
      ti.command = { command: 'claudAutopilot.showStats', title: 'Statistics', arguments: [] }; // (2026-10-26)
    } else if (node.kind === 'leftover') {
      // (2026-10-27, U4, 8.10) a leftover copy: the item click (two clicks = Open folder); no new command
      ti.command = { command: 'claudAutopilot.itemClick', title: t('Open folder'), arguments: [node] };
    }
    return ti;
  }

  /** @param {import('./lib/view').Node} [node] */
  getChildren(node) {
    if (!node) return this.header.concat(this.roots, this.footer);
    return node.kind === 'setting' ? [] : node.children || [];
  }

  dispose() {
    this._emitter.dispose();
  }
}

/**
 * @typedef {{key: string, uri: any, q: {root: string | null, lane: string, id: string}, log: ItemLog,
 *   item: import('./lib/parse').Item | null, text: string, watcher: any, timer: any, follow: any[], gone?: boolean}} LogState
 */

/**
 * (2026-10-15, SPEC_V03 1.3) The live task log: a read-only `claud-autopilot-log:` document per item.
 * While the item runs, a FileSystemWatcher plus a 1 s poll read only the NEW bytes of its run files;
 * the item's status comes from the regular queue poll. Closing the document drops its state.
 */
class LiveLogProvider {
  /** @param {Autopilot} ap */
  constructor(ap) {
    this.ap = ap;
    this._emitter = new vscode.EventEmitter();
    this.onDidChange = this._emitter.event;
    /** @type {Map<string, LogState>} */
    this.docs = new Map();
    /** @type {Map<string, Promise<LogState>>} */
    this._opening = new Map();
  }

  /** @param {any} uri */
  async provideTextDocumentContent(uri) {
    try {
      const st = await this._state(uri);
      return st.text;
    } catch (err) {
      this.ap.log(`${proc.stamp()} ERROR live log ${uri.toString()}: ${err instanceof Error ? err.message : err}`);
      return `${t('The log could not be read: {0}', err instanceof Error ? err.message : String(err))}\n`;
    }
  }

  /** @param {any} uri @returns {Promise<LogState>} */
  _state(uri) {
    const key = uri.toString();
    const have = this.docs.get(key);
    if (have) return Promise.resolve(have);
    let p = this._opening.get(key);
    if (!p) {
      p = this._open(uri, key).finally(() => this._opening.delete(key));
      this._opening.set(key, p);
    }
    return p;
  }

  /** @param {any} uri @param {string} key @returns {Promise<LogState>} */
  async _open(uri, key) {
    const q = argv.parseLiveLogQuery(uri.query);
    // The run folder comes from the data dir the CORE resolves for (project, lane) (amendment M9).
    // (2026-10-26, speed) the one the last poll's `lanes --json` gave, else `where --json`
    const dataDir = this.ap.knownDataDir(q.root, q.lane)
      || (await this.ap.cliJson(argv.whereArgs(), parse.parseWhere, undefined, q.root, q.lane)).dataDir;
    const runDir = argv.runLogDir(dataDir, q.id);
    /** @type {LogState} */
    const st = { key, uri, q, log: new ItemLog(runDir), item: null, text: '', watcher: null, timer: null, follow: [] };
    st.item = await this._item(st);
    st.log.refresh({ final: !this._running(st) });
    st.text = st.log.render(st.item ? st.item.replies : []);
    this.docs.set(key, st);
    this._sync(st);
    return st;
  }

  /** @param {LogState} st */
  _running(st) {
    return !!st.item && st.item.status === 'running';
  }

  /**
   * The item from the last queue poll, else `show --json` (another project's item).
   * (2026-10-17, 0.3.3) `quiet` from onPoll: a poller-driven read.
   * @param {LogState} st @param {boolean} [quiet]
   */
  async _item(st, quiet = false) {
    const known = this.ap.findItem(st.q.root, st.q.lane, st.q.id);
    if (known) return known;
    if (st.gone) return null; // (0.4.1 D6) the core said it does not exist: not asked again
    try {
      return await this.ap.cliJson(argv.itemArgs('show', st.q.id), parse.parseShow, undefined, st.q.root, st.q.lane, { quiet });
    } catch (err) {
      if (parse.isItemNotFound(err)) {
        st.gone = true;
        this.ap.log(`${proc.stamp()} live log: item ${st.q.id} is not in the queue any more; this log stops asking for it`);
      } else {
        this.ap.log(`${proc.stamp()} WARN live log: item ${st.q.id}: ${err instanceof Error ? err.message : err}`);
      }
      return null;
    }
  }

  /** After every queue poll: new status, new replies; start or stop watching. */
  async onPoll() {
    for (const st of [...this.docs.values()]) {
      const fresh = await this._item(st, true);
      // (2026-10-26) closed meanwhile, or closed and opened again as a NEW state: this one is dropped
      // (a `has` check let it start a timer nobody ever cleared)
      if (this.docs.get(st.key) !== st) continue;
      // (2026-10-26) a failed `show` is no news: keep the last item (taking null stopped a running
      // item's watch and flushed its half-written last line as final, as in TaskViews.onPoll)
      if (!fresh && !st.gone) continue;
      st.item = fresh;
      const wasWatching = !!st.timer;
      this._sync(st);
      st.log.refresh({ final: wasWatching && !st.timer }); // the final refresh when it stopped running
      this._update(st);
    }
  }

  /** Watch the run folder while the item runs; stop when it stops. @param {LogState} st */
  _sync(st) {
    const running = this._running(st);
    if (running && !st.timer) {
      const tick = () => {
        if (st.log.refresh()) this._update(st);
      };
      st.timer = setInterval(tick, LIVE_POLL_MS);
      if (st.timer && typeof st.timer.unref === 'function') st.timer.unref();
      try {
        // The extension never creates the run folder; until it exists the 1 s poll covers it.
        if (vscode.workspace.createFileSystemWatcher && vscode.RelativePattern && fs.existsSync(st.log.runDir)) {
          const w = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(st.log.runDir), '*'));
          w.onDidChange(tick);
          w.onDidCreate(tick);
          st.watcher = w;
        }
      } catch (err) {
        this.ap.log(`${proc.stamp()} WARN live log watcher: ${err instanceof Error ? err.message : err} (the 1 s poll continues)`);
      }
    } else if (!running && st.timer) {
      this._unwatch(st);
    }
  }

  /** @param {LogState} st */
  _unwatch(st) {
    if (st.timer) clearInterval(st.timer);
    st.timer = null;
    if (st.watcher) st.watcher.dispose();
    st.watcher = null;
  }

  /** Re-render; fire only when the text changed. Editors whose cursor is on the last line follow. @param {LogState} st */
  _update(st) {
    const text = st.log.render(st.item ? st.item.replies : []);
    if (text === st.text) return;
    st.text = text;
    st.follow = (vscode.window.visibleTextEditors || []).filter((e) => e.document.uri.toString() === st.key
      && e.selection && e.selection.active.line >= e.document.lineCount - 1);
    this._emitter.fire(st.uri);
  }

  /** workspace.onDidChangeTextDocument: move following editors to the new end. @param {any} doc */
  onDocumentChanged(doc) {
    const st = this.docs.get(doc.uri.toString());
    if (!st || !st.follow.length) return;
    const end = doc.lineAt(doc.lineCount - 1).range.end;
    for (const e of st.follow) {
      e.selection = new vscode.Selection(end, end);
      e.revealRange(new vscode.Range(end, end));
    }
    st.follow = [];
  }

  /** workspace.onDidCloseTextDocument: drop the state unless the document comes back. @param {any} doc */
  onDocumentClosed(doc) {
    if (!doc.uri || doc.uri.scheme !== LOG_SCHEME) return;
    const key = doc.uri.toString();
    const timer = setTimeout(() => {
      const open = (vscode.workspace.textDocuments || []).some((d) => d.uri.toString() === key);
      const st = this.docs.get(key);
      if (!open && st) {
        this._unwatch(st);
        this.docs.delete(key);
      }
    }, LOG_CLOSE_GRACE_MS);
    if (timer && typeof timer.unref === 'function') timer.unref();
  }

  dispose() {
    for (const st of this.docs.values()) this._unwatch(st);
    this.docs.clear();
    this._emitter.dispose();
  }
}

class Autopilot {
  /** @param {vscode.ExtensionContext} context */
  constructor(context) {
    this.context = context;
    this.out = vscode.window.createOutputChannel('Sleeping Clanker');
    this.pythonDir = path.join(context.extensionPath, 'python');
    // (2026-10-26, speed) the core's bytecode cache for the short CLI calls (argv.childEnv): VS Code's
    // per-user storage of this extension, never the installed extension; null = no cache (as before).
    // The runner keeps the old env: its claude children (and their tools) inherit it unchanged.
    const gs = context.globalStorageUri && context.globalStorageUri.fsPath;
    this.pycacheDir = gs ? path.join(gs, 'pycache') : null;
    /** @type {import('./lib/argv').Settings} */
    this.settings = this._readSettings();
    this._applyLanguage(); // (2026-10-21) before the first text is rendered
    /** @type {Promise<Python> | null} */
    this._python = null;
    /** @type {import('./lib/parse').Status | null} */
    this.status = null;
    /** @type {string | null} */
    this.lastError = null;
    /** one record per queue, keyed by the core's data dir (amendment M12) @type {Map<string, RunnerRecord>} */
    this.ownRunners = new Map();
    /**
     * (2026-10-21, subagents, SPEC_V049_AGENTS) queues that asked for a runner while this window was
     * at `claudAutopilot.maxParallelRunners`; the first one starts as soon as a runner exits.
     * @type {{project: string | null, lane: string | null, label: string, ignoreHold?: boolean, heldSince?: string | null}[]}
     */
    this.waitingRunners = [];
    this._shuttingDown = false;   // (2026-10-21, subagents) set by shutdown(): no runner starts after it
    this._settingsWrites = 0;     // (2026-10-26) _updateSettings in progress: _readSettings keeps the full marker
    /** (2026-10-15) this window's project, per queue (`default` first) @type {import('./lib/parse').Lane[]} */
    this.lanes = [];
    /** @type {import('./lib/parse').Project[] | null} */
    this.rows = null;
    this.clicks = new ClickTracker();
    this.liveLog = new LiveLogProvider(this);
    /** (2026-10-17, 0.4.0) the Task view panels */
    this.taskViews = new TaskViews(vscode, this, context.extensionPath);
    /** (2026-10-21, add panel) `providers --json` (names, key present, models; never a key) @type {Promise<import('./lib/parse').Providers> | null} */
    this._providers = null;
    /** (2026-10-21, add panel) the "+ Add new task" webview view under the Queue view */
    this.addPanel = new addpanel.AddPanel(vscode, context.extensionPath, {
      settings: () => this.settings,
      lanes: () => (this.lanes.length ? this.lanes.map((l) => l.name) : []),
      providers: (reload) => this.providerList(reload),
      update: (patch) => this._updateDefaults(patch), // full asks its confirmation there
      add: () => this.cmdAdd(),
      log: (line) => this.log(`${proc.stamp()} ${line}`),
      guarded: (what, fn) => this.guarded(what, fn),
      chatSettings: () => this.taskViews._chatSettings(null), // (2026-10-26) moved here from the Task view
      saveEdit: (target, text) => this.saveEditText(target, text), // (2026-10-26, edit in panel)
      reportError: (what, err) => this.reportError(what, err),
      language: (id) => this.setUiLanguage(id), // (2026-10-26) the gear's changeLanguage path: a GLOBAL setting
    });
    /** untitled reply drafts -> their target (SPEC_V03 5.2) @type {Map<string, {item: import('./lib/parse').Item, project: string | null, lane: string, dataDir: string | null}>} */
    this.replyDrafts = new Map();
    /** (2026-10-21, edit) untitled edit drafts -> their target @type {Map<string, {item: import('./lib/parse').Item, project: string | null, lane: string, laneArg: string | null}>} */
    this.editDrafts = new Map();
    this.codeLensEmitter = new vscode.EventEmitter();
    /** projects whose parallel-queue warning was shown this session @type {Set<string>} */
    this._parallelWarned = new Set();
    /** @type {string | null} */
    this.projectRoot = null;
    /** @type {{dataDir: string, projectRoot: string | null} | null} */
    this.where = null;
    const ws = context.workspaceState;
    this.allProjects = !!(ws && ws.get(ALL_PROJECTS_KEY, false));
    this._migrationChecked = false;
    /** (2026-10-17, 0.3.3) item statuses of the last poll, for `item X: a -> b` @type {import('./lib/poller').StatusMap | null} */
    this._statuses = null;
    /** (2026-10-17, 0.3.3) the lane warnings of the last poll: logged only on change @type {Set<string> | null} */
    this._laneWarnings = null;
    /** (2026-10-21) the attempts whose "cache runs out" warning was shown: lane#id#last_ended_at @type {Set<string>} */
    this._cacheWarned = new Set();
    this.provider = new QueueProvider();
    // (2026-10-26, owner task f60cec52) the statistics document: read-only, from the last poll's items
    this._statsEmitter = new vscode.EventEmitter();
    this._statsText = '';
    this.statsDoc = { onDidChange: this._statsEmitter.event, provideTextDocumentContent: () => this.statsText() };
    // (2026-10-26, owner task 07254297) drag a not-done task onto another task: it goes before that one
    this.dnd = new move.QueueDragAndDrop(vscode, VIEW_ID, {
      move: (node, where) => this.guarded('move task', () => this.moveItem(node, where)),
      info: (msg) => { vscode.window.showInformationMessage(`Sleeping Clanker: ${msg}`); },
    });
    this.view = vscode.window.createTreeView(VIEW_ID, { treeDataProvider: this.provider, showCollapseAll: false,
      dragAndDropController: this.dnd });
    this.statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.statusItem.command = 'claudAutopilot.statusMenu';
    this.statusItem.text = format.statusBarText(null);
    this.statusItem.tooltip = format.statusBarTooltip(null);
    this.statusItem.show();
    this.goalItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 49);
    this.goalItem.command = 'claudAutopilot.goalMenu';
    // (2026-10-21, owner) the coloured count segments between the main item and the goal bar
    /** @type {Map<string, any>} key -> status bar item */
    this.countItems = new Map();
    format.STATUS_SEGMENTS.forEach((s, i) => {
      const it = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 49.9 - i / 10);
      it.command = CONTAINER_COMMAND;
      it.color = new vscode.ThemeColor(s.color);
      this.countItems.set(s.key, it);
    });
    /** @type {import('./lib/parse').Item | null} */
    this.goal = null;
    /** @type {any} */
    this._goalTimer = null;
    this._goalVisible = false;
    this.resolveProject();
    this.poller = new Poller(() => this.refreshNow(), () => pollIntervalMs({
      viewVisible: this.view.visible,
      runnerAlive: !!(this.status && this.status.runnerAlive) || this.ownRunners.size > 0,
      refreshSeconds: this.settings.refreshSeconds,
    }), { onError: (err) => this.reportError('refresh failed', err, { quiet: true }) });
  }

  /** The runner this window started for ITS OWN project (0.1 API, kept for tests). */
  get ownRunner() {
    const dir = this.status ? this.status.dataDir : this.where ? this.where.dataDir : null;
    return dir ? this.ownRunners.get(dir) || null : null;
  }

  // ---- logging and errors ----------------------------------------------------------------------
  /** @param {string} line */
  log(line) {
    // (2026-10-20, 0.4.4 logfix G) after dispose (or once VS Code closed the channel) a late line from a
    // child's exit handler threw "Channel has been closed"; it is dropped instead
    if (this._disposed) return;
    try {
      this.out.appendLine(line);
    } catch (_closed) {
      this._disposed = true;
    }
  }

  /**
   * (2026-10-17, 0.3.3) A meaningful event (queued, runner started/stopped/exited, an item status
   * change), marked `>>` so it stands out from the cli lines. @param {string} text
   */
  event(text) {
    this.log(`${proc.stamp()} >> ${text}`);
  }

  /**
   * Log every error; show it unless `quiet` and it repeats the last shown poll error.
   * @param {string} what @param {unknown} err @param {{quiet?: boolean}} [opts]
   */
  reportError(what, err, opts = {}) {
    const msg = `${what}: ${err instanceof Error ? err.message : String(err)}`;
    this.log(`${proc.stamp()} !! ERROR ${msg}`);
    if (opts.quiet && msg === this.lastError) return;
    this.lastError = msg;
    // (2026-10-27, i18n fix review) proc's no-Python error is in the UI language: match its text in every
    // language (the part before the reasons), not only the English one
    const NO_PYTHON = 'no Python >= 3.11 found ({0})'; // lib/proc.js findPython's error key
    const isPython = /pythonPath/.test(msg) || ['en', ...i18n.LANGUAGES].some((lang) => {
      const d = /** @type {Record<string, string>} */ (i18n.DICT[lang] || {});
      const head = String(d[NO_PYTHON] || NO_PYTHON).split('{0}')[0].replace(/[\s(]+$/, '');
      return !!head && msg.includes(head);
    });
    // (2026-10-27, i18n) the toast speaks the UI language: `what` is a key (the log line above stays
    // English), the buttons are translated once and the shown text is the one compared
    const openSettings = t('Open Settings');
    const showOutput = t('Show Output');
    const buttons = isPython ? [openSettings, showOutput] : [showOutput];
    vscode.window.showErrorMessage(`Sleeping Clanker: ${t(what)}: ${err instanceof Error ? err.message : String(err)}`, ...buttons).then((pick) => {
      if (pick === openSettings) {
        vscode.commands.executeCommand('workbench.action.openSettings', `${SECTION}.pythonPath`);
      } else if (pick === showOutput) this.out.show(true);
    }, (e) => this.log(`${proc.stamp()} !! ERROR could not show a message: ${e}`));
  }

  _readSettings() {
    const cfg = vscode.workspace.getConfiguration(SECTION);
    /** @type {Record<string, unknown>} */
    const raw = {};
    for (const key of Object.keys(argv.DEFAULTS)) raw[key] = cfg.get(key);
    // (2026-10-21, owner "Lisää varoituksella") a stored full default counts only with the machine-local
    // marker the confirmed choice wrote; once the setting is something else, the marker goes
    // (2026-10-26) ...but not while _updateSettings writes: the configuration event of its first key
    // (askProfile) arrives before defaultProfile=full is written and cleared the fresh marker, so the
    // next setting change (e.g. the add panel's Subagents) read full back as safe
    const marker = this._fullMarker();
    raw[argv.FULL_DEFAULT_MARKER] = marker;
    if (marker && raw.defaultProfile !== argv.FULL_PROFILE && !this._settingsWrites) {
      this._setFullMarker(false).catch((e) => this.log(`${proc.stamp()} WARN full default marker: ${e}`));
    }
    const { settings, warnings } = argv.normalizeSettings(raw);
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.1) tasksAtOnce + newTaskStart: normalised beside the others
    const parallel = argv.normalizeParallelSettings(raw);
    Object.assign(settings, parallel.settings);
    warnings.push(...parallel.warnings);
    for (const w of warnings) {
      this.log(`${proc.stamp()} WARN ${w}`);
      vscode.window.showWarningMessage(`Sleeping Clanker: ${w}`);
    }
    return settings;
  }

  onConfigChanged() {
    const before = this.settings;
    this.settings = this._readSettings();
    if (before.pythonPath !== this.settings.pythonPath) this._python = null;
    if (before.dataDir !== this.settings.dataDir) this.where = null;
    this.log(`${proc.stamp()} settings changed`);
    this.onLanguageChanged(); // (2026-10-21) claudAutopilot.language; a no-op when it did not change
    if (!before.autoContinueAfterLimit && this.settings.autoContinueAfterLimit) { // (2026-10-27, limit_continue fix round 2)
      this._releaseLimitHolds().catch((err) => this.reportError('usage-limit hold', err));
    }
    if (before.autoContinueAfterLimit && !this.settings.autoContinueAfterLimit) this._autoRunnersNotice();
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.4) tasks at once changed: each runner this window started takes it at its
    // next pick (`tasks-at-once N --json`, no restart); the first time it goes above 1, the one-time intro (8.12)
    if (before.tasksAtOnce !== this.settings.tasksAtOnce) {
      this._applyTasksAtOnce(this.settings.tasksAtOnce).catch((err) => this.reportError('tasks at once', err));
    }
    this._refreshAddPanel(); // (2026-10-21, add panel) a setting changed elsewhere: the dropdowns follow
    this.poller.trigger();
  }

  /**
   * (2026-10-27, integrate: limit_continue review LOW-3) claudAutopilot.autoContinueAfterLimit turned off:
   * a runner reads its --on-limit once, at its start, so the runners this window started with `auto` still
   * continue by themselves after a limit. Say so once, naming them; nothing when there is none.
   */
  _autoRunnersNotice() {
    const names = [...this.ownRunners.values()].filter((r) => r.onLimit === 'auto').map((r) => r.name);
    if (!names.length) return;
    this.event(`autoContinueAfterLimit turned off: ${names.length} runner(s) already running keep --on-limit auto `
      + `until they are restarted (${names.join(', ')})`);
    vscode.window.showInformationMessage(t('Sleeping Clanker: continuing after a usage limit is off now, but the runners already running ({0}) still continue by themselves after a limit until they are restarted.', names.join(', ')));
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.4) `tasks-at-once N --json` for every runner this window started: it applies
   * at the runner's next pick, so nothing restarts. A runner of another window keeps its own until it restarts.
   * @param {number} n
   */
  async _applyTasksAtOnce(n) {
    const want = argv.tasksAtOnceOf({ tasksAtOnce: n });
    for (const r of [...this.ownRunners.values()]) {
      try {
        const v = await this.cliJson(argv.tasksAtOnceArgs(want), parse.parseTasksAtOnce, undefined, r.project, laneArg(r.lane || argv.DEFAULT_LANE));
        // (2026-10-29, integrate round 9) without a live runner the number is not kept for a plain run: this window
        // passes --tasks-at-once from the setting when it starts the runner again (argv.runArgs)
        this.event(`tasks at once ${v.n} for the runner of ${r.name} (${v.applies === 'next-pick' ? 'applies at its next pick'
          : 'no runner now: the next start passes --tasks-at-once from the setting'})`);
      } catch (err) {
        this.log(`${proc.stamp()} WARN tasks-at-once for ${r.name}: ${err instanceof Error ? err.message : err}`);
      }
    }
    this._parallelIntro();
    this.poller.trigger();
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.12) The first time tasks at once is above 1: a one-time note (copies, conflicts
   * ask you, .env/.venv are not copied). Kept in globalState; never awaited (a toast settles only when dismissed).
   */
  _parallelIntro() {
    const n = argv.tasksAtOnceOf(this.settings);
    const gs = this.context.globalState;
    if (n <= 1 || !gs || gs.get(PARALLEL_INTRO_KEY, false)) return;
    Promise.resolve(gs.update(PARALLEL_INTRO_KEY, true)).catch(() => {});
    this.event(`tasks at once ${n}: the one-time intro shown`);
    Promise.resolve(vscode.window.showInformationMessage(t(PARALLEL_INTRO, n)))
      .catch((err) => this.log(`${proc.stamp()} WARN tasks-at-once intro: ${err instanceof Error ? err.message : err}`));
  }

  // ---- the window's project (SPEC_PROJECTS 3, amendment M8) -------------------------------------
  /** Re-read the project root from the workspace folders; the data dir is re-resolved lazily. */
  resolveProject() {
    const folders = (vscode.workspace.workspaceFolders || []).map((f) => ({ scheme: f.uri.scheme, fsPath: f.uri.fsPath }));
    const root = argv.pickProjectRoot(folders);
    if (root !== this.projectRoot) {
      this.projectRoot = root;
      this.where = null;
      this.status = null;
      this._statuses = null; // another project: a new baseline, no bogus changes
      this.log(`${proc.stamp()} project: ${root || '(no folder: the shared queue)'}`);
    }
    this._renderTitle();
  }

  projectName() {
    return this.projectRoot ? path.basename(this.projectRoot) || this.projectRoot : null;
  }

  _renderTitle() {
    setIfChanged(this.view, 'title', `${t('Queue')} — ${this.projectName() || t('(no folder)')}`);
  }

  onFoldersChanged() {
    this.resolveProject(); // a runner this window started keeps running for its own project
    this.poller.trigger();
  }

  async setAllProjects(/** @type {boolean} */ on) {
    this.allProjects = on;
    const ws = this.context.workspaceState;
    if (ws) await ws.update(ALL_PROJECTS_KEY, on);
    await vscode.commands.executeCommand('setContext', ALL_PROJECTS_KEY, on);
    await this.poller.trigger();
  }

  // ---- python + CLI -------------------------------------------------------------------------
  /** Resolve the interpreter once per session (again after pythonPath changes). */
  python() {
    if (!this._python) {
      const p = this._detectPython();
      this._python = p;
      p.catch(() => { if (this._python === p) this._python = null; }); // retry on the next call
    }
    return this._python;
  }

  async _detectPython() {
    const core = path.join(this.pythonDir, argv.CORE_MODULE, '__main__.py');
    if (!fs.existsSync(core)) throw new Error(t('bundled core missing ({0}); rebuild the VSIX', core));
    const candidates = argv.pythonCandidates(this.settings.pythonPath);
    const found = await proc.detectPython(candidates, (c) => proc.runCli({
      command: c.command, args: proc.versionProbeArgs(c), cwd: this.pythonDir,
      env: argv.childEnv(process.env, this.pythonDir, this.pycacheDir), log: (l) => this.log(l), timeoutMs: 15000,
    }));
    this.log(`${proc.stamp()} python: ${proc.formatArgv(found.python.command, found.python.args)} ${found.version.join('.')}`);
    return found.python;
  }

  /**
   * Run one core command; throws on a non-zero exit with the stderr tail.
   * (2026-10-13) cwd is the bundled python dir, never a workspace folder: `python -m` puts cwd first
   * on sys.path, so a workspace could otherwise shadow the core's modules.
   * (2026-10-13, v0.2) `project` defaults to this window's project; null = the shared queue.
   * (2026-10-15, v0.3) `lane` = the queue (`--lane=<name>`); every item or lane call passes it.
   * (2026-10-17, 0.3.3) `opts.quiet`: a poller-driven read-only call, logged only on failure or when
   * slow (proc.runCli), unless claudAutopilot.logPolling is on.
   * @param {string[]} args @param {string[]} [display] @param {string | null} [project] @param {string | null} [lane]
   * @param {{quiet?: boolean, okCodes?: number[]}} [opts]
   * (2026-10-27, U4) `opts.okCodes`: exit codes that still return (e.g. 1 = `merge` refused, with its JSON on stdout)
   */
  async cli(args, display, project, lane, opts = {}) {
    const proj = project === undefined ? this.projectRoot : project;
    const py = await this.python();
    const full = argv.cliArgv(py, this.settings, args, proj, lane || null);
    const shown = display ? argv.cliArgv(py, this.settings, display, proj, lane || null) : full;
    const r = await proc.runCli({
      command: py.command, args: full, display: shown, cwd: this.pythonDir,
      env: argv.childEnv(process.env, this.pythonDir, this.pycacheDir), log: (l) => this.log(l),
      quiet: !!opts.quiet && !this.settings.logPolling,
    });
    if (r.code !== 0 && !(opts.okCodes && r.code !== null && opts.okCodes.includes(r.code))) throw this._cliFailed(args, r);
    return r;
  }

  /**
   * The error of a failed core command, with the stderr tail (also logged).
   * @param {string[]} args @param {{code: number | null, stderr: string, spawnError?: string | null}} r
   */
  _cliFailed(args, r) {
    const tail = r.stderr.trim().split(/\r?\n/).slice(-5).join(' | ');
    if (r.stderr.trim()) this.log(r.stderr.trimEnd());
    return new Error(`autopilot ${args[0]} failed (${r.spawnError || `exit ${r.code}`})${tail ? `: ${tail}` : ''}`);
  }

  /**
   * @template T
   * @param {string[]} args
   * @param {(s: string) => ({ok: true, value: T} | {ok: false, error: string})} parser
   * @param {string[]} [display]
   * @param {string | null} [project]
   * @param {string | null} [lane]
   * @param {{quiet?: boolean, okCodes?: number[]}} [opts]
   * (2026-10-29, F4) an exit in `opts.okCodes` whose stdout is not the JSON fails as cli() fails (the stderr tail)
   * @returns {Promise<T>}
   */
  async cliJson(args, parser, display, project, lane, opts = {}) {
    const r = await this.cli(args, display, project, lane, opts);
    const p = parser(r.stdout);
    if (!p.ok && r.code !== 0) throw this._cliFailed(args, r);
    if (!p.ok) throw new Error(`autopilot ${args[0]}: ${p.error}`);
    return p.value;
  }

  // ---- refresh ------------------------------------------------------------------------------
  async refreshNow() {
    try {
      // (2026-10-26, speed) the first poll no longer spawns `where --json` before `lanes`: the lanes
      // output carries the same core-resolved data dir (the main queue's), so the first tree waits for
      // ONE spawn; `projects` (all-projects view) runs side by side with `lanes`
      const root = this.projectRoot;
      // (2026-10-15, SPEC_V03 2.2) ONE call for every queue of the project, items included; the
      // window's status is the sum over the queues.
      const lanesP = this.cli(argv.lanesArgs(), undefined, undefined, undefined, QUIET);
      const rowsP = this.allProjects
        ? this.cliJson(argv.projectsArgs(), parse.parseProjects, undefined, undefined, undefined, QUIET) : null;
      const [lanesOut, p] = await Promise.all([lanesP, rowsP]);
      const pollRoot = root;
      const lanes = parse.parseLanes(lanesOut.stdout);
      if (!lanes.ok) throw new Error(`autopilot lanes: ${lanes.error}`);
      const warned = warningChanges(this._laneWarnings, lanes.warnings); // (0.3.3) only when the set changes
      this._laneWarnings = warned.next;
      for (const line of warned.lines) this.log(`${proc.stamp()} ${line}`);
      const status = parse.sumLanes(lanes.value);
      // folders changed meanwhile: resolved again by the next poll
      if (!this.where && status.dataDir && root === this.projectRoot) this.where = { dataDir: status.dataDir, projectRoot: root };
      const items = lanes.value.flatMap((l) => l.items);
      /** @type {import('./lib/parse').Project[] | null} */
      const rows = p || null;
      if (pollRoot === this.projectRoot) {
        // (2026-10-17, 0.3.3) each item status change since the last poll, as an event
        const diff = statusChanges(this._statuses, items, argv.DEFAULT_LANE);
        this._statuses = diff.next;
        for (const line of diff.lines) this.event(line);
      }
      this.status = status;
      this.lanes = lanes.value;
      this._lanesRoot = pollRoot; // (2026-10-26, speed) whose queues these are (knownDataDir)
      this.rows = rows;
      if (pollRoot === this.projectRoot) this._checkRunnerVersions(lanes.value);
      this.lastError = null;
      this._setTree(items, rows);
      if (pollRoot === this.projectRoot) this._cacheWarnings(items);
      if (pollRoot === this.projectRoot) this._mergeWatch(items); // (2026-10-27, U4, 8.11) the dirty-editor warning
      if (pollRoot === this.projectRoot) this._leftoversPoll(lanes.value); // (2026-10-27, U4, 8.10) not awaited
      if (pollRoot === this.projectRoot) this._inPlaceWatch(lanes.value); // (2026-10-27, U4 fix round 1, 8.13)
      if (pollRoot === this.projectRoot) this._limitHolds(lanes.value, pollRoot); // (2026-10-26, limit_continue)
      this._render();
      await vscode.commands.executeCommand('setContext', 'claudAutopilot.runnerAlive', status.runnerAlive);
      // (2026-10-21, v049 dontask_hold) the title bar's Hold queue / Release hold
      const held = lanes.value.filter((l) => l.hold).length;
      await vscode.commands.executeCommand('setContext', 'claudAutopilot.anyHeld', held > 0);
      await vscode.commands.executeCommand('setContext', 'claudAutopilot.allHeld', // (2026-10-27, fix round 2 MED) timed: not "all held"
        lanes.value.length > 0 && lanes.value.every((l) => !usage.holdCandidate(l, true)));
      await this.liveLog.onPoll();
      await this.taskViews.onPoll();
      if (pollRoot === this.projectRoot) this.taskViews.autorunPoll(items); // (2026-10-26, modes) not awaited
      if (!this._migrationChecked) {
        this._migrationChecked = true;
        this.offerMigration().catch((err) => this.reportError('shared queue check', err));
      }
    } catch (err) {
      this._render(err instanceof Error ? err.message : String(err));
      throw err;
    }
  }

  /**
   * (2026-10-20, 0.4.4 logfix D) The core this window would start (python/<argv.CORE_MODULE>/__init__.py);
   * "" when it cannot be read, which turns the check off.
   */
  _bundledVersion() {
    if (this._coreVersion === undefined) {
      let v = '';
      try {
        const text = fs.readFileSync(path.join(this.pythonDir, argv.CORE_MODULE, '__init__.py'), 'utf8');
        const m = /^__version__\s*=\s*["']([^"']+)["']/m.exec(text);
        v = m ? m[1] : '';
      } catch (_none) {
        v = '';
      }
      this._coreVersion = v;
    }
    return this._coreVersion;
  }

  /**
   * (2026-10-20, 0.4.4 logfix D) One non-modal warning per live runner whose core is not this window's
   * (an old lock without `version` counts as unknown/old), with a "Reload Window" action.
   * @param {import('./lib/parse').Lane[]} lanes
   */
  _checkRunnerVersions(lanes) {
    const bundled = this._bundledVersion();
    if (!bundled) return;
    if (!this._versionWarned) this._versionWarned = new Set();
    const warned = this._versionWarned;
    const fresh = parse.staleRunners(lanes, bundled).filter((r) => !warned.has(`${r.dataDir}|${r.pid}`));
    if (!fresh.length) return;
    for (const r of fresh) warned.add(`${r.dataDir}|${r.pid}`);
    const msg = parse.staleRunnerWarning(fresh, bundled);
    if (!msg) return;
    this.log(`${proc.stamp()} WARN ${msg}`);
    const reload = t(RELOAD_WINDOW); // (2026-10-27, i18n) translated once: the shown button is the one compared
    Promise.resolve(vscode.window.showWarningMessage(msg, reload)).then((pick) => {
      if (pick === reload) return vscode.commands.executeCommand('workbench.action.reloadWindow');
      return undefined;
    }).catch((err) => this.reportError('reload window', err));
  }

  /** @returns {import('./lib/format').Ctx} */
  _ctx() {
    return { runnerAlive: !!(this.status && this.status.runnerAlive), now: new Date(), maxLimitWaits: this.settings.maxLimitWaits };
  }

  /**
   * @param {import('./lib/parse').Item[]} items this window's project
   * @param {import('./lib/parse').Project[] | null} rows all projects, or null = this project only
   */
  _setTree(items, rows) {
    // (2026-10-26, owner) the running task's live activity line, read from its newest run file
    if (!this._activity) this._activity = new ActivityCache();
    const asked = new Set();
    const activity = (/** @type {string} */ dir, /** @type {string} */ id) => {
      try {
        const runDir = argv.runLogDir(dir, id);
        asked.add(runDir);
        return /** @type {ActivityCache} */ (this._activity).get(runDir);
      } catch (_e) {
        return null;
      }
    };
    const ctx = { ...this._ctx(), groups: this.settings.groupByStatus, activity }; // (2026-10-21) status groups
    const dataDir = this.status ? this.status.dataDir : null;
    /** @type {import('./lib/view').Node[]} */
    let roots;
    if (rows) {
      roots = view.projectTree(rows, { thisDataDir: dataDir, thisRoot: this.projectRoot, thisName: this.projectName() }, ctx);
    } else if (this.lanes.length) {
      roots = view.laneTree(this.lanes, { ...ctx, project: this.projectRoot, projectName: this.projectName() || '' });
    } else {
      roots = view.itemList(items, { ...ctx, project: this.projectRoot, dataDir }); // (2026-10-21) subtasks, groups
    }
    this._activity.prune(asked);
    // (2026-10-26, owner task f60cec52) the Today counter on top, from the same items: no extra CLI call
    const head = this.status && items.length ? [view.counterNode(stats.counter(items, ctx.now), this.projectName())] : [];
    this.provider.setNodes(items, roots, this._footer(roots), head);
    this._refreshStats();
  }

  /** (2026-10-26, owner task f60cec52) The statistics document's text: this window's project, now. */
  statsText() {
    this._statsText = stats.report(this.provider.items, { now: new Date(), project: this.projectName() });
    return this._statsText;
  }

  /** (2026-10-26) An open statistics document is rendered again when its text would change. */
  _refreshStats() {
    const docs = (vscode.workspace && vscode.workspace.textDocuments) || [];
    const open = docs.find((/** @type {any} */ d) => d.uri && d.uri.scheme === STATS_SCHEME);
    if (!open || stats.report(this.provider.items, { now: new Date(), project: this.projectName() }) === this._statsText) return;
    this._statsEmitter.fire(open.uri);
  }

  /** (2026-10-26, owner task f60cec52) The counter's click: the statistics, today and the last 7 days. */
  async showStats() {
    const uri = vscode.Uri.from({ scheme: STATS_SCHEME, path: STATS_PATH });
    this._statsEmitter.fire(uri); // an already open document is rendered again
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  /**
   * (2026-10-21) Once per attempt: a task that needs you (blocked) and whose prompt cache runs out
   * within 15 minutes (format.cacheState) gets a warning; a reply before then reuses the cache.
   * @param {import('./lib/parse').Item[]} items this window's project
   */
  _cacheWarnings(items) {
    const now = new Date();
    for (const item of items) {
      const c = item.status === 'blocked' ? format.cacheState(item, now) : null;
      if (!c || !c.warn) continue;
      const key = `${item.lane}#${item.id}#${item.raw.last_ended_at}`;
      if (this._cacheWarned.has(key)) continue;
      this._cacheWarned.add(key);
      const open = i18n.t('Open Task View');
      const msg = i18n.t('Sleeping Clanker: the prompt cache of "{0}" runs out at {1}. Reply before then to reuse it; after that the whole conversation is read again.',
        format.itemLabel(item, 40), format.localTime(c.until.toISOString(), now));
      Promise.resolve(vscode.window.showWarningMessage(msg, open))
        .then((pick) => (pick === open ? this.openTaskView(item) : undefined))
        .catch((err) => this.reportError('cache warning', err));
    }
  }

  /**
   * (2026-10-26, limit_continue) Once per usage-limit hold (lib/limithold.js): with
   * claudAutopilot.autoContinueAfterLimit on, release it and start the queue's runner again; else ask
   * Continue after reset / Always (the setting, User scope) / Not now (the hold stays).
   * @param {import('./lib/parse').Lane[]} lanes this window's project
   * @param {string | null} [project] (2026-10-27, fix LOW-5) the project the poll read: the answer acts on it
   */
  _limitHolds(lanes, project = this.projectRoot, label = undefined) {
    // (2026-10-27, fix round 2) answered holds survive a window reload (workspaceState); `_limitBusy`: asked,
    // not answered yet; `_limitLater`: answered "Not now" (released when the setting turns on)
    const ws = this.context && this.context.workspaceState;
    if (!this._limitAsked) this._limitAsked = new Set(ws ? ws.get(LIMIT_ASKED_KEY, []) : []);
    if (!this._limitBusy) this._limitBusy = new Set();
    if (!this._limitLater) this._limitLater = new Map();
    for (const h of limithold.newLimitHolds(lanes, this._limitAsked, project, label)) {
      this._limitBusy.add(h.key);
      const done = (/** @type {string} */ res) => {
        this._limitBusy.delete(h.key);
        if (res === 'later') this._limitLater.set(h.key, h);
        if (ws) ws.update(LIMIT_ASKED_KEY, [...(ws.get(LIMIT_ASKED_KEY, []) || []).filter((/** @type {string} */ k) => k !== h.key), h.key].slice(-50));
      };
      limithold.handleLimitHold(h, {
        autoContinue: this.settings.autoContinueAfterLimit, many: lanes.length >= 2,
        ask: (msg, buttons) => vscode.window.showWarningMessage(msg, ...buttons),
        setAlways: () => vscode.workspace.getConfiguration(SECTION).update('autoContinueAfterLimit', true, vscode.ConfigurationTarget.Global),
        resume: (x) => this._continueAfterLimit(x),
        notNow: (x) => vscode.window.showInformationMessage(
          t('Sleeping Clanker: {0} stays on hold. "Resume Queue (Release Hold)" in the Queue view continues it later.', x.label || format.laneLabel(x.lane))),
        event: (line) => this.event(line),
      }).then(done, (err) => { done('error'); this.reportError('usage-limit hold', err); });
    }
  }

  /**
   * (2026-10-27, limit_continue fix round 2) A runner this window started for ANOTHER project exited (code 0):
   * read its queue once; a usage-limit hold gets the question, acting on that project.
   * @param {RunnerRecord} rec
   */
  async _limitHoldAfterExit(rec) {
    const st = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, rec.project, rec.lane);
    if (!st.hold || st.hold.reason !== 'limit') return;
    this._limitHolds([/** @type {any} */ ({ name: rec.lane || argv.DEFAULT_LANE, dataDir: st.dataDir, hold: st.hold })], rec.project, rec.name);
  }

  /**
   * (2026-10-27, limit_continue fix round 2) claudAutopilot.autoContinueAfterLimit turned on: release the
   * usage-limit holds still open (answered "Not now", or this project's held queues) as the auto path does.
   * A question still open is left to its answer.
   */
  async _releaseLimitHolds() {
    const open = new Map(this._limitLater || []);
    this._limitLater = new Map();
    for (const h of limithold.newLimitHolds(this.lanes, new Set(), this.projectRoot)) if (!open.has(h.key)) open.set(h.key, h);
    for (const h of open.values()) {
      if (this._limitBusy && this._limitBusy.has(h.key)) continue;
      if (!(await this._sameLimitHold(h, h.project === undefined ? this.projectRoot : h.project))) continue;
      this.event(`usage-limit hold of ${h.label || format.laneLabel(h.lane)}: autoContinueAfterLimit turned on; releasing it`);
      await this._continueAfterLimit(h, true);
    }
  }

  /**
   * (2026-10-27, integrate: limit_continue review LOW-1) True while the queue still has THIS usage-limit
   * hold (`status --json`, the local core CLI): reason "limit" and the same since.
   * @param {import('./lib/limithold').LimitHold} h @param {string | null} project
   */
  async _sameLimitHold(h, project) {
    const st = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, project, laneArg(h.lane));
    return !!st.hold && st.hold.reason === 'limit' && st.hold.since === h.since;
  }

  /**
   * (2026-10-26, limit_continue) `hold off` for a usage-limit hold, then start the queue's runner (an
   * automatic start: it waits out wait_until and resumes the same session). The runner that set the hold
   * exits right after; when this window started it, its exit is awaited first (at most 15 s).
   * (2026-10-27, integrate: review LOW-1) The notification stays in the notification center, so its
   * Continue can be clicked much later: first check that the queue still has the same limit hold
   * (_sameLimitHold). If it was released, or replaced by the owner's own hold or a newer limit hold, do
   * nothing. `checked`: the caller (_releaseLimitHolds) has just checked.
   * @param {import('./lib/limithold').LimitHold} h @param {boolean} [checked]
   */
  async _continueAfterLimit(h, checked = false) {
    // (2026-10-27, fix LOW-5) the project where the hold was seen, not this.projectRoot at click time
    const project = h.project === undefined ? this.projectRoot : h.project;
    if (!checked && !(await this._sameLimitHold(h, project))) {
      this.event(`usage-limit hold of ${h.label || format.laneLabel(h.lane)} (since ${h.since || '?'}) is no longer on; `
        + 'nothing released, no runner started');
      return;
    }
    // (2026-10-27, integrate: review LOW-2) only the usage-limit part: a limit hold that replaced the owner's
    // own hold ("Start anyway") gives it back, and the runner starts past it again, as "Start anyway" did
    const res = await this.cliJson(argv.holdArgs('limit-off'), parse.parseHoldResult, undefined, project, laneArg(h.lane));
    // (2026-10-27, integrate: hold kinds compose) a TIMED owner hold that came back is not started past: the
    // runner starts as usual and waits it out, as for any timed hold
    const timed = res && res.hold && res.until ? res.until : null;
    const ownerSince = res && res.hold && res.since && !timed ? res.since : null;
    this.event(ownerSince
      ? `usage-limit hold released: ${format.laneLabel(h.lane)} (${h.dataDir}); your own hold (since ${ownerSince}) is back, `
        + 'and the runner starts past it as "Start anyway" did'
      : timed ? `usage-limit hold released: ${format.laneLabel(h.lane)} (${h.dataDir}); your timed hold (until ${timed}) is back, `
        + 'and the runner waits for it'
        : `hold off (usage limit): ${format.laneLabel(h.lane)} (${h.dataDir})`);
    await this._startAfterLimit(project, h.lane, h.dataDir, h.since, ownerSince);
    await this.poller.trigger();
  }

  /**
   * (2026-10-27, limit_continue fix MED-1) After a usage-limit hold is released (Continue, Always, or
   * "Release hold" in the Queue view): start the queue's runner (an automatic start), since the ask runner
   * has exited. A runner this window started is awaited first (at most 15 s).
   * (2026-10-27, fix round 2) only when it ran when the hold was set (it set it, so it exits); a runner of
   * this window started later (past the hold) keeps working: nothing is started, at once.
   * (2026-10-27, integrate: review LOW-2) `ownerSince`: the owner's own hold came back (`hold limit-off`):
   * the runner starts past exactly that hold (ignoreHold + heldSince), as "Start anyway" did.
   * @param {string | null} project @param {string} lane @param {string} dataDir @param {string | null} [since] the hold's
   * @param {string | null} [ownerSince]
   */
  async _startAfterLimit(project, lane, dataDir, since = null, ownerSince = null) {
    const own = this.ownRunners.get(dataDir);
    if (own && !((own.startedAt || 0) <= Date.parse(since || ''))) {
      this.event(`runner start (${format.laneLabel(lane)}) skipped: this window's runner ${own.name} is running`);
      return;
    }
    if (own) await Promise.race([own.exited, new Promise((r) => setTimeout(r, 15000))]);
    await this.startRunner(project, undefined, laneArg(lane),
      ownerSince ? { auto: true, ignoreHold: true, heldSince: ownerSince } : { auto: true });
  }

  /**
   * (2026-10-20, 0.4.5, SPEC_V045_DEFAULTS) The "Defaults for +" section, last in the view.
   * (2026-10-21, owner "move default for bottom") ALWAYS the last root node while it is shown: 0.4.5
   * left it out of an empty tree (an empty queue, an empty all-projects list, a window before its first
   * successful poll), so in those states it was not at the bottom at all. An empty tree after a poll
   * gets the "queue is empty" row above it instead of VS Code's view welcome (shown only for a tree
   * without nodes; it still appears when the rows are hidden).
   * @param {import('./lib/view').Node[]} roots
   * @returns {import('./lib/view').Node[]}
   */
  _footer(roots) {
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.10) "Leftover copies (n)" above the defaults, only when there are any
    const left = view.leftoversNode(this.leftovers || null);
    const extra = left ? [left] : [];
    if (!this.settings.showAddDefaults) return extra;
    const section = view.defaultsNode(this.addDefaults(), this._defaultsCtx());
    return roots.length || !this.status ? [...extra, section] : [view.emptyNode(), ...extra, section];
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.10) `worktrees --json` of this window's project, at most once a minute and only
   * while tasks at once is in use (the setting, a lane's runner, or an item with a copy): its leftovers feed the
   * "Leftover copies (n)" node. Never awaited; a failure is a log line (the node keeps its last list).
   * @param {import('./lib/parse').Lane[]} lanes
   */
  _leftoversPoll(lanes) {
    const used = argv.tasksAtOnceOf(this.settings) > 1 || (lanes || []).some((l) => !!l.parallel || (l.tasksAtOnce || 1) > 1
      || l.items.some((i) => !!i.worktree)) || !!(this.leftovers && this.leftovers.length);
    if (!used || this._leftoversBusy) return;
    const now = Date.now();
    if (now - (this._leftoversAt || 0) < LEFTOVERS_POLL_MS) return;
    this._leftoversAt = now;
    this._leftoversBusy = true;
    const root = this.projectRoot;
    this.cliJson(argv.worktreesArgs(), parse.parseWorktrees, undefined, root, undefined, QUIET).then((v) => {
      if (root !== this.projectRoot) return;
      const before = JSON.stringify(this.leftovers || []);
      this.leftovers = v.leftovers;
      if (JSON.stringify(v.leftovers) !== before) this._setTree(this.provider.items, this.rows || null);
    }).catch((err) => this.log(`${proc.stamp()} WARN worktrees: ${err instanceof Error ? err.message : err}`))
      .finally(() => { this._leftoversBusy = false; });
  }

  /**
   * (2026-10-27, U4 fix round 1, SPEC_V05_PARALLEL 8.13) A runner this window started with tasks at once > 1 that
   * fell back to in-place (its lane's parallel.mode reads 'in-place') edits the folder itself: the first time that
   * is read, the "Two queues edit the same folder" check runs for it (not awaited, as at a runner start).
   * @param {import('./lib/parse').Lane[]} lanes
   */
  _inPlaceWatch(lanes) {
    for (const r of this.ownRunners.values()) {
      if (r.inPlaceSeen || r.project !== this.projectRoot) continue;
      if (!lanes.some((l) => l.dataDir === r.dataDir && !!l.parallel && l.parallel.mode === 'in-place')) continue;
      r.inPlaceSeen = true;
      this._parallelWarning(r.project, r.dataDir)
        .catch((err) => this.log(`${proc.stamp()} WARN parallel-queue warning: ${err instanceof Error ? err.message : err}`));
    }
  }

  /**
   * (2026-10-27, U4, 8.10) Open folder of a leftover copy: only a path of the host's own `worktrees --json` list.
   * @param {string} dir
   */
  async _openLeftover(dir) {
    if (!(this.leftovers || []).some((l) => l.path === dir)) throw new Error(t('That copy is not listed any more: {0}', dir));
    if (!fs.existsSync(dir)) throw new Error(t('The folder is gone: {0}', dir));
    // (2026-10-27, U4 fix round 1) a path from data: openExternal on a file would launch it; only a real folder
    const s = fs.lstatSync(dir, { throwIfNoEntry: false });
    if (!s || !s.isDirectory() || s.isSymbolicLink()) throw new Error(t('{0} is not a folder.', dir));
    await this._openExternal(dir);
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.11) When an item's merge turns "merged": an open editor with unsaved edits
   * whose file the merge changed gets a warning (VS Code itself asks on save). The first poll is the baseline.
   * @param {import('./lib/parse').Item[]} items this window's project
   */
  _mergeWatch(items) {
    const prev = this._mergeStates;
    /** @type {Map<string, string>} */
    const next = new Map();
    const keyOf = (/** @type {import('./lib/parse').Item} */ it) => `${String(it.lane || argv.DEFAULT_LANE).toLowerCase()}\u0000${it.id}`;
    for (const it of items) if (it.merge && it.merge.state) next.set(keyOf(it), it.merge.state);
    this._mergeStates = next;
    if (!prev || !this.projectRoot) return;
    for (const it of items) {
      if (!it.merge || it.merge.state !== 'merged' || prev.get(keyOf(it)) === 'merged') continue;
      this._dirtyWarning(it, this.projectRoot);
    }
  }

  /** (2026-10-27, U4, 8.11) @param {import('./lib/parse').Item} item @param {string} root */
  _dirtyWarning(item, root) {
    const fold = (/** @type {string} */ p) => (process.platform === 'win32' ? p.toLowerCase() : p);
    const changed = new Set(((item.merge && item.merge.changed) || []).map((p) => fold(path.resolve(root, p))));
    if (!changed.size) return;
    const docs = (vscode.workspace && vscode.workspace.textDocuments) || [];
    for (const d of docs) {
      if (!d || !d.isDirty || !d.uri || d.uri.scheme !== 'file' || !changed.has(fold(path.resolve(d.uri.fsPath)))) continue;
      const rel = path.relative(root, d.uri.fsPath) || d.uri.fsPath;
      this.event(`merge of ${item.id} changed ${rel} while it has unsaved edits in an editor`);
      Promise.resolve(vscode.window.showWarningMessage(t(DIRTY_MERGED, rel))).catch(() => {});
    }
  }

  /** @returns {import('./lib/view').AddDefaults} */
  addDefaults() {
    const s = this.settings;
    return { kind: s.defaultKind, profile: s.defaultProfile, askProfile: s.askProfile, lane: s.defaultLane, askGoalCaps: s.askGoalCaps,
      ultracode: s.defaultUltracode, autorun: s.defaultAutorun, maxAgents: s.defaultMaxAgents }; // (2026-10-26, modes; 2026-10-27 maxAgents)
  }

  /** @returns {import('./lib/view').DefaultsCtx} */
  _defaultsCtx() {
    return { lanes: this.laneNames(), goalMaxRounds: this.settings.goalMaxRounds, goalMaxHours: this.settings.goalMaxHours };
  }

  /** @param {string | null} [error] */
  _render(error = null) {
    const opts = { error, projectName: this.projectName(), lanes: this.lanes, ownLanes: this.ownLaneNames() };
    setIfChanged(this.statusItem, 'text', format.statusBarText(this.status, opts));
    setIfChanged(this.statusItem, 'tooltip', this._withUsage(format.statusBarTooltip(this.status, opts), error));
    // (2026-10-15, SPEC_V03 5.1) the warning background while any item needs the owner
    const bg = format.statusBarBackground(this.status, opts);
    if (bg !== this._statusBg) {
      this._statusBg = bg;
      this.statusItem.backgroundColor = bg ? new vscode.ThemeColor(bg) : undefined;
    }
    // (2026-10-20, 0.4.4) green text while a task runs; VS Code has no green item background
    const fg = format.statusBarColor(this.status, opts);
    if (fg !== this._statusFg) {
      this._statusFg = fg;
      this.statusItem.color = fg ? new vscode.ThemeColor(fg) : undefined;
    }
    this._renderCounts(format.statusBarSegments(this.status, opts));
    this._renderGoal(error ? null : format.activeGoal(this.provider.items));
  }

  /**
   * (2026-10-21, owner) Show the count segments that have a count, hide the rest; the yellow "?"
   * count also badges the sidebar icon, so a question is seen with the view closed.
   * @param {{key: string, text: string, tooltip: string}[]} segments
   */
  _renderCounts(segments) {
    const shown = new Map(segments.map((s) => [s.key, s]));
    for (const [key, it] of this.countItems) {
      const s = shown.get(key);
      if (s) {
        setIfChanged(it, 'text', s.text);
        setIfChanged(it, 'tooltip', s.tooltip);
        if (!it._shown) { it._shown = true; it.show(); }
      } else if (it._shown !== false) { it._shown = false; it.hide(); }
    }
    const n = shown.has('blocked') && this.status ? this.status.counts.blocked : 0;
    if (n !== this._badgeCount) {
      this._badgeCount = n;
      this.view.badge = n > 0 ? { value: n, tooltip: t('{0} task(s) need your answer', n) } : undefined; // (tester round 2) translated
    }
  }

  /** (2026-10-17, 0.3.3) Queues of this window's project whose runner this window started. */
  ownLaneNames() {
    return [...this.ownRunners.values()].filter((r) => r.project === this.projectRoot)
      .map((r) => r.lane || argv.DEFAULT_LANE);
  }

  /**
   * (2026-10-17, 0.3.3) Is a runner alive for (project, lane)? This window's own runner, else the
   * last poll (this project only; another project's queue is not polled: false).
   * @param {string | null} project @param {string | null} lane
   */
  laneAlive(project, lane) {
    const name = (lane || argv.DEFAULT_LANE).toLowerCase();
    const own = [...this.ownRunners.values()].some((r) => r.project === project && (r.lane || argv.DEFAULT_LANE).toLowerCase() === name);
    if (own) return true;
    if (project !== this.projectRoot) return false;
    const row = this.lanes.find((l) => l.name.toLowerCase() === name);
    return row ? row.runnerAlive : !!(this.status && this.status.runnerAlive);
  }

  /**
   * (2026-10-21, v049 dontask_hold) Is (project, lane) on hold, as far as the last poll knows? This
   * window's project only (another project's queue: false; startRunner reads its status anyway).
   * @param {string | null} project @param {string | null} lane
   */
  laneHeld(project, lane) {
    if (project !== this.projectRoot) return false;
    const name = (lane || argv.DEFAULT_LANE).toLowerCase();
    const row = this.lanes.find((l) => l.name.toLowerCase() === name);
    // (2026-10-26, usage forecast) a timed hold is not "held" here: its runner starts and waits it out
    const hold = row ? row.hold : name === argv.DEFAULT_LANE && this.status ? this.status.hold : null;
    return !!hold && !hold.until;
  }

  /**
   * (2026-10-26, usage forecast) The status bar tooltip plus the usage forecast line(s) and the queues on
   * a timed hold; unchanged on an error or without usage data.
   * @param {string} text @param {string | null} error
   */
  _withUsage(text, error) {
    if (error || !this.status) return text;
    const now = new Date();
    const fc = usage.forecast(usage.fromLanes(this.lanes), this.lanes.flatMap((l) => l.items || []), now.getTime());
    const extra = [...usage.lines(fc, now), ...usage.heldLines(this.lanes, now)];
    return extra.length ? `${text}\n${extra.join('\n')}` : text;
  }

  /**
   * (2026-10-26, usage forecast) Before a task or reply is sent: when the forecast says the usage limit
   * will likely hit before a typical task finishes (or a window is at 95% or more), ask Send anyway /
   * Queue for after the reset / Cancel. "Queue for after the reset" puts the queue on a timed hold
   * (`hold on --until`) BEFORE the send, so a live runner cannot take it first; its runner then waits
   * until the reset. Off with claudAutopilot.limitForecastWarning. False = cancelled, send nothing.
   * (2026-10-27, fix LOW) 'held' = send, and this gate put the hold on just now (an add that then fails
   * releases it: _releaseGateHold); a hold that is not the one asked for is said on screen.
   * @param {string | null | undefined} project undefined = this window's project (as cli()), null = the shared queue
   * @param {string | null | undefined} lane
   * @returns {Promise<boolean | 'held'>}
   */
  async _limitGate(project, lane) {
    if (!this.settings.limitForecastWarning) return true;
    const nowMs = Date.now();
    const own = project === undefined || project === this.projectRoot;
    const fc = usage.forecast(usage.fromLanes(this.lanes), own ? this.lanes.flatMap((l) => l.items || []) : [], nowMs);
    const d = usage.warnDecision(fc, nowMs);
    if (!d) return true;
    const buttons = d.holdUntil ? [t(SEND_ANYWAY), t(AFTER_RESET)] : [t(SEND_ANYWAY)];
    const pick = await vscode.window.showWarningMessage(`Sleeping Clanker: ${usage.warningText(d, new Date(nowMs))}`, { modal: true }, ...buttons);
    if (pick === t(SEND_ANYWAY)) {
      this.event(`usage forecast: ${d.window} at ${Math.round(d.utilization * 100)}%; the owner chose "Send anyway"`);
      return true;
    }
    if (pick !== t(AFTER_RESET) || !d.holdUntil) {
      this.event(`usage forecast: ${d.window} at ${Math.round(d.utilization * 100)}%; sending cancelled`);
      return false;
    }
    const before = Date.now();
    const r = await this.cliJson(usage.holdUntilArgs(d.holdUntil), parse.parseHoldResult, undefined, project, lane);
    this.event(`usage forecast: queue held until ${r.hold ? r.until || 'released (no end time)' : d.holdUntil} (the ${d.window} reset`
      + ` ${d.holdUntil}); the task waits for it${r.hold ? '' : ' (the hold did not take)'}`);
    const off = usage.holdMismatch(r, d.holdUntil, new Date());
    if (off) vscode.window.showWarningMessage(`Sleeping Clanker: ${off}`);
    await this.poller.trigger();
    return usage.holdIsNew(r, before) ? 'held' : true;
  }

  /**
   * (2026-10-27, fix LOW) The add that _limitGate put the queue on hold for failed: release that hold, so
   * the queue does not wait until the reset with nothing new in it.
   * @param {string | null | undefined} project @param {string | null | undefined} lane
   */
  async _releaseGateHold(project, lane) {
    try {
      await this.cliJson(argv.holdArgs('off'), parse.parseHoldResult, undefined, project, lane);
      this.event('usage forecast: the add failed, so the hold put on for it is released');
    } catch (e) {
      this.event(`usage forecast: releasing the hold after the failed add failed too: ${e instanceof Error ? e.message : e}`);
    }
  }

  /**
   * (2026-10-27, usage forecast fix round 2 LOW) `send` (the core call that queues the task, subtask,
   * retry or reply) after _limitGate gave `gate`: when it throws and the gate put the hold on just now,
   * that hold is released (_releaseGateHold), as addPrompt does.
   * @template T @param {boolean | 'held'} gate @param {string | null | undefined} project
   * @param {string | null | undefined} lane @param {() => Promise<T>} send @returns {Promise<T>}
   */
  async _afterGate(gate, project, lane, send) {
    try {
      return await send();
    } catch (e) {
      if (gate === 'held') await this._releaseGateHold(project, lane);
      throw e;
    }
  }

  /** The queue names of this window's project (`default` first). */
  laneNames() {
    return this.lanes.length ? this.lanes.map((l) => l.name) : [argv.DEFAULT_LANE];
  }

  /**
   * An item from the last poll (this window's project only; another project's item is read with
   * `show`). No path comparison: `root` is this window's own root string or not (amendment M9).
   * @param {string | null} root @param {string} lane @param {string} id
   */
  findItem(root, lane, id) {
    if (root !== this.projectRoot) return null;
    const l = this.lanes.find((x) => x.name === lane);
    return l ? l.items.find((i) => i.id === id) || null : null;
  }

  /**
   * (2026-10-26, speed) The data dir the CORE resolved for (root, lane) in the last poll (`lanes --json`
   * gives it per queue), so opening a Task view or a live log needs no `where --json` spawn; null when
   * the last poll was not of this project (another project, the shared queue view) or has no such queue.
   * @param {string | null} root @param {string} lane @returns {string | null}
   */
  knownDataDir(root, lane) {
    if (root !== this.projectRoot || this._lanesRoot !== root) return null;
    const l = this.lanes.find((x) => x.name === (lane || argv.DEFAULT_LANE));
    return l && typeof l.dataDir === 'string' && l.dataDir ? l.dataDir : null;
  }

  /** The goal bar: tooltip only on poll changes, text every second (SPEC_GOALS 5.2, SPEC_HOVER 1).
   * @param {import('./lib/parse').Item | null} goal */
  _renderGoal(goal) {
    this.goal = goal;
    if (!goal) {
      this._stopGoalTimer();
      if (this._goalVisible) {
        this.goalItem.hide();
        this._goalVisible = false;
      }
      return;
    }
    const lane = this.lanes.length >= 2 ? goal.lane : null; // (2026-10-15) the queue, in the tooltip
    setIfChanged(this.goalItem, 'tooltip', format.goalBarTooltip(goal, { ...this._ctx(), lane }));
    this._tickGoal();
    if (!this._goalVisible) {
      this.goalItem.show();
      this._goalVisible = true;
    }
    if (!this._goalTimer) {
      // Local timer only: it never calls the CLI (the poller's cycle stays as it is).
      this._goalTimer = setInterval(() => this._tickGoal(), GOAL_TICK_MS);
      if (this._goalTimer && typeof this._goalTimer.unref === 'function') this._goalTimer.unref();
    }
  }

  _tickGoal() {
    if (this.goal) setIfChanged(this.goalItem, 'text', format.goalBarText(this.goal, Date.now()));
  }

  _stopGoalTimer() {
    if (this._goalTimer) clearInterval(this._goalTimer);
    this._goalTimer = null;
  }

  /** @param {string} what @param {() => Promise<void>} fn */
  async guarded(what, fn) {
    try {
      await fn();
    } catch (err) {
      this.reportError(what, err);
    }
  }

  // ---- one-time migration offer (SPEC_PROJECTS 3, amendment M9) --------------------------------
  async offerMigration() {
    if (!this.projectRoot) return;
    // The comparison of cwd and root happens in the core (`--for`), never here.
    const dry = await this.cliJson(argv.migrateArgs({ dryRun: true, forRoot: this.projectRoot }), parse.parseMigrate,
      undefined, null, undefined, QUIET);
    const n = dry.forRoot || 0;
    if (n <= 0) return;
    const notNow = t('Not now'); // (2026-10-27, i18n) buttons translated once, compared as shown
    if (dry.runnerAlive) {
      const stopShared = t('Stop shared runner');
      const pick = await vscode.window.showInformationMessage(
        t('Sleeping Clanker: the shared queue has {0} task(s) for this folder, but a shared runner is running. Stop it first, then reload the window to move them.', n),
        stopShared, notNow);
      if (pick === stopShared) await this.stopRunner(STOP_TIMEOUT_MS, null);
      return;
    }
    const moveBtn = t('Move');
    const pick = await vscode.window.showInformationMessage(
      t('Sleeping Clanker: move {0} task(s) from the shared queue to this project?', n), moveBtn, notNow);
    if (pick !== moveBtn) return;
    const res = await this.cliJson(argv.migrateArgs(), parse.parseMigrate, undefined, null);
    this.log(`${proc.stamp()} migrate-global: moved ${res.moved}; backup ${res.backup}`);
    vscode.window.showInformationMessage(t('Sleeping Clanker: moved {0} task(s) into their project queues.', res.moved));
    await this.poller.trigger();
  }

  // ---- adding tasks -------------------------------------------------------------------------
  /**
   * The permission picker. (2026-10-21, security review B1) `full` is never sorted first (it stays
   * last), `opts.noFull` leaves it out, and picking it needs the modal confirmation (confirmFull).
   * @param {{noFull?: boolean}} [opts]
   */
  async pickProfile(opts = {}) {
    const first = this.safeDefaultProfile();
    const list = opts.noFull ? argv.DEFAULT_PROFILES : argv.PROFILES;
    const items = list.map((p) => ({
      label: p, description: p === 'safe' ? t('{0} (recommended)', t(format.PROFILE_LINES.safe)) // (2026-10-27, i18n)
        : t(format.PROFILE_LINES[/** @type {'safe'} */ (p)]),
    })).sort((a, b) => (a.label === first ? -1 : b.label === first ? 1 : 0));
    const pick = await pickers.showQuickPick(items, { title: t('Sleeping Clanker: permission profile'), ignoreFocusOut: true });
    if (!pick) return null;
    return (await this.confirmFull(pick.label, 'this task')) ? pick.label : null;
  }

  /** (2026-10-21, security review B1) The default profile, never `full` (normalizeSettings already says so). */
  safeDefaultProfile() {
    const p = this.settings.defaultProfile;
    return p && p !== argv.FULL_PROFILE && argv.PROFILES.includes(p) ? p : 'safe';
  }

  /**
   * (2026-10-21, security review B1/L8) A modal confirmation, in the host, before `full` is chosen
   * anywhere (QuickPick, Reply…, Retry, the Task view's dropdown). Any other profile passes.
   * (2026-10-21, v049 tester fix LOW-4) The modal's question, detail and button are in the UI language
   * (FULL_QUESTIONS); the log line keeps the English `what`.
   * @param {string | null | undefined} profile @param {string} what e.g. "this task", "the reply to ab12cd34"
   * @param {string} [id] the task id that `what` names ("the retry of {0}", "the reply to {0}")
   * @returns {Promise<boolean>} false = cancelled: the caller sends nothing
   */
  async confirmFull(profile, what, id) {
    if (profile !== argv.FULL_PROFILE) return true;
    const key = FULL_QUESTIONS[id ? what.split(id).join('{0}') : what];
    const question = key ? t(key, id) : `Sleeping Clanker: FULL ACCESS for ${what}?`;
    const button = t(RUN_FULL);
    const pick = await vscode.window.showWarningMessage(
      question,
      { modal: true, detail: `${t(format.PROFILE_LINES.full)}\n\n${t(FULL_RISKS)}\n\n${t('Claude runs with --dangerously-skip-permissions: no permission checks at all.')}` },
      button);
    const ok = pick === button;
    this.event(`full access for ${what}: ${ok ? 'confirmed' : 'cancelled, nothing sent'}`);
    return ok;
  }

  /**
   * (2026-10-29, F4, SEC-L22 owner "Kysy aina full-tehtävissä") The one condition confirmIdleFull asks on: would a
   * send to this queue need the idle FULL ACCESS confirmation now? Auto-pick and Autorun (lib/taskview.js
   * _idlePause) use it too: a full task's question then waits for the owner instead of being answered.
   * @param {string | null} project @param {string | null | undefined} lane @param {{item?: any}} [opts]
   * @returns {{ask: boolean, last: number | null, limitMs: number | null, idle: string, key: string}}
   */
  idleFullCheck(project, lane, opts = {}) {
    const name = lane || argv.DEFAULT_LANE;
    const l = project === this.projectRoot ? this.lanes.find((x) => x.name === name) : null;
    const items = l ? l.items : opts.item ? [opts.item] : [];
    const idle = this.settings.fullAccessIdleConfirm;
    const key = `${project || ''}\n${name}`;
    const yes = this._idleYes || (this._idleYes = new Map());
    return { ...fullaccess.idleCheck({ items, now: Date.now(), idle, confirmed: yes.get(key) }), idle, key };
  }

  /**
   * (2026-10-26, owner fa388b7b) Before a reply to a full-access task, or a new task that gets full
   * without the picker's own modal: when the owner has not used this queue for longer than
   * claudAutopilot.fullAccessIdleConfirm (lib/fullaccess.js), one modal first. True = go on.
   * @param {string | null} project @param {string | null | undefined} lane
   * @param {{id?: string, item?: any}} [opts] `id`: the reply's task; `item`: its copy (another project's queue)
   * @returns {Promise<boolean>} false = cancelled: the caller sends nothing
   */
  async confirmIdleFull(project, lane, opts = {}) {
    const chk = this.idleFullCheck(project, lane, opts);
    const { idle, key } = chk;
    /** @type {Map<string, number>} a "yes" counts as use at once (the next poll shows the send) */
    const yes = this._idleYes || (this._idleYes = new Map());
    if (!chk.ask) return true;
    const last = chk.last === null ? t('You have not used this queue before.')
      : t('You last used this queue on {0}.', format.localStamp(new Date(chk.last).toISOString()) || '?');
    const question = opts.id ? t('Sleeping Clanker: {0} has FULL ACCESS. {1} Send this reply?', opts.id, last)
      : t('Sleeping Clanker: the new task gets FULL ACCESS. {0} Add it?', last);
    const words = /** @type {Record<string, string>} */ (fullaccess.IDLE_WORDS)[idle] || idle;
    const button = t('Yes, with full access');
    const pick = await vscode.window.showWarningMessage(question, { modal: true,
      detail: `${t(format.PROFILE_LINES.full)}\n\n${t(FULL_RISKS)}\n\n${t('Asked because nothing was added to this queue for longer than {0} (setting claudAutopilot.fullAccessIdleConfirm).', t(words))}` },
      button);
    const ok = pick === button;
    if (ok) yes.set(key, Date.now());
    this.event(`full access after an idle queue (${idle}) for ${opts.id ? `the reply to ${opts.id}` : 'a new task'}: ${ok ? 'confirmed' : 'cancelled, nothing sent'}`);
    return ok;
  }

  /**
   * (2026-10-20, 0.4.5) The preselected permission profile, or null when the picker must be shown.
   * `claudAutopilot.askProfile` off = use `defaultProfile` for every new task without asking.
   * (2026-10-21, security review B1) never `full`. (2026-10-21, owner) unless the owner confirmed it as
   * the default for + (`_updateDefaults`); `opts.noFull` (the fix flow) still never gets it.
   * @param {{noFull?: boolean}} [opts]
   */
  presetProfile(opts = {}) {
    if (this.settings.askProfile) return null;
    // (2026-10-21, owner "Lisää varoituksella") a CONFIRMED full default (normalizeSettings keeps full
    // only with the machine-local marker, checked again here); `noFull` callers get the safe default
    if (!opts.noFull && this.settings.defaultProfile === argv.FULL_PROFILE && this._fullMarker()) return argv.FULL_PROFILE;
    return this.safeDefaultProfile();
  }

  /** (2026-10-21) Was FULL ACCESS confirmed as the default for +? (globalState, never a settings.json) */
  _fullMarker() {
    const gs = this.context && this.context.globalState;
    return !!(gs && gs.get(argv.FULL_DEFAULT_KEY, false) === true);
  }

  /** @param {boolean} on */
  async _setFullMarker(on) {
    const gs = this.context && this.context.globalState;
    if (!gs) return false;
    await gs.update(argv.FULL_DEFAULT_KEY, on ? true : undefined);
    return true;
  }

  /**
   * (2026-10-21) Save one "Defaults for +" choice (a tree row or the "+ Add new task" panel: one source of
   * truth). `full` as the default asks ONE modal confirmation (confirmFull); cancel = nothing changes.
   * @param {Record<string, any>} patch
   */
  async _updateDefaults(patch) {
    if (patch && patch.defaultProfile === argv.FULL_PROFILE) {
      if (!(await this.confirmFull(argv.FULL_PROFILE, 'every new task (the default for +)'))) return;
      await this._updateSettings(patch, { fullConfirmed: true });
      return;
    }
    await this._updateSettings(patch);
  }

  /**
   * (2026-10-21, task model) A task's model: the runner's (''), a known Claude model or any model id.
   * `undefined` = cancelled.
   * @param {string | null} [current] the task's model now, marked `current` (undefined = a new task)
   * @param {{models?: string[], runner?: string}} [opts] (2026-10-21, add panel) another provider's
   *   model ids and what its default is (default: Claude's TASK_MODELS and the runner's model)
   * @returns {Promise<string | undefined>}
   */
  async pickModel(current, opts = {}) {
    // (2026-10-22, v049 tester LOW-15) the first known model, not a second copy of it
    const run = opts.runner !== undefined ? opts.runner : (this.settings.model || argv.TASK_MODELS[0]);
    const models = opts.models || argv.TASK_MODELS;
    /** @type {{label: string, description?: string, model: string | null}[]} */
    const items = [
      { label: t('Runner default'), description: run, model: '' },
      ...models.map((m) => ({ label: m, model: m })),
      { label: t('$(edit) Other model id…'), model: null },
    ];
    if (current !== undefined) {
      for (const i of items) if (i.model === (current || '')) i.description = i.description ? t('{0} · current', i.description) : t('current');
      if (current && !models.includes(current)) items.unshift({ label: current, description: t('current'), model: current });
    }
    const pick = await pickers.showQuickPick(items, { title: t('Sleeping Clanker: model for this task'), ignoreFocusOut: true });
    if (!pick) return undefined;
    if (pick.model !== null) return pick.model;
    const typed = await pickers.showInputBox({
      title: t('Sleeping Clanker: model id'), prompt: t('For example claude-sonnet-5-5, opus or the provider\'s own id'),
      value: current || '', ignoreFocusOut: true,
      validateInput: (v) => (!v.trim() || argv.validModel(v.trim()) ? null
        : t('Letters, digits and . _ : / [ ] -, at most 100 characters, not starting with -')),
    });
    return typed === undefined ? undefined : typed.trim();
  }

  /**
   * (2026-10-21, task model) With `askModel` on, ask; else '' (the runner's model). `undefined` = cancelled.
   * (2026-10-21, add panel) the panel's model (`defaultModel`) wins: it is not asked again.
   * @param {{models?: string[], runner?: string}} [opts] the provider's model list (pickModel)
   */
  async presetModel(opts = {}) {
    if (this.settings.defaultModel) return this.settings.defaultModel;
    return this.settings.askModel ? this.pickModel(undefined, opts) : '';
  }

  // ---- provider, model, effort (2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) -----------------
  /**
   * `providers --json`, parsed down to names, labels, key-present booleans and model ids
   * (parse.parseProviders); cached, `reload` reads it again. null = it could not be read.
   * @param {boolean} [reload]
   * @returns {Promise<import('./lib/parse').Providers | null>}
   */
  async providerList(reload = false) {
    if (reload || !this._providers) {
      const p = this.cliJson(['providers', '--json'], parse.parseProviders, undefined, undefined, undefined, QUIET);
      this._providers = p;
      p.catch(() => { if (this._providers === p) this._providers = null; }); // read again next time
    }
    try {
      return await this._providers;
    } catch (e) {
      this.log(`${proc.stamp()} WARN providers: ${e instanceof Error ? e.message : String(e)}`);
      return null;
    }
  }

  /** (2026-10-21, add panel) Re-render the panel from the settings (no core call). */
  _refreshAddPanel() {
    if (!this.addPanel || !this.addPanel.view) return;
    this.addPanel.refresh(false).catch((e) => this.reportError('add panel', e));
  }

  /**
   * (2026-10-21, add panel) A new task's own provider, model and effort: the panel's choices
   * (defaultProvider/defaultModel/defaultEffort, '' = the runner's); the model is asked when `askModel`
   * is on and none is set. An effort the provider cannot take is left out. `undefined` = cancelled.
   * (2026-10-26, modes) plus the defaultUltracode / defaultAutorun settings (`add --ultracode/--autorun=on`).
   * @param {string} [profile] (2026-10-27) the new task's profile: Ultracode only where argv.ultracodeRefusal allows it
   * @returns {Promise<{provider: string, model: string, effort: string, ultracode: boolean, autorun: boolean} | undefined>}
   */
  async presetRun(profile) {
    const s = this.settings;
    const provider = s.defaultProvider || '';
    let effort = s.defaultEffort || '';
    /** @type {import('./lib/parse').Providers | null} */
    let cat = null;
    if (provider || effort || (s.askModel && !s.defaultModel)) cat = await this.providerList(false);
    const name = provider || (cat ? addpanel.runnerProvider(cat) : '');
    const opts = provider && cat ? { models: addpanel.modelsFor(cat, provider), runner: addpanel.defaultModelOf(cat, provider, s) } : {};
    const model = await this.presetModel(opts);
    if (model === undefined) return undefined;
    const info = name && cat ? addpanel.providerInfo(cat, name) : null;
    if (effort && info && !info.supportsEffort) {
      this.event(`effort ${effort} left out: provider ${name} takes none`);
      effort = '';
    }
    // (2026-10-27, modes fix HIGH) `profile`: the task's; Ultracode is left out (logged) where the core refuses it
    // (2026-10-27, modes fix round 2) or where the add panel / Defaults for + showed it Off and locked (askProfile,
    // a default profile that is not full): a profile picked or passed later never turns it on unseen
    const shownWhy = argv.ultracodeRefusal(s.askProfile ? '' : s.defaultProfile, s.defaultMaxAgents);
    const ucWhy = s.defaultUltracode ? shownWhy || (profile !== undefined ? argv.ultracodeRefusal(profile, s.defaultMaxAgents) : '') : '';
    if (ucWhy) this.event(`ultracode left out: ${ucWhy}`);
    return { provider, model, effort, ultracode: !!s.defaultUltracode && !ucWhy, autorun: !!s.defaultAutorun };
  }

  /**
   * (2026-10-22, v049 tester LOW-3) A shell profile is refused on a provider that puts an API key in the
   * task's environment: the task's own provider, else the runner's (providers.env or
   * $CLAUDE_AUTOPILOT_PROVIDER, as `providers --json` reports it). Thrown (the same error as a task's own
   * key provider gets from argv.addArgs) before any folder or queue is asked, so nothing is added. The
   * core's `add` refuses the same.
   * @param {string} profile @param {string} provider the task's own ('' = the runner's)
   */
  async _refuseKeyProvider(profile, provider) {
    if (argv.NO_SHELL_PROFILES.includes(profile)) return;
    const name = provider || addpanel.runnerProvider(await this.providerList(false));
    const refused = argv.providerRefusal(name, profile);
    if (!refused) return;
    this.event(`add refused: ${refused}`);
    throw new Error(refused);
  }

  /**
   * (2026-10-21, add panel) "Provider, Model, Effort…": the task's own provider, model and effort for its
   * next attempt (`edit ID --provider= --model= --effort=`); status, queue place and session are kept.
   * A new provider also clears a model it does not list and an effort it cannot take.
   * @param {any} arg
   */
  async changeRun(arg) {
    const tg = this._target(arg);
    const item = tg.item;
    if (item.status === 'running') throw new Error(t('{0} is running; pause it first, then change it', item.id));
    const cat = await this.providerList(false);
    const runner = addpanel.runnerProvider(cat);
    const prov = item.provider || runner;
    const info = addpanel.providerInfo(cat, prov);
    const effortOk = !info || info.supportsEffort;
    const runners = t("the runner's");
    // (2026-10-27, modes fix HIGH) Ultracode only with the full profile and subagents: else its row says why
    const ucWhy = argv.ultracodeRefusal(item.profile || 'safe', item.max_agents);
    const rows = [
      { label: t('Provider'), description: item.provider ? addpanel.providerLabel(item.provider) : `${runners} (${addpanel.providerLabel(runner)})`, key: 'provider' },
      { label: t('Model'), description: item.model || runners, key: 'model' },
      { label: t('Effort'), description: !effortOk ? t('{0} takes no effort setting (an Anthropic-model feature).', addpanel.providerLabel(prov)) : (item.effort || runners), key: 'effort' },
      // (2026-10-26, modes) a click switches it; ultracode from the next attempt, autorun at the next question
      { label: t('Ultracode'), description: item.ultracode ? t('On') : ucWhy ? `$(lock) ${t('Off')}` : t('Off'), detail: t(ucWhy || argv.ULTRACODE_TIP), key: 'ultracode' },
      { label: t('Autorun'), description: item.autorun ? t('On') : t('Off'), detail: t(argv.AUTORUN_TIP), key: 'autorun' },
    ].filter((r) => r.key !== 'effort' || effortOk);
    const row = /** @type {any} */ (await pickers.showQuickPick(rows, { title: `Sleeping Clanker: ${t('provider, model and effort of {0}', item.id)}`, ignoreFocusOut: true }));
    if (!row) return;
    /** @type {{provider?: string, model?: string, effort?: string, ultracode?: boolean, autorun?: boolean}} */
    const change = {};
    if (row.key === 'ultracode' && !item.ultracode && ucWhy) {
      vscode.window.showWarningMessage(`Sleeping Clanker: ${t(ucWhy)}`); // (2026-10-27) the core would refuse it
      return;
    } else if (row.key === 'ultracode' || row.key === 'autorun') {
      change[/** @type {'autorun'} */ (row.key)] = !item[/** @type {'autorun'} */ (row.key)];
    } else if (row.key === 'provider') {
      const c = cat || addpanel.FALLBACK;
      const picks = [{ label: `${runners} (${addpanel.providerLabel(runner)})`, value: '', description: !item.provider ? t('current') : '' },
        ...c.providers.filter((p) => p.ready || p.name === item.provider).map((p) => ({
          label: addpanel.providerLabel(p.name), value: p.name, description: p.name === item.provider ? t('current') : '', detail: p.label,
        }))];
      const pick = /** @type {any} */ (await pickers.showQuickPick(picks, { title: `Sleeping Clanker: ${t('Provider')}`, ignoreFocusOut: true }));
      if (!pick) return;
      const next = pick.value || runner;
      const refused = argv.providerRefusal(pick.value, item.profile || 'safe');
      if (refused) throw new Error(`${item.id}: ${refused}`);
      change.provider = pick.value;
      if (item.model && next !== prov && !addpanel.modelsFor(c, next).includes(item.model)) change.model = '';
      const nextInfo = addpanel.providerInfo(c, next);
      if (item.effort && nextInfo && !nextInfo.supportsEffort) change.effort = '';
    } else if (row.key === 'model') {
      const model = await this.pickModel(item.model || null, cat && (item.provider || !addpanel.CLAUDE_FAMILY.includes(prov))
        ? { models: addpanel.modelsFor(cat, prov), runner: addpanel.defaultModelOf(cat, prov, this.settings) } : {});
      if (model === undefined) return;
      change.model = model;
    } else {
      const picks = [{ label: runners, value: '' }, ...argv.EFFORTS.map((e) => ({ label: e, value: e }))]
        .map((p) => ({ ...p, description: (item.effort || '') === p.value ? t('current') : '' }));
      const pick = /** @type {any} */ (await pickers.showQuickPick(picks, { title: `Sleeping Clanker: ${t('Effort')}`, ignoreFocusOut: true }));
      if (!pick) return;
      change.effort = pick.value;
    }
    const r = await this.cliJson(argv.runEditArgs(item.id, change), parse.parseEdit, undefined, tg.project, tg.laneArg);
    const what = Object.entries(change).map(([k, v]) => `${k}=${typeof v === 'boolean' ? (v ? 'on' : 'off') : v || "(runner's)"}`).join(', ');
    this.event(r.changed.length ? `${item.id}: ${what}` : `no change to ${item.id} (${what})`);
    await this.poller.trigger();
    vscode.window.showInformationMessage(r.changed.length
      ? `Sleeping Clanker: ${t('{0} uses {1} from its next attempt.', item.id, what)}`
      : `Sleeping Clanker: ${t('{0} already uses {1}.', item.id, what)}`);
  }

  /**
   * (2026-10-21, task model) "Change Model…": `edit ID --model=<id>` (or '' = the runner's model) for the
   * next attempt; status, queue place and session are kept.
   * @param {any} arg
   */
  async changeModel(arg) {
    const tg = this._target(arg);
    if (tg.item.status === 'running') throw new Error(t('{0} is running; pause it first, then change its model', tg.item.id));
    const model = await this.pickModel(tg.item.model || null);
    if (model === undefined) return;
    const r = await this.cliJson(argv.modelArgs(tg.item.id, model), parse.parseEdit, undefined, tg.project, tg.laneArg);
    const what = model || 'the runner\'s model';
    this.event(r.changed.length ? `model of ${tg.item.id} -> ${what}` : `no change to the model of ${tg.item.id}`);
    await this.poller.trigger();
    const shownWhat = model || t('the runner\'s model'); // (2026-10-27, i18n) the event line above stays English
    vscode.window.showInformationMessage(r.changed.length ? t('Sleeping Clanker: {0} runs on {1} from its next attempt.', tg.item.id, shownWhat)
      : t('Sleeping Clanker: {0} already runs on {1}.', tg.item.id, shownWhat));
  }

  /** The active editor's workspace folder; with several folders, ask (active one first). */
  async pickCwd() {
    const folders = (vscode.workspace.workspaceFolders || []).filter((f) => f.uri.scheme === 'file');
    if (folders.length === 0) throw new Error(t('open a local folder first: a task runs inside a workspace folder'));
    const editor = vscode.window.activeTextEditor;
    const active = editor ? vscode.workspace.getWorkspaceFolder(editor.document.uri) : undefined;
    if (folders.length === 1) return folders[0].uri.fsPath;
    const ordered = active ? [active, ...folders.filter((f) => f !== active)] : folders;
    const pick = await pickers.showQuickPick(ordered.map((f) => ({
      label: f.name, description: f === active ? t('active editor') : '', detail: f.uri.fsPath,
    })), { title: t('Sleeping Clanker: run the task in which folder?'), ignoreFocusOut: true });
    return pick ? /** @type {string} */ (pick.detail) : null;
  }

  /** Goal caps; Enter accepts the defaults from the settings. Null = cancelled. */
  async pickGoalCaps() {
    // (2026-10-20, 0.4.5) preselected: the settings' caps, nothing is asked
    if (!this.settings.askGoalCaps) {
      return { maxRounds: this.settings.goalMaxRounds, maxHours: this.settings.goalMaxHours };
    }
    return this.askGoalCaps();
  }

  /** The two cap questions, prefilled from the settings. Null = cancelled. */
  async askGoalCaps() {
    const rounds = await pickers.showInputBox({
      title: t('Sleeping Clanker: goal round cap'), prompt: t('Maximum rounds (-1 = unlimited, until stopped; Enter = default)'),
      value: String(this.settings.goalMaxRounds), ignoreFocusOut: true,
      validateInput: (v) => (/^(\d+|-1)$/.test(v.trim()) && argv.validRounds(Number(v)) ? null
        : t('A whole number from 1 to {0}, or -1 for unlimited', argv.MAX_ROUNDS_LIMIT)),
    });
    if (rounds === undefined) return null;
    const hrs = await pickers.showInputBox({
      title: t('Sleeping Clanker: goal time cap'), prompt: t('Maximum hours (Enter = default)'),
      value: String(this.settings.goalMaxHours), ignoreFocusOut: true,
      validateInput: (v) => (v.trim() && argv.validHours(Number(v)) ? null : t('A number > 0 and <= {0}', argv.MAX_HOURS_LIMIT)),
    });
    if (hrs === undefined) return null;
    return { maxRounds: Number(rounds.trim()), maxHours: Number(hrs.trim()) };
  }

  /**
   * Ask for a NEW queue name (validated as the core does). Null = cancelled.
   * @returns {Promise<string | null>}
   */
  async askLaneName() {
    const existing = this.laneNames();
    const name = await pickers.showInputBox({
      title: t('Sleeping Clanker: new queue'), prompt: t('A name for the queue (letters, digits, "_" or "-"). It has its own runner.'),
      ignoreFocusOut: true, validateInput: (v) => argv.validateLaneName(v, existing),
    });
    if (name === undefined) return null;
    const err = argv.validateLaneName(name, existing);
    if (err) throw new Error(err);
    return name.trim();
  }

  /**
   * (2026-10-15, SPEC_V03 2.3) The queue for a new task: with one queue nothing is asked (null = no
   * --lane, the default); with several, a QuickPick (last used first, then `$(add) New queue…`).
   * `undefined` = cancelled.
   * @returns {Promise<string | null | undefined>}
   */
  async pickLane() {
    const names = this.laneNames();
    if (names.length < 2) return null;
    const ws = this.context.workspaceState;
    // (2026-10-20, 0.4.5) a preselected queue wins, as long as it still exists; else ask as before
    const preset = names.find((n) => n.toLowerCase() === String(this.settings.defaultLane || '').toLowerCase());
    if (preset) {
      if (ws) await ws.update(LAST_LANE_KEY, preset);
      return preset;
    }
    const last = ws ? ws.get(LAST_LANE_KEY, null) : null;
    const pick = await pickers.showQuickPick(view.laneChoices(names, last), {
      title: t('Sleeping Clanker: add to which queue?'), ignoreFocusOut: true,
    });
    if (!pick) return undefined;
    const lane = pick.lane === null ? await this.askLaneName() : pick.lane;
    if (lane === null) return undefined;
    if (ws) await ws.update(LAST_LANE_KEY, lane);
    return lane;
  }

  /** @param {string | null | undefined} preset the lane from a lane node or the New Queue flow */
  async _laneFor(preset) {
    return preset === undefined ? this.pickLane() : preset;
  }

  /**
   * @param {string} prompt
   * (2026-10-21, split tasks) `opts.split`: a prompt that lists several tasks (lib/split.js) is queued
   * as one item per task, with the same profile/folder/queue/model, when `splitTasks` is on.
   * @param {{profile?: string, goal?: boolean, attachments?: string[], lane?: string | null, split?: boolean}} [opts]
   */
  async addPrompt(prompt, opts = {}) {
    const once = addpanel.currentOnce(); // (2026-10-26) the add panel's options for this one task, else null
    const parts = opts.split && !opts.goal && this.settings.splitTasks ? await this._askSplit(prompt) : [prompt];
    let profile = opts.profile || this.presetProfile() || await this.pickProfile();
    if (!profile || (opts.profile && !(await this.confirmFull(opts.profile, 'this task')))) return;
    let demotedFull = false;
    if (parts.length > 1 && profile === argv.FULL_PROFILE) {
      // (2026-10-21, v049 tester fix MED-1, SPEC_V049_ADD_PANEL 1) `full` is never inherited: split parts
      // get the safe default, even when full was picked or is the confirmed default for +
      profile = this.safeDefaultProfile();
      demotedFull = true;
      this.event(`split: full access is never inherited; the ${parts.length} parts get profile ${profile}`);
    } else if (!opts.profile && profile === argv.FULL_PROFILE && !this.settings.askProfile) {
      this.event('full access for this task: the confirmed default for +'); // (2026-10-21) the audit line
    }
    await this._refuseKeyProvider(profile, this.settings.defaultProvider || ''); // (v049 LOW-3)
    const cwd = (once && once.cwd) || await this.pickCwd();
    if (!cwd) return;
    const lane = await this._laneFor(opts.lane);
    if (lane === undefined) return;
    // (2026-10-26, owner fa388b7b) the confirmed full default showed no modal now: ask when the queue sat idle
    if (!opts.profile && profile === argv.FULL_PROFILE && !this.settings.askProfile
      && !(await this.confirmIdleFull(this.projectRoot, lane))) return;
    /** @type {{maxRounds: number, maxHours: number} | null} */
    let caps = null;
    if (opts.goal) {
      caps = addpanel.onceCaps(once, this.settings) || await this.pickGoalCaps();
      if (!caps) return;
    }
    // (2026-10-21, add panel) the panel's provider/model/effort (the model may be asked)
    const run = await this.presetRun(profile);
    if (!run) return;
    const gate = await this._limitGate(undefined, lane); // (2026-10-26, usage forecast)
    if (!gate) return;
    /** @type {string[]} */
    const ids = [];
    try {
      for (const [i, part] of parts.entries()) {
        // (2026-10-27, U4, SPEC_V05_PARALLEL 5.1/8.14) split parts are chained: part 2..n waits for the part before it
        // (`--after <its id>` from add --json); the first one waits for the task added before it only with
        // claudAutopilot.newTaskStart afterPrevious (tasks at once > 1). A previous part whose add printed no
        // usable id is still chained: --after-previous (the core resolves the last unfinished task = that part)
        const prev = i > 0 ? ids[i - 1] : null;
        const order = typeof prev === 'string' && argv.ID_RE.test(prev) ? { after: prev }
          : i > 0 || this._afterPrevious() ? { afterPrevious: true } : {};
        const built = argv.addArgs({
          ...order,
          prompt: part, cwd, profile, goal: !!opts.goal, maxRounds: caps ? caps.maxRounds : null,
          maxHours: caps ? caps.maxHours : null, attachments: opts.attachments || [], model: run.model,
          // (2026-10-21, subagents) the `defaultMaxAgents` setting; 'default' adds no flag
          maxAgents: this.settings.defaultMaxAgents,
          provider: run.provider, effort: run.effort, // (2026-10-21, add panel)
          ultracode: run.ultracode, autorun: run.autorun, // (2026-10-26, modes)
          ...addpanel.onceTask(once, !!opts.goal, opts.attachments), // (2026-10-26) {} = nothing set
        }, (text) => proc.writeTempPrompt(text));
        try {
          ids.push(await this.cliJson(built.args, parse.parseAdd, built.display, undefined, lane));
        } finally {
          if (built.tempFile) this._removeTemp(built.tempFile);
        }
      }
    } catch (e) {
      // the parts queued before the failure stay queued; say which, so none is a surprise
      if (ids.length) this.event(`split: queued ${ids.join(', ')} of ${parts.length} tasks before the error`);
      else if (gate === 'held') await this._releaseGateHold(undefined, lane); // (2026-10-27, fix LOW)
      throw e;
    }
    if (parts.length > 1) {
      this.event(`split one prompt into ${parts.length} tasks (claudAutopilot.splitTasks)`);
      // (2026-10-22, v049 re-test LOW-T3/R2) said on screen once the parts are queued: the owner may have
      // just confirmed FULL ACCESS
      if (demotedFull) {
        vscode.window.showInformationMessage(t('Sleeping Clanker: full access is never given to split tasks, so the {0} tasks were added with profile {1}.', String(ids.length), profile));
      }
      await this._queued(ids.join(', '), `${parts.length} tasks`, lane);
      return;
    }
    await this._queued(ids[0], opts.goal ? 'goal' : 'task', lane);
  }

  /** (2026-10-27, U4, SPEC_V05_PARALLEL 8.2) A new task waits for the one added before it (newTaskStart, tasks at once > 1). */
  _afterPrevious() {
    return argv.tasksAtOnceOf(this.settings) > 1 && this.settings.newTaskStart === 'afterPrevious';
  }

  /**
   * (2026-10-21, tester round 2 M-4) Never split silently: when lib/split.js finds several tasks, a
   * modal lists the proposed titles and asks Split / Keep as one. Closing it keeps the prompt as one task.
   * @param {string} prompt @returns {Promise<string[]>}
   */
  async _askSplit(prompt) {
    const found = split.proposeSplit(prompt);
    if (!found) return [prompt];
    const n = found.parts.length;
    // (2026-10-27, i18n) the buttons translated once and compared as shown
    const yes = t(SPLIT_YES);
    const no = t(SPLIT_NO);
    const pick = await vscode.window.showInformationMessage(
      t('Sleeping Clanker: this prompt looks like {0} tasks. Split it into {0} tasks?', n),
      { modal: true, detail: `${found.titles.map((title, k) => `${k + 1}. ${title}`).join('\n')}\n\n${t('{0} queues the whole prompt as one task.', no)}` },
      yes, no);
    if (pick !== yes) {
      this.event(`split: kept as one task (${n} parts proposed)`);
      return [prompt];
    }
    return found.parts;
  }

  /** @param {string} file @param {string | null} tempFile @param {string | null} [presetLane] */
  async addFile(file, tempFile, presetLane) {
    try {
      const profile = this.presetProfile() || await this.pickProfile();
      if (!profile) return;
      if (profile === argv.FULL_PROFILE && !this.settings.askProfile) {
        this.event('full access for this task: the confirmed default for +'); // (2026-10-21) the audit line
      }
      await this._refuseKeyProvider(profile, this.settings.defaultProvider || ''); // (v049 LOW-3)
      const once = addpanel.currentOnce(); // (2026-10-26) the add panel's options for this one task
      const cwd = (once && once.cwd) || await this.pickCwd();
      if (!cwd) return;
      const lane = await this._laneFor(presetLane);
      if (lane === undefined) return;
      // (2026-10-27, S11 review 4) as addPrompt: the confirmed full default showed no modal now, so ask when the
      // queue sat idle (claudAutopilot.fullAccessIdleConfirm)
      if (profile === argv.FULL_PROFILE && !this.settings.askProfile
        && !(await this.confirmIdleFull(this.projectRoot, lane))) return;
      const run = await this.presetRun(profile); // (2026-10-21, add panel)
      if (!run) return;
      const gate = await this._limitGate(undefined, lane); // (2026-10-27, usage forecast fix LOW)
      if (!gate) return;
      const id = await this._afterGate(gate, undefined, lane, // (2026-10-27, fix round 2 LOW)
        () => this.cliJson(argv.addFileArgs({ file, cwd, profile, ...run, ...addpanel.onceTask(once, false),
          ...(this._afterPrevious() ? { afterPrevious: true } : {}) }), parse.parseAdd, undefined, undefined, lane)); // (2026-10-27, U4)
      await this._queued(id, 'task', lane);
    } finally {
      if (tempFile) this._removeTemp(tempFile);
    }
  }

  /** @param {string} file */
  _removeTemp(file) {
    const err = proc.removeTempPrompt(file);
    if (err) this.reportError('temp file cleanup', err);
  }

  /** @param {string} id @param {string} what @param {string | null} [lane] */
  async _queued(id, what, lane = null) {
    addpanel.onceQueued(); // (2026-10-26) the add panel's options for this task go back to the defaults
    this.event(`queued ${what} ${id}${lane ? ` (queue ${lane})` : ''}`);
    await this.poller.trigger();
    // (2026-10-27, i18n) the toast in the UI language (the event line above stays English); `what` is
    // 'task', 'goal', 'subtask' or 'N tasks'
    const queue = lane && lane !== argv.DEFAULT_LANE ? format.laneLabel(lane) : '';
    const many = /^(\d+) tasks$/.exec(what);
    const kind = many ? t('{0} tasks', many[1]) : t(what);
    await this._offerRunner(queue ? t('Sleeping Clanker: queued {0} {1} in queue {2}.', kind, id, queue)
      : t('Sleeping Clanker: queued {0} {1}.', kind, id), this.projectRoot, lane);
  }

  /**
   * (2026-10-17, 0.3.3) After a queue/reply: a live runner = just the message; else with
   * autoStartRunner on, start that queue's runner without asking; else offer `Start Runner` and
   * `Always start automatically` (sets autoStartRunner in the user settings, then starts it).
   * @param {string} msg @param {string | null} project @param {string | null} lane
   */
  async _offerRunner(msg, project, lane, autoStart = false) {
    // (2026-10-27, modes fix round 2) `autoStart` = an Autorun answer: the event log only, no toast per answer
    const say = (/** @type {string} */ m) => (autoStart ? this.event(m.replace(/^Sleeping Clanker: /, '')) : vscode.window.showInformationMessage(m));
    if (this.laneAlive(project, lane)) {
      say(msg);
      return;
    }
    // (2026-10-21, v049 dontask_hold) a held queue: no runner, and no Start button (the Queue view's
    // "Release hold", or Start Runner with its "start anyway?" question, is the owner's call)
    // (2026-10-27, integrate: i18n) an Autorun answer's lines go to the event log only: English, as every log line
    const held = autoStart ? ' The queue is on hold, so no runner was started.' : ` ${t('The queue is on hold, so no runner was started.')}`;
    if (this.laneHeld(project, lane)) {
      this.event(`auto-start: ${lane ? `queue ${lane}` : 'the main queue'} is on hold; no runner started`);
      say(`${msg}${held}`);
      return;
    }
    if (this.settings.autoStartRunner || autoStart) {
      // (2026-10-27, integrate: modes review LOW-5) the log names why: the setting, or an Autorun answer
      this.event(`auto-start: no runner for ${lane ? `queue ${lane}` : 'the main queue'}; starting it (${
        this.settings.autoStartRunner ? 'claudAutopilot.autoStartRunner' : 'Autorun answer'})`);
      let started = false;
      let onHold = false;
      await this.guarded('auto-start runner', async () => {
        const r = await this.startRunner(project, undefined, lane, { auto: true });
        onHold = r === 'held';
        started = !onHold;
      });
      say(onHold ? `${msg}${held}` : started ? `${msg} ${autoStart ? 'Runner started automatically.' : t('Runner started automatically.')}` : msg);
      return;
    }
    // (2026-10-27, i18n) the buttons translated once and compared as shown
    const startBtn = t(START_RUNNER);
    const alwaysBtn = t(ALWAYS_START);
    const pick = await vscode.window.showInformationMessage(msg, startBtn, alwaysBtn);
    if (pick === alwaysBtn) {
      await this.guarded('turn on autoStartRunner', async () => {
        await vscode.workspace.getConfiguration(SECTION).update('autoStartRunner', true, vscode.ConfigurationTarget.Global);
        this.settings = { ...this.settings, autoStartRunner: true }; // before the change event arrives
        this.event('claudAutopilot.autoStartRunner turned on (user settings)');
      });
    }
    if (pick === startBtn || pick === alwaysBtn) {
      await this.guarded('start runner', () => this.startRunner(project, undefined, lane));
    }
  }

  /** @param {string} title @param {string} prompt */
  async _askPrompt(title, prompt) {
    const text = await pickers.showInputBox({
      title, prompt, ignoreFocusOut: true, validateInput: (v) => (v.trim() ? null : t('The prompt is empty')),
    });
    return text === undefined ? null : text;
  }

  /** @param {string | null} [lane] preset queue (a lane node's + button); undefined = ask when needed */
  async cmdAddTask(lane) {
    const prompt = await this._askPrompt(t('Sleeping Clanker: new task'), t('Prompt for Claude Code'));
    if (prompt !== null) await this.addPrompt(prompt, { lane, split: true });
  }

  /** @param {string[]} [attachments] @param {string | null} [lane] */
  async cmdAddGoal(attachments = [], lane = undefined) {
    const prompt = await this._askPrompt(t('Sleeping Clanker: new goal'), t('The goal Claude keeps pursuing, round after round'));
    if (prompt !== null) await this.addPrompt(prompt, { goal: true, attachments, lane });
  }

  /** @param {string | null} [lane] */
  async cmdAddPlan(lane) {
    const prompt = await this._askPrompt(t('Sleeping Clanker: plan-mode task'), t('What should Claude plan? (it changes nothing)'));
    if (prompt !== null) await this.addPrompt(prompt, { profile: 'plan', lane, split: true });
  }

  /**
   * Files and folders: from the Explorer (uri, uris) or an open dialog; then Task or Goal, then the
   * prompt. Only local files (review L10).
   * @param {any} [uri] @param {any[]} [uris] @param {string | null} [lane]
   */
  async cmdAddWithFiles(uri, uris, lane = undefined) {
    /** @type {any[]} */
    let picked = Array.isArray(uris) && uris.length ? uris : uri && uri.fsPath ? [uri] : [];
    if (!picked.length) {
      // (2026-10-21, attach fix) on Windows and Linux an open dialog cannot pick files and folders at
      // once: with both on it is a folder picker that hides every file. There, ask which one first.
      let pickFiles = true;
      let pickFolders = true;
      if (process.platform !== 'darwin') {
        const which = /** @type {any} */ (await vscode.window.showQuickPick([
          { label: t('$(file) Files'), id: 'files' }, { label: t('$(folder) Folders'), id: 'folders' },
        ], { title: t('Sleeping Clanker: attach files or folders?') }));
        if (!which) return;
        pickFiles = which.id === 'files';
        pickFolders = !pickFiles;
      }
      const dialog = await vscode.window.showOpenDialog({
        canSelectFiles: pickFiles, canSelectFolders: pickFolders, canSelectMany: true,
        title: t('Sleeping Clanker: attach files or folders'), openLabel: t('Attach'),
      });
      if (!dialog || !dialog.length) return;
      picked = dialog;
    }
    const nonLocal = picked.filter((u) => u.scheme !== 'file');
    if (nonLocal.length) {
      vscode.window.showWarningMessage(t('Sleeping Clanker: only local files can be attached ({0} skipped).', nonLocal.length));
    }
    const files = picked.filter((u) => u.scheme === 'file').map((u) => /** @type {string} */ (u.fsPath));
    if (!files.length) return;
    const kind = await pickers.showQuickPick([
      { label: t('$(checklist) Task'), id: 'task' }, { label: t('$(target) Goal'), id: 'goal' },
    ], { title: t('Sleeping Clanker: {0} attachment(s): Task or Goal?', files.length), ignoreFocusOut: true });
    if (!kind) return;
    if (kind.id === 'goal') {
      await this.cmdAddGoal(files, lane);
      return;
    }
    const prompt = await this._askPrompt(t('Sleeping Clanker: new task with attachments'), t('Prompt for Claude Code'));
    if (prompt !== null) await this.addPrompt(prompt, { attachments: files, lane, split: true });
  }

  /**
   * The "+ Add" menu (SPEC_GOALS 5.1). (2026-10-15) `lane` presets the queue (a lane node's + button
   * or New Queue…); the menu's "New queue…" entry starts the New Queue flow.
   * @param {string | null} [lane]
   */
  async cmdAdd(lane = undefined) {
    // (2026-10-20, 0.4.5) a preselected kind skips this menu: + goes straight to that flow
    const preset = this.settings.defaultKind;
    if (preset && preset !== 'ask') {
      await this._addKind(preset, lane);
      return;
    }
    const editor = vscode.window.activeTextEditor;
    const hasSelection = !!editor && editor.selections.some((s) => !s.isEmpty);
    const sep = vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1;
    const entries = view.addMenuItems({ hasSelection, hasFile: !!editor })
      .filter((e) => !(lane !== undefined && e.id === 'lane'))
      .filter((e, i, all) => !(e.separator && (i === all.length - 1 || all[i + 1].separator)))
      .map((e) => (e.separator ? { label: e.label, kind: sep } : { label: e.label, description: e.description, id: e.id }));
    const title = lane ? t('Sleeping Clanker: Add to queue {0}…', format.laneLabel(lane)) : t('Sleeping Clanker: Add…'); // (2026-10-27, i18n)
    const pick = /** @type {any} */ (await vscode.window.showQuickPick(entries, { title }));
    if (!pick || !pick.id) return;
    await this._addKind(pick.id, lane);
  }

  /** One Add-menu entry (also the preselected kind, 0.4.5). @param {string} id @param {string | null} [lane] */
  async _addKind(id, lane = undefined) {
    if (id === 'task') await this.cmdAddTask(lane);
    else if (id === 'goal') await this.cmdAddGoal([], lane);
    else if (id === 'files') await this.cmdAddWithFiles(undefined, undefined, lane);
    else if (id === 'plan') await this.cmdAddPlan(lane);
    else if (id === 'selection') await this.cmdAddSelection(lane);
    else if (id === 'file') await this.cmdAddFile(lane);
    else if (id === 'lane') await this.newLane();
  }

  // ---- the + defaults (2026-10-20, 0.4.5, SPEC_V045_DEFAULTS) -----------------------------------
  /**
   * One row's picker: the dropdown behind a "Defaults for +" row (a click on the row, or the
   * Defaults menu). An unknown key is a no-op.
   * @param {string | {key?: string}} arg the row's key, or the row node (the context menu passes it)
   */
  async setAddDefault(arg) {
    const key = typeof arg === 'string' ? arg : (arg && arg.key) || '';
    const d = this.addDefaults();
    const ctx = this._defaultsCtx();
    const choices = view.defaultChoices(key, d, ctx);
    if (!choices.length) return;
    const row = view.defaultsRows(d, ctx).find((r) => r.key === key);
    const pick = /** @type {any} */ (await pickers.showQuickPick(choices, {
      title: t('Sleeping Clanker: {0} for every +', row ? row.label : key), ignoreFocusOut: true,
    }));
    if (!pick) return;
    if (pick.edit) {
      const caps = await this.askGoalCaps();
      if (!caps) return;
      await this._updateSettings({ goalMaxRounds: caps.maxRounds, goalMaxHours: caps.maxHours, askGoalCaps: false });
      return;
    }
    await this._updateDefaults(pick.patch || {}); // (2026-10-21) full asks its confirmation there
  }

  /** The whole section as a QuickPick: the Command Palette and the view welcome use it. */
  async addDefaultsMenu() {
    const d = this.addDefaults();
    const ctx = this._defaultsCtx();
    const rows = view.defaultsRows(d, ctx);
    const items = [
      ...rows.map((r) => ({ label: r.label, description: r.value, detail: r.hint, id: r.key })),
      { label: `$(discard) ${t('Ask every time again')}`, description: t('clear every preselection'), detail: '', id: 'reset' },
      this.settings.showAddDefaults
        ? { label: `$(eye-closed) ${t('Hide these rows in the view')}`, description: '', detail: '', id: 'hide' }
        : { label: `$(eye) ${t('Show these rows in the view')}`, description: '', detail: '', id: 'show' },
    ];
    const pick = await pickers.showQuickPick(items, { title: `Sleeping Clanker: ${t('Defaults for +')}`, ignoreFocusOut: true });
    if (!pick) return;
    if (pick.id === 'reset') {
      // (2026-10-27, integrate: modes review LOW-4) "clear every preselection" clears Ultracode and Autorun too
      await this._updateSettings({ defaultKind: 'ask', askProfile: true, defaultLane: '', askGoalCaps: true,
        defaultUltracode: false, defaultAutorun: false });
    } else if (pick.id === 'hide') await this._updateSettings({ showAddDefaults: false });
    // (2026-10-22, v049 tester LOW-15) no "they appear once the queue has a task" message: _footer
    // shows the section in an empty tree too
    else if (pick.id === 'show') await this._updateSettings({ showAddDefaults: true });
    else await this.setAddDefault(pick.id);
  }

  /** The section's context menu: hide the rows; the Command Palette still changes the defaults. */
  async hideAddDefaults() {
    await this._updateSettings({ showAddDefaults: false });
    vscode.window.showInformationMessage(
      t('Sleeping Clanker: the defaults rows are hidden. "Sleeping Clanker: Defaults for +…" (Command Palette) still changes them.'));
  }

  // (2026-10-26, owner "no easy way to close new task window") the New Task panel's title-bar X: the
  // view's `when` reads the showAddPanel setting, so turning it off removes the panel until shown again
  async hideAddPanel() {
    await this._updateSettings({ showAddPanel: false });
    vscode.window.showInformationMessage(
      t('Sleeping Clanker: the New Task panel is closed. "Show the New Task panel" in the Queue view ... menu brings it back.'));
  }

  async showAddPanel() {
    await this._updateSettings({ showAddPanel: true });
    await vscode.commands.executeCommand(`${addpanel.VIEW_ID}.focus`);
  }

  // ---- (2026-10-21) the Settings button and the UI language ------------------------------------
  /**
   * The Queue view's gear: the GLOBAL settings. The UI language, the + defaults, the runner and queue
   * switches (2026-10-21; a pick flips the user setting at once), and every setting of the extension.
   * The chat box's own settings are "⚙ Chat settings" in the New Task panel's Other settings (TaskViews._chatSettings).
   */
  async settingsMenu() {
    const s = /** @type {Record<string, any>} */ (this.settings);
    const onOff = (/** @type {boolean} */ on) => (on ? t('On') : t('Off'));
    /** @param {string} icon @param {string} label @param {string} key @param {string} detail */
    const toggle = (icon, label, key, detail) => ({ label: `$(${icon}) ${t(label)}`, description: onOff(!!s[key]), detail: t(detail), id: 'toggle', key });
    const items = [
      { label: `$(globe) ${t('Language')}`, description: this._languageDescription(),
        detail: t('The language of the Queue view, the status bar, the hovers, the New Task panel and the Task view.'), id: 'language', key: '' },
      { label: `$(settings-gear) ${t('Defaults for +…')}`, description: '',
        detail: t('What + adds, the permission profile, the queue and the goal caps.'), id: 'defaults', key: '' },
      toggle('play', 'Start runners automatically', 'autoStartRunner',
        'When VS Code starts, and after you queue a task or send a reply to a queue whose runner is not running.'),
      toggle('list-ordered', 'Split a prompt that lists several tasks', 'splitTasks',
        'One queued task per list item, in the same queue; they run one after another.'),
      toggle('hubot', 'Ask the model for each new task', 'askModel',
        "Off: every new task uses the runner's model."),
      toggle('list-tree', 'Group the queue by status', 'groupByStatus',
        'Questions, Running, Not done yet, Errors, Not completed and Completed. Off: one flat list.'),
      // (2026-10-26, owner "add ADHD mode in other settings too") the same switch as the Task view's
      // View dropdown and its ⚙: one choice for every chat (globalState, TaskViews.setAdhd)
      { label: `$(eye) ${t('ADHD mode')}`, description: onOff(this.taskViews.adhdOn()),
        detail: t('Only the questions, the answers and the status; the steps in between fold away. Every Task view.'),
        id: 'adhd', key: '' },
      // (2026-10-21, subagents, SPEC_V049_AGENTS) the two limiters: runners at once (here) and the
      // per-task subagent limit (the dropdown in each Task view's bottom bar)
      { label: `$(server-process) ${t('Runners at once…')}`, description: this.runnerLimitLabel(),
        detail: t('How many queues this window may run at the same time. A queue over the limit waits '
          + 'for a free slot.'), id: 'runners', key: '' },
      { label: `$(organization) ${t('Subagents for new tasks…')}`, description: this.agentsDefaultLabel(),
        detail: t('How many subagents a NEW task may start. Each task keeps its own limit in the '
          + 'dropdown at the bottom of its Task view.'), id: 'agents', key: '' },
      { label: `$(gear) ${t('All settings…')}`, description: '',
        detail: t('Every Sleeping Clanker setting in the VS Code Settings editor.'), id: 'all', key: '' },
    ];
    const pick = await pickers.showQuickPick(items, {
      title: `Sleeping Clanker: ${t('Global settings')}`, ignoreFocusOut: true,
      placeHolder: t('The chat box settings are "⚙ Chat settings" under Other settings in the New Task panel.'),
    });
    if (!pick) return;
    if (pick.id === 'language') await this.languageMenu();
    else if (pick.id === 'defaults') await this.addDefaultsMenu();
    else if (pick.id === 'toggle') await this._updateSettings({ [pick.key]: !s[pick.key] });
    else if (pick.id === 'runners') await this.pickRunnerLimit();
    else if (pick.id === 'agents') await this.pickAgentsDefault();
    else if (pick.id === 'adhd') await this.toggleAdhd();
    else await vscode.commands.executeCommand('workbench.action.openSettings', SETTINGS_QUERY);
  }

  /** (2026-10-26) ADHD mode on or off for every Task view (Global settings, Queue view ..., Command Palette). */
  async toggleAdhd() {
    const on = !this.taskViews.adhdOn();
    await this.taskViews.setAdhd(on, null);
    if (vscode.window.setStatusBarMessage) vscode.window.setStatusBarMessage(`Sleeping Clanker: ${t('ADHD mode')} ${on ? t('On') : t('Off')}`, 3000);
  }

  // ---- (2026-10-21, subagents, SPEC_V049_AGENTS) the two limiters -----------------------------
  /** How many runners this window may have at once; 0 = no limit. */
  runnerLimit() {
    const n = this.settings.maxParallelRunners;
    return Number.isInteger(n) && n > 0 ? n : 0;
  }

  /** "No limit" or "3 at once", for a menu row. */
  runnerLimitLabel() {
    const n = this.runnerLimit();
    return n ? t('{0} at once', n) : t('No limit');
  }

  /** The `defaultMaxAgents` setting as a menu row reads it. */
  agentsDefaultLabel() {
    const v = this.settings.defaultMaxAgents || 'default';
    const hit = argv.AGENT_LIMITS.find((a) => a.value === v);
    return hit ? t(hit.label) : v;
  }

  /** Pick the global "runners at once" cap (claudAutopilot.maxParallelRunners). */
  async pickRunnerLimit() {
    const now = this.runnerLimit();
    const items = argv.RUNNER_LIMITS.map((n) => ({
      label: n ? t('{0} at once', n) : t('No limit'),
      description: [n === 0 ? t('as before') : '', n === now ? t('current') : ''].filter(Boolean).join(' · '),
      n,
    }));
    const pick = await pickers.showQuickPick(items, {
      title: `Sleeping Clanker: ${t('Runners at once…')}`, ignoreFocusOut: true,
      placeHolder: t('This window starts no more runners than this; the rest wait for a free slot.'),
    });
    if (!pick || pick.n === now) return;
    await this._updateSettings({ maxParallelRunners: pick.n });
    this.event(`runners at once: ${pick.n || 'no limit'} (user settings)`);
    await this._startWaitingRunners();   // a raised limit lets a waiting queue start at once
  }

  /** Pick what a NEW task's subagent limit is (claudAutopilot.defaultMaxAgents). */
  async pickAgentsDefault() {
    const now = this.settings.defaultMaxAgents || 'default';
    const items = argv.AGENT_LIMITS.map((a) => ({
      label: t(a.label), description: a.value === now ? t('current') : '', value: a.value,
    }));
    const pick = await pickers.showQuickPick(items, {
      title: `Sleeping Clanker: ${t('Subagents for new tasks…')}`, ignoreFocusOut: true,
      placeHolder: t('A task that may start subagents gets the Agent tool; its other permissions do not change.'),
    });
    if (!pick || pick.value === now) return;
    await this._updateSettings({ defaultMaxAgents: pick.value });
    this.event(`subagents for new tasks: ${pick.value} (user settings)`);
  }

  /**
   * The Task view's subagent dropdown: `edit ID --max-agents=<value>` for the item's next attempt.
   * @param {any} arg @param {string} value one of argv.AGENT_LIMITS
   */
  async setMaxAgents(arg, value) {
    const t0 = this._target(arg);
    if (!argv.canSetAgents(t0.item)) throw new Error(t('{0} is running; pause it first, then change its subagent limit', t0.item.id));
    const r = await this.cliJson(argv.agentsArgs(t0.item.id, value), parse.parseEdit, undefined, t0.project, t0.laneArg);
    this.event(r.changed.length ? `subagents of ${t0.item.id} -> ${value}` : `no change to the subagents of ${t0.item.id}`);
    await this.poller.trigger();
  }

  /** "English" or "Auto: English"; the language setting as the owner set it. */
  _languageDescription() {
    const set = this.settings.language || i18n.AUTO;
    const name = i18n.NAMES[/** @type {'en'} */ (i18n.language())];
    return set === i18n.AUTO ? `${t('Auto')}: ${name}` : name;
  }

  /** Pick the UI language; it is saved in the user settings and applied at once (onConfigChanged). */
  async languageMenu() {
    const set = this.settings.language || i18n.AUTO;
    const vsLang = vscode.env && typeof vscode.env.language === 'string' ? vscode.env.language : '';
    const items = [i18n.AUTO, ...i18n.LANGUAGES].map((id) => ({
      label: id === i18n.AUTO
        ? `$(sync) ${t('Auto')}: ${i18n.NAMES[/** @type {'en'} */ (i18n.resolve(i18n.AUTO, vsLang))]}`
        : i18n.NAMES[/** @type {'en'} */ (id)],
      description: [id === i18n.AUTO ? t('the VS Code display language') : id, id === set ? t('current') : '']
        .filter(Boolean).join(' · '),
      id,
    }));
    const pick = await pickers.showQuickPick(items, {
      title: `Sleeping Clanker: ${t('Language')}`, placeHolder: 'Language / Язык / Kieli', ignoreFocusOut: true,
    });
    if (!pick || pick.id === set) return;
    await this.setUiLanguage(pick.id);
  }

  /**
   * (2026-10-26) Save the UI language in the USER settings and apply it at once: the gear's quick pick
   * and the add panel's Language select share this one path. Never a per-task option.
   * @param {string} id auto | en | ru | fi
   */
  async setUiLanguage(id) {
    if (![i18n.AUTO, ...i18n.LANGUAGES].includes(id) || id === (this.settings.language || i18n.AUTO)) return;
    await vscode.workspace.getConfiguration(SECTION).update('language', id, vscode.ConfigurationTarget.Global);
    this.settings = { ...this.settings, language: id }; // before the change event arrives
    this.event(`language: ${id} (user settings)`);
    this.onLanguageChanged();
  }

  /**
   * (2026-10-26, owner "Valinta + VS Code-kieli") The UI language just became `lang` (any path). Russian
   * while VS Code is not: one non-modal offer of VS Code's own display-language picker (any language
   * pack download is VS Code's own action after the owner picks it) or "Don't ask again". Finnish: VS
   * Code has no Finnish pack, so its menus and the Settings page stay in its language; said once. Never
   * argv.json, never an install, never the Marketplace from here.
   * @param {string} lang
   */
  async _languageNotice(lang) {
    // (2026-10-27, review) the user setting changes in EVERY open window; only the focused one (where the
    // gear, the add panel, the Settings page or settings.json was used) speaks, the others only re-render
    const ws = vscode.window && vscode.window.state;
    if (ws && ws.focused === false) return;
    const gs = this.context && this.context.globalState;
    if (lang === 'ru') {
      const vsLang = vscode.env && typeof vscode.env.language === 'string' ? vscode.env.language : '';
      if (vsLang.toLowerCase().startsWith('ru') || (gs && gs.get(RU_LOCALE_OFFER_OFF_KEY, false))) return;
      const sw = t('Switch VS Code to Russian');
      const off = t("Don't ask again");
      const pick = await vscode.window.showInformationMessage(t("Sleeping Clanker is in Russian now, but VS Code itself is not: its menus, the command names and the Settings page stay in VS Code's language. Switch VS Code to Russian too? VS Code opens its own display-language picker; it installs its Russian language pack itself and asks to restart."), sw, off);
      if (pick === sw) await vscode.commands.executeCommand('workbench.action.configureLocale');
      else if (pick === off && gs) await gs.update(RU_LOCALE_OFFER_OFF_KEY, true);
    } else if (lang === 'fi') {
      if (gs && gs.get(FI_LOCALE_NOTE_KEY, false)) return;
      if (gs) await gs.update(FI_LOCALE_NOTE_KEY, true);
      vscode.window.showInformationMessage(t("Sleeping Clanker is in Finnish now. VS Code has no Finnish language pack, so its menus, the command names and the Settings page stay in VS Code's language."));
    }
  }

  /**
   * Read the language setting into lib/i18n (auto = VS Code's display language).
   * @returns {boolean} true when the UI language changed
   */
  _applyLanguage() {
    const vsLang = vscode.env && typeof vscode.env.language === 'string' ? vscode.env.language : '';
    const before = i18n.language();
    return i18n.setLanguage(i18n.resolve(this.settings.language, vsLang)) !== before;
  }

  /** Re-render everything that shows text: the tree, the status bar (next poll) and open Task views. */
  onLanguageChanged() {
    if (!this._applyLanguage()) return;
    this._badgeCount = undefined; // (2026-10-27, i18n) the next poll rebuilds the badge tooltip in the new language
    this.codeLensEmitter.fire(undefined); // (2026-10-27, i18n) the Save / Send reply lenses too
    this._renderTitle();
    this._setTree(this.provider.items, this.rows);
    this.taskViews.relocalize();
    this._refreshAddPanel(); // (2026-10-21, add panel)
    this.poller.trigger();
    this._languageNotice(i18n.language()).catch((e) => this.reportError('language', e)); // (2026-10-26)
  }

  /**
   * Write to the USER settings (as the "Always start automatically" button does) and render the new
   * values at once: the configuration event only arrives a tick later.
   * @param {Record<string, any>} patch
   */
  async _updateSettings(patch, opts = {}) {
    // (2026-10-21, security review B1) full is never written as a default...
    // (2026-10-21, owner "Lisää varoituksella") ...unless confirmed (_updateDefaults), and then the
    // machine-local marker is written FIRST: the configuration event reads the setting back with it
    if (patch && patch.defaultProfile === argv.FULL_PROFILE) {
      if (!(opts.fullConfirmed && await this._setFullMarker(true))) patch = { ...patch, defaultProfile: 'safe' };
    } else if (patch && ('askProfile' in patch || 'defaultProfile' in patch)) {
      // any other permission choice ends a full default: the setting goes back to safe, the marker goes
      if (this.settings.defaultProfile === argv.FULL_PROFILE && !('defaultProfile' in patch)) patch = { ...patch, defaultProfile: 'safe' };
      if (this._fullMarker()) await this._setFullMarker(false);
    }
    const keys = Object.keys(patch || {});
    if (!keys.length) return;
    const cfg = vscode.workspace.getConfiguration(SECTION);
    this._settingsWrites += 1;
    try {
      for (const key of keys) await cfg.update(key, patch[key], vscode.ConfigurationTarget.Global);
    } finally {
      this._settingsWrites -= 1;
    }
    this.settings = { ...this.settings, ...patch };
    this.event(`defaults: ${keys.map((k) => `${k}=${JSON.stringify(patch[k])}`).join(', ')} (user settings)`);
    this._setTree(this.provider.items, this.rows);
    this._refreshAddPanel(); // (2026-10-21, add panel) one source of truth: the tree rows and the panel
  }

  /**
   * New Queue… (SPEC_V03 2.3): name it, then add its first task; the core creates the queue folder
   * on that first add, so a cancelled flow leaves nothing behind.
   */
  async newLane() {
    const name = await this.askLaneName();
    if (!name) return;
    await this.cmdAdd(name);
  }

  /** @param {string | null} [lane] */
  async cmdAddSelection(lane) {
    const editor = vscode.window.activeTextEditor;
    const text = editor ? editor.selections.map((s) => editor.document.getText(s)).join('\n') : '';
    if (!text.trim()) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: select some text first.'));
      return;
    }
    await this.addPrompt(text, { lane });
  }

  /** @param {string | null} [lane] */
  async cmdAddFile(lane) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: open a file first.'));
      return;
    }
    const doc = editor.document;
    if (!doc.getText().trim()) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: the file is empty.'));
      return;
    }
    // A saved local file goes by path; unsaved or virtual buffers go through a temp copy.
    if (doc.uri.scheme === 'file' && !doc.isDirty && !doc.isUntitled) {
      await this.addFile(doc.uri.fsPath, null, lane);
    } else {
      const tmp = proc.writeTempPrompt(doc.getText());
      await this.addFile(tmp, tmp, lane);
    }
  }

  // ---- runner -------------------------------------------------------------------------------
  /**
   * (2026-10-26) One start per queue at a time: a second start of the same (project, queue) runs after
   * the first has settled, so it sees the first runner and says 'running'. Two overlapping starts (a
   * double click on Start, an auto-start during another) both read "no runner" and spawned two; the
   * loser hit the core's lock (exit 1, an error toast) and its record replaced the live runner's in
   * ownRunners, so this window lost track of the runner it had to stop on close.
   * @param {string | null} [project] @param {string} [name] @param {string | null} [lane]
   * @param {{auto?: boolean, ignoreHold?: boolean, heldSince?: string | null}} [opts]
   * @returns {Promise<'started' | 'held' | 'waiting' | 'running' | 'cancelled'>}
   */
  startRunner(project, name, lane = null, opts = {}) {
    const proj = project === undefined ? this.projectRoot : project;
    const key = `${proj || ''}\u0000${(laneArg(lane) || '').toLowerCase()}`;
    if (!this._runnerStarts) this._runnerStarts = new Map();
    const starts = this._runnerStarts;
    const run = (starts.get(key) || Promise.resolve()).catch(() => {})
      .then(() => this._startRunnerNow(project, name, lane, opts));
    starts.set(key, run);
    const done = () => { if (starts.get(key) === run) starts.delete(key); };
    run.then(done, done);
    return run;
  }

  /**
   * Start the runner of a project (default: this window's). Another project's runner is allowed,
   * and is this window's child process too (SPEC_PROJECTS 3.1).
   * (2026-10-15, SPEC_V03 2.3) `lane` = the queue whose runner starts; one runner per queue.
   * (2026-10-21, v049 dontask_hold) A held queue (`status.hold`): `opts.auto` (auto-start after add or
   * reply, on window open, a slot that freed) never starts it and returns 'held'; an explicit start asks
   * once, modal, "start anyway?", and only then runs with `--ignore-hold` (opts.ignoreHold = already
   * confirmed). Without a hold the argv keeps its 0.4.0-0.4.8 shape.
   * (2026-10-22, v049 tester LOW-10) `opts.heldSince`: the `since` of the hold the owner said "Start
   * anyway" to. A start that waited for a runner slot keeps --ignore-hold only for that same hold; a
   * hold set again meanwhile (a new since) drops the start, said once ('held'); a released hold starts
   * normally.
   * @param {string | null} [project] @param {string} [name] @param {string | null} [lane]
   * @param {{auto?: boolean, ignoreHold?: boolean, heldSince?: string | null}} [opts]
   * @returns {Promise<'started' | 'held' | 'waiting' | 'running' | 'cancelled'>}
   */
  async _startRunnerNow(project, name, lane = null, opts = {}) {
    const proj = project === undefined ? this.projectRoot : project;
    const projLabel = name || (proj ? path.basename(proj) || proj : t('the shared queue')); // (2026-10-27, i18n) shown
    const label = lane && lane !== argv.DEFAULT_LANE ? `${projLabel} / ${format.laneLabel(lane)}` : projLabel;
    const status = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, proj, lane);
    if (this.ownRunners.has(status.dataDir)) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: the runner for {0} is already running.', label));
      return 'running';
    }
    if (status.runnerAlive) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: a runner for {0} is already running (pid {1}).', label, status.runnerPid));
      return 'running';
    }
    let ignoreHold = false;
    /** @type {string | null} */
    let heldSince = null;
    if (status.hold && !status.hold.until) { // (2026-10-26) a timed hold: the runner starts and waits it out
      const since = status.hold.since || null;
      if (opts.ignoreHold && (opts.heldSince || null) === since) {
        ignoreHold = true;
        heldSince = since;
      } else if (opts.ignoreHold) {
        // (2026-10-22, v049 tester LOW-10) "Start anyway" was for another hold: never past a new one
        this.event(`runner start (${label}) dropped: the queue was put on hold again (since ${since || '?'}) while it `
          + `waited for a runner slot; "Start anyway" was for the hold since ${opts.heldSince || '?'}`);
        vscode.window.showInformationMessage(t('Sleeping Clanker: {0} was put on hold again while it waited for a runner slot, so its runner did not start. Start it again to run it anyway.', label));
        return 'held';
      } else if (opts.auto) {
        this.event(`runner start (${label}) skipped: the queue is on hold (since ${status.hold.since || '?'}); `
          + 'an automatic start never starts a held queue');
        return 'held';
      } else {
        const pick = await vscode.window.showWarningMessage(
          t('Sleeping Clanker: queue {0} is on hold — start anyway?', label),
          { modal: true, detail: t('The hold stays on: the runner works through the queue, and the next automatic start skips it again. "Resume Queue (Release Hold)" in the Queue view ends the hold.') },
          t(START_ANYWAY));
        if (pick !== t(START_ANYWAY)) {
          this.event(`runner start (${label}) cancelled: the queue is on hold`);
          return 'cancelled';
        }
        this.event(`runner start (${label}): the queue is on hold; the owner chose "Start anyway" (--ignore-hold)`);
        ignoreHold = true;
        heldSince = since;
      }
    }
    // (2026-10-21, subagents, SPEC_V049_AGENTS) the "runners at once" cap: count the runners THIS
    // window started (the only ones it can hold back) and queue the rest
    if (!argv.hasRunnerSlot(this.runnerLimit(), this.ownRunners.size)) {
      this._waitForRunnerSlot(proj, lane, label, { ignoreHold, heldSince });
      return 'waiting';
    }
    await this._parallelWarning(proj, status.dataDir);
    this._parallelIntro(); // (2026-10-27, U4, 8.12) a no-op unless tasks at once > 1 for the first time
    const py = await this.python();
    // (2026-10-26, limit_continue) --on-limit ask while claudAutopilot.autoContinueAfterLimit is off
    const args = argv.cliArgv(py, this.settings, argv.runArgs(this.settings, { ignoreHold, onLimit: argv.onLimitFor(this.settings) }), proj, lane);
    const onLimit = argv.onLimitFor(this.settings); // (2026-10-27, integrate: review LOW-3) kept on the record
    this.event(`runner start (${label}): ${proc.formatArgv(py.command, args)}`);
    /** @type {() => void} */
    let markExited = () => {};
    const exited = new Promise((resolve) => { markExited = () => resolve(undefined); });
    const t0 = Date.now();
    const key = status.dataDir;
    /** @type {RunnerRecord} */
    const record = { child: /** @type {any} */ (null), exited, stopRequested: false, project: proj, lane, dataDir: key, name: label, startedAt: t0,
      onLimit };
    const forget = () => { if (this.ownRunners.get(key) === record) this.ownRunners.delete(key); };
    const child = proc.spawnRunner({
      command: py.command, args, cwd: this.pythonDir, env: argv.childEnv(process.env, this.pythonDir),
      onLine: (line) => {
        // (2026-10-26) the bundled core's LockHeld text (store.acquire_lock): another runner owns the queue
        if (/another runner (is active|raced us)/.test(line)) record.lockHeld = true;
        this.log(`[runner ${label}] ${line}`);
      },
      onExit: (code, signal) => {
        this.event(`runner exited: code=${code} signal=${signal} after ${Date.now() - t0}ms (${label})`);
        forget();
        markExited();
        this.poller.trigger();
        // (2026-10-21, subagents) a slot freed: start the queue that has waited longest
        this._startWaitingRunners().catch(() => {});
        if (record.stopRequested) {
          // (2026-10-13, tester finding) A stop the user asked for may end in taskkill (exit 1).
          this.event(`INFO runner stopped (requested by the user; exit code ${code})`);
        } else if (code === argv.EXIT_HELD) {
          // (2026-10-21, v049 dontask_hold) the core refused: the queue went on hold before it started
          this.event(`INFO runner (${label}) did not start: the queue is on hold`);
          vscode.window.showInformationMessage(t('Sleeping Clanker: {0} is on hold, so its runner did not start.', label));
        } else if (record.lockHeld && code === 1) {
          // (2026-10-26) two windows on one project started this queue at once (both auto-start on
          // launch): the other window's runner works through it, so this exit is no error
          this.event(`INFO runner (${label}) did not start: another runner already works through this queue`);
          vscode.window.showInformationMessage(t('Sleeping Clanker: a runner for {0} is already running (another window started it).', label));
        } else if (code !== 0 && code !== null) {
          this.reportError('runner', new Error(t('exited with code {0}; see the output channel', code)));
        } else if (code === 0 && proj !== this.projectRoot) {
          // (2026-10-27, limit_continue fix round 2) no poll of this window reads another project's queues:
          // an ask runner that held its queue at a usage limit is asked about here
          this._limitHoldAfterExit(record).catch((err) => this.reportError('usage-limit hold', err));
        }
      },
      onError: (err) => {
        this.reportError('runner', err);
        forget();
        markExited();
      },
    });
    record.child = child;
    this.ownRunners.set(key, record);
    this.event(`runner started pid=${child.pid} (${label})`);
    if (proj !== this.projectRoot) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: started the runner for {0}. It runs as a child process of THIS window and stops when this window closes.', label));
    }
    setTimeout(() => this.poller.trigger(), 1500);
    return 'started';
  }

  /**
   * (2026-10-21, subagents, SPEC_V049_AGENTS) Remember a queue that has to wait for a free runner
   * slot (once per queue), and say so once. Nothing is started here.
   * (2026-10-21, v049 dontask_hold) `opts.ignoreHold`: the owner already said "Start anyway"
   * (2026-10-22, v049 tester LOW-10: to the hold since `opts.heldSince`, re-checked when the slot frees).
   * @param {string | null} project @param {string | null} lane @param {string} label
   * @param {{ignoreHold?: boolean, heldSince?: string | null}} [opts]
   */
  _waitForRunnerSlot(project, lane, label, opts = {}) {
    const key = `${project || ''}\u0000${lane || ''}`;
    if (this.waitingRunners.some((w) => `${w.project || ''}\u0000${w.lane || ''}` === key)) return;
    this.waitingRunners.push({ project, lane, label, ignoreHold: !!opts.ignoreHold, heldSince: opts.heldSince || null });
    const n = this.runnerLimit();
    this.event(`runner start (${label}) waits: ${this.ownRunners.size}/${n} runners already running `
      + `(claudAutopilot.maxParallelRunners); ${this.waitingRunners.length} queue(s) waiting`);
    vscode.window.showInformationMessage(t('Sleeping Clanker: {0} waits for a free runner slot ({1} at once). It starts by itself when a runner finishes.', label, n));
  }

  /**
   * (2026-10-21, tester round 2 M-3) Take one queue off the runner waitlist (a stop asked for it).
   * The default queue matches however its lane was given (null, "" or "default").
   * @param {string | null} project @param {string | null} lane @returns {boolean} it was waiting
   */
  _dropWaitingRunner(project, lane) {
    const same = (/** @type {{project: string | null, lane: string | null}} */ w) =>
      (w.project || '') === (project || '') && (laneArg(w.lane) || '') === (laneArg(lane) || '');
    const before = this.waitingRunners.length;
    this.waitingRunners = this.waitingRunners.filter((w) => !same(w));
    if (this.waitingRunners.length === before) return false;
    this.event(`runner stop: ${project ? path.basename(project) : 'the shared queue'}${laneArg(lane) ? ` / ${lane}` : ''} no longer waits for a runner slot`);
    return true;
  }

  /** (2026-10-21, tester round 2 M-3) Empty the runner waitlist. @param {string} why */
  _clearWaitingRunners(why) {
    if (!this.waitingRunners.length) return;
    this.event(`${why}: ${this.waitingRunners.length} queue(s) no longer wait for a runner slot`);
    this.waitingRunners = [];
  }

  /**
   * (2026-10-21, subagents) Start the queues that waited for a slot, the oldest first, while slots
   * last. Never while this window is shutting down (a stopped runner must stay stopped).
   */
  async _startWaitingRunners() {
    if (this._disposed || this._shuttingDown) return;
    while (this.waitingRunners.length) {
      if (!argv.hasRunnerSlot(this.runnerLimit(), this.ownRunners.size)) return;
      const next = /** @type {{project: string | null, lane: string | null, label: string, ignoreHold?: boolean, heldSince?: string | null}} */ (this.waitingRunners.shift());
      this.event(`runner start (${next.label}): a slot freed (${this.waitingRunners.length} still waiting)`);
      // (2026-10-21, v049 dontask_hold) a later, unattended start: a queue held meanwhile is skipped
      // unless the owner already chose "Start anyway" for it (2026-10-22, v049 tester LOW-10: for THAT hold)
      await this.guarded('start waiting runner', () => this.startRunner(next.project, undefined, next.lane,
        { auto: true, ignoreHold: !!next.ignoreHold, heldSince: next.heldSince || null }));
    }
  }

  /**
   * (2026-10-15, SPEC_V03 2.1) The first time a second queue's runner starts in a project (another
   * queue's runner is alive), warn once per session; "Don't show again" is kept in globalState.
   * @param {string | null} project @param {string} dataDir the queue about to start
   */
  async _parallelWarning(project, dataDir) {
    const gs = this.context.globalState;
    if (gs && gs.get(PARALLEL_WARNING_OFF_KEY, false)) return;
    const key = project || '';
    if (this._parallelWarned.has(key)) return;
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.13) only queues that work IN the folder: not when this one starts with tasks
    // at once > 1 (its tasks run in private copies), and not for another queue whose runner works in copies
    // (2026-10-27, U4 fix round 1) by parallel.mode (8.13), not by the setting alone: a runner that fell back to
    // in-place (git missing/old, no root, …) edits the folder itself, so it is warned about like a single runner
    if (argv.tasksAtOnceOf(this.settings) > 1
      && !this.lanes.some((l) => l.dataDir === dataDir && !!l.parallel && l.parallel.mode === 'in-place')) return;
    const copies = (/** @type {string} */ dd) => this.lanes.some((l) => l.dataDir === dd && !!l.parallel && l.parallel.mode === 'copies');
    const others = [...this.ownRunners.values()].some((r) => r.project === project && r.dataDir !== dataDir && !copies(r.dataDir))
      || (project === this.projectRoot && this.lanes.some((l) => l.runnerAlive && l.dataDir !== dataDir && !copies(l.dataDir)));
    if (!others) return;
    this._parallelWarned.add(key);
    // (2026-10-26) not awaited: a non-modal warning settles only when the owner clicks or clears it, so
    // the runner start (an auto-start, a queue that waited for a slot) hung until then
    const dontShow = t('Don\'t show again'); // (2026-10-27, i18n) translated once, compared as shown
    Promise.resolve(vscode.window.showWarningMessage(`Sleeping Clanker: ${t(PARALLEL_WARNING)}`, dontShow))
      .then((pick) => (pick === dontShow && gs ? gs.update(PARALLEL_WARNING_OFF_KEY, true) : undefined))
      .catch((err) => this.log(`${proc.stamp()} WARN parallel-queue warning: ${err instanceof Error ? err.message : err}`));
  }

  /**
   * Start/Stop from the status bar or the palette (SPEC_V03 2.3): one queue = that one; several = a
   * QuickPick of every queue plus `All queues`. Returns the chosen lanes; null = cancelled.
   * @param {'start' | 'stop'} what
   * @returns {Promise<(string | null)[] | null>}
   */
  async _pickRunnerLanes(what) {
    if (this.lanes.length < 2) return [null];
    const items = [
      ...this.lanes.map((l) => ({ label: format.laneLabel(l.name), description: format.laneStateParts(l), lanes: [l.name] })),
      { label: t('$(layers) All queues'), description: '', lanes: this.lanes.map((l) => l.name) },
    ];
    const pick = await vscode.window.showQuickPick(items, { title: what === 'start' ? t('Sleeping Clanker: start the runner of which queue?')
      : t('Sleeping Clanker: stop the runner of which queue?') });
    return pick ? pick.lanes : null;
  }

  async cmdStartRunner() {
    const lanes = await this._pickRunnerLanes('start');
    if (!lanes) return;
    for (const lane of lanes) {
      const row = this.lanes.find((l) => l.name === lane);
      if (lanes.length > 1 && row && row.runnerAlive) continue; // "All queues": skip the running ones quietly
      await this.startRunner(undefined, undefined, lane);
    }
  }

  async cmdStopRunner() {
    const lanes = await this._pickRunnerLanes('stop');
    if (!lanes) return;
    // (2026-10-21, tester round 2 M-3) "Stop all": no queue that waited for a runner slot may start
    // when a stopped runner exits, so the whole waitlist goes first
    if (lanes.length > 1) this._clearWaitingRunners('stop all');
    const alive = lanes.length > 1 ? lanes.filter((n) => {
      const row = this.lanes.find((l) => l.name === n);
      return (row && row.runnerAlive) || [...this.ownRunners.values()].some((r) => r.project === this.projectRoot && r.lane === n);
    }) : lanes;
    if (!alive.length) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: no runner is running.'));
      return;
    }
    await Promise.all(alive.map((lane) => this.stopRunner(STOP_TIMEOUT_MS, undefined, lane)));
  }

  /**
   * Graceful `stop`, then `taskkill /T /F` after `timeoutMs`.
   * @param {number} [timeoutMs] @param {string | null} [project] @param {string | null} [lane]
   */
  async stopRunner(timeoutMs = STOP_TIMEOUT_MS, project, lane = null) {
    const proj = project === undefined ? this.projectRoot : project;
    // (2026-10-21, tester round 2 M-3) a stopped queue that was waiting for a runner slot stays stopped
    const wasWaiting = this._dropWaitingRunner(proj, lane);
    const status = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, proj, lane);
    const own = this.ownRunners.get(status.dataDir) || null;
    if (!status.runnerAlive && !own) {
      vscode.window.showInformationMessage(wasWaiting ? t('Sleeping Clanker: the queue no longer waits for a runner slot; it will not start.')
        : t('Sleeping Clanker: no runner is running.'));
      return;
    }
    if (own) own.stopRequested = true;
    try {
      await this.cli(['stop'], undefined, proj, lane);
    } catch (err) {
      // Still fall through to the wait + taskkill: the runner must not be left running.
      this.reportError('stop request', err);
    }
    const stopped = await this._waitForStop(own, timeoutMs, proj, lane);
    if (!stopped) {
      const pid = own && own.child.pid ? own.child.pid : status.runnerPid;
      if (pid && await this._stillTheRunner(pid, own, proj, lane)) {
        this.event(`runner did not stop within ${timeoutMs}ms; taskkill pid=${pid}`);
        await this._taskkill(pid, own ? own.exited : null);
      }
    }
    await this.poller.trigger();
  }

  /**
   * (2026-10-26, runner identity) Right before a taskkill of `pid`: is it still the runner? Windows
   * reuses PIDs (a dead runner's lock PID once belonged to chrome.exe), so a PID taken from the lock is
   * only killed when the core (`status --json`, which checks the process's identity) still reports
   * the SAME pid alive. A runner this window started is its own child: its handle is held until it
   * exits, so its PID cannot be reused before `exited` resolves.
   * @param {number} pid @param {{child: {pid?: number}, exited: Promise<void>} | null} own
   * @param {string | null} project @param {string | null} [lane]
   * @returns {Promise<boolean>}
   */
  async _stillTheRunner(pid, own, project, lane = null) {
    if (own && own.child.pid === pid) return !(await exitedWithin(own.exited, 0));
    let s;
    try {
      s = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, project, lane, QUIET);
    } catch (err) {
      this.log(`${proc.stamp()} INFO stop: no taskkill of pid ${pid}; the core could not confirm it is the runner `
        + `(${err instanceof Error ? err.message : err})`);
      return false;
    }
    if (s.runnerAlive && s.runnerPid === pid) return true;
    this.log(`${proc.stamp()} INFO stop: no taskkill of pid ${pid}; the core reports `
      + (s.runnerAlive ? `the runner as pid ${s.runnerPid}` : 'no runner running (stale lock)'));
    return false;
  }

  /**
   * @param {{exited: Promise<void>} | null} own
   * @param {number} timeoutMs
   * @param {string | null} project
   * @param {string | null} [lane]
   */
  async _waitForStop(own, timeoutMs, project, lane = null) {
    const deadline = Date.now() + timeoutMs;
    if (own) return exitedWithin(own.exited, timeoutMs);
    while (Date.now() < deadline) {
      await sleep(500);
      const s = await this.cliJson(['status', '--json'], parse.parseStatus, undefined, project, lane, QUIET);
      if (!s.runnerAlive) return true;
    }
    return false;
  }

  /** @param {number} pid */
  /**
   * @param {number} pid
   * @param {Promise<void> | null} [exited] the runner record's exit promise, when this window started it
   */
  async _taskkill(pid, exited = null) {
    const exe = proc.taskkillPath();
    const r = await proc.runCli({ command: exe, args: proc.taskkillArgs(pid), cwd: this.pythonDir, env: process.env,
      log: (l) => this.log(l), timeoutMs: 30000 });
    if (r.code === 0) return;
    const detail = `${r.spawnError || `exit ${r.code}`}: ${r.stdout}${r.stderr}`.trim();
    // (2026-10-20, 0.4.4 logfix I) `/T` kills the tree: a process in it that ended meanwhile (or the
    // runner itself, between the stop timeout and the kill) makes taskkill exit 128/255 although the
    // runner is gone. That is not an error; a runner that is still alive is.
    if (exited && await exitedWithin(exited, TASKKILL_EXIT_GRACE_MS)) {
      this.log(`${proc.stamp()} INFO taskkill: ${detail} (the runner has exited)`);
      return;
    }
    this.reportError('taskkill', new Error(detail));
  }

  /** On deactivate: stop every runner this window started, each on its OWN project and queue (M12). */
  async shutdown() {
    this.poller.stop();
    this._stopGoalTimer();
    // (2026-10-21, subagents) a queue waiting for a slot must not start while the window closes
    this._shuttingDown = true;
    this.waitingRunners = [];
    const records = [...this.ownRunners.values()];
    await Promise.all(records.map(async (own) => {
      own.stopRequested = true;
      this.event(`deactivate: stopping the runner this window started (${own.name})`);
      try {
        await this.cli(['stop'], undefined, own.project, own.lane);
      } catch (err) {
        this.log(`${proc.stamp()} !! ERROR deactivate stop: ${err instanceof Error ? err.message : err}`);
      }
      const done = await exitedWithin(own.exited, DEACTIVATE_TIMEOUT_MS);
      if (!done && own.child.pid) {
        this.event(`deactivate: runner still alive after ${DEACTIVATE_TIMEOUT_MS}ms; taskkill`);
        await this._taskkill(own.child.pid, own.exited);
      }
    }));
  }

  // ---- item commands ------------------------------------------------------------------------
  /**
   * A tree node (carrying its project and queue) or a bare item of this window's project.
   * (2026-10-15, SPEC_V03 2.3) `lane` is the item's queue; `laneArg` is what goes to `--lane` (null
   * for `default`, so the argv of a one-queue project is unchanged).
   * @param {any} arg
   * @returns {{item: import('./lib/parse').Item, project: string | null, dataDir: string | null, lane: string, laneArg: string | null}}
   */
  _target(arg) {
    if (arg && arg.kind === 'item' && arg.item && typeof arg.item.id === 'string') {
      const lane = arg.lane || arg.item.lane || argv.DEFAULT_LANE;
      return { item: arg.item, project: arg.project, dataDir: arg.dataDir, lane, laneArg: laneArg(lane) };
    }
    if (!arg || typeof arg.id !== 'string') throw new Error(t('run this command from an item in the Queue view'));
    const lane = arg.lane || argv.DEFAULT_LANE;
    const row = this.lanes.find((l) => l.name === lane);
    const dataDir = row ? row.dataDir : this.status ? this.status.dataDir : null;
    return { item: arg, project: this.projectRoot, dataDir, lane, laneArg: laneArg(lane) };
  }

  /**
   * (2026-10-21) The subtasks of a main task, from the tree node's children, else this window's items.
   * @param {import('./lib/parse').Item} item @param {any} [node]
   * @returns {import('./lib/parse').Item[]}
   */
  subtasksOf(item, node) {
    if (node && node.kind === 'item') return (node.children || []).map((/** @type {any} */ c) => c.item);
    return (this.provider.items || []).filter((i) => i.parent === item.id && (i.lane || argv.DEFAULT_LANE) === (item.lane || argv.DEFAULT_LANE));
  }

  /**
   * (2026-10-21) Add a subtask under an item (under its main task when the item is a subtask): one
   * input box; the core gives it the main task's folder and profile and runs it after the main task.
   * (2026-10-21, security review M2) never `full`: a full main task's subtask gets the safe default
   * profile (sent as --profile, and the core does the same without it).
   * @param {any} arg
   */
  async addSubtask(arg) {
    const tg = this._target(arg);
    const main = tg.item.parent ? (this.provider.items.find((i) => i.id === tg.item.parent) || tg.item) : tg.item;
    const fromFull = main.profile === argv.FULL_PROFILE;
    const profile = fromFull ? this.safeDefaultProfile() : undefined;
    const prompt = await pickers.showInputBox({
      title: t('Subtask of: {0}', format.itemLabel(main)),
      prompt: fromFull
        ? t('It runs after the main task is done, in the same folder, with the {0} profile (a subtask never gets FULL ACCESS).', profile)
        : t('It runs after the main task is done, in the same folder with the same permissions.'),
      placeHolder: t('e.g. bake cookies'),
      ignoreFocusOut: true,
    });
    if (!prompt || !prompt.trim()) return;
    const gate = await this._limitGate(tg.project, tg.laneArg); // (2026-10-27, usage forecast fix LOW)
    if (!gate) return;
    const built = argv.subtaskArgs(main.id, prompt, (text) => proc.writeTempPrompt(text), profile);
    try {
      const id = await this._afterGate(gate, tg.project, tg.laneArg, // (2026-10-27, fix round 2 LOW)
        () => this.cliJson(built.args, parse.parseAdd, built.display, tg.project, tg.laneArg));
      await this._queued(id, 'subtask', tg.laneArg);
    } finally {
      if (built.tempFile) this._removeTemp(built.tempFile);
    }
  }

  /**
   * (2026-10-21) The trash button of the Complete / Errors group: delete every task in it (a main task
   * takes its subtasks with it), after one confirmation.
   * @param {any} node a group node
   */
  async removeGroup(node) {
    if (!node || node.kind !== 'group' || !Array.isArray(node.children)) throw new Error(t('run this command from a group in the Queue view'));
    // (2026-10-21, Completed by date) "Completed" holds date nodes: their tasks are the group's
    const kids = node.children.flatMap((/** @type {any} */ c) => (c.kind === 'day' ? c.children || [] : [c]));
    const mains = kids.filter((/** @type {any} */ c) => c.kind === 'item' && c.item.status !== 'running');
    const n = mains.reduce((sum, /** @type {any} */ c) => sum + 1 + (c.children ? c.children.length : 0), 0);
    if (!n) return;
    const del = t('Delete'); // (2026-10-27, i18n) translated once, compared as shown; plurals as two keys
    const ok = await vscode.window.showWarningMessage(n === 1
      ? t('Delete the task in "{0}" from the queue? This cannot be undone.', node.render.label)
      : t('Delete all {0} tasks in "{1}" from the queue? This cannot be undone.', n, node.render.label),
    { modal: true }, del);
    if (ok !== del) return;
    // (2026-10-21, tester round 2 L-5) one `remove ID ID …` per queue: the core removes them in one
    // transaction, so a failure leaves the queue as it was. On a failure, say what remains.
    /** @type {Map<string, {project: string | null, lane: string | null, ids: string[]}>} */
    const byQueue = new Map();
    for (const c of mains) {
      const tg = this._target(c);
      const key = `${tg.project || ''}\u0000${tg.laneArg || ''}`;
      if (!byQueue.has(key)) byQueue.set(key, { project: tg.project, lane: tg.laneArg, ids: [] });
      /** @type {any} */ (byQueue.get(key)).ids.push(tg.item.id);
    }
    let removed = 0;
    try {
      for (const q of byQueue.values()) {
        await this.cli(argv.removeManyArgs(q.ids), undefined, q.project, q.lane);
        removed += q.ids.length;
      }
    } catch (err) {
      await this.poller.trigger();
      const left = mains.length - removed;
      const msg = `${removed ? `deleted ${removed} of ${mains.length} main task(s)` : 'nothing was deleted'}; `
        + `${left} main task(s) of "${node.render.label}" remain in the queue`;
      this.event(`delete group: ${msg}`);
      // (2026-10-27, i18n) the toast in the UI language; the event line above stays English
      const shownMsg = removed ? t('deleted {0} of {1} main task(s); {2} main task(s) of "{3}" remain in the queue', removed, mains.length, left, node.render.label)
        : t('nothing was deleted; {0} main task(s) of "{1}" remain in the queue', left, node.render.label);
      throw new Error(`${shownMsg}: ${err instanceof Error ? err.message : String(err)}`);
    }
    this.event(`deleted ${n} task(s) from the ${node.render.label} group`);
    await this.poller.trigger();
  }

  /** Show Details: the unclipped explanation, then the raw fields (SPEC_HOVER 2.3). @param {any} arg */
  async showDetails(arg) {
    const tg = this._target(arg);
    const full = await this.cliJson(argv.itemArgs('show', tg.item.id), parse.parseShow, undefined, tg.project, tg.laneArg);
    const content = [
      format.itemExplanation(full, { ...this._ctx(), lane: tg.lane }, { full: true }),
      '',
      t('---- raw fields ----'), // (2026-10-27, i18n)
      JSON.stringify(format.detailFields(full), null, 2), // (2026-10-20) cost fields only for API billing
    ].join('\n');
    const doc = await vscode.workspace.openTextDocument({ language: 'plaintext', content });
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  /**
   * @param {'retry' | 'cancel' | 'remove' | 'pause' | 'resume'} command @param {any} arg
   */
  async itemCommand(command, arg) {
    const tg = this._target(arg);
    const { item, project, laneArg: lane } = tg;
    if (command === 'remove') {
      const what = item.kind === 'goal' ? 'goal' : 'task';
      const running = item.status === 'running';
      // (2026-10-21) the core removes a main task's subtasks with it
      const subs = this.subtasksOf(item, arg);
      // (2026-10-21) one click for finished work: a done or cancelled item without subtasks goes without asking
      const quick = (item.status === 'done' || item.status === 'cancelled') && !subs.length;
      if (!quick) {
        // (2026-10-27, i18n) one key per kind and state; the button translated once, compared as shown
        const label = format.itemLabel(item);
        const head = running ? (what === 'goal' ? t('Cancel and remove goal {0} ({1})', item.id, label) : t('Cancel and remove task {0} ({1})', item.id, label))
          : (what === 'goal' ? t('Remove goal {0} ({1})', item.id, label) : t('Remove task {0} ({1})', item.id, label));
        const removeBtn = t('Remove');
        const ok = await vscode.window.showWarningMessage(!subs.length ? t('{0} from the queue?', head)
          : subs.length === 1 ? t('{0} and its subtask from the queue?', head) : t('{0} and its {1} subtasks from the queue?', head, subs.length),
        { modal: true }, removeBtn);
        if (ok !== removeBtn) return;
      }
      await this._cancelIfRunning(item.id, project, lane);
      for (const s of subs) if (s.status === 'running') await this._cancelIfRunning(s.id, project, lane);
    }
    const gate = command === 'retry' ? await this._limitGate(project, lane) : true; // (2026-10-27, usage forecast fix LOW)
    if (!gate) return;
    await this._afterGate(gate, project, lane, () => this.cli(argv.itemArgs(command, item.id), undefined, project, lane));
    if (command === 'cancel' && item.status === 'running') {
      vscode.window.showInformationMessage(t('Sleeping Clanker: cancel requested; the runner stops its attempt within about a second.'));
    }
    await this.poller.trigger();
  }

  /**
   * (2026-10-20, 0.4.4 logfix B) Retry. On a BLOCKED item it opens the same Reply/profile picker as
   * Reply… (a blocked item with no session yet gets the profile QuickPick, then `retry --profile`);
   * any other status retries as before.
   * @param {any} arg
   */
  async retryItem(arg) {
    const tg = this._target(arg);
    if (tg.item.status !== 'blocked') {
      await this.itemCommand('retry', arg);
      return;
    }
    if (argv.canReply(tg.item)) {
      await this.replyItem(arg);
      return;
    }
    const choices = format.replyProfileChoices(tg.item, argv.PROFILES);
    const pick = await pickers.showQuickPick(choices, {
      title: t('Sleeping Clanker: retry {0} ({1}) with which profile?', tg.item.id, format.itemLabel(tg.item, 40)), ignoreFocusOut: true,
    });
    if (!pick) return;
    await this.retryWithProfile(arg, pick.profile);
  }

  /**
   * (2026-10-20, 0.4.4 logfix B) `retry <id> [--profile P]` (null or the item's own = keep it), then the
   * Start Runner offer. shell-dev with an empty allow-list says that it runs like shell-read.
   * @param {any} arg @param {string | null} profile
   */
  async retryWithProfile(arg, profile) {
    const tg = this._target(arg);
    const p = profile && profile !== (tg.item.profile || 'safe') ? profile : null;
    // (2026-10-21, security review B1) "Retry with full" needs the confirmation too
    if (!(await this.confirmFull(p, `the retry of ${tg.item.id}`, tg.item.id))) return;
    const gate = await this._limitGate(tg.project, tg.laneArg); // (2026-10-27, usage forecast fix LOW)
    if (!gate) return;
    await this._afterGate(gate, tg.project, tg.laneArg, () => this.cli(argv.retryArgs(tg.item.id, p || undefined), undefined, tg.project, tg.laneArg));
    this.event(`retry ${tg.item.id}${p ? ` with profile ${p}` : ''}`);
    await this.poller.trigger();
    if ((p || tg.item.profile) === 'shell-dev') {
      const list = await this.cliJson(argv.shellAllowArgs('list'), parse.parseShellAllow, undefined, tg.project, undefined, QUIET);
      if (!list.commands.length) {
        const edit = t(EDIT_ALLOWED); // (2026-10-27, i18n) translated once, compared as shown
        Promise.resolve(vscode.window.showWarningMessage(
          t('Sleeping Clanker: the allow-list of this project is empty, so shell-dev runs like shell-read.'), edit,
        )).then((pick) => (pick === edit ? this.guarded('edit allowed commands', () => this.editAllowedCommands(arg)) : undefined));
      }
    }
    await this._offerRunner(p ? t('Sleeping Clanker: {0} is queued again with {1}.', tg.item.id, p) : t('Sleeping Clanker: {0} is queued again.', tg.item.id),
      tg.project, tg.laneArg);
  }

  /**
   * (2026-10-20, 0.4.4 logfix A) "Edit allowed commands…": the project's shell-dev allow-list (kept in
   * its data dir, never the repo) as a QuickPick: add a command, or pick one to remove it. The core
   * validates every entry and refuses the hard-denied ones (git push, deleting, installs, web).
   * (2026-10-21, v049 dontask_hold) `prefill`: the entry a denial block suggested (the host's own copy
   * of the item's denial_block.allow, never webview text): the add box opens first, filled in, and the
   * owner still confirms it with Enter. Nothing is added without that.
   * @param {any} [arg] an item (its project), or nothing (this window's project)
   * @param {string | null} [prefill]
   */
  async editAllowedCommands(arg, prefill = null) {
    const project = arg && (arg.kind === 'item' || typeof arg.id === 'string') ? this._target(arg).project : undefined;
    let suggested = typeof prefill === 'string' && prefill.trim() ? prefill.trim() : null;
    for (;;) {
      const cur = await this.cliJson(argv.shellAllowArgs('list'), parse.parseShellAllow, undefined, project, undefined, QUIET);
      if (suggested && cur.commands.includes(suggested)) {
        vscode.window.showInformationMessage(t('Sleeping Clanker: "{0}" is already an allowed command of this project.', suggested));
        suggested = null;
      }
      /** @type {{label: string, description?: string, id: string, cmd?: string}[]} */
      const picks = [{ label: t('$(add) Add a command…'), id: 'add' },
        ...cur.commands.map((c) => ({ label: c, description: t('pick to remove'), id: 'remove', cmd: c }))];
      const value = suggested || undefined;
      suggested = null;
      const pick = value ? picks[0] : await pickers.showQuickPick(picks, {
        title: t('Sleeping Clanker: commands shell-dev may run in this project ({0})', cur.commands.length),
        placeHolder: cur.commands.length ? t('Add a command, or pick one to remove it')
          : t('Empty: shell-dev runs like shell-read. Add a command, e.g. "git merge" or "pytest".'),
        ignoreFocusOut: true,
      });
      if (!pick) return;
      try {
        if (pick.id === 'add') {
          const text = await pickers.showInputBox({
            value,
            title: t('Sleeping Clanker: allow a command for shell-dev'),
            // (2026-10-27, SEC-M5) says what is true: an allowed runner runs any code the task writes, with web access
            prompt: t('A command prefix, e.g. "git merge", "pytest" or ".venv\\Scripts\\python -m pytest". Any further arguments are allowed too. This is not a sandbox: the commands you allow run with your rights (a test runner, interpreter or build tool runs any code the task writes, and that code can push, delete, install and reach the web); only a fixed list of risky programs is refused.'),
            ignoreFocusOut: true,
            validateInput: (/** @type {string} */ v) => {
              try {
                argv.shellAllowArgs('add', v);
                return null;
              } catch (err) {
                return err instanceof Error ? err.message : String(err);
              }
            },
          });
          if (!text || !text.trim()) continue;
          await this.cliJson(argv.shellAllowArgs('add', text), parse.parseShellAllow, undefined, project);
          this.event(`shell-dev allow-list: added ${JSON.stringify(text.trim())}`);
        } else if (pick.cmd) {
          const removeBtn = t('Remove'); // (2026-10-27, i18n) translated once, compared as shown
          const ok = await vscode.window.showWarningMessage(t('Remove "{0}" from the commands shell-dev may run?', pick.cmd), { modal: true }, removeBtn);
          if (ok !== removeBtn) continue;
          await this.cliJson(argv.shellAllowArgs('remove', pick.cmd), parse.parseShellAllow, undefined, project);
          this.event(`shell-dev allow-list: removed ${JSON.stringify(pick.cmd)}`);
        }
      } catch (err) {
        // a refused entry (exit 2 with the reason): say why, and show the list again
        this.reportError('edit allowed commands', err);
      }
    }
  }

  /**
   * Amendment M13: Delete on a running item cancels it, waits up to 10 s until it is no longer
   * running (the runner kills the attempt within ~1 s), then the caller removes it.
   * @param {string} id @param {string | null} project @param {string | null} [lane]
   */
  async _cancelIfRunning(id, project, lane = null) {
    const now = await this.cliJson(argv.itemArgs('show', id), parse.parseShow, undefined, project, lane);
    if (now.status !== 'running') return;
    await this.cli(argv.itemArgs('cancel', id), undefined, project, lane);
    const deadline = Date.now() + DELETE_RUNNING_WAIT_MS;
    while (Date.now() < deadline) {
      await sleep(500);
      const s = await this.cliJson(argv.itemArgs('show', id), parse.parseShow, undefined, project, lane, QUIET);
      if (s.status !== 'running') return;
    }
    throw new Error(t('item {0} is still running {1} s after the cancel; try again', id, DELETE_RUNNING_WAIT_MS / 1000));
  }

  _dataDir() {
    if (this.status) return this.status.dataDir;
    if (this.where) return this.where.dataDir;
    throw new Error(t('the data folder is not known yet; refresh first'));
  }

  /** @param {any} arg */
  async openRunLogFolder(arg) {
    const tg = this._target(arg);
    const dir = argv.runLogDir(tg.dataDir || this._dataDir(), tg.item.id); // throws (shown + logged) unless inside runs/
    if (!fs.existsSync(dir)) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: task {0} has no run logs yet.', tg.item.id));
      return;
    }
    await this._openExternal(dir);
  }

  // ---- live task log (2026-10-15, SPEC_V03 1.3) ---------------------------------------------
  /**
   * (2026-10-17, 0.4.0) Double-click / Open Task View / the goal bar's log entry: what
   * `claudAutopilot.taskView` names — `chat` (the Task view webview, default) or `text` (the raw log).
   * @param {any} arg
   */
  async openLiveLog(arg) {
    if (this.settings.taskView === 'text' || !vscode.window.createWebviewPanel) {
      await this.openRawLog(arg);
      return;
    }
    await this.openTaskView(arg);
  }

  /** (2026-10-17, 0.4.0) The Task view of an item (docs/SPEC_V04_CHAT.md). @param {any} arg */
  async openTaskView(arg) {
    await this.taskViews.open(this._target(arg));
  }

  /**
   * (0.4.3) The target of a Task view restored after a window reload, or null when it no longer
   * resolves. `s` passed chatview.validateRestoreState (shape only); the CORE decides the rest:
   * the project must be this window's root, the shared queue (null) or a root `projects --json`
   * lists (an exact string match, amendment M9; a path from the webview is never used otherwise);
   * the queue must be one `lanes --json --items` lists for it and the item must be in that queue.
   * @param {{itemId: string, project: string | null, lane: string}} s
   * @returns {Promise<{item: import('./lib/parse').Item, project: string | null, dataDir: string, lane: string, laneArg: string | null} | null>}
   */
  async resolveTaskViewTarget(s) {
    const project = s.project;
    if (project !== null && project !== this.projectRoot) {
      const rows = await this.cliJson(argv.projectsArgs(), parse.parseProjects, undefined, undefined, undefined, QUIET);
      if (!rows.some((r) => !r.shared && r.root === project)) return null;
    }
    const out = await this.cli(argv.lanesArgs(), undefined, project, undefined, QUIET);
    const lanes = parse.parseLanes(out.stdout);
    if (!lanes.ok) throw new Error(`autopilot lanes: ${lanes.error}`);
    const lane = lanes.value.find((l) => l.name === s.lane);
    const item = lane ? lane.items.find((i) => i.id === s.itemId) || null : null;
    if (!lane || !item) return null;
    return { item, project, dataDir: lane.dataDir, lane: lane.name, laneArg: laneArg(lane.name) };
  }

  /**
   * Open the item's raw text log beside the editor: `claud-autopilot-log:/<project>/<lane>/<id>.log`
   * with (root, lane, id) in the query. (2026-10-15) The path ends in `.log` so the tab reads
   * "<id>.log"; the language is set to `autopilot-log` explicitly. (2026-10-17, 0.4.0) This was
   * `openLiveLog`; it is now Open Raw Log (and the Task view's "Raw log" link).
   * @param {any} arg
   */
  async openRawLog(arg) {
    const tg = this._target(arg);
    const projName = (tg.project ? path.basename(tg.project) : '') || (tg.project ? 'project' : 'shared');
    const safe = projName.replace(/[\\/?#%]/g, '_');
    const uri = vscode.Uri.from({
      scheme: LOG_SCHEME, path: `/${safe}/${tg.lane}/${tg.item.id}.log`,
      query: argv.liveLogQuery({ root: tg.project, lane: tg.lane, id: tg.item.id }),
    });
    let doc = await vscode.workspace.openTextDocument(uri);
    if (vscode.languages && vscode.languages.setTextDocumentLanguage && doc.languageId !== LOG_LANGUAGE) {
      try {
        doc = await vscode.languages.setTextDocumentLanguage(doc, LOG_LANGUAGE);
      } catch (err) {
        this.log(`${proc.stamp()} WARN live log language: ${err instanceof Error ? err.message : err}`);
      }
    }
    const editor = await vscode.window.showTextDocument(doc, { viewColumn: vscode.ViewColumn ? vscode.ViewColumn.Beside : undefined, preview: false });
    // Start at the end, so the view follows a running task like a terminal.
    if (editor && doc.lineCount && vscode.Selection) {
      const end = doc.lineAt(doc.lineCount - 1).range.end;
      editor.selection = new vscode.Selection(end, end);
      editor.revealRange(new vscode.Range(end, end));
    }
  }

  /** A tree click: two clicks on the same item within 500 ms open the live log. @param {any} node */
  async itemClick(node) {
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.10) a leftover copy: two clicks open its folder (Open folder only)
    if (node && node.kind === 'leftover' && typeof node.path === 'string') {
      if (this.clicks.click(node.render.id, Date.now())) await this._openLeftover(node.path);
      return;
    }
    if (!node || node.kind !== 'item') return;
    if (this.clicks.click(node.render.id, Date.now())) await this.openLiveLog(node);
  }

  // ---- reply (2026-10-15, SPEC_V03 5.2) -----------------------------------------------------
  /**
   * Reply…: a one-line input box; Enter on an EMPTY box opens an untitled editor for a long reply,
   * sent with the "Send reply" CodeLens (or the `Send Reply` command).
   * (2026-10-19, 0.4.1) A blocked item with a `blocked_question` option list first shows a QuickPick
   * of the options (recommended first) plus "Write my own reply…" (the input box below). The goal
   * status bar's Reply… comes here too.
   * @param {any} arg
   */
  async replyItem(arg) {
    const tg = this._target(arg);
    // (2026-10-27, U4 fix round 1, 8.6) a merge question is answered with `merge`, not a reply text: its card's
    // actions as a QuickPick (take-task, discard and undo keep their modal in mergeAction)
    const card = tg.item.blocked_question && tg.item.blocked_question.kind === 'merge' ? format.mergeCardModel(tg.item) : null;
    if (card && card.actions.length) {
      const pick = await pickers.showQuickPick(card.actions.map((a) => ({ label: a.label, action: a.action })), {
        title: t('Sleeping Clanker: reply to {0} ({1})', tg.item.id, format.itemLabel(tg.item, 40)),
        placeHolder: card.title, ignoreFocusOut: true,
      });
      if (pick) await this.mergeAction(arg, pick.action, card.gen);
      return;
    }
    if (!argv.canReply(tg.item)) {
      throw new Error(tg.item.session_id ? t('a {0} item cannot get a reply (only blocked, done, failed, cancelled or paused without a queued reply)', t(tg.item.status))
        : t('item {0} has no session to resume yet', tg.item.id));
    }
    const picks = format.replyOptionPicks(tg.item);
    if (picks.length) {
      const q = tg.item.blocked_question;
      const pick = await pickers.showQuickPick(picks, {
        title: t('Sleeping Clanker: reply to {0} ({1})', tg.item.id, format.itemLabel(tg.item, 40)),
        placeHolder: q && q.text ? t('Claude asks: {0}', q.text) : t('Pick an option, or write your own reply'),
        ignoreFocusOut: true, matchOnDetail: true,
      });
      if (!pick) return;
      if (pick.n !== null) {
        // the text comes from the item's own option list (format.choiceReplyText), never from the label
        const text = format.choiceReplyText(tg.item, pick.n);
        if (!text) throw new Error(t('option {0} is not offered for {1} any more', pick.n, tg.item.id));
        await this.sendReplyText(tg, text, { shell: pick.shell });
        return;
      }
    }
    const reason = tg.item.status === 'blocked' && tg.item.error ? tg.item.error.replace(/^blocked:\s*/, '') : '';
    const text = await pickers.showInputBox({
      title: t('Sleeping Clanker: reply to {0} ({1})', tg.item.id, format.itemLabel(tg.item, 40)),
      prompt: reason ? t('Claude needs: {0}. Type the reply and press Enter, or press Enter on an empty box to open an editor for a longer reply.', reason)
        : t('Type the reply and press Enter, or press Enter on an empty box to open an editor for a longer reply.'),
      ignoreFocusOut: true,
    });
    if (text === undefined) return;
    if (text.trim()) {
      await this.sendReplyText(tg, text);
      return;
    }
    const header = `<!-- Reply to ${tg.item.id}: write below this line, then click "Send reply" above. This line is not sent. -->\n`;
    const doc = await vscode.workspace.openTextDocument({ language: 'markdown', content: `${header}\n` });
    this.replyDrafts.set(doc.uri.toString(), tg);
    this.codeLensEmitter.fire(undefined);
    const editor = await vscode.window.showTextDocument(doc, { preview: false });
    if (editor && vscode.Selection) {
      const end = doc.lineAt(doc.lineCount - 1).range.end;
      editor.selection = new vscode.Selection(end, end);
    }
  }

  /**
   * The "Send reply" CodeLens / command: the draft in the active editor (or the given uri).
   * @param {any} [uri]
   */
  async sendReply(uri) {
    const key = uri ? uri.toString() : vscode.window.activeTextEditor ? vscode.window.activeTextEditor.document.uri.toString() : '';
    const tg = this.replyDrafts.get(key);
    if (!tg) throw new Error(t('open a reply draft first (Reply… on a task, then Enter on an empty box)'));
    const doc = (vscode.workspace.textDocuments || []).find((d) => d.uri.toString() === key)
      || (vscode.window.activeTextEditor ? vscode.window.activeTextEditor.document : null);
    if (!doc) throw new Error(t('the reply draft is no longer open'));
    const text = argv.replyDraftText(doc.getText());
    if (!text.trim()) throw new Error(t('the reply is empty'));
    if (await this.sendReplyText(tg, text)) {
      this.replyDrafts.delete(key);
      this.codeLensEmitter.fire(undefined);
    }
  }

  /**
   * The profile QuickPick, then `reply` (SPEC_V03 5.2). True when the reply was queued.
   * (2026-10-17, 0.4.0) The Task view's composer passes `opts.profile` (null = keep the item's) and
   * skips the QuickPick; `opts.onQueued` runs once the core has queued the reply, before the
   * Start Runner offer (which may wait for a click).
   * @param {{item: import('./lib/parse').Item, project: string | null, lane: string, laneArg?: string | null}} tg
   * @param {string} text
   * (0.4.1) `opts.shell`: the chosen option mentions a shell, so the QuickPick puts shell-read first.
   * (2026-10-26, modes) `opts.autoStart`: an Autorun answer starts the queue's runner as autoStartRunner does.
   * (2026-10-27, integrate) `opts.auto` or `opts.autoStart`: an automatic answer (Auto-pick best, Autorun):
   * no modal that waits for the owner (the idle FULL ACCESS question, the usage-limit forecast).
   * (2026-10-29, F4, SEC-L22) a full task's automatic answer never even gets here while that idle question would be
   * asked: lib/taskview.js _idlePause leaves the question to the owner (idleFullCheck).
   * (2026-10-29, F4) a reply to a merge question says the merge answer it became and its result; a refused one throws.
   * @param {{profile?: string | null, onQueued?: () => void, shell?: boolean, auto?: boolean, autoStart?: boolean}} [opts]
   */
  async sendReplyText(tg, text, opts = {}) {
    /** @type {{profile: string | null} | undefined} */
    let pick;
    if (opts.profile !== undefined) {
      pick = { profile: opts.profile && opts.profile !== tg.item.profile ? opts.profile : null };
    } else {
      // (0.4.2 L2) a chosen option's shell flag alone decides; a free-text reply uses the reason rule
      const choices = format.replyProfileChoices(tg.item, argv.PROFILES, typeof opts.shell === 'boolean' ? { shell: opts.shell } : {});
      pick = await pickers.showQuickPick(choices, { title: t('Sleeping Clanker: permission profile for the resumed session'), ignoreFocusOut: true });
    }
    if (!pick) return false;
    // (2026-10-21, security review B1/L8) switching to full, from Reply… or the Task view's dropdown,
    // needs the host's own confirmation; cancelled = nothing is sent
    if (!(await this.confirmFull(pick.profile, `the reply to ${tg.item.id}`, tg.item.id))) return false;
    const automatic = !!(opts.auto || opts.autoStart); // (2026-10-27, integrate) Auto-pick best or Autorun
    // (2026-10-26, owner fa388b7b) a task that keeps full access, in a queue the owner left idle: ask once
    // (never for an automatic answer: Auto-pick works while nobody is there; (2026-10-29, F4, SEC-L22) so while this
    // would ask, lib/taskview.js _idlePause does not answer a full task automatically at all)
    if (!pick.profile && tg.item.profile === argv.FULL_PROFILE && !automatic
      && !(await this.confirmIdleFull(tg.project, tg.lane, { id: tg.item.id, item: tg.item }))) return false;
    // (2026-10-26, usage forecast) the near-the-limit warning; (2026-10-27, integrate) never for an automatic
    // answer either: its modal would wait for an owner who is not there
    const gate = automatic ? true : await this._limitGate(tg.project, laneArg(tg.lane));
    if (!gate) return false;
    // (2026-10-27, modes fix round 2) an Autorun answer is stored as one (`--by autorun`): a note, not the owner's reply
    // (2026-10-29, integrate round 9) an Auto-pick best answer too (`--by autopick`): neither counts as the owner's
    // use of the queue for the idle FULL ACCESS check (lib/fullaccess.js lastOwnerUse)
    const by = opts.autoStart ? 'autorun' : opts.auto ? 'autopick' : null;
    const built = argv.replyArgs({ id: tg.item.id, text, profile: pick.profile, by }, (s) => proc.writeTempPrompt(s));
    const lane = laneArg(tg.lane);
    /** @type {{action?: string, ok?: boolean, state?: string | null, code?: string | null} | undefined} */
    let res;
    try {
      // (2026-10-29, F4) a reply to a merge question is a merge answer: exit 1 = refused, its JSON on stdout (as `merge`);
      // thrown inside the send so a held gate is released as after any failed send
      res = await this._afterGate(gate, tg.project, lane, async () => {
        const v = await this.cliJson(built.args, parse.parseReply, built.display, tg.project, lane, { okCodes: [1] });
        if (v.action && v.ok === false) {
          this.event(`reply to ${tg.item.id} answered its merge question: ${v.action} refused state=${v.state || '-'} code=${v.code || '-'}`);
          const why = v.code && Object.prototype.hasOwnProperty.call(MERGE_REFUSALS, v.code)
            ? t(MERGE_REFUSALS[/** @type {keyof typeof MERGE_REFUSALS} */ (v.code)]) : (v.code || t('no reason given'));
          // (2026-10-29, integrate round 9) marked: the reply WAS sent (the chat says "Sent, but …", not "Not sent: …")
          throw Object.assign(new Error(t(MERGE_REPLY_REFUSED, format.mergeActionLabel(v.action), why)), { mergeRefused: true });
        }
        return v;
      });
    } finally {
      if (built.tempFile) this._removeTemp(built.tempFile);
    }
    const merge = res && res.action ? res : null;
    this.event(merge ? `reply to ${tg.item.id} answered its merge question: ${merge.action} state=${merge.state || '-'} code=${merge.code || '-'}`
      : `reply queued for ${tg.item.id}${pick.profile ? ` (profile ${pick.profile})` : ''}`);
    // (2026-10-27, modes fix LOW) the owner's own reply (not an Autorun answer), from any path, re-arms its Autorun
    if (!opts.autoStart && tg.item.autorun && this.taskViews) this.taskViews.rearmAutorun(tg.lane, tg.item.id);
    await this.poller.trigger();
    if (opts.onQueued) opts.onQueued();
    if (merge) {
      // (2026-10-29, F4, integrate round 7) not "reply queued": the merge answer it became and its result
      const action = /** @type {string} */ (merge.action);
      const code = merge.code || '';
      const result = action === 'let-claude' ? t(MERGE_REPLY_BACK)
        : Object.prototype.hasOwnProperty.call(MERGE_REPLY_RESULTS, code) ? t(MERGE_REPLY_RESULTS[/** @type {keyof typeof MERGE_REPLY_RESULTS} */ (code)])
          : t('Result: {0}.', code || merge.state || '?');
      const msg = t(MERGE_REPLY_DONE, tg.item.id, format.mergeActionLabel(action), result);
      // the task goes back to the queue (let-claude) or its merge waits for a runner's merger: offered as after a reply
      // (never an Autorun answer: _autoPick answers a merge question through mergeAction, not a reply)
      if (action === 'let-claude' || code === 'waiting' || code === 'pending') await this._offerRunner(msg, tg.project, lane, !!opts.autoStart);
      else vscode.window.showInformationMessage(msg);
      return true;
    }
    // (2026-10-27, integrate: i18n) an Autorun answer's message goes to the event log only (English)
    const msg = opts.autoStart ? `Sleeping Clanker: reply queued; ${tg.item.id} resumes its session when the runner reaches it.`
      : t('Sleeping Clanker: reply queued; {0} resumes its session when the runner reaches it.', tg.item.id);
    await this._offerRunner(msg, tg.project, lane, !!opts.autoStart);
    return true;
  }

  // ---- merge (2026-10-27, U4, SPEC_V05_PARALLEL 3.6/8.6) ----------------------------------------------
  /**
   * A merge card answer: `merge <id> --<action> --gen <gen> --json`. take-task, discard and undo replace files or
   * throw work away: a modal asks first (an automatic pick is only ever let-claude, 8.7). Exit 1 = refused (the
   * question changed, not offered now, …): thrown in words. let-claude queues the task again, and a retry whose
   * merge still waits (pending/waiting) needs the runner's merger: only then is the runner offered (or started) as
   * after a reply (2026-10-30, M5 check 6). Returns the parsed result; null = the owner cancelled.
   * (2026-10-27, U4 fix round 1) As after a reply, only `opts.autorun` (an Autorun answer) starts a runner without
   * asking; an Auto-pick answer (`opts.auto` alone) offers it like a hand-picked one, or follows autoStartRunner.
   * @param {any} arg a tree node / Task view target @param {string} action @param {number} gen
   * @param {{auto?: boolean, autorun?: boolean, note?: string | null}} [opts]
   */
  async mergeAction(arg, action, gen, opts = {}) {
    const tg = this._target(arg);
    const id = tg.item.id;
    if (!argv.MERGE_ACTIONS.includes(action)) throw new Error(t('unknown merge action {0}', action));
    if (opts.auto && action !== 'let-claude') throw new Error(t('only "Let Claude fix it" is ever picked automatically'));
    const ask = Object.prototype.hasOwnProperty.call(MERGE_CONFIRMS, action) ? /** @type {keyof typeof MERGE_CONFIRMS} */ (action) : null;
    if (ask && !opts.auto) {
      const ok = format.mergeActionLabel(action);
      const pick = await vscode.window.showWarningMessage(t(MERGE_CONFIRMS[ask], `${id} — ${format.itemLabel(tg.item, 40)}`),
        { modal: true, detail: t(MERGE_CONFIRM_DETAILS[ask]) }, ok);
      if (pick !== ok) {
        this.event(`merge ${action} for ${id}: cancelled`);
        return null;
      }
    }
    const lane = laneArg(tg.lane);
    // (2026-10-29, integrate round 9) an automatic let-claude is stored as an automatic answer (`--by`), not owner use
    const by = opts.auto ? (opts.autorun ? 'autorun' : 'autopick') : null;
    const r = await this.cli(argv.mergeArgs(id, action, gen, opts.note || null, by), undefined, tg.project, lane, { okCodes: [1] });
    const p = parse.parseMerge(r.stdout);
    if (!p.ok) throw new Error(`autopilot merge: ${p.error}`);
    const v = p.value;
    this.event(`merge ${action} for ${id} (gen ${gen}${opts.auto ? ', automatic' : ''}): ${v.ok ? 'done' : 'refused'}`
      + ` state=${v.state || '-'} code=${v.code || '-'}`);
    await this.poller.trigger();
    if (!v.ok) {
      let why = v.code && Object.prototype.hasOwnProperty.call(MERGE_REFUSALS, v.code)
        ? t(MERGE_REFUSALS[/** @type {keyof typeof MERGE_REFUSALS} */ (v.code)]) : (v.detail || v.code || t('no reason given'));
      // (2026-10-29, integrate round 8, INTEGRATION [F1 -> F4/cli]) an undo refused because a later merge changed the
      // same files names that merge (`later`, newest first) and what to do
      if (v.code === 'changed-since' && v.later && v.later.length) {
        why = `${why}. ${t('A later merge changed them too: {0}. Undo that merge first (newest first), or edit the files by hand.',
          v.later.map((l) => `#${l.id.slice(0, 8)}`).join(', '))}`;
      }
      throw new Error(t('the merge answer was refused: {0}', why));
    }
    // (2026-10-30, M5 check 6) a runner is offered (or started) only when the answer left work for one: every ok
    // let-claude queued its reply (cli.py _merge_run: merge "fixing", the item pending); a retry merges at once and
    // leaves work only when its merge still waits for the runner's merger (lock wait timed out: "pending"; "waiting"),
    // the same rule as a typed reply's (sendReplyText). A retry that merged or still conflicts says so, nothing more.
    const code = v.code || '';
    if (action === 'let-claude' || (action === 'retry' && (code === 'waiting' || code === 'pending'))) {
      const autoStart = !!opts.autorun; // (2026-10-27, U4 fix round 1) an Autorun answer only, as taskview's reply path
      const msg = action === 'retry' ? t('Sleeping Clanker: {1} for {0}: {2}', id, format.mergeActionLabel(action),
        t(MERGE_REPLY_RESULTS[/** @type {keyof typeof MERGE_REPLY_RESULTS} */ (code)]))
        : autoStart ? `Sleeping Clanker: ${id} goes back to the queue to fix the merge conflict.`
          : t('Sleeping Clanker: {0} goes back to the queue ({1}).', id, format.mergeActionLabel(action));
      await this._offerRunner(msg, tg.project, lane, autoStart);
    } else if (action === 'retry') {
      if (code === 'conflict') {
        vscode.window.showWarningMessage(t('Sleeping Clanker: {0} is still a merge conflict; its merge card is back.', id));
      } else {
        const result = Object.prototype.hasOwnProperty.call(MERGE_REPLY_RESULTS, code)
          ? t(MERGE_REPLY_RESULTS[/** @type {keyof typeof MERGE_REPLY_RESULTS} */ (code)]) : t('Result: {0}.', code || v.state || '?');
        vscode.window.showInformationMessage(t('Sleeping Clanker: {1} for {0}: {2}', id, format.mergeActionLabel(action), result));
      }
    } else {
      if (v.patch) vscode.window.showInformationMessage(t("Sleeping Clanker: the task's changes were saved as a patch: {0}", v.patch));
      // (2026-10-29, integrate round 7: U3's `kept`) a discard that could not put all the work in a patch moved the
      // copy aside instead: say where it is (it is also listed under "Leftover copies")
      if (v.kept) {
        this.event(`merge ${action} for ${id}: copy kept at ${v.kept}`);
        vscode.window.showInformationMessage(t('Sleeping Clanker: the task\'s copy was moved aside and kept under "Leftover copies": {0}', v.kept));
      }
    }
    return v;
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.6) Preview of one conflicted file: `<merge.preview>\<path>` (the core's
   * result with the conflict markers, written outside the project) beside the Task view. (2026-10-30, M5) It opens as a
   * normal (editable) editor; nothing reads the preview back, so an edit there changes nothing. Only a path of
   * the item's own card, and only inside its preview folder. No diff command (8, Rejected).
   * @param {any} arg @param {string} rel
   */
  async _openMergePreview(arg, rel) {
    const tg = this._target(arg);
    const m = tg.item.merge;
    const card = format.mergeCardModel(tg.item);
    if (!m || !m.preview || !card || !card.preview || !card.paths.includes(rel)) {
      throw new Error(t('{0} is not on the merge card any more.', rel));
    }
    const base = path.resolve(m.preview);
    const file = path.resolve(base, rel);
    const inside = path.relative(base, file);
    if (!inside || inside.startsWith('..') || path.isAbsolute(inside)) throw new Error(t('{0} is outside the preview folder.', rel));
    if (!fs.existsSync(file)) throw new Error(t('No preview of {0} (only text files with conflicts have one).', rel));
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    await vscode.window.showTextDocument(doc, { preview: true, viewColumn: vscode.ViewColumn ? vscode.ViewColumn.Beside : undefined });
  }

  // ---- edit (2026-10-21) ---------------------------------------------------------------------
  /**
   * Edit Task…: (2026-10-26, edit in panel; owner "älä avaa uutta ikkunaa") the current text goes into
   * the New Task panel's edit box (shown and focused, turned on if it was closed), changed there and
   * saved with its Save. No input box, no editor. Only an item editSource allows: before the task has
   * started, a queued reply, or a paused task (saving starts it over).
   * @param {any} arg
   */
  async editItem(arg) {
    const { target, text } = await this.editSource(arg);
    await this.addPanel.startEdit(target, text);
    if (!this.settings.showAddPanel) await this._updateSettings({ showAddPanel: true });
    await vscode.commands.executeCommand(`${addpanel.VIEW_ID}.focus`);
  }

  /**
   * (2026-10-26, edit in box) The text an edit starts from: the fresh task text, or the queued reply
   * Claude has not got yet. Throws when neither can be edited any more. `target` goes to saveEditText.
   * @param {any} arg
   * @returns {Promise<{target: any, text: string}>}
   */
  async editSource(arg) {
    const tg = this._target(arg);
    // the full, fresh text: the tree may hold an older copy
    const full = await this.cliJson(argv.itemArgs('show', tg.item.id), parse.parseShow, undefined, tg.project, tg.laneArg);
    const item = { ...tg.item, ...full };
    // (2026-10-21, edit reply) a reply Claude has not got yet is what the owner wants to change
    const reply = argv.canEditReply(item);
    // (2026-10-26, edit paused) a started paused task: saving starts it over with the new text
    const restart = !reply && !argv.canEdit(item) && argv.canEditRestart(item);
    if (!reply && !restart && !argv.canEdit(item)) {
      throw new Error(item.status === 'running' ? t('{0} is running; pause it first, then edit it', item.id)
        : t('{0} has already started, so Claude has its text; use Reply… to give it new instructions', item.id));
    }
    return { target: { ...tg, item, reply, restart }, text: (reply ? item.pending_reply : item.prompt) || '' };
  }

  /**
   * The "Save task" CodeLens / command: the edit draft in the active editor (or the given uri).
   * @param {any} [uri]
   */
  async saveEdit(uri) {
    const key = uri ? uri.toString() : vscode.window.activeTextEditor ? vscode.window.activeTextEditor.document.uri.toString() : '';
    const tg = this.editDrafts.get(key);
    if (!tg) throw new Error(t('open an edit draft first (Edit Task… on a task)'));
    const doc = (vscode.workspace.textDocuments || []).find((d) => d.uri.toString() === key)
      || (vscode.window.activeTextEditor ? vscode.window.activeTextEditor.document : null);
    if (!doc) throw new Error(t('the edit draft is no longer open'));
    const text = argv.editDraftText(doc.getText());
    if (!text.trim()) throw new Error(tg.reply ? t('the reply is empty; use Withdraw reply to take it back') : t('the task text is empty'));
    await this.saveEditText(tg, text);
    this.editDrafts.delete(key);
    this.codeLensEmitter.fire(undefined);
  }

  /**
   * `edit ID --text`: the core keeps the task's status and place in the queue.
   * (2026-10-21, edit reply) `tg.reply`: `edit ID --reply-text`, the reply not sent yet.
   * @param {{item: import('./lib/parse').Item, project: string | null, lane: string, laneArg?: string | null, reply?: boolean}} tg
   * @param {string} text
   */
  async saveEditText(tg, text) {
    const built = argv.editArgs({ id: tg.item.id, text, reply: !!tg.reply, restart: !!tg.restart }, (s) => proc.writeTempPrompt(s));
    let r;
    try {
      r = await this.cliJson(built.args, parse.parseEdit, built.display, tg.project, laneArg(tg.lane));
    } finally {
      if (built.tempFile) this._removeTemp(built.tempFile);
    }
    const what = tg.reply ? `the reply to ${tg.item.id}` : tg.item.id;
    this.event(r.changed.length ? `edited ${what}` : `no change to ${what}`);
    await this.poller.trigger();
    const shownWhat = tg.reply ? t('the reply to {0}', tg.item.id) : tg.item.id; // (2026-10-27, i18n) the event line stays English
    vscode.window.showInformationMessage(r.changed.includes('restart')
      ? t('Sleeping Clanker: {0} saved. It starts over with the new text when you resume it.', shownWhat)
      : r.changed.length ? t('Sleeping Clanker: {0} saved.', shownWhat)
      : t('Sleeping Clanker: {0} already had that text.', shownWhat));
  }

  /**
   * (2026-10-21, edit reply) Withdraw Reply: `edit ID --drop-reply` after a confirm. The core puts the
   * item back as the reply found it (blocked with its question, done, …) and drops the reply.
   * @param {any} arg
   */
  async withdrawReply(arg) {
    const tg = this._target(arg);
    const full = await this.cliJson(argv.itemArgs('show', tg.item.id), parse.parseShow, undefined, tg.project, tg.laneArg);
    const item = { ...tg.item, ...full };
    if (!argv.canEditReply(item)) {
      throw new Error(item.status === 'running' ? t('{0} is running; Claude may already have the reply', item.id)
        : t('{0} has no reply waiting; Claude already has the replies it shows', item.id));
    }
    const withdraw = t('Withdraw'); // (2026-10-27, i18n) translated once, compared as shown
    const ok = await vscode.window.showWarningMessage(
      t('Withdraw your reply to {0}? Claude never gets it, and the task goes back to how it was before the reply.', item.id),
      { modal: true }, withdraw);
    if (ok !== withdraw) return;
    const r = await this.cliJson(argv.dropReplyArgs(item.id), parse.parseEdit, undefined, tg.project, tg.laneArg); // (tester round 2) one lane form
    this.event(r.changed.length ? `withdrew the reply to ${item.id}` : `no reply withdrawn from ${item.id}`);
    await this.poller.trigger();
  }

  /**
   * (2026-10-21, start now) The Task view's "Start now": `first ID` moves the pending task to the front
   * of its queue, then that queue's runner starts without asking (the click is the owner's go). A live
   * runner is never stopped: it starts the task when its current attempt (or usage-limit wait) ends.
   * @param {any} arg
   */
  async startNow(arg) {
    const tg = this._target(arg);
    const lane = laneArg(tg.lane);
    const r = await this.cliJson(argv.firstArgs(tg.item.id), parse.parseFirst, undefined, tg.project, lane);
    this.event(`start now: ${r.id} ${r.moved ? 'moved to the front of its queue' : 'was already first in its queue'}`
      + (r.ahead ? ` (${r.ahead} task(s) still run before it)` : ''));
    await this.poller.trigger();
    if (this.laneAlive(tg.project, lane)) {
      vscode.window.showInformationMessage(r.ahead
        ? t('Sleeping Clanker: {0} is number {1} in the queue; the runner starts it as soon as it finishes what it is doing now.', r.id, r.ahead + 1)
        : t('Sleeping Clanker: {0} is next in the queue; the runner starts it as soon as it finishes what it is doing now.', r.id));
      return;
    }
    await this.startRunner(tg.project, undefined, lane);
  }

  /**
   * (2026-10-26, owner task 07254297) Move a not-done task to another place in its queue: Move to Top,
   * Move to Bottom, or a drop onto another task (before it). The core's `move` changes only the place
   * (seq); the runner follows the new order and the view refreshes now.
   * @param {any} arg @param {'top' | 'bottom' | {before: string}} where
   */
  async moveItem(arg, where) {
    const tg = this._target(arg);
    const r = await this.cliJson(move.moveArgs(tg.item.id, where), move.parseMove, undefined, tg.project, tg.laneArg);
    const place = typeof where === 'string' ? `to the ${where}` : `before ${where.before}`;
    this.event(`move: ${r.id} ${r.moved ? `moved ${place}` : 'was already there'}`
      + (r.ahead ? ` (${r.ahead} pending task(s) run before it)` : ''));
    await this.poller.trigger();
  }

  /**
   * (2026-10-21, SPEC_V048_REPORT) "Report an Issue on GitHub…": the core writes a redacted draft (no
   * prompt, result or run log; error lines can quote short, redacted pieces of Claude's text; keys, e-mails, user, PC and folder names replaced) and returns a
   * prefilled github.com new-issue link. The draft opens first; GitHub opens only on "Send", and
   * nothing is posted until the owner presses Submit there.
   * @param {any} [arg] an item (its status and error go in), or nothing
   */
  async reportIssue(arg) {
    const tg = arg && (arg.kind === 'item' || typeof arg.id === 'string') ? this._target(arg) : null;
    const text = await pickers.showInputBox({
      title: t('Sleeping Clanker: report an issue on GitHub'),
      prompt: t('What happened? (optional) Keys, e-mails, user and folder names are taken out; prompts and results are not included, but error lines can quote short pieces of what Claude wrote.'),
      ignoreFocusOut: true,
    });
    if (text === undefined) return;
    const rep = await this.cliJson(argv.reportArgs({ item: tg ? tg.item.id : undefined, text }), parse.parseReport,
      undefined, tg ? tg.project : undefined, tg ? tg.laneArg : undefined);
    this.event(`report: draft ${rep.file} (${rep.redactions} value(s) redacted)`);
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(rep.file));
    await vscode.window.showTextDocument(doc, { preview: false });
    // (tester round 2 L-3) not modal: the owner can edit the draft in its editor before choosing
    const send = t(REPORT_SEND); // (2026-10-27, i18n) translated once, compared as shown
    // (2026-10-27, SEC-L10) the dialog names the repo the link goes to (a wrong link throws here, before it)
    const repo = argv.reportRepo(rep.url);
    const pick = await vscode.window.showInformationMessage(
      t('Sleeping Clanker: read the draft ({0} value(s) redacted) and edit it if you like. Send opens a new issue on {1} with the draft as it is then (it goes to github.com in the link and lands in your browser history); nothing is posted until you press Submit there.', rep.redactions, repo),
      send, t(REPORT_CANCEL));
    if (pick !== send) return;
    // (2026-10-21, security review M3) the draft as the owner left it: saved, then re-read from the file,
    // as "Send for fix" does; the core's link was built before any edit and is not used
    if (doc && doc.isDirty && typeof doc.save === 'function') await doc.save();
    const link = argv.reportLink(fs.readFileSync(rep.file, 'utf8'), rep.url, rep.title);
    if (link.clipboard !== null) {
      await vscode.env.clipboard.writeText(link.clipboard);
      vscode.window.showInformationMessage(t('Sleeping Clanker: the draft is too long for a link. GitHub opens with the title only; the text is on your clipboard: paste it into the issue.'));
    }
    this.event(`report: opening GitHub, ${repo} (${link.clipboard !== null ? 'title only, body on the clipboard' : 'draft in the link'})`);
    // the string, not a Uri: Uri.parse decodes the %-escapes and would re-encode the body differently
    const ok = await vscode.env.openExternal(/** @type {any} */ (link.url));
    if (!ok) throw new Error(t('could not open GitHub in the browser'));
  }

  /**
   * (2026-10-21, SPEC_V048_REPORT §5) "Send Errors to Claude for a Fix…": the same redacted draft as
   * reportIssue, queued as a fix task (draft text inlined) in the Sleeping Clanker source folder
   * (`claudAutopilot.fixProject`, picked once). Nothing is queued until "Send"; nothing goes to GitHub.
   * @param {any} [arg] an item (its status and error go in), or nothing
   */
  async sendForFix(arg) {
    const tg = arg && (arg.kind === 'item' || typeof arg.id === 'string') ? this._target(arg) : null;
    const text = await pickers.showInputBox({
      title: t('Sleeping Clanker: send errors to Claude for a fix'),
      prompt: t('What happened? (optional) Keys, e-mails, user and folder names are taken out; prompts and results are not included, but error lines can quote short pieces of what Claude wrote.'),
      ignoreFocusOut: true,
    });
    if (text === undefined) return;
    const rep = await this.cliJson(argv.reportArgs({ item: tg ? tg.item.id : undefined, text }), parse.parseReport,
      undefined, tg ? tg.project : undefined, tg ? tg.laneArg : undefined);
    this.event(`report: draft ${rep.file} (${rep.redactions} value(s) redacted)`);
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(rep.file));
    await vscode.window.showTextDocument(doc, { preview: false });
    // (tester round 2 L-3) not modal: the owner can edit the draft in its editor before choosing
    // (2026-10-27, i18n) one key per case; the buttons translated once, compared as shown
    const send = t(REPORT_SEND);
    const pick = await vscode.window.showInformationMessage(this.settings.fixProject
      ? t('Sleeping Clanker: read the draft ({0} value(s) redacted) and edit it if you like. On Send, its text as it is then becomes a fix task for Claude in {1}. Nothing goes to GitHub.', rep.redactions, this.settings.fixProject)
      : t('Sleeping Clanker: read the draft ({0} value(s) redacted) and edit it if you like. On Send, its text as it is then becomes a fix task for Claude; you pick the Sleeping Clanker source folder next. Nothing goes to GitHub.', rep.redactions),
    send, t(REPORT_CANCEL));
    if (pick !== send) return;
    if (doc && doc.isDirty && typeof doc.save === 'function') await doc.save();
    const fixDir = await this._fixProject();
    if (!fixDir) return;
    // (2026-10-21, security review B1) the draft carries Claude's own text: a fix task never runs full
    const profile = this.presetProfile({ noFull: true }) || await this.pickProfile({ noFull: true });
    if (!profile || profile === argv.FULL_PROFILE) return;
    await this._refuseKeyProvider(profile, ''); // (v049 LOW-3) a fix task runs on the runner's provider
    const gate = await this._limitGate(fixDir, null); // (2026-10-27, usage forecast fix LOW)
    if (!gate) return;
    // the draft as the owner saw it (a saved edit included); inlined, see argv.fixPrompt
    const built = argv.addArgs({ prompt: argv.fixPrompt(fs.readFileSync(rep.file, 'utf8'), text), cwd: fixDir, profile },
      (p) => proc.writeTempPrompt(p));
    try {
      const id = await this._afterGate(gate, fixDir, null, () => this.cliJson(built.args, parse.parseAdd, built.display, fixDir, null)); // (fix round 2 LOW)
      this.event(`queued fix task ${id} in ${fixDir}`);
      await this.poller.trigger();
      await this._offerRunner(t('Sleeping Clanker: queued fix task {0} in {1}.', id, path.basename(fixDir)), fixDir, null);
    } finally {
      if (built.tempFile) this._removeTemp(built.tempFile);
    }
  }

  /** The Sleeping Clanker source folder for fix tasks: the setting, else a folder picked now and saved. */
  async _fixProject() {
    const set = this.settings.fixProject;
    if (set) {
      if (!fs.existsSync(set)) throw new Error(t('claudAutopilot.fixProject: {0} does not exist', set));
      return set;
    }
    const dialog = await vscode.window.showOpenDialog({
      canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
      title: t('Sleeping Clanker: its source folder (the fix is made there)'), openLabel: t('Use This Folder'),
    });
    if (!dialog || !dialog.length) return null;
    if (dialog[0].scheme !== 'file') throw new Error(t('pick a local folder'));
    const dir = /** @type {string} */ (dialog[0].fsPath);
    await vscode.workspace.getConfiguration(SECTION).update('fixProject', dir, vscode.ConfigurationTarget.Global);
    this.settings = { ...this.settings, fixProject: dir }; // before the change event arrives
    this.event(`claudAutopilot.fixProject set to ${dir} (user settings)`);
    return dir;
  }

  async showLog() {
    const file = path.join(this._dataDir(), 'logs', 'app.log');
    if (!fs.existsSync(file)) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: no log yet at {0}.', file));
      return;
    }
    const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(file));
    await vscode.window.showTextDocument(doc, { preview: true });
  }

  async openDataDir() {
    const dir = this._dataDir();
    if (!fs.existsSync(dir)) {
      // (amendment M11) a project dir exists only after the first add or run
      vscode.window.showInformationMessage(t('Sleeping Clanker: no data for this project yet ({0} is created by the first task).', dir));
      return;
    }
    await this._openExternal(dir);
  }

  /** @param {string} dir */
  async _openExternal(dir) {
    const ok = await vscode.env.openExternal(vscode.Uri.file(dir));
    if (!ok) throw new Error(t('could not open {0}', dir));
  }

  /** @param {any} node a project node */
  _projectOf(node) {
    if (!node || node.kind !== 'project' || !node.row) throw new Error(t('run this command from a project in the Queue view'));
    const row = /** @type {import('./lib/parse').Project} */ (node.row);
    return { project: row.shared ? null : row.root, name: view.projectLabel(row) };
  }

  /** @param {any} node */
  async startProjectRunner(node) {
    const p = this._projectOf(node);
    await this.startRunner(p.project, p.name);
  }

  /** @param {any} node */
  async stopProjectRunner(node) {
    const p = this._projectOf(node);
    await this.stopRunner(STOP_TIMEOUT_MS, p.project);
  }

  // ---- lane nodes (2026-10-15, SPEC_V03 2.3) ------------------------------------------------
  /** @param {any} node a lane node */
  _laneOf(node) {
    if (!node || node.kind !== 'lane' || !node.lane) throw new Error(t('run this command from a queue in the Queue view'));
    const lane = /** @type {import('./lib/parse').Lane} */ (node.lane);
    const projName = node.projectName || this.projectName() || '';
    return { project: node.project === undefined ? this.projectRoot : node.project, lane: lane.name, row: lane, projName };
  }

  /** @param {any} node */
  async startLaneRunner(node) {
    const l = this._laneOf(node);
    await this.startRunner(l.project, l.projName || undefined, laneArg(l.lane));
  }

  /** @param {any} node */
  async stopLaneRunner(node) {
    const l = this._laneOf(node);
    await this.stopRunner(STOP_TIMEOUT_MS, l.project, laneArg(l.lane));
  }

  /**
   * (2026-10-21, v049 dontask_hold) "Hold queue" / "Release hold": `hold on|off` of a lane node's queue,
   * or from the view's title bar this window's queue (a pick when it has several). The hold lives in
   * the queue's data dir (hold.json), never the repo. A live runner is not stopped: it finishes its
   * current task, then exits; no automatic start starts a held queue.
   * @param {any} node a lane node, or nothing @param {boolean} on
   */
  async setHold(node, on) {
    /** @type {{project: string | null, lane: string, limit?: boolean, since?: string | null}[]} */
    let targets;
    // (2026-10-27, limit_continue fix MED-1) a usage-limit hold: its ask runner has exited
    const isLimit = (/** @type {any} */ h) => !!(h && h.reason === 'limit');
    if (node && node.kind === 'lane') {
      const l = this._laneOf(node);
      targets = [{ project: l.project, lane: l.lane, limit: isLimit(l.row && l.row.hold), since: l.row && l.row.hold ? l.row.hold.since : null }];
    } else {
      const rows = this.lanes.length ? this.lanes : [{ name: argv.DEFAULT_LANE, hold: this.status ? this.status.hold : null }];
      // (2026-10-27, usage forecast fix round 2 MED) a timed hold can be made indefinite: it is a candidate for `on`
      const cand = rows.filter((l) => usage.holdCandidate(l, on));
      if (!cand.length) {
        vscode.window.showInformationMessage(on ? t('Sleeping Clanker: every queue of this window is already on hold.')
          : t('Sleeping Clanker: no queue of this window is on hold.'));
        return;
      }
      let names = cand.map((l) => l.name);
      if (rows.length > 1) {
        const pick = await vscode.window.showQuickPick(
          [...cand.map((l) => ({ label: format.laneLabel(l.name), names: [l.name] })),
            ...(cand.length > 1 ? [{ label: on ? t('$(layers) All queues') : t('$(layers) All held queues'), names }] : [])],
          { title: on ? t('Sleeping Clanker: hold which queue?') : t('Sleeping Clanker: release the hold of which queue?') });
        if (!pick) return;
        names = pick.names;
      }
      targets = names.map((name) => ({ project: this.projectRoot, lane: name,
        limit: isLimit((rows.find((l) => l.name === name) || {}).hold),
        since: ((/** @type {any} */ (rows.find((l) => l.name === name) || {})).hold || {}).since || null }));
    }
    for (const tg of targets) {
      const label = format.laneLabel(tg.lane);
      const r = await this.cliJson(argv.holdArgs(on ? 'on' : 'off'), parse.parseHoldResult, undefined, tg.project, laneArg(tg.lane));
      this.event(`hold ${on ? 'on' : 'off'}: ${label} (${r.dataDir})${r.runnerAlive ? `; runner pid ${r.runnerPid} finishes its current task, then exits` : ''}`);
      if (on) {
        // a runner waiting for a slot was an automatic start to be: it is dropped, not started later
        this._dropWaitingRunner(tg.project, laneArg(tg.lane));
        vscode.window.showInformationMessage(r.runnerAlive
          ? t('Sleeping Clanker: {0} is on hold. Its runner finishes the current task, then stops; nothing starts it again until you release the hold.', label)
          : t('Sleeping Clanker: {0} is on hold. No runner starts for it until you release the hold.', label));
      } else {
        vscode.window.showInformationMessage(t('Sleeping Clanker: released the hold of {0}.', label));
        // (2026-10-27, fix MED-1) a usage-limit hold: start the runner again (it waits out the reset)
        if (tg.limit && (!r.runnerAlive || this.ownRunners.has(r.dataDir))) await this._startAfterLimit(tg.project, tg.lane, r.dataDir, tg.since);
      }
    }
    await this.poller.trigger();
  }

  /** The lane node's inline $(add): the + Add menu, with the queue preset. @param {any} node */
  async addToLane(node) {
    const l = this._laneOf(node);
    if (l.project !== this.projectRoot) throw new Error(t('add to another project from a window of that project'));
    await this.cmdAdd(l.lane);
  }

  /** Remove Queue: confirm, then `lane-remove` (the core refuses a busy queue). @param {any} node */
  async removeLane(node) {
    const l = this._laneOf(node);
    if (!view.laneRemovable(l.row)) throw new Error(t('queue {0} still has work or a running runner', format.laneLabel(l.lane)));
    const removeBtn = t('Remove'); // (2026-10-27, i18n) translated once, compared as shown
    const ok = await vscode.window.showWarningMessage(
      t('Remove queue {0}? Its folder is moved to lanes\\.removed (a backup), not deleted.', format.laneLabel(l.lane)),
      { modal: true }, removeBtn);
    if (ok !== removeBtn) return;
    const res = await this.cliJson(argv.laneRemoveArgs(l.lane), parse.parseLaneRemove, undefined, l.project, null);
    this.log(`${proc.stamp()} lane-remove ${res.removed}; backup ${res.backup}`
      + (res.leftovers.length ? `; copies kept: ${res.leftovers.join(', ')}; patches: ${res.patches.join(', ') || '-'}` : ''));
    // (2026-10-29, integrate round 7: U3's patches/leftovers) a queue with task copies: they are kept, their work saved
    vscode.window.showInformationMessage(!res.leftovers.length ? t('Sleeping Clanker: removed queue {0}.', format.laneLabel(l.lane))
      : res.patches.length
        ? t('Sleeping Clanker: removed queue {0}. Its task copies ({1}) are kept under "Leftover copies"; their changes were saved as patches: {2}',
          format.laneLabel(l.lane), res.leftovers.length, res.patches.join(', '))
        : t('Sleeping Clanker: removed queue {0}. Its task copies ({1}) are kept under "Leftover copies".', format.laneLabel(l.lane), res.leftovers.length));
    await this.poller.trigger();
  }

  async statusMenu() {
    const alive = !!(this.status && this.status.runnerAlive) || !!this.ownRunner;
    const several = this.lanes.length >= 2;
    // (2026-10-17, 0.3.3) pending/waiting items without a runner: Start runner first, with the count
    const stopped = format.stoppedRunnerPending(this.lanes, this.ownLaneNames());
    const why = stopped > 0 ? t('{0} pending — runner stopped', stopped) : ''; // (2026-10-27, i18n)
    const items = [
      ...(several ? [{ label: t('$(play) Start runner…'), description: why, id: 'start' }, { label: t('$(debug-stop) Stop runner…'), id: 'stop' }]
        : [alive && !stopped ? { label: t('$(debug-stop) Stop runner'), id: 'stop' } : { label: t('$(play) Start runner'), description: why, id: 'start' }]),
      { label: t('$(add) Add…'), id: 'add' },
      { label: t('$(list-unordered) Show queue'), id: 'show' },
      this.allProjects ? { label: t('$(root-folder) Show this project only'), id: 'this' }
        : { label: t('$(layers) Show all projects'), id: 'all' },
    ];
    const pick = await vscode.window.showQuickPick(items, { title: `Sleeping Clanker — ${this.projectName() || t('(no folder)')}` });
    if (!pick) return;
    if (pick.id === 'stop') await this.cmdStopRunner();
    else if (pick.id === 'start') await this.cmdStartRunner();
    else if (pick.id === 'add') await this.cmdAdd();
    else if (pick.id === 'this') await this.setAllProjects(false);
    else if (pick.id === 'all') await this.setAllProjects(true);
    else await vscode.commands.executeCommand(CONTAINER_COMMAND);
  }

  /**
   * Goal bar click (SPEC_GOALS 5.2): Pause/Resume, Delete, Expand, Start runner; (2026-10-15,
   * SPEC_V03 1.3 / 5.2) plus Open Live Log, and Reply on a blocked goal.
   */
  async goalMenu() {
    const goal = this.goal;
    if (!goal) {
      vscode.window.showInformationMessage(t('Sleeping Clanker: no active goal.'));
      return;
    }
    const lane = goal.lane || argv.DEFAULT_LANE;
    const row = this.lanes.find((l) => l.name === lane);
    const alive = row ? row.runnerAlive : !!(this.status && this.status.runnerAlive) || !!this.ownRunner;
    const blocked = goal.status === 'blocked';
    const items = [
      ...(blocked && argv.canReply(goal) ? [{ label: t('$(reply) Reply…'), id: 'reply' }] : []),
      { label: this.settings.taskView === 'text' ? t('$(output) Open Raw Log') : t('$(comment-discussion) Open Task View'), id: 'log' },
      ...(blocked ? [] : [goal.status === 'paused' ? { label: t('$(debug-continue) Resume'), id: 'resume' } : { label: t('$(debug-pause) Pause'), id: 'pause' }]),
      { label: t('$(trash) Delete'), id: 'remove' },
      { label: t('$(open-preview) Expand'), id: 'expand' },
      ...(alive || blocked ? [] : [{ label: t('$(play) Start runner'), id: 'start' }]),
    ];
    const pick = await vscode.window.showQuickPick(items, { title: t('Goal: {0}', format.itemLabel(goal, 40)) });
    if (!pick) return;
    if (pick.id === 'expand') await this.showDetails(goal);
    else if (pick.id === 'log') await this.openLiveLog(goal);
    else if (pick.id === 'reply') await this.replyItem(goal);
    else if (pick.id === 'start') await this.startRunner(undefined, undefined, laneArg(lane));
    else await this.itemCommand(/** @type {'pause' | 'resume' | 'remove'} */ (pick.id), goal);
  }

  /**
   * (2026-10-15, SPEC_V03 2.4) autoStartRunner: every queue with pending or waiting items.
   * (2026-10-20, 0.4.4 logfix F) Only those, and only without a live runner: an idle project starts
   * nothing (it used to start the `default` queue's runner, which then polled an empty queue).
   */
  async autoStart() {
    const names = parse.autoStartLanes(this.lanes);
    // (2026-10-21, v049 dontask_hold) autoStartLanes skips held queues; say which
    // (2026-10-27, integrate: usage forecast review LOW-1) a timed hold is started (its runner waits it out)
    const held = this.lanes.filter((l) => l.hold && !l.hold.until).map((l) => l.name);
    if (held.length) this.event(`auto-start: on hold, not started: ${held.join(', ')}`);
    if (!names.length) {
      this.event(held.length ? 'auto-start: no other queue has a runnable item; no runner started'
        : 'auto-start: no queue has a runnable item; no runner started');
      return;
    }
    for (const name of names) {
      await this.guarded('auto-start runner', () => this.startRunner(undefined, undefined, laneArg(name), { auto: true }));
    }
  }

  dispose() {
    this.poller.stop();
    this._stopGoalTimer();
    this.liveLog.dispose();
    this.taskViews.dispose();
    this.codeLensEmitter.dispose();
    this.view.dispose();
    this.statusItem.dispose();
    this.goalItem.dispose();
    for (const it of this.countItems.values()) it.dispose();
    this.provider.dispose();
    this._disposed = true; // (0.4.4 logfix G) log() drops lines from now on
    this.out.dispose();
  }
}

/** @param {vscode.ExtensionContext} context */
/**
 * (2026-10-21, security review M5) The tree rows of `full` tasks (resourceUri scheme FULL_SCHEME): a red
 * "!!" badge, the row in the error colour and a FULL ACCESS tooltip.
 * @param {any} vs the vscode API
 * (2026-10-26, owner fa388b7b) `plain()` true (claudAutopilot.disableFullAccessColor): the badge and the
 * tooltip stay, the red goes; `refresh()` makes VS Code ask again after the setting changed.
 * @param {() => boolean} [plain]
 */
function fullDecorations(vs, plain = () => false) {
  const changed = vs.EventEmitter ? new vs.EventEmitter() : null;
  return {
    onDidChangeFileDecorations: changed ? changed.event : undefined,
    refresh() { if (changed) changed.fire(undefined); },
    provideFileDecoration(/** @type {any} */ uri) {
      if (!uri || uri.scheme !== FULL_SCHEME) return undefined;
      return { badge: '!!', tooltip: t(format.FULL_WARN), color: plain() ? undefined : new vs.ThemeColor('errorForeground'), propagate: false };
    },
  };
}

/**
 * (2026-10-26) "<channel> build <commit>, <UTC time>, package <x.y.z>" from python/<core>/build_info.json,
 * which build.py writes into the VSIX copy; "package <x.y.z>" without it (a checkout or an older build).
 * @param {string} pythonDir @param {string} pkgVersion
 */
function buildStamp(pythonDir, pkgVersion) {
  /** @type {any} */
  let info = null;
  try {
    info = JSON.parse(fs.readFileSync(path.join(pythonDir, argv.CORE_MODULE, 'build_info.json'), 'utf8'));
  } catch (_none) {
    info = null;
  }
  const built = info && info.channel ? `${info.channel} build ${info.commit}, ${info.built_utc}, ` : '';
  return `${built}package ${pkgVersion}`;
}

function activate(context) {
  const ap = new Autopilot(context);
  instance = ap;
  // (2026-10-26) the full four-part version (the bundled core's) + build.py's stamp; package.json has the VS Code form
  const pkgVersion = context.extension ? context.extension.packageJSON.version : '?';
  ap.log(`${proc.stamp()} activated v${ap._bundledVersion() || pkgVersion} (${buildStamp(ap.pythonDir, pkgVersion)}) from ${context.extensionPath}`);
  /** @param {string} id @param {string} what @param {(...a: any[]) => Promise<void>} fn */
  const reg = (id, what, fn) => context.subscriptions.push(
    vscode.commands.registerCommand(id, (...a) => ap.guarded(what, () => fn(...a))));
  reg('claudAutopilot.add', 'add', () => ap.cmdAdd());
  reg('claudAutopilot.addTask', 'add task', () => ap.cmdAddTask());
  reg('claudAutopilot.addGoal', 'add goal', () => ap.cmdAddGoal());
  reg('claudAutopilot.addPlan', 'add plan', () => ap.cmdAddPlan());
  reg('claudAutopilot.addWithFiles', 'add with files', (uri, uris) => ap.cmdAddWithFiles(uri, uris));
  reg('claudAutopilot.addSelectionAsTask', 'add selection', () => ap.cmdAddSelection());
  reg('claudAutopilot.addFileAsTask', 'add file', () => ap.cmdAddFile());
  reg('claudAutopilot.startRunner', 'start runner', () => ap.cmdStartRunner());
  reg('claudAutopilot.stopRunner', 'stop runner', () => ap.cmdStopRunner());
  reg('claudAutopilot.startProjectRunner', 'start runner', (node) => ap.startProjectRunner(node));
  reg('claudAutopilot.stopProjectRunner', 'stop runner', (node) => ap.stopProjectRunner(node));
  reg('claudAutopilot.refresh', 'refresh', () => ap.poller.trigger());
  reg('claudAutopilot.showLog', 'show log', () => ap.showLog());
  reg('claudAutopilot.openDataDir', 'open data folder', () => ap.openDataDir());
  reg('claudAutopilot.showDetails', 'show details', (item) => ap.showDetails(item));
  reg('claudAutopilot.retryItem', 'retry', (item) => ap.retryItem(item)); // (0.4.4 logfix B)
  reg('claudAutopilot.cancelItem', 'cancel', (item) => ap.itemCommand('cancel', item));
  reg('claudAutopilot.removeItem', 'remove', (item) => ap.itemCommand('remove', item));
  // (2026-10-26, owner task 07254297) the keyboard way to reorder; dragging is lib/move.js
  reg('claudAutopilot.moveToTop', 'move to top', (item) => ap.moveItem(item, 'top'));
  reg('claudAutopilot.moveToBottom', 'move to bottom', (item) => ap.moveItem(item, 'bottom'));
  // (2026-10-21) subtasks under a main task, and deleting a whole status group
  reg('claudAutopilot.addSubtask', 'add subtask', (item) => ap.addSubtask(item));
  reg('claudAutopilot.removeGroup', 'delete group', (node) => ap.removeGroup(node));
  reg('claudAutopilot.pauseItem', 'pause', (item) => ap.itemCommand('pause', item));
  reg('claudAutopilot.resumeItem', 'resume', (item) => ap.itemCommand('resume', item));
  reg('claudAutopilot.openRunLogFolder', 'open run log folder', (item) => ap.openRunLogFolder(item));
  reg('claudAutopilot.statusMenu', 'status menu', () => ap.statusMenu());
  reg('claudAutopilot.goalMenu', 'goal menu', () => ap.goalMenu());
  reg('claudAutopilot.showAllProjects', 'show all projects', () => ap.setAllProjects(true));
  reg('claudAutopilot.showThisProject', 'show this project', () => ap.setAllProjects(false));
  // (2026-10-15, v0.3) live log, queues, reply
  reg('claudAutopilot.itemClick', 'open live log', (node) => ap.itemClick(node));
  reg('claudAutopilot.openLiveLog', 'open task view', (item) => ap.openLiveLog(item));
  reg('claudAutopilot.openRawLog', 'open raw log', (item) => ap.openRawLog(item)); // (2026-10-17, 0.4.0)
  reg('claudAutopilot.newLane', 'new queue', () => ap.newLane());
  reg('claudAutopilot.removeLane', 'remove queue', (node) => ap.removeLane(node));
  reg('claudAutopilot.startLaneRunner', 'start runner', (node) => ap.startLaneRunner(node));
  reg('claudAutopilot.stopLaneRunner', 'stop runner', (node) => ap.stopLaneRunner(node));
  // (2026-10-21, v049 dontask_hold) the queue's hold toggle
  reg('claudAutopilot.holdQueue', 'hold queue', (node) => ap.setHold(node, true));
  reg('claudAutopilot.releaseHold', 'release hold', (node) => ap.setHold(node, false));
  reg('claudAutopilot.addToLane', 'add', (node) => ap.addToLane(node));
  reg('claudAutopilot.replyItem', 'reply', (item) => ap.replyItem(item));
  reg('claudAutopilot.sendReply', 'send reply', (uri) => ap.sendReply(uri));
  // (2026-10-21, edit) change a task's text before it has started
  reg('claudAutopilot.editItem', 'edit task', (item) => ap.editItem(item));
  reg('claudAutopilot.saveEdit', 'save task', (uri) => ap.saveEdit(uri));
  // (2026-10-21, task model) the model of a task's next attempt
  reg('claudAutopilot.changeModel', 'change model', (item) => ap.changeModel(item));
  // (2026-10-21, add panel) the provider, model and effort of a task's next attempt
  reg('claudAutopilot.changeRun', 'change provider, model, effort', (item) => ap.changeRun(item));
  // (2026-10-21, edit reply) take back a reply Claude has not got yet
  reg('claudAutopilot.withdrawReply', 'withdraw reply', (item) => ap.withdrawReply(item));
  // (2026-10-20, 0.4.4 logfix A) the shell-dev allow-list of the project
  reg('claudAutopilot.editAllowedCommands', 'edit allowed commands', (item) => ap.editAllowedCommands(item));
  // (2026-10-21, SPEC_V048_REPORT) a redacted GitHub issue draft; the owner submits it on GitHub
  reg('claudAutopilot.reportIssue', 'report an issue', (item) => ap.reportIssue(item));
  reg('claudAutopilot.sendForFix', 'send errors for a fix', (item) => ap.sendForFix(item));
  // (2026-10-20, 0.4.5) the + defaults: a row's dropdown, the whole menu, and hiding the rows
  reg('claudAutopilot.setAddDefault', 'change a default', (key) => ap.setAddDefault(key));
  reg('claudAutopilot.addDefaults', 'defaults for +', () => ap.addDefaultsMenu());
  reg('claudAutopilot.hideAddPanel', 'close the New Task panel', () => ap.hideAddPanel());
  reg('claudAutopilot.showAddPanel', 'show the New Task panel', () => ap.showAddPanel());
  reg('claudAutopilot.hideAddDefaults', 'hide the defaults', () => ap.hideAddDefaults());
  // (2026-10-21) the Queue view's Settings button and the UI language
  reg('claudAutopilot.openSettings', 'settings', () => ap.settingsMenu());
  reg('claudAutopilot.changeLanguage', 'change the UI language', () => ap.languageMenu());
  reg('claudAutopilot.showStats', 'statistics', () => ap.showStats()); // (2026-10-26, owner task f60cec52)
  reg('claudAutopilot.toggleAdhd', 'toggle ADHD mode', () => ap.toggleAdhd());
  if (vscode.workspace.registerTextDocumentContentProvider) {
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(LOG_SCHEME, ap.liveLog));
    context.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(STATS_SCHEME, ap.statsDoc)); // (2026-10-26)
  }
  if (vscode.window.registerFileDecorationProvider) {
    // (2026-10-26, owner fa388b7b) read from the configuration itself: no order with onConfigChanged
    const deco = fullDecorations(vscode, () => vscode.workspace.getConfiguration(SECTION).get('disableFullAccessColor') === true);
    context.subscriptions.push(vscode.window.registerFileDecorationProvider(deco));
    if (vscode.workspace.onDidChangeConfiguration) {
      context.subscriptions.push(vscode.workspace.onDidChangeConfiguration((e) => {
        // (2026-10-27, i18n) and on a language change: the tooltip is translated
        if (e.affectsConfiguration(`${SECTION}.disableFullAccessColor`) || e.affectsConfiguration(`${SECTION}.language`)) deco.refresh();
      }));
    }
  }
  if (vscode.window.registerWebviewViewProvider) {
    // (2026-10-21, add panel) the "+ Add new task" panel under the Queue view
    context.subscriptions.push(vscode.window.registerWebviewViewProvider(addpanel.VIEW_ID, ap.addPanel));
  }
  // (2026-10-21, owner "move default for bottom") the "Defaults for +" section from the start, before the first poll
  ap._setTree([], null);
  if (vscode.window.registerWebviewPanelSerializer) {
    // (0.4.3) Task views come back after a window reload: validated, then rebuilt like a fresh open
    context.subscriptions.push(vscode.window.registerWebviewPanelSerializer(TASK_VIEW_TYPE, {
      deserializeWebviewPanel: async (/** @type {any} */ panel, /** @type {unknown} */ state) => {
        await ap.taskViews.restore(panel, state);
      },
    }));
  }
  if (vscode.workspace.onDidChangeTextDocument) {
    context.subscriptions.push(vscode.workspace.onDidChangeTextDocument((e) => ap.liveLog.onDocumentChanged(e.document)));
  }
  if (vscode.workspace.onDidCloseTextDocument) {
    context.subscriptions.push(vscode.workspace.onDidCloseTextDocument((doc) => {
      ap.liveLog.onDocumentClosed(doc);
      const key = doc.uri.toString();
      const wasReply = ap.replyDrafts.delete(key);
      const wasEdit = ap.editDrafts.delete(key); // (2026-10-21, edit)
      if (wasReply || wasEdit) ap.codeLensEmitter.fire(undefined);
    }));
  }
  if (vscode.languages && vscode.languages.registerCodeLensProvider && vscode.CodeLens) {
    // The "Send reply" lens on the first line of an open reply draft (untitled documents only).
    // (2026-10-21, edit) and the "Save task" lens of an edit draft
    context.subscriptions.push(vscode.languages.registerCodeLensProvider({ scheme: 'untitled' }, {
      onDidChangeCodeLenses: ap.codeLensEmitter.event,
      provideCodeLenses: (/** @type {any} */ doc) => {
        const e = ap.editDrafts.get(doc.uri.toString());
        if (e) {
          return [new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), {
            title: e.reply ? t('$(save) Save reply to {0}', e.item.id) : t('$(save) Save task {0}', e.item.id), command: 'claudAutopilot.saveEdit', arguments: [doc.uri],
          })];
        }
        const tg = ap.replyDrafts.get(doc.uri.toString());
        if (!tg) return [];
        return [new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), {
          title: t('$(send) Send reply to {0}', tg.item.id), command: 'claudAutopilot.sendReply', arguments: [doc.uri],
        })];
      },
    }));
  }
  // (2026-10-21, scroll) the owner's expand/collapse survives the poll refresh (QueueProvider.setOpen)
  if (ap.view.onDidExpandElement) {
    context.subscriptions.push(ap.view.onDidExpandElement((/** @type {any} */ e) => ap.provider.setOpen(e.element, true)));
  }
  if (ap.view.onDidCollapseElement) {
    context.subscriptions.push(ap.view.onDidCollapseElement((/** @type {any} */ e) => ap.provider.setOpen(e.element, false)));
  }
  context.subscriptions.push(
    ap.view.onDidChangeVisibility((e) => (e.visible ? ap.poller.trigger() : ap.poller.reschedule())),
    vscode.workspace.onDidChangeConfiguration((e) => { if (e.affectsConfiguration(SECTION)) ap.onConfigChanged(); }),
    { dispose: () => ap.dispose() },
  );
  if (vscode.workspace.onDidChangeWorkspaceFolders) {
    context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => ap.onFoldersChanged()));
  }
  vscode.commands.executeCommand('setContext', ALL_PROJECTS_KEY, ap.allProjects);
  ap.poller.start();
  if (ap.settings.autoStartRunner) {
    ap.poller.trigger().then(() => ap.autoStart(),
      (err) => ap.reportError('auto-start runner', err));
  }
  return ap;
}

function deactivate() {
  const ap = instance;
  instance = null;
  return ap ? ap.shutdown() : undefined;
}

module.exports = { activate, deactivate };

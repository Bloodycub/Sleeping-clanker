// @ts-check
// The Task view panels (2026-10-17, 0.4.0, docs/SPEC_V04_CHAT.md). One locked-down webview per item:
// scripts on, but only media/chat.js with a per-render nonce; no command URIs; resources only from
// media/. The `vscode` module is passed in (this file never requires it), like a lib/ module.
// Reading: lib/livelog.js (new bytes only, 2 MB per attempt) with lib/chatmodel.js parseLine per line.
// Rendering: lib/chatmodel.js buildView -> lib/chatview.js diffBlocks -> postMessage({type:'patch'}).
// (0.4.3) restore(): a Task view VS Code brings back after a window reload, validated, then open().
// (0.4.6, docs/SPEC_V046_AUTOPICK.md) _autoPick(): with the chat's "Auto-pick best" box on, a blocked
// question is answered with its best option through the SAME reply path, without widening the profile.
// (2026-10-21) _chatSettings(): the composer's ⚙ "Chat settings" QuickPick (the Queue view's gear has
// the global settings).
// (2026-10-26, modes) Autorun: a task's own `autorun` flag makes _autoPick answer its option questions
// whether or not the chat box is on, and autorunPoll() does the same for a task whose chat is closed.
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const argv = require('./argv');
const parse = require('./parse');
const format = require('./format');
const proc = require('./proc');
const chatmodel = require('./chatmodel');
const chatview = require('./chatview');
const pickers = require('./pickers'); // (2026-10-26) pickers with a close button (X) in the title bar
const markdown = require('./markdown');
const i18n = require('./i18n');
const { ItemLog } = require('./livelog');

const VIEW_TYPE = 'claudAutopilot.taskView';
const LIVE_POLL_MS = 1000;
// (0.4.6, SPEC_V046_AUTOPICK) how many questions one chat answers by itself in a row before it waits
// for the owner again. An owner reply (typed or an option button) re-arms it, as does the box itself.
const AUTO_PICK_MAX = 5;
// (2026-10-26, modes) an Autorun task's cap: automatic answers in a row before the question waits again
const AUTORUN_MAX = 20;
// (2026-10-21) ADHD mode (the Task view's View dropdown): one owner choice for every chat, in globalState
const ADHD_KEY = 'claudAutopilot.taskViewAdhd';
// (2026-10-26, silent run) the "run to continue" messages a full-access task already ran by itself
const AUTO_RUN_KEY = 'claudAutopilot.autoRanCode';
const AUTO_RUN_KEEP = 200;
const AUTO_RUN_SINCE_KEY = 'claudAutopilot.autoRunSince';
// (2026-10-26, M4) the task ids whose one-line "run to continue" commands run without asking (workspaceState)
const AUTO_RUN_NO_ASK_KEY = 'claudAutopilot.autoRunNoAsk';
// (2026-10-27, SEC-L13) the most a Run modal shows; a longer command is offered for copying only
const SHOW_MAX = 2000;
// (2026-10-27, SEC-L13 review) and the most lines: a taller modal does not fit the screen (a native Windows
// dialog does not scroll), so a command with more lines is offered for copying only too
const SHOW_LINES = 40;

/**
 * @typedef {{item: import('./parse').Item, project: string | null, lane: string, laneArg: string | null,
 *   dataDir?: string | null}} Target
 * @typedef {{key: string, t: Target, panel: any, log: ItemLog | null, item: import('./parse').Item | null,
 *   sent: Map<string, string>, timer: any, watcher: any, ready: boolean, head: string, title: string,
 *   order: string, disposed: boolean, init: Promise<void> | null, gone?: boolean, tickFailed?: boolean,
 *   autoPick: boolean | null, autoKey: string, autoCount: number, queued?: boolean, edit?: any, idleNote?: string,
 *   idlePaused?: string}} PanelState
 *   (2026-10-29, F4) `idleNote`: the question this chat showed the "paused: full access" note for; `idlePaused`:
 *   the one that was logged (_idlePause).
 *   (0.4.1) `gone`: `show --json` said the item does not exist (D6); `tickFailed`: the 1 s update
 *   threw once and was logged (D3).
 *   (0.4.6) `autoPick`: this chat's own box (null = follow the setting); `autoKey`: the question it
 *   has already answered by itself; `autoCount`: automatic answers in a row (cap AUTO_PICK_MAX).
 */

/**
 * (0.4.6) One question instance: a new attempt, a new reply or different options make a new key, so
 * one question is answered automatically exactly once.
 * @param {import('./parse').Item} item @param {{n: number, label: string}[]} opts
 */
function questionKey(item, opts) {
  const q = item.blocked_question || { text: '' };
  return JSON.stringify([item.id, item.attempts || 0, (item.replies || []).length, q.text || '',
    opts.map((o) => [o.n, o.label])]);
}

class TaskViews {
  /**
   * @param {any} vscode the vscode module
   * @param {any} ap the Autopilot instance (cliJson, findItem, log, reportError, openRawLog, sendReplyText)
   * @param {string} extensionPath
   */
  constructor(vscode, ap, extensionPath) {
    this.vscode = vscode;
    this.ap = ap;
    this.extensionPath = extensionPath;
    this.mediaDir = path.join(extensionPath, 'media');
    /** @type {Map<string, PanelState>} */
    this.panels = new Map();
    /** (2026-10-21) ADHD mode as set in this session; null = what globalState holds @type {boolean | null} */
    this.adhd = null;
    /** (2026-10-21) Run in terminal: one terminal per shell + folder, reused while it is open @type {Map<string, any>} */
    this.terminals = new Map();
    /** (2026-10-26, silent run) the "run to continue" messages _autoRun already ran @type {Set<string>} */
    this.autoRan = new Set();
    /** (2026-10-26, silent run) when auto-run first started here (globalState keeps the first one) */
    this.autoRunSince = new Date().toISOString();
    /** (2026-10-26, M4) task ids answered "Run and don't ask again" in this session @type {Set<string>} */
    this.autoRunNoAsk = new Set();
    /**
     * (2026-10-26, modes) Autorun per task (lane + id), shared by its open chat and autorunPoll so one
     * question is answered once: the question answered, answers in a row, and whether it stopped.
     * @type {Map<string, {autoKey: string, autoCount: number, stopped: boolean}>}
     */
    this.autorun = new Map();
  }

  /** (2026-10-26, modes) @param {string | null | undefined} lane @param {string} id */
  _autorunRec(lane, id) {
    const key = `${String(lane || argv.DEFAULT_LANE).toLowerCase()}\u0000${id}`;
    let rec = this.autorun.get(key);
    if (!rec) {
      rec = { autoKey: '', autoCount: 0, stopped: false, idlePaused: '' }; // (2026-10-29, F4) idlePaused: _idlePause
      this.autorun.set(key, rec);
    }
    return rec;
  }

  /** (2026-10-27, modes fix LOW) An owner's reply, from any path: the task's Autorun answers again. @param {string | null | undefined} lane @param {string} id */
  rearmAutorun(lane, id) {
    Object.assign(this._autorunRec(lane, id), { autoCount: 0, stopped: false });
  }

  /**
   * (2026-10-26, modes) After a poll of this window's project: an Autorun task that waits with options
   * and has no open chat is answered here, through the same _autoPick (an open chat answers its own).
   * Never awaited by the caller and never throws.
   * @param {import('./parse').Item[]} items
   */
  autorunPoll(items) {
    try {
      for (const item of items || []) {
        if (!item || !item.autorun || item.status !== 'blocked') continue;
        const lane = String(item.lane || argv.DEFAULT_LANE).toLowerCase();
        const open = [...this.panels.values()].some((p) => !p.disposed && p.t.item.id === item.id
          && String(p.t.lane || argv.DEFAULT_LANE).toLowerCase() === lane);
        if (open) continue;
        const t = this.ap._target(item);
        /** @type {PanelState} */
        const st = { key: '', t, panel: null, log: null, item, sent: new Map(), timer: null, watcher: null, ready: false,
          head: '', title: '', order: '', disposed: false, init: null, autoPick: null, autoKey: '', autoCount: 0 };
        this._autoPick(st);
      }
    } catch (err) {
      this.ap.log(`${proc.stamp()} !! ERROR autorun: ${err instanceof Error ? err.message : err}`);
    }
  }

  /** (2026-10-21) Is ADHD mode on? One choice for every Task view, kept in globalState; off by default. */
  adhdOn() {
    if (typeof this.adhd === 'boolean') return this.adhd;
    const gs = this.ap.context && this.ap.context.globalState;
    return !!(gs && gs.get(ADHD_KEY, false) === true);
  }

  /** @param {Target} t */
  static keyOf(t) {
    return `${t.project || ''}|${t.lane}|${t.item.id}`;
  }

  /**
   * The webview options of every Task view, fresh or restored: scripts on (only the nonce'd
   * media/chat.js passes the CSP), no command URIs, no forms, resources only from media/.
   */
  webviewOptions() {
    return { enableScripts: true, enableCommandUris: false, enableForms: false, localResourceRoots: [this.vscode.Uri.file(this.mediaDir)] };
  }

  /**
   * (2026-10-26, new logo) The extension's colour logo (icon.png) on a Task view's editor tab, fresh or
   * restored after a reload. A stub without Uri.joinPath (the unit tests) gets no icon.
   * @param {any} panel
   */
  setIcon(panel) {
    const Uri = this.vscode.Uri;
    if (!Uri || typeof Uri.joinPath !== 'function' || typeof Uri.file !== 'function') return;
    try {
      panel.iconPath = Uri.joinPath(Uri.file(this.extensionPath), 'icon.png');
    } catch (_e) {
      // the panel was closed meanwhile
    }
  }

  /**
   * Open (or reveal) the Task view of an item, beside the editor, focused.
   * (0.4.3) `adopt`: a panel VS Code restored after a window reload (TaskViews.restore). It gets the
   * SAME webview options and HTML as a fresh panel and goes into the same per-item map; when the item
   * already has a panel, the restored one is closed and the existing one revealed.
   * (0.4.6) `opts.autoPick`: the "Auto-pick best" box this chat starts with (a restored panel brings
   * the owner's own choice back); undefined/null = follow `claudAutopilot.autoPickBestOption`.
   * @param {Target} t @param {any} [adopt] @param {{autoPick?: boolean | null}} [opts]
   */
  async open(t, adopt, opts = {}) {
    const vscode = this.vscode;
    const key = TaskViews.keyOf(t);
    const column = vscode.ViewColumn ? vscode.ViewColumn.Beside : undefined;
    const have = this.panels.get(key);
    if (have) {
      if (adopt && adopt !== have.panel) {
        try { adopt.dispose(); } catch (_e) { /* already gone */ }
      }
      have.panel.reveal(column, false);
      return have;
    }
    let panel = adopt;
    if (panel) {
      panel.webview.options = this.webviewOptions();
      panel.title = chatmodel.titleOf(t.item);
    } else {
      panel = vscode.window.createWebviewPanel(VIEW_TYPE, chatmodel.titleOf(t.item), { viewColumn: column, preserveFocus: false },
        { ...this.webviewOptions(), retainContextWhenHidden: false });
    }
    this.setIcon(panel);
    /** @type {PanelState} */
    const st = { key, t, panel, log: null, item: t.item, sent: new Map(), timer: null, watcher: null, ready: false,
      head: '', title: '', order: '', disposed: false, init: null,
      autoPick: typeof opts.autoPick === 'boolean' ? opts.autoPick : null, autoKey: '', autoCount: 0 };
    this.panels.set(key, st);
    panel.webview.html = this._html(panel.webview, chatmodel.titleOf(t.item), t);
    panel.webview.onDidReceiveMessage((/** @type {unknown} */ raw) => this._onMessage(st, raw));
    panel.onDidChangeViewState(() => {
      // retainContextWhenHidden is off: a hidden webview is destroyed and reloads (then says `ready`)
      if (!panel.visible) st.ready = false;
    });
    panel.onDidDispose(() => {
      st.disposed = true;
      this._unwatch(st);
      this.panels.delete(key);
    });
    st.init = this._init(st);
    await st.init;
    return st;
  }

  /** @param {any} webview @param {string} title @param {Target} t */
  _html(webview, title, t) {
    const vscode = this.vscode;
    const uri = (/** @type {string} */ f) => webview.asWebviewUri(vscode.Uri.file(path.join(this.mediaDir, f))).toString();
    return chatview.panelHtml({ cspSource: webview.cspSource, nonce: chatview.makeNonce(), scriptUri: uri('chat.js'),
      styleUri: uri('chat.css'), title, ident: { itemId: t.item.id, project: t.project || null, lane: t.lane },
      lang: i18n.language(), strings: i18n.strings() });
  }

  /**
   * (2026-10-21, UI language) The language changed: every open panel gets fresh HTML in it. The
   * webview reloads, says `ready`, and gets a full patch (host-side labels follow i18n too).
   */
  relocalize() {
    for (const st of this.panels.values()) {
      if (st.disposed) continue;
      st.ready = false;
      st.panel.webview.html = this._html(st.panel.webview, chatmodel.titleOf(st.item || st.t.item), st.t);
    }
  }

  /**
   * (0.4.3) VS Code restores a Task view after a window reload (registerWebviewPanelSerializer in
   * extension.js). The webview state is untrusted: chatview.validateRestoreState checks its shape,
   * then `ap.resolveTaskViewTarget` asks the CORE whether the project is known, the queue exists and
   * the item is in it. Only then is the panel rebuilt exactly like a fresh open (`open(t, panel)`);
   * otherwise it shows the static "no longer available" page and nothing is watched.
   * Never throws. @param {any} panel @param {unknown} state @returns {Promise<PanelState | null>}
   */
  async restore(panel, state) {
    const s = chatview.validateRestoreState(state);
    // (2026-10-27, i18n) `reason` goes to the log (English), `shown` to the page (the UI language)
    let reason = 'The saved view state is not valid.';
    let shown = i18n.t('The saved view state is not valid.');
    if (s) {
      let closed = false; // the owner closed the tab while the core was asked
      try { panel.onDidDispose(() => { closed = true; }); } catch (_e) { closed = true; }
      try {
        const t = await this.ap.resolveTaskViewTarget(s);
        if (closed) return null;
        // (0.4.6) the chat's own Auto-pick box comes back with it (chatview.restoreAutoPick)
        if (t) return await this.open(t, panel, { autoPick: chatview.restoreAutoPick(state) });
        reason = `Task ${s.itemId} is not in that queue any more.`;
        shown = i18n.t('Task {0} is not in that queue any more.', s.itemId);
      } catch (err) {
        const why = err instanceof Error ? err.message : String(err);
        reason = `Task ${s.itemId}: ${why}`;
        shown = i18n.t('Task {0}: {1}', s.itemId, why);
      }
    }
    this.ap.log(`${proc.stamp()} task view: not restored after the reload (${reason})`);
    this._unavailable(panel, shown);
    return null;
  }

  /** The static page; no script, no resource root, no map entry, no timer, no watcher. @param {any} panel @param {string} reason */
  _unavailable(panel, reason) {
    this.setIcon(panel);
    try {
      panel.webview.options = { enableScripts: false, enableCommandUris: false, enableForms: false, localResourceRoots: [] };
      panel.title = i18n.t('Task view (not available)');
      panel.webview.html = chatview.unavailableHtml(reason, i18n.language());
    } catch (_e) {
      // the panel was closed meanwhile
    }
  }

  /** The run folder from the data dir the CORE resolves for (project, lane) (amendment M9). @param {PanelState} st */
  async _init(st) {
    try {
      // (2026-10-26, speed) the data dir the core gave in the last poll for this queue: no `where` spawn
      const known = typeof this.ap.knownDataDir === 'function' ? this.ap.knownDataDir(st.t.project, st.t.lane) : null;
      const dataDir = known
        || (await this.ap.cliJson(argv.whereArgs(), parse.parseWhere, undefined, st.t.project, st.t.laneArg)).dataDir;
      if (st.disposed) return; // (0.4.1 D2) closed while loading: start no timer, no watcher
      st.log = new ItemLog(argv.runLogDir(dataDir, st.t.item.id), { mapLine: chatmodel.parseLine });
      const fresh = await this._item(st, false);
      if (st.disposed) return;
      if (fresh) st.item = fresh;
      st.log.refresh({ final: !this._running(st) });
      this._sync(st);
      this._push(st, true);
      // (0.4.6) no auto-pick here: the webview is still loading, so its note would be posted into
      // nothing. The `ready` message does it (and `onPoll` for every later question).
    } catch (err) {
      if (st.disposed) return;
      this.ap.log(`${proc.stamp()} !! ERROR task view ${st.t.item.id}: ${err instanceof Error ? err.message : err}`);
      this._post(st, { type: 'error', text: i18n.t('The run files could not be read: {0}', err instanceof Error ? err.message : String(err)) });
    }
  }

  /** @param {PanelState} st */
  _running(st) {
    return !!st.item && st.item.status === 'running';
  }

  /**
   * The item from the last poll, else `show --json`. (0.4.1 D6) After the core said the item does not
   * exist (deleted), it is not asked again: logged once, then null.
   * (2026-10-27, modes fix round 2) `fresh`: always `show` (an Autorun answer: another window may have answered).
   * @param {PanelState} st @param {boolean} quiet @param {boolean} [fresh]
   */
  async _item(st, quiet, fresh = false) {
    const known = fresh ? null : this.ap.findItem(st.t.project, st.t.lane, st.t.item.id);
    if (known) return known;
    if (st.gone) return null;
    try {
      return await this.ap.cliJson(argv.itemArgs('show', st.t.item.id), parse.parseShow, undefined, st.t.project, st.t.laneArg, { quiet });
    } catch (err) {
      if (parse.isItemNotFound(err)) {
        st.gone = true;
        this.ap.log(`${proc.stamp()} task view: item ${st.t.item.id} is not in the queue any more; this view stops asking for it`);
      } else {
        this.ap.log(`${proc.stamp()} WARN task view: item ${st.t.item.id}: ${err instanceof Error ? err.message : err}`);
      }
      return null;
    }
  }

  /** After every queue poll: new status, new replies; start or stop watching; push the changes. */
  async onPoll() {
    for (const st of [...this.panels.values()]) {
      if (st.init) await st.init;
      const fresh = await this._item(st, true);
      if (st.disposed) continue;
      // (2026-10-26) a failed `show` (busy or slow core) is no news: keep the last item. Taking null
      // stopped a RUNNING task's watch and flushed its half-written last line as final; the rest of
      // that line came later as a second fragment, so the event was lost until the view reopened.
      if (!fresh && !st.gone) continue;
      st.item = fresh;
      const wasWatching = !!st.timer;
      this._sync(st);
      if (st.log) st.log.refresh({ final: wasWatching && !st.timer });
      this._push(st, false);
      this._autoPick(st); // (0.4.6) not awaited: the poll must not wait for a reply or a click
      this._autoRun(st); // (2026-10-26, silent run) a full-access task's "run to continue" command
    }
  }

  /** Watch the run folder while the item runs. @param {PanelState} st */
  _sync(st) {
    if (st.disposed) return; // (0.4.1 D2) a closed panel never starts a timer or a watcher
    const running = this._running(st);
    if (running && !st.timer && st.log) {
      const log = st.log;
      const tick = () => {
        // (0.4.1 D3) an exception here would escape a timer on the extension host: log it once
        try {
          if (log.refresh()) this._push(st, false);
        } catch (err) {
          if (!st.tickFailed) {
            st.tickFailed = true;
            this.ap.log(`${proc.stamp()} !! ERROR task view ${st.t.item.id} update: ${err instanceof Error ? err.message : err}`);
          }
        }
      };
      st.timer = setInterval(tick, LIVE_POLL_MS);
      if (st.timer && typeof st.timer.unref === 'function') st.timer.unref();
      try {
        const vscode = this.vscode;
        if (vscode.workspace.createFileSystemWatcher && vscode.RelativePattern && fs.existsSync(log.runDir)) {
          const w = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(vscode.Uri.file(log.runDir), '*'));
          w.onDidChange(tick);
          w.onDidCreate(tick);
          st.watcher = w;
        }
      } catch (err) {
        this.ap.log(`${proc.stamp()} WARN task view watcher: ${err instanceof Error ? err.message : err} (the 1 s poll continues)`);
      }
    } else if (!running && st.timer) {
      this._unwatch(st);
    }
  }

  /** @param {PanelState} st */
  _unwatch(st) {
    if (st.timer) clearInterval(st.timer);
    st.timer = null;
    if (st.watcher) st.watcher.dispose();
    st.watcher = null;
  }

  /** The view-model of a panel (also used by tests). @param {PanelState} st */
  view(st) {
    const lane = st.t.lane && st.t.lane !== argv.DEFAULT_LANE ? format.laneLabel(st.t.lane) : format.laneLabel(argv.DEFAULT_LANE);
    // (2026-10-21, owner) the header says which queue this view reads: shared / another project / a lane
    const queueLabel = chatmodel.queueLabelOf({ project: st.t.project, lane: st.t.lane, ownRoot: this.ap.projectRoot });
    // (2026-10-27, limit_continue fix LOW-4) the item's queue on a usage-limit hold, as the last poll saw it
    // (fix round 2) another project's queue: its row of the all-projects list (null = the shared queue)
    const other = st.t.project === this.ap.projectRoot ? null
      : (this.ap.rows || []).find((/** @type {any} */ r) => (st.t.project === null ? r.shared : r.root === st.t.project));
    const row = (st.t.project === this.ap.projectRoot ? this.ap.lanes || [] : (other && other.lanes) || [])
      .find((/** @type {any} */ l) => String(l.name).toLowerCase() === String(st.t.lane || argv.DEFAULT_LANE).toLowerCase());
    return chatmodel.buildView({ item: st.item, attempts: st.log ? st.log.views() : [], laneLabel: lane, queueLabel,
      limitHeld: !!(row && row.hold && row.hold.reason === 'limit'),
      parallel: (row && row.parallel) || null, // (2026-10-29, integrate round 9) the header: why it runs alone
      autoPick: this._autoPickView(st), changesQuestion: this.changesQuestion(),
      quickPrompts: this.quickPrompts(),
      // (2026-10-26, owner fa388b7b) a plain "full access" note instead of the red badge
      fullPlain: !!(this.ap.settings && this.ap.settings.disableFullAccessColor) });
  }

  /** (2026-10-21) What the "What did you change?" button sends; '' = no button. */
  changesQuestion() {
    const s = this.ap.settings;
    return s && typeof s.changesQuestion === 'string' ? s.changesQuestion : '';
  }

  /**
   * (2026-10-21, subagents) The host's "runners at once" label ("No limit" / "3 at once"); '' when the
   * host does not have the limiter (an older host, a test stub), like _languageDescription above.
   */
  runnerLimitLabel() {
    return typeof this.ap.runnerLimitLabel === 'function' ? this.ap.runnerLimitLabel() : '';
  }

  /** (2026-10-21) The quick-prompt buttons' texts (claudAutopilot.quickPrompts); [] = none. @returns {string[]} */
  quickPrompts() {
    const s = this.ap.settings;
    return s && Array.isArray(s.quickPrompts) ? s.quickPrompts.filter((/** @type {unknown} */ t) => typeof t === 'string') : [];
  }

  /**
   * (2026-10-26, modes) The composer's auto-pick state: an Autorun task is always on, with its own cap.
   * @param {PanelState} st
   */
  _autoPickView(st) {
    const item = st.item || st.t.item;
    if (item && item.autorun) {
      const rec = this._autorunRec(st.t.lane, item.id);
      return { on: true, stopped: rec.stopped, autorun: true };
    }
    return { on: this.autoPickOn(st), stopped: st.autoCount >= AUTO_PICK_MAX };
  }

  /**
   * (0.4.6) Is this chat allowed to answer a blocked question by itself? The chat's own box wins;
   * `null` (never touched here) follows `claudAutopilot.autoPickBestOption`, which is off by default.
   * @param {PanelState} st
   */
  autoPickOn(st) {
    if (typeof st.autoPick === 'boolean') return st.autoPick;
    const s = this.ap.settings;
    return !!(s && s.autoPickBestOption);
  }

  /**
   * (0.4.6, SPEC_V046_AUTOPICK) With the box on, answer a blocked question with its BEST option:
   * `format.questionOptions` puts the recommended one first and keeps the core's "best first" order
   * otherwise, so that is option index 0. Rules:
   * - exactly once per question (`questionKey`), and only while the core would accept a reply;
   * - at most AUTO_PICK_MAX in a row for one chat; after that the question waits for the owner
   *   (an owner reply, or unchecking and checking the box, re-arms it);
   * - the reply text is the same `Option <n>: <label>` an option button sends, built from the HOST's
   *   copy of the item, and the profile is always `null` = keep the task's own profile: an automatic
   *   answer never widens permissions, even for an option that asks for a shell.
   * Never awaited by its callers (it can wait for a Start Runner click) and never throws.
   * @param {PanelState} st
   */
  async _autoPick(st) {
    try {
      // (2026-10-26, modes) an Autorun task answers whatever the box says, with its shared record and cap
      const autorun = !!(st.item && st.item.autorun);
      // (2026-10-26, write while working) the owner's queued message wins over auto-pick (and Autorun)
      if (st.disposed || !autorun && !this.autoPickOn(st) || st.queued) return;
      const item = st.item;
      // (2026-10-27, U4, SPEC_V05_PARALLEL 8.7) a merge question: only `merge <id> --let-claude --gen <gen>`, and
      // only when let-claude is its FIRST action; take-task, discard and retry are never picked automatically
      if (item && item.status === 'blocked' && item.blocked_question && item.blocked_question.kind === 'merge') {
        await this._autoMerge(st, item, autorun);
        return;
      }
      if (!item || item.status !== 'blocked' || !argv.canReply(item)) return;
      const opts = format.questionOptions(item);
      if (!opts.length) return;
      const key = questionKey(item, opts);
      const rec = autorun ? this._autorunRec(st.t.lane, item.id) : st;
      const max = autorun ? AUTORUN_MAX : AUTO_PICK_MAX;
      if (rec.autoKey === key) return;
      if (this._idlePause(st, item, rec, key, autorun)) return; // (2026-10-29, F4, SEC-L22) before the key is taken
      rec.autoKey = key;
      // (2026-10-27, modes fix MED) Autorun never answers a question that came back (item.repeat_block) or whose
      // best option needs a shell this profile lacks: the resumed session would only ask it again
      const repeated = autorun && (item.repeat_block || 0) > 0;
      const noShell = autorun && !repeated && format.optionWantsShell(opts[0]) && argv.NO_SHELL_PROFILES.includes(item.profile || 'safe');
      const back = repeated ? 'autorun: the same question came back; it waits for you'
        : noShell ? `autorun: the best option needs a shell the ${item.profile || 'safe'} profile does not have; it waits for you` : '';
      if (back) {
        /** @type {{stopped: boolean}} */ (rec).stopped = true;
        this.ap.log(`${proc.stamp()} task view ${item.id}: ${back}`);
        // (2026-10-27, integrate: i18n) the log stays English; the chat's note is in the UI language
        const shown = repeated ? i18n.t('autorun: the same question came back; it waits for you')
          : i18n.t('autorun: the best option needs a shell the {0} profile does not have; it waits for you', item.profile || 'safe');
        this._post(st, { type: 'autoPicked', ok: false, text: '', error: shown, autorun });
        this._push(st, false);
        return;
      }
      if (rec.autoCount >= max) {
        const why = autorun ? `autorun stopped after ${max} automatic answers in a row; this question waits for you`
          : `auto-pick stopped after ${max} automatic answers in a row; this question waits for you`;
        if (autorun) /** @type {{stopped: boolean}} */ (rec).stopped = true;
        this.ap.log(`${proc.stamp()} task view ${item.id}: ${why}`);
        // (2026-10-27, i18n) the log stays English; the chat's note is in the UI language
        const shown = autorun ? i18n.t('autorun stopped after {0} automatic answers in a row; this question waits for you', max)
          : i18n.t('auto-pick stopped after {0} automatic answers in a row; this question waits for you', max);
        this._post(st, { type: 'autoPicked', ok: false, text: '', error: shown, ...(autorun ? { autorun } : {}) });
        if (autorun) this._push(st, false);
        return;
      }
      rec.autoCount += 1;
      if (autorun) /** @type {{stopped: boolean}} */ (rec).stopped = false;
      const best = opts[0];
      const note = `${format.optionReplyText(best)}${best.recommended ? ' (recommended)' : ''}`;
      const kept = format.optionWantsShell(best) ? `; kept the ${item.profile || 'safe'} profile` : '';
      this.ap.event(autorun ? `autorun picked ${note} for ${item.id} (autorun ${rec.autoCount}/${max}${kept})`
        : `auto-picked ${note} for ${item.id} (Task view, auto-pick ${rec.autoCount}/${max}${kept})`);
      // (2026-10-27, i18n) the chat's note: "Option N: label" stays English (it is the reply Claude reads),
      // the "(recommended)" tag is in the UI language
      const shown = `${format.optionReplyText(best)}${best.recommended ? ` ${i18n.t('(recommended)')}` : ''}`;
      await this._reply(st, (fresh) => format.choiceReplyText(fresh, best.n), null, { auto: true, note: shown, autorun, key });
    } catch (err) {
      // _reply reports its own failures; this is only the belt for an unexpected one
      this.ap.log(`${proc.stamp()} !! ERROR task view auto-pick ${st.t.item.id}: ${err instanceof Error ? err.message : err}`);
    }
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.7) Auto-pick / Autorun for a merge question: when its first action is
   * let-claude, answer `merge <id> --let-claude --gen <gen>` once per (item, gen), within the same caps as an
   * option pick; any other first action waits for the owner (take-task and discard are never automatic).
   * @param {PanelState} st @param {import('./parse').Item} item @param {boolean} autorun
   */
  async _autoMerge(st, item, autorun) {
    const q = item.blocked_question;
    if (!q || q.kind !== 'merge' || !Array.isArray(q.actions) || q.actions[0] !== 'let-claude' || !Number.isInteger(q.gen)) return;
    const gen = /** @type {number} */ (q.gen);
    const key = `merge\u0000${item.id}\u0000${gen}`;
    const rec = autorun ? this._autorunRec(st.t.lane, item.id) : st;
    const max = autorun ? AUTORUN_MAX : AUTO_PICK_MAX;
    if (rec.autoKey === key) return;
    if (this._idlePause(st, item, rec, key, autorun)) return; // (2026-10-29, F4, SEC-L22) let-claude resumes a full task
    rec.autoKey = key;
    if (rec.autoCount >= max) {
      if (autorun) /** @type {{stopped: boolean}} */ (rec).stopped = true;
      this.ap.log(`${proc.stamp()} task view ${item.id}: auto-pick stopped after ${max} automatic answers in a row; the merge question waits for you`);
      const shown = autorun ? i18n.t('autorun stopped after {0} automatic answers in a row; this question waits for you', max)
        : i18n.t('auto-pick stopped after {0} automatic answers in a row; this question waits for you', max);
      this._post(st, { type: 'autoPicked', ok: false, text: '', error: shown, ...(autorun ? { autorun } : {}) });
      return;
    }
    rec.autoCount += 1;
    // (2026-10-29, F4 fix round 1) like _autoPick: an automatic answer clears an earlier stop (an idle pause's)
    if (autorun) /** @type {{stopped: boolean}} */ (rec).stopped = false;
    this.ap.event(`${autorun ? 'autorun' : 'auto-picked'} merge --let-claude --gen ${gen} for ${item.id} (${rec.autoCount}/${max})`);
    const shown = format.mergeActionLabel('let-claude', true);
    let error = '';
    try {
      // (2026-10-27, U4 fix round 1) `autorun`: as a reply, only an Autorun answer starts a runner by itself
      await this.ap.mergeAction(this._target(st), 'let-claude', gen, { auto: true, autorun });
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      if (autorun) /** @type {{stopped: boolean}} */ (rec).stopped = true;
    }
    this._post(st, { type: 'autoPicked', ok: !error, text: shown, error, ...(autorun ? { autorun } : {}) });
    this._push(st, false);
  }

  /**
   * (2026-10-29, F4, SEC-L22 owner "Kysy aina full-tehtävissä") A full-access task while a reply to it would need the
   * idle FULL ACCESS confirmation (Autopilot.idleFullCheck: the one condition confirmIdleFull asks on): Auto-pick and
   * Autorun do not answer it. The question keeps its buttons (an Autorun task's come back) and the chat gets one note
   * per question. Checked on every pass and before the question's key is taken, so once the owner says yes (the
   * reply's "Yes, with full access") or uses the queue, the next pass answers again. True = paused, nothing sent;
   * an idleFullCheck that throws sends nothing either (the caller's catch).
   * @param {PanelState} st @param {import('./parse').Item} item
   * @param {{stopped?: boolean, idlePaused?: string}} rec @param {string} key @param {boolean} autorun
   */
  _idlePause(st, item, rec, key, autorun) {
    if (item.profile !== argv.FULL_PROFILE || !this.ap.idleFullCheck(st.t.project, st.t.lane, { item }).ask) return false;
    const was = !!rec.stopped;
    if (autorun) rec.stopped = true;
    if (rec.idlePaused !== key) {
      rec.idlePaused = key;
      this.ap.log(`${proc.stamp()} task view ${item.id}: ${autorun ? 'autorun' : 'auto-pick'} paused: full access needs your OK after an idle period`);
    }
    const note = st.idleNote !== key;
    if (note) {
      st.idleNote = key;
      this._post(st, { type: 'autoPicked', ok: false, text: '', paused: true, ...(autorun ? { autorun } : {}),
        error: autorun ? i18n.t('Autorun paused: full access needs your OK after an idle period')
          : i18n.t('Auto-pick paused: full access needs your OK after an idle period') });
    }
    if (note || (autorun && !was)) this._push(st, false);
    return true;
  }

  /**
   * (2026-10-27, U4, 8.6) The merge card the chat shows now (the host's own copy: buildView renders st.item;
   * no item read yet = no card). @param {PanelState} st
   */
  _mergeCard(st) {
    return st.item ? format.mergeCardModel(st.item) : null;
  }

  /**
   * Post what changed since the last post (everything after a reset). The title follows the status.
   * @param {PanelState} st @param {boolean} reset
   */
  _push(st, reset) {
    if (st.disposed || !st.panel) return;
    const v = this.view(st);
    if (v.title !== st.title) {
      st.title = v.title;
      st.panel.title = v.title;
    }
    if (!st.ready) return; // the webview asks with `ready` when it (re)loads
    if (reset) st.sent.clear();
    const head = JSON.stringify([v.header, v.composer]);
    const { upserts, order } = chatview.diffBlocks(v, st.sent);
    const changedHead = head !== st.head;
    st.head = head;
    const orderSig = order.join('\n');
    const changedOrder = orderSig !== st.order;
    st.order = orderSig;
    if (!reset && !upserts.length && !changedHead && !changedOrder) return;
    this._post(st, { type: 'patch', reset, adhd: this.adhdOn(), header: v.header, composer: v.composer, upserts, order: changedOrder || reset ? order : null });
  }

  /** @param {PanelState} st @param {any} msg */
  _post(st, msg) {
    if (st.disposed || !st.panel) return; // (2026-10-26, modes) autorunPoll's state has no panel
    try {
      const p = st.panel.webview.postMessage(msg);
      if (p && typeof p.then === 'function') p.then(undefined, () => {});
    } catch (_e) {
      // a disposed webview: nothing to do
    }
  }

  /**
   * Every message from the webview goes through chatview.validateMessage first.
   * @param {PanelState} st @param {unknown} raw
   */
  async _onMessage(st, raw) {
    // (2026-10-27, U4, 8.6) a merge/compare answer is checked against the card the host shows now
    const msg = chatview.validateMessage(raw, this._mergeCard(st));
    if (!msg) {
      const type = raw && typeof raw === 'object' ? String(/** @type {any} */ (raw).type).slice(0, 40) : typeof raw;
      this.ap.log(`${proc.stamp()} WARN task view: dropped a message from the webview (type ${type})`);
      return;
    }
    if (msg.type === 'ready') {
      st.ready = true;
      if (st.init) await st.init;
      if (st.log) st.log.refresh({ final: !this._running(st) });
      this._push(st, true);
      // (0.4.6) a question that is already waiting when the chat opens (or comes back after being
      // hidden); not awaited, because the reply may wait for a Start Runner click
      st.idleNote = ''; // (2026-10-29, F4) a (re)loaded webview has lost the live note: _idlePause posts it again
      this._autoPick(st);
      this._autoRun(st);
      return;
    }
    if (msg.type === 'openRaw') {
      await this.ap.guarded('open raw log', () => this.ap.openRawLog(this._target(st)));
      return;
    }
    if (msg.type === 'openLink') {
      const vscode = this.vscode;
      await this.ap.guarded('open link', async () => {
        // (2026-10-26, security L1) a link's text can name another host than its target: a modal shows
        // the full URL and its host first, and only Open opens it. https only and no user:password@
        // (chatview.httpsOnly) are checked again here.
        const href = chatview.httpsOnly(msg.href);
        if (!href || !vscode.window.showWarningMessage) return;
        const open = { title: i18n.t('Open') };
        const cancel = { title: i18n.t('Cancel'), isCloseAffordance: true };
        const pick = await vscode.window.showWarningMessage(i18n.t('Sleeping Clanker: open this link in your browser?'),
          { modal: true, detail: `${href}\n\n${i18n.t('Host: {0}', new URL(href).host)}` }, open, cancel);
        if (!pick || pick === cancel || pick.title !== open.title) {
          this.ap.log(`${proc.stamp()} task view: link not opened (cancelled)`);
          return;
        }
        if (vscode.env && vscode.env.openExternal && vscode.Uri.parse) await vscode.env.openExternal(vscode.Uri.parse(href));
      });
      return;
    }
    if (msg.type === 'autoPick') {
      this._setAutoPick(st, msg.on);
      return;
    }
    if (msg.type === 'queued') {
      st.queued = msg.on; // (2026-10-26, write while working) the owner's queued message wins over auto-pick
      return;
    }
    if (msg.type === 'autorun') {
      // (2026-10-27, modes fix round 2) the composer's Autorun box: `edit --autorun`, also while it runs
      await this.ap.guarded('autorun', () => this._setAutorun(st, msg.on));
      st.head = ''; // the box shows the item's value again, also after a refused edit
      this._push(st, false);
      return;
    }
    if (msg.type === 'maxAgents') {
      // (2026-10-21, subagents) the bottom bar's dropdown: `edit ID --max-agents=<value>` in the core
      await this.ap.guarded('subagent limit', () => this.ap.setMaxAgents(this._target(st), msg.value));
      return;
    }
    if (msg.type === 'viewMode') {
      await this.setAdhd(msg.adhd, st);
      return;
    }
    if (msg.type === 'saveEdit') {
      // (2026-10-26, edit in box) Send while the box holds an edit: `edit ID --text` / `--reply-text`
      // on the item the host fetched for editText (never an id from the webview)
      const target = st.edit;
      let error = '';
      if (!target) error = i18n.t('Nothing is being edited; press Edit task… again.');
      else {
        try {
          await this.ap.saveEditText(target, msg.text);
          st.edit = null;
        } catch (e) {
          error = e instanceof Error ? e.message : String(e);
        }
      }
      this._post(st, target ? { type: 'editResult', ok: !error, error } : { type: 'editResult', ok: false, error, stale: true });
      return;
    }
    if (msg.type === 'cancelEdit') {
      st.edit = null;
      return;
    }
    if (msg.type === 'chatSettings') {
      await this.ap.guarded('chat settings', () => this._chatSettings(st));
      return;
    }
    if (msg.type === 'merge') {
      // (2026-10-27, U4, 8.6) a merge card button: `merge <id> --<action> --gen <gen>` (the host confirms
      // take-task, discard and undo first; a refusal, e.g. "the question changed", is shown as an error)
      await this.ap.guarded('merge', () => this.ap.mergeAction(this._target(st), msg.action, msg.gen));
      this._push(st, false);
      return;
    }
    if (msg.type === 'compare') {
      // (2026-10-27, U4, 8.6) Preview: the preview copy of one conflicted file, beside this view (no diff command)
      await this.ap.guarded('merge preview', () => this.ap._openMergePreview(this._target(st), msg.path));
      return;
    }
    if (msg.type === 'action') {
      // (2026-10-20, 0.4.4 logfix A) the blocked item's "Retry with shell-dev" / "Edit allowed commands…"
      // (2026-10-21, v049 dontask_hold) a denial block's actions read the host's own copy of the item
      const cur = st.item || st.t.item;   // the last poll's copy, else the one the view opened with
      const block = cur && cur.status === 'blocked' ? cur.denial_block : null;
      if (msg.id === 'retry-shell-dev') {
        await this.ap.guarded('retry with shell-dev', () => this.ap.retryWithProfile(this._target(st), 'shell-dev'));
      } else if (msg.id === 'edit-allowed') {
        // pre-filled with the denial's first allow-list entry; the owner still confirms it
        const prefill = block && block.allow.length ? block.allow[0] : null;
        await this.ap.guarded('edit allowed commands', () => this.ap.editAllowedCommands(this._target(st), prefill));
      } else if (msg.id === 'retry-suggested') {
        if (!block || (!block.profile && !block.allow.length)) {
          this._post(st, { type: 'error', text: i18n.t('This task is not blocked by a denial any more; nothing was done.') });
        } else {
          await this.ap.guarded('retry', () => this.ap.retryWithProfile(this._target(st), block.profile));
        }
      } else if (msg.id === 'copy-allow') {
        const entry = block && typeof msg.n === 'number' ? block.allow[msg.n] : undefined;
        if (!entry) {
          this._post(st, { type: 'error', text: i18n.t('That allowed-command entry is not on this task any more; nothing was copied.') });
        } else {
          await this.vscode.env.clipboard.writeText(entry);
          if (this.vscode.window.setStatusBarMessage) this.vscode.window.setStatusBarMessage(i18n.t('Sleeping Clanker: copied "{0}"', entry), 3000);
        }
      } else if (msg.id === 'edit-task' || msg.id === 'edit-reply') {
        // (2026-10-26, edit in box) the text goes back into the reply box; Send there saves it (saveEdit)
        const what = msg.id === 'edit-task' ? 'edit task' : 'edit reply';
        await this.ap.guarded(what, async () => {
          const src = await this.ap.editSource(this._target(st));
          if (src.text.length > chatview.REPLY_MAX) {
            await this.ap.editItem(this._target(st)); // too long for the box: the New Task panel's edit box
            return;
          }
          st.edit = src.target;
          // (2026-10-26, edit paused) `restart`: a started paused task; Save starts it over
          this._post(st, { type: 'editText', text: src.text, reply: !!src.target.reply, ...(src.target.restart ? { restart: true } : {}) });
        });
      } else if (msg.id === 'withdraw-reply') {
        await this.ap.guarded('withdraw reply', () => this.ap.withdrawReply(this._target(st)));
      } else if (msg.id === 'open-copy') {
        // (2026-10-27, U4, 8.6) the task's private copy, from the host's own copy of the item
        const w = cur && cur.worktree && cur.worktree.state !== 'removed' ? cur.worktree.path : null;
        if (!w) this._post(st, { type: 'error', text: i18n.t('This task has no private copy any more.') });
        else {
          await this.ap.guarded('open copy', async () => {
            // (2026-10-27, U4 fix round 1) a path from queue.json: openExternal on a file would launch it; only a real folder
            const s = fs.lstatSync(w, { throwIfNoEntry: false });
            if (!s || !s.isDirectory() || s.isSymbolicLink()) throw new Error(i18n.t('{0} is not a folder.', w));
            await this.ap._openExternal(w);
          });
        }
      } else if (msg.id === 'start-now') {
        // (2026-10-21, start now) to the front of its queue, then that queue's runner
        await this.ap.guarded('start now', () => this.ap.startNow(this._target(st)));
      } else if (msg.id === 'ask-changes') {
        // (2026-10-21) the setting's text, read here: the webview sends only the id; the profile is kept
        st.autoCount = 0;
        await this._reply(st, (item) => {
          // (2026-10-21, tester round 2 L-5) the status as it is now (the fresh item _reply just read),
          // not as the webview saw it: a forged or stale message never sends this to a blocked or
          // paused item (chatmodel offers the action on neither)
          // (2026-10-27, i18n) these reasons are shown in the chat ("Not sent: …"): the UI language
          if (item.status === 'blocked' || item.status === 'paused') {
            throw new Error(i18n.t('"What did you change?" is not offered on a {0} item; nothing was sent', i18n.t(item.status)));
          }
          const q = this.changesQuestion();
          if (!q) throw new Error(i18n.t('claudAutopilot.changesQuestion is empty; nothing was sent'));
          return q;
        }, null);
      }
      return;
    }
    if (msg.type === 'copyCode' || msg.type === 'runCode') {
      await this.ap.guarded(msg.type === 'copyCode' ? 'copy code' : 'run code in terminal', () => this._code(st, msg));
      return;
    }
    if (msg.type === 'attach') {
      await this.ap.guarded('add picture or file', () => this._attach(st));
      return;
    }
    if (msg.type === 'prompt') {
      // (2026-10-21) a quick-prompt button: the host's own copy of the setting, only while it still
      // shows the text the owner clicked; the profile is the dropdown's, as for a typed reply
      st.autoCount = 0;
      await this._reply(st, () => {
        // (2026-10-27, i18n) `p`, not `t` (that name is i18n's)
        const p = this.quickPrompts()[msg.i];
        if (p !== msg.text) throw new Error(i18n.t('that quick prompt is not in claudAutopilot.quickPrompts any more; nothing was sent'));
        return p;
      }, msg.profile);
      return;
    }
    // an owner reply re-arms the automatic answers (0.4.6)
    if (msg.type === 'reply' || msg.type === 'choose') st.autoCount = 0;
    if (msg.type === 'reply') await this._reply(st, () => msg.text, msg.profile);
    // (0.4.1) an option button: the text comes from the host's own copy of the item, never the webview
    if (msg.type === 'choose') await this._reply(st, (item) => format.choiceReplyText(item, msg.n), msg.profile);
  }

  /**
   * (2026-10-21) A code block's Copy / Run in terminal button. Only a block this chat really shows
   * (chatview.codeShown on what the host sent) is copied or run. Run: the task's folder, the shell of
   * chatview.shellFor (null = the default terminal), one reused terminal per shell + folder.
   * (security review L7) One line: only after a modal that shows the cleaned command
   * (chatview.cleanCode); it is sent with Enter. (tester round 2 M-2) Several lines never go through
   * sendText (VS Code turns each newline into an Enter, so every line but the last would run): they are
   * copied to the clipboard and the terminal is focused, for the owner to paste and check.
   * (2026-10-26, silent run) `msg.silent`, `claudAutopilot.runCodeSilently` or `auto` (_autoRun): a hidden
   * terminal instead (_runSilently; (2026-10-26, M4) `auto`: a new, visible one after _autoAsk).
   * @param {PanelState} st @param {{type: 'copyCode' | 'runCode', text: string, lang: string, silent?: boolean}} msg
   * @param {boolean} [auto]
   */
  async _code(st, msg, auto = false) {
    const vscode = this.vscode;
    const id = st.t.item.id;
    if (!chatview.codeShown(st.sent.values(), msg.text)) {
      this.ap.log(`${proc.stamp()} WARN task view ${id}: a code block that is not in this chat; nothing was ${msg.type === 'copyCode' ? 'copied' : 'run'}`);
      this._post(st, { type: 'error', text: i18n.t('That code block is not in this chat any more; nothing was done.') });
      return;
    }
    // (2026-10-21, security review L7) zero-width/bidi/control characters out: what is shown is what runs
    const code = chatview.cleanCode(msg.text);
    const text = code.text;
    if (msg.type === 'copyCode') {
      await vscode.env.clipboard.writeText(text);
      if (vscode.window.setStatusBarMessage) vscode.window.setStatusBarMessage(i18n.t('Sleeping Clanker: code copied'), 3000);
      return;
    }
    const item = st.item || st.t.item;
    const cwd = [item.cwd, st.t.project].find((d) => {
      try {
        return !!d && fs.statSync(d).isDirectory();
      } catch (_e) {
        return false;
      }
    }) || undefined;
    const shell = chatview.shellFor(msg.lang, text);
    const where = `${shell || 'default shell'}, ${cwd || 'no folder'}`;
    // (2026-10-27, i18n) `where` goes to the log (English), `place` to the dialogs (the UI language)
    const place = `${shell || i18n.t('default shell')}, ${cwd || i18n.t('no folder')}`;
    const lines = text.split('\n').length;
    const hiddenNote = code.removed ? i18n.t('{0} hidden character(s) (zero-width, bidi, tag or control) were removed.', code.removed) : '';
    const s = this.ap.settings;
    if (msg.silent === true || !!(s && s.runCodeSilently) || auto) {
      await this._runSilently(st, { text, shell, cwd, multiline: code.multiline, lines, hiddenNote, removed: code.removed, auto });
      return;
    }
    // (2026-10-27, SEC-L13) a command longer than the modal shows is never run from it: copy only
    // (several lines are only copied anyway, below)
    if (!code.multiline && text.length > SHOW_MAX) {
      await this._copyOnly(st, text, place, hiddenNote);
      return;
    }
    if (!code.multiline) {
      // (2026-10-21, security review L7) a modal that shows the exact command first
      const shown = text;
      const notes = [i18n.t('It is sent to the terminal with Enter, so it runs at once.'), hiddenNote].filter(Boolean).join('\n');
      const button = i18n.t('Run in terminal'); // translated once: the pick is compared with this same value
      const pick = vscode.window.showWarningMessage ? await vscode.window.showWarningMessage(
        i18n.t('Sleeping Clanker: run this command from the chat ({0})?', place),
        { modal: true, detail: `${notes}\n\n${shown}` }, button) : undefined;
      if (pick !== button) {
        this.ap.log(`${proc.stamp()} task view ${id}: run in terminal cancelled; nothing was sent`);
        return;
      }
    }
    const term = this._terminal(shell, cwd);
    if (code.multiline) {
      // (2026-10-21, tester round 2 M-2) never sendText: VS Code turns every "\n" into an Enter, so all
      // lines but the last would run at once. Copy, focus the terminal, and let the owner paste and check.
      await vscode.env.clipboard.writeText(text);
      term.show(false);
      const note = `${i18n.t('Sleeping Clanker: Copied {0} lines — paste and check before pressing Enter.', lines)}${hiddenNote ? ` ${hiddenNote}` : ''}`;
      if (vscode.window.showInformationMessage) vscode.window.showInformationMessage(note);
      this.ap.log(`${proc.stamp()} task view ${id}: copied a ${lines}-line code block for the terminal; nothing was sent (${where}${code.removed ? `, ${code.removed} hidden character(s) removed` : ''})`);
      return;
    }
    term.show();
    this._send(term, text);
    this.ap.log(`${proc.stamp()} task view ${id}: ran a code block in the terminal (${where}${code.removed ? `, ${code.removed} hidden character(s) removed` : ''})`);
  }

  /**
   * (2026-10-27, SEC-L13) A clicked Run / Run silently whose command is longer than a modal shows (SHOW_MAX):
   * the modal offers Copy only, so nothing the owner could not read runs. Copy = the clipboard; nothing is sent.
   * @param {PanelState} st @param {string} text the cleaned command @param {string} place @param {string} hiddenNote
   */
  async _copyOnly(st, text, place, hiddenNote) {
    const vscode = this.vscode;
    const t = i18n.t;
    const id = st.t.item.id;
    const button = t('Copy'); // translated once: the pick is compared with this same value
    // (2026-10-27, SEC-L13 review) too many lines (SHOW_LINES) says so; the dialog shows at most SHOW_LINES
    // lines and SHOW_MAX characters of it
    const lines = text.split('\n');
    const tall = lines.length > SHOW_LINES;
    const why = tall
      ? t('It has more lines ({0}) than this dialog can show, so it is not run from here: copy it, paste it into a terminal and read it there first.', lines.length)
      : t('It is longer than this dialog can show ({0} characters), so it is not run from here: copy it, paste it into a terminal and read it there first.', text.length);
    const notes = [why, hiddenNote].filter(Boolean).join('\n');
    const head = lines.slice(0, SHOW_LINES).join('\n').slice(0, SHOW_MAX);
    const rest = tall ? `… ${t('(+{0} lines)', lines.length - head.split('\n').length)}` : t('… ({0} more characters)', text.length - SHOW_MAX);
    const pick = vscode.window.showWarningMessage ? await vscode.window.showWarningMessage(
      t('Sleeping Clanker: copy this long command from the chat ({0})?', place),
      { modal: true, detail: `${notes}\n\n${head}\n${rest}` }, button) : undefined;
    if (pick !== button) {
      this.ap.log(`${proc.stamp()} task view ${id}: a ${text.length}-character command was not copied; nothing was sent`);
      return;
    }
    await vscode.env.clipboard.writeText(text);
    if (vscode.window.setStatusBarMessage) vscode.window.setStatusBarMessage(t('Sleeping Clanker: code copied'), 3000);
    this.ap.log(`${proc.stamp()} task view ${id}: copied a ${text.length}-character command (too long to run from a dialog); nothing was sent`);
  }

  /**
   * (2026-10-26, silent run) Run a cleaned code block in a hidden terminal (createTerminal hideFromUser:
   * no window opens, no tab in the terminal list). Several lines go to a script file in the temp folder
   * (chatview.silentScript) and ONE line runs it. Clicked: a modal shows the exact command first, as
   * Run in terminal does. Either way a chat note and an information message with "Show terminal" say what ran.
   * (2026-10-26, M4, owner "Oletus pois + kysy kerran") `auto` (_autoRun, a full-access task): _autoAsk
   * first (once per task; several lines always), then a NEW, VISIBLE terminal per run (never hidden, never
   * one that may still wait for input). Several PowerShell lines never run as a script (that would need
   * -ExecutionPolicy Bypass): they are copied and a new terminal is focused, as Run in terminal does.
   * @param {PanelState} st
   * @param {{text: string, shell: 'cmd' | 'powershell' | null, cwd: string | undefined, multiline: boolean, lines: number,
   *   hiddenNote: string, removed: number, auto: boolean}} r
   */
  async _runSilently(st, r) {
    const vscode = this.vscode;
    const id = st.t.item.id;
    const script = chatview.silentScript(r.shell, process.platform);
    const shellName = r.shell || (process.platform === 'win32' ? 'powershell' : 'default shell');
    const where = `${shellName}, ${r.cwd || 'no folder'}, ${r.auto ? 'new terminal' : 'hidden terminal'}`;
    const t = i18n.t;
    // (2026-10-27, i18n) `where` goes to the log (English), `place` to the dialogs and messages (the UI language)
    const place = `${r.shell || (process.platform === 'win32' ? 'powershell' : t('default shell'))}, ${r.cwd || t('no folder')}, `
      + `${r.auto ? t('new terminal') : t('hidden terminal')}`;
    if (r.auto) {
      const copyOnly = r.multiline && script.ext === '.ps1';
      if (!(await this._autoAsk(st, r, place, copyOnly))) return;
      if (copyOnly) {
        await vscode.env.clipboard.writeText(r.text);
        this._terminal(r.shell, r.cwd, true, true).show(false);
        if (vscode.window.showInformationMessage) {
          vscode.window.showInformationMessage(t('Sleeping Clanker: Copied {0} lines — paste and check before pressing Enter.', r.lines));
        }
        this.ap.log(`${proc.stamp()} task view ${id}: copied a full-access task's ${r.lines}-line code block for a new terminal; nothing was sent (${where})`);
        this._post(st, { type: 'note', text: t('▶ Copied the full-access task\'s {0} lines for a new terminal — paste, check and run them, then reply so the task continues.', r.lines) });
        return;
      }
    }
    if (!r.auto) {
      // (2026-10-27, SEC-L13) a command longer than the modal shows is never run from it: copy only
      // (2026-10-27, SEC-L13 review) and one with more lines than it shows (SHOW_LINES)
      if (r.text.length > SHOW_MAX || r.lines > SHOW_LINES) {
        await this._copyOnly(st, r.text, place, r.hiddenNote);
        return;
      }
      const shown = r.text;
      // (2026-10-27, SEC-L13) several PowerShell lines run as a .ps1 script with -ExecutionPolicy Bypass: say so
      const what = r.multiline && script.ext === '.ps1' ? t('These {0} lines run as one script: a PowerShell file run with -ExecutionPolicy Bypass (for this file only); no terminal window opens.', r.lines)
        : r.multiline ? t('These {0} lines run as one script; no terminal window opens.', r.lines) : t('It runs at once; no terminal window opens.');
      const button = t('Run silently'); // translated once: the pick is compared with this same value
      const pick = vscode.window.showWarningMessage ? await vscode.window.showWarningMessage(
        t('Sleeping Clanker: run this command from the chat silently ({0})?', place),
        { modal: true, detail: `${[what, r.hiddenNote].filter(Boolean).join('\n')}\n\n${shown}` }, button) : undefined;
      if (pick !== button) {
        this.ap.log(`${proc.stamp()} task view ${id}: silent run cancelled; nothing was sent`);
        return;
      }
    }
    let line = r.text;
    if (r.multiline) {
      try {
        const dir = path.join(os.tmpdir(), 'sleeping-clanker-run');
        fs.mkdirSync(dir, { recursive: true });
        // (review 5) our own scripts older than a day go; a running one is far younger
        for (const old of fs.readdirSync(dir)) {
          const p = path.join(dir, old);
          try {
            if (/^run-[\w-]+\.(?:ps1|cmd|sh)$/.test(old) && Date.now() - fs.statSync(p).mtimeMs > 86400000) fs.unlinkSync(p);
          } catch (_e) {
            // in use or gone: left for the next run
          }
        }
        const file = path.join(dir, `run-${String(id).replace(/[^\w-]/g, '_')}-${Date.now()}${script.ext}`);
        // a BOM so Windows PowerShell 5.1 reads the script as UTF-8; cmd: CRLF and the UTF-8 code page
        const text = script.ext === '.cmd' ? `@chcp 65001>nul\n${r.text}` : r.text;
        const body = script.ext === '.sh' ? `${text}\n` : `${text.replace(/\n/g, '\r\n')}\r\n`;
        fs.writeFileSync(file, script.ext === '.ps1' ? `\uFEFF${body}` : body, 'utf8');
        line = script.line(file);
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        const why = `could not write the script file for a silent run: ${msg}`;
        this.ap.log(`${proc.stamp()} !! ERROR task view ${id}: ${why}; nothing was sent`);
        this._post(st, { type: 'error', text: t('Silent run failed: could not write the script file for a silent run: {0}', msg) });
        return;
      }
    }
    const term = this._terminal(r.shell, r.cwd, true, r.auto);
    if (r.auto) term.show(true); // (2026-10-26, M4) visible, the focus stays where it is
    this._send(term, line);
    const how = r.auto ? 'ran by itself (full access)' : 'ran';
    this.ap.log(`${proc.stamp()} task view ${id}: ${how} a ${r.lines}-line code block${r.auto ? '' : ' silently'} (${where}${r.removed ? `, ${r.removed} hidden character(s) removed` : ''})`);
    const first = r.text.split('\n')[0];
    const short = first.length > 80 ? `${first.slice(0, 80)}…` : first;
    const more = `${short}${r.lines > 1 ? ` ${t('(+{0} lines)', r.lines - 1)}` : ''}`;
    this._post(st, { type: 'note', text: r.auto ? t('▶ Ran by itself (full access) in a new terminal: {0} — reply so the task continues.', more)
      : t('▶ Ran silently: {0} — reply so the task continues.', more) });
    if (vscode.window.showInformationMessage) {
      // (2026-10-27, i18n) translated once for both kinds: the pick is compared with this same value
      const show = t('Show terminal');
      const p = vscode.window.showInformationMessage(r.auto ? t('Sleeping Clanker: ran the task\'s command by itself ({0}).', place)
        : t('Sleeping Clanker: ran the command silently ({0}).', place), show);
      if (p && typeof p.then === 'function') p.then((pick) => { if (pick === show) term.show(); }, () => {});
    }
  }

  /**
   * (2026-10-26, M4, owner "Oletus pois + kysy kerran") Before a full-access task's command runs by itself:
   * a modal with the exact, whole command. Each task asks before its first auto-run; "Run and don't ask
   * again for this task" is kept per task id in workspaceState, after which its one-line commands run
   * without asking. Several lines always ask. Cancel (or no dialog) = no run.
   * @param {PanelState} st @param {{text: string, multiline: boolean, lines: number, hiddenNote: string}} r
   * @param {string} where @param {boolean} copyOnly several PowerShell lines: copied, not run
   * @returns {Promise<boolean>} go on
   */
  async _autoAsk(st, r, where, copyOnly) {
    const vscode = this.vscode;
    const t = i18n.t;
    const id = String(st.t.item.id);
    const ws = this.ap.context && this.ap.context.workspaceState;
    const kept = ws ? ws.get(AUTO_RUN_NO_ASK_KEY, []) : [];
    const noAsk = this._noAsk(id);
    if (noAsk && !r.multiline) return true;
    const run = copyOnly ? t('Copy for a new terminal') : t('Run');
    const always = t('Run and don\'t ask again for this task');
    const what = copyOnly
      // (2026-10-27, SEC-L13) true now: only a clicked Run silently uses Bypass (its modal says so), an auto-run never
      ? t('Several PowerShell lines run as a script only with -ExecutionPolicy Bypass, which an auto-run never uses: these {0} lines are copied for you to paste into a new terminal and check.', r.lines)
      : r.multiline ? t('These {0} lines run as one script in a new terminal.', r.lines) : t('It runs at once in a new terminal.');
    const pick = vscode.window.showWarningMessage ? await vscode.window.showWarningMessage(
      t('Sleeping Clanker: the full-access task {0} stopped with a command for you to run ({1}).', id, where),
      { modal: true, detail: `${[what, r.hiddenNote].filter(Boolean).join('\n')}\n\n${r.text}` },
      ...(copyOnly || noAsk ? [run] : [run, always])) : undefined;
    if (pick !== run && pick !== always) {
      this.ap.log(`${proc.stamp()} task view ${id}: auto-run cancelled; nothing was sent`);
      return false;
    }
    if (pick === always) {
      this.autoRunNoAsk.add(id);
      if (ws) await ws.update(AUTO_RUN_NO_ASK_KEY, [...(Array.isArray(kept) ? kept : []).filter((k) => k !== id), id].slice(-AUTO_RUN_KEEP));
    }
    // (review 6) the owner may have replied while the dialog was open
    return !st.disposed && !!st.item && st.item.status === 'blocked';
  }

  /**
   * (2026-10-27, SEC-L11) Does task `id` keep "Run and don't ask again" (this window, or workspaceState)?
   * @param {string} id
   */
  _noAsk(id) {
    const ws = this.ap.context && this.ap.context.workspaceState;
    const kept = ws ? ws.get(AUTO_RUN_NO_ASK_KEY, []) : [];
    return this.autoRunNoAsk.has(id) || (Array.isArray(kept) && kept.includes(id));
  }

  /**
   * (2026-10-27, U4, SEC-L11 open item) Every task that keeps "Run and don't ask again" (this window, or workspaceState).
   * @returns {string[]}
   */
  _noAskIds() {
    const ws = this.ap.context && this.ap.context.workspaceState;
    const kept = ws ? ws.get(AUTO_RUN_NO_ASK_KEY, []) : [];
    const list = Array.isArray(kept) ? kept.filter((k) => typeof k === 'string' && k) : [];
    return [...new Set([...this.autoRunNoAsk, ...list])];
  }

  /**
   * (2026-10-27, U4, SEC-L11 open item) Which task forgets "Run and don't ask again": one kept = that one; several =
   * a QuickPick of them (#id8 + the task's name when this window knows it) and "All of these tasks". [] = cancelled.
   * @param {string[]} ids @returns {Promise<string[]>}
   */
  async _pickNoAsk(ids) {
    if (ids.length <= 1) return ids.slice();
    const t = i18n.t;
    const lanes = Array.isArray(this.ap.lanes) ? this.ap.lanes : [];
    /** @param {string} id */
    const nameOf = (id) => {
      for (const l of lanes) {
        const it = (l && Array.isArray(l.items) ? l.items : []).find((/** @type {any} */ x) => x && x.id === id);
        if (it) return format.itemLabel(it);
      }
      return '';
    };
    const items = [...ids.map((id) => ({ label: `#${id.slice(0, 8)}`, description: nameOf(id), id })),
      { label: `$(clear-all) ${t('All of these tasks')}`, description: t('{0} task(s)', ids.length), id: '*' }];
    const pick = await pickers.forVscode(this.vscode).showQuickPick(items,
      { title: `Sleeping Clanker: ${t('Ask before each auto-run again')}`, ignoreFocusOut: true });
    if (!pick) return [];
    return pick.id === '*' ? ids.slice() : [pick.id];
  }

  /**
   * (2026-10-27, SEC-L11) The chat settings' "Ask before each auto-run again": task `id`'s "Run and don't ask
   * again" is forgotten (memory and workspaceState), so its next auto-run asks first again.
   * @param {string} id
   */
  async forgetNoAsk(id) {
    this.autoRunNoAsk.delete(id);
    const ws = this.ap.context && this.ap.context.workspaceState;
    const kept = ws ? ws.get(AUTO_RUN_NO_ASK_KEY, []) : [];
    if (ws && Array.isArray(kept) && kept.includes(id)) await ws.update(AUTO_RUN_NO_ASK_KEY, kept.filter((k) => k !== id));
    this.ap.log(`${proc.stamp()} task view ${id}: "don't ask again" for auto-runs forgotten; the next one asks first`);
  }

  /**
   * (2026-10-26, silent run) A full-access task blocked on a command ("run this to continue",
   * chatmodel.markNeedsRun): with `claudAutopilot.autoRunInFullAccess` on, an open, ready Task view runs
   * that message's code blocks silently, once per message (kept in globalState, so a reload does not run
   * it again). Other profiles never run by themselves. Never awaited by its callers; never throws.
   * (2026-10-26, M4) off by default; each run asks first (_autoAsk) and uses a visible terminal.
   * @param {PanelState} st
   */
  async _autoRun(st) {
    try {
      if (st.disposed || !st.ready) return;
      const s = this.ap.settings;
      if (!s || !s.autoRunInFullAccess) return;
      const item = st.item;
      if (!item || item.status !== 'blocked' || item.profile !== 'full') return;
      const need = /** @type {any} */ (this.view(st).blocks.find((b) => /** @type {any} */ (b).needsRun));
      if (!need) return;
      // (review 3) the block's key + the attempt, not its html (a re-render must not run it twice)
      const key = `${st.t.project || ''}|${st.t.lane}|${item.id}|${item.attempts}|${need.key}`;
      const gs = this.ap.context && this.ap.context.globalState;
      // (review 2) a question that was already waiting before auto-run first started here (e.g. right
      // after the upgrade) is old: it only gets the highlighted button
      let since = gs ? gs.get(AUTO_RUN_SINCE_KEY, null) : this.autoRunSince;
      if (typeof since !== 'string') {
        since = this.autoRunSince;
        if (gs) await gs.update(AUTO_RUN_SINCE_KEY, since);
      }
      const at = Date.parse(item.finished_at || '');
      if (!Number.isFinite(at) || at < Date.parse(since)) return;
      const ran = gs ? /** @type {string[]} */ (gs.get(AUTO_RUN_KEY, [])) : [];
      if (this.autoRan.has(key) || (Array.isArray(ran) && ran.includes(key))) return;
      this.autoRan.add(key);
      if (gs) await gs.update(AUTO_RUN_KEY, [...(Array.isArray(ran) ? ran : []), key].slice(-AUTO_RUN_KEEP));
      // (review 1) shell blocks only: python, json, diff, logs … are never run by themselves
      // (2026-10-27, SEC-L11) and only the LAST top-level one (not one in a list or a quote): one command per
      // question, never a series the owner did not look at
      const runnable = markdown.codeBlocksOf(chatview.topLevelHtml(need.html)).filter((b) => markdown.runnableLang(b.lang));
      for (const c of runnable.slice(-1)) {
        // (review 6) the owner may have replied meanwhile
        if (st.disposed || !st.item || st.item.status !== 'blocked') return;
        await this._code(st, { type: 'runCode', text: c.text, lang: c.lang, silent: true }, true);
      }
    } catch (err) {
      this.ap.log(`${proc.stamp()} !! ERROR task view auto-run ${st.t.item.id}: ${err instanceof Error ? err.message : err}`);
    }
  }

  /**
   * (2026-10-26, silent run) The one place a line goes to a terminal, with Enter (tests/test_network_contract.py
   * pins it): Run in terminal and Run silently after their modal, _autoRun for a full-access task.
   * @param {any} term @param {string} line
   */
  _send(term, line) {
    term.sendText(line, true);
  }

  /**
   * One reused terminal per shell + folder; a closed one is replaced.
   * (2026-10-26, silent run) `silent`: a separate hidden one (hideFromUser) whose shell is known.
   * (2026-10-26, M4) `fresh` (an auto-run): always a NEW, visible one with that known shell, never reused,
   * so no line goes into whatever an earlier command left waiting for input.
   * @param {string | null} shell @param {string | undefined} cwd @param {boolean} [silent] @param {boolean} [fresh]
   */
  _terminal(shell, cwd, silent = false, fresh = false) {
    const vscode = this.vscode;
    const key = `${silent ? 'silent|' : ''}${shell || ''}|${cwd || ''}`;
    let term = fresh ? undefined : this.terminals.get(key);
    const open = vscode.window.terminals || [];
    // a hidden terminal may be missing from window.terminals until it is shown: only its exit counts
    if (!term || term.exitStatus !== undefined || (!silent && !open.includes(term))) {
      /** @type {Record<string, any>} */
      // (2026-10-27, i18n) the terminal list shows the name: its tag is in the UI language
      const tag = fresh ? ` ${i18n.t('(auto-run)')}` : silent ? ` ${i18n.t('(silent)')}` : '';
      // (2026-10-27, SEC-L11) no secret-named variable of the window, and no program from the cwd in cmd.exe
      const opts = { name: `Sleeping Clanker${shell ? ` (${shell})` : ''}${tag}`, cwd, env: chatview.terminalEnv(process.env) };
      if (shell === 'cmd') opts.shellPath = 'cmd.exe';
      if (shell === 'powershell') opts.shellPath = 'powershell.exe';
      if (silent) {
        if (!fresh) opts.hideFromUser = true;
        const sp = chatview.silentScript(shell, process.platform).shellPath;
        if (sp) opts.shellPath = sp;
      }
      term = vscode.window.createTerminal(opts);
      if (!fresh) this.terminals.set(key, term);
    }
    return term;
  }

  /**
   * (2026-10-21, attach) The composer's "Add picture or file" button: an open dialog (the task's folder
   * first); the chosen local paths go back to the webview as `attached`, which lists them in the draft
   * (Claude opens them with Read, which shows pictures too). Nothing is copied or sent here.
   * @param {PanelState} st
   */
  async _attach(st) {
    const vscode = this.vscode;
    const item = st.item || st.t.item;
    // (2026-10-21, attach fix) files only: with folders on too, the Windows/Linux dialog is a folder
    // picker that hides every file
    /** @type {Record<string, any>} */
    // (2026-10-27, i18n) the dialog's button, title and filter names are in the UI language
    const opts = {
      canSelectFiles: true, canSelectFolders: false, canSelectMany: true, openLabel: i18n.t('Add'),
      title: i18n.t('Sleeping Clanker: add pictures or files to your reply'),
      filters: { [i18n.t('All files')]: ['*'], [i18n.t('Images')]: chatview.IMAGE_EXTS },
    };
    if (item.cwd && vscode.Uri && vscode.Uri.file) opts.defaultUri = vscode.Uri.file(item.cwd);
    const picked = (await vscode.window.showOpenDialog(opts)) || [];
    const nonLocal = picked.filter((/** @type {any} */ u) => u.scheme !== 'file').length;
    if (nonLocal) vscode.window.showWarningMessage(i18n.t('Sleeping Clanker: only local files can be added ({0} skipped).', nonLocal));
    const paths = chatview.attachPaths(picked.filter((/** @type {any} */ u) => u.scheme === 'file').map((/** @type {any} */ u) => u.fsPath));
    if (!paths.length) return;
    this.ap.log(`${proc.stamp()} task view ${st.t.item.id}: ${paths.length} file(s) added to the reply draft`);
    this._post(st, { type: 'attached', paths });
  }

  /**
   * (0.4.6) A chat's own "Auto-pick best" box: it overrides the setting for this panel and re-arms
   * the cap. (2026-10-21) Also set from ⚙ Chat settings (`tell`: the webview keeps it like a click).
   * @param {PanelState} st @param {boolean} on @param {boolean} [tell]
   */
  _setAutoPick(st, on, tell) {
    st.autoPick = on;
    st.autoKey = '';
    st.autoCount = 0;
    this.ap.log(`${proc.stamp()} task view ${st.t.item.id}: auto-pick best option ${on ? 'on' : 'off'} (this chat)`);
    if (tell) this._post(st, { type: 'autoPick', on });
    this._push(st, false);
    this._autoPick(st); // not awaited: an answer may wait for a Start Runner click
  }

  /**
   * (2026-10-21) ADHD mode on or off: kept for every chat (also new ones); the open chats follow at
   * once. `from`: the chat whose own View dropdown changed it (it shows the value already); null = all.
   * @param {boolean} on @param {PanelState | null} from
   */
  async setAdhd(on, from) {
    this.adhd = on;
    const gs = this.ap.context && this.ap.context.globalState;
    if (gs) {
      try {
        await gs.update(ADHD_KEY, on);
      } catch (err) {
        this.ap.log(`${proc.stamp()} WARN task view: could not keep ADHD mode: ${err instanceof Error ? err.message : err}`);
      }
    }
    this.ap.log(`${proc.stamp()} task view: ADHD mode ${on ? 'on' : 'off'}`);
    for (const other of this.panels.values()) {
      if (other !== from) this._post(other, { type: 'viewMode', adhd: on });
    }
  }

  /**
   * (2026-10-21) The composer's ⚙ "Chat settings": what the chat box shows and does. ADHD mode (every
   * chat), Auto-pick best (this chat, and what new chats start with), the quick prompts, the "What did
   * you change?" text and the language. The last row opens the global settings (the Queue view's gear).
   * (2026-10-26, owner) Opened from the New Task panel (its only button now): st = null, no chat, so no
   * "Auto-pick best in this chat" row.
   * @param {PanelState | null} st
   */
  async _chatSettings(st) {
    const vscode = this.vscode;
    const t = i18n.t;
    const onOff = (/** @type {boolean} */ on) => (on ? t('On') : t('Off'));
    const s = this.ap.settings || {};
    const changes = this.changesQuestion();
    const lang = typeof this.ap._languageDescription === 'function'
      ? this.ap._languageDescription() : i18n.NAMES[/** @type {'en'} */ (i18n.language())];
    const all = [
      { label: `$(eye) ${t('ADHD mode')}`, description: onOff(this.adhdOn()),
        detail: t('Only the questions, the answers and the status; the steps in between fold away. Every Task view.'), id: 'adhd' },
      { label: `$(wand) ${t('Auto-pick best in this chat')}`, description: onOff(!!st && this.autoPickOn(st)),
        detail: t('Answer a blocked question by itself with its best (recommended) option. Only this chat.'), id: 'auto' },
      { label: `$(wand) ${t('Auto-pick best in new chats')}`, description: onOff(!!s.autoPickBestOption),
        detail: t('What a newly opened chat starts with (claudAutopilot.autoPickBestOption).'), id: 'auto-new' },
      // (2026-10-26, modes) this task's own Autorun (`edit --autorun`, also while it runs)
      { label: `$(rocket) ${t('Autorun for this task')}`, description: onOff(!!(st && (st.item || st.t.item).autorun)),
        detail: t(argv.AUTORUN_TIP), id: 'autorun' },
      // (2026-10-27, SEC-L11) only while this task's "Run and don't ask again" is kept (_autoAsk)
      { label: `$(debug-restart) ${t('Ask before each auto-run again')}`, description: '',
        detail: t('Forget "Run and don\'t ask again" for this task: its next command asks first.'), id: 'noask-forget' },
      // (2026-10-21, tester round 2 L-6) no "Runners at once…" here: it is a runner setting, and the
      // settings split (DECISIONS) keeps those in the sidebar's Global settings
      { label: `$(comment) ${t('Quick prompts…')}`, description: t('{0} button(s)', this.quickPrompts().length),
        detail: t('The buttons above the reply box (claudAutopilot.quickPrompts).'), id: 'prompts' },
      { label: `$(question) ${t('"What did you change?" text…')}`, description: changes ? '' : t('no button'),
        detail: changes || t('Empty = no button.'), id: 'changes' },
      { label: `$(globe) ${t('Language')}`, description: lang,
        detail: t('The language of the Queue view, the status bar, the hovers and the Task view.'), id: 'language' },
      { label: `$(gear) ${t('Global settings…')}`, description: '',
        detail: t('The Queue view\'s gear: language, defaults for +, runners and every setting.'), id: 'global' },
    ];
    // (2026-10-26, integrate) opened from the add panel (st = null): no "this chat" row, no "this task" row (modes)
    // (2026-10-27, U4, SEC-L11 open item) the add panel is this menu's only caller (st = null), so "Ask before each
    // auto-run again" shows there whenever ANY task keeps "Run and don't ask again"; picking it asks which one
    const noAskIds = st ? (this._noAsk(String(st.t.item.id)) ? [String(st.t.item.id)] : []) : this._noAskIds();
    if (!st && noAskIds.length) {
      const row = /** @type {{description: string, detail: string}} */ (all.find((i) => i.id === 'noask-forget'));
      row.description = t('{0} task(s)', noAskIds.length);
      row.detail = t('Forget "Run and don\'t ask again" for a task: its next command asks first.');
    }
    const items = (st ? all : all.filter((i) => i.id !== 'auto' && i.id !== 'autorun')).filter((i) => i.id !== 'noask-forget' || noAskIds.length > 0);
    const pick = await pickers.forVscode(vscode).showQuickPick(items, { title: `Sleeping Clanker: ${t('Chat settings')}`, ignoreFocusOut: true });
    if (!pick || (st && st.disposed)) return;
    if (pick.id === 'noask-forget') { for (const id of st ? noAskIds : await this._pickNoAsk(noAskIds)) await this.forgetNoAsk(id); }
    else if (pick.id === 'adhd') await this.setAdhd(!this.adhdOn(), null);
    else if (pick.id === 'auto') { if (st) this._setAutoPick(st, !this.autoPickOn(st), true); }
    else if (pick.id === 'autorun') { if (st) await this._setAutorun(st); }
    else if (pick.id === 'auto-new') {
      await this.ap._updateSettings({ autoPickBestOption: !s.autoPickBestOption });
      for (const p of this.panels.values()) this._push(p, false); // chats that follow the setting show it at once
    } else if (pick.id === 'prompts') {
      await vscode.commands.executeCommand('workbench.action.openSettings', 'claudAutopilot.quickPrompts');
    } else if (pick.id === 'changes') {
      const v = await pickers.forVscode(vscode).showInputBox({
        title: `Sleeping Clanker: ${t('"What did you change?" text…')}`, value: changes, ignoreFocusOut: true,
        prompt: t('What the "What did you change?" button sends to a finished, failed or cancelled task. Empty = no button.'),
      });
      if (v === undefined) return;
      await this.ap._updateSettings({ changesQuestion: v.trim() });
      for (const p of this.panels.values()) this._push(p, false);
    } else if (pick.id === 'language') await this.ap.languageMenu();
    else await this.ap.settingsMenu();
  }

  /**
   * (2026-10-26, modes) Switch this task's Autorun in the core (`edit ID --autorun=on|off`); on re-arms
   * its cap and answers a question that is already waiting.
   * (2026-10-27, modes fix round 2) `want`: the composer's Autorun box (a boolean); absent = switch it.
   * @param {PanelState} st @param {boolean} [want]
   */
  async _setAutorun(st, want) {
    const item = st.item || st.t.item;
    const on = typeof want === 'boolean' ? want : !item.autorun;
    await this.ap.cliJson(argv.runEditArgs(item.id, { autorun: on }), parse.parseEdit, undefined, st.t.project, st.t.laneArg);
    Object.assign(this._autorunRec(st.t.lane, item.id), { autoKey: '', autoCount: 0, stopped: false });
    this.ap.event(`${item.id}: autorun ${on ? 'on' : 'off'} (Task view)`);
    const fresh = await this._item(st, true);
    // (2026-10-27, modes fix round 2) the core took it; the last poll's copy may still have the old value
    st.item = { ...(fresh || item), autorun: on };
    this._push(st, false);
    this._autoPick(st); // not awaited: an answer may wait for a Start Runner click
  }

  /** A tree-node-like target (what Autopilot._target and sendReplyText accept). @param {PanelState} st */
  _target(st) {
    return { kind: 'item', ...st.t, item: st.item || st.t.item };
  }

  /**
   * The composer: the SAME path as Reply… (Autopilot.sendReplyText: cli `reply`, then the
   * autoStartRunner / Start Runner offer), with the profile from the dropdown instead of a QuickPick.
   * (0.4.1) `textOf` gets the fresh item: the composer's text, or `Option <n>: <label>` of an option
   * button looked up in the CURRENT item (null = that option is not offered: nothing is sent).
   * (0.4.6) `opts.auto`: this reply is an automatic option pick, so the webview hears about it as
   * `autoPicked` (a note in the log) instead of `replyResult` — nothing in the composer moves, and a
   * typed draft is left alone.
   * @param {PanelState} st @param {(item: import('./parse').Item) => string | null} textOf
   * (2026-10-26, modes) `opts.autorun`: an Autorun answer (its note says so, and it starts the runner); an
   * owner's own reply re-arms the task's Autorun cap.
   * (2026-10-27, integrate) `opts.key`: the questionKey the Autorun answer was picked for (re-checked on the fresh item).
   * @param {string | null} profile @param {{auto?: boolean, note?: string, autorun?: boolean, key?: string}} [opts]
   */
  async _reply(st, textOf, profile, opts = {}) {
    let answered = false;
    const item0 = st.item || st.t.item;
    if (!opts.auto && item0 && item0.autorun) Object.assign(this._autorunRec(st.t.lane, item0.id), { autoCount: 0, stopped: false });
    // (2026-10-29, integrate round 9) `sent`: the reply reached the core and became a merge answer that was refused
    const answer = (/** @type {boolean} */ ok, error = '', sent = false) => {
      if (answered) return;
      answered = true;
      if (opts.autorun && !ok) {
        this._autorunRec(st.t.lane, (st.item || st.t.item).id).stopped = true; // the buttons come back
        this._push(st, false);
      }
      // (2026-10-27, modes fix round 2) a sent Autorun answer is stored as one (`reply --by autorun`): the
      // chat shows it as its "Autorun picked" note from the item (chatmodel replyBlock), also after a reload
      if (opts.autorun && ok) {
        this._push(st, false);
        return;
      }
      this._post(st, opts.auto ? { type: 'autoPicked', ok, text: opts.note || '', error: error || '', ...(opts.autorun ? { autorun: true } : {}) }
        : { type: 'replyResult', ok, error: error || '', ...(sent && !ok ? { sent: true } : {}) });
    };
    try {
      const item = (await this._item(st, false, !!opts.autorun)) || st.item;
      if (opts.autorun && (!item || item.status !== 'blocked')) {
        // (2026-10-27, modes fix LOW) answered meanwhile (another window, the CLI): nothing to do, no error
        answered = true;
        this.ap.log(`${proc.stamp()} task view ${item0 ? item0.id : '?'}: autorun skipped; the question was already answered`);
        return;
      }
      // (2026-10-27, integrate: modes review LOW-1) blocked again, but with ANOTHER question (answered elsewhere
      // and asked anew): the old option number would answer the new question. Skip; the next pass answers
      // the new question with its own best option.
      if (opts.autorun && opts.key && questionKey(item, format.questionOptions(item)) !== opts.key) {
        answered = true;
        this.ap.log(`${proc.stamp()} task view ${item0 ? item0.id : '?'}: autorun skipped; the question changed meanwhile`);
        return;
      }
      // (2026-10-27, i18n) these reasons are shown in the chat ("Not sent: …"): the UI language
      if (!item || !argv.canReply(item)) {
        throw new Error(item && item.session_id
          ? i18n.t('a {0} item cannot get a reply (only blocked, done, failed, cancelled or paused without a queued reply)', i18n.t(item.status))
          : i18n.t('this task has no session to resume yet'));
      }
      st.item = item;
      const text = textOf(item);
      if (!text) throw new Error(i18n.t('that option is not offered any more (the question changed); nothing was sent'));
      // (2026-10-26, owner fa388b7b) `auto`: an automatic answer never waits for the idle full-access question
      // (2026-10-26, modes) `autoStart`: an Autorun answer (stored `--by autorun`, starts the runner)
      const ok = await this.ap.sendReplyText(this._target(st), text, { profile, onQueued: () => answer(true),
        auto: !!opts.auto, autoStart: !!opts.autorun });
      answer(!!ok, ok ? '' : i18n.t('the reply was not sent'));
    } catch (err) {
      const m = err instanceof Error ? err.message : String(err);
      // (2026-10-27, modes fix round 2) the core refused an Autorun answer because the question was
      // answered meanwhile (another window): the same quiet skip, no error, Autorun stays armed
      const now = opts.autorun && !answered ? await this._item(st, true, true) : null;
      if (now && now.status !== 'blocked') {
        answered = true;
        this.ap.log(`${proc.stamp()} task view ${item0 ? item0.id : '?'}: autorun skipped; the question was already answered (${m})`);
        return;
      }
      answer(false, m, !!(err && /** @type {any} */ (err).mergeRefused));
      this.ap.reportError(opts.auto ? 'auto-pick reply' : 'reply', err);
    }
  }

  dispose() {
    for (const st of [...this.panels.values()]) {
      this._unwatch(st);
      try { st.panel.dispose(); } catch (_e) { /* already gone */ }
    }
    this.panels.clear();
  }
}

module.exports = { TaskViews, VIEW_TYPE, AUTO_PICK_MAX, AUTORUN_MAX, questionKey };

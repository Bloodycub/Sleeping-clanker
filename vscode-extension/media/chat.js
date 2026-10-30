// @ts-check
// The Task view's webview script (2026-10-17, 0.4.0, docs/SPEC_V04_CHAT.md). Runs inside the
// locked-down webview (CSP: only this nonce'd file). It builds the DOM from the host's `patch`
// messages with createElement/textContent; the only innerHTML is the host's markdown HTML, which
// lib/markdown.js produced escape-first. Messages to the host: ready, openRaw, openLink, reply,
// (0.4.1) choose {n, profile}: an option button of a blocked question (the host looks the option up).
// (0.4.1 D7) The log keeps its last lines above the composer at any textarea height (ResizeObserver).
// (0.4.3) The webview state is {itemId, project, lane, draft}: the restore after a window reload.
// (0.4.6) autoPick {on}: the "Auto-pick best" box next to the profile dropdown; the host answers a
// blocked question by itself while it is checked and reports back as `autoPicked`. State: autoPick.
// (2026-10-21) viewMode {adhd}: the header's View dropdown. ADHD mode shows only the owner's messages,
// the questions, the answers and the status; the rest folds into "N hidden steps" rows. The host keeps
// the choice for every chat (globalState) and sends it as `adhd` in each patch; not in the webview state.
// (2026-10-21) copyCode / runCode {text, lang}: a code block's Copy and Run in terminal buttons.
// (2026-10-21) action start-now: a pending task's "Start now"; attach: the bottom bar's "Add picture or
// file" (the host answers with `attached` {paths}, which are listed in the draft).
// (2026-10-21) chatSettings: the bottom bar's ⚙ "Chat settings"; the host opens a QuickPick and pushes
// the result back (patch / viewMode), so the webview keeps no settings of its own. (2026-10-26, owner)
// The button moved to the New Task panel (media/addpanel.js); the host still accepts the message.
// (2026-10-21, subagents) maxAgents {value}: the bottom bar's subagent dropdown (docs/SPEC_V049_AGENTS.md).
// The host writes it to the item (`edit --max-agents`) and sends the item's value back in `patch`;
// nothing about it is kept in the webview state.
/* global acquireVsCodeApi */
(function () {
  'use strict';

  // @ts-ignore acquireVsCodeApi is provided by the webview host
  const vscode = acquireVsCodeApi();
  const hdr = /** @type {HTMLElement} */ (document.getElementById('hdr'));
  const log = /** @type {HTMLElement} */ (document.getElementById('log'));
  const composer = /** @type {HTMLElement} */ (document.getElementById('composer'));
  const BOTTOM_PX = 40;
  const REPLY_MAX = 100000;
  // (2026-10-21, UI language) the host puts this panel's translations in <body data-i18n> (JSON, English
  // text -> translated text); absent = English. `{0}`, `{1}` in a text are filled from the arguments.
  /** @type {Record<string, string>} */
  const I18N = (() => {
    try {
      const raw = document.body.dataset.i18n;
      const d = raw ? JSON.parse(raw) : null;
      return d && typeof d === 'object' ? d : {};
    } catch (_) { return {}; }
  })();
  /** @param {string} text @param {...any} args */
  function tr(text, ...args) {
    const s = Object.prototype.hasOwnProperty.call(I18N, text) && typeof I18N[text] === 'string' ? I18N[text] : text;
    return args.length ? s.replace(/\{(\d+)\}/g, (m, i) => (Number(i) < args.length ? String(args[Number(i)]) : m)) : s;
  }
  // (0.4.6) the Auto-pick box's tooltip, and what it says once the chat has hit its cap
  const AUTO_TIP = tr('Answer a question like this one automatically with its best (recommended) option, '
    + 'so the task keeps going while you are away.\nOnly in this chat, and the task keeps its own permission '
    + 'profile — an automatic answer never widens it. Default for new chats: the claudAutopilot.autoPickBestOption setting.');
  const AUTO_TIP_STOPPED = `${AUTO_TIP}\n\n${tr('This chat has answered several questions in a row by itself, so the next one waits for you.')}`;
  // (2026-10-26, modes) an Autorun task: the box is on and locked; (2026-10-27, modes fix round 2) the Autorun box switches it
  const AUTORUN_BOX_TIP = tr('Autorun is on for this task: its questions are answered with the best option. Switch it with the Autorun box next to this one.');
  // (2026-10-21, subagents, SPEC_V049_AGENTS) the subagent dropdown's tooltip
  const AGENTS_TIP = tr('How many helper agents (subagents) this task may start at the same time.\n'
    + '"Profile default" = only the `full` profile may start any. A subagent gets this task\'s own permissions, '
    + 'nothing more. The limit takes effect from the task\'s next attempt, and the count itself is asked for in '
    + 'the system prompt: the log says so if the task went over it.\n'
    + 'How many QUEUES run at once is the Queue view\'s gear → Global settings → "Runners at once…".');
  const AGENTS_TIP_RUNNING = `${AGENTS_TIP}\n\n${tr('The task is running: pause it first to change this.')}`;
  // (2026-10-21, attach) the line above the added paths in a reply
  const ATTACH_HEAD = 'Attached files (open each with the Read tool; it shows pictures too):';
  const ICONS = {
    Read: '▤', Write: '✎', Edit: '✎', MultiEdit: '✎', NotebookEdit: '✎', Glob: '⌕', Grep: '⌕', Bash: '$', PowerShell: '$',
    WebFetch: '⇣', WebSearch: '⌕', Task: '◆', Agent: '◆', TodoWrite: '☰', Skill: '★',
  };

  /** @type {Map<string, HTMLElement>} */
  const els = new Map();
  /** @type {any} */
  let header = null;
  /** @type {HTMLElement | null} */
  let optimistic = null;
  let sending = false;
  const saved = (vscode.getState && vscode.getState()) || {};
  // (2026-10-26, write while working) as in Claude chat: a message sent while Claude works waits here
  // (kept in the webview state) and goes out by itself when the turn ends (the host's `autoSend`)
  let queued = typeof saved.queued === 'string' ? saved.queued : '';
  /** @type {HTMLElement | null} */
  let queuedEl = null;
  let sentQueued = ''; // the queued text being sent now: back into the box if the send fails
  let stickyNote = ''; // "back in the box": stays until the owner types or sends
  let canReplyNow = true; // the last composer state's `enabled` (Send replies now, else it queues)
  // (2026-10-26, edit in box) Edit task… / Edit reply… put the text into the box; Send then saves it
  // (`saveEdit`) instead of replying. `editStash` = the draft that was in the box; it comes back after.
  /** @type {{reply: boolean} | null} */
  let editing = null;
  let editStash = '';
  /** @type {any} */
  let lastComposer = null;
  let lateNote = ''; // the edit ended by itself (too late); cleared when the owner types
  // (0.4.3) which item this panel shows, from the host's HTML; kept in the webview state (with the
  // draft, nothing else) so VS Code can restore the panel after a window reload. The host validates it.
  const ds = document.body.dataset;
  const ident = { itemId: ds.item || '', project: ds.project || null, lane: ds.lane || 'default' };
  // (2026-10-21) ADHD mode: the host's value (each patch carries it); the header dropdown changes it
  let adhd = false;
  /** @type {HTMLSelectElement | null} */
  let viewSel = null;
  /** (2026-10-21) the header's prompt-cache line @type {HTMLElement | null} */
  let cacheEl = null;
  /** @type {Set<string>} the hidden runs the owner opened, by the key of their first row */
  const openFolds = new Set();

  // ---- helpers -------------------------------------------------------------------------------------
  /**
   * @param {string} tag @param {string} [cls] @param {string} [text]
   * @returns {HTMLElement}
   */
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function atBottom() {
    const s = document.scrollingElement || document.documentElement;
    return s.scrollHeight - s.scrollTop - s.clientHeight <= BOTTOM_PX;
  }

  function toBottom() {
    const s = document.scrollingElement || document.documentElement;
    s.scrollTop = s.scrollHeight;
  }

  /** @param {number} ms */
  function elapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    // (2026-10-27, i18n fix) the units through tr(): '{0}m {1}s' and '{0}s' are lib/format.js's keys too
    return h ? tr('{0}h {1}m', h, m) : m ? tr('{0}m {1}s', m, sec) : tr('{0}s', sec);
  }

  /**
   * (2026-10-21) ADHD mode: a long text as its first sentence (or first line) and the rest. Full mode
   * shows both; ADHD mode only the first, with "…" (a click or the tooltip shows the rest).
   * @param {string} s @returns {[string, string]}
   */
  function splitShort(s) {
    const t = String(s || '');
    let cut = t.indexOf('\n');
    const re = /[.!?](?=\s)/g;
    let m;
    while ((m = re.exec(t))) {
      if (m.index < 20) continue;
      if (cut < 0 || m.index + 1 < cut) cut = m.index + 1;
      break;
    }
    if ((cut < 0 || cut > 240) && t.length > 240) {
      const sp = t.lastIndexOf(' ', 200);
      if (sp > 0) cut = sp;
    }
    if (cut <= 0 || !t.slice(cut).trim()) return [t, ''];
    return [t.slice(0, cut), t.slice(cut)];
  }

  /** (2026-10-21) `el()` for a text that ADHD mode shortens (splitShort). @param {string} tag @param {string} cls @param {string} text */
  function cutText(tag, cls, text) {
    const [head, rest] = splitShort(text);
    if (!rest) return el(tag, cls, text);
    const d = el(tag, `${cls} cut`);
    d.appendChild(el('span', 'cuthead', head));
    d.appendChild(el('span', 'cutrest', rest));
    d.title = String(text);
    return d;
  }

  /**
   * (2026-10-21) ADHD mode: which log rows stay visible. `rows`: {kind, key, answer} in log order
   * (kind '' = a row this script added itself: a pending bubble, an auto-pick note; always shown).
   * Shown: the owner's messages, results, Done / Needs-you badges, pauses, "Working…", the empty-log
   * note, and the answer of a finished attempt: its result's text, else (the result only repeated it)
   * the attempt's last assistant message. Hidden: attempt lines, tool calls, interim messages,
   * denials, stderr and notes.
   * @param {{kind: string, key: string, answer: boolean}[]} rows @returns {boolean[]}
   */
  function adhdShown(rows) {
    // (2026-10-21, v049 dontask_hold addendum) + 'warning': background work lost at the end of a run
    const LOUD = ['', 'user', 'result', 'marker', 'limit', 'working', 'warning'];
    // (2026-10-27, modes fix round 2) + an "Autorun picked" note (a reply's key r<i>): it is an answer
    const shown = rows.map((r) => LOUD.includes(r.kind) || (r.kind === 'note' && (r.key === 'empty' || /^r\d+$/.test(r.key))));
    let start = 0;
    for (let i = 0; i <= rows.length; i += 1) {
      if (i < rows.length && (rows[i].kind !== 'attempt' || i === start)) continue;
      // rows[start..i) is one attempt (or what comes before the first one)
      let finished = false;
      let answered = false;
      let last = -1;
      for (let j = start; j < i; j += 1) {
        if (rows[j].kind === 'result') {
          finished = true;
          answered = answered || rows[j].answer;
        } else if (rows[j].kind === 'assistant') {
          last = j;
        }
      }
      if (finished && !answered && last >= 0) shown[last] = true;
      start = i;
    }
    return shown;
  }

  let queuedSig = '0'; // (2026-10-26, write while working) what the host was last told about `queued` (it starts off)

  function saveState() {
    // the host skips auto-pick while a queued message waits (it answers the question itself)
    const q = queued ? '1' : '0';
    if (q !== queuedSig) {
      queuedSig = q;
      vscode.postMessage({ type: 'queued', on: !!queued });
    }
    const ta = /** @type {HTMLTextAreaElement | null} */ (composer.querySelector('textarea'));
    try {
      // (0.4.6) autoPick is kept only when the owner set it in THIS chat; null = follow the setting
      vscode.setState({ itemId: ident.itemId, project: ident.project, lane: ident.lane, draft: editing ? editStash : ta ? ta.value : '',
        autoPick: autoTouched && auto ? auto.checked : null, queued });
    } catch (_e) { /* state is a convenience */ }
  }

  // (2026-10-26, silent run) lib/markdown.js runnableLang, both platforms (the host decides what auto-runs)
  const RUN_LANGS = ['', 'sh', 'bash', 'shell', 'console', 'zsh', 'cmd', 'bat', 'batch', 'dos', 'powershell', 'pwsh', 'ps', 'ps1'];

  /**
   * (2026-10-21) Copy and Run in terminal buttons on every fenced code block of a markdown message.
   * Only the block's text and fence language go to the host (copyCode / runCode); the host checks the
   * block is in this chat before it copies or runs it.
   * (2026-10-26, silent run) `needsRun`: the blocked task's last message (the host's `needsRun`): its
   * blocks are highlighted with "Run this to continue" and a "Run silently" button (runCode silent:
   * a hidden terminal, no window opens).
   * @param {HTMLElement} md @param {boolean} [needsRun]
   */
  function codeTools(md, needsRun) {
    for (const code of md.querySelectorAll('pre > code')) {
      const pre = /** @type {HTMLElement} */ (code.parentElement);
      const text = code.textContent || '';
      if (!text.trim()) continue;
      const lang = (/^language-(.*)$/.exec(code.className) || ['', ''])[1].slice(0, 32);
      const bar = el('div', 'codetools');
      const copy = /** @type {HTMLButtonElement} */ (el('button', 'codebtn', tr('Copy')));
      copy.type = 'button';
      copy.title = tr('Copy this code to the clipboard');
      copy.addEventListener('click', () => {
        vscode.postMessage({ type: 'copyCode', text, lang });
        copy.textContent = tr('Copied ✓');
        setTimeout(() => { copy.textContent = tr('Copy'); }, 1500);
      });
      const run = /** @type {HTMLButtonElement} */ (el('button', 'codebtn', `▶ ${tr('Run in terminal')}`));
      run.type = 'button';
      run.title = tr("Open a terminal in the task's folder and run these lines (each line as typed + Enter)");
      run.addEventListener('click', () => vscode.postMessage({ type: 'runCode', text, lang }));
      bar.appendChild(copy);
      bar.appendChild(run);
      // only a shell block (or no language) is a command to run; python, json, diff … stay plain
      const runnable = needsRun && RUN_LANGS.includes(lang.toLowerCase());
      const wrap = el('div', runnable ? 'codeblock needsrun' : 'codeblock');
      if (runnable) {
        const silent = /** @type {HTMLButtonElement} */ (el('button', 'codebtn primary', `▶ ${tr('Run silently')}`));
        silent.type = 'button';
        silent.title = tr('Run it in a hidden terminal (no window opens); then reply so the task continues');
        silent.addEventListener('click', () => vscode.postMessage({ type: 'runCode', text, lang, silent: true }));
        bar.appendChild(silent);
        wrap.appendChild(el('div', 'needsrun-note', `⚠ ${tr('Run this to continue the task')}`));
      }
      pre.replaceWith(wrap);
      wrap.appendChild(bar);
      wrap.appendChild(pre);
    }
  }

  // ---- blocks --------------------------------------------------------------------------------------
  /** @param {any} b @returns {HTMLElement} */
  function render(b) {
    switch (b.kind) {
      case 'attempt': {
        const d = el('div', 'blk attempt');
        const parts = [tr('Attempt {0}', b.n), b.time];
        if (b.model) parts.push(b.model);
        if (b.session) parts.push(tr('session {0}', b.session));
        d.appendChild(el('span', 'line', parts.join(' · ')));
        if (b.tools && b.tools.length) {
          const det = el('details', 'tools');
          det.appendChild(el('summary', '', tr('tools ({0})', b.tools.length)));
          det.appendChild(el('div', 'toollist', b.tools.join(', ')));
          d.appendChild(det);
        }
        if (b.skipped) {
          d.appendChild(el('div', 'note', tr(b.skipped === 1
            ? '… {0} earlier event not shown (the last 2 MB are kept; Open Run Log Folder has the full file)'
            : '… {0} earlier events not shown (the last 2 MB are kept; Open Run Log Folder has the full file)', b.skipped)));
        }
        return d;
      }
      case 'user': {
        const d = el('div', `blk msg user${b.pending ? ' pending' : ''}`);
        const bub = el('div', 'bubble');
        bub.appendChild(el('div', 'label', b.pending ? `${b.label} · ${tr('pending')}` : b.label));
        bub.appendChild(el('div', 'text', b.head));
        if (b.more) {
          const det = el('details', 'more');
          det.appendChild(el('summary', '', tr('Show more')));
          det.appendChild(el('div', 'text', b.more));
          bub.appendChild(det);
        }
        d.appendChild(bub);
        return d;
      }
      case 'assistant': {
        const d = el('div', 'blk msg assistant');
        const md = el('div', 'md');
        md.innerHTML = b.html; // escape-first output of lib/markdown.js (host side)
        codeTools(md, b.needsRun === true);
        d.appendChild(md);
        return d;
      }
      case 'tool': {
        const det = /** @type {HTMLDetailsElement} */ (el('details', `blk tool st-${b.status}`));
        const sum = el('summary', 'row');
        sum.appendChild(el('span', 'dot'));
        sum.appendChild(el('span', 'icon', /** @type {any} */ (ICONS)[b.name] || '•'));
        sum.appendChild(el('span', 'name', b.name));
        const tgt = el('span', 'target', b.target);
        if (b.title) tgt.title = b.title;
        sum.appendChild(tgt);
        sum.appendChild(el('span', 'summary', b.status === 'running' ? tr('running…') : b.summary));
        det.appendChild(sum);
        det.appendChild(el('pre', 'out', b.body || (b.status === 'running' ? tr('No result yet.') : tr('(no output)'))));
        return det;
      }
      case 'denial': {
        const d = el('div', 'blk row denial');
        d.appendChild(el('span', 'icon', '⛔'));
        d.appendChild(el('span', '', `${tr('Permission denied: {0}', b.tool)}${b.reason ? ` (${b.reason})` : ''}`));
        if (b.text) d.title = b.text;
        return d;
      }
      case 'limit': {
        const d = el('div', 'blk row limit');
        d.appendChild(el('span', 'icon', '⏸'));
        d.appendChild(el('span', '', b.text));
        if (b.until) d.appendChild(leftEl(b.until)); // (2026-10-26, limit_continue) "(in 42 min)"
        return d;
      }
      case 'warning': {
        // (2026-10-21, v049 dontask_hold addendum) lost work, e.g. background agents killed at the end
        const d = el('div', 'blk row warning');
        d.appendChild(el('span', 'icon', '⚠'));
        d.appendChild(el('span', '', b.text));
        return d;
      }
      case 'stderr': {
        // (2026-10-20) muted "CLI notes" after a successful attempt, red "stderr" otherwise
        const det = el('details', `blk stderr${b.tone === 'muted' ? ' muted' : ''}`);
        det.appendChild(el('summary', '', b.label || tr('stderr')));
        det.appendChild(el('pre', 'out', b.text));
        return det;
      }
      case 'marker':
        return cutText('div', `blk badge tone-${b.tone}`, b.text);
      case 'result': {
        const d = el('div', `blk result ${b.ok ? 'ok' : 'err'}`);
        if (b.html) {
          const md = el('div', 'md');
          md.innerHTML = b.html; // escape-first output of lib/markdown.js (host side)
          codeTools(md, b.needsRun === true);
          d.appendChild(md);
        }
        d.appendChild(el('div', 'foot', b.text));
        return d;
      }
      case 'working': {
        const d = el('div', 'blk working');
        d.appendChild(el('span', 'spinner'));
        d.appendChild(el('span', '', `${tr('Working…')} `));
        // (2026-10-26, owner d7f2e585) the live activity line, like Claude Code's terminal:
        // "Working… Read(view.js) · 23s · 45.2k context" (the host sends the step and the tokens)
        if (typeof b.step === 'string' && b.step) {
          d.appendChild(el('span', 'step', b.step));
          d.appendChild(el('span', 'sep', '·'));
        }
        const t = el('span', 'elapsed');
        t.dataset.start = b.startedAt || '';
        d.appendChild(t);
        if (typeof b.tokens === 'string' && b.tokens) {
          d.appendChild(el('span', 'sep', '·'));
          const k = el('span', 'tokens', b.tokens);
          k.title = tr("The context of Claude's latest call: input and cached tokens.");
          d.appendChild(k);
        }
        return d;
      }
      default:
        return el('div', 'blk note', b.text || '');
    }
  }

  /** Copy the open/closed state of <details> from an old element to its replacement. @param {HTMLElement} from @param {HTMLElement} to */
  function keepOpen(from, to) {
    const a = from.tagName === 'DETAILS' ? [from, ...from.querySelectorAll('details')] : [...from.querySelectorAll('details')];
    const b = to.tagName === 'DETAILS' ? [to, ...to.querySelectorAll('details')] : [...to.querySelectorAll('details')];
    a.forEach((d, i) => {
      if (b[i] && /** @type {HTMLDetailsElement} */ (d).open) /** @type {HTMLDetailsElement} */ (b[i]).open = true;
    });
  }

  // ---- header ------------------------------------------------------------------------------------
  /** @param {any} h */
  function renderHeader(h) {
    header = h;
    hdr.textContent = '';
    const top = el('div', 'top');
    top.appendChild(el('span', 'title', h.label));
    top.appendChild(el('span', `pill s-${h.status}`, h.statusText || h.status));
    if (h.waitUntil) top.appendChild(leftEl(h.waitUntil)); // (2026-10-26, limit_continue) the time left
    const raw = el('a', 'raw', tr('Raw log'));
    raw.setAttribute('href', '#');
    raw.dataset.action = 'raw';
    raw.title = tr('Open the plain-text log');
    // (2026-10-21) the View dropdown: Full chat or ADHD mode (one choice for every chat, kept by the host)
    viewSel = /** @type {HTMLSelectElement} */ (el('select', 'viewmode'));
    for (const [v, label] of [['full', 'Full chat'], ['adhd', 'ADHD mode']]) {
      const o = /** @type {HTMLOptionElement} */ (el('option', '', tr(label)));
      o.value = v;
      viewSel.appendChild(o);
    }
    viewSel.value = adhd ? 'adhd' : 'full';
    viewSel.title = tr('ADHD mode shows only the questions, the answers and the status; '
      + 'the steps in between fold into "hidden steps" rows (a click opens one). Applies to every Task view.');
    const vs = viewSel;
    vs.addEventListener('change', () => {
      setAdhd(vs.value === 'adhd');
      vscode.postMessage({ type: 'viewMode', adhd });
    });
    top.appendChild(vs);
    top.appendChild(raw);
    hdr.appendChild(top);
    const meta = el('div', 'meta');
    const bits = [];
    if (h.kind === 'goal') bits.push(h.rounds ? tr('goal · round {0}', `${h.rounds.done}${h.rounds.max ? `/${h.rounds.max}` : ''}`) : tr('goal'));
    if (h.profile) bits.push(tr('profile {0}', h.profile));
    // (2026-10-21, owner) which queue this view reads: "shared queue", another project's folder, a
    // lane; nothing for this window's own default queue (an older host without queueLabel: the lane)
    if (typeof h.queueLabel === 'string' && h.queueLabel) bits.push(h.queueLabel);
    else if (!('queueLabel' in h) && h.lane) bits.push(tr('queue {0}', h.lane));
    // (2026-10-21, add panel) the task's own provider · model · effort (text only, never markup)
    if (typeof h.runOn === 'string' && h.runOn) bits.push(tr('runs on {0}', h.runOn));
    if (h.autorun === true) bits.push(tr('Autorun')); // (2026-10-26, modes)
    meta.appendChild(el('span', '', bits.join(' · ')));
    // (2026-10-21, security review M5) a full task says so in red, next to its profile
    if (h.profile === 'full') {
      const b = el('span', 'badge tone-error fullaccess', tr('FULL ACCESS'));
      // (2026-10-26, owner fa388b7b) claudAutopilot.disableFullAccessColor: a small plain note instead of red
      if (h.fullPlain === true) {
        b.className = 'badge fullaccess plain';
        b.textContent = tr('⚠ full access');
      }
      b.title = tr('No permission checks: any command, subagents and web.');
      meta.appendChild(b);
    }
    if (h.cwd) {
      const c = el('span', 'cwd', h.cwd);
      c.title = h.cwdFull || h.cwd;
      meta.appendChild(c);
    }
    hdr.appendChild(meta);
    // (2026-10-29, integrate round 9) a task that works alone in the project folder: why (text only)
    if (typeof h.alone === 'string' && h.alone) hdr.appendChild(el('div', 'alone', h.alone));
    // (2026-10-29, delta M1) files no merge could carry back and the copy they are kept in (text only)
    if (typeof h.notMerged === 'string' && h.notMerged) hdr.appendChild(el('div', 'alone notmerged', h.notMerged));
    // (2026-10-21) when the prompt cache runs out; tick() counts down and warns in the last 15 min
    cacheEl = null;
    if (h.cacheUntil) {
      cacheEl = el('div', 'cache');
      cacheEl.dataset.until = h.cacheUntil;
      cacheEl.title = tr('A reply while the cache is warm reuses the conversation cheaply; '
        + 'after that the whole conversation is read again at the full input price.');
      hdr.appendChild(cacheEl);
      cacheTick();
    }
    if (h.error) hdr.appendChild(cutText('div', 'why', h.error));
    document.body.classList.toggle('item-running', !!h.running);
  }

  // ---- composer ----------------------------------------------------------------------------------
  /** @type {HTMLTextAreaElement} */
  let ta;
  /** @type {HTMLSelectElement} */
  let sel;
  /** @type {HTMLButtonElement} */
  let btn;
  /** @type {HTMLElement} */
  let hint;
  /** @type {HTMLElement} */
  let qbox;
  /** (2026-10-20, 0.4.4 logfix A/C) the repeat-block notice and the action buttons @type {HTMLElement} */
  let helpBox;
  /** (2026-10-26, edit in box) @type {HTMLElement} */
  let editBox;
  /** (2026-10-26, pen) the ✎ in the reply box: Edit task… / Edit reply… without the button row @type {HTMLButtonElement} */
  let penBtn;
  let penId = '';
  /** @type {HTMLElement} */
  let editLab;
  let helpSig = '';
  /** (0.4.6) the "Auto-pick best" box and its label @type {HTMLInputElement} */
  let auto;
  /** @type {HTMLElement} */
  let autoWrap;
  let autoSig = '';
  /** (2026-10-27, modes fix round 2) this task's own Autorun box and its label @type {HTMLInputElement} */
  let autorunBox;
  /** @type {HTMLElement} */
  let autorunWrap;
  let autorunSig = '';
  // the owner set the box in this chat: only then is it kept in the webview state over a reload
  let autoTouched = typeof saved.autoPick === 'boolean';
  /** (2026-10-21) the quick-prompt buttons (claudAutopilot.quickPrompts) @type {HTMLElement} */
  let promptBox;
  /** (2026-10-21, start now) the "Start now" row of a pending task @type {HTMLElement} */
  let startBox;
  /** @type {HTMLButtonElement} */
  let startBtn;
  /** (2026-10-21, attach) the "Add picture or file" button in the bottom bar @type {HTMLButtonElement} */
  let attachBtn;
  /** (2026-10-21, subagents) the bottom bar's subagent-limit dropdown @type {HTMLSelectElement} */
  let agentSel;
  let agentSig = '';
  let promptSig = '';
  let optSig = '';
  let qSig = '';
  let selTouched = false; // the owner picked a profile by hand: an option button does not override it
  let sentChoice = false; // the pending send came from an option button (the draft is kept)

  function buildComposer() {
    composer.textContent = '';
    helpBox = el('div', 'help');
    helpBox.hidden = true;
    qbox = el('div', 'question');
    qbox.hidden = true;
    promptBox = el('div', 'prompts acts');
    promptBox.hidden = true;
    // (2026-10-21, start now) a pending task: to the front of its queue and its runner started (host)
    startBox = el('div', 'help startnow');
    startBox.hidden = true;
    startBtn = /** @type {HTMLButtonElement} */ (el('button', 'act', `▶ ${tr('Start now')}`));
    startBtn.title = tr('Run this task next: it moves to the front of its queue and the runner starts. '
      + 'A runner that is busy finishes its current task first.');
    startBtn.addEventListener('click', () => {
      if (startBtn.disabled) return;
      startBtn.disabled = true; // one click; the next patch (or 3 s) brings it back if still pending
      setTimeout(() => { startBtn.disabled = false; }, 3000);
      vscode.postMessage({ type: 'action', id: 'start-now' });
    });
    startBox.appendChild(startBtn);
    // (2026-10-26, edit in box) shown while the box holds the task's (or the queued reply's) text
    editBox = el('div', 'help editing');
    editBox.hidden = true;
    editLab = el('span', 'notice');
    const cancelEdit = /** @type {HTMLButtonElement} */ (el('button', 'act secondary', tr('Cancel edit')));
    cancelEdit.title = tr('Keep the text as it was; your earlier draft comes back into the box');
    cancelEdit.addEventListener('click', () => {
      if (sending) return;
      vscode.postMessage({ type: 'cancelEdit' });
      endEdit();
    });
    editBox.appendChild(editLab);
    editBox.appendChild(cancelEdit);
    ta = /** @type {HTMLTextAreaElement} */ (el('textarea'));
    // (2026-10-26, pen) the box and its ✎ (top right) share a wrapper; shown while an edit is offered
    const taWrap = el('div', 'tawrap');
    penBtn = /** @type {HTMLButtonElement} */ (el('button', 'pen', '✎'));
    penBtn.hidden = true;
    penBtn.addEventListener('click', () => {
      if (penId && !sending && !editing) vscode.postMessage({ type: 'action', id: penId });
    });
    ta.rows = 3;
    ta.maxLength = REPLY_MAX;
    ta.placeholder = tr('Reply to Claude… (Enter to send, Shift+Enter for a new line)');
    ta.value = typeof saved.draft === 'string' ? saved.draft : '';
    ta.addEventListener('input', () => { stickyNote = ''; lateNote = ''; saveState(); });
    // (2026-10-21) Enter sends (Ctrl+Enter still does); Shift+Enter is a new line; an IME composition
    // (keyCode 229) keeps its own Enter
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.isComposing && e.keyCode !== 229) {
        e.preventDefault();
        send();
      }
    });
    const bar = el('div', 'bar');
    sel = /** @type {HTMLSelectElement} */ (el('select'));
    sel.title = tr('Permission profile for the resumed session');
    sel.addEventListener('change', () => { selTouched = true; });
    // (0.4.6) the per-chat "Auto-pick best" box; the host owns the value and sends it back in `patch`
    autoWrap = el('label', 'autopick');
    auto = /** @type {HTMLInputElement} */ (el('input'));
    auto.type = 'checkbox';
    if (typeof saved.autoPick === 'boolean') auto.checked = saved.autoPick;
    auto.addEventListener('change', () => {
      autoTouched = true;
      saveState();
      vscode.postMessage({ type: 'autoPick', on: auto.checked });
    });
    autoWrap.appendChild(auto);
    autoWrap.appendChild(el('span', '', tr('Auto-pick best')));
    autoWrap.title = AUTO_TIP;
    // (2026-10-27, modes fix round 2) the task's Autorun (`edit --autorun`, also while it runs); the host owns
    // the value (the core's item field) and sends it back in `patch`, so a refused change shows the old value
    autorunWrap = el('label', 'autopick autorun');
    autorunBox = /** @type {HTMLInputElement} */ (el('input'));
    autorunBox.type = 'checkbox';
    autorunBox.addEventListener('change', () => {
      autorunSig = '';
      vscode.postMessage({ type: 'autorun', on: autorunBox.checked });
    });
    autorunWrap.appendChild(autorunBox);
    autorunWrap.appendChild(el('span', '', tr('Autorun')));
    autorunWrap.hidden = true;
    // (2026-10-21, subagents) the task's own subagent limit; the host owns the value (the core's item
    // field) and sends it back in `patch`, so a failed change shows up as the old value again
    agentSel = /** @type {HTMLSelectElement} */ (el('select', 'agents'));
    agentSel.title = AGENTS_TIP;
    agentSel.addEventListener('change', () => {
      agentSig = '';   // the next patch re-applies the host's value, whatever happened
      vscode.postMessage({ type: 'maxAgents', value: agentSel.value });
    });
    btn = /** @type {HTMLButtonElement} */ (el('button', '', tr('Send')));
    btn.addEventListener('click', send);
    hint = el('span', 'hint');
    // (2026-10-21, attach) the host opens the file dialog and answers with `attached` (the paths)
    attachBtn = /** @type {HTMLButtonElement} */ (el('button', 'act secondary attach', `📎 ${tr('Add picture or file')}`));
    attachBtn.title = tr('Add pictures or files to your reply: their paths go into the reply box, and Claude opens them '
      + '(it can see pictures). Example: a screenshot of the error, a log file, a design mock-up.');
    attachBtn.addEventListener('click', () => {
      if (!attachBtn.disabled) vscode.postMessage({ type: 'attach' });
    });
    // (2026-10-26, owner) no ⚙ Chat settings here any more: it moved to the New Task panel's Other settings
    bar.appendChild(attachBtn);
    bar.appendChild(sel);
    bar.appendChild(agentSel);
    bar.appendChild(autoWrap);
    bar.appendChild(autorunWrap);
    bar.appendChild(hint);
    bar.appendChild(btn);
    composer.appendChild(startBox);
    composer.appendChild(helpBox);
    composer.appendChild(qbox);
    composer.appendChild(editBox);
    composer.appendChild(promptBox);
    taWrap.appendChild(ta);
    taWrap.appendChild(penBtn);
    composer.appendChild(taWrap);
    composer.appendChild(bar);
  }

  /**
   * (2026-10-20, 0.4.4 logfix A/C) `notice` (the same question as last time) and `actions` ({id, label}:
   * "Retry with shell-dev", "Edit allowed commands…"). A button sends only its id; the host acts.
   * (2026-10-21, v049 dontask_hold) `denial` (chatmodel.denialCard): what don't-ask mode denied, the fix,
   * and each allow-list entry as selectable text with a Copy button (the host copies its own copy).
   * @param {string | null | undefined} notice @param {{id: string, label: string}[] | undefined} actions
   * @param {any} [denial]
   */
  function renderHelp(notice, actions, denial, merge) {
    const list = Array.isArray(actions) ? actions : [];
    const card = denial && typeof denial === 'object' ? denial : null;
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.6) the merge card (chatmodel.composerState.merge)
    const mcard = merge && typeof merge === 'object' ? merge : null;
    const sig = JSON.stringify([notice || '', list, card, mcard]);
    if (sig === helpSig) return;
    helpSig = sig;
    helpBox.textContent = '';
    helpBox.hidden = !notice && !list.length && !card && !mcard;
    if (notice) helpBox.appendChild(el('div', 'notice', notice));
    if (mcard) helpBox.appendChild(mergeBox(mcard));
    if (card) helpBox.appendChild(denialBox(card));
    if (!list.length) return;
    const row = el('div', 'acts');
    for (const a of list) {
      const b = /** @type {HTMLButtonElement} */ (el('button', 'act secondary', a.label));
      if (a.id === 'ask-changes') {
        // (2026-10-21) a reply like an option button: the host sends claudAutopilot.changesQuestion
        b.title = tr('Ask Claude which files it changed (paths and why); the text is the claudAutopilot.changesQuestion setting');
        b.addEventListener('click', () => askChanges(a.label));
      } else {
        b.addEventListener('click', () => vscode.postMessage({ type: 'action', id: a.id }));
      }
      row.appendChild(b);
    }
    helpBox.appendChild(row);
  }

  /**
   * (2026-10-27, U4, SPEC_V05_PARALLEL 8.6) The merge card: its title, the paths (each with Preview when the core
   * wrote a preview), "… and n more", Open copy, and one button per action (the recommended one first and primary).
   * Text only (textContent). A button sends only {type: 'merge', action, gen}, {type: 'compare', path} or the
   * 'open-copy' action; the host checks each against its own copy of the card (chatview.validateMessage).
   * @param {{title?: string, paths?: string[], more?: number, preview?: boolean, copy?: boolean, gen?: number,
   *   actions?: {action: string, label: string, recommended?: boolean}[]}} card
   */
  function mergeBox(card) {
    const box = el('div', 'merge');
    box.appendChild(el('div', 'merge-title', String(card.title || '')));
    for (const p of Array.isArray(card.paths) ? card.paths : []) {
      const line = el('div', 'merge-path');
      line.appendChild(el('code', '', String(p)));
      if (card.preview) {
        const b = /** @type {HTMLButtonElement} */ (el('button', 'act secondary preview', tr('Preview')));
        // (2026-10-30, M5) not "read-only": the preview opens as a normal editor; edits to the copy are simply not used
        b.title = tr('Open the merged file with its conflict markers beside this view (a copy outside the project: edits to it are not used, and the project is not changed)');
        b.addEventListener('click', () => vscode.postMessage({ type: 'compare', path: String(p) }));
        line.appendChild(b);
      }
      box.appendChild(line);
    }
    if (Number(card.more) > 0) box.appendChild(el('div', 'merge-more', tr('… and {0} more', Number(card.more))));
    const row = el('div', 'acts');
    if (card.copy) {
      const b = /** @type {HTMLButtonElement} */ (el('button', 'act secondary', tr('Open copy')));
      b.title = tr("Open the task's private copy of the project in the file manager");
      b.addEventListener('click', () => vscode.postMessage({ type: 'action', id: 'open-copy' }));
      row.appendChild(b);
    }
    for (const a of Array.isArray(card.actions) ? card.actions : []) {
      const b = /** @type {HTMLButtonElement} */ (el('button', a.recommended ? 'act' : 'act secondary', String(a.label || '')));
      b.addEventListener('click', () => vscode.postMessage({ type: 'merge', action: String(a.action), gen: Number(card.gen) }));
      row.appendChild(b);
    }
    box.appendChild(row);
    return box;
  }

  /**
   * (2026-10-21, v049 dontask_hold) The denial card: text only (textContent), never HTML from the item.
   * @param {{title?: string, rows?: {text: string, note: string}[], hint?: string, allow?: {n: number, text: string}[], copyLabel?: string}} card
   */
  function denialBox(card) {
    const box = el('div', 'denial');
    box.appendChild(el('div', 'denial-title', String(card.title || '')));
    for (const r of Array.isArray(card.rows) ? card.rows : []) {
      const line = el('div', 'denial-row');
      line.appendChild(el('code', '', String(r.text || '')));
      if (r.note) line.appendChild(el('span', 'denial-note', ` — ${r.note}`));
      box.appendChild(line);
    }
    if (card.hint) box.appendChild(el('div', 'denial-hint', String(card.hint)));
    for (const a of Array.isArray(card.allow) ? card.allow : []) {
      const line = el('div', 'denial-allow');
      line.appendChild(el('code', 'allow-entry', String(a.text || '')));
      const b = /** @type {HTMLButtonElement} */ (el('button', 'act secondary copy', String(card.copyLabel || tr('Copy'))));
      b.addEventListener('click', () => vscode.postMessage({ type: 'action', id: 'copy-allow', n: a.n }));
      line.appendChild(b);
      box.appendChild(line);
    }
    return box;
  }

  // ---- (0.4.1) option buttons of a blocked question -------------------------------------------------
  /** @param {any} q null, or {text, options: [{n, label, detail, recommended, shell}]} (recommended first) */
  function renderQuestion(q) {
    const sig = JSON.stringify(q || null);
    if (sig === qSig) return;
    qSig = sig;
    selTouched = false;
    qbox.textContent = '';
    qbox.hidden = !q;
    if (!q) return;
    if (q.text) qbox.appendChild(cutText('div', 'qtext', q.text));
    const row = el('div', 'opts');
    for (const o of q.options) {
      const b = /** @type {HTMLButtonElement} */ (el('button', `opt ${o.recommended ? 'primary' : 'secondary'}`));
      const top = el('span', 'otop');
      top.appendChild(el('span', 'onum', `${o.n}`));
      top.appendChild(el('span', 'olabel', o.label));
      if (o.recommended) top.appendChild(el('span', 'otag', tr('Recommended')));
      b.appendChild(top);
      if (o.detail) b.appendChild(el('span', 'odetail', o.detail));
      const tip = [tr('Reply "{0}"', `Option ${o.n}: ${o.label}`)];
      if (o.detail) tip.push(o.detail);
      if (o.shell) tip.push(tr('Resumes with the shell-read profile unless you picked one below.'));
      tip.push(tr('Click: send this answer now. Shift+click or Alt+click: put it in the reply box to edit first.'));
      b.title = tip.join('\n');
      // (2026-10-26, owner 22536a8c) a click sends at once (once: `sending` locks every button);
      // Shift/Alt+click only fills the reply box, to edit before sending
      b.addEventListener('click', (e) => (e && (e.shiftKey || e.altKey) ? editChoice(o) : choose(o)));
      row.appendChild(b);
    }
    const other = /** @type {HTMLButtonElement} */ (el('button', 'opt secondary other', tr('Other…')));
    other.title = tr('Write your own reply below');
    other.addEventListener('click', () => ta.focus());
    row.appendChild(other);
    qbox.appendChild(row);
    lockQuestion();
  }

  function lockQuestion() {
    for (const b of qbox.querySelectorAll('button')) /** @type {HTMLButtonElement} */ (b).disabled = sending || ta.disabled;
    for (const b of promptBox.querySelectorAll('button')) /** @type {HTMLButtonElement} */ (b).disabled = sending || ta.disabled;
    attachBtn.disabled = sending || ta.disabled; // (2026-10-21, attach) only while a reply can be written
  }

  /**
   * (2026-10-21, attach) The host's `attached` paths: listed at the end of the draft under one
   * ATTACH_HEAD line (added once), one "- path" line each; a path already in the draft is skipped.
   * @param {unknown} paths
   */
  function addPaths(paths) {
    if (ta.disabled || !Array.isArray(paths)) return;
    let draft = ta.value.replace(/\s+$/, '');
    const fresh = paths.filter((p) => typeof p === 'string' && p && !draft.split('\n').includes(`- ${p}`));
    if (!fresh.length) return;
    if (!draft.split('\n').includes(ATTACH_HEAD)) draft = draft ? `${draft}\n\n${ATTACH_HEAD}` : ATTACH_HEAD;
    ta.value = `${draft}\n${fresh.map((p) => `- ${p}`).join('\n')}\n`.slice(0, REPLY_MAX);
    ta.focus();
    saveState();
  }

  // ---- (2026-10-21) quick prompts: one click sends the text as a reply; Shift+click puts it in the box --
  /** @param {any} list [{i, label, text}] from the host (chatmodel.composerState), [] = none */
  function renderPrompts(list) {
    const ps = Array.isArray(list) ? list : [];
    const sig = JSON.stringify(ps);
    if (sig === promptSig) return;
    promptSig = sig;
    promptBox.textContent = '';
    promptBox.hidden = !ps.length;
    for (const p of ps) {
      const b = /** @type {HTMLButtonElement} */ (el('button', 'act secondary prompt', p.label));
      b.title = `${p.text}\n\n${tr('Click: send this as your reply. Shift+click: put it in the reply box to edit first.')}`;
      b.addEventListener('click', (e) => {
        if (e.shiftKey) insertPrompt(p.text);
        else sendPrompt(p);
      });
      promptBox.appendChild(b);
    }
  }

  /** Shift+click: add the text to the draft (on a new line) instead of sending it. @param {string} text */
  function insertPrompt(text) {
    if (ta.disabled) return;
    const draft = ta.value.replace(/\s+$/, '');
    ta.value = (draft ? `${draft}\n${text}` : text).slice(0, REPLY_MAX);
    ta.focus();
    saveState();
  }

  /** A click: a pending reply bubble, then the host answers with replyResult. @param {any} p */
  function sendPrompt(p) {
    if (sending || ta.disabled) return;
    sending = true;
    sentChoice = true; // a typed draft stays
    btn.disabled = true;
    sel.disabled = true;
    lockQuestion();
    optimistic = render({ kind: 'user', label: tr("Owner's reply"), pending: true, head: p.text, more: '' });
    optimistic.classList.add('sending');
    const lab = optimistic.querySelector('.label');
    if (lab) lab.textContent = tr("Owner's reply · sending…");
    log.appendChild(optimistic);
    toBottom();
    vscode.postMessage({ type: 'prompt', i: p.i, text: p.text, profile: sel.value });
  }

  /**
   * (0.4.2 L2) The profile an option button sends: the dropdown's value only when the owner picked
   * one by hand; otherwise the option's own profile (host-computed, format.optionProfile; '' = keep).
   * The dropdown's default follows the question text and is meant for a free-text reply.
   * @param {any} o @param {boolean} touched @param {string} selValue
   */
  function choiceProfile(o, touched, selValue) {
    return touched ? selValue : (typeof o.profile === 'string' ? o.profile : '');
  }

  /**
   * (2026-10-26, owner 22536a8c) Shift/Alt+click on an option: its reply text goes into the box (on a
   * new line) to edit first; a shell option's profile is preselected unless the owner picked one.
   * @param {any} o
   */
  function editChoice(o) {
    if (sending || ta.disabled) return;
    const p = choiceProfile(o, selTouched, sel.value);
    if (!selTouched && p && Array.from(sel.options || []).some((x) => x.value === p)) sel.value = p;
    insertPrompt(`Option ${o.n}: ${o.label}`);
  }

  /** @param {any} o */
  function choose(o) {
    if (sending || ta.disabled) return;
    const profile = choiceProfile(o, selTouched, sel.value);
    sending = true;
    sentChoice = true;
    btn.disabled = true;
    sel.disabled = true;
    lockQuestion();
    optimistic = render({ kind: 'user', label: tr("Owner's reply"), pending: true, head: `Option ${o.n}: ${o.label}`, more: '' });
    optimistic.classList.add('sending');
    const lab = optimistic.querySelector('.label');
    if (lab) lab.textContent = tr("Owner's reply · sending…");
    log.appendChild(optimistic);
    toBottom();
    vscode.postMessage({ type: 'choose', n: o.n, profile });
  }

  /** (2026-10-21) "What did you change?": a pending reply bubble, then the host answers with replyResult. @param {string} label */
  function askChanges(label) {
    if (sending || ta.disabled) return;
    sending = true;
    sentChoice = true; // a typed draft stays
    btn.disabled = true;
    sel.disabled = true;
    lockQuestion();
    optimistic = render({ kind: 'user', label: tr("Owner's reply"), pending: true, head: label, more: '' });
    optimistic.classList.add('sending');
    const lab = optimistic.querySelector('.label');
    if (lab) lab.textContent = tr("Owner's reply · sending…");
    log.appendChild(optimistic);
    toBottom();
    vscode.postMessage({ type: 'action', id: 'ask-changes' });
  }

  /** @param {any} c */
  function updateComposer(c) {
    lastComposer = c;
    // (2026-10-26, edit in box) the task started (or the reply went out) before the edit was saved:
    // the edit is over, the text stays in the box as a draft
    if (editing && !sending) {
      const id = editing.reply ? 'edit-reply' : 'edit-task';
      if (!(Array.isArray(c.actions) ? c.actions : []).some((/** @type {any} */ a) => a && a.id === id)) {
        endEdit(true);
        lateNote = tr('Too late to edit: Claude already has the text. Your text stays in the box.');
      }
    }
    const sig = JSON.stringify(c.options);
    if (sig !== optSig) {
      optSig = sig;
      const keep = sel.value;
      sel.textContent = '';
      for (const o of c.options) {
        const opt = /** @type {HTMLOptionElement} */ (el('option', '', o.label));
        opt.value = o.value;
        sel.appendChild(opt);
      }
      sel.value = c.options.some((/** @type {any} */ o) => o.value === keep) && keep !== '' ? keep : c.value;
    }
    // (0.4.6) the host is the source of truth for the box; the signature gate keeps a patch that
    // crossed a click from flipping it back (an unchanged value is never re-applied)
    const a = c.autoPick || { on: false, stopped: false };
    const aSig = JSON.stringify([!!a.on, !!a.stopped, !!a.autorun]);
    if (aSig !== autoSig) {
      autoSig = aSig;
      auto.checked = !!a.on;
      auto.disabled = !!a.autorun;
      autoWrap.classList.toggle('stopped', !!a.stopped);
      autoWrap.title = a.autorun ? AUTORUN_BOX_TIP : a.stopped ? AUTO_TIP_STOPPED : AUTO_TIP;
    }
    // (2026-10-27, modes fix round 2) the Autorun box: the host's value wins; hidden when the task is gone
    const ar = c.autorun || null;
    const arSig = JSON.stringify(ar ? [!!ar.on, ar.tip || ''] : null);
    if (arSig !== autorunSig) {
      autorunSig = arSig;
      autorunWrap.hidden = !ar;
      autorunBox.checked = !!(ar && ar.on);
      autorunWrap.title = ar && ar.tip ? ar.tip : '';
    }
    // (2026-10-21, subagents) the subagent dropdown: the host's value wins, and it stays usable while
    // the task waits in the queue (only a RUNNING task refuses the change: `agents.on`)
    const ag = c.agents || null;
    if (ag) {
      const agSig = JSON.stringify([ag.value, ag.options, !!ag.on]);
      if (agSig !== agentSig) {
        agentSig = agSig;
        agentSel.textContent = '';
        for (const o of ag.options) {
          const opt = /** @type {HTMLOptionElement} */ (el('option', '', o.label));
          opt.value = o.value;
          agentSel.appendChild(opt);
        }
        agentSel.value = ag.value;
        agentSel.disabled = !ag.on;
        agentSel.title = ag.on ? AGENTS_TIP : AGENTS_TIP_RUNNING;
      }
    }
    // (2026-10-26, edit in box) an edit is always writable, whatever the task's status lets a reply do
    const writable = !!c.enabled || !!c.queueable || !!editing; // (2026-10-26, write while working)
    canReplyNow = !!c.enabled;
    const on = writable && !sending;
    ta.disabled = !writable;
    sel.disabled = !on || !!editing;
    btn.disabled = !on;
    hint.textContent = lateNote || (c.enabled || editing ? stickyNote : c.hint || '');
    composer.classList.toggle('off', !writable);
    startBox.hidden = !c.startNow; // (2026-10-21, start now)
    // (2026-10-26, pen) the queued reply first (as the host's editSource), else the task text
    const ids = (Array.isArray(c.actions) ? c.actions : []).map((/** @type {any} */ x) => x && x.id);
    penId = ids.includes('edit-reply') ? 'edit-reply' : ids.includes('edit-task') ? 'edit-task' : '';
    penBtn.hidden = !penId || !!editing;
    penBtn.disabled = sending;
    penBtn.title = penId === 'edit-reply' ? tr('Edit your queued reply here in the box')
      : tr('Edit your task text here in the box');
    renderHelp(c.notice, c.actions, c.denial, c.merge);
    renderQuestion(c.enabled ? c.question || null : null);
    renderPrompts(c.enabled ? c.prompts : []);
    lockQuestion();
    // (2026-10-26, write while working) the turn ended: send the queued message, or (a stop that does
    // not take it: failed, cancelled, paused, gone) put it back into the box
    if (queued && !sending && !c.queueable && !editing) {
      if (c.enabled && c.autoSend) sendQueued();
      else unqueue(tr('The task stopped without finishing; your queued message is back in the box.'));
    }
  }

  /** (2026-10-26, write while working) the queued message's bubble at the end of the log */
  function renderQueued() {
    if (queuedEl) queuedEl.remove();
    queuedEl = null;
    if (!queued) return;
    queuedEl = render({ kind: 'user', label: tr("Owner's reply"), pending: false, head: queued, more: '' });
    queuedEl.classList.add('queued');
    const lab = queuedEl.querySelector('.label');
    if (lab) lab.textContent = `${tr("Owner's reply")} · ${tr('Queued: sent when Claude stops')}`;
    const back = /** @type {HTMLButtonElement} */ (el('button', 'act secondary', tr('Back to the box')));
    back.title = tr('Put the queued message back in the reply box to edit it');
    back.addEventListener('click', () => unqueue(''));
    queuedEl.appendChild(back);
    log.appendChild(queuedEl);
    toBottom();
  }

  /** The queued text goes back to the top of the draft. @param {string} note */
  function unqueue(note) {
    const text = queued;
    queued = '';
    renderQueued();
    // (2026-10-26, edit in box) never into an edit: it joins the draft that comes back after it
    if (text && editing) editStash = (editStash.trim() ? `${text}\n\n${editStash}` : text).slice(0, REPLY_MAX);
    else if (text) ta.value = (ta.value.trim() ? `${text}\n\n${ta.value}` : text).slice(0, REPLY_MAX);
    stickyNote = note;
    if (note) hint.textContent = note;
    saveState();
  }

  function sendQueued() {
    const text = queued;
    queued = '';
    renderQueued();
    saveState();
    sentQueued = text;
    post(text);
  }

  /**
   * (2026-10-26, edit in box) The host's `editText`: the task's (or queued reply's) current text goes
   * into the box; the draft that was there waits in `editStash`.
   * (2026-10-26, edit paused) `restart`: the task has started; Save starts it over with the new text.
   * @param {{text: unknown, reply: unknown, restart?: unknown}} m
   */
  function startEdit(m) {
    if (typeof m.text !== 'string' || sending) return;
    if (!editing) editStash = ta.value;
    editing = { reply: !!m.reply };
    ta.value = m.text.slice(0, REPLY_MAX);
    editLab.textContent = editing.reply ? tr('Editing your queued reply: change it, then Save.')
      : m.restart ? tr('Editing the task text: Save starts the task over with it (the old session is not continued).')
        : tr('Editing the task text: change it, then Save.');
    editBox.hidden = false;
    btn.textContent = tr('Save');
    stickyNote = '';
    if (lastComposer) updateComposer(lastComposer);
    else { ta.disabled = false; btn.disabled = false; }
    lockQuestion();
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    saveState();
  }

  /** The edit is over: the earlier draft comes back (`keep`: the edited text stays instead). @param {boolean} [keep] */
  function endEdit(keep) {
    if (!editing) return;
    editing = null;
    ta.value = keep ? ta.value : editStash;
    editStash = '';
    editBox.hidden = true;
    btn.textContent = tr('Send');
    stickyNote = '';
    if (lastComposer) updateComposer(lastComposer);
    lockQuestion();
    saveState();
  }

  /** (2026-10-26, edit in box) the host saved the edit, or says why not @param {{ok: boolean, error: string}} r */
  function editResult(r) {
    sending = false;
    if (r.ok) endEdit();
    else if (r.stale) { endEdit(true); stickyNote = r.error || ''; } // the host has no edit open any more
    else stickyNote = r.error ? tr('Not saved: {0}', r.error) : tr('Not saved.');
    if (lastComposer) updateComposer(lastComposer);
    else btn.disabled = ta.disabled;
    hint.textContent = r.ok ? tr('Saved.') : stickyNote;
    lockQuestion();
  }

  function send() {
    if (btn.disabled || sending) return;
    const text = ta.value;
    if (!text.trim()) return;
    lateNote = '';
    if (editing) {
      sending = true;
      btn.disabled = true;
      lockQuestion();
      vscode.postMessage({ type: 'saveEdit', text });
      return;
    }
    if (!canReplyNow) {
      // (2026-10-26, write while working) Claude is working: queue it (a second one joins the first)
      queued = (queued ? `${queued}\n\n${text}` : text).slice(0, REPLY_MAX);
      ta.value = '';
      renderQueued();
      saveState();
      return;
    }
    sentQueued = '';
    post(text);
  }

  /** @param {string} text */
  function post(text) {
    sending = true;
    stickyNote = '';
    sentChoice = !!sentQueued; // a queued message leaves the new draft alone, as an option button does
    btn.disabled = true;
    sel.disabled = true;
    lockQuestion();
    optimistic = render({ kind: 'user', label: tr("Owner's reply"), pending: true, head: text, more: '' });
    optimistic.classList.add('sending');
    const lab = optimistic.querySelector('.label');
    if (lab) lab.textContent = tr("Owner's reply · sending…");
    log.appendChild(optimistic);
    toBottom();
    vscode.postMessage({ type: 'reply', text, profile: sel.value });
  }

  /** (2026-10-29, integrate round 9) `sent`: the reply was sent but became a refused merge answer.
   * @param {{ok: boolean, error: string, sent?: boolean}} r */
  function replyResult(r) {
    sending = false;
    if (optimistic) optimistic.remove();
    optimistic = null;
    if (r.ok) {
      if (!sentChoice) ta.value = ''; // an option button leaves a typed draft alone
      saveState();
      hint.textContent = '';
    } else {
      hint.textContent = r.error ? (r.sent === true ? tr('Sent, but {0}', r.error) : tr('Not sent: {0}', r.error)) : tr('Not sent.');
      // (2026-10-26, write while working) a queued message that failed goes back into the box
      if (sentQueued) ta.value = (ta.value.trim() ? `${sentQueued}

${ta.value}` : sentQueued).slice(0, REPLY_MAX);
      saveState();
    }
    sentQueued = '';
    btn.disabled = ta.disabled;
    sel.disabled = ta.disabled;
    lockQuestion();
  }

  /**
   * (0.4.6) The host answered a blocked question by itself, or stopped doing it: a note in the log.
   * The composer is NOT touched (no pending bubble of ours to clear, and a typed draft stays).
   * (2026-10-26, modes) `autorun`: the task's own Autorun answered: one "Autorun picked: …" line.
   * (2026-10-29, F4, SEC-L22) `paused`: a full task waits for the owner's OK; `error` is the host's whole sentence
   * ("Autorun paused: …", already in the UI language), shown without the "Autorun:" prefix.
   * @param {{ok: boolean, text: string, error: string, autorun?: boolean, paused?: boolean}} m
   */
  function autoPicked(m) {
    const follow = atBottom();
    const d = m.ok ? el('div', 'blk note auto', tr(m.autorun ? 'Autorun picked: {0}' : 'Auto-picked {0}', m.text))
      : m.paused === true ? el('div', 'blk row denial', String(m.error || ''))
        : el('div', 'blk row denial', tr(m.autorun ? 'Autorun: {0}' : 'Auto-pick: {0}', m.error || tr('nothing was sent')));
    log.appendChild(d);
    if (follow) toBottom();
  }

  // ---- (2026-10-21) ADHD mode ----------------------------------------------------------------------
  /** Drop the "hidden steps" rows (before a patch reorders the log). */
  function unfold() {
    for (const f of [...log.querySelectorAll('.adhd-fold')]) f.remove();
  }

  /** Hide what adhdShown() leaves out; each run of hidden rows gets one row that opens it in place. */
  function fold() {
    unfold();
    const kids = /** @type {HTMLElement[]} */ ([...log.children]);
    for (const e of kids) e.classList.remove('quiet', 'shown');
    if (!adhd) return;
    const shown = adhdShown(kids.map((e) => ({ kind: e.dataset.kind || '', key: e.dataset.key || '', answer: e.dataset.answer === '1' })));
    let i = 0;
    while (i < kids.length) {
      if (shown[i]) {
        i += 1;
        continue;
      }
      let j = i;
      while (j < kids.length && !shown[j]) j += 1;
      const run = kids.slice(i, j);
      const k = run[0].dataset.key || `#${i}`;
      const open = openFolds.has(k);
      for (const e of run) {
        e.classList.add('quiet');
        if (open) e.classList.add('shown');
      }
      const f = el('div', 'blk adhd-fold', `${open ? '▾' : '▸'} ${tr(run.length === 1 ? '{0} hidden step' : '{0} hidden steps', run.length)}`);
      f.setAttribute('role', 'button');
      // (2026-10-26) a keyboard can reach and open it too (Tab, then Enter or Space), and a screen
      // reader hears whether it is open; the rebuilt row gets the focus back
      f.tabIndex = 0;
      f.setAttribute('aria-expanded', open ? 'true' : 'false');
      f.dataset.fold = k;
      f.title = tr(open ? 'Hide these steps again' : 'Show the hidden steps (tool calls, interim messages, notes)');
      const flip = () => {
        if (openFolds.has(k)) openFolds.delete(k);
        else openFolds.add(k);
        fold();
      };
      f.addEventListener('click', flip);
      f.addEventListener('keydown', (/** @type {KeyboardEvent} */ ev) => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        flip();
        const again = [...log.querySelectorAll('.adhd-fold')].find((x) => /** @type {HTMLElement} */ (x).dataset.fold === k);
        if (again) /** @type {HTMLElement} */ (again).focus();
      });
      log.insertBefore(f, run[0]);
      i = j;
    }
  }

  /** @param {boolean} on */
  function setAdhd(on) {
    if (on === adhd) return;
    const follow = atBottom();
    adhd = on;
    document.body.classList.toggle('adhd', adhd);
    if (viewSel) viewSel.value = adhd ? 'adhd' : 'full';
    fold();
    if (follow) toBottom();
  }

  // ---- patches -----------------------------------------------------------------------------------
  /** @param {any} m */
  function patch(m) {
    const follow = atBottom();
    unfold();
    if (m.reset) {
      log.textContent = '';
      els.clear();
    }
    if (typeof m.adhd === 'boolean' && m.adhd !== adhd) {
      adhd = m.adhd;
      document.body.classList.toggle('adhd', adhd);
      if (viewSel) viewSel.value = adhd ? 'adhd' : 'full';
    }
    if (m.header) renderHeader(m.header);
    if (m.composer) updateComposer(m.composer);
    for (const b of m.upserts || []) {
      const fresh = render(b);
      fresh.dataset.key = b.key;
      fresh.dataset.kind = b.kind; // (2026-10-21) ADHD mode decides by kind (adhdShown)
      if (b.kind === 'result' && b.html) fresh.dataset.answer = '1';
      const old = els.get(b.key);
      if (old) {
        keepOpen(old, fresh);
        old.replaceWith(fresh);
      }
      els.set(b.key, fresh);
    }
    if (m.order) {
      const keep = new Set(m.order);
      for (const [k, e] of [...els]) {
        if (!keep.has(k)) {
          e.remove();
          els.delete(k);
        }
      }
      let prev = /** @type {Element | null} */ (null);
      for (const k of m.order) {
        const e = els.get(k);
        if (!e) continue;
        const want = prev ? prev.nextElementSibling : log.firstElementChild;
        if (want !== e) log.insertBefore(e, want);
        prev = e;
      }
    }
    if (optimistic) log.appendChild(optimistic);
    if (queuedEl) log.appendChild(queuedEl); // (2026-10-26, write while working) stays last
    fold();
    tick();
    if (follow || m.reset) toBottom();
  }

  function tick() {
    for (const t of log.querySelectorAll('.elapsed')) {
      const start = Date.parse(/** @type {HTMLElement} */ (t).dataset.start || '');
      t.textContent = Number.isNaN(start) ? '' : elapsed(Date.now() - start);
    }
    cacheTick();
    // (2026-10-26, limit_continue) the usage-limit countdowns (the header's and the limit rows')
    for (const t of [...hdr.querySelectorAll('.left'), ...log.querySelectorAll('.left')]) {
      t.textContent = leftText(/** @type {HTMLElement} */ (t).dataset.until || '');
    }
  }

  /**
   * (2026-10-26, limit_continue) " (in 42 min)" until `iso`, '' once it has passed: the same texts as
   * lib/format.js timeLeft (minutes round up). @param {string} iso
   */
  function leftText(iso) {
    const ms = Date.parse(iso) - Date.now();
    if (!(ms > 0)) return '';
    const min = Math.ceil(ms / 60000);
    const s = ms < 60000 ? tr('in under 1 min')
      : min < 60 ? tr('in {0} min', min) : tr('in {0} h {1} min', Math.floor(min / 60), min % 60);
    return ` (${s})`;
  }

  /** (2026-10-26, limit_continue) A countdown span that tick() keeps current. @param {string} iso */
  function leftEl(iso) {
    const s = el('span', 'left', leftText(iso));
    s.dataset.until = iso;
    return s;
  }

  /** (2026-10-21) The header's cache line: a clock time, then a countdown warning, then "ran out". */
  function cacheTick() {
    if (!cacheEl) return;
    const until = Date.parse(cacheEl.dataset.until || '');
    if (Number.isNaN(until)) return;
    const d = new Date(until);
    const at = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    const left = until - Date.now();
    const warn = left > 0 && left <= (Number(header && header.cacheWarnMs) || 15 * 60 * 1000);
    cacheEl.textContent = left <= 0 ? tr('Prompt cache ran out at {0}: a reply reads the whole conversation again.', at)
      : warn ? tr('⚠ Prompt cache runs out in {0} (at {1}). Reply before then to reuse it.', elapsed(left), at)
        : tr('Prompt cache warm until {0}', at);
    cacheEl.classList.toggle('warn', warn);
  }

  // ---- events ------------------------------------------------------------------------------------
  document.addEventListener('click', (e) => {
    if (e.target instanceof Element && e.target.closest('.codebtn')) return; // (2026-10-21) its own handler
    // (2026-10-21) ADHD mode: a click on a shortened text shows (or hides again) the rest
    const cut = adhd && e.target instanceof Element ? e.target.closest('.cut') : null;
    if (cut) cut.classList.toggle('open');
    const a = e.target instanceof Element ? e.target.closest('a') : null;
    if (!a) return;
    e.preventDefault();
    // (2026-10-27, SEC-M1) VS Code's own click listener on the window (it runs after this one) opens a
    // clicked link itself, past the host's confirm modal: the click stops here. A link's URL is its
    // data-href (lib/markdown.js); its href is only "#".
    e.stopPropagation();
    if (a.dataset.action === 'raw') {
      vscode.postMessage({ type: 'openRaw' });
      return;
    }
    const href = a.dataset.href || '';
    if (/^https:\/\//i.test(href)) vscode.postMessage({ type: 'openLink', href });
  });

  window.addEventListener('message', (ev) => {
    const m = ev.data;
    if (!m || typeof m !== 'object') return;
    if (m.type === 'patch') patch(m);
    else if (m.type === 'replyResult') replyResult(m);
    else if (m.type === 'editText') startEdit(m); // (2026-10-26, edit in box)
    else if (m.type === 'editResult') editResult(m);
    else if (m.type === 'autoPicked') autoPicked(m);
    else if (m.type === 'viewMode') setAdhd(!!m.adhd); // (2026-10-21) changed in another Task view
    else if (m.type === 'attached') addPaths(m.paths); // (2026-10-21, attach) the files the owner picked
    else if (m.type === 'autoPick' && typeof m.on === 'boolean') {
      // (2026-10-21, chat settings) the owner set this chat's box from ⚙: kept over a reload like a click
      autoTouched = true;
      auto.checked = m.on;
      saveState();
    } else if (m.type === 'error') {
      log.appendChild(el('div', 'blk row denial', m.text));
    } else if (m.type === 'note') {
      // (2026-10-26, silent run) e.g. "Ran silently: …" after a hidden-terminal run
      log.appendChild(el('div', 'blk row note', m.text));
    }
  });

  buildComposer();
  renderQueued(); // (2026-10-26, write while working) a message queued before a reload
  saveState(); // (0.4.3) the identity is in the state from the first load, not only after typing
  // (0.4.1 D7) pad the page by the composer's live height (a resized textarea, the option buttons),
  // so the last lines of the conversation stay visible above it; stay at the bottom if we were there
  let stick = true;
  window.addEventListener('scroll', () => { stick = atBottom(); }, { passive: true });
  const fitComposer = () => {
    document.body.style.paddingBottom = `${composer.offsetHeight + 16}px`;
    if (stick) toBottom();
  };
  if (typeof ResizeObserver === 'function') new ResizeObserver(fitComposer).observe(composer);
  fitComposer();
  setInterval(tick, 1000);
  vscode.postMessage({ type: 'ready' });
}());

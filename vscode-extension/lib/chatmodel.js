// @ts-check
// The Task view's view-model (2026-10-17, 0.4.0, docs/SPEC_V04_CHAT.md 3). Pure: no vscode API.
// stream-json lines -> slim events (parseLine, once per line, kept in lib/livelog.js's 2 MB window)
// -> blocks with STABLE keys (buildView), so the host can post only the blocks that changed.
// Shares its parsing helpers with lib/transcript.js (the text log) instead of duplicating them.
'use strict';

const path = require('path');
const transcript = require('./transcript');
const markdown = require('./markdown');
const format = require('./format');
const argv = require('./argv');
const { t } = require('./i18n'); // (2026-10-21) the UI language; the English text is the key

const RESULT_BODY_MAX = 4096;
const INPUT_STR_MAX = 500;
const RESULT_TEXT_MAX = 200000;
const STDERR_MAX = 4096;
const TARGET_MAX = 160;
const PROMPT_FOLD_LINES = 12;

const PATH_TOOLS = ['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'NotebookRead'];
const PATTERN_TOOLS = ['Glob', 'Grep'];
const SHELL_TOOLS = ['Bash', 'PowerShell', 'BashOutput'];

// The core's marker grammar (src/sleeping_clanker/goals.py _MARKER / _TASK_MARKER / _normalise).
const GOAL_MARKER_RE = /^(GOAL_COMPLETE|GOAL_CONTINUE|GOAL_BLOCKED)(?![A-Za-z0-9]|_+[A-Za-z0-9])([\s\S]*)$/i;
const TASK_MARKER_RE = /^(TASK_DONE|TASK_BLOCKED)(?![A-Za-z0-9]|_+[A-Za-z0-9])([\s\S]*)$/i;
const FENCE_RE = /^(`{3,}|~{3,})[\w+-]*$/;
// (0.4.1) the core's OPTION line (goals.py _OPTION)
const OPTION_RE = /^OPTION\s+([1-4])\s*(\((recommended|suositeltu)\))?\s*[:.)\-–—]\s*(.+)$/is;
// (0.4.1 D3) rendered markdown by text: buildView runs every second while an item runs
const MD_CACHE_MAX = 400;
/** @type {Map<string, string>} */
const mdCache = new Map();

/** markdown.renderMarkdown, cached (oldest entry dropped first). @param {string} text */
function renderMd(text) {
  const hit = mdCache.get(text);
  if (hit !== undefined) return hit;
  const html = markdown.renderMarkdown(text);
  mdCache.set(text, html);
  if (mdCache.size > MD_CACHE_MAX) mdCache.delete(/** @type {string} */ (mdCache.keys().next().value));
  return html;
}
const WRAP = ' \t*_`#>';
const REASON_SEP = ':-–—';
// (2026-10-27, i18n) goals.py's reason for a bare blocked marker (the core writes it in English);
// the badge shows it as t(NO_REASON)
const NO_REASON = '(no reason given)';

const GLYPHS = Object.freeze({
  running: '▶', pending: '○', waiting: '⏸', paused: '⏸', done: '✓', blocked: '!', failed: '✗', cancelled: '⊘',
});

/** @param {unknown} v */
const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
// (2026-10-27, i18n) no local `t` in this file: it is i18n's t
/** @param {string} s @param {number} max */
const clip = (s, max) => (s.length <= max ? s : s.slice(0, max) + '…');
/** @param {string} s */
const oneLine = (s) => s.replace(/\s+/g, ' ').trim();

// ---- markers ---------------------------------------------------------------------------------------
/** Python's str.strip(chars). @param {string} s @param {string} chars @param {'both' | 'right' | 'left'} [side] */
function stripChars(s, chars, side = 'both') {
  let a = 0;
  let b = s.length;
  if (side !== 'right') while (a < b && chars.includes(s[a])) a += 1;
  if (side !== 'left') while (b > a && chars.includes(s[b - 1])) b -= 1;
  return s.slice(a, b);
}

/** goals.py `_normalise`: strip wrappers and trailing punctuation until stable. @param {string} line */
function normaliseMarkerLine(line) {
  let text = line;
  for (;;) {
    const before = text;
    text = stripChars(stripChars(stripChars(text, WRAP), '.!:;', 'right'), WRAP);
    if (text === before) return text;
  }
}

/**
 * The marker on the last line that is neither blank nor a code fence (goals.py `_last_marker`).
 * @param {string | null | undefined} text
 * @returns {{kind: 'done' | 'complete' | 'continue' | 'blocked', reason: string | null, lineIndex: number} | null}
 */
function findMarker(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const stripped = lines[i].trim();
    if (!stripped || FENCE_RE.test(stripped)) continue;
    const norm = normaliseMarkerLine(stripped);
    const m = TASK_MARKER_RE.exec(norm) || GOAL_MARKER_RE.exec(norm);
    if (!m) return null;
    const mark = m[1].toUpperCase();
    const reason = () => normaliseMarkerLine(stripChars(m[2], WRAP + REASON_SEP, 'left')) || NO_REASON;
    if (mark === 'TASK_DONE') return { kind: 'done', reason: null, lineIndex: i };
    if (mark === 'GOAL_COMPLETE') return { kind: 'complete', reason: null, lineIndex: i };
    if (mark === 'GOAL_CONTINUE') return { kind: 'continue', reason: null, lineIndex: i };
    return { kind: 'blocked', reason: reason(), lineIndex: i };
  }
  return null;
}

/**
 * Split the marker line off a text: {text without it, marker | null}.
 * @param {string} text
 */
function stripMarker(text) {
  const marker = findMarker(text);
  if (!marker) return { text, marker: null };
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  lines.splice(marker.lineIndex, 1);
  return { text: lines.join('\n').replace(/\s+$/, ''), marker };
}

// ---- slim events ---------------------------------------------------------------------------------
/** A tool input with long strings clipped and arrays/objects summarised. @param {unknown} input */
function slimInput(input) {
  if (!isObj(input)) return {};
  /** @type {Record<string, string | number | boolean | null>} */
  const out = {};
  for (const [k, v] of Object.entries(/** @type {Record<string, unknown>} */ (input))) {
    if (typeof v === 'string') out[k] = clip(v, INPUT_STR_MAX);
    else if (typeof v === 'number' || typeof v === 'boolean' || v === null) out[k] = v;
    else if (Array.isArray(v)) out[k] = `[${v.length} item${v.length === 1 ? '' : 's'}]`;
    else out[k] = '[object]';
  }
  return out;
}

/**
 * One parsed stream-json event -> the few fields the view needs (null = not shown at all).
 * @param {unknown} ev
 * @returns {Record<string, any> | null}
 */
function slimEvent(ev) {
  if (!isObj(ev)) return { type: '_unparsed' };
  const e = /** @type {Record<string, any>} */ (ev);
  if (e.type === 'system') {
    if (e.subtype === 'init') {
      return { type: 'system', subtype: 'init', session_id: typeof e.session_id === 'string' ? e.session_id : null,
        model: typeof e.model === 'string' ? e.model : null,
        apiKeySource: typeof e.apiKeySource === 'string' ? e.apiKeySource : null, // (2026-10-20) billing
        tools: Array.isArray(e.tools) ? e.tools.filter((x) => typeof x === 'string').slice(0, 200) : [] };
    }
    if (e.subtype === 'permission_denied') {
      return { type: 'system', subtype: 'permission_denied', tool_name: typeof e.tool_name === 'string' ? e.tool_name : '?',
        tool_use_id: typeof e.tool_use_id === 'string' ? e.tool_use_id : null,
        reason: String(e.decision_reason_type || e.reason || ''), message: clip(String(e.message || ''), 400) };
    }
    if (e.subtype === 'compact_boundary') return { type: 'system', subtype: 'compact_boundary' };
    return null;
  }
  if (e.type === 'assistant') {
    const content = isObj(e.message) && Array.isArray(e.message.content) ? e.message.content : [];
    const blocks = [];
    for (const b of content) {
      if (!isObj(b)) continue;
      if (b.type === 'text' && typeof b.text === 'string' && b.text.trim()) blocks.push({ type: 'text', text: b.text });
      else if (b.type === 'tool_use') {
        blocks.push({ type: 'tool_use', id: typeof b.id === 'string' ? b.id : '', name: typeof b.name === 'string' ? b.name : 'tool',
          input: slimInput(b.input) });
      }
    }
    if (!blocks.length) return null;
    // (2026-10-26, owner d7f2e585) the context size of this call, for the live activity line (stream-json's
    // output_tokens is only a placeholder while the turn streams, so the output count is not known)
    const ctx = contextTokens(isObj(e.message) ? e.message.usage : null);
    return ctx ? { type: 'assistant', content: blocks, ctx } : { type: 'assistant', content: blocks };
  }
  if (e.type === 'user') {
    const content = isObj(e.message) && Array.isArray(e.message.content) ? e.message.content : [];
    const blocks = [];
    for (const b of content) {
      if (!isObj(b) || b.type !== 'tool_result') continue;
      const full = transcript.resultText(b.content);
      const trimmed = full.replace(/\n+$/, '');
      blocks.push({ type: 'tool_result', tool_use_id: typeof b.tool_use_id === 'string' ? b.tool_use_id : '',
        is_error: b.is_error === true, text: trimmed.slice(0, RESULT_BODY_MAX), chars: trimmed.length,
        lines: trimmed ? trimmed.split('\n').length : 0 });
    }
    return blocks.length ? { type: 'user', content: blocks } : null;
  }
  if (e.type === 'rate_limit_event') {
    const info = isObj(e.rate_limit_info) ? e.rate_limit_info : {};
    return info.status === 'rejected' ? { type: 'rate_limit_event', resetsAt: info.resetsAt === undefined ? null : info.resetsAt } : null;
  }
  if (e.type === 'result') {
    return { type: 'result', subtype: typeof e.subtype === 'string' ? e.subtype : '?', is_error: e.is_error === true,
      num_turns: typeof e.num_turns === 'number' ? e.num_turns : null,
      total_cost_usd: typeof e.total_cost_usd === 'number' ? e.total_cost_usd : null,
      terminal_reason: typeof e.terminal_reason === 'string' ? e.terminal_reason : null,
      result: typeof e.result === 'string' ? e.result.slice(0, RESULT_TEXT_MAX) : null };
  }
  return null;
}

/**
 * One raw jsonl line -> a slim event; null for a blank line or an event the view never shows.
 * @param {string} line
 */
function parseLine(line) {
  const s = String(line == null ? '' : line).trim();
  if (!s) return null;
  try {
    return slimEvent(JSON.parse(s));
  } catch (_e) {
    return { type: '_unparsed' };
  }
}

// ---- (2026-10-26, owner d7f2e585) the live activity line of a running task ---------------------------
const STEP_MAX = 60;

/** input + cache read + cache creation tokens of one call, or 0. @param {unknown} usage */
function contextTokens(usage) {
  if (!isObj(usage)) return 0;
  const u = /** @type {Record<string, unknown>} */ (usage);
  const n = (/** @type {string} */ k) => (typeof u[k] === 'number' && Number.isFinite(u[k]) && /** @type {number} */ (u[k]) > 0 ? /** @type {number} */ (u[k]) : 0);
  return n('input_tokens') + n('cache_read_input_tokens') + n('cache_creation_input_tokens');
}

/**
 * What a running attempt is doing now, like Claude Code's terminal: the last step (`Read(view.js)`,
 * `Bash(pytest -q)`, `Thinking…` after a tool returned, `Writing…`, `Starting…`) and the context size
 * of the latest call. `step` null = nothing yet or the attempt ended; `tokens` null = not known.
 * @param {(Record<string, any> | null)[]} events slim events (parseLine), oldest first
 * @returns {{step: string | null, tokens: number | null}}
 */
function liveActivity(events) {
  /** @type {string | null} */
  let step = null;
  let found = false;
  /** @type {number | null} */
  let tokens = null;
  for (let i = events.length - 1; i >= 0 && (!found || tokens === null); i -= 1) {
    const e = events[i];
    if (!e || e.type === '_unparsed') continue;
    if (tokens === null && e.type === 'assistant' && typeof e.ctx === 'number' && e.ctx > 0) tokens = e.ctx;
    if (found) continue;
    if (e.type === 'result') return { step: null, tokens: null };
    if (e.type === 'system') {
      if (e.subtype !== 'init') continue; // a denial or a compaction: the step before it still runs
      step = t('Starting…');
    } else if (e.type === 'user') step = t('Thinking…'); // a tool returned: Claude works on its next step
    else if (e.type === 'assistant') {
      const last = e.content[e.content.length - 1];
      if (last && last.type === 'tool_use') {
        const { target } = toolTarget(last.name, last.input || {});
        step = clip(oneLine(target ? `${last.name}(${target})` : last.name), STEP_MAX);
      } else step = t('Writing…');
    } else if (e.type === 'rate_limit_event') step = t('Usage limit hit');
    else continue;
    found = true;
  }
  return { step, tokens };
}

/** 950 -> "950", 1234 -> "1.2k", 45210 -> "45.2k", 123456 -> "123k", 1520000 -> "1.5M". @param {number} n */
function shortCount(n) {
  const v = Math.max(0, Math.round(n));
  /** @param {number} x @param {string} unit */
  const fix = (x, unit) => `${x >= 100 ? Math.round(x) : Number(x.toFixed(1))}${unit}`;
  if (v < 1000) return String(v);
  if (v < 999500) return fix(v / 1000, 'k');
  return fix(v / 1e6, 'M');
}

// ---- tool rows -------------------------------------------------------------------------------------
/** @param {string} p */
const baseName = (p) => p.replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p;

/**
 * The verb + target of a tool row, like Claude Code (`Read  notes.md`).
 * @param {string} name @param {Record<string, any>} input
 * @returns {{target: string, title: string}}
 */
function toolTarget(name, input) {
  const s = (/** @type {unknown} */ v) => (typeof v === 'string' ? v : '');
  if (PATH_TOOLS.includes(name)) {
    const p = s(input.file_path) || s(input.notebook_path) || s(input.path);
    return { target: p ? baseName(p) : '', title: p };
  }
  if (PATTERN_TOOLS.includes(name)) {
    const pat = s(input.pattern);
    const where = s(input.path);
    const text = where ? t('{0}  in {1}', pat, where) : pat; // (2026-10-27, i18n)
    return { target: clip(text, TARGET_MAX), title: text };
  }
  if (SHELL_TOOLS.includes(name)) {
    const cmd = s(input.command) || s(input.bash_id);
    return { target: clip(oneLine(cmd), TARGET_MAX), title: cmd };
  }
  const pick = slimText(s(input.url) || s(input.query) || s(input.description) || s(input.skill) || s(input.todos) || s(input.prompt)
    || Object.values(input).filter((v) => typeof v === 'string')[0] || '');
  return { target: clip(oneLine(pick), TARGET_MAX), title: pick };
}

// (2026-10-27, i18n) slimInput's placeholders ("[3 items]", "[object]") stay English in the slim events
// (parsed once per line and kept); a row shows them in the UI language when it is built
const SLIM_ITEMS_RE = /^\[(\d+) items?\]$/;
/** @param {unknown} v */
function slimText(v) {
  const text = String(v);
  if (text === '[object]') return t('[object]');
  const m = SLIM_ITEMS_RE.exec(text);
  return m ? t(m[1] === '1' ? '[{0} item]' : '[{0} items]', Number(m[1])) : text;
}

/** @param {Record<string, any>} block a slim tool_result */
function resultSummary(block) {
  if (block.is_error) return clip(oneLine(block.text), 100) || t('error');
  const n = block.lines;
  return n ? t(n === 1 ? '{0} line' : '{0} lines', n) : t('no output'); // (2026-10-27, i18n)
}

/** @param {Record<string, any>} row @param {Record<string, any>} res */
function applyResult(row, res) {
  // (2026-10-20, 0.4.4 logfix H) a PreToolUse hook denial is a muted note; real errors stay red
  const hook = res.is_error ? transcript.hookDenial(oneLine(res.text)) : null;
  const denied = !hook && res.is_error && transcript.DENIAL_RE.test(oneLine(res.text));
  row.status = hook ? 'hook' : denied ? 'denied' : res.is_error ? 'error' : 'ok';
  row.summary = hook || (denied ? t('denied') : resultSummary(res));
  row.body = res.text + (res.chars > res.text.length ? `\n${t('… ({0} more characters)', res.chars - res.text.length)}` : '');
}

// ---- header / composer -----------------------------------------------------------------------------
/** @param {string} cwd */
function shortPath(cwd) {
  const p = String(cwd || '');
  if (p.length <= 48) return p;
  const parts = p.split(/[\\/]/).filter(Boolean);
  if (parts.length <= 3) return clip(p, 48);
  const sep = p.includes('\\') ? '\\' : '/';
  return `${parts[0]}${sep}…${sep}${parts.slice(-2).join(sep)}`;
}

/** Local HH:MM, '?' for a bad time. @param {Date | string | number | null | undefined} when */
function hhmm(when) {
  if (when === null || when === undefined || when === '') return '?';
  const d = when instanceof Date ? when : new Date(when);
  if (Number.isNaN(d.getTime())) return '?';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/**
 * (2026-10-20, 0.4.6, SPEC_V046_AUTOPICK) The state of the chat's "Auto-pick best" box, as the host
 * (lib/taskview.js) knows it: `on` = this chat answers a blocked question by itself, `stopped` = it
 * has answered AUTO_PICK_MAX questions in a row, so the next one waits for the owner. Nothing passed
 * (an old caller, a test) = off.
 * (2026-10-26, modes) `autorun`: the task's own Autorun answers for it (only present when true).
 * @param {{on?: boolean, stopped?: boolean, autorun?: boolean} | null | undefined} [o]
 */
function autoPickState(o) {
  return { on: !!(o && o.on), stopped: !!(o && o.stopped), ...(o && o.autorun ? { autorun: true } : {}) };
}

/**
 * (2026-10-21, subagents, SPEC_V049_AGENTS) The bottom bar's subagent dropdown: the task's own limit
 * (`max_agents`) as the selected value, every choice the core takes, and whether it can be changed
 * now (`on`: every status but running). The runner limit next to it is a global setting, so it is
 * NOT part of an item's state: the ⚙ Chat settings and the Queue view's gear set it.
 * @param {import('./parse').Item | null} item
 */
function agentsState(item) {
  return {
    value: argv.agentsValue(item ? item.max_agents : null),
    options: argv.AGENT_LIMITS.map((a) => ({ value: a.value, label: t(a.label) })),
    on: argv.canSetAgents(item),
  };
}

/**
 * The composer state: enabled only when the core would accept `reply` (argv.canReply).
 * (2026-10-21) `opts.changesQuestion`: the `claudAutopilot.changesQuestion` setting; non-empty = a
 * stopped item (not blocked or paused) that can take a reply gets the "What did you change?" action.
 * (2026-10-21, reply to paused) a paused item with a session is enabled too: the reply un-pauses it.
 * (2026-10-21) `opts.quickPrompts`: the `claudAutopilot.quickPrompts` setting; an item that can take a
 * reply (blocked too) gets one button each as `prompts` ({i, label, text}; the host sends its own copy).
 * (2026-10-21, start now) `startNow`: a pending item; the webview shows "Start now" (action 'start-now').
 * @param {import('./parse').Item | null} item
 * (2026-10-26, modes) An Autorun task shows no option buttons (its answers are notes) unless it stopped.
 * @param {{autoPick?: {on?: boolean, stopped?: boolean, autorun?: boolean} | null, changesQuestion?: string, quickPrompts?: string[]}} [opts]
 */
function composerState(item, opts = {}) {
  const choices = item ? format.replyProfileChoices(item, argv.PROFILES) : [];
  // (2026-10-21, security review M5) `full` (or keeping it) carries its warning in the label
  const isFull = (/** @type {{profile: string | null}} */ c) => (c.profile || (item && item.profile)) === argv.FULL_PROFILE;
  const options = choices.map((c) => ({ value: c.profile || '', label: isFull(c) ? `${c.label} ${t(format.FULL_WARN)}` : c.label }));
  const autoPick = autoPickState(opts.autoPick);
  const agents = agentsState(item);   // (2026-10-21, subagents) the bottom bar's subagent dropdown
  if (!item) {
    return { enabled: false, hint: t('The task is not in the queue any more.'), options, value: '', autoPick, agents, merge: null };
  }
  const enabled = argv.canReply(item);
  // (2026-10-26, write while working) as in Claude chat: the box stays writable while Claude works and
  // Send queues the message in the webview; `autoSend` = the turn ended in a state that takes it (done,
  // blocked). Any other stop (failed, cancelled, paused) puts it back into the box instead.
  const queueable = !enabled && QUEUE_STATUSES.includes(item.status) && !argv.canEditReply(item)
    && (item.status === 'running' || !!item.session_id);
  const autoSend = enabled && AUTO_SEND_STATUSES.includes(item.status);
  let hint = '';
  if (queueable) hint = t('Claude is working… Write now: Send queues your message for when it stops.');
  else if (!enabled) {
    if (item.status === 'running') hint = t('Claude is working…');
    // (2026-10-21, reply to paused) a paused task that never started: nothing to reply to yet
    else if (!item.session_id && item.status === 'paused') hint = t('Not started yet: press Resume, or Edit task… to change it.');
    else if (!item.session_id && argv.REPLY_STATUSES.includes(item.status)) hint = t('No session to resume yet (use Retry).');
    else if (argv.canEditReply(item)) hint = t('Your reply is queued. Until Claude gets it you can edit or withdraw it.');
    else hint = t('Reply is available when the task stops.');
  }
  const help = blockedHelp(item);
  /** (2026-10-21, UI language) an action button in the current language @param {{id: string, label: string}} a */
  const act = (a) => ({ id: a.id, label: t(a.label) });
  if (enabled && item.status !== 'blocked' && item.status !== 'paused' && opts.changesQuestion) help.actions.push(act(ASK_CHANGES));
  // (2026-10-26, edit paused) a started paused task too (Save starts it over with the new text)
  if (argv.canEdit(item) || argv.canEditRestart(item)) help.actions.push(act(EDIT_TASK)); // (2026-10-21, edit)
  if (argv.canEditReply(item)) help.actions.push(act(EDIT_REPLY), act(WITHDRAW_REPLY)); // (2026-10-21, edit reply)
  return {
    enabled, queueable, autoSend, hint, options, value: options.length ? options[0].value : '',
    question: enabled && !(autoPick.autorun && !autoPick.stopped) ? questionOf(item) : null, autoPick, agents,
    // (2026-10-27, modes fix round 2) the task's own Autorun box (any status: the core takes it while it runs)
    autorun: { on: item.autorun === true, tip: t(argv.AUTORUN_TIP) },
    prompts: enabled ? quickPromptButtons(opts.quickPrompts) : [],
    // (2026-10-21, start now) a pending task: the "Start now" button (action id 'start-now')
    startNow: argv.canStartNow(item),
    // (2026-10-27, U4, SPEC_V05_PARALLEL 8.6) the merge card (a conflict question, a held or a merged merge);
    // shown whatever the status and Autorun: its buttons are the only way to answer take-task/discard/undo
    merge: format.mergeCardModel(item),
    ...help,
  };
}

// (2026-10-26, write while working) the box takes a queued message while the item is in one of these
// (pending/waiting only mid-conversation: with a session); it is sent by itself only after these
const QUEUE_STATUSES = Object.freeze(['running', 'waiting', 'pending']);
const AUTO_SEND_STATUSES = Object.freeze(['done', 'blocked']);

const PROMPT_LABEL_MAX = 40;

/**
 * (2026-10-21) The quick-prompt buttons: `i` is the index in the setting, `label` a short cut of the text.
 * @param {unknown} list
 * @returns {{i: number, label: string, text: string}[]}
 */
function quickPromptButtons(list) {
  if (!Array.isArray(list)) return [];
  /** @type {{i: number, label: string, text: string}[]} */
  const out = [];
  list.forEach((text, i) => {
    if (typeof text !== 'string' || !text.trim()) return;
    const line = text.trim().replace(/\s+/g, ' ');
    out.push({ i, label: line.length > PROMPT_LABEL_MAX ? `${line.slice(0, PROMPT_LABEL_MAX - 1)}…` : line, text });
  });
  return out;
}

// (2026-10-21) the host sends `claudAutopilot.changesQuestion` as the reply (never text from the webview)
const ASK_CHANGES = Object.freeze({ id: 'ask-changes', label: 'What did you change?' });
// (2026-10-21, edit) a task that has not started yet: change its text (the host opens Edit Task…)
const EDIT_TASK = Object.freeze({ id: 'edit-task', label: 'Edit task…' });
// (2026-10-21, edit reply) the owner's reply while it is still queued (argv.canEditReply)
const EDIT_REPLY = Object.freeze({ id: 'edit-reply', label: 'Edit reply…' });
const WITHDRAW_REPLY = Object.freeze({ id: 'withdraw-reply', label: 'Withdraw reply' });

/**
 * (2026-10-20, 0.4.4 logfix A/C) Above the composer of a blocked item: `notice` when the attempt blocked
 * with the same question as the last one (item.repeat_block), and the `actions` for an item that needs a
 * command: "Retry with shell-dev" and "Edit allowed commands…" (ids the host handles; never a command).
 * (2026-10-21, v049 dontask_hold) An item blocked by don't-ask denials (`denial_block`) gets its own
 * card instead (`denial`: what was denied, the fix, the allow-list entries with a Copy button each) and
 * the actions that fix exactly that: "Add to allowed commands…" (the host pre-fills the first entry from
 * its own copy of the item) and "Retry with <profile>" (or "Retry" when only the entry was missing). A
 * hard-denied command gets neither: no profile allows it.
 * @param {import('./parse').Item} item
 * @returns {{notice: string | null, actions: {id: string, label: string}[], denial: DenialCard | null}}
 */
function blockedHelp(item) {
  const block = item && item.status === 'blocked' && item.denial_block ? item.denial_block : null;
  if (block) {
    /** @type {{id: string, label: string}[]} */
    const actions = [];
    if (block.allow.length) actions.push({ id: 'edit-allowed', label: t('Add to allowed commands…') });
    if (block.profile) actions.push({ id: 'retry-suggested', label: t('Retry with {0}', block.profile) });
    else if (block.allow.length) actions.push({ id: 'retry-suggested', label: t('Retry') });
    return { notice: format.repeatBlockNotice(item), actions, denial: denialCard(block) };
  }
  const actions = format.needsCommand(item)
    ? [{ id: 'retry-shell-dev', label: t('Retry with shell-dev') }, { id: 'edit-allowed', label: t('Edit allowed commands…') }]
    : [];
  return { notice: format.repeatBlockNotice(item), actions, denial: null };
}

/**
 * @typedef {{title: string, rows: {text: string, note: string}[], hint: string,
 *   allow: {n: number, text: string}[], copyLabel: string}} DenialCard
 */

/**
 * (2026-10-21, v049 dontask_hold) The blocked card's content, plain text only (the webview renders it
 * with textContent). `allow[n]` is what "Copy" copies: the host copies its own copy, by index.
 * @param {import('./parse').DenialBlock} block
 * @returns {DenialCard}
 */
function denialCard(block) {
  const rows = block.denied.map((d) => ({ text: d.what ? `${d.tool} \`${d.what}\`` : d.tool, note: d.note || '' }));
  if (block.more > 0) rows.push({ text: t('(+{0} more)', block.more), note: '' });
  return {
    title: t("Denied in don't-ask mode (no one was there to approve it):"),
    rows, hint: block.hint, allow: block.allow.map((text, n) => ({ n, text })), copyLabel: t('Copy'),
  };
}


/**
 * (2026-10-19, 0.4.1) The option buttons above the composer: null unless the item is blocked with a
 * valid `blocked_question` option list (format.questionOptions; recommended first). `shell` = the
 * option mentions a shell, so the webview preselects `shell-read` for it.
 * (2026-10-19, 0.4.2 L2) `profile`: what the button sends while the owner has not picked a profile in
 * the dropdown (format.optionProfile; '' = keep). The dropdown's own default (shell-read when the
 * question text mentions a shell) applies to free-text replies only.
 * @param {import('./parse').Item} item
 */
function questionOf(item) {
  // (2026-10-27, U4, 8.6) a merge question has no option buttons: its card (composerState.merge) answers it
  if (item.blocked_question && item.blocked_question.kind === 'merge') return null;
  const opts = format.questionOptions(item);
  if (!opts.length) return null;
  const q = /** @type {{text: string}} */ (item.blocked_question);
  return { text: q.text || '', options: opts.map((o) => ({ n: o.n, label: o.label, detail: o.detail, recommended: o.recommended,
    shell: format.optionWantsShell(o), profile: format.optionProfile(item, o) || '' })) };
}

/**
 * (0.4.1) The OPTION lines right above where the marker was (core goals.py `_option_line`), cut off
 * the end of a text: the options are shown as buttons instead.
 * @param {string} text the text WITHOUT its marker line
 */
function stripOptionLines(text) {
  const lines = String(text).replace(/\r\n?/g, '\n').split('\n');
  let end = lines.length;
  let k = end - 1;
  let found = 0;
  for (; k >= 0; k -= 1) {
    const raw = lines[k].trim();
    if (!raw) continue;
    if (!isOptionLine(raw)) break;
    found += 1;
    end = k;
  }
  if (!found) return text;
  return lines.slice(0, end).join('\n').replace(/\s+$/, '');
}

/** @param {string} raw one trimmed line */
function isOptionLine(raw) {
  let s = raw.replace(/[*`]/g, '').trim();
  for (;;) {
    const before = s;
    s = stripChars(s.trim(), ' \t_#>').replace(/^(?:[-+•]|\d+[.)])\s+/, '').trim();
    if (s === before) break;
  }
  return OPTION_RE.test(s);
}

/**
 * (2026-10-21, owner) Which queue a Task view reads, said in its header: a restored Task view of a
 * task in the SHARED queue (no folder) or another project's queue looked like this window's own.
 * "shared queue" when the panel has no project; the folder name when it is another project than
 * this window's; the queue (lane) when it is not `default`; null (nothing shown) for this window's
 * own default queue.
 * @param {{project?: string | null, lane?: string | null, ownRoot?: string | null}} t0
 * @returns {string | null}
 */
function queueLabelOf(t0) {
  const parts = [];
  const project = t0.project || null;
  if (!project) parts.push(t('shared queue'));
  else if (!samePath(project, t0.ownRoot || null)) parts.push(t('project {0}', path.basename(project) || project));
  const lane = t0.lane || argv.DEFAULT_LANE;
  if (lane !== argv.DEFAULT_LANE) parts.push(t('queue {0}', format.laneLabel(lane)));
  return parts.length ? parts.join(' / ') : null;
}

/** Two project folders are the same (Windows paths: case and separators do not count). @param {string} a @param {string | null} b */
function samePath(a, b) {
  if (!b) return false;
  const norm = (/** @type {string} */ p) => path.resolve(p).replace(/[\\/]+$/, '').toLowerCase();
  return norm(a) === norm(b);
}

/**
 * @param {import('./parse').Item | null} item
 * @param {{laneLabel?: string, queueLabel?: string | null, fullPlain?: boolean, limitHeld?: boolean,
 *   parallel?: any}} [opts]
 */
function headerOf(item, opts = {}) {
  if (!item) return { label: t('(unknown task)'), status: 'unknown', running: false };
  const status = item.status;
  // (2026-10-29, integrate round 9) a task that works alone in the project folder: why, in the Queue view's words
  // (format.aloneReason of its lane's parallel.json; nothing without a live parallel runner)
  const why = format.aloneReason(item, opts.parallel || null);
  const alone = why === null ? null
    : why ? t('It runs alone in the project folder: {0}.', why) : t('It runs alone in the project folder.');
  const notMerged = format.notMergedText(item); // (2026-10-29, delta M1) files no merge could carry back
  return {
    id: item.id, label: format.itemLabel(item, 80), kind: item.kind, status,
    // (2026-10-27, limit_continue fix LOW-4) a usage-limit hold (`--on-limit ask`): no countdown, it waits for you
    // (2026-10-27, i18n) the pill's word in the UI language (`status` stays the id: the pill's class)
    statusText: status === 'waiting' && item.wait_until
      ? (opts.limitHeld ? t('on hold after the usage limit — Resume Queue (Release Hold) to continue at {0}', hhmm(item.wait_until)) : t('waiting until {0}', hhmm(item.wait_until))) : t(status),
    // (2026-10-26, limit_continue) the webview counts down to it next to the status pill
    waitUntil: status === 'waiting' && item.wait_until && !opts.limitHeld ? item.wait_until : null,
    profile: item.profile || 'safe', lane: opts.laneLabel || item.lane || argv.DEFAULT_LANE,
    cwd: shortPath(item.cwd), cwdFull: item.cwd || '', running: status === 'running',
    startedAt: status === 'running' ? item.started_at : null,
    rounds: item.kind === 'goal' ? { done: item.rounds || 0, max: item.max_rounds === -1 ? '∞' : item.max_rounds } : null,
    error: status === 'blocked' || status === 'failed' ? (item.error || '') : '',
    // (2026-10-21) when the prompt cache runs out; the webview ticks the countdown and the warning
    cacheUntil: cacheUntilOf(item), cacheWarnMs: format.CACHE_WARN_MS,
    // (2026-10-21, add panel) the task's own `provider · model · effort`; null = the runner's
    runOn: format.runOnText(item),
    // (2026-10-21, owner) which queue this view reads (queueLabelOf); null = this window's own default
    queueLabel: typeof opts.queueLabel === 'string' && opts.queueLabel ? opts.queueLabel : null,
    // (2026-10-26, owner fa388b7b) claudAutopilot.disableFullAccessColor: the webview shows a plain note
    fullPlain: opts.fullPlain === true,
    ...(item.autorun ? { autorun: true } : {}), // (2026-10-26, modes)
    ...(alone ? { alone } : {}), // (2026-10-29, integrate round 9)
    ...(notMerged ? { notMerged } : {}), // (2026-10-29, delta M1)
  };
}

/** The ISO time the item's prompt cache runs out, or null (format.cacheState). @param {import('./parse').Item} item */
function cacheUntilOf(item) {
  const c = format.cacheState(item);
  return c ? c.until.toISOString() : null;
}

/** @param {import('./parse').Item | null} item */
function titleOf(item) {
  if (!item) return t('Task');
  const g = GLYPHS[/** @type {keyof typeof GLYPHS} */ (item.status)] || '•';
  return `${g} ${format.itemLabel(item, 40)}`;
}

// ---- blocks ----------------------------------------------------------------------------------------
/**
 * @typedef {{attempt: number, startedAt?: Date | string | number | null, headLine?: string | null,
 *   rendered?: any[], events?: any[], skipped?: number, stderr?: string | null}} AttemptView
 *   `rendered` (from livelog with mapLine = parseLine) or `events`: slim events, null entries allowed.
 * @typedef {Record<string, any> & {key: string, kind: string}} Block
 */

/**
 * The whole view of one item.
 * (0.4.6) `autoPick`: the host's "Auto-pick best" state for this chat (autoPickState).
 * (2026-10-21) `changesQuestion`: the setting behind the "What did you change?" action (composerState).
 * (2026-10-21) `quickPrompts`: the setting behind the quick-prompt buttons (composerState).
 * (2026-10-21) `queueLabel`: which queue the view reads (queueLabelOf), shown in the header.
 * @param {{item: import('./parse').Item | null, attempts: AttemptView[], laneLabel?: string, queueLabel?: string | null,
 *   autoPick?: {on?: boolean, stopped?: boolean} | null, changesQuestion?: string, quickPrompts?: string[], fullPlain?: boolean,
 *   limitHeld?: boolean, parallel?: any}} input
 * (2026-10-29, integrate round 9) `parallel`: the item's lane's parallel.json (the header says why it runs alone).
 * @returns {{title: string, header: Record<string, any>, composer: Record<string, any>, blocks: Block[]}}
 */
function buildView(input) {
  const item = input.item;
  const attempts = (input.attempts || []).slice().sort((a, b) => a.attempt - b.attempt);
  /** @type {Block[]} */
  const blocks = [];
  const replies = item && Array.isArray(item.replies) ? item.replies : [];
  const usedReplies = new Set();
  if (item && typeof item.prompt === 'string' && item.prompt) {
    blocks.push(userBlock('prompt', item.prompt, t(item.kind === 'goal' ? 'Goal' : 'Task'), false));
  }
  let continues = 0;
  const composer = composerState(item, { autoPick: input.autoPick, changesQuestion: input.changesQuestion,
    quickPrompts: input.quickPrompts });
  const last = attempts.length ? attempts[attempts.length - 1].attempt : null;
  for (const a of attempts) {
    replies.forEach((r, i) => {
      if (!usedReplies.has(i) && r.attempt === a.attempt) {
        usedReplies.add(i);
        blocks.push(replyBlock(r, i, false));
      }
    });
    // (0.4.1) the options are buttons: their OPTION lines are cut off the last attempt's blocked answer
    continues = attemptBlocks(a, blocks, continues, { stripOptions: !!composer.question && a.attempt === last, limitHeld: !!input.limitHeld });
  }
  replies.forEach((r, i) => {
    if (!usedReplies.has(i)) blocks.push(replyBlock(r, i, true));
  });
  markNeedsRun(item, blocks);
  if (item && item.status === 'running') {
    const w = { key: 'working', kind: 'working', startedAt: item.started_at || null };
    // (2026-10-26, owner d7f2e585) the live activity line: the current step and the context tokens
    const lastA = attempts.length ? attempts[attempts.length - 1] : null;
    const act = liveActivity(lastA ? lastA.events || lastA.rendered || [] : []);
    if (act.step) Object.assign(w, { step: act.step });
    if (act.tokens) Object.assign(w, { tokens: t('{0} context', shortCount(act.tokens)) });
    blocks.push(w);
  }
  if (!attempts.length && !(item && item.status === 'running')) {
    blocks.push({ key: 'empty', kind: 'note', text: t('No runs yet. The conversation appears here when the runner starts this task.') });
  }
  const header = headerOf(item, { laneLabel: input.laneLabel, queueLabel: input.queueLabel, fullPlain: input.fullPlain,
    limitHeld: !!input.limitHeld, parallel: input.parallel || null });
  // (0.4.1 nit) the header's "blocked: …" line is dropped when a badge already shows that reason
  if (header.error && item && item.status === 'blocked') {
    const shown = needsYouText(header.error.replace(/^blocked:\s*/i, '')); // (2026-10-27, i18n) the badge's own text
    if (blocks.some((b) => b.kind === 'marker' && b.text === shown)) header.error = '';
  }
  return { title: titleOf(item), header, composer, blocks };
}

/**
 * (2026-10-26, silent run) A blocked item waits for the owner; when Claude's last message (after the
 * owner's last reply) has a fenced shell block (markdown.runnableLang), that is what must run for the task to continue:
 * the message gets `needsRun: true` (the webview highlights its code blocks and adds "Run silently";
 * a full-access task runs them by itself, lib/taskview.js _autoRun).
 * (2026-10-27, integrate) an Autorun answer (a note block with `autorun`) is a reply too: it ends the search.
 * @param {import('./parse').Item | null} item @param {Block[]} blocks
 */
function markNeedsRun(item, blocks) {
  if (!item || item.status !== 'blocked') return;
  for (let i = blocks.length - 1; i >= 0; i -= 1) {
    const b = /** @type {Record<string, any>} */ (blocks[i]);
    if (b.kind === 'user' || (b.kind === 'note' && b.autorun)) return;
    if ((b.kind === 'assistant' || b.kind === 'result') && typeof b.html === 'string' && b.html) {
      if (markdown.codeBlocksOf(b.html).some((c) => markdown.runnableLang(c.lang))) b.needsRun = true;
      return;
    }
  }
}

/**
 * (2026-10-27, modes fix round 2) A reply: the owner's bubble, or for an Autorun answer (`by: 'autorun'`,
 * `reply --by autorun`) one note "Autorun picked: <label>" that also shows after a reload or a closed chat.
 * @param {{at?: string | null, text: string, by?: string}} r @param {number} i @param {boolean} pending
 * @returns {Block}
 */
function replyBlock(r, i, pending) {
  if (r.by !== 'autorun') return userBlock(`r${i}`, r.text, t("Owner's reply"), pending, r.at);
  const line = String(r.text).split(/\r?\n/)[0].trim();
  return { key: `r${i}`, kind: 'note', autorun: true, text: t('Autorun picked: {0}', line.length > 200 ? `${line.slice(0, 199)}…` : line) };
}

/**
 * @param {string} key @param {string} text @param {string} label @param {boolean} pending @param {string | null} [at]
 * @returns {Block}
 */
function userBlock(key, text, label, pending, at = null) {
  const lines = String(text).replace(/\r\n?/g, '\n').replace(/\n+$/, '').split('\n');
  return { key, kind: 'user', label, pending, at: at || null,
    head: lines.slice(0, PROMPT_FOLD_LINES).join('\n'), more: lines.length > PROMPT_FOLD_LINES ? lines.slice(PROMPT_FOLD_LINES).join('\n') : '' };
}

/** @param {{kind: string, reason: string | null}} m @param {number} round @param {string} key @returns {Block} */
function markerBlock(m, round, key) {
  // (2026-10-27, i18n) the badges in the UI language
  if (m.kind === 'done') return { key, kind: 'marker', tone: 'ok', text: t('✓ Done') };
  if (m.kind === 'complete') return { key, kind: 'marker', tone: 'ok', text: t('✓ Goal complete') };
  if (m.kind === 'continue') return { key, kind: 'marker', tone: 'info', text: t('↻ Continuing (round {0})', round) };
  return { key, kind: 'marker', tone: 'error', text: needsYouText(m.reason || NO_REASON) };
}

/**
 * (2026-10-27, i18n) The blocked badge's text; buildView builds the header's reason with it too, so the
 * two compare equal in every language. @param {string} reason
 */
function needsYouText(reason) {
  return t('✗ Needs you: {0}', reason === NO_REASON ? t(NO_REASON) : reason);
}

/**
 * One attempt's blocks, appended to `out`. Returns the GOAL_CONTINUE count so far.
 * @param {AttemptView} a @param {Block[]} out @param {number} continues
 * @param {{stripOptions?: boolean, limitHeld?: boolean}} [opts] (0.4.1) cut the OPTION lines off a TASK_BLOCKED answer;
 *   (2026-10-27, limit_continue fix round 2) `limitHeld`: the queue waits for the owner, so no limit row counts down
 */
function attemptBlocks(a, out, continues, opts = {}) {
  const n = a.attempt;
  const events = a.events || a.rendered || [];
  const skipped = a.skipped || 0;
  let init = events.find((e) => e && e.type === 'system' && e.subtype === 'init') || null;
  if (!init && skipped > 0 && a.headLine) {
    const h = parseLine(a.headLine);
    if (h && h.subtype === 'init') init = h;
  }
  out.push({ key: `a${n}`, kind: 'attempt', n, time: transcript.localStampSec(a.startedAt || null),
    model: init ? init.model : null, session: init && init.session_id ? init.session_id.slice(0, 8) : null,
    tools: init ? init.tools : [], skipped });
  // (2026-10-20) the amount only for API billing; the attempt's own init line decides (old runs too)
  const billing = transcript.billingOf(init ? init.apiKeySource : null);
  /** @type {boolean | null} the last result's outcome; null = no result (died, running) */
  let resultOk = null;
  /** @type {Map<string, Block>} */
  const tools = new Map();
  /** @type {Map<string, Record<string, any>>} */
  const early = new Map(); // a result seen before its tool_use
  // (0.4.1 D1) every assistant text of this attempt: a result repeating ANY of them is not shown again
  const said = transcript.newRenderState();
  /** @type {{kind: string, reason: string | null} | null} */
  let marker = null;
  let unparsed = 0;
  const pushMarker = () => {
    if (!marker) return;
    if (marker.kind === 'continue') continues += 1;
    out.push(markerBlock(marker, continues + 1, `a${n}:marker`));
    marker = null;
  };
  events.forEach((e, idx) => {
    if (!e) return;
    const at = skipped + idx; // absolute line index: stable while the window slides
    if (e.type === '_unparsed') { unparsed += 1; return; }
    if (e.type === 'system') {
      if (e.subtype === 'permission_denied') {
        const row = e.tool_use_id ? tools.get(e.tool_use_id) : null;
        if (row) {
          row.status = 'denied';
          row.summary = t('denied'); // (2026-10-27, i18n)
          if (!row.body) row.body = e.message || t('permission denied ({0})', e.reason);
        } else {
          out.push({ key: `a${n}:d${at}`, kind: 'denial', tool: e.tool_name, reason: e.reason, text: e.message || '' });
        }
      } else if (e.subtype === 'compact_boundary') {
        out.push({ key: `a${n}:c${at}`, kind: 'note', text: t('context compacted') });
      }
      return;
    }
    if (e.type === 'assistant') {
      const texts = [];
      e.content.forEach((/** @type {Record<string, any>} */ b, bi) => {
        if (b.type === 'text') {
          texts.push(b.text);
          const cut = stripMarker(b.text);
          if (cut.marker) marker = cut.marker;
          const shown = opts.stripOptions && cut.marker && cut.marker.kind === 'blocked' ? stripOptionLines(cut.text) : cut.text;
          if (shown.trim()) out.push({ key: `a${n}:x${at}.${bi}`, kind: 'assistant', html: renderMd(shown) });
        } else if (b.type === 'tool_use') {
          const { target, title } = toolTarget(b.name, b.input || {});
          const row = { key: `a${n}:t:${b.id || at + '.' + bi}`, kind: 'tool', name: b.name, target, title,
            status: 'running', summary: '', body: '' };
          if (b.id) tools.set(b.id, row);
          const res = b.id ? early.get(b.id) : null;
          if (res) applyResult(row, res);
          out.push(row);
        }
      });
      if (texts.length) transcript.rememberText(said, texts.join('\n'), texts);
      return;
    }
    if (e.type === 'user') {
      for (const r of e.content) {
        const row = tools.get(r.tool_use_id);
        if (row) applyResult(row, r);
        else early.set(r.tool_use_id, r);
      }
      return;
    }
    if (e.type === 'rate_limit_event') {
      const d = transcript.epochToDate(e.resetsAt);
      // (2026-10-26, limit_continue) `until`: the webview adds "(in 42 min)" while it is ahead
      out.push({ key: `a${n}:l${at}`, kind: 'limit', text: d ? t('Paused until {0}', hhmm(d)) : t('Usage limit reached; paused'),
        until: d && !opts.limitHeld ? d.toISOString() : null });
      return;
    }
    if (e.type === 'result') {
      const dup = transcript.repeatsAnyText(e.result, said);
      let html = '';
      if (!dup && typeof e.result === 'string' && e.result.trim()) {
        const cut = stripMarker(e.result);
        if (cut.marker) marker = cut.marker;
        const shown = opts.stripOptions && cut.marker && cut.marker.kind === 'blocked' ? stripOptionLines(cut.text) : cut.text;
        if (shown.trim()) html = renderMd(shown);
      }
      pushMarker();
      const ok = e.subtype === 'success' && !e.is_error;
      const parts = [`${ok ? '✓' : '✗'} ${e.subtype}`];
      if (e.num_turns !== null) parts.push(t(e.num_turns === 1 ? '{0} turn' : '{0} turns', e.num_turns)); // (2026-10-27, i18n)
      const cost = transcript.costPart(billing, e.total_cost_usd);
      if (cost) parts.push(cost);
      if (e.terminal_reason) parts.push(e.terminal_reason);
      resultOk = ok;
      out.push({ key: `a${n}:res${at}`, kind: 'result', ok, text: parts.join(' · '), html });
    }
  });
  pushMarker();
  // a tool still "running" after the attempt ended is left as it is (the row shows a spinner only
  // while the item runs; the webview greys it out otherwise)
  if (unparsed) out.push({ key: `a${n}:unparsed`, kind: 'note', text: t(unparsed === 1 ? '{0} unparsed line' : '{0} unparsed lines', unparsed) });
  // (2026-10-20) stderr is red only when the attempt failed or had no result; after a success it is a
  // muted, collapsed "CLI notes" block. A known harmless CLI warning is a one-line muted note.
  const { notes, warnings, rest: err } = transcript.stderrNotes(a.stderr);
  // (2026-10-21, v049 dontask_hold addendum) background work lost at the end of the run: a visible
  // warning row (shown in the short view too), never inside the muted, collapsed CLI notes
  warnings.forEach((w, i) => out.push({ key: `a${n}:warn${i}`, kind: 'warning', text: t(transcript.BG_KILLED_WARNING, w) }));
  notes.forEach((note, i) => out.push({ key: `a${n}:cli${i}`, kind: 'note', text: note }));
  if (err) {
    const muted = resultOk === true;
    out.push({ key: `a${n}:stderr`, kind: 'stderr', tone: muted ? 'muted' : 'error', label: muted ? t('CLI notes') : t('stderr'),
      text: err.length > STDERR_MAX ? '…' + err.slice(-STDERR_MAX) : err });
  }
  return continues;
}

module.exports = {
  RESULT_BODY_MAX,
  PROMPT_FOLD_LINES,
  findMarker,
  stripMarker,
  normaliseMarkerLine,
  slimEvent,
  parseLine,
  toolTarget,
  shortPath,
  composerState,
  blockedHelp,
  markNeedsRun,
  denialCard,
  autoPickState,
  agentsState,
  questionOf,
  stripOptionLines,
  titleOf,
  queueLabelOf,
  buildView,
  liveActivity, // (2026-10-26, owner d7f2e585)
  shortCount,
};

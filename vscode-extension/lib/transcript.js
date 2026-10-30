// @ts-check
// The live task log: stream-json lines -> readable plain text, without the vscode API (2026-10-15,
// docs/SPEC_V03.md 1.2). Tolerant by design: a bad line renders as `[unparsed line]` and nothing here
// throws. Every event renders on its own, so lib/livelog.js can render each line once, when it
// arrives, and keep only the rendered text in its 2 MB window. (2026-10-17, 0.4.0) One piece of state
// per attempt, `ctx.state.lastText` (the last assistant text): a `result` whose text equals it (after
// trim) prints only its header line; 0.3.3 printed the final answer twice. (2026-10-19, 0.4.1 D1)
// `ctx.state.texts` also keeps every assistant text of the attempt: a run can end with TWO result
// events, the first repeating an OLDER message (run 6f1b9503), and that one is hidden too.
'use strict';

// (2026-10-27, i18n fix) the UI language: every shown line is rendered through t() (the English text is
// the key, so English output is unchanged); the locals once named t are renamed so none shadows it
const i18n = require('./i18n');
const { t } = i18n;

const UNPARSED = '[unparsed line]';
const TOOL_ARG_MAX = 120;
const RESULT_ERROR_MAX = 200;
const TOOLS_LIST_MAX = 300;
const EMPTY_TEXT = 'No runs yet. The log appears here when the runner starts this task.';
// A tool_result error that is a permission denial (claude.exe 2.1.278 wording, probed 2026-10-15:
// "Permission to use PowerShell has been denied because Claude Code is running in don't ask mode").
const DENIAL_RE = /permission|has been denied|haven't granted|not allowed/i;
// (2026-10-20, 0.4.4 logfix H) a PreToolUse hook that stopped a tool call, e.g. the owner's read_guard:
// "PreToolUse:Read hook error: read_guard (global rule #27): view.js is 20,052 bytes …" (real run log,
// 2026-10-20). The model then reads a slice: a note, not an error.
const HOOK_DENIAL_RE = /^(?:Error:\s*)?PreToolUse:(\w+) hook (?:error|denied|blocked)\b[^:]*:?\s*([A-Za-z0-9_.-]+)?/;

/**
 * The one-line note for a hook denial, or null when `text` is not one.
 * @param {string} text the tool_result text
 */
function hookDenial(text) {
  const m = HOOK_DENIAL_RE.exec(String(text || '').trim());
  if (!m) return null;
  if (!m[2]) return t('hook: a hook stopped this {0} call', m[1]);
  return m[2] === 'read_guard' ? t('hook: read_guard asked for a slice') : t('hook: {0} stopped this {1} call', m[2], m[1]);
}
const PATH_TOOLS = ['Read', 'Edit', 'Write', 'MultiEdit', 'NotebookEdit'];
const PATTERN_TOOLS = ['Glob', 'Grep'];

/** @param {number} n */
const pad2 = (n) => String(n).padStart(2, '0');

/**
 * Local `MM-DD HH:MM:SS` (a log reads in seconds); '?' for a bad time.
 * @param {Date | string | number | null | undefined} when
 */
function localStampSec(when) {
  if (when === null || when === undefined || when === '') return '?';
  const d = when instanceof Date ? when : new Date(when);
  if (Number.isNaN(d.getTime())) return '?';
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/**
 * `resetsAt` is epoch seconds in 2.1.278; a value that looks like milliseconds is accepted too.
 * @param {unknown} v
 */
function epochToDate(v) {
  const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() ? Number(v) : NaN;
  if (!Number.isFinite(n) || n <= 0) return null;
  return new Date(n > 1e12 ? n : n * 1000);
}

/** @param {unknown} v */
const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
/** @param {unknown} v @param {string} [dflt] */
const str = (v, dflt = '?') => (typeof v === 'string' && v ? v : typeof v === 'number' ? String(v) : dflt);
/** @param {string} s @param {number} max */
const clip = (s, max) => (s.length <= max ? s : s.slice(0, max) + '…');
/** @param {string} s */
const oneLine = (s) => s.replace(/\s+/g, ' ').trim();

/** @param {unknown} v */
function money(v) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '$?';
  return `$${v.toFixed(v < 1 ? 3 : 2)}`;
}

/**
 * (2026-10-20) How an attempt was billed, from its init event's `apiKeySource` (claude.exe 2.1.304:
 * "ANTHROPIC_API_KEY" | "apiKeyHelper" | "/login managed key" | "none"; "none" = no API key, i.e. the
 * claude.ai subscription login in every real run log). `total_cost_usd` is an API-equivalent estimate,
 * so it is shown only for 'api'. A missing source (older runs, no init line) is 'unknown'.
 * @param {unknown} source
 * @returns {'api' | 'subscription' | 'unknown'}
 */
function billingOf(source) {
  if (typeof source !== 'string' || !source) return 'unknown';
  return source === 'none' ? 'subscription' : 'api';
}

/**
 * The cost part of a result line: `$3.01 (API)` | `subscription` | null (unknown: no amount).
 * @param {string | null | undefined} billing @param {unknown} cost
 */
function costPart(billing, cost) {
  if (billing === 'api') return typeof cost === 'number' && Number.isFinite(cost) ? `${money(cost)} (API)` : null;
  if (billing === 'subscription') return t('subscription');
  return null;
}

// (2026-10-20) claude.exe 2.1.304 stderr when the workspace's trust dialog was never accepted: "Ignoring
// 10 permissions.allow entries from .claude/settings.local.json: this workspace has not been trusted. …"
const UNTRUSTED_RE = /Ignoring \d+ permissions\.allow entr(?:y|ies) from (\S+?):? this workspace has not been trusted/i;

// (2026-10-21, v049 dontask_hold addendum) claude.exe -p killed the background tasks at the end of the
// run (item 86735ff0 stderr): "Background tasks still running after 600s; terminating. Set
// CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0 to wait indefinitely." The same loose match as the core's
// runner.BG_KILLED_RE. Lost work: a visible warning, never a muted CLI note.
const BG_KILLED_RE = /background\s+tasks?\b[^\n]{0,80}?\bstill\s+running\b[^\n]{0,80}?\bterminat/i;

// (2026-10-21, v049 dontask_hold addendum) the warning for a BG_KILLED_RE line; {0} = what claude.exe said
const BG_KILLED_WARNING = 'Background agents were killed when the run ended ("{0}"): the work they were doing is lost.';

/**
 * Split an attempt's stderr into known, harmless CLI warnings (one-line notes), lines that mean lost
 * work (`warnings`: claude.exe's own first sentence, without its "set it to 0" advice; the caller
 * shows it loud with BG_KILLED_WARNING) and the rest.
 * @param {unknown} stderr
 * @returns {{notes: string[], warnings: string[], rest: string}}
 */
function stderrNotes(stderr) {
  /** @type {string[]} */
  const notes = [];
  /** @type {string[]} */
  const warnings = [];
  /** @type {string[]} */
  const rest = [];
  for (const line of String(stderr || '').replace(/\r\n/g, '\n').split('\n')) {
    const m = UNTRUSTED_RE.exec(line);
    if (m) {
      notes.push(t("Project allow-list in {0} ignored (workspace not trusted); autopilot's profile permissions apply.", m[1].replace(/:$/, '')));
    } else if (BG_KILLED_RE.test(line)) {
      warnings.push(line.trim().split(/(?<=\.)\s+/)[0].slice(0, 200));
    } else rest.push(line);
  }
  return { notes, warnings, rest: rest.join('\n').trim() };
}

/**
 * `▶ <Tool> <short arg>` (SPEC_V03 1.2 table).
 * @param {Record<string, any>} block a tool_use content block
 */
function toolLine(block) {
  const name = str(block.name, 'tool');
  const input = isObj(block.input) ? block.input : {};
  let arg;
  if (PATH_TOOLS.includes(name)) arg = str(input.file_path || input.notebook_path || input.path, '');
  else if (PATTERN_TOOLS.includes(name)) {
    arg = str(input.pattern, '');
    if (typeof input.path === 'string' && input.path) arg = t('{0} in {1}', arg, input.path);
  } else {
    let json = '';
    try { json = JSON.stringify(block.input === undefined ? {} : block.input); } catch (_e) { json = '?'; }
    arg = clip(json, TOOL_ARG_MAX);
  }
  return `▶ ${name}${arg ? ` ${arg}` : ''}`;
}

/** The text of a tool_result `content` (a string, or an array of {type:'text', text}). @param {unknown} c */
function resultText(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) {
    return c.map((b) => (isObj(b) && typeof b.text === 'string' ? b.text : isObj(b) && b.type === 'image' ? '[image]' : ''))
      .filter(Boolean).join('\n');
  }
  return '';
}

/**
 * `  ↳ ok (<n> lines)`, `  ↳ ERROR: <first 200 chars>`, `  ↳ denied: <reason>` or (0.4.4 logfix H)
 * `  ↳ hook: <note>`.
 * @param {Record<string, any>} block a tool_result content block
 */
function toolResultLine(block) {
  const text = resultText(block.content);
  if (block.is_error === true) {
    const flat = oneLine(text);
    const hook = hookDenial(flat);
    if (hook) return `  ↳ ${hook}`;
    if (DENIAL_RE.test(flat)) return `  ↳ ${t('denied: {0}', clip(flat, RESULT_ERROR_MAX))}`;
    return `  ↳ ${t('ERROR: {0}', clip(flat, RESULT_ERROR_MAX))}`;
  }
  const n = text ? text.replace(/\n+$/, '').split('\n').length : 0;
  return `  ↳ ${t(n === 1 ? 'ok ({0} line)' : 'ok ({0} lines)', n)}`;
}

/**
 * The attempt header (system/init).
 * @param {Record<string, any>} ev @param {{attempt?: number | null, startedAt?: Date | string | number | null}} ctx
 */
function headerLine(ev, ctx) {
  const tools = Array.isArray(ev.tools) ? clip(ev.tools.filter((x) => typeof x === 'string').join(', '), TOOLS_LIST_MAX) : '?';
  return `── ${t('Attempt {0}', ctx.attempt != null ? ctx.attempt : '?')} · ${localStampSec(ctx.startedAt)}`
    + ` · ${t('session {0}', str(ev.session_id))} · ${t('model {0}', str(ev.model))}`
    + ` · ${t('tools {0}', tools || t('(none)'))} ──`;
}

/** A header for an attempt whose init line is missing (it died first, or was cut). @param {{attempt?: number | null, startedAt?: Date | string | number | null}} ctx */
function fallbackHeader(ctx) {
  return `── ${t('Attempt {0}', ctx.attempt != null ? ctx.attempt : '?')} · ${localStampSec(ctx.startedAt)} ──`;
}

/**
 * (2026-10-27, i18n fix) Is `line` an attempt header (headerLine / fallbackHeader) in ANY UI language?
 * livelog keeps lines rendered before a language switch, so the current language alone is not enough.
 * A translation whose 'Attempt {0}' starts with the number gives no prefix and is skipped.
 * @param {unknown} line
 */
function isAttemptHeader(line) {
  if (typeof line !== 'string') return false;
  return ['en', ...i18n.LANGUAGES].some((lang) => {
    const d = /** @type {Record<string, string>} */ (i18n.DICT[lang] || {});
    const word = String(d['Attempt {0}'] || 'Attempt {0}').split('{0}')[0].trim();
    return !!word && line.startsWith(`── ${word} `);
  });
}

/**
 * @typedef {{lastText: string | null, texts?: Set<string>, billing?: string, resultOk?: boolean}} RenderState
 *   per attempt, carried across lines; `texts` = every assistant text of the attempt (normalised), see
 *   repeatsAnyText; (2026-10-20) `billing` from the init line, `resultOk` from the last result line
 * @typedef {{attempt?: number | null, startedAt?: Date | string | number | null, state?: RenderState}} Ctx
 */

/** @param {string} s */
const normText = (s) => s.replace(/\r\n/g, '\n').trim();

/**
 * (2026-10-17, 0.4.0) True when a result's text only repeats the last assistant text (after trim).
 * @param {unknown} resultText @param {string | null | undefined} lastText
 */
function repeatsLastText(resultText, lastText) {
  return typeof resultText === 'string' && typeof lastText === 'string' && normText(resultText) !== ''
    && normText(resultText) === normText(lastText);
}

/** A fresh per-attempt state. @returns {RenderState} */
function newRenderState() {
  return { lastText: null, texts: new Set() };
}

/**
 * (0.4.1 D1) Remember an assistant event's text: the joined text and each block on its own.
 * @param {RenderState} state @param {string} joined @param {string[]} [parts]
 */
function rememberText(state, joined, parts = []) {
  state.lastText = joined;
  if (!state.texts) state.texts = new Set();
  for (const s of [joined, ...parts]) {
    const n = normText(s);
    if (n) state.texts.add(n);
  }
}

/**
 * (0.4.1 D1) True when a result's text repeats ANY assistant text of the attempt (normalised as in
 * repeatsLastText), not only the last one.
 * @param {unknown} resultText @param {RenderState | null | undefined} state
 */
function repeatsAnyText(resultText, state) {
  if (!state || typeof resultText !== 'string') return false;
  const n = normText(resultText);
  if (!n) return false;
  return (!!state.texts && state.texts.has(n)) || repeatsLastText(resultText, state.lastText);
}

/**
 * The joined text blocks of an assistant event, or null when it has none.
 * @param {Record<string, any>} e
 */
function assistantText(e) {
  if (e.type !== 'assistant' || !isObj(e.message) || !Array.isArray(e.message.content)) return null;
  const parts = e.message.content.filter((b) => isObj(b) && b.type === 'text' && typeof b.text === 'string' && b.text.trim())
    .map((b) => b.text);
  return parts.length ? parts.join('\n') : null;
}

/**
 * Render one parsed stream-json event to zero or more lines.
 * @param {unknown} ev
 * @param {Ctx} [ctx]
 * @returns {string[]}
 */
function renderEvent(ev, ctx = {}) {
  if (!isObj(ev)) return [t(UNPARSED)];
  const e = /** @type {Record<string, any>} */ (ev);
  const type = e.type;
  if (type === 'system') {
    if (e.subtype === 'init') {
      if (ctx.state) ctx.state.billing = billingOf(e.apiKeySource); // (2026-10-20) for the result line
      return [headerLine(e, ctx)];
    }
    if (e.subtype === 'permission_denied') return [`  ⛔ ${t('denied {0}: {1}', str(e.tool_name), str(e.decision_reason_type || e.reason))}`];
    if (e.subtype === 'compact_boundary') return [`── ${t('context compacted')} ──`];
    return []; // other system events are skipped
  }
  if (type === 'assistant' || type === 'user') {
    const said = type === 'assistant' ? assistantText(e) : null;
    if (said !== null && ctx.state) {
      const parts = e.message.content.filter((/** @type {any} */ b) => isObj(b) && b.type === 'text' && typeof b.text === 'string').map((/** @type {any} */ b) => b.text);
      rememberText(ctx.state, said, parts);
    }
    const content = isObj(e.message) && Array.isArray(e.message.content) ? e.message.content : [];
    /** @type {string[]} */
    const out = [];
    for (const b of content) {
      if (!isObj(b)) continue;
      if (type === 'assistant' && b.type === 'text' && typeof b.text === 'string') {
        if (b.text.trim()) out.push(...b.text.replace(/\r\n/g, '\n').replace(/\n+$/, '').split('\n'));
      } else if (type === 'assistant' && b.type === 'tool_use') out.push(toolLine(b));
      else if (type === 'user' && b.type === 'tool_result') out.push(toolResultLine(b));
      // thinking / redacted_thinking blocks are never shown; a user's text block is the prompt
    }
    return out;
  }
  if (type === 'rate_limit_event') {
    const info = isObj(e.rate_limit_info) ? e.rate_limit_info : {};
    if (info.status !== 'rejected') return [];
    const d = epochToDate(info.resetsAt);
    return [`⏸ ${d ? t('usage limit reached; resets {0}', localStampSec(d)) : t('usage limit reached; resets at an unknown time')}`];
  }
  if (type === 'result') {
    // (2026-10-20) the amount only for API billing; a subscription run says `subscription`
    const cost = costPart(ctx.state ? ctx.state.billing : null, e.total_cost_usd);
    if (ctx.state) ctx.state.resultOk = e.subtype === 'success' && e.is_error !== true;
    const head = `── ${t('Result: {0}', str(e.subtype))} · ${t('turns {0}', str(e.num_turns))} · ${cost ? `${cost} · ` : ''}${str(e.terminal_reason)} ──`;
    const dup = repeatsAnyText(e.result, ctx.state);
    const text = !dup && typeof e.result === 'string' && e.result.trim() ? e.result.replace(/\r\n/g, '\n').replace(/\n+$/, '').split('\n') : [];
    return [head, ...text];
  }
  return []; // stream_event and anything new: not part of the readable log
}

/**
 * Render one raw jsonl line (tolerant: a bad line is `[unparsed line]`, a blank line nothing).
 * @param {string} line
 * @param {Ctx} [ctx]
 * @returns {string[]}
 */
function renderLine(line, ctx = {}) {
  const raw = String(line == null ? '' : line).trim();
  if (!raw) return [];
  let ev;
  try {
    ev = JSON.parse(raw);
  } catch (_e) {
    return [t(UNPARSED)];
  }
  return renderEvent(ev, ctx);
}

/** @param {number} n */
function cutMarker(n) {
  return t(n === 1 ? '… ({0} earlier event not shown — Open Run Log Folder for the raw file)'
    : '… ({0} earlier events not shown — Open Run Log Folder for the raw file)', n);
}

/**
 * @typedef {{attempt: number, startedAt?: Date | string | number | null, headLine?: string | null,
 *   lines?: string[], rendered?: string[][], skipped?: number, stderr?: string | null,
 *   state?: RenderState}} Attempt
 *   `lines` = raw jsonl lines in the window, or `rendered` = those lines already rendered (livelog);
 *   `skipped` = how many earlier lines were cut (2 MB window); `headLine` = the file's FIRST line,
 *   kept for the header when it was cut; `state` = the render state `rendered` was made with.
 * @typedef {{at?: string | null, text?: string, attempt?: number | null}} Reply
 */

/**
 * One attempt: header, cut marker, the events, then stderr.
 * @param {Attempt} a
 * @returns {string[]}
 */
function renderAttempt(a) {
  const ctx = { attempt: a.attempt, startedAt: a.startedAt, state: newRenderState() };
  /** @type {string[]} */
  const out = [];
  const skipped = a.skipped || 0;
  // the head line first: its init event carries the billing the body's result line needs
  const head = skipped > 0 && a.headLine ? renderLine(a.headLine, ctx) : [];
  const body = a.rendered ? a.rendered.flat() : (a.lines || []).flatMap((l) => renderLine(l, ctx));
  if (skipped > 0) {
    out.push(head.length && isAttemptHeader(head[0]) ? head[0] : fallbackHeader(ctx));
    out.push(cutMarker(skipped));
  } else if (!body.length || !isAttemptHeader(body[0])) {
    out.push(fallbackHeader(ctx));
  }
  out.push(...body);
  // (2026-10-20) stderr is red-flagged (`stderr`) only when the attempt did not end in a success
  // result; after a success it is `CLI notes`. A known harmless warning is one `ℹ` line.
  const st = a.rendered ? a.state || {} : ctx.state;
  const { notes, warnings, rest } = stderrNotes(a.stderr);
  // (2026-10-21, v049 dontask_hold addendum) lost work
  for (const w of warnings) out.push(`⚠ ${t(BG_KILLED_WARNING, w)}`);
  for (const n of notes) out.push(`ℹ ${n}`);
  const errLines = rest.split('\n').filter((l) => l.trim());
  if (errLines.length) out.push(`── ${st.resultOk === true ? t('CLI notes') : t('stderr')} ──`, ...errLines);
  return out;
}

/** @param {Reply} r @returns {string[]} */
function renderReply(r) {
  const text = String(r.text || '').replace(/\r\n/g, '\n').replace(/\n+$/, '');
  return [t('── Owner reply · {0} ──', localStampSec(r.at || null)), ...(text ? text.split('\n') : [])];
}

/**
 * The whole log of an item: every attempt in order; an owner's reply goes right before the attempt
 * that carried it (a reply not yet sent goes last). Nothing at all = the empty state.
 * @param {Attempt[]} attempts
 * @param {{replies?: Reply[]}} [opts]
 */
function renderTranscript(attempts, opts = {}) {
  const list = (attempts || []).slice().sort((x, y) => x.attempt - y.attempt);
  const replies = (opts.replies || []).filter((r) => r && typeof r.text === 'string');
  if (!list.length && !replies.length) return t(EMPTY_TEXT);
  /** @type {string[]} */
  const out = [];
  const used = new Set();
  for (const a of list) {
    replies.forEach((r, i) => {
      if (!used.has(i) && r.attempt === a.attempt) {
        used.add(i);
        if (out.length) out.push('');
        out.push(...renderReply(r));
      }
    });
    if (out.length) out.push('');
    out.push(...renderAttempt(a));
  }
  replies.forEach((r, i) => {
    if (used.has(i)) return;
    if (out.length) out.push('');
    out.push(...renderReply(r));
  });
  if (!list.length) out.push('', t(EMPTY_TEXT));
  return out.join('\n') + '\n';
}

module.exports = {
  UNPARSED,
  EMPTY_TEXT,
  TOOL_ARG_MAX,
  RESULT_ERROR_MAX,
  DENIAL_RE,
  HOOK_DENIAL_RE,
  hookDenial,
  isAttemptHeader,
  localStampSec,
  epochToDate,
  money,
  billingOf,
  costPart,
  stderrNotes,
  BG_KILLED_RE,
  BG_KILLED_WARNING,
  resultText,
  assistantText,
  repeatsLastText,
  repeatsAnyText,
  rememberText,
  newRenderState,
  renderEvent,
  renderLine,
  renderAttempt,
  renderReply,
  renderTranscript,
  cutMarker,
};

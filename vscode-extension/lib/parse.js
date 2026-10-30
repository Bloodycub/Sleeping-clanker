// @ts-check
// Parsing of the core's `--json` outputs, without the vscode API (2026-10-13, SPEC_VSCODE section 2).
// Malformed input never throws: it returns {ok:false, error} so the caller can show and log it.
// (2026-10-13, v0.2) Goal/attachment/pause fields (SPEC_GOALS 1) and `where`, `projects`,
// `migrate-global` (SPEC_PROJECTS 2, amendments M9/M10/M11).
// (2026-10-15, v0.3) `lanes --json [--items]`, `lanes` in projects rows, the `blocked` status and the
// reply fields (SPEC_V03 2.2, 5.1, 5.2).
'use strict';

const { t } = require('./i18n'); // (2026-10-27, i18n fix) staleRunnerWarning is a toast: the UI language
// (2026-10-27, tasks at once, SPEC_V05_PARALLEL 8.5) a merge question's actions are only these
const { QUESTION_ACTIONS, MERGE_ACTIONS, TASKS_AT_ONCE_MAX } = require('./argv');

const STATUSES = ['pending', 'running', 'waiting', 'paused', 'done', 'failed', 'cancelled', 'blocked'];
const DEFAULT_LANE = 'default';

/**
 * The JSON document is the last non-empty stdout line (console logs go to stderr, but a stray
 * print must not break parsing).
 * @param {string} stdout
 * @returns {{ok: true, value: any} | {ok: false, error: string}}
 */
function parseJsonOutput(stdout) {
  const text = String(stdout == null ? '' : stdout).trim();
  if (!text) return { ok: false, error: 'empty output (expected JSON)' };
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (_whole) {
    const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    const last = lines[lines.length - 1];
    try {
      return { ok: true, value: JSON.parse(last) };
    } catch (err) {
      return { ok: false, error: `not JSON: ${err instanceof Error ? err.message : String(err)}: ${last.slice(0, 200)}` };
    }
  }
}

/** @param {unknown} v */
const isObj = (v) => typeof v === 'object' && v !== null && !Array.isArray(v);
/** @param {unknown} v */
const strOrNull = (v) => (typeof v === 'string' ? v : null);
/** @param {unknown} v */
const numOrNull = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/**
 * (2026-10-21, v049 dontask_hold) The queue's hold from `status --json` / a `lanes --json` row: null when
 * not held (or from a core before 0.4.9, which has no "hold" key), else `{since}` (since may be null: an
 * unreadable hold file still holds).
 * (2026-10-26, limit_continue) A usage-limit hold (`run --on-limit ask`) also has `reason: 'limit'` and
 * `resetsAt` (the item's wait_until); any other hold keeps the plain `{since}` shape.
 * @param {unknown} v
 * (2026-10-26, usage forecast) `until`: a timed hold (`hold on --until`), which a runner waits out
 * instead of exiting; null = an ordinary hold.
 * (2026-10-27, integrate) the two compose: `until` and `reason`/`resetsAt` are each added when present.
 * @returns {{since: string | null, until?: string | null, reason?: string, resetsAt?: string | null} | null}
 */
function parseHold(v) {
  if (v === true) return { since: null };
  if (!isObj(v)) return null;
  const o = /** @type {any} */ (v);
  const until = strOrNull(o.until);
  const base = until ? { since: strOrNull(o.since), until } : { since: strOrNull(o.since) };
  if (o.reason === 'limit') return { ...base, reason: 'limit', resetsAt: strOrNull(o.resets_at) };
  return base;
}

/**
 * (2026-10-21, v049 dontask_hold) `hold on|off|status --json`: {hold, since, runner_alive, runner_pid, data_dir}.
 * @param {string} stdout
 * @returns {{ok: true, value: {hold: boolean, since: string | null, runnerAlive: boolean, runnerPid: number | null, dataDir: string,
 *   until?: string}} | {ok: false, error: string}}
 */
function parseHoldResult(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.hold !== 'boolean' || typeof v.data_dir !== 'string') {
    return { ok: false, error: 'hold --json: expected {hold, data_dir}' };
  }
  const alive = v.runner_alive === true;
  return { ok: true, value: { hold: v.hold, since: strOrNull(v.since), runnerAlive: alive,
    runnerPid: alive && Number.isInteger(v.runner_pid) && v.runner_pid > 0 ? v.runner_pid : null, dataDir: v.data_dir,
    // (2026-10-27, usage forecast fix LOW) a timed hold's end (`hold on --until`); absent = none
    ...(strOrNull(v.until) ? { until: strOrNull(v.until) } : {}) } };
}

/**
 * @typedef {{counts: Record<string, number>, runnerAlive: boolean, runnerPid: number | null,
 *   nextWaitUntil: string | null, dataDir: string,
 *   hold: {since: string | null, until?: string | null, reason?: string, resetsAt?: string | null} | null}} Status
 */

/**
 * @param {string} stdout
 * @returns {{ok: true, value: Status} | {ok: false, error: string}}
 */
function parseStatus(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v)) return { ok: false, error: 'status --json: expected an object' };
  if (!isObj(v.counts)) return { ok: false, error: 'status --json: "counts" missing or not an object' };
  if (typeof v.runner_alive !== 'boolean') return { ok: false, error: 'status --json: "runner_alive" is not a boolean' };
  if (typeof v.data_dir !== 'string' || !v.data_dir) return { ok: false, error: 'status --json: "data_dir" missing' };
  /** @type {Record<string, number>} */
  const counts = {};
  for (const s of STATUSES) {
    const n = v.counts[s];
    counts[s] = Number.isInteger(n) && n >= 0 ? n : 0;
  }
  const pid = Number.isInteger(v.runner_pid) && v.runner_pid > 0 ? v.runner_pid : null;
  return {
    ok: true,
    value: {
      counts,
      runnerAlive: v.runner_alive,
      runnerPid: v.runner_alive ? pid : null,
      nextWaitUntil: strOrNull(v.next_wait_until),
      dataDir: v.data_dir,
      hold: parseHold(v.hold),
    },
  };
}

/**
 * @typedef {{id: string, status: string, prompt: string, name: string | null, cwd: string,
 *   session_id: string | null, attempts: number, wait_until: string | null, result: string | null,
 *   error: string | null, profile: string | null, created_at: string | null, started_at: string | null,
 *   finished_at: string | null, limit_waits: number, num_turns: number | null, cost_usd: number | null,
 *   total_cost_usd: number | null, billing?: 'api' | 'subscription' | 'unknown', api_key_source?: string | null,
 *   kind: 'task' | 'goal', attachments: string[], rounds: number,
 *   max_rounds: number | null, max_hours: number | null, goal_started_at: string | null,
 *   paused_from: string | null, replies: {at: string | null, text: string, attempt: number | null, by?: 'autorun'}[],
 *   pending_reply: string | null, blocked_question?: Question | null, repeat_block?: number,
 *   parent?: string | null, parent_label?: string | null, model?: string | null,
 *   max_agents?: number | null, provider?: string | null, effort?: string | null,
 *   ultracode?: boolean, autorun?: boolean,
 *   denial_block?: DenialBlock | null, lane: string,
 *   after?: string | null, after_source?: string | null, in_place?: boolean,
 *   worktree?: {path: string | null, state: string | null, recreated: number} | null,
 *   merge?: ReturnType<typeof normalizeMerge>, phase?: string | null, waits?: {code: string, args: Record<string, string>} | null,
 *   raw: Record<string, any>}} Item
 *   `lane` is not in the core's item JSON: the lane the item was listed in (2026-10-15).
 *   (2026-10-27, tasks at once) after .. waits: SPEC_V05_PARALLEL 8.5 (absent/null in an older core's JSON)
 * @typedef {{n: number, label: string, detail: string, recommended: boolean}} QuestionOption
 * @typedef {{text: string, options: QuestionOption[], kind?: 'merge', code?: string,
 *   args?: {paths: string[], n: number, from?: string, to?: string, copy?: string}, actions?: string[], gen?: number | null}} Question
 *   (2026-10-27, tasks at once) kind .. gen: only on a merge question (normalizeQuestion)
 */

/** @param {unknown} v */
function normalizeReplies(v) {
  if (!Array.isArray(v)) return [];
  return v.filter((r) => isObj(r) && typeof r.text === 'string')
    .map((r) => ({ at: strOrNull(r.at), text: r.text, attempt: numOrNull(r.attempt),
      // (2026-10-27, modes fix round 2) who answered; (2026-10-29, integrate round 9) + 'autopick' (Auto-pick best)
      ...(r.by === 'autorun' || r.by === 'autopick' ? { by: r.by } : {}) }));
}

const QUESTION_LABEL_MAX = 80;

/**
 * (2026-10-19, 0.4.1) The core's additive `blocked_question`: null, or {text, options} with 2–4
 * options of unique integer n (1–4), a non-empty label (≤ 80 chars), a detail string and a
 * boolean `recommended` (at most one). Anything else in `options` makes the list empty (free-text
 * reply, as before 0.4.1); a missing or malformed field is null.
 * @param {unknown} v
 * @returns {Question | null}
 */
function normalizeQuestion(v) {
  if (!isObj(v)) return null;
  const q = /** @type {Record<string, any>} */ (v);
  const text = typeof q.text === 'string' ? q.text : '';
  /** @type {QuestionOption[]} */
  const options = [];
  const raw = Array.isArray(q.options) ? q.options : [];
  const seen = new Set();
  let valid = raw.length >= 2 && raw.length <= 4;
  let recommended = false;
  for (const o of valid ? raw : []) {
    const n = isObj(o) ? o.n : undefined;
    const label = isObj(o) && typeof o.label === 'string' ? o.label.replace(/\s+/g, ' ').trim() : '';
    if (!Number.isInteger(n) || n < 1 || n > 4 || seen.has(n) || !label) { valid = false; break; }
    seen.add(n);
    const rec = o.recommended === true && !recommended;
    recommended = recommended || rec;
    options.push({ n, ...fitLabel(label, typeof o.detail === 'string' ? o.detail.trim() : ''), recommended: rec });
  }
  // (2026-10-27, tasks at once, SPEC_V05_PARALLEL 3.5/8.5) a merge question also keeps its code, args, the
  // actions it offers (only QUESTION_ACTIONS, in the core's order) and its gen; any other question parses as before
  if (q.kind === 'merge') {
    const actions = Array.isArray(q.actions) ? q.actions.filter((a, i, all) => QUESTION_ACTIONS.includes(a) && all.indexOf(a) === i) : [];
    return { text, options: valid ? options : [], kind: 'merge', code: typeof q.code === 'string' ? q.code : '',
      args: mergeQuestionArgs(q.args), actions, gen: Number.isInteger(q.gen) && q.gen >= 0 ? q.gen : null };
  }
  return { text, options: valid ? options : [] };
}

const MERGE_PATHS_MAX = 20; // == worktree.QUESTION_PATHS_MAX
const MERGE_LIST_MAX = 200; // merge.paths / merge.changed (spec 3.3/3.5: <= 200)

/** Strings only, at most `max`. @param {unknown} v @param {number} max @returns {string[]} */
const strList = (v, max) => (Array.isArray(v) ? v.filter((p) => typeof p === 'string' && p).slice(0, max) : []);

/**
 * (2026-10-27, tasks at once) A merge question's args: {paths (<= 20), n (total), from?, to? (branch-switched),
 * copy? (nested-repo)}; nothing else is kept.
 * @param {unknown} v @returns {{paths: string[], n: number, from?: string, to?: string, copy?: string}}
 */
function mergeQuestionArgs(v) {
  const a = isObj(v) ? /** @type {Record<string, any>} */ (v) : {};
  const paths = strList(a.paths, MERGE_PATHS_MAX);
  /** @type {{paths: string[], n: number, from?: string, to?: string, copy?: string}} */
  const out = { paths, n: Number.isInteger(a.n) && a.n >= paths.length ? a.n : paths.length };
  for (const k of /** @type {const} */ (['from', 'to', 'copy'])) if (typeof a[k] === 'string' && a[k]) out[k] = a[k];
  return out;
}

/**
 * (2026-10-27, tasks at once, SPEC_V05_PARALLEL 8.5) An item's copy record: {path, state, recreated}; null = none.
 * @param {unknown} v
 */
function normalizeWorktree(v) {
  if (!isObj(v)) return null;
  const w = /** @type {Record<string, any>} */ (v);
  return { path: strOrNull(w.path), state: strOrNull(w.state), recreated: Number.isInteger(w.recreated) && w.recreated > 0 ? w.recreated : 0 };
}

/**
 * (2026-10-27, tasks at once, SPEC_V05_PARALLEL 3.11/8.5) An item's merge record; null = none (and an older core).
 * @param {unknown} v
 */
function normalizeMerge(v) {
  if (!isObj(v)) return null;
  const m = /** @type {Record<string, any>} */ (v);
  return {
    state: strOrNull(m.state), gen: Number.isInteger(m.gen) && m.gen >= 0 ? m.gen : 0, kind: strOrNull(m.kind),
    wait: strOrNull(m.wait), result_status: strOrNull(m.result_status),
    paths: strList(m.paths, MERGE_LIST_MAX), changed: strList(m.changed, MERGE_LIST_MAX),
    preview: strOrNull(m.preview), backup: strOrNull(m.backup), patch: strOrNull(m.patch), commits: strList(m.commits, 10),
    // (2026-10-29, delta M1) files no merge could carry (paths only), their count and the copy they are kept in
    not_merged: strList(m.not_merged, MERGE_LIST_MAX),
    not_merged_n: Number.isInteger(m.not_merged_n) && m.not_merged_n > 0 ? m.not_merged_n : 0, kept: strOrNull(m.kept),
  };
}

/**
 * (2026-10-27, tasks at once, SPEC_V05_PARALLEL 5.5) `waits`: why a pending item does not start ({code, args}); null = none.
 * @param {unknown} v
 */
function normalizeWaits(v) {
  if (!isObj(v) || typeof v.code !== 'string' || !v.code) return null;
  /** @type {Record<string, string>} */
  const args = {};
  if (isObj(v.args)) {
    for (const [k, x] of Object.entries(/** @type {Record<string, unknown>} */ (v.args))) {
      if (typeof x === 'string' || typeof x === 'number') args[k] = String(x);
    }
  }
  return { code: /** @type {string} */ (v.code), args };
}

/**
 * (2026-10-26, owner d7f2e585 "why text isent full?") As goals.py `_fit_label`: a label over 80 chars
 * ends at a word with "…" and the rest starts the detail (a hard cut dropped it: "… shows (e").
 * @param {string} label @param {string} detail @returns {{label: string, detail: string}}
 */
function fitLabel(label, detail) {
  if (label.length <= QUESTION_LABEL_MAX) return { label, detail };
  let cut = label.lastIndexOf(' ', QUESTION_LABEL_MAX - 1);
  if (cut <= QUESTION_LABEL_MAX / 2) cut = QUESTION_LABEL_MAX - 1;
  const rest = label.slice(cut).trim();
  return { label: `${label.slice(0, cut).trimEnd()}…`, detail: `…${rest}${detail ? ` — ${detail}` : ''}` };
}

/**
 * @param {unknown} v
 * @param {string} [lane]
 * @returns {Item | null}
 */
function normalizeItem(v, lane = DEFAULT_LANE) {
  if (!isObj(v)) return null;
  const o = /** @type {Record<string, any>} */ (v);
  if (typeof o.id !== 'string' || !o.id) return null;
  return {
    id: o.id,
    status: STATUSES.includes(o.status) ? o.status : 'unknown',
    prompt: typeof o.prompt === 'string' ? o.prompt : '',
    name: strOrNull(o.name),
    cwd: typeof o.cwd === 'string' ? o.cwd : '',
    session_id: strOrNull(o.session_id),
    attempts: numOrNull(o.attempts) ?? 0,
    wait_until: strOrNull(o.wait_until),
    result: strOrNull(o.result),
    error: strOrNull(o.error),
    profile: strOrNull(o.profile),
    created_at: strOrNull(o.created_at),
    started_at: strOrNull(o.started_at),
    finished_at: strOrNull(o.finished_at),
    limit_waits: numOrNull(o.limit_waits) ?? 0,
    num_turns: numOrNull(o.num_turns),
    cost_usd: numOrNull(o.cost_usd),
    total_cost_usd: numOrNull(o.total_cost_usd),
    // (2026-10-20, core 'billing') the cost is an amount someone pays only when 'api'
    billing: o.billing === 'api' || o.billing === 'subscription' ? o.billing : 'unknown',
    api_key_source: strOrNull(o.api_key_source),
    kind: o.kind === 'goal' ? 'goal' : 'task', // an old queue.json has no kind: a task
    attachments: Array.isArray(o.attachments) ? o.attachments.filter((a) => typeof a === 'string') : [],
    rounds: numOrNull(o.rounds) ?? 0,
    max_rounds: numOrNull(o.max_rounds),
    max_hours: numOrNull(o.max_hours),
    goal_started_at: strOrNull(o.goal_started_at),
    paused_from: strOrNull(o.paused_from),
    replies: normalizeReplies(o.replies),
    pending_reply: strOrNull(o.pending_reply),
    blocked_question: normalizeQuestion(o.blocked_question),
    // (2026-10-20, 0.4.4 logfix C) how many attempts in a row blocked with the same question
    repeat_block: Number.isInteger(o.repeat_block) && o.repeat_block > 0 ? o.repeat_block : 0,
    // (2026-10-21, subtasks) the main task's id; null for a main task (and an older core)
    parent: strOrNull(o.parent),
    parent_label: strOrNull(o.parent_label),
    // (2026-10-21, task model) the task's own model; null = the run's (and an older core)
    model: strOrNull(o.model),
    // (2026-10-21, subagents) how many subagents the task may start; null = the profile decides
    // (an older core too), 0 = none, -1 = no limit
    max_agents: numOrNull(o.max_agents),
    // (2026-10-21, add panel) the task's own provider and effort; null = the runner's (an older core too)
    provider: strOrNull(o.provider),
    effort: strOrNull(o.effort),
    // (2026-10-26, modes) on only when the core says exactly true (an older core: off)
    ultracode: o.ultracode === true,
    autorun: o.autorun === true,
    // (2026-10-21, v049 dontask_hold) what don't-ask mode denied and the owner's fix; null = none
    denial_block: normalizeDenialBlock(o.denial_block),
    // (2026-10-27, tasks at once, SPEC_V05_PARALLEL 5.1/8.5) the order, the copy and the merge-back; an older
    // core has none of them: no wait, in a copy never, no merge
    after: strOrNull(o.after),
    after_source: o.after_source === 'owner' || o.after_source === 'split' ? o.after_source : null,
    in_place: o.in_place === true,
    worktree: normalizeWorktree(o.worktree),
    merge: normalizeMerge(o.merge),
    phase: strOrNull(o.phase),
    waits: normalizeWaits(o.waits),
    lane,
    raw: o,
  };
}

const DENIAL_ROWS_MAX = 3;
const DENIAL_ALLOW_MAX = 3;
const DENIAL_TEXT_MAX = 600;

/**
 * (2026-10-21, v049 dontask_hold) The core's `denial_block` on a blocked item: {denied: [{tool, what,
 * fix, note}] (at most 3), more, profile (the profile to retry with, or null), allow (allow-list entries
 * to add, at most 3), hint (the fix as one CLI sentence)}. null when missing or malformed (an older core).
 * @param {unknown} v
 * @returns {DenialBlock | null}
 */
function normalizeDenialBlock(v) {
  if (!isObj(v)) return null;
  const o = /** @type {Record<string, any>} */ (v);
  const str = (/** @type {unknown} */ s, max = DENIAL_TEXT_MAX) => (typeof s === 'string' ? s.slice(0, max) : '');
  const denied = (Array.isArray(o.denied) ? o.denied : []).filter(isObj).slice(0, DENIAL_ROWS_MAX)
    .map((/** @type {Record<string, any>} */ d) => ({ tool: str(d.tool, 60), what: str(d.what, 200),
      fix: strOrNull(d.fix), note: str(d.note) }));
  const allow = (Array.isArray(o.allow) ? o.allow : []).filter((/** @type {unknown} */ a) => typeof a === 'string'
    && a.trim() && !a.trim().startsWith('-')).slice(0, DENIAL_ALLOW_MAX).map((/** @type {string} */ a) => a.trim().slice(0, 200));
  const hint = str(o.hint);
  if (!denied.length && !hint) return null;
  return { denied, more: Number.isInteger(o.more) && o.more > 0 ? o.more : 0,
    profile: typeof o.profile === 'string' && o.profile ? o.profile : null, allow, hint };
}

/**
 * @typedef {{denied: {tool: string, what: string, fix: string | null, note: string}[], more: number,
 *   profile: string | null, allow: string[], hint: string}} DenialBlock
 */

/**
 * @param {string} stdout
 * @returns {{ok: true, value: Item[], warnings: string[]} | {ok: false, error: string}}
 */
function parseList(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  if (!Array.isArray(r.value)) return { ok: false, error: 'list --json: expected an array' };
  /** @type {Item[]} */
  const items = [];
  /** @type {string[]} */
  const warnings = [];
  r.value.forEach((v, i) => {
    const item = normalizeItem(v);
    if (item) items.push(item);
    else warnings.push(`list --json: entry ${i} skipped (not an object with an id)`);
  });
  return { ok: true, value: items, warnings };
}

/**
 * @param {string} stdout
 * @returns {{ok: true, value: Item} | {ok: false, error: string}}
 */
function parseShow(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const item = normalizeItem(r.value);
  return item ? { ok: true, value: item } : { ok: false, error: 'show --json: expected an item object with an id' };
}

/**
 * @param {string} stdout
 * @returns {{ok: true, value: string} | {ok: false, error: string}}
 */
function parseAdd(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  if (!isObj(r.value) || typeof r.value.id !== 'string' || !r.value.id) {
    return { ok: false, error: 'add --json: expected {"id": ...}' };
  }
  return { ok: true, value: r.value.id };
}

/**
 * `where --json` -> {dataDir, projectRoot}; projectRoot is null for the shared queue.
 * @param {string} stdout
 * @returns {{ok: true, value: {dataDir: string, projectRoot: string | null}} | {ok: false, error: string}}
 */
function parseWhere(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.data_dir !== 'string' || !v.data_dir) {
    return { ok: false, error: 'where --json: expected {"data_dir": ...}' };
  }
  return { ok: true, value: { dataDir: v.data_dir, projectRoot: strOrNull(v.project_root) } };
}

/**
 * @typedef {{name: string, dataDir: string, error: string | null, counts: Record<string, number>,
 *   runnerAlive: boolean, runnerPid: number | null, nextWaitUntil: string | null, items: Item[],
 *   runnerVersion?: string | null,
 *   hold?: {since: string | null, until?: string | null, reason?: string, resetsAt?: string | null} | null, usage?: unknown,
 *   tasksAtOnce?: number | null, parallel?: ReturnType<typeof normalizeParallel>}} Lane
 *   (2026-10-27, tasks at once) tasksAtOnce / parallel: normalizeParallel
 * @typedef {{name: string, dataDir: string, root: string | null, shared: boolean, rootExists: boolean,
 *   error: string | null, counts: Record<string, number>, runnerAlive: boolean, runnerPid: number | null,
 *   nextWaitUntil: string | null, items: Item[], lanes: Lane[]}} Project
 */

/** @param {unknown} c */
function normalizeCounts(c) {
  /** @type {Record<string, number>} */
  const counts = {};
  const src = isObj(c) ? /** @type {Record<string, any>} */ (c) : {};
  for (const s of STATUSES) {
    const n = src[s];
    counts[s] = Number.isInteger(n) && n >= 0 ? n : 0;
  }
  return counts;
}

/**
 * One lane row of `lanes --json [--items]` or of a project's `lanes` (2026-10-15, SPEC_V03 2.2).
 * @param {unknown} v @param {string} where for warnings @param {string[]} warnings
 * @returns {Lane | null}
 */
function normalizeLane(v, where, warnings) {
  if (!isObj(v) || typeof v.name !== 'string' || !v.name || typeof v.data_dir !== 'string' || !v.data_dir) {
    warnings.push(`${where} skipped (no name or data_dir)`);
    return null;
  }
  /** @type {Item[]} */
  const items = [];
  if (Array.isArray(v.items)) {
    v.items.forEach((/** @type {unknown} */ it, j) => {
      const item = normalizeItem(it, v.name);
      if (item) items.push(item);
      else warnings.push(`${where} item ${j} skipped`);
    });
  }
  const alive = v.runner_alive === true;
  const pid = Number.isInteger(v.runner_pid) && v.runner_pid > 0 ? v.runner_pid : null;
  return {
    name: v.name, dataDir: v.data_dir, error: strOrNull(v.error), counts: normalizeCounts(v.counts),
    runnerAlive: alive, runnerPid: alive ? pid : null, nextWaitUntil: strOrNull(v.next_wait_until), items,
    // (2026-10-20, 0.4.4 logfix D) the live runner's core version; "" = an older core (no field)
    runnerVersion: alive ? (typeof v.runner_version === 'string' ? v.runner_version : '') : null,
    // (2026-10-21, v049 dontask_hold) null = not held (or a core without holds)
    hold: parseHold(v.hold),
    // (2026-10-26, usage forecast) the account's usage samples (first row only); lib/usage.js checks them
    usage: isObj(v.usage) ? v.usage : null,
    // (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.9/8.5) the queue's live "tasks at once" (null = not set:
    // one after another) and its runner's heartbeat (null = no parallel runner, and an older core)
    tasksAtOnce: Number.isInteger(v.tasks_at_once) && v.tasks_at_once >= 1 && v.tasks_at_once <= TASKS_AT_ONCE_MAX ? v.tasks_at_once : null,
    parallel: normalizeParallel(v.parallel),
  };
}

/**
 * (2026-10-27, tasks at once, SPEC_V05_PARALLEL 1.9) A lane's parallel.json as `lanes --json` gives it (only while
 * its runner is alive): n, nEff, mode (copies | in-place), beat, the running slots, the merges, why fewer run
 * ({code, args} | null) and the in-place reason. Anything malformed = null.
 * (2026-10-29, integrate round 8) + a slot's `note`: why that task runs alone (scheduler.py, e.g. "could not make a
 * copy: cwd-outside"); absent = none.
 * @param {unknown} v
 * @returns {{n: number, nEff: number, mode: string, beat: string | null,
 *   slots: {item: string, phase: string, mode: string, note?: string}[],
 *   merging: {item: string, state: string}[], why: {code: string, args: Record<string, string>} | null, reason: string | null} | null}
 */
function normalizeParallel(v) {
  if (!isObj(v) || !Number.isInteger(v.n) || v.n < 1) return null;
  const p = /** @type {Record<string, any>} */ (v);
  const rows = (/** @type {unknown} */ list) => (Array.isArray(list) ? list.filter((r) => isObj(r) && typeof r.item === 'string' && r.item) : []);
  return {
    n: p.n,
    nEff: Number.isInteger(p.n_eff) && p.n_eff >= 0 ? p.n_eff : p.n,
    mode: p.mode === 'in-place' ? 'in-place' : 'copies',
    beat: strOrNull(p.beat),
    slots: rows(p.slots).map((r) => ({ item: r.item, phase: typeof r.phase === 'string' ? r.phase : 'attempt',
      mode: typeof r.mode === 'string' ? r.mode : 'copy', ...(typeof r.note === 'string' && r.note.trim() ? { note: r.note } : {}) })),
    merging: rows(p.merging).map((r) => ({ item: r.item, state: typeof r.state === 'string' ? r.state : 'merging' })),
    why: normalizeWaits(p.why),
    reason: strOrNull(p.reason),
  };
}

/**
 * (2026-10-27, tasks at once, SPEC_V05_PARALLEL 3.6, U3) `merge ID --<action> --json`: {id, action, ok, state,
 * code, merge}. Exit 1 (refused) prints the same object with ok false.
 * (2026-10-29, integrate round 7) U3's extra keys are kept too: merge (the item's merge record), removed (the copy
 * folder was deleted), kept (a discard moved the copy aside: where the work is), reply (a let-claude answer's
 * queued reply {status, seq, replies}).
 * (2026-10-29, integrate round 8) + `later`: a refused undo's later merges [{id, at, paths}], newest first.
 * @param {string} stdout
 * @returns {{ok: true, value: {id: string, action: string, ok: boolean, state: string | null, code: string | null,
 *   detail: string | null, paths: string[], patch: string | null, merge: ReturnType<typeof normalizeMerge>,
 *   removed: boolean | null, kept: string | null,
 *   reply: {status: string | null, seq: number | null, replies: number | null} | null,
 *   later: {id: string, at: string | null, paths: string[]}[]}} | {ok: false, error: string}}
 */
function parseMerge(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.ok !== 'boolean') return { ok: false, error: 'merge --json: expected {ok, state, code}' };
  const rp = isObj(v.reply) ? /** @type {Record<string, any>} */ (v.reply) : null;
  return { ok: true, value: { id: typeof v.id === 'string' ? v.id : '', action: MERGE_ACTIONS.includes(v.action) ? v.action : '',
    ok: v.ok, state: strOrNull(v.state), code: strOrNull(v.code), detail: strOrNull(v.detail),
    paths: strList(v.paths, MERGE_LIST_MAX), patch: strOrNull(v.patch), merge: normalizeMerge(v.merge),
    removed: typeof v.removed === 'boolean' ? v.removed : null, kept: strOrNull(v.kept),
    reply: rp && { status: strOrNull(rp.status), seq: numOrNull(rp.seq), replies: numOrNull(rp.replies) },
    later: (Array.isArray(v.later) ? v.later : []).filter((x) => isObj(x) && typeof x.id === 'string' && x.id)
      .slice(0, MERGE_LIST_MAX).map((x) => ({ id: x.id, at: strOrNull(x.at), paths: strList(x.paths, MERGE_LIST_MAX) })) } };
}

/**
 * (2026-10-27, tasks at once, U3) `tasks-at-once N --json`: {lane, tasks_at_once, runner_alive, applies}.
 * @param {string} stdout
 * @returns {{ok: true, value: {lane: string, n: number, runnerAlive: boolean, applies: string}} | {ok: false, error: string}}
 */
function parseTasksAtOnce(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || !Number.isInteger(v.tasks_at_once)) return { ok: false, error: 'tasks-at-once --json: expected {tasks_at_once}' };
  return { ok: true, value: { lane: typeof v.lane === 'string' ? v.lane : DEFAULT_LANE, n: v.tasks_at_once,
    runnerAlive: v.runner_alive === true, applies: v.applies === 'next-pick' ? 'next-pick' : 'next-run' } };
}

/**
 * (2026-10-27, tasks at once, U3) `worktrees --json`: the leftover copies ({path, reason}) the Queue view lists
 * (Open folder only; nothing here deletes anything) and the copies of live items.
 * @param {string} stdout
 * @returns {{ok: true, value: {leftovers: {path: string, reason: string}[], copies: {id: string, path: string, state: string}[]}} | {ok: false, error: string}}
 */
function parseWorktrees(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v)) return { ok: false, error: 'worktrees --json: expected an object' };
  const rows = (/** @type {unknown} */ list) => (Array.isArray(list) ? list.filter((x) => isObj(x) && typeof x.path === 'string' && x.path) : []);
  return { ok: true, value: {
    leftovers: rows(v.leftovers).map((x) => ({ path: x.path, reason: typeof x.reason === 'string' ? x.reason : '' })),
    copies: rows(v.copies).map((x) => ({ id: typeof x.id === 'string' ? x.id : '', path: x.path, state: typeof x.state === 'string' ? x.state : '' })),
  } };
}

/**
 * (2026-10-20, 0.4.4 logfix A) `shell-allow list|add|remove … --json`: {file, commands}.
 * @param {string} stdout
 * @returns {{ok: true, value: {file: string, commands: string[]}} | {ok: false, error: string}}
 */
function parseShellAllow(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.file !== 'string' || !Array.isArray(v.commands)) {
    return { ok: false, error: 'shell-allow --json: expected {file, commands}' };
  }
  return { ok: true, value: { file: v.file, commands: v.commands.filter((/** @type {unknown} */ c) => typeof c === 'string') } };
}

/**
 * (2026-10-21, SPEC_V048_REPORT) `report --json`: the redacted draft and its prefilled new-issue link.
 * Anything but a github.com link is refused, so the extension never opens another address.
 * @param {string} stdout
 * @returns {{ok: true, value: {file: string, url: string, title: string, redactions: number, logLines: number, urlLogLines: number}} | {ok: false, error: string}}
 */
function parseReport(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.file !== 'string' || !v.file || typeof v.url !== 'string' || !v.url.startsWith('https://github.com/')) {
    return { ok: false, error: 'report --json: expected {file, url} with a https://github.com/ link' };
  }
  const num = (/** @type {unknown} */ n) => (typeof n === 'number' && Number.isFinite(n) ? n : 0);
  return {
    ok: true,
    value: {
      file: v.file, url: v.url, title: typeof v.title === 'string' ? v.title : '',
      redactions: num(v.redactions), logLines: num(v.log_lines), urlLogLines: num(v.url_log_lines),
    },
  };
}

/**
 * @typedef {{name: string, label: string, needsKey: boolean, keySet: boolean, ready: boolean,
 *   missing: string[], supportsEffort: boolean, models: string[], model: string | null}} ProviderInfo
 * @typedef {{selected: string, providers: ProviderInfo[]}} Providers
 */

const PROVIDER_NAME_RE = /^[a-z][a-z0-9-]{0,31}$/;          // == argv.PROVIDER_RE
const ENV_VAR_RE = /^[A-Z_][A-Z0-9_]{0,63}$/;
const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/[\]-]{0,99}$/; // == argv.MODEL_RE
const PROVIDER_LABEL_MAX = 200;

/**
 * (2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) `providers --json`. Keeps ONLY what the "+ Add
 * new task" panel shows: names, labels, whether the key is present (a boolean), readiness, the names of
 * missing variables, effort support and model ids. The env file path, the endpoints and anything else
 * the core prints are dropped here, so they never reach a webview. The core never prints a key value.
 * @param {string} stdout
 * @returns {{ok: true, value: Providers} | {ok: false, error: string}}
 */
function parseProviders(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || !Array.isArray(v.providers)) return { ok: false, error: 'providers --json: expected {providers: [...]}' };
  /** @type {ProviderInfo[]} */
  const rows = [];
  /** @param {unknown} m */
  const modelId = (m) => typeof m === 'string' && MODEL_ID_RE.test(m);
  for (const p of v.providers) {
    if (!isObj(p) || typeof p.name !== 'string' || !PROVIDER_NAME_RE.test(p.name)) continue;
    if (rows.some((x) => x.name === p.name)) continue;
    rows.push({
      name: p.name,
      label: typeof p.label === 'string' && p.label.trim() ? p.label.trim().slice(0, PROVIDER_LABEL_MAX) : p.name,
      needsKey: typeof p.key_var === 'string' && p.key_var !== '',
      keySet: p.key_set === true,
      ready: p.ready === true,
      missing: Array.isArray(p.missing) ? p.missing.filter((/** @type {unknown} */ m) => typeof m === 'string' && ENV_VAR_RE.test(m)) : [],
      supportsEffort: p.supports_effort !== false,
      models: Array.isArray(p.models) ? p.models.filter(modelId) : [],
      model: modelId(p.model) ? p.model : null,
    });
  }
  if (!rows.length) return { ok: false, error: 'providers --json: no provider' };
  const selected = typeof v.selected === 'string' && rows.some((x) => x.name === v.selected) ? v.selected : rows[0].name;
  return { ok: true, value: { selected, providers: rows } };
}

/**
 * (2026-10-20, 0.4.4 logfix F) The queues autoStartRunner starts: those with a runnable item
 * (pending or waiting) and no live runner. An idle project starts nothing.
 * @param {Lane[]} lanes
 * @returns {string[]}
 */
function autoStartLanes(lanes) {
  // (2026-10-21, v049 dontask_hold) a held queue is never auto-started
  // (2026-10-26, usage forecast) ...but a timed hold is: its runner waits until the hold ends
  return (lanes || []).filter((l) => !l.runnerAlive && !(l.hold && !l.hold.until)
    && ((l.counts.pending || 0) > 0 || (l.counts.waiting || 0) > 0))
    .map((l) => l.name);
}

/**
 * (2026-10-20, 0.4.4 logfix D) The live runners whose core version is not `bundled` (the version this
 * window would start). A runner without the field (an older core) counts as unknown/old.
 * @param {Lane[]} lanes @param {string} bundled
 * @returns {{name: string, dataDir: string, pid: number | null, version: string}[]}
 */
function staleRunners(lanes, bundled) {
  return (lanes || []).filter((l) => l.runnerAlive && (l.runnerVersion || '') !== bundled)
    .map((l) => ({ name: l.name, dataDir: l.dataDir, pid: l.runnerPid, version: l.runnerVersion || '' }));
}

/**
 * The one warning for `stale` runners (null = none), with "Reload Window" as its action.
 * @param {{name: string, version: string}[]} stale @param {string} bundled
 * @returns {string | null}
 */
function staleRunnerWarning(stale, bundled) {
  if (!stale.length) return null;
  // (2026-10-27, i18n fix) every piece through t(): the queue label, the core version and the sentence
  const what = stale.map((r) => t('{0} (core {1})', r.name === DEFAULT_LANE ? t('the main queue') : t('queue {0}', r.name),
    r.version || t('unknown, older than 0.4.4'))).join(', ');
  return t("Sleeping Clanker: the runner for {0} is not this window's core {1}. "
    + 'Reload the window that started it (or stop it and start it again) to use the new core.', what, bundled);
}

/**
 * `lanes --json [--items]`: `default` first (the core's order is kept).
 * @param {string} stdout
 * @returns {{ok: true, value: Lane[], warnings: string[]} | {ok: false, error: string}}
 */
function parseLanes(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  if (!Array.isArray(r.value)) return { ok: false, error: 'lanes --json: expected an array' };
  /** @type {string[]} */
  const warnings = [];
  /** @type {Lane[]} */
  const lanes = [];
  r.value.forEach((v, i) => {
    const lane = normalizeLane(v, `lanes --json: entry ${i}`, warnings);
    if (lane) lanes.push(lane);
  });
  if (!lanes.length) return { ok: false, error: 'lanes --json: no queues listed' };
  return { ok: true, value: lanes, warnings };
}

/** @param {string | null} w */
const waitMs = (w) => (w ? new Date(w).getTime() : NaN);

/**
 * The project-wide Status from its lanes: counts summed, runner alive when any lane's is, the first
 * live pid, the earliest wait; `dataDir` is the DEFAULT lane's (= the project dir).
 * @param {Lane[]} lanes
 * @returns {Status}
 */
function sumLanes(lanes) {
  /** @type {Record<string, number>} */
  const counts = {};
  for (const st of STATUSES) counts[st] = lanes.reduce((n, l) => n + (l.counts[st] || 0), 0);
  const alive = lanes.filter((l) => l.runnerAlive);
  const waits = lanes.map((l) => l.nextWaitUntil).filter((w) => !Number.isNaN(waitMs(w)))
    .sort((a, b) => waitMs(a) - waitMs(b));
  const main = lanes.find((l) => l.name === DEFAULT_LANE) || lanes[0];
  return {
    counts, runnerAlive: alive.length > 0, runnerPid: alive.length ? alive[0].runnerPid : null,
    nextWaitUntil: waits.length ? waits[0] : null, dataDir: main ? main.dataDir : '',
    // (2026-10-21, v049 dontask_hold) the main queue's hold, like dataDir
    hold: main && main.hold ? main.hold : null,
  };
}

/**
 * `projects --json [--items]` (amendments M10/M11). Rows without a data_dir are skipped with a warning.
 * @param {string} stdout
 * @returns {{ok: true, value: Project[], warnings: string[]} | {ok: false, error: string}}
 */
function parseProjects(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  if (!Array.isArray(r.value)) return { ok: false, error: 'projects --json: expected an array' };
  /** @type {Project[]} */
  const rows = [];
  /** @type {string[]} */
  const warnings = [];
  r.value.forEach((v, i) => {
    if (!isObj(v) || typeof v.data_dir !== 'string' || !v.data_dir) {
      warnings.push(`projects --json: entry ${i} skipped (no data_dir)`);
      return;
    }
    /** @type {Lane[]} */
    const lanes = [];
    if (Array.isArray(v.lanes)) {
      v.lanes.forEach((/** @type {unknown} */ l, j) => {
        const lane = normalizeLane(l, `projects --json: entry ${i} queue ${j}`, warnings);
        if (lane) lanes.push(lane);
      });
    }
    /** @type {Item[]} */
    const items = [];
    if (lanes.length) items.push(...lanes.flatMap((l) => l.items)); // (2026-10-15) items carry their lane
    else if (Array.isArray(v.items)) {
      v.items.forEach((/** @type {unknown} */ it, j) => {
        const item = normalizeItem(it);
        if (item) items.push(item);
        else warnings.push(`projects --json: entry ${i} item ${j} skipped`);
      });
    }
    const alive = v.runner_alive === true;
    const pid = Number.isInteger(v.runner_pid) && v.runner_pid > 0 ? v.runner_pid : null;
    const root = strOrNull(v.root);
    rows.push({
      name: typeof v.name === 'string' && v.name ? v.name : (root || v.data_dir),
      dataDir: v.data_dir,
      root,
      shared: v.shared === true,
      rootExists: v.root_exists === true,
      error: strOrNull(v.error),
      counts: normalizeCounts(v.counts),
      runnerAlive: alive,
      runnerPid: alive ? pid : null,
      nextWaitUntil: strOrNull(v.next_wait_until),
      items,
      lanes: lanes.length ? lanes : [{
        // (2026-10-15) a row without `lanes` (an older core) is its own default lane
        name: DEFAULT_LANE, dataDir: v.data_dir, error: strOrNull(v.error), counts: normalizeCounts(v.counts),
        runnerAlive: alive, runnerPid: alive ? pid : null, nextWaitUntil: strOrNull(v.next_wait_until), items,
      }],
    });
  });
  return { ok: true, value: rows, warnings };
}

/**
 * `migrate-global --json [--dry-run] [--for ROOT]` summary (amendments M7/M9).
 * @param {string} stdout
 * @returns {{ok: true, value: {moved: number, wouldMove: number, forRoot: number | null,
 *   runnerAlive: boolean, backup: string | null, dryRun: boolean}} | {ok: false, error: string}}
 */
function parseMigrate(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || !Number.isInteger(v.moved) || !Number.isInteger(v.would_move)) {
    return { ok: false, error: 'migrate-global --json: expected {"moved", "would_move", ...}' };
  }
  return {
    ok: true,
    value: {
      moved: v.moved,
      wouldMove: v.would_move,
      forRoot: Number.isInteger(v.for_root) ? v.for_root : null,
      runnerAlive: v.runner_alive === true,
      backup: strOrNull(v.backup),
      dryRun: v.dry_run === true,
    },
  };
}

/**
 * `lane-remove NAME --json` -> {removed, backup}; (2026-10-29, integrate round 7: U3) + patches (the saved
 * patches of its tasks' copies) and leftovers (the copy folders kept), only when the queue had copies.
 * @param {string} stdout
 * @returns {{ok: true, value: {removed: string, backup: string | null, patches: string[], leftovers: string[]}} | {ok: false, error: string}}
 */
function parseLaneRemove(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  if (!isObj(r.value) || typeof r.value.removed !== 'string') return { ok: false, error: 'lane-remove --json: expected {"removed", "backup"}' };
  return { ok: true, value: { removed: r.value.removed, backup: strOrNull(r.value.backup),
    patches: strList(r.value.patches, MERGE_LIST_MAX), leftovers: strList(r.value.leftovers, MERGE_LIST_MAX) } };
}

/**
 * `reply ID --json` -> {id, seq, replies}; (2026-10-29, integrate round 7: U3) a reply to a merge question also
 * has {action, ok, state, code} (the merge answer it became), kept only when present.
 * @param {string} stdout
 * @returns {{ok: true, value: {id: string, seq: number | null, replies: number, action?: string, ok?: boolean,
 *   state?: string | null, code?: string | null}} | {ok: false, error: string}}
 */
function parseReply(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.id !== 'string' || !v.id) return { ok: false, error: 'reply --json: expected {"id", ...}' };
  const merge = MERGE_ACTIONS.includes(v.action) && typeof v.ok === 'boolean'
    ? { action: v.action, ok: v.ok, state: strOrNull(v.state), code: strOrNull(v.code) } : {};
  return { ok: true, value: { id: v.id, seq: numOrNull(v.seq), replies: numOrNull(v.replies) ?? 0, ...merge } };
}

/**
 * (2026-10-21, edit) `edit ID --json` -> {id, changed}.
 * @param {string} stdout
 * @returns {{ok: true, value: {id: string, changed: string[]}} | {ok: false, error: string}}
 */
function parseEdit(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.id !== 'string' || !v.id) return { ok: false, error: 'edit --json: expected {"id", "changed"}' };
  const changed = Array.isArray(v.changed) ? v.changed.filter((c) => typeof c === 'string') : [];
  return { ok: true, value: { id: v.id, changed } };
}

/**
 * (2026-10-21, start now) `first ID --json` -> {id, moved, ahead}: `ahead` = the pending items that
 * still run before it (0 = it is next).
 * @param {string} stdout
 * @returns {{ok: true, value: {id: string, moved: boolean, ahead: number}} | {ok: false, error: string}}
 */
function parseFirst(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = r.value;
  if (!isObj(v) || typeof v.id !== 'string' || !v.id) return { ok: false, error: 'first --json: expected {"id", "moved", "ahead"}' };
  return { ok: true, value: { id: v.id, moved: v.moved === true, ahead: numOrNull(v.ahead) ?? 0 } };
}

/**
 * (2026-10-19, 0.4.1 D6) True when a cli error says the item is gone (store.py `find`:
 * "no item with id 'x'"): a view of a deleted item stops asking for it.
 * @param {unknown} err
 */
function isItemNotFound(err) {
  return /\bno item with id\b/i.test(err instanceof Error ? err.message : String(err));
}

module.exports = {
  QUESTION_LABEL_MAX, isItemNotFound, normalizeQuestion,
  STATUSES, DEFAULT_LANE, parseJsonOutput, parseStatus, parseList, parseShow, parseAdd, normalizeItem,
  parseWhere, parseProjects, parseMigrate, parseLanes, sumLanes, parseLaneRemove, parseReply,
  parseEdit, parseFirst, autoStartLanes, staleRunners, staleRunnerWarning, parseShellAllow, parseReport,
  parseProviders, parseHold, parseHoldResult, normalizeDenialBlock,
  // (2026-10-27, tasks at once) normalizeItem / normalizeLane are the spec's parseItem / parseLane
  normalizeLane, normalizeWorktree, normalizeMerge, normalizeWaits, normalizeParallel, parseMerge, parseTasksAtOnce, parseWorktrees,
};

// @ts-check
// (2026-10-26, owner task f60cec52 "counter on top how many task done today and pending; if clicked
// show stats") The Queue view's Today counter and the statistics document, computed from the task
// items the last poll already has: no CLI call of its own. Pure: no vscode API.
// Times are the core's UTC ISO stamps. "Today" is the LOCAL calendar day of `now` (from local
// midnight to the next one); "the last 7 days" is today plus the 6 local days before it.
// A finished task belongs to the day of its finish: `finished_at`, else the end of its last attempt
// (`last_ended_at`), else `created_at` (as the Completed-by-date groups in lib/view.js).
// Bias (the core keeps no runner history): `started_at` is the start of the LAST attempt, so "start to
// finish" is the last attempt's run and "wait in the queue" (created_at -> started_at) includes any
// earlier attempts; "active hours" are the union of the tasks' run intervals over all queues (a runner
// that is alive but idle does not count, so "done per hour" is per hour of work).
'use strict';

const format = require('./format');
const { t } = require('./i18n');

const HOUR_MS = 3600000;
const WEEK_DAYS = 7;
const FINISHED = Object.freeze(['done', 'failed', 'cancelled']);
const LIVE = Object.freeze(['running', 'waiting']); // a task that runs, or waits for a usage-limit reset
const MIN_ACTIVE_MS = 60000; // under a minute of work: no "per hour" figure (one task would read as 60/h)
const DASH = '—';

/**
 * @typedef {import('./parse').Item} Item
 * @typedef {{done: number, failed: number, pending: number, running: number, blocked: number}} Counter
 * @typedef {{lane: string, done: number, failed: number, cancelled: number}} LaneRow
 * @typedef {{from: number, until: number, done: number, failed: number, cancelled: number, finished: number,
 *   successRate: number | null, activeMs: number, perHour: number | null, avgRunMs: number | null,
 *   medianRunMs: number | null, avgWaitMs: number | null, lanes: LaneRow[],
 *   cost: {usd: number, tasks: number} | null}} WindowStats
 */

/** An ISO stamp as epoch ms, or null when it is not a date. @param {unknown} v */
function stampMs(v) {
  if (typeof v !== 'string' || !v) return null;
  const ms = Date.parse(v);
  return Number.isFinite(ms) ? ms : null;
}

/** When a finished task finished (see the header), or null. @param {Item} it */
function endMs(it) {
  const raw = it.raw || {};
  for (const v of [it.finished_at, raw.last_ended_at, it.created_at]) {
    const ms = stampMs(v);
    if (ms !== null) return ms;
  }
  return null;
}

/** Local midnight `back` days before today's (negative = after). @param {Date} now @param {number} [back] */
function dayStartMs(now, back = 0) {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate() - back).getTime();
}

/**
 * The counter on top of the Queue view: done and failed TODAY (local day), pending, running (or
 * waiting for a limit reset) and blocked (needs you) NOW.
 * @param {Item[]} items @param {Date} now
 * @returns {Counter}
 */
function counter(items, now) {
  const from = dayStartMs(now);
  const until = dayStartMs(now, -1);
  const c = { done: 0, failed: 0, pending: 0, running: 0, blocked: 0 };
  for (const it of items || []) {
    const s = it.status;
    if (s === 'pending') c.pending += 1;
    else if (LIVE.includes(s)) c.running += 1;
    else if (s === 'blocked') c.blocked += 1;
    else if (s === 'done' || s === 'failed') {
      const end = endMs(it);
      if (end !== null && end >= from && end < until) {
        if (s === 'done') c.done += 1;
        else c.failed += 1;
      }
    }
  }
  return c;
}

/** "Today: 5 done · 3 pending", plus failed, running and need-you when non-zero. @param {Counter} c */
function counterLabel(c) {
  const parts = [t('Today: {0} done', c.done)];
  if (c.failed > 0) parts.push(t('{0} failed', c.failed));
  parts.push(t('{0} pending', c.pending));
  if (c.running > 0) parts.push(t('{0} running', c.running));
  if (c.blocked > 0) parts.push(t('{0} need you', c.blocked));
  return parts.join(' · ');
}

/** The statistics' "Now:" line: pending, running and need-you (the counter's current part). @param {Counter} c */
function nowLabel(c) {
  const parts = [t('{0} pending', c.pending)];
  if (c.running > 0) parts.push(t('{0} running', c.running));
  if (c.blocked > 0) parts.push(t('{0} need you', c.blocked));
  return parts.join(' · ');
}

/** The end of a task's run for the active time, or null. @param {Item} it @param {number} start @param {number} nowMs */
function runEndMs(it, start, nowMs) {
  if (LIVE.includes(it.status)) return nowMs;
  const raw = it.raw || {};
  for (const v of [it.finished_at, raw.last_ended_at]) {
    const ms = stampMs(v);
    if (ms !== null && ms >= start) return ms;
  }
  return null;
}

/** The total length of the union of [a, b) intervals. @param {[number, number][]} list */
function unionMs(list) {
  const sorted = list.slice().sort((x, y) => x[0] - y[0]);
  let total = 0;
  let a = -Infinity;
  let b = -Infinity;
  for (const [s, e] of sorted) {
    if (s > b) {
      if (b > a) total += b - a;
      a = s;
      b = e;
    } else if (e > b) b = e;
  }
  if (b > a) total += b - a;
  return total;
}

/** @param {number[]} xs */
const average = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null);

/** @param {number[]} xs */
function median(xs) {
  if (!xs.length) return null;
  const s = xs.slice().sort((x, y) => x - y);
  const mid = Math.floor(s.length / 2);
  return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
}

/** `default` first, then by name (case-insensitive). @param {string} a @param {string} b */
function laneOrder(a, b) {
  const da = a.toLowerCase() === 'default';
  const db = b.toLowerCase() === 'default';
  if (da !== db) return da ? -1 : 1;
  return a.toLowerCase().localeCompare(b.toLowerCase()) || a.localeCompare(b);
}

/**
 * The numbers of one window [from, until), with `nowMs` closing the run of a task that still runs.
 * @param {Item[]} items @param {number} from @param {number} until @param {number} nowMs
 * @returns {WindowStats}
 */
function windowStats(items, from, until, nowMs) {
  let done = 0;
  let failed = 0;
  let cancelled = 0;
  /** @type {number[]} */
  const runs = [];
  /** @type {number[]} */
  const waits = [];
  /** @type {[number, number][]} */
  const active = [];
  /** @type {Map<string, LaneRow>} */
  const lanes = new Map();
  let usd = 0;
  let paid = 0;
  for (const it of items || []) {
    const start = stampMs(it.started_at);
    const created = stampMs(it.created_at);
    if (FINISHED.includes(it.status)) {
      const end = endMs(it);
      if (end !== null && end >= from && end < until) {
        const lane = it.lane || 'default';
        const row = lanes.get(lane) || { lane, done: 0, failed: 0, cancelled: 0 };
        if (it.status === 'done') { done += 1; row.done += 1; }
        else if (it.status === 'failed') { failed += 1; row.failed += 1; }
        else { cancelled += 1; row.cancelled += 1; }
        lanes.set(lane, row);
        if (it.status === 'done' && start !== null && end >= start) runs.push(end - start);
        // a cost only where the item has one billed through an API key (as format.itemExplanation)
        const cost = it.billing === 'api' ? (it.total_cost_usd != null ? it.total_cost_usd : it.cost_usd) : null;
        if (typeof cost === 'number' && Number.isFinite(cost)) { usd += cost; paid += 1; }
      }
    }
    if (start !== null && created !== null && start >= created && start >= from && start < until) waits.push(start - created);
    const runEnd = start === null ? null : runEndMs(it, start, nowMs);
    if (start !== null && runEnd !== null) {
      const a = Math.max(start, from);
      const b = Math.min(runEnd, until, nowMs);
      if (b > a) active.push([a, b]);
    }
  }
  const finished = done + failed + cancelled;
  const activeMs = unionMs(active);
  return {
    from,
    until,
    done,
    failed,
    cancelled,
    finished,
    successRate: finished ? done / finished : null,
    activeMs,
    perHour: activeMs >= MIN_ACTIVE_MS ? done / (activeMs / HOUR_MS) : null,
    avgRunMs: average(runs),
    medianRunMs: median(runs),
    avgWaitMs: average(waits),
    lanes: [...lanes.values()].sort((x, y) => laneOrder(x.lane, y.lane)),
    cost: paid ? { usd, tasks: paid } : null,
  };
}

/**
 * Today and the last 7 days, plus the counter.
 * @param {Item[]} items @param {Date} now
 * @returns {{now: Counter, today: WindowStats, week: WindowStats}}
 */
function compute(items, now) {
  const nowMs = now.getTime();
  const until = dayStartMs(now, -1);
  return {
    now: counter(items, now),
    today: windowStats(items, dayStartMs(now), until, nowMs),
    week: windowStats(items, dayStartMs(now, WEEK_DAYS - 1), until, nowMs),
  };
}

// ---- the statistics document --------------------------------------------------------------------
/** @param {number} n */
const pad2 = (n) => String(n).padStart(2, '0');
/** Local YYYY-MM-DD. @param {number} ms */
const ymd = (ms) => { const d = new Date(ms); return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`; };
/** @param {number | null} ms */
const duration = (ms) => (ms === null ? DASH : format.formatElapsed(ms));
/** @param {number} v */
const money = (v) => `$${v.toFixed(v < 1 ? 3 : 2)}`;

/** @param {LaneRow | undefined} row */
function laneCell(row) {
  if (!row) return DASH;
  const parts = [t('{0} done', row.done)];
  if (row.failed) parts.push(t('{0} failed', row.failed));
  if (row.cancelled) parts.push(t('{0} cancelled', row.cancelled));
  return parts.join(' · ');
}

/** @param {WindowStats} w */
function cells(w) {
  return {
    done: String(w.done),
    perHour: w.perHour === null ? DASH : t('{0} ({1} active)', w.perHour.toFixed(1), format.formatElapsed(w.activeMs)),
    avg: duration(w.avgRunMs),
    median: duration(w.medianRunMs),
    wait: duration(w.avgWaitMs),
    success: w.successRate === null ? DASH
      : t('{0}% ({1} of {2})', Math.round(w.successRate * 100), w.done, w.finished),
    cost: w.cost ? t(w.cost.tasks === 1 ? '{0} ({1} task)' : '{0} ({1} tasks)', money(w.cost.usd), w.cost.tasks) : DASH,
  };
}

/** A markdown table with padded columns, so the plain text reads as a table too. @param {string[][]} rows */
function table(rows) {
  const widths = rows[0].map((_, i) => Math.max(3, ...rows.map((r) => r[i].length)));
  const line = (/** @type {string[]} */ r) => `| ${r.map((c, i) => c.padEnd(widths[i])).join(' | ')} |`;
  return [line(rows[0]), `|${widths.map((w) => '-'.repeat(w + 2)).join('|')}|`, ...rows.slice(1).map(line)].join('\n');
}

/**
 * The read-only statistics document (markdown) of this window's project.
 * @param {Item[]} items @param {{now: Date, project?: string | null}} opts
 */
function report(items, opts) {
  const now = opts.now;
  const s = compute(items, now);
  const today = cells(s.today);
  const week = cells(s.week);
  const head = ['', t('Today'), t('Last 7 days')];
  const rows = [
    head,
    [t('Tasks done'), today.done, week.done],
    [t('Done per hour while the runner was active'), today.perHour, week.perHour],
    [t('Start to finish, average'), today.avg, week.avg],
    [t('Start to finish, median'), today.median, week.median],
    [t('Wait in the queue, average'), today.wait, week.wait],
    [t('Success rate'), today.success, week.success],
  ];
  if (s.today.cost || s.week.cost) rows.push([t('Cost (API billing)'), today.cost, week.cost]);
  const names = [...new Set([...s.week.lanes, ...s.today.lanes].map((r) => r.lane))].sort(laneOrder);
  const out = [
    `# ${t('Sleeping Clanker statistics')}${opts.project ? ` — ${opts.project}` : ''}`,
    '',
    t('Updated {0}. Today = {1} (local day); last 7 days = {2} to {1}.',
      `${pad2(now.getHours())}:${pad2(now.getMinutes())}`, ymd(s.today.from), ymd(s.week.from)),
    '',
    `${t('Now:')} ${nowLabel(s.now)}`,
    '',
    table(rows),
    '',
    `## ${t('Tasks per queue')}`,
    '',
  ];
  if (names.length) {
    out.push(table([[t('Queue'), t('Today'), t('Last 7 days')],
      ...names.map((n) => [format.laneLabel(n), laneCell(s.today.lanes.find((r) => r.lane === n)),
        laneCell(s.week.lanes.find((r) => r.lane === n))])]));
  } else out.push(t('No finished tasks in the last 7 days.'));
  out.push(
    '',
    `## ${t('How these are counted')}`,
    '',
    `- ${t('A finished task counts on the local day it finished.')}`,
    `- ${t('Done per hour: tasks done divided by the hours any task of this project was running or waiting for a limit reset (all queues together).')}`,
    `- ${t('Start to finish: the last attempt of each done task. Wait in the queue: from adding a task to its last start.')}`,
    `- ${t('Success rate: done out of done, failed and cancelled.')}`,
    `- ${t('Cost: only tasks billed through an API key; a subscription has no cost per task.')}`,
    `- ${t('Only the tasks still in the queue count: a removed task is gone from these numbers.')}`,
    `- ${t('This page is read-only and updates with the Queue view.')}`,
    '',
  );
  return out.join('\n');
}

module.exports = {
  HOUR_MS,
  MIN_ACTIVE_MS,
  stampMs,
  endMs,
  dayStartMs,
  counter,
  counterLabel,
  unionMs,
  median,
  windowStats,
  compute,
  report,
};

// @ts-check
// (2026-10-26, usage forecast) How long the account's usage limits last at the current rate. Pure: no
// vscode API, no network, no file access. The core records one sample per usage window from every
// rate_limit_event its runs stream (src/sleeping_clanker/usage.py) and `lanes --json` puts them on its
// first row as `usage`: {latest: {window: sample}, samples: [sample, ...]}, a sample being
// {t: epoch s, window: "five_hour" | "seven_day" | ..., utilization: 0..1, resets_at: epoch s | null,
// status: string | null}. From these this module computes, per window:
// - the burn rate: utilization per hour of ACTIVE time (only samples of the latest window instance count,
//   so a reset never reads as negative use). (2026-10-27, fix LOW) Active = the part of a sample pair a
//   known item ran in (runIntervals); a pair no known item ran in, with neither sample inside a run of
//   this project (another project's runs), keeps the old rule: up to GAP_S apart = active;
// - the time to 100% at that rate, and whether that comes before the window resets;
// - how many queued tasks fit: (1 - utilization) / the mean utilization a finished task used (null =
//   unknown with fewer than MIN_TASKS such tasks). (2026-10-27, fix MED-3) Each sample pair's increase is
//   split among the items that ran in it, by the time each ran in it: 10 tasks at once each get a tenth.
//   (2026-10-27, fix round 2) The running tasks' remaining use (half a task each) comes off first.
// Data older than STALE_MS is shown as stale, with no forecast (but a stale reading at WARN_UTILIZATION
// of a window that has not reset still warns: it is a lower bound). warnDecision() is the send-path check.
// (2026-10-27, fix round 2) Not stale while an item that started before the newest sample still runs.
'use strict';

const { t } = require('./i18n');
const { localTime } = require('./format');

const STALE_MS = 30 * 60 * 1000;   // the latest sample older than this: stale, no forecast
const GAP_S = 30 * 60;             // two samples further apart than this: the time between was idle
const RATE_SPAN_S = 60 * 60;       // the rate looks back over at most this much active time
const MIN_SPAN_S = 5 * 60;         // less active time than this: no rate
const TASKS_LOOKBACK = 10;         // the last N finished tasks give the per-task use and duration
const MIN_TASKS = 2;               // fewer: 'unknown'
const MIN_COVER = 0.5;             // (2026-10-27) a task counts when sample pairs cover this much of its run
const DEFAULT_TASK_MIN = 15;       // a typical task's minutes while no finished task says otherwise
const WARN_UTILIZATION = 0.95;     // at or above: always warn before sending
const RESET_MARGIN_S = 90;         // "queue for after the reset" holds until the reset + this (core RESET_MARGIN)
const HOLD_MAX_S = 8 * 86400;      // the core's `hold on --until` limit
const WINDOW_RE = /^[a-z][a-z0-9_]{0,31}$/;
// (2026-10-27, integrate: review LOW-4) the core's bounds (usage.py EPOCH_MIN/MAX): a reset in 2000..2100,
// a sample time never past 2100 (far inside what a Date holds)
const EPOCH_MIN = 946684800;       // 2000-01-01 UTC
const EPOCH_MAX = 4102444800;      // 2100-01-01 UTC
const ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/;

/**
 * @typedef {{t: number, window: string, utilization: number, resetsAt: number | null, status: string | null}} Sample
 * @typedef {{latest: Record<string, Sample>, samples: Sample[]}} Usage
 * @typedef {{window: string, utilization: number, resetsAt: number | null, status: string | null, ageMs: number,
 *   stale: boolean, reset: boolean, ratePerHour: number | null, minutesLeft: number | null, hitsBeforeReset: boolean}} WindowForecast
 * @typedef {{windows: WindowForecast[], binding: WindowForecast | null, pending: number,
 *   perTask: number | null, typicalMinutes: number, typicalKnown: boolean, fit: number | null}} Forecast
 */

/** @param {unknown} v @returns {number | null} */
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

/** @param {unknown} v @returns {Sample | null} */
function sample(v) {
  if (!v || typeof v !== 'object') return null;
  const o = /** @type {Record<string, unknown>} */ (v);
  const tt = num(o.t);
  const u = num(o.utilization);
  if (tt === null || u === null || u < 0 || typeof o.window !== 'string' || !WINDOW_RE.test(o.window)) return null;
  // (2026-10-27, integrate: review LOW-4) as the core: no time past 2100, a reset in 2000..2100 or none
  if (tt < 0 || tt > EPOCH_MAX) return null;
  const r = num(o.resets_at);
  return { t: tt, window: o.window, utilization: u,
    resetsAt: r !== null && r >= EPOCH_MIN && r <= EPOCH_MAX ? r : null,
    status: typeof o.status === 'string' ? o.status : null };
}

/**
 * The core's `usage` field, checked; null = no samples (or a core without the field).
 * @param {unknown} raw @returns {Usage | null}
 */
function normalize(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const o = /** @type {Record<string, unknown>} */ (raw);
  /** @type {Record<string, Sample>} */
  const latest = {};
  if (o.latest && typeof o.latest === 'object') {
    for (const v of Object.values(/** @type {Record<string, unknown>} */ (o.latest))) {
      const s = sample(v);
      if (s) latest[s.window] = s;
    }
  }
  const samples = Array.isArray(o.samples) ? o.samples.map(sample).filter((s) => s !== null) : [];
  samples.sort((a, b) => /** @type {Sample} */ (a).t - /** @type {Sample} */ (b).t);
  for (const s of /** @type {Sample[]} */ (samples)) {
    if (!latest[s.window] || latest[s.window].t < s.t) latest[s.window] = s;
  }
  if (!Object.keys(latest).length) return null;
  return { latest, samples: /** @type {Sample[]} */ (samples) };
}

/**
 * The `usage` of a `lanes --json` poll: the core puts it on the first row only.
 * @param {{usage?: unknown}[] | null | undefined} lanes @returns {Usage | null}
 */
function fromLanes(lanes) {
  for (const l of lanes || []) {
    if (l && l.usage) return normalize(l.usage);
  }
  return null;
}

/**
 * One window's samples of its latest instance (the latest resets_at), oldest first.
 * @param {Usage} usage @param {string} window @returns {Sample[]}
 */
function current(usage, window) {
  const last = usage.latest[window];
  if (!last) return [];
  const rows = usage.samples.filter((s) => s.window === window && s.resetsAt === last.resetsAt && s.t <= last.t);
  if (!rows.length || rows[rows.length - 1].t !== last.t) rows.push(last);
  return rows;
}

/**
 * @typedef {{start: number, end: number, done: boolean, running?: boolean}} Run an item's last attempt, epoch s
 */

/**
 * (2026-10-27, fix MED-3/LOW) The run of every item that ran: its last attempt's started_at to finished_at,
 * a running item up to now.
 * @param {{status?: string, started_at?: string | null, finished_at?: string | null}[]} items
 * @param {number} nowS @returns {Run[]}
 */
function runIntervals(items, nowS) {
  /** @type {Run[]} */
  const out = [];
  for (const it of items || []) {
    const start = it ? epochS(it.started_at) : null;
    if (start === null) continue;
    const end = it.status === 'running' ? nowS : epochS(it.finished_at);
    if (end !== null && end > start) out.push({ start, end, done: it.status === 'done', running: it.status === 'running' });
  }
  return out;
}

/** Seconds [a, b] and [c, d] share. @param {number} a @param {number} b @param {number} c @param {number} d */
const overlap = (a, b, c, d) => Math.max(0, Math.min(b, d) - Math.max(a, c));

/**
 * (2026-10-27) Seconds of [a, b] inside the union of the runs (runs at the same time count once).
 * @param {number} a @param {number} b @param {Run[]} runs
 */
function activeSeconds(a, b, runs) {
  const parts = runs.map((r) => [Math.max(a, r.start), Math.min(b, r.end)]).filter(([x, y]) => y > x)
    .sort((p, q) => p[0] - q[0]);
  let total = 0;
  let edge = a;
  for (const [x, y] of parts) {
    const from = Math.max(x, edge);
    if (y > from) {
      total += y - from;
      edge = y;
    }
  }
  return total;
}

/**
 * Utilization per hour of active time over the newest RATE_SPAN_S of it; null = too little data.
 * (2026-10-27, fix LOW) `runs` (runIntervals): the active time of a pair is the part a known item ran in,
 * so the idle time between two tasks never dilutes the rate. A pair no known item ran in counts whole
 * (up to GAP_S) only when neither sample is inside a known run: then both came from another project's
 * runs. Without `runs`, every pair up to GAP_S apart is active (the 2026-10-26 rule).
 * @param {Sample[]} rows one window instance, oldest first @param {Run[]} [runs] @returns {number | null}
 */
function burnRate(rows, runs) {
  const known = runs || [];
  const inRun = (/** @type {number} */ x) => known.some((r) => x >= r.start && x <= r.end);
  let span = 0;
  let used = 0;
  for (let i = rows.length - 1; i > 0 && span < RATE_SPAN_S; i -= 1) {
    const a = rows[i - 1].t;
    const b = rows[i].t;
    const du = rows[i].utilization - rows[i - 1].utilization;
    if (b <= a || du < 0) continue; // a value that went down
    let dt = known.length ? activeSeconds(a, b, known) : 0;
    if (!dt && b - a <= GAP_S && !inRun(a) && !inRun(b)) dt = b - a;
    if (!dt) continue; // idle
    span += dt;
    used += du;
  }
  return span >= MIN_SPAN_S ? (used / span) * 3600 : null;
}

/**
 * @param {Usage} usage @param {string} window @param {number} nowMs @param {Run[]} [runs] see burnRate
 * @returns {WindowForecast | null}
 */
function windowForecast(usage, window, nowMs, runs) {
  const last = usage.latest[window];
  if (!last) return null;
  const ageMs = nowMs - last.t * 1000;
  const reset = last.resetsAt !== null && last.resetsAt * 1000 <= nowMs;
  // (2026-10-27, fix round 2 LOW) a run of this project live since before the newest sample: a slow burn
  // sends no event until the value moves 1%, so the old sample is read as unchanged up to now, not stale
  const live = !reset && ageMs > STALE_MS && (runs || []).some((r) => r.running && r.start <= last.t);
  const stale = !reset && ageMs > STALE_MS && !live;
  /** @type {WindowForecast} */
  const fc = { window, utilization: last.utilization, resetsAt: last.resetsAt, status: last.status, ageMs, stale, reset,
    ratePerHour: null, minutesLeft: null, hitsBeforeReset: false };
  if (reset || stale) return fc;
  const rows = current(usage, window);
  const rate = burnRate(live ? [...rows, { ...last, t: nowMs / 1000 }] : rows, runs);
  fc.ratePerHour = rate;
  if (last.utilization >= 1) {
    fc.minutesLeft = 0;
  } else if (rate !== null && rate > 0) {
    fc.minutesLeft = ((1 - last.utilization) / rate) * 60;
  }
  if (fc.minutesLeft !== null) {
    // the time left is ACTIVE time: counted from now, as if the tasks ran on from here
    fc.hitsBeforeReset = last.resetsAt === null || nowMs + fc.minutesLeft * 60000 < last.resetsAt * 1000;
  }
  return fc;
}

/** @param {string | null | undefined} iso @returns {number | null} epoch s */
function epochS(iso) {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? null : ms / 1000;
}

/** @param {number[]} xs */
function median(xs) {
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/**
 * What the last finished tasks used of `window` (mean) and took (median minutes).
 * (2026-10-27, fix MED-3) Each sample pair's increase is split among ALL the items that ran in it (any
 * status, a running one too), by the seconds each ran in it, so tasks running at the same time (lanes,
 * parallel runners) are not each charged with all of it. A done task counts when the pairs cover at least
 * MIN_COVER of its run; its use is then scaled up to its whole run.
 * @param {Usage | null} usage @param {string} window
 * @param {{status?: string, started_at?: string | null, finished_at?: string | null}[]} items
 * @param {number} [nowS] for a running item's run (default: now)
 * @returns {{perTask: number | null, typicalMinutes: number | null}}
 */
function taskUse(usage, window, items, nowS) {
  const runs = runIntervals(items, nowS === undefined ? Date.now() / 1000 : nowS);
  const done = runs.filter((r) => r.done).sort((a, b) => b.end - a.end).slice(0, TASKS_LOOKBACK);
  const minutes = done.map((r) => (r.end - r.start) / 60);
  const rows = usage ? usage.samples.filter((x) => x.window === window) : [];
  const share = done.map(() => 0);
  const covered = done.map(() => 0);
  for (let i = 1; i < rows.length; i += 1) {
    const p = rows[i - 1];
    const q = rows[i];
    const du = q.utilization - p.utilization;
    if (q.t <= p.t || du < 0 || q.resetsAt !== p.resetsAt) continue; // a reset in between, or no time
    const total = runs.reduce((sum, r) => sum + overlap(p.t, q.t, r.start, r.end), 0);
    if (total <= 0) continue;
    done.forEach((r, k) => {
      const o = overlap(p.t, q.t, r.start, r.end);
      share[k] += (du * o) / total;
      covered[k] += o;
    });
  }
  /** @type {number[]} */
  const uses = [];
  done.forEach((r, k) => {
    const cover = covered[k] / (r.end - r.start);
    if (cover >= MIN_COVER) uses.push(share[k] / Math.min(1, cover));
  });
  return {
    perTask: uses.length >= MIN_TASKS ? uses.reduce((a, b) => a + b, 0) / uses.length : null,
    typicalMinutes: minutes.length >= MIN_TASKS ? median(minutes) : null,
  };
}

/**
 * The forecast for every window, the binding one (the first to run out before its reset, else the
 * fullest) and the queue's fit.
 * @param {Usage | null} usage
 * @param {{status?: string, started_at?: string | null, finished_at?: string | null}[]} items this project's items
 * @param {number} nowMs
 * @returns {Forecast | null} null = no usage data at all
 */
function forecast(usage, items, nowMs) {
  if (!usage) return null;
  const runs = runIntervals(items, nowMs / 1000);
  const windows = Object.keys(usage.latest).sort()
    .map((w) => windowForecast(usage, w, nowMs, runs)).filter((w) => w !== null);
  const live = /** @type {WindowForecast[]} */ (windows).filter((w) => !w.stale && !w.reset);
  const hitting = live.filter((w) => w.hitsBeforeReset && w.minutesLeft !== null)
    .sort((a, b) => /** @type {number} */ (a.minutesLeft) - /** @type {number} */ (b.minutesLeft));
  const binding = hitting[0] || [...live].sort((a, b) => b.utilization - a.utilization)[0] || null;
  const pending = (items || []).filter((it) => it.status === 'pending' || it.status === 'waiting').length;
  const use = taskUse(usage, binding ? binding.window : 'five_hour', items, nowMs / 1000);
  // (2026-10-27, fix round 2 LOW) the running tasks still need about half a task each first
  const running = (items || []).filter((it) => it.status === 'running').length;
  const fit = binding && use.perTask !== null && use.perTask > 0
    ? Math.max(0, Math.floor((1 - binding.utilization - running * use.perTask * 0.5) / use.perTask + 1e-9)) : null;
  return { windows: /** @type {WindowForecast[]} */ (windows), binding, pending, perTask: use.perTask,
    typicalMinutes: use.typicalMinutes === null ? DEFAULT_TASK_MIN : use.typicalMinutes,
    typicalKnown: use.typicalMinutes !== null, fit };
}

/** @param {string} window */
function windowLabel(window) {
  if (window === 'five_hour') return t('5h');
  if (window === 'seven_day') return t('7d');
  return window;
}

/** The limit's name in a sentence. @param {string} window */
function limitName(window) {
  if (window === 'five_hour') return t('5-hour');
  if (window === 'seven_day') return t('7-day');
  return window;
}

/** `~25 min`, `~3 h`, `~2 d`. @param {number} minutes */
function duration(minutes) {
  const m = Math.max(0, Math.round(minutes));
  if (m < 90) return t('~{0} min', m);
  if (m < 48 * 60) return t('~{0} h', Math.round(m / 60));
  return t('~{0} d', Math.round(m / 1440));
}

/** @param {number | null} epoch @param {Date} now */
function at(epoch, now) {
  // (2026-10-27, integrate: review LOW-4) a time Date cannot hold is no time (toISOString would throw)
  const d = epoch === null ? null : new Date(epoch * 1000);
  return d === null || !Number.isFinite(d.getTime()) ? null : localTime(d.toISOString(), now);
}

/**
 * One window's part of the line. @param {WindowForecast} w @param {Forecast} fc @param {Date} now
 * @param {boolean} withFit
 */
function windowPart(w, fc, now, withFit) {
  const label = windowLabel(w.window);
  if (w.reset) return `${label}: ${t('reset at {0}; no new data yet', at(w.resetsAt, now) || '?')}`;
  const parts = [`${label}: ${Math.round(w.utilization * 100)}%`];
  if (w.stale) {
    parts.push(t('no new data since {0}, so no forecast', at(Math.floor(now.getTime() / 1000 - w.ageMs / 1000), now) || '?'));
  } else if (w.minutesLeft !== null && w.hitsBeforeReset) {
    parts.push(t('{0} at the current rate', duration(w.minutesLeft)));
  } else if (w.ratePerHour !== null) {
    parts.push(t('lasts until the reset at the current rate'));
  }
  const r = at(w.resetsAt, now);
  if (r) parts.push(t('resets {0}', r));
  if (withFit && !w.stale) {
    if (fc.fit !== null && fc.pending && fc.fit >= fc.pending) parts.push(t('all {0} queued tasks fit', fc.pending)); // (2026-10-27, fix round 2 LOW)
    else if (fc.fit !== null) parts.push(fc.pending ? t('~{0} of {1} queued tasks fit', fc.fit, fc.pending) : t('~{0} tasks fit', fc.fit));
    else if (fc.pending) parts.push(t('tasks that fit: unknown (too few finished tasks)'));
  }
  return parts.join(' · ');
}

/**
 * The compact tooltip line(s): the 5-hour window, and the binding window when it is another one.
 * e.g. "5h: 82% · ~25 min at the current rate · resets 23:00 · ~3 of 10 queued tasks fit". [] = no data.
 * @param {Forecast | null} fc @param {Date} now @returns {string[]}
 */
function lines(fc, now) {
  if (!fc || !fc.windows.length) return [];
  const five = fc.windows.find((w) => w.window === 'five_hour') || null;
  const shown = [five, fc.binding].filter((w, i, a) => w && a.indexOf(w) === i);
  if (!shown.length) shown.push(fc.windows[0]);
  return shown.map((w) => windowPart(/** @type {WindowForecast} */ (w), fc, now, w === (fc.binding || five)));
}

/**
 * (2026-10-26) The send-path check: warn when a window is at WARN_UTILIZATION or more, or when at the
 * current rate it runs out before a typical task (fc.typicalMinutes) would finish and before it resets.
 * Reset data never warns. (2026-10-27, fix MED-2) Stale data has no rate, but a stale reading at
 * WARN_UTILIZATION or more of a window whose reset is still ahead warns: utilization only rises until
 * resets_at, so it is a lower bound (`stale` true, `sampleT` its time). null = no warning.
 * @param {Forecast | null} fc @param {number} nowMs
 * @returns {{window: string, minutes: number | null, utilization: number, resetsAt: number | null, holdUntil: string | null,
 *   stale: boolean, sampleT: number} | null}
 */
function warnDecision(fc, nowMs) {
  if (!fc) return null;
  const hits = fc.windows.filter((w) => !w.reset && (w.stale
    ? w.resetsAt !== null && w.utilization >= WARN_UTILIZATION
    : w.utilization >= WARN_UTILIZATION || (w.hitsBeforeReset && w.minutesLeft !== null && w.minutesLeft < fc.typicalMinutes)));
  if (!hits.length) return null;
  // (2026-10-27, integrate: review LOW-2) the minutes to the limit only when it hits before the reset: at 95%+
  // with the reset first, "you will likely hit it in ~N min" would be false (the text says the % instead)
  const left = (/** @type {WindowForecast} */ w) => (w.hitsBeforeReset && w.minutesLeft !== null ? w.minutesLeft : null);
  hits.sort((a, b) => (left(a) === null ? Infinity : /** @type {number} */ (left(a)))
    - (left(b) === null ? Infinity : /** @type {number} */ (left(b))) || b.utilization - a.utilization);
  const w = hits[0];
  const m = left(w);
  return { window: w.window, minutes: m === null ? null : Math.round(m), utilization: w.utilization,
    resetsAt: w.resetsAt, holdUntil: holdUntil(w.resetsAt, nowMs), stale: w.stale, sampleT: Math.round((nowMs - w.ageMs) / 1000) };
}

/**
 * The ISO time "queue for after the reset" holds until; null when the reset is unknown, past or too far.
 * @param {number | null} resetsAt epoch s @param {number} nowMs
 */
function holdUntil(resetsAt, nowMs) {
  if (resetsAt === null) return null;
  const until = (resetsAt + RESET_MARGIN_S) * 1000;
  if (until <= nowMs || until - nowMs > HOLD_MAX_S * 1000) return null;
  return new Date(until).toISOString();
}

/**
 * The warning's text. @param {{window: string, minutes: number | null, utilization: number, stale?: boolean,
 *   sampleT?: number}} d @param {Date} [now] for a stale reading's time
 */
function warningText(d, now = new Date()) {
  const first = d.stale // (2026-10-27, fix MED-2) the last reading, with its time
    ? t('The {0} limit was at {1}% at {2}.', limitName(d.window), Math.round(d.utilization * 100),
      at(d.sampleT === undefined ? null : d.sampleT, now) || '?')
    : d.minutes !== null && d.minutes >= 1
    ? t('You will likely hit the {0} limit in {1}.', limitName(d.window), duration(d.minutes))
    : t('You are at {0}% of the {1} limit.', Math.round(d.utilization * 100), limitName(d.window));
  return `${first} ${t('A task started now may stop midway; after the reset its prompt cache is cold and resuming costs more.')}`;
}

/**
 * `hold on --until ISO --json`: the timed hold behind "Queue for after the reset" (the core's runner
 * waits for it instead of exiting).
 * @param {string} iso
 */
function holdUntilArgs(iso) {
  if (typeof iso !== 'string' || !ISO_RE.test(iso)) throw new Error(`not an ISO time: ${iso}`);
  return ['hold', 'on', '--json', `--until=${iso}`];
}

/**
 * (2026-10-27, fix LOW) What to tell the owner when "Queue for after the reset" did not leave the hold it
 * asked for: none taken, an untimed hold already on, or another until. null = the hold is as asked.
 * @param {{hold: boolean, until?: string | null, runnerAlive?: boolean} | null} r the `hold on --until` result @param {string} wanted ISO
 * @param {Date} now
 */
function holdMismatch(r, wanted, now) {
  if (!r || !r.hold) return t('The queue could not be put on hold, so the task was sent without waiting for the reset.');
  // (2026-10-27, integrate: review LOW-5) a runner alive past that hold ("Start anyway") still takes the task
  if (!r.until && r.runnerAlive) {
    return t('The queue was already on hold with no end time, but its runner was started anyway: it may take the task now.');
  }
  if (!r.until) return t('The queue was already on hold with no end time; it stays on hold until you release it.');
  if (Math.abs(Date.parse(r.until) - Date.parse(wanted)) <= 1000) return null;
  return t('The queue is on hold until {0}, not until the reset at {1}.', localTime(r.until, now) || r.until,
    localTime(wanted, now) || wanted);
}

/**
 * (2026-10-27, fix LOW) Whether a `hold on` result is a hold put on just now (its since not before
 * `beforeMs`, less 2 s for the core's whole seconds), not one that was already on: only such a hold is
 * released again when the add it was for fails.
 * @param {{hold: boolean, since?: string | null} | null} r @param {number} beforeMs
 */
function holdIsNew(r, beforeMs) {
  const since = r && r.hold && typeof r.since === 'string' ? Date.parse(r.since) : NaN;
  return Number.isFinite(since) && since >= beforeMs - 2000;
}

/**
 * (2026-10-27, usage forecast fix round 2 MED) Whether Hold queue (`on`) / Release hold (`off`) applies to
 * a queue row: `on` also to a timed hold (a plain `hold on` makes it indefinite), `off` to any hold.
 * @param {{hold?: {until?: string | null} | null}} lane @param {boolean} on
 */
function holdCandidate(lane, on) {
  return on ? (!lane.hold || !!lane.hold.until) : !!lane.hold;
}

/**
 * The tooltip line of each queue on a timed hold. @param {{name: string, hold?: {until?: string | null} | null}[]} lanes
 * @param {Date} now
 */
function heldLines(lanes, now) {
  return (lanes || []).filter((l) => l.hold && l.hold.until).map((l) => t('Queue {0} waits until {1} (queued for after the usage-limit reset).',
    l.name, localTime(/** @type {string} */ (/** @type {{until: string}} */ (l.hold).until), now) || '?'));
}

module.exports = {
  STALE_MS, GAP_S, RATE_SPAN_S, MIN_SPAN_S, MIN_TASKS, DEFAULT_TASK_MIN, WARN_UTILIZATION, RESET_MARGIN_S,
  normalize, fromLanes, burnRate, current, windowForecast, taskUse, forecast, lines, warnDecision, holdUntil,
  warningText, holdUntilArgs, heldLines, duration, runIntervals, activeSeconds, holdMismatch, holdIsNew,
  holdCandidate,
};

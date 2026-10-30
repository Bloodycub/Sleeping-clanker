// @ts-check
// Non-overlapping refresh scheduler, without the vscode API (2026-10-13, SPEC_VSCODE section 3).
// A setTimeout chain (never setInterval): the next tick is scheduled only after the current one
// settles, so polls cannot overlap. A trigger during a tick queues exactly one follow-up tick.
'use strict';

const IDLE_INTERVAL_MS = 30000;

/**
 * Fast interval while the view is visible or a runner is alive, else 30 s.
 * @param {{viewVisible: boolean, runnerAlive: boolean, refreshSeconds: number}} s
 */
function pollIntervalMs(s) {
  const fast = Math.max(1, Number(s.refreshSeconds) || 5) * 1000;
  return s.viewVisible || s.runnerAlive ? fast : IDLE_INTERVAL_MS;
}

class Poller {
  /**
   * @param {() => Promise<void>} tick
   * @param {() => number} intervalMs
   * @param {{setTimeout?: typeof setTimeout, clearTimeout?: typeof clearTimeout,
   *   onError?: (err: unknown) => void}} [deps]
   */
  constructor(tick, intervalMs, deps = {}) {
    this._tick = tick;
    this._intervalMs = intervalMs;
    this._setTimeout = deps.setTimeout || setTimeout;
    this._clearTimeout = deps.clearTimeout || clearTimeout;
    this._onError = deps.onError || ((err) => console.error('claud-autopilot poll failed:', err));
    /** @type {any} */
    this._timer = null;
    this._inFlight = false;
    this._again = false;
    this._stopped = true;
    /** @type {Promise<void> | null} */
    this._current = null;
  }

  get inFlight() { return this._inFlight; }

  start() {
    this._stopped = false;
    this.trigger();
  }

  stop() {
    this._stopped = true;
    if (this._timer) this._clearTimeout(this._timer);
    this._timer = null;
  }

  /** Run a tick now (or right after the one in flight). Resolves when that tick has settled. */
  trigger() {
    if (this._stopped) return Promise.resolve();
    if (this._inFlight) {
      this._again = true;
      return /** @type {Promise<void>} */ (this._current);
    }
    if (this._timer) this._clearTimeout(this._timer);
    this._timer = null;
    this._current = this._run();
    return this._current;
  }

  /** Re-arm the timer with the current interval (e.g. after the view became visible). */
  reschedule() {
    if (this._stopped || this._inFlight) return;
    this._schedule();
  }

  async _run() {
    this._inFlight = true;
    try {
      await this._tick();
    } catch (err) {
      this._onError(err);
    } finally {
      this._inFlight = false;
    }
    if (this._stopped) return;
    if (this._again) {
      this._again = false;
      this._current = this._run();
      await this._current;
      return;
    }
    this._schedule();
  }

  _schedule() {
    if (this._timer) this._clearTimeout(this._timer);
    this._timer = this._setTimeout(() => {
      this._timer = null;
      this.trigger();
    }, this._intervalMs());
  }
}

/**
 * (2026-10-17, 0.3.3) The item status changes between two polls, for the output channel:
 * `item 6f1b9503: pending -> running` (` (queue docs)` outside the main queue), `new -> pending` for
 * an item that appeared, `done -> removed` for one that is gone. `prev` null = the first poll: no
 * lines, only the baseline. Pure: the caller keeps `next` for the following poll.
 * @typedef {Map<string, {id: string, lane: string, status: string}>} StatusMap key = queue + id
 * @param {StatusMap | null} prev from the previous call
 * @param {{id: string, status: string, lane?: string | null}[]} items this poll's items
 * @param {string} [mainLane] the queue whose name is not shown
 * @returns {{lines: string[], next: StatusMap}}
 */
function statusChanges(prev, items, mainLane = 'default') {
  /** @type {StatusMap} */
  const next = new Map();
  for (const it of items) {
    const lane = it.lane || mainLane;
    next.set(`${lane.toLowerCase()}/${it.id}`, { id: it.id, lane, status: it.status });
  }
  /** @type {string[]} */
  const lines = [];
  if (!prev) return { lines, next };
  /** @param {{id: string, lane: string}} w @param {string} from @param {string} to */
  const line = (w, from, to) => `item ${w.id}: ${from} -> ${to}`
    + (w.lane.toLowerCase() === mainLane.toLowerCase() ? '' : ` (queue ${w.lane})`);
  for (const [key, now] of next) {
    const was = prev.get(key);
    if (!was) lines.push(line(now, 'new', now.status));
    else if (was.status !== now.status) lines.push(line(now, was.status, now.status));
  }
  for (const [key, was] of prev) if (!next.has(key)) lines.push(line(was, was.status, 'removed'));
  return { lines, next };
}

/**
 * (2026-10-17, 0.3.3) Poll warnings logged only when the SET changes: `WARN <w>` when a warning first
 * appears, `warning cleared: <w>` when it is gone; a repeated warning logs nothing. Pure.
 * @param {Set<string> | null} prev the warnings of the previous poll (null = none yet)
 * @param {string[]} warnings this poll's warnings
 * @returns {{lines: string[], next: Set<string>}}
 */
function warningChanges(prev, warnings) {
  const next = new Set(warnings);
  const was = prev || new Set();
  /** @type {string[]} */
  const lines = [];
  for (const w of next) if (!was.has(w)) lines.push(`WARN ${w}`);
  for (const w of was) if (!next.has(w)) lines.push(`warning cleared: ${w}`);
  return { lines, next };
}

module.exports = { IDLE_INTERVAL_MS, pollIntervalMs, Poller, statusChanges, warningChanges };

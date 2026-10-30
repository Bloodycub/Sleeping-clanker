// @ts-check
// Status bar text and tree item formatting, without the vscode API (2026-10-13, SPEC_VSCODE section 3).
// (2026-10-13, v0.2) Goal bar + formatElapsed (SPEC_GOALS 5.2/5.3) and hovers that explain what is
// happening (docs/SPEC_HOVER.md section 2). Everything here is plain text, never Markdown (the
// prompt and result are untrusted), and uses ABSOLUTE times only: nothing in a tooltip or the tree
// may change by itself on a timer, or VS Code closes an open hover (SPEC_HOVER section 1).
// (2026-10-15, v0.3) The `blocked` status ("Needs you", a red X; `failed` moved to circle-slash),
// "N need you" in the status bar, replies, the shell-read profile and per-queue lines (SPEC_V03 2.3, 5).
'use strict';

const { t } = require('./i18n'); // (2026-10-21) the UI language; the English text is the key
const { providerRefusal, NO_SHELL_PROFILES } = require('./argv'); // (2026-10-21, v049 merge) no shell fix for a key provider

const LABEL_MAX = 60;
const TOOLTIP_TEXT_MAX = 500;
const TOOLTIP_PROMPT_MAX = 1500;
const TOOLTIP_ATTACH_MAX = 5;
const GOAL_BAR_TEXT_MAX = 40;
const GOAL_TOOLTIP_TEXT_MAX = 300;

/** Codicon id + optional theme color per status. */
const ICONS = Object.freeze({
  pending: { id: 'circle-outline', color: null },
  running: { id: 'sync~spin', color: 'charts.blue' },
  waiting: { id: 'watch', color: 'charts.yellow' },
  paused: { id: 'debug-pause', color: 'charts.orange' },
  done: { id: 'pass', color: 'testing.iconPassed' },
  // (2026-10-15, SPEC_V03 5.1) `failed` moved off the red X so it can be told apart from `blocked`
  failed: { id: 'circle-slash', color: 'testing.iconFailed' },
  cancelled: { id: 'circle-slash', color: 'disabledForeground' },
  // (2026-10-21, owner) a task that needs the owner is a yellow question mark, goal or not
  blocked: { id: 'question', color: 'charts.yellow' },
  unknown: { id: 'question', color: null },
});
const GOAL_ICON = 'target';
/** (2026-10-26, owner) a goal's round cap for display: -1 = unlimited (∞), none = '?' @param {number | null | undefined} m */
const roundsCap = (m) => (m === -1 ? '∞' : m || '?');
const BLOCKED_LABEL = 'Needs you';

/** What each profile lets Claude do (SPEC_HOVER 2.1). */
// (2026-10-21, security review M5) what every `full` label, row and header says
const FULL_WARN = '⚠ FULL ACCESS (no permission checks)';
const FULL_BADGE = '⚠ FULL ACCESS';
const PROFILE_LINES = Object.freeze({
  safe: 'may read files and edit files only inside this folder; no shell, no web.',
  edits: 'may read and edit files; no shell commands, no web.',
  plan: 'read-only: makes a plan, changes nothing.',
  'shell-read': 'may read and edit files in this folder and run a fixed list of read-only commands (exact git status/log/diff forms, ls, Get-Content); no deleting, no installs, no ssh, no web.',
  // (2026-10-20, 0.4.4 logfix A)
  // (2026-10-27, SEC-M5) no "web stays denied": an allowed test runner, interpreter or build tool runs any
  // code the task writes, and that code can reach the web (only Claude's own web tools stay denied)
  // (2026-10-27, SEC-M5 review 3) and only the DIRECT git push / delete / install command stays denied: the code
  // an allowed runner runs can push, delete and install too
  // (2026-10-27, SEC-M5 review 4) no claim about which direct commands stay denied (the validator accepts
  // `go install`, `cargo add`, `git tag -d`, ...): shell-dev is not a sandbox, the allowed commands run with
  // the owner's rights, and only a fixed list of risky programs is refused
  'shell-dev': 'like shell-read, plus the commands on this project\'s allow-list (Edit allowed commands…). This is not a sandbox: the commands you allow run with your rights (a test runner, interpreter or build tool runs any code the task writes, and that code can push, delete, install and reach the web); only a fixed list of risky programs is refused. An empty list = shell-read.',
  // (2026-10-21, owner) opt-in only
  full: '⚠ FULL ACCESS: runs any command (Bash, PowerShell), subagents and web with no permission checks — it can push, delete and install. For unattended overnight work you trust.',
});
// (2026-10-20, 0.4.4 logfix A) a blocked item that needs a command: the core's _SHELL_NEED_RE (loop.py)
const NEEDS_COMMAND_RE = /\b(shell|bash|powershell|terminal|commands?|command line|komen\w*|komento\w*|git|pytest|npm)\b/i;
// (2026-10-20, 0.4.4 logfix C) the Task view's notice for item.repeat_block > 0
const REPEAT_BLOCK_TEXT = 'Same question as last time — change the profile or answer it.';
// (2026-10-15, SPEC_V03 5.2) a blocked reason like this puts shell-read first in the reply picker
const SHELL_RE = /\b(shell|bash|powershell|terminal|command line)\b/i;

/** The queue's display name: `default` is shown as `Main` (SPEC_V03 2.3). @param {string} name */
function laneLabel(name) {
  return !name || name.toLowerCase() === 'default' ? t('Main') : name;
}

/** @param {number} n */
const pad2 = (n) => String(n).padStart(2, '0');

/** @param {string | null | undefined} iso */
function toDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Local "HH:MM" for today, "MM-DD HH:MM" for another day; null for a bad timestamp.
 * @param {string | null} iso
 * @param {Date} [now]
 */
function localTime(iso, now = new Date()) {
  const d = toDate(iso);
  if (!d) return null;
  const hm = `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
  return sameDay ? hm : `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${hm}`;
}

/**
 * (2026-10-26, limit_continue) The time left until `iso`: "in 42 min", "in 2 h 5 min", "in under 1 min";
 * null when it has passed or is not a time. Minutes round up. Never in the tree or a tooltip (SPEC_HOVER
 * 1): the status bar text and the Task view only. media/chat.js leftText has the same texts.
 * @param {string | null | undefined} iso @param {Date} [now]
 */
function timeLeft(iso, now = new Date()) {
  const d = toDate(iso);
  const ms = d ? d.getTime() - now.getTime() : 0;
  if (!(ms > 0)) return null;
  if (ms < 60000) return t('in under 1 min');
  const min = Math.ceil(ms / 60000);
  return min < 60 ? t('in {0} min', min) : t('in {0} h {1} min', Math.floor(min / 60), min % 60);
}

/** (2026-10-26, limit_continue) " (in 42 min)", or '' (see timeLeft). @param {string | null | undefined} iso @param {Date} [now] */
function leftSuffix(iso, now) {
  const left = timeLeft(iso, now);
  return left ? ` (${left})` : '';
}

/** Local "MM-DD HH:MM" always (the goal's start in the tree and tooltips). @param {string | null} iso */
function localStamp(iso) {
  const d = toDate(iso);
  return d ? `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}` : null;
}

/**
 * `33s`, `21m 33s`, `5h 21m 33s`, `2d 5h 21m 33s` (SPEC_GOALS 5.2). Negative or bad input = `0s`.
 * @param {number} ms
 */
function formatElapsed(ms) {
  const total = Number.isFinite(ms) && ms > 0 ? Math.floor(ms / 1000) : 0;
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  // (2026-10-27, i18n) the units in the UI language
  if (d > 0) return t('{0}d {1}h {2}m {3}s', d, h, m, s);
  if (h > 0) return t('{0}h {1}m {2}s', h, m, s);
  if (m > 0) return t('{0}m {1}s', m, s);
  return t('{0}s', s);
}

/** @param {string} text @param {number} max */
const clip = (text, max) => (text.length <= max ? text : text.slice(0, max) + '…');

/** @param {{kind?: string}} item */
const isGoal = (item) => item.kind === 'goal';

/**
 * (2026-10-17, 0.3.3) Pending + waiting items in the queues that have NO live runner: nothing will
 * start or resume them. A queue counts as alive when its lock says so or this window has started
 * its runner (`ownLanes`, names; the lock may not be visible yet). Pure.
 * @param {import('./parse').Lane[] | null | undefined} lanes this window's project, one row per queue
 * @param {Iterable<string>} [ownLanes] queue names whose runner this window started
 */
function stoppedRunnerPending(lanes, ownLanes = []) {
  if (!Array.isArray(lanes)) return 0;
  const own = new Set([...ownLanes].map((n) => n.toLowerCase()));
  let n = 0;
  for (const l of lanes) {
    // (2026-10-21, v049 dontask_hold) a held queue is stopped on purpose: no "runner stopped" warning
    // (2026-10-27, integrate: usage forecast review LOW-1) but not on a TIMED hold: a runner must be there
    // to wait it out, so without one its tasks are stopped like any other
    if (l.runnerAlive || own.has(l.name.toLowerCase()) || (l.hold && !l.hold.until)) continue;
    n += (l.counts.pending || 0) + (l.counts.waiting || 0);
  }
  return n;
}

/**
 * (2026-10-21, v049 dontask_hold) The held queues of this window's project (lanes, else the main
 * queue's status). Pure.
 * @param {import('./parse').Lane[] | null | undefined} lanes @param {import('./parse').Status | null} [status]
 * (2026-10-26, limit_continue) a usage-limit hold also has `resetsAt` (any other hold keeps {name, since}).
 * @returns {{name: string, since: string | null, resetsAt?: string | null}[]}
 */
function heldQueues(lanes, status = null) {
  const entry = (/** @type {string} */ name, /** @type {any} */ h) => (h && h.reason === 'limit'
    ? { name, since: h.since, resetsAt: h.resetsAt || null } : { name, since: h ? h.since : null });
  // (2026-10-26, usage forecast) a timed hold is not listed: its runner waits it out (lib/usage.js heldLines)
  if (Array.isArray(lanes) && lanes.length) {
    return lanes.filter((l) => l.hold && !l.hold.until).map((l) => entry(l.name, l.hold));
  }
  return status && status.hold && !status.hold.until ? [entry('default', status.hold)] : [];
}

/** (2026-10-26, limit_continue) The earliest reset of the usage-limit holds, or null. @param {{resetsAt?: string | null}[]} held */
function limitHoldReset(held) {
  /** @type {string[]} */
  const at = [];
  for (const h of held) if (h.resetsAt && toDate(h.resetsAt)) at.push(h.resetsAt);
  return at.sort((a, b) => Number(toDate(a)) - Number(toDate(b)))[0] || null;
}

/** (2026-10-21, v049 dontask_hold) The status bar's hold marker, '' without a held queue.
 * (2026-10-26, limit_continue) a usage-limit hold: "on hold: usage limit until 23:01 (in 42 min)". */
function holdMarker(/** @type {{name: string, resetsAt?: string | null}[]} */ held, /** @type {Date | undefined} */ now) {
  const reset = limitHoldReset(/** @type {any} */ (held));
  // (2026-10-27, integrate: limit_continue review LOW-6) after the reset "until 23:01" was wrong: the hold
  // stays until the owner releases it
  if (reset && !timeLeft(reset, now)) return ` · $(debug-pause) ${t('on hold: usage limit reset passed — Resume Queue (Release Hold) to continue')}`;
  if (reset) return ` · $(debug-pause) ${t('on hold: usage limit until {0}', localTime(reset, now))}${leftSuffix(reset, now)}`;
  return held.length ? ` · $(debug-pause) ${t('on hold')}` : '';
}

/**
 * `$(rocket) Sleeping Clanker: idle | running 1 · 3 pending | waiting until 15:45`.
 * (2026-10-17, 0.3.3) With `opts.lanes`: `$(warning) Sleeping Clanker: N pending — runner stopped` while a
 * queue without a live runner holds pending or waiting items (stoppedRunnerPending).
 * @param {import('./parse').Status | null} status null = not known yet
 * @param {{error?: string | null, now?: Date, lanes?: import('./parse').Lane[] | null, ownLanes?: Iterable<string>}} [opts]
 */
function statusBarText(status, opts = {}) {
  if (opts.error) return `$(warning) Sleeping Clanker: ${t('error')}`;
  if (!status) return '$(rocket) Sleeping Clanker: …';
  const stopped = stoppedRunnerPending(opts.lanes, opts.ownLanes);
  const held = heldQueues(opts.lanes, status);
  const hold = holdMarker(held, opts.now); // (2026-10-21, v049 dontask_hold)
  if (stopped > 0) return `$(warning) Sleeping Clanker: ${t('{0} pending — runner stopped', stopped)}${hold}`;
  const c = status.counts;
  let state;
  if (c.running > 0) state = t('running {0}', c.running);
  else if (c.waiting > 0) {
    const at = localTime(status.nextWaitUntil, opts.now);
    // (2026-10-26, limit_continue) + the time left, unless a limit hold's marker already counts down
    const left = limitHoldReset(held) ? '' : leftSuffix(status.nextWaitUntil, opts.now);
    // (2026-10-27, integrate: review LOW-6) a limit-held queue past its wait time waits for the owner, not a clock
    const passed = !!limitHoldReset(held) && !timeLeft(status.nextWaitUntil, opts.now);
    state = at && !passed ? `${t('waiting until {0}', at)}${left}` : t('waiting');
  } else state = t('idle');
  const pending = c.pending > 0 ? ` · ${t('{0} pending', c.pending)}` : '';
  const blocked = c.blocked > 0 ? ` · ${t('{0} need you', c.blocked)}` : ''; // (2026-10-15, SPEC_V03 5.1)
  return `$(rocket) Sleeping Clanker: ${state}${pending}${blocked}${hold}`;
}

/**
 * (2026-10-15, SPEC_V03 5.1) The main item's background: the warning colour while any item is
 * blocked, else none. (2026-10-17, 0.3.3) Also while pending/waiting items have no runner.
 * @param {import('./parse').Status | null} status
 * @param {{error?: string | null, lanes?: import('./parse').Lane[] | null, ownLanes?: Iterable<string>}} [opts]
 */
function statusBarBackground(status, opts = {}) {
  if (opts.error || !status) return null;
  if (status.counts.blocked > 0 || stoppedRunnerPending(opts.lanes, opts.ownLanes) > 0) return 'statusBarItem.warningBackground';
  return null;
}

/**
 * (2026-10-20, 0.4.4) The main item's foreground: green while a task is running, else none (the
 * theme's own colour). VS Code honours only `statusBarItem.errorBackground` and
 * `warningBackground` as item backgrounds, so "running" is a green TEXT colour, not a green bar.
 * A warning state wins: the warning background brings its own foreground.
 * @param {import('./parse').Status | null} status
 * @param {{error?: string | null, lanes?: import('./parse').Lane[] | null, ownLanes?: Iterable<string>}} [opts]
 */
function statusBarColor(status, opts = {}) {
  if (opts.error || !status) return null;
  if (statusBarBackground(status, opts)) return null;
  return status.counts.running > 0 ? 'charts.green' : null;
}

// (2026-10-21, owner) the coloured count segments right of the main item, in this order
const STATUS_SEGMENTS = Object.freeze([
  { key: 'done', icon: 'pass', color: 'charts.green', word: 'complete', count: (/** @type {any} */ c) => c.done || 0 },
  { key: 'blocked', icon: 'question', color: 'charts.yellow', word: 'need your answer', count: (/** @type {any} */ c) => c.blocked || 0 },
  { key: 'failed', icon: 'error', color: 'charts.red', word: 'failed', count: (/** @type {any} */ c) => c.failed || 0 },
  { key: 'open', icon: 'circle-large-outline', color: 'charts.blue', word: 'not done yet (pending, running, waiting or paused)',
    count: (/** @type {any} */ c) => (c.pending || 0) + (c.running || 0) + (c.waiting || 0) + (c.paused || 0) },
]);

/**
 * (2026-10-21, owner) The "rainbow" next to the main item: green = complete, yellow ? = need your
 * answer, red = failed, blue = not done yet. A count of 0 has no segment; no status or an error = none.
 * @param {import('./parse').Status | null} status @param {{error?: string | null}} [opts]
 * @returns {{key: string, text: string, color: string, tooltip: string}[]}
 */
function statusBarSegments(status, opts = {}) {
  if (opts.error || !status) return [];
  const out = [];
  for (const s of STATUS_SEGMENTS) {
    const n = s.count(status.counts);
    if (n > 0) out.push({ key: s.key, text: `$(${s.icon}) ${n}`, color: s.color, tooltip: `Sleeping Clanker: ${n} ${t(s.word)}. ${t('Click to open the queue.')}` });
  }
  return out;
}

/**
 * One line per queue for the status bar tooltip (only with two or more queues).
 * @param {import('./parse').Lane} lane @param {Date} [now]
 */
function laneStateLine(lane, now) {
  return t('Queue {0}: {1}', laneLabel(lane.name), laneStateParts(lane, now));
}

/** (2026-10-21) laneStateLine without its "Queue <name>: " lead. @param {import('./parse').Lane} lane @param {Date} [now] */
function laneStateParts(lane, now) {
  const c = lane.counts;
  const runner = lane.runnerAlive
    ? (lane.runnerPid ? t('runner running (pid {0})', lane.runnerPid) : t('runner running'))
    : t('runner stopped');
  const parts = [runner];
  if (lane.hold) parts.push(t('on hold')); // (2026-10-21, v049 dontask_hold)
  if (c.running > 0) parts.push(t('running {0}', c.running));
  if (c.waiting > 0) {
    const at = localTime(lane.nextWaitUntil, now);
    parts.push(at ? t('waiting until {0}', at) : t('waiting'));
  }
  if (c.pending > 0) parts.push(t('{0} pending', c.pending));
  if (c.paused > 0) parts.push(t('{0} paused', c.paused));
  if (c.blocked > 0) parts.push(t('{0} need you', c.blocked));
  const par = parallelLine(lane, now); // (2026-10-27, U4, 8.10) "Tasks at once: 3 (2 working, 1 merging)"
  if (par) parts.push(par);
  return parts.join(' · ');
}

/**
 * Main status bar tooltip (SPEC_HOVER 2.2): runner state in words, counts, next resume time.
 * @param {import('./parse').Status | null} status
 * @param {{error?: string | null, projectName?: string | null, now?: Date, lanes?: import('./parse').Lane[] | null,
 *   ownLanes?: Iterable<string>}} [opts]
 */
function statusBarTooltip(status, opts = {}) {
  if (opts.error) return `${t('Sleeping Clanker error: {0}', opts.error)}\n${t('Click for actions.')}`;
  if (!status) return t('Sleeping Clanker: loading…');
  const c = status.counts;
  const where = opts.projectName ? t('for {0}', opts.projectName) : t('for the shared queue');
  const runner = status.runnerAlive
    ? (status.runnerPid ? t('Runner is running {0} (pid {1}).', where, status.runnerPid) : t('Runner is running {0}.', where))
    : t('Runner is stopped: tasks wait until you start it.');
  const lines = [
    `Sleeping Clanker — ${opts.projectName || t('(no folder)')}`,
    runner,
    t('pending {0} · running {1} · waiting {2} · paused {3} · done {4} · failed {5} · cancelled {6} · need you {7}',
      c.pending, c.running, c.waiting, c.paused || 0, c.done, c.failed, c.cancelled, c.blocked || 0),
  ];
  const stopped = stoppedRunnerPending(opts.lanes, opts.ownLanes);
  if (stopped > 0) {
    lines.push(t('{0} pending or waiting task(s) have no runner, so nothing will start them. Click and choose '
      + 'Start runner, or turn on "Always start automatically" (claudAutopilot.autoStartRunner).', stopped));
  }
  // (2026-10-21, v049 dontask_hold) each held queue, by name
  for (const h of heldQueues(opts.lanes, status)) {
    // (2026-10-26, limit_continue) a usage-limit hold says when the limit resets (absolute time only)
    lines.push(h.resetsAt
      ? t('Queue {0} is on hold after a usage limit; the limit resets at {1}. Release the hold to continue then.',
        laneLabel(h.name), localTime(h.resetsAt, opts.now) || h.resetsAt)
      : t('Queue {0} is on hold (since {1}): no runner starts it until you release the hold.',
        laneLabel(h.name), localTime(h.since, opts.now) || h.since || '?'));
  }
  const lanes = opts.lanes || [];
  if (lanes.length >= 2) for (const lane of lanes) lines.push(laneStateLine(lane, opts.now));
  else if (lanes.length === 1 && parallelLine(lanes[0], opts.now)) lines.push(/** @type {string} */ (parallelLine(lanes[0], opts.now))); // (2026-10-27, U4, 8.10)
  const at = localTime(status.nextWaitUntil, opts.now);
  if (at) lines.push(t('Next usage-limit resume: {0}.', at));
  lines.push(t('Data: {0}', status.dataDir), t('Click for actions.'));
  return lines.join('\n');
}

/** Name, else the prompt's first 60 characters (whitespace collapsed), like the core's `list`.
 * @param {{name: string | null, prompt: string}} item @param {number} [max] */
function itemLabel(item, max = LABEL_MAX) {
  const text = item.name || String(item.prompt || '').split(/\s+/).filter(Boolean).join(' ');
  return text.slice(0, max) || t('(empty prompt)');
}

/**
 * Tree description. Never a ticking value (SPEC_HOVER 1): a goal shows its start TIME, not elapsed.
 * (2026-10-29, integrate round 8) `parallel`: the queue's live parallel.json (Ctx.parallel): a running task that
 * works alone in the project folder says so, a task waiting behind one says why that one runs alone.
 * @param {import('./parse').Item} item @param {Date} [now] @param {Ctx['parallel']} [parallel]
 */
function itemDescription(item, now, parallel) {
  /** @type {string[]} */
  const parts = [];
  if (item.profile === 'full') parts.push(t(FULL_BADGE)); // (2026-10-21, security review M5) first, always
  if (isGoal(item)) {
    parts.push(t('r{0}/{1}', item.rounds || 0, roundsCap(item.max_rounds))); // (2026-10-27, i18n) r = round
    const started = localStamp(item.goal_started_at);
    if (started) parts.push(t('started {0}', started));
  } else {
    const n = item.attempts || 0;
    parts.push(t(n === 1 ? '{0} attempt' : '{0} attempts', n));
  }
  if (item.status === 'waiting') {
    const until = localTime(item.wait_until, now);
    parts.push(until ? t('waiting until {0}', until) : t('waiting'));
  }
  if (item.status === 'paused') parts.push(t('paused'));
  // (2026-10-27, U4, 8.10) tasks at once: "needs you: merge conflict" instead of the plain "Needs you"
  if (item.status === 'blocked') parts.push(item.phase === 'needs-you' ? /** @type {string} */ (phaseText(item)) : t(BLOCKED_LABEL));
  // (2026-10-27, U4, 8.10) preparing a copy / merging / waiting to merge: <reason>; a pending one: "waits for #id8"
  if (item.phase === 'preparing' || item.phase === 'merging' || item.phase === 'merge-waiting') parts.push(/** @type {string} */ (phaseText(item)));
  if (aloneReason(item, parallel) !== null) parts.push(t('alone in the project folder')); // (2026-10-29, integrate round 8)
  const waits = item.status === 'pending' ? waitsText(item.waits, now, parallel && parallel.why) : null;
  if (waits) parts.push(waits);
  // (2026-10-21) when it finished: "done 14:32", "failed 10-20 09:05" (rendered here only, no tokens)
  const word = FINISH_WORDS[/** @type {keyof typeof FINISH_WORDS} */ (item.status)];
  const at = word ? localTime(item.finished_at, now) : null;
  if (at) parts.push(t(word, at));
  if (cancelledButMerged(item)) parts.push(t('its changes were already merged')); // (2026-10-27, U4, 3.3)
  const cache = cacheDescription(item, now); // (2026-10-21) when the prompt cache runs out
  if (cache) parts.push(cache);
  return parts.join(' · ');
}

const FINISH_WORDS = Object.freeze({ done: 'done {0}', failed: 'failed {0}', cancelled: 'cancelled {0}' });

// ---- prompt cache (2026-10-21) -----------------------------------------------------------------
// When the prompt cache of an item's session runs out. The runner's main thread writes the 1-hour
// cache, and the core counts a reply more than 55 min after the attempt ended as cold
// (goals.RESUME_FRESH_AFTER_S), so it "runs out" here at the same time. 15 min before: a warning.
const CACHE_WARM_MS = 55 * 60 * 1000;
const CACHE_WARN_MS = 15 * 60 * 1000;
const CACHE_STATUSES = Object.freeze(['blocked', 'done', 'failed', 'cancelled', 'paused']); // cli.REPLY_STATUSES

/**
 * The prompt cache of an item a reply resumes, or null (no session, no ended attempt, or an item
 * that is not waiting for a reply: pending, running, waiting).
 * @param {import('./parse').Item} item @param {Date} [now]
 * @returns {{until: Date, left: number, warn: boolean, expired: boolean} | null}
 */
function cacheState(item, now = new Date()) {
  if (!item || !CACHE_STATUSES.includes(item.status) || !item.session_id) return null;
  const ended = toDate(item.raw && item.raw.last_ended_at);
  if (!ended) return null;
  const until = new Date(ended.getTime() + CACHE_WARM_MS);
  const left = until.getTime() - now.getTime();
  return { until, left, warn: left > 0 && left <= CACHE_WARN_MS, expired: left <= 0 };
}

/**
 * The tree's cache part: a clock time, never a countdown (SPEC_HOVER 1); nothing once it ran out.
 * @param {import('./parse').Item} item @param {Date} [now]
 */
function cacheDescription(item, now = new Date()) {
  const c = cacheState(item, now);
  if (!c || c.expired) return null;
  const at = localTime(c.until.toISOString(), now);
  return c.warn ? t('⚠ cache runs out {0}', at) : t('cache until {0}', at);
}

/** The tooltip's cache line, or null. @param {import('./parse').Item} item @param {Date} [now] */
function cacheLine(item, now = new Date()) {
  const c = cacheState(item, now);
  if (!c) return null;
  const at = localTime(c.until.toISOString(), now);
  if (c.expired) return t('Prompt cache: ran out at {0}; a reply reads the whole conversation again.', at);
  return c.warn ? t('Prompt cache: runs out at {0}. Reply before then to reuse it.', at)
    : t('Prompt cache: warm until {0}; a reply before then reuses it.', at);
}

/**
 * @typedef {{runnerAlive?: boolean, now?: Date, maxLimitWaits?: number | null, lane?: string | null,
 *   groups?: boolean, limitHeld?: boolean, parallel?: import('./parse').Lane['parallel']}} Ctx
 *   `parallel` (2026-10-29, integrate round 8): the item's queue's live parallel.json (lanes --json), for why a
 *   task runs alone (aloneReason)
 *   `limitHeld` (2026-10-27): the item's queue is on a usage-limit hold (lib/limithold.js)
 *   `lane` (2026-10-15): the item's queue, shown when it is not the main one;
 *   `groups` (2026-10-21): sort the tree's items into status groups (view.itemList)
 */

/**
 * "Task", "Goal" or "Plan" (a plan-profile task).
 * @param {import('./parse').Item} item
 */
function kindWord(item) {
  if (isGoal(item)) return 'Goal';
  return item.profile === 'plan' ? 'Plan' : 'Task';
}

/** @param {string | null} error @param {string} prefix */
const after = (error, prefix) => (error && error.startsWith(prefix) ? error.slice(prefix.length).trim() : null);

/**
 * One or two sentences: what is happening now and what happens next (SPEC_HOVER 2.1 table).
 * @param {import('./parse').Item} item
 * @param {Ctx} [ctx]
 */
function statusSentence(item, ctx = {}) {
  const now = ctx.now || new Date();
  const at = (/** @type {string | null} */ iso) => localTime(iso, now) || t('an unknown time');
  const goal = isGoal(item);
  const rounds = item.rounds || 0;
  const max = roundsCap(item.max_rounds);
  const runnerWord = ctx.runnerAlive
    ? t('The runner is running.')
    : t('The runner is not running — start it from the ▶ button.');
  switch (item.status) {
    case 'pending':
      if (goal && (item.goal_started_at || rounds > 0)) {
        return `${t('Between rounds. Round {0} starts when the runner reaches it.', rounds + 1)} ${runnerWord}`;
      }
      if (item.session_id) return `${t('Will resume its earlier session when the runner reaches it.')} ${runnerWord}`;
      // (2026-10-27, U4, 5.5) tasks at once: why it does not start yet ("waits for #id8")
      // (2026-10-29, integrate round 8) + why the task it waits for runs alone, when the runner knows it
      if (waitsText(item.waits, now)) {
        const why = ctx.parallel ? ctx.parallel.why : null;
        return `${t('Waiting in line: {0}.', /** @type {string} */ (waitsText(item.waits, now, why)))} ${runnerWord}`;
      }
      return `${t('Waiting in line. Starts when the runner reaches it.')} ${runnerWord}`;
    case 'running':
      // (2026-10-27, U4, 8.10) tasks at once: its copy is being made, or its finished work is merged back
      if (item.phase === 'preparing') return t('Sleeping Clanker is preparing a private copy of the project for this task.');
      if (item.phase === 'merging') return t('Claude finished; Sleeping Clanker is merging the changes into the project.');
      if (item.phase === 'merge-waiting') return t('Claude finished; the merge waits: {0}.', codeText(MERGE_WAIT_TEXTS, item.merge && item.merge.wait) || t('no reason given'));
      {
        // (2026-10-29, integrate round 8; D5 NOT DONE, spec 2.7) a task that works alone in the project folder: why
        const alone = aloneReason(item, ctx.parallel);
        const aloneLine = alone === null ? ''
          : ` ${alone ? t('It runs alone in the project folder: {0}.', alone) : t('It runs alone in the project folder.')}`;
        if (goal) return `${t('Claude is working on round {0} of {1} now.', rounds + 1, max)}${aloneLine}`;
        return `${t('Claude is working on this now (attempt {0}, started {1}).', item.attempts || 1, at(item.started_at))}${aloneLine}`;
      }
    case 'waiting': {
      const until = localTime(item.wait_until, now);
      // (2026-10-27, limit_continue fix LOW-4) `run --on-limit ask` held the queue: nothing resumes by itself
      if (ctx.limitHeld) return t('Claude hit the usage limit, and the queue is on hold until you answer. "Continue after reset" in the notification, or "Resume Queue (Release Hold)" in the Queue view, resumes this same session at {0}.', until || t('the reset time'));
      const base = t('Claude hit the usage limit. Sleeping Clanker resumes this same session at {0}. Nothing to do.', until || t('the reset time'));
      return ctx.runnerAlive ? base : `${base} (${t('The runner is not running — start it from the ▶ button.')})`;
    }
    case 'paused':
      return t('Paused by you. Nothing runs until you press Resume. It will continue as {0}.', t(item.paused_from === 'waiting' ? 'waiting' : 'pending'));
    case 'done':
      if (goal) return t(rounds === 1 ? 'Goal reported complete at {0}, after {1} round.' : 'Goal reported complete at {0}, after {1} rounds.', at(item.finished_at), rounds);
      return t('Finished at {0}.', at(item.finished_at));
    case 'failed': {
      if (goal) {
        const blocked = after(item.error, 'blocked:');
        if (blocked !== null) return t('Claude says it is blocked: {0}. Fix it, then Retry.', clip(blocked, TOOLTIP_TEXT_MAX));
        const cap = after(item.error, 'goal cap:');
        if (cap !== null) return t('Goal stopped: {0}. Retry continues the same goal.', cap);
        if (item.error && item.error.startsWith('no goal marker')) {
          return t('Goal stopped: {0}. Retry continues the same goal.', item.error);
        }
      }
      return t('Stopped with an error at {0}. Use Retry to run it again; the session is kept.', at(item.finished_at));
    }
    case 'cancelled':
      // (2026-10-27, U4, 3.3) its merge had already started: the changes are in the project; Undo is offered
      if (cancelledButMerged(item)) return t('Cancelled, but its changes were already merged. The Task view offers Undo.');
      return t('Cancelled by you.');
    case 'blocked': {
      // (2026-10-27, U4, 8.6) a merge question: its card (in the Task view) has the choices
      const card = item.blocked_question && item.blocked_question.kind === 'merge' ? mergeCardModel(item) : null;
      if (card) return t('The merge needs you: {0} Open the Task view to choose.', card.title);
      // (2026-10-15, SPEC_V03 5.1) plain text: the tooltip is never Markdown
      const reason = after(item.error, 'blocked:');
      // (2026-10-20, 0.4.4 logfix B) Retry on a blocked item opens the same Reply/profile picker
      return `${t('Claude is stuck and needs you: {0}.', clip(reason || item.error || t('no reason given'), TOOLTIP_TEXT_MAX))} `
        + t('Use Reply (or Retry) to answer; both let you pick another profile for the next attempt.')
        + (needsCommand(item) ? ` ${t('It may need a command: Retry with shell-dev runs the commands on this project\'s allow-list (Edit allowed commands…; an empty list = shell-read); Retry with full runs any command, with no permission checks.')}` : '');
    }
    default:
      return t('Unknown status "{0}".', item.status);
  }
}

/** @param {number} v */
const money = (v) => `$${v.toFixed(v < 1 ? 3 : 2)}`;

/** @param {number} v */
const hours = (v) => (Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100));

/**
 * (2026-10-21, add panel, docs/SPEC_V049_ADD_PANEL.md) `provider · model · effort` of a task's OWN
 * choices (each only when set); null = it runs on the runner's provider, model and effort.
 * @param {{provider?: string | null, model?: string | null, effort?: string | null} | null | undefined} item
 */
function runOnText(item) {
  if (!item) return null;
  const parts = [item.provider, item.model, item.effort].filter((v) => typeof v === 'string' && v);
  if (item.ultracode === true) parts.push('ultracode'); // (2026-10-26, modes)
  return parts.length ? parts.join(' · ') : null;
}

/**
 * The item explanation (SPEC_HOVER 2.1). `full` = unclipped (Show Details, 2.3).
 * @param {import('./parse').Item} item
 * @param {Ctx} [ctx]
 * @param {{full?: boolean}} [opts]
 */
function itemExplanation(item, ctx = {}, opts = {}) {
  const full = !!opts.full;
  const cut = (/** @type {string} */ s, /** @type {number} */ max) => (full ? s : clip(s, max));
  // (2026-10-27, i18n) every line in the UI language; the status word too (the profile name is an id)
  const head = `${t(kindWord(item))} · ${item.status === 'blocked' ? `${t(item.status)} — ${t(BLOCKED_LABEL)}` : t(item.status)}`;
  const lines = [head, statusSentence(item, ctx), ''];
  const profile = item.profile || 'safe';
  const line = PROFILE_LINES[/** @type {keyof typeof PROFILE_LINES} */ (profile)];
  lines.push(t('Profile {0}: {1}', profile, line ? t(line) : t('unknown profile.')));
  // (2026-10-21, task model) only a task's own model; none = the runner's
  // (2026-10-21, add panel) and its own provider and effort, as `provider · model · effort`
  const runOn = runOnText(item);
  if (runOn) lines.push(t("Model {0} (this task's own; Provider, Model, Effort… to switch)", runOn));
  /** @type {string[]} */
  const stats = [t('Attempts {0}', item.attempts || 0)];
  const maxWaits = ctx.maxLimitWaits;
  stats.push(t('limit waits {0}', `${item.limit_waits || 0}${maxWaits != null ? `/${maxWaits}` : ''}`));
  if (item.num_turns != null) stats.push(t('turns {0}', item.num_turns));
  // (2026-10-20) an amount only for API billing: a subscription run's cost is an estimate nobody pays
  const cost = item.total_cost_usd != null ? item.total_cost_usd : item.cost_usd;
  if (item.billing === 'api' && cost != null) stats.push(t('cost {0} (API)', money(cost)));
  else if (item.billing === 'subscription') stats.push(t('subscription'));
  const replies = item.replies || [];
  if (replies.length) stats.push(t('Replies: {0}', replies.length)); // (2026-10-15, SPEC_V03 5.2)
  lines.push(stats.join(' · '));
  if (ctx.lane && ctx.lane !== 'default') lines.push(t('Queue: {0}', laneLabel(ctx.lane)));
  if (isGoal(item)) {
    const parts = [t('Goal: round {0}/{1}', item.rounds || 0, roundsCap(item.max_rounds))];
    const started = localStamp(item.goal_started_at);
    parts.push(started ? t('started {0}', started) : t('not started yet'));
    if (item.max_hours != null) parts.push(t('cap {0} h', hours(item.max_hours)));
    lines.push(parts.join(' · '));
  }
  const att = item.attachments || [];
  if (att.length) {
    lines.push(t('Attached: {0} file(s)/folder(s)', att.length));
    const shown = full ? att : att.slice(0, TOOLTIP_ATTACH_MAX);
    for (const a of shown) lines.push(`  - ${a}`);
    if (shown.length < att.length) lines.push(`  ${t('… and {0} more', att.length - shown.length)}`);
  }
  lines.push('', t('Prompt:'), cut(String(item.prompt || ''), TOOLTIP_PROMPT_MAX), '');
  lines.push(t('Folder: {0}', item.cwd || '-'));
  lines.push(t('Session: {0}', item.session_id || t('not started yet')));
  const cache = cacheLine(item, ctx.now); // (2026-10-21)
  if (cache) lines.push(cache);
  if (item.error) lines.push(t('Last error: {0}', cut(item.error, TOOLTIP_TEXT_MAX)));
  else if (item.result) lines.push(t('Last result: {0}', cut(item.result, TOOLTIP_TEXT_MAX)));
  if (full && replies.length) {
    // (2026-10-15, SPEC_V03 5.2) Show Details lists every reply, as the live log does
    for (const r of replies) {
      lines.push('', t('── Owner reply · {0} ──', (r.at && localStamp(r.at)) || '?'), r.text);
    }
  }
  return lines.join('\n');
}

/**
 * The reply's profile QuickPick (SPEC_V03 5.2): "keep" first, then the other profiles; when the item
 * is blocked and its reason mentions a shell, `shell-read` goes first. (0.4.1) `opts.shell`: the
 * chosen option asks for a shell (optionWantsShell), which also puts `shell-read` first.
 * (2026-10-19, 0.4.2 L2) When `opts.shell` is given (a CHOSEN option), it alone decides: a non-shell
 * option keeps the current profile first even if the question text mentions a shell. Without it
 * (a free-text reply) the reason rule applies as before.
 * (2026-10-21, v049 tester fix MED-5) A task on its own key provider is never offered a profile the core
 * refuses for it (argv.providerRefusal: shell-read, shell-dev, full): Retry, Reply… and the Task view's
 * dropdown all use this list, so none of them offers or preselects one.
 * @param {{status: string, error?: string | null, profile?: string | null, provider?: string | null}} item
 * @param {string[]} profiles
 * @param {{shell?: boolean}} [opts]
 * @returns {{label: string, description: string, profile: string | null}[]}
 */
function replyProfileChoices(item, profiles, opts = {}) {
  const current = item.profile || 'safe';
  // (2026-10-27, i18n) the labels and profile lines in the UI language (`profile` stays the id)
  const line = (/** @type {string} */ p) => {
    const text = PROFILE_LINES[/** @type {'safe'} */ (p)];
    return text ? t(text) : '';
  };
  const keep = { label: t('Keep {0}', current), description: line(current), profile: null };
  const others = profiles.filter((p) => p !== current && !providerRefusal(item.provider, p))
    .map((p) => ({ label: p, description: line(p), profile: p }));
  const wantsShell = typeof opts.shell === 'boolean' ? opts.shell : SHELL_RE.test(String(item.error || ''));
  // (2026-10-26, owner 22536a8c) only a profile without a shell: shell-read first narrowed full/shell-dev
  const shellFirst = item.status === 'blocked' && wantsShell && NO_SHELL_PROFILES.includes(current);
  if (!shellFirst) return [keep, ...others];
  const shell = others.filter((o) => o.profile === 'shell-read');
  return [...shell, keep, ...others.filter((o) => o.profile !== 'shell-read')];
}

/**
 * (2026-10-20, 0.4.4 logfix A) A blocked item whose reason or question asks for a command, and whose
 * profile is not shell-dev yet: the Task view and the tooltip suggest "Retry with shell-dev".
 * (2026-10-21, owner) Never for `full`: it can already run any command.
 * (2026-10-21, v049 merge) Never for a task on its own key provider: `retry --profile shell-dev` is
 * refused for it (argv.providerRefusal, cli._check_run_choice).
 * @param {{status: string, error?: string | null, profile?: string | null, blocked_question?: {text: string} | null,
 *   provider?: string | null}} item
 */
function needsCommand(item) {
  if (!item || item.status !== 'blocked' || ['shell-dev', 'full'].includes(item.profile || 'safe')) return false;
  if (providerRefusal(item.provider, 'shell-dev')) return false;
  const q = item.blocked_question && typeof item.blocked_question.text === 'string' ? item.blocked_question.text : '';
  return NEEDS_COMMAND_RE.test(`${item.error || ''} ${q}`);
}

/**
 * (2026-10-20, 0.4.4 logfix C) The notice when the last attempts blocked with the same question.
 * @param {{status: string, repeat_block?: number}} item
 * @returns {string | null}
 */
function repeatBlockNotice(item) {
  return item && item.status === 'blocked' && (item.repeat_block || 0) > 0 ? t(REPEAT_BLOCK_TEXT) : null; // (2026-10-27, i18n)
}

// ---- (2026-10-19, 0.4.1) structured blocked questions -----------------------------------------
// An option whose label or detail mentions one of these resumes with `shell-read` preselected.
const OPTION_SHELL_RE = /\b(shell|bash|powershell|terminal|command line|git|komen\w*|komento\w*)\b/i;

/**
 * The options of a blocked item's `blocked_question` (lib/parse.js normalizeQuestion), recommended
 * first, then in the core's order; [] when the item is not blocked or has no valid option list (old
 * core, free-text block): every caller then behaves exactly as before 0.4.1.
 * @param {{status: string, blocked_question?: {text: string, options: {n: number, label: string, detail: string, recommended: boolean}[]} | null} | null | undefined} item
 * @returns {{n: number, label: string, detail: string, recommended: boolean}[]}
 */
function questionOptions(item) {
  const q = item && item.status === 'blocked' ? item.blocked_question : null;
  const opts = q && Array.isArray(q.options) && q.options.length >= 2 ? q.options : [];
  return [...opts.filter((o) => o.recommended), ...opts.filter((o) => !o.recommended)];
}

/** @param {{label: string, detail?: string}} o */
function optionWantsShell(o) {
  return OPTION_SHELL_RE.test(`${o.label} ${o.detail || ''}`);
}

/**
 * (2026-10-19, 0.4.2 L2) The profile an option button sends while the owner has not picked one in
 * the Task view's dropdown: `shell-read` for a shell-flagged option (unless the item already runs
 * with it), else null = keep the item's profile. The question text never decides it.
 * @param {{profile?: string | null}} item @param {{label: string, detail?: string}} o
 * @returns {string | null}
 */
function optionProfile(item, o) {
  // (2026-10-26, owner 22536a8c) only a profile WITHOUT a shell moves up: a full (or shell-dev) task
  // whose option said "terminal" was narrowed to shell-read by one click and lost its commands
  return optionWantsShell(o) && NO_SHELL_PROFILES.includes(item.profile || 'safe') ? 'shell-read' : null;
}

/** The reply an option sends. @param {{n: number, label: string}} o */
function optionReplyText(o) {
  return `Option ${o.n}: ${o.label}`;
}

/**
 * The option n of the CURRENT item, or null (not blocked, no options, or no such n).
 * @param {Parameters<typeof questionOptions>[0]} item @param {unknown} n
 */
function chooseOption(item, n) {
  if (!Number.isInteger(n)) return null;
  return questionOptions(item).find((o) => o.n === n) || null;
}

/**
 * `Option <n>: <label>` for option n of the item, or null when it is not offered.
 * @param {Parameters<typeof questionOptions>[0]} item @param {unknown} n
 */
function choiceReplyText(item, n) {
  const o = chooseOption(item, n);
  return o ? optionReplyText(o) : null;
}

/**
 * Reply…'s first QuickPick for a blocked item with options: the options (recommended first, marked
 * "$(star) Recommended"), then "Write my own reply…" (id 'own'). [] = no options: the input box as before.
 * @param {Parameters<typeof questionOptions>[0]} item
 * @returns {{label: string, description: string, detail: string, n: number | null, shell: boolean}[]}
 */
function replyOptionPicks(item) {
  const opts = questionOptions(item);
  if (!opts.length) return [];
  // (2026-10-27, i18n) the picker in the UI language; the reply sent stays choiceReplyText's English
  // "Option N: label" (Claude reads it), never this label
  return [
    ...opts.map((o) => ({ label: t('Option {0}: {1}', o.n, o.label), description: o.recommended ? `$(star) ${t('Recommended')}` : '',
      detail: o.detail, n: o.n, shell: optionWantsShell(o) })),
    { label: `$(edit) ${t('Write my own reply…')}`, description: '', detail: '', n: null, shell: false },
  ];
}

/**
 * (2026-10-20) Show Details' raw fields: `cost_usd`/`total_cost_usd` only when the run was billed to
 * the API (the core keeps them in its JSON for the budget cap).
 * @param {import('./parse').Item} item
 */
function detailFields(item) {
  const raw = item.raw || {};
  if (item.billing === 'api') return raw;
  const { cost_usd: _c, total_cost_usd: _t, ...rest } = raw;
  return rest;
}

/** Plain-text tree tooltip (never Markdown). @param {import('./parse').Item} item @param {Ctx} [ctx] */
function itemTooltip(item, ctx = {}) {
  return itemExplanation(item, ctx, { full: false });
}

/**
 * Icon for an item (or a bare status, as in 0.1): paused = debug-pause; a goal = target, colored
 * by its status; otherwise per status.
 * @param {string | {status: string, kind?: string}} itemOrStatus
 */
function itemIcon(itemOrStatus) {
  const item = typeof itemOrStatus === 'string' ? { status: itemOrStatus } : itemOrStatus;
  const byStatus = ICONS[/** @type {keyof typeof ICONS} */ (item.status)] || ICONS.unknown;
  if (item.status !== 'paused' && item.status !== 'blocked' && isGoal(item)) return { id: GOAL_ICON, color: byStatus.color };
  return byStatus;
}

// (2026-10-21) the kind emblem before the tree description, so a goal or a plan is told apart from a
// normal edit task at a glance; the left icon stays the status
const KIND_EMBLEMS = Object.freeze({ Task: '✏️', Goal: '🎯', Plan: '💡' });

/** ✏️ a normal (edit) task, 🎯 a goal, 💡 a plan-profile task. @param {import('./parse').Item} item */
function itemEmblem(item) {
  return KIND_EMBLEMS[/** @type {keyof typeof KIND_EMBLEMS} */ (kindWord(item))];
}

/** contextValue drives the per-status context menu (`viewItem =~ /^item\.(failed|...)(\.edit)?$/`).
 * (2026-10-21, tester round 2 L-5) `.edit` marks an item Edit Task… can change (argv.canEdit or
 * argv.canEditReply: not running, and no session yet unless done, or a reply Claude has not got), so
 * the menu never offers an edit that always fails.
 * @param {{status: string, session_id?: string | null, pending_reply?: string | null}} item */
function itemContext(item) {
  const reply = typeof item.pending_reply === 'string' && !!item.pending_reply.trim();
  // (2026-10-26, edit paused) a started paused task too: argv.canEditRestart
  const restart = item.status === 'paused' && !!item.session_id && /** @type {any} */ (item).kind !== 'goal';
  const editable = item.status !== 'running' && (reply || restart || (item.status !== 'done' && !item.session_id));
  return `item.${item.status}${editable ? '.edit' : ''}`;
}

// ---- the goal bar (SPEC_GOALS 5.2, SPEC_HOVER 2.2) -------------------------------------------
const GOAL_BAR_STATUSES = ['running', 'waiting', 'paused'];

/**
 * The goal shown in the goal bar: running, waiting, paused, or pending once started
 * (`goal_started_at` set). With several, the OLDEST start wins.
 * @param {import('./parse').Item[]} items
 * @returns {import('./parse').Item | null}
 */
function activeGoal(items) {
  const goals = (items || []).filter((i) => isGoal(i) && (GOAL_BAR_STATUSES.includes(i.status)
    || (i.status === 'pending' && !!i.goal_started_at)));
  if (!goals.length) {
    // (2026-10-15, SPEC_V03 5.2) with no active goal, a goal that needs the owner (the newest) is
    // shown, so its Reply entry is one click away.
    const blocked = (items || []).filter((i) => isGoal(i) && i.status === 'blocked');
    if (!blocked.length) return null;
    const fin = (/** @type {import('./parse').Item} */ i) => (toDate(i.finished_at) || new Date(0)).getTime();
    return blocked.slice().sort((a, b) => fin(b) - fin(a))[0];
  }
  const key = (/** @type {import('./parse').Item} */ i) => {
    const d = toDate(i.goal_started_at) || toDate(i.created_at);
    return d ? d.getTime() : Number.MAX_SAFE_INTEGER;
  };
  return goals.slice().sort((a, b) => key(a) - key(b))[0];
}

/**
 * `$(target) Pursuing goal: <first 40 chars> · 2d 5h 21m 33s`; paused: `$(debug-pause) Goal paused: …`;
 * blocked (2026-10-15): `$(error) Goal needs you: …` with no ticking time.
 * @param {import('./parse').Item} item
 * @param {number} nowMs
 */
function goalBarText(item, nowMs) {
  const label = itemLabel(item, GOAL_BAR_TEXT_MAX);
  if (item.status === 'blocked') return `$(error) ${t('Goal needs you: {0}', label)}`;
  const started = toDate(item.goal_started_at);
  const elapsed = started ? ` · ${formatElapsed(nowMs - started.getTime())}` : '';
  return item.status === 'paused'
    ? `$(debug-pause) ${t('Goal paused: {0}', label)}${elapsed}`
    : `$(target) ${t('Pursuing goal: {0}', label)}${elapsed}`;
}

/**
 * Goal bar tooltip: no ticking values, so it changes only when the polled data changes.
 * @param {import('./parse').Item} item
 * @param {Ctx} [ctx]
 */
function goalBarTooltip(item, ctx = {}) {
  const goalText = clip(String(item.prompt || ''), GOAL_TOOLTIP_TEXT_MAX);
  // (2026-10-27, i18n) every line in the UI language; no start = "not started yet" (was "started not yet")
  const lines = [t('Goal: {0}', goalText), ''];
  const started = localStamp(item.goal_started_at);
  lines.push(`${t('Round {0}/{1}', item.rounds || 0, roundsCap(item.max_rounds))} · ${started ? t('started {0}', started) : t('not started yet')}`);
  lines.push(t('Caps: {0} rounds, {1} h · profile {2} · {3}', roundsCap(item.max_rounds),
    item.max_hours != null ? hours(item.max_hours) : '?', item.profile || 'safe', t(item.status)));
  if (ctx.lane) lines.push(t('Queue: {0}', laneLabel(ctx.lane))); // (2026-10-15, SPEC_V03 2.3)
  lines.push(statusSentence(item, ctx));
  if (item.error) lines.push(t('Last error: {0}', clip(item.error, TOOLTIP_TEXT_MAX)));
  lines.push('', t(item.status === 'blocked' ? 'Click: Reply, Open Task View, Delete, Expand'
    : 'Click: Pause/Resume, Open Task View, Delete, Expand'));
  return lines.join('\n');
}

// ---- tasks at once (2026-10-27, U4, SPEC_V05_PARALLEL 8.6/8.9/8.10) --------------------------------------
// Every text comes from a code the core gives (phase, waits, why, conflict kind, action, gate); the English
// text is the i18n key, so ru/fi come from lib/i18n.js. An unknown code (a newer core) shows a neutral text.

/** list --json `phase` (the statuses keep their own words). */
const PHASE_TEXTS = Object.freeze({
  queued: 'queued',
  preparing: 'preparing a copy',
  working: 'working',
  merging: 'merging',
  'merge-waiting': 'waiting to merge',
  'needs-you': 'needs you',
});
/** merge.wait: why a finished task's merge waits. */
const MERGE_WAIT_TEXTS = Object.freeze({
  'repo-busy': 'the project is in the middle of a git merge or rebase',
  'low-disk': 'not enough free disk space',
  'merge-error': 'the merge failed; it is tried again',
  'changed-during-merge': 'the project changed during the merge',
});
/** merge.kind / a merge question's code: a short word for the tree. */
const CONFLICT_SHORT_TEXTS = Object.freeze({
  content: 'merge conflict',
  markers: 'conflict markers left',
  link: 'link conflict',
  case: 'file name case conflict',
  'branch-switched': 'the branch changed',
  'changed-during-merge': 'the project changed during the merge',
  'repo-busy': 'git is busy in the project',
  'nested-repo': 'a nested repository',
});
/** The merge card's title per conflict kind ({0}/{1}: branch-switched from/to; {0}: nested-repo copy). */
const CONFLICT_TITLE_TEXTS = Object.freeze({
  content: "The task's changes conflict with newer changes in the project.",
  markers: 'The merged files still contain conflict markers.',
  link: 'A link (symlink or junction) changed both in the task and in the project.',
  case: 'Two file names differ only in upper/lower case.',
  'branch-switched': 'The project switched from branch {0} to {1} while the task ran.',
  'changed-during-merge': 'The project changed while the merge ran.',
  'repo-busy': 'Finished, but not merged yet: the project is in the middle of a git merge or rebase.',
  'nested-repo': 'The changes touch a nested git repository ({0}).',
});
/** order.start_block codes: why a pending task does not start yet ({0} = #id8, or a time). */
const WAITS_TEXTS = Object.freeze({
  'waits-after': 'waits for #{0}',
  'held-by-parent': 'waits for #{0} (its parent)',
  family: 'waits for #{0} (same family)',
  goal: 'waits for goal #{0}',
  'alone-barrier': 'waits: #{0} runs alone first',
  'prepare-backoff': 'waits: its copy could not be made; tries again at {0}',
  'limit-gate': 'waits for the usage limit to reset',
  hold: 'waits: the queue is on hold',
});
/** parallel.json `why`: why fewer than N tasks run. */
const WHY_TEXTS = Object.freeze({
  'limit-gate': 'no new starts until the usage limit resets',
  ramp: 'starting one at a time after a usage limit',
  forecast: 'fewer at once: the usage limit is near',
  'alone-barrier': 'the next task runs alone',
  'waits-after': 'the next tasks wait for others to finish',
  family: 'parts of one task run one after another',
  goal: 'one goal at a time',
  hold: 'the queue is on hold',
  'prepare-backoff': 'a copy could not be made; trying again soon',
  'nothing-pending': 'nothing else is waiting',
});
/** Gate codes (runner-wide reason + per task): why a task runs alone in the project folder. */
const GATE_TEXTS = Object.freeze({
  'git-missing': 'Git is not installed',
  'git-old': 'Git is older than 2.40',
  'shadow-broken': 'the copies store is damaged',
  'no-root': 'the queue has no project folder',
  'root-missing': 'the project folder is missing',
  unc: 'the project is on a network path',
  'too-many-files': 'the project has more than 20,000 files',
  'too-big': 'the project is larger than 1 GiB',
  'slow-listing': 'listing the project files is too slow',
  'low-disk': 'not enough free disk space for a copy',
  'long-path': 'a file path in the copy would be too long',
  'nested-repo': 'the project contains a nested git repository',
  'link-in-tree': 'the project contains links (symlinks or junctions)',
  'cwd-outside': "the task's folder is outside the project",
  'wt-root-denied': "the task's profile denies the copies folder",
  // (2026-10-29, delta H1, C-M1)
  'cwd-excluded': "the task's folder is left out of copies (the project ignores it)",
  'wt-root-granted': 'your Claude settings let tasks write in the copies folder',
});
/** Merge card buttons (merge --<action>); let-claude first gets "(recommended)". */
const MERGE_ACTION_TEXTS = Object.freeze({
  'let-claude': 'Let Claude fix it',
  retry: 'Try the merge again',
  'take-task': "Take the task's version",
  discard: "Discard the task's changes",
  now: 'Merge now',
  undo: 'Undo the merge',
});
const MERGE_CARD_PATHS = 20; // == worktree.QUESTION_PATHS_MAX
const BEAT_STALE_MS = 60 * 1000; // spec 1.9: a beat older than 60 s = "runner not responding"

/** @param {Record<string, string>} table @param {unknown} code @returns {string | null} */
const codeText = (table, code) => (typeof code === 'string' && Object.prototype.hasOwnProperty.call(table, code) ? t(table[code]) : null);
/** @param {unknown} id */
const id8 = (id) => (typeof id === 'string' ? id.slice(0, 8) : '?');

/**
 * (2026-10-27, U4, 8.10) An item's tasks-at-once phase in words: preparing a copy, working, merging, waiting to
 * merge: <reason>, needs you: <conflict>. null = no phase (an older core) or a plain status.
 * @param {import('./parse').Item} item
 */
function phaseText(item) {
  const phase = item ? item.phase : null;
  const merge = item ? item.merge : null;
  if (phase === 'merge-waiting') {
    const why = codeText(MERGE_WAIT_TEXTS, merge && merge.wait);
    return why ? t('waiting to merge: {0}', why) : t('waiting to merge');
  }
  if (phase === 'needs-you') {
    const what = codeText(CONFLICT_SHORT_TEXTS, merge && merge.kind);
    return what ? t('needs you: {0}', what) : t('needs you');
  }
  return codeText(PHASE_TEXTS, phase);
}

/**
 * (2026-10-27, U4, 5.5/5.8) Why a pending item does not start yet: "waits for #id8", …; null = it may start.
 * (2026-10-29, integrate round 8) `why`: the live runner's parallel.json `why`; when it names the same task that
 * runs alone first (alone-barrier, args.id8) with a reason, the reason follows in parentheses.
 * @param {{code: string, args?: Record<string, string>} | null | undefined} waits @param {Date} [now]
 * @param {{code: string, args?: Record<string, string>} | null | undefined} [why]
 */
function waitsText(waits, now, why) {
  if (!waits || typeof waits.code !== 'string') return null;
  const a = waits.args || {};
  const code = /** @type {keyof typeof WAITS_TEXTS} */ (waits.code);
  if (code === 'not-pending' || !Object.prototype.hasOwnProperty.call(WAITS_TEXTS, code)) return code === 'not-pending' ? null : t('waits');
  // {0}: the retry time (prepare-backoff), else #id8 (limit-gate and hold have none)
  const arg = code === 'prepare-backoff' ? localTime(a.until || null, now) || t('an unknown time') : id8(a.id8);
  if (code === 'alone-barrier' && why && why.code === 'alone-barrier' && why.args && a.id8
    && why.args.id8 === a.id8 && aloneNoteText(why.args.reason)) {
    return t('waits: #{0} runs alone first ({1})', arg, /** @type {string} */ (aloneNoteText(why.args.reason)));
  }
  return t(WAITS_TEXTS[code], arg);
}

/**
 * (2026-10-27, U4, 1.9) Why fewer than N tasks run (parallel.json `why`), or the in-place reason; null = none.
 * (2026-10-29, integrate round 8) alone-barrier + args.reason: why the next task runs alone.
 * @param {{code: string, args?: Record<string, string>} | null | undefined} why
 */
function whyText(why) {
  if (!why || typeof why.code !== 'string') return null;
  const reason = why.code === 'alone-barrier' && why.args ? aloneNoteText(why.args.reason) : null;
  if (reason) return t('the next task runs alone ({0})', reason);
  return codeText(WHY_TEXTS, why.code) || codeText(GATE_TEXTS, why.code);
}

/** (2026-10-27, U4, 2.7) A gate code (why a task or the whole runner works in the project folder). @param {unknown} code */
function gateText(code) {
  return codeText(GATE_TEXTS, code);
}

/**
 * (2026-10-29, integrate round 8; D5 NOT DONE, spec 2.7) The core's note on why ONE task runs alone (scheduler.py:
 * "could not make a copy: <gate code or kind>") in words: a known gate code is translated, any other note is shown
 * as the core wrote it. null = no note.
 * @param {unknown} note @returns {string | null}
 */
function aloneNoteText(note) {
  if (typeof note !== 'string' || !note.trim()) return null;
  const m = /^could not make a copy: (.+)$/.exec(note.trim());
  return m ? t('its copy could not be made: {0}', gateText(m[1]) || m[1]) : note.trim();
}

/**
 * (2026-10-29, integrate round 8; D5 NOT DONE, spec 2.7) Why a running task works alone in the project folder,
 * from the live runner's parallel.json (the task's slot: mode "alone" and its note) and the task. null = it does
 * not run alone (or no live parallel runner); '' = alone for a reason the runner does not say.
 * @param {import('./parse').Item} item @param {Ctx['parallel']} [parallel] @returns {string | null}
 */
function aloneReason(item, parallel) {
  if (!item || !parallel || item.status !== 'running') return null;
  const slot = parallel.slots.find((s) => s.item === item.id);
  if (!slot || slot.mode !== 'alone') return null;
  const note = aloneNoteText(slot.note);
  if (note) return note;
  if (item.in_place) return t('you set it to run alone');
  if (parallel.mode === 'in-place') return gateText(parallel.reason) || '';
  return '';
}

/**
 * (2026-10-27, U4, 8.6) A merge card button label; `recommended` adds "(recommended)" (let-claude first).
 * @param {string} action @param {boolean} [recommended]
 */
function mergeActionLabel(action, recommended = false) {
  const text = codeText(MERGE_ACTION_TEXTS, action) || t('Unknown action');
  return recommended ? t('{0} (recommended)', text) : text;
}

/**
 * (2026-10-27, U4, 1.9/8.10) The parallel runner's heartbeat is older than 60 s (or unreadable).
 * @param {{beat?: string | null} | null | undefined} parallel @param {Date} [now]
 */
function runnerNotResponding(parallel, now = new Date()) {
  if (!parallel) return false;
  const beat = toDate(parallel.beat || null);
  return !beat || now.getTime() - beat.getTime() > BEAT_STALE_MS;
}

/**
 * (2026-10-27, U4, 8.10) The lane line: "Tasks at once: 3 (2 working, 1 merging)" + why fewer run, the in-place
 * reason, or "runner not responding". null = no parallel runner (and an older core).
 * @param {import('./parse').Lane} lane @param {Date} [now]
 */
function parallelLine(lane, now = new Date()) {
  const p = lane && lane.parallel;
  if (!p || !lane.runnerAlive) return null;
  if (runnerNotResponding(p, now)) return t('Tasks at once: {0} (runner not responding)', p.n);
  /** @type {string[]} */
  const counts = [];
  const working = p.slots.filter((s) => s.phase === 'attempt').length;
  const preparing = p.slots.length - working;
  if (working) counts.push(t('{0} working', working));
  if (preparing) counts.push(t('{0} preparing', preparing));
  if (p.merging.length) counts.push(t('{0} merging', p.merging.length));
  const head = counts.length ? t('Tasks at once: {0} ({1})', p.n, counts.join(', ')) : t('Tasks at once: {0}', p.n);
  /** @type {string[]} */
  const tail = [];
  if (p.mode === 'in-place') {
    const reason = gateText(p.reason);
    tail.push(reason ? t('one at a time in the project folder: {0}', reason) : t('one at a time in the project folder'));
  }
  const busy = p.slots.length + p.merging.length;
  const why = busy < p.n ? whyText(p.why) : null;
  if (why) tail.push(why);
  return [head, ...tail].join(' · ');
}

/**
 * (2026-10-29, delta M1) The note on files no merge could carry back (merge.not_merged: the app's own copy excludes
 * left them out) and where the copy is kept; null = none. Paths only, at most 20 named + "… and N more".
 * @param {import('./parse').Item | null | undefined} item
 * @returns {string | null}
 */
function notMergedText(item) {
  const m = item ? item.merge : null;
  const paths = m && Array.isArray(m.not_merged) ? m.not_merged : [];
  if (!m || !paths.length) return null;
  const n = Number.isInteger(m.not_merged_n) && /** @type {number} */ (m.not_merged_n) >= paths.length ? /** @type {number} */ (m.not_merged_n) : paths.length;
  const shown = paths.slice(0, MERGE_CARD_PATHS);
  const list = shown.join(', ') + (n > shown.length ? ` ${t('… and {0} more', n - shown.length)}` : '');
  const kept = m.kept || '?';
  return n === 1
    ? t('1 file could not be merged back because copies leave it out: {0}; it is kept in the copy {1}', list, kept)
    : t('{0} files could not be merged back because copies leave them out: {1}; they are kept in the copy {2}', n, list, kept);
}

/** (2026-10-27, U4, 3.3) Cancelled after its merge already ran (Undo is offered). @param {import('./parse').Item} item */
function cancelledButMerged(item) {
  return !!item && item.status === 'cancelled' && !!item.merge && item.merge.state === 'merged';
}

/**
 * (2026-10-27, U4, 8.6) The merge card of an item, or null: a conflict question (blocked, kind merge), a held merge
 * (Merge now / Discard) or a merged one with a backup (Undo). Paths: at most 20 + "n more". `copy` = the item has
 * a private copy the owner may open. Text only: the webview renders it, the host checks every answer (chatview).
 * @param {import('./parse').Item | null | undefined} item
 * @returns {{kind: 'conflict' | 'held' | 'merged', code: string, title: string, paths: string[], more: number,
 *   actions: {action: string, label: string, recommended: boolean}[], gen: number, preview: boolean, copy: boolean} | null}
 */
function mergeCardModel(item) {
  if (!item) return null;
  const q = item.blocked_question;
  const merge = item.merge;
  const copy = !!(item.worktree && item.worktree.path && item.worktree.state !== 'removed');
  /** @param {string[]} actions */
  const buttons = (actions) => actions.map((action, i) => {
    const recommended = i === 0 && action === 'let-claude';
    return { action, label: mergeActionLabel(action, recommended), recommended };
  });
  if (item.status === 'blocked' && q && q.kind === 'merge' && Number.isInteger(q.gen)) {
    const args = q.args || { paths: [], n: 0 };
    const code = q.code || '';
    // {0}/{1}: branch-switched from/to; {0}: the nested-repo copy; the other titles have none
    const fill = code === 'branch-switched' ? [args.from || '?', args.to || '?'] : [args.copy || '?'];
    const title = Object.prototype.hasOwnProperty.call(CONFLICT_TITLE_TEXTS, code)
      ? t(CONFLICT_TITLE_TEXTS[/** @type {keyof typeof CONFLICT_TITLE_TEXTS} */ (code)], ...fill)
      : t('The task\'s changes could not be merged.');
    const paths = args.paths.slice(0, MERGE_CARD_PATHS);
    return { kind: 'conflict', code, title, paths, more: Math.max(0, (args.n || 0) - paths.length),
      actions: buttons(q.actions || []), gen: /** @type {number} */ (q.gen), preview: !!(merge && merge.preview), copy };
  }
  if (!merge) return null;
  const changed = merge.changed.slice(0, MERGE_CARD_PATHS);
  const more = Math.max(0, merge.changed.length - changed.length);
  if (merge.state === 'held') {
    return { kind: 'held', code: 'held', title: t("The task's changes wait in its copy; they are not merged yet."),
      paths: changed, more, actions: buttons(['now', 'discard']), gen: merge.gen, preview: false, copy };
  }
  if (merge.state === 'merged' && merge.backup) {
    const title = cancelledButMerged(item) ? t('Cancelled, but its changes were already merged.') : t("The task's changes were merged into the project.");
    return { kind: 'merged', code: 'merged', title, paths: changed, more, actions: buttons(['undo']), gen: merge.gen, preview: false, copy: false };
  }
  return null;
}

module.exports = {
  PHASE_TEXTS,
  MERGE_WAIT_TEXTS,
  CONFLICT_SHORT_TEXTS,
  CONFLICT_TITLE_TEXTS,
  WAITS_TEXTS,
  WHY_TEXTS,
  GATE_TEXTS,
  MERGE_ACTION_TEXTS,
  BEAT_STALE_MS,
  phaseText,
  waitsText,
  whyText,
  gateText,
  aloneNoteText, // (2026-10-29, integrate round 8)
  aloneReason, // (2026-10-29, integrate round 8)
  mergeActionLabel,
  runnerNotResponding,
  parallelLine,
  cancelledButMerged,
  mergeCardModel,
  notMergedText, // (2026-10-29, delta M1)
  runOnText,
  LABEL_MAX,
  TOOLTIP_TEXT_MAX,
  TOOLTIP_PROMPT_MAX,
  TOOLTIP_ATTACH_MAX,
  GOAL_BAR_TEXT_MAX,
  ICONS,
  PROFILE_LINES,
  BLOCKED_LABEL,
  laneLabel,
  laneStateParts,
  laneStateLine,
  statusBarBackground,
  statusBarColor,
  statusBarSegments,
  STATUS_SEGMENTS,
  stoppedRunnerPending,
  heldQueues,
  timeLeft, // (2026-10-26, limit_continue)
  leftSuffix,
  replyProfileChoices,
  FULL_WARN,
  FULL_BADGE,
  REPEAT_BLOCK_TEXT,
  needsCommand,
  repeatBlockNotice,
  questionOptions,
  optionWantsShell,
  optionProfile,
  optionReplyText,
  chooseOption,
  choiceReplyText,
  replyOptionPicks,
  localTime,
  CACHE_WARM_MS,
  CACHE_WARN_MS,
  cacheState,
  cacheDescription,
  cacheLine,
  localStamp,
  formatElapsed,
  statusBarText,
  statusBarTooltip,
  itemLabel,
  itemDescription,
  statusSentence,
  itemExplanation,
  itemTooltip,
  detailFields,
  itemIcon,
  KIND_EMBLEMS,
  itemEmblem,
  itemContext,
  activeGoal,
  goalBarText,
  goalBarTooltip,
};

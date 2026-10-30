// @ts-check
// (2026-10-26, limit_continue) The usage-limit hold: a runner started with `run --on-limit ask` (the
// default while claudAutopilot.autoContinueAfterLimit is off) parks the item at a usage limit, holds
// its queue (hold.json reason "limit" + the reset time) and exits. The poll sees the hold; this module
// decides, once per hold, what happens: with the setting on, the hold is released and the queue's
// runner started again (it waits out the reset, then resumes the same session); else ONE notification
// asks [Continue after reset] [Always (turn the setting on)] [Not now]. Pure apart from the deps the
// caller passes (extension.js: vscode, the CLI); no timers, no process.
'use strict';

const { t } = require('./i18n');
const format = require('./format');

const CONTINUE = 'Continue after reset';
const ALWAYS = 'Always (turn the setting on)';
const NOT_NOW = 'Not now';

/**
 * @typedef {{lane: string, dataDir: string, since: string | null, resetsAt: string | null, key: string,
 *   project?: string | null, label?: string}} LimitHold `project` (2026-10-27, fix LOW-5): where the poll saw
 *   the hold. `label` (2026-10-27, fix round 2): the queue's name for a runner of another project ("proj / lane")
 */

/**
 * The usage-limit holds of `lanes` that `asked` does not know yet; each one's key is added to `asked`
 * (once per hold: a hold put on again has another `since`, so it is asked again).
 * @param {import('./parse').Lane[] | null | undefined} lanes @param {Set<string>} asked
 * @param {string | null} [project] the lanes' project root (null = the shared queue)
 * @param {string} [label] (2026-10-27, fix round 2) names the queue in the question (another project)
 * @returns {LimitHold[]}
 */
function newLimitHolds(lanes, asked, project, label) {
  /** @type {LimitHold[]} */
  const out = [];
  for (const l of lanes || []) {
    const h = /** @type {any} */ (l.hold);
    if (!h || h.reason !== 'limit') continue;
    const key = `${l.dataDir}|${h.since || ''}`;
    if (asked.has(key)) continue;
    asked.add(key);
    out.push({ lane: l.name, dataDir: l.dataDir, since: h.since || null, resetsAt: h.resetsAt || null, key,
      ...(project === undefined ? {} : { project }), ...(label ? { label } : {}) });
  }
  return out;
}

/**
 * The notification's text: "Usage limit reached — resets at 23:01. Continue automatically then?"; with
 * several queues it names the queue. @param {LimitHold} h @param {boolean} many @param {Date} [now]
 */
function limitMessage(h, many, now) {
  const at = format.localTime(h.resetsAt, now) || '?';
  return many || h.label
    ? t('Sleeping Clanker: queue {0} reached the usage limit — resets at {1}. Continue automatically then?', h.label || format.laneLabel(h.lane), at)
    : t('Sleeping Clanker: Usage limit reached — resets at {0}. Continue automatically then?', at);
}

/** The buttons, in this order (translated). */
function limitButtons() {
  return [t(CONTINUE), t(ALWAYS), t(NOT_NOW)];
}

/**
 * Handle one new usage-limit hold.
 * @param {LimitHold} h
 * @param {{autoContinue: boolean, many?: boolean, now?: Date,
 *   ask: (message: string, buttons: string[]) => PromiseLike<string | undefined>,
 *   setAlways: () => PromiseLike<unknown>, resume: (h: LimitHold) => PromiseLike<unknown>,
 *   notNow?: (h: LimitHold) => unknown, event: (line: string) => void}} deps
 * @returns {Promise<'auto' | 'continue' | 'always' | 'later'>}
 */
async function handleLimitHold(h, deps) {
  const label = h.label || format.laneLabel(h.lane);
  if (deps.autoContinue) {
    deps.event(`usage-limit hold of ${label} (resets ${h.resetsAt || '?'}): autoContinueAfterLimit is on; releasing it`);
    await deps.resume(h);
    return 'auto';
  }
  const [cont, always] = limitButtons();
  const pick = await deps.ask(limitMessage(h, !!deps.many, deps.now), limitButtons());
  if (pick === always) {
    deps.event(`usage-limit hold of ${label}: "Always" — claudAutopilot.autoContinueAfterLimit turned on (User settings)`);
    await deps.setAlways();
    await deps.resume(h);
    return 'always';
  }
  if (pick === cont) {
    deps.event(`usage-limit hold of ${label}: "Continue after reset" (resets ${h.resetsAt || '?'})`);
    await deps.resume(h);
    return 'continue';
  }
  deps.event(`usage-limit hold of ${label}: not now; the queue stays on hold`);
  if (deps.notNow) deps.notNow(h);
  return 'later';
}

module.exports = { CONTINUE, ALWAYS, NOT_NOW, newLimitHolds, limitMessage, limitButtons, handleLimitHold };

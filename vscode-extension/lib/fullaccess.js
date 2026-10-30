// @ts-check
// (2026-10-26, owner fa388b7b) The idle reminder for FULL ACCESS. Pure: no vscode API.
// "if you haven't used that queue for 4h-unlimited days, warn before sending to a full-access task":
// the owner's last use of a queue is the newest task or reply they queued in it (`created_at`,
// `replies[*].at`); the runner's own activity and automatic answers (`replies[*].by`) do not count. extension.js (confirmIdleFull) asks once
// before a reply to a full task, or before a new task that gets the confirmed full default for +, when
// that use is older than claudAutopilot.fullAccessIdleConfirm ('4h', '1d', '7d'; 'never' = never ask).
'use strict';

const HOUR_MS = 3600 * 1000;
/** @type {Readonly<Record<string, number>>} */
const IDLE_MS = Object.freeze({ '4h': 4 * HOUR_MS, '1d': 24 * HOUR_MS, '7d': 7 * 24 * HOUR_MS });
/** the setting's value in words (an English i18n key) */
const IDLE_WORDS = Object.freeze({ '4h': '4 hours', '1d': '1 day', '7d': '7 days' });

/** An ISO time in ms, or null. @param {unknown} v */
function timeMs(v) {
  if (typeof v !== 'string' || !v) return null;
  const n = Date.parse(v);
  return Number.isNaN(n) ? null : n;
}

/**
 * The newest time the owner queued a task or a reply in these items, or null (none at all).
 * (2026-10-29, integrate round 9; SEC-L22 owner "Kysy aina full-tehtävissä") an automatic answer is not the owner's
 * use: a reply stored with `by` (Autorun `autorun`, Auto-pick best `autopick`; `reply/merge --by`) is skipped, so a
 * chain of automatic answers never keeps an idle queue "used" and the idle FULL ACCESS pause still comes.
 * @param {readonly any[] | null | undefined} items one queue's items (lib/parse Item)
 * @returns {number | null}
 */
function lastOwnerUse(items) {
  /** @type {number | null} */
  let last = null;
  for (const it of items || []) {
    if (!it || typeof it !== 'object') continue;
    const replies = (Array.isArray(it.replies) ? it.replies : []).filter((/** @type {any} */ r) => r && typeof r === 'object' && !r.by);
    for (const v of [it.created_at, ...replies.map((/** @type {any} */ r) => r.at)]) {
      const ms = timeMs(v);
      if (ms !== null && (last === null || ms > last)) last = ms;
    }
  }
  return last;
}

/**
 * Should a send to a full-access task in this queue ask first? `ask` = the setting is on and the owner's
 * last use is older than it (or there is none). `last` = that use (ms) or null. `confirmed`: when the
 * owner last said yes to this question for the queue (ms; the next poll may not show that send yet).
 * @param {{items: readonly any[] | null | undefined, now: number, idle: string, confirmed?: number | null}} input
 * @returns {{ask: boolean, last: number | null, limitMs: number | null}}
 */
function idleCheck(input) {
  const limitMs = Object.prototype.hasOwnProperty.call(IDLE_MS, input.idle) ? IDLE_MS[input.idle] : null;
  if (limitMs === null) return { ask: false, last: null, limitMs: null }; // 'never'
  const used = lastOwnerUse(input.items);
  const yes = typeof input.confirmed === 'number' && Number.isFinite(input.confirmed) ? input.confirmed : null;
  const last = used === null ? yes : yes === null ? used : Math.max(used, yes);
  return { ask: last === null || input.now - last > limitMs, last, limitMs };
}

module.exports = { IDLE_MS, IDLE_WORDS, lastOwnerUse, idleCheck };

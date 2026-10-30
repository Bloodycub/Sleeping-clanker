// @ts-check
// Double-click detection for tree items (2026-10-15, docs/SPEC_V03.md 1.3). A TreeView has no
// double-click event, so every click runs `claudAutopilot.itemClick`, which records (key, time); a
// second click on the SAME item within 500 ms is a double-click. Pure: the caller passes the time.
'use strict';

const DOUBLE_CLICK_MS = 500;

/**
 * @param {{key: string, at: number} | null} last the previous click, or null
 * @param {string} key the clicked item
 * @param {number} now ms
 * @param {number} [ms]
 */
function isDoubleClick(last, key, now, ms = DOUBLE_CLICK_MS) {
  return !!last && last.key === key && now - last.at >= 0 && now - last.at <= ms;
}

class ClickTracker {
  /** @param {number} [ms] */
  constructor(ms = DOUBLE_CLICK_MS) {
    this.ms = ms;
    /** @type {{key: string, at: number} | null} */
    this.last = null;
  }

  /**
   * Record a click; true when it completes a double-click (a third click starts over).
   * @param {string} key @param {number} now
   */
  click(key, now) {
    if (isDoubleClick(this.last, key, now, this.ms)) {
      this.last = null;
      return true;
    }
    this.last = { key, at: now };
    return false;
  }
}

module.exports = { DOUBLE_CLICK_MS, isDoubleClick, ClickTracker };

// @ts-check
// (2026-10-26, owner: "terminal status in the queue") The live activity line of a running task, like
// the Claude Code terminal shows it: `Read(view.js)`, `Bash(pytest -q)`, `Thinking…`. No vscode API:
// extension.js asks once per poll; this module reads only the last TAIL_BYTES of the newest
// `<runDir>/<n>.jsonl`, and nothing at all while that file's size and mtime are unchanged.
'use strict';

const fs = require('fs');
const path = require('path');
const chatmodel = require('./chatmodel');

const TAIL_BYTES = 64 * 1024;
const ATTEMPT_RE = /^(\d{1,6})\.jsonl$/;

/**
 * What the last event of a run says Claude is doing now, or null (nothing yet, or it finished).
 * @param {(Record<string, any> | null)[]} events slim events (chatmodel.parseLine), oldest first
 * @returns {string | null}
 */
function activityOf(events) {
  // (2026-10-26, integrate) one implementation: the Task view's activity line (chatmodel.liveActivity)
  // decides the step, so the Queue row and the Task view always say the same thing
  return chatmodel.liveActivity(events).step;
}

/** The newest attempt file of a run folder, or null. @param {string} runDir */
function newestAttempt(runDir) {
  let best = -1;
  try {
    for (const name of fs.readdirSync(runDir)) {
      const m = ATTEMPT_RE.exec(name);
      if (m && Number(m[1]) > best) best = Number(m[1]);
    }
  } catch (_e) {
    return null;
  }
  return best < 0 ? null : path.join(runDir, `${best}.jsonl`);
}

/** The complete lines in the last TAIL_BYTES of a file. @param {string} file @param {number} size */
function tailLines(file, size) {
  const len = Math.min(size, TAIL_BYTES);
  const buf = Buffer.alloc(len);
  const fd = fs.openSync(file, 'r');
  let got = 0;
  try {
    got = fs.readSync(fd, buf, 0, len, size - len);
  } finally {
    fs.closeSync(fd);
  }
  const lines = buf.subarray(0, got).toString('utf8').split('\n');
  if (size > len) lines.shift(); // cut mid-line
  return lines;
}

/** Activity lines per run folder, re-read only when the newest attempt file changed. */
class ActivityCache {
  constructor() {
    /** @type {Map<string, {file: string, size: number, mtime: number, line: string | null}>} */
    this.byDir = new Map();
  }

  /** @param {string} runDir @returns {string | null} */
  get(runDir) {
    const file = newestAttempt(runDir);
    if (!file) return null;
    /** @type {fs.Stats} */
    let st;
    try {
      st = fs.statSync(file);
    } catch (_e) {
      return null;
    }
    const old = this.byDir.get(runDir);
    if (old && old.file === file && old.size === st.size && old.mtime === st.mtimeMs) return old.line;
    let line = null;
    try {
      line = activityOf(tailLines(file, st.size).map((l) => chatmodel.parseLine(l)));
    } catch (_e) {
      line = null;
    }
    this.byDir.set(runDir, { file, size: st.size, mtime: st.mtimeMs, line });
    return line;
  }

  /** Forget run folders not asked for in this poll. @param {Set<string>} keep */
  prune(keep) {
    for (const k of this.byDir.keys()) if (!keep.has(k)) this.byDir.delete(k);
  }
}

module.exports = { TAIL_BYTES, activityOf, newestAttempt, ActivityCache };

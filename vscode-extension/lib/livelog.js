// @ts-check
// Incremental reading of an item's run files for the live log (2026-10-15, docs/SPEC_V03.md 1.3).
// No vscode API: extension.js owns the document, the watcher and the timer; this module only reads
// NEW bytes since the last offset (never the whole file again) and keeps at most the last 2 MB of
// jsonl per attempt, each line rendered once by lib/transcript.js when it arrives.
// (2026-10-17, 0.4.0) `opts.mapLine` replaces that renderer: the Task view keeps each line as one
// slim parsed event (lib/chatmodel.js parseLine) instead of text. The text renderer gets a per-attempt
// state (`lastText`) so a result that repeats the last answer is not printed twice.
'use strict';

const fs = require('fs');
const path = require('path');
const transcript = require('./transcript');

const WINDOW_BYTES = 2 * 1024 * 1024;
const HEAD_MAX = 256 * 1024; // the first line (system/init) is kept for the header after a cut
const STDERR_MAX = 64 * 1024;
const CHUNK = 1024 * 1024;
const ATTEMPT_RE = /^(\d{1,6})\.jsonl$/;

/** Count '\n' bytes in [start, end) of an open file, in chunks (no lines are kept). */
function countNewlines(/** @type {number} */ fd, /** @type {number} */ start, /** @type {number} */ end) {
  let n = 0;
  const buf = Buffer.alloc(Math.min(CHUNK, Math.max(1, end - start)));
  for (let pos = start; pos < end;) {
    const got = fs.readSync(fd, buf, 0, Math.min(buf.length, end - pos), pos);
    if (got <= 0) break;
    for (let i = 0; i < got; i += 1) if (buf[i] === 0x0a) n += 1;
    pos += got;
  }
  return n;
}

/**
 * (2026-10-26) Open for reading, or null. A run file can be deleted (remove, retry) or locked by
 * another process (EBUSY/EPERM, e.g. a virus scanner) between the guarded statSync and the open; the
 * throw used to escape the Task view's `ready` handler as an unhandled rejection and fail a whole
 * queue poll. @param {string} file @returns {number | null}
 */
function openOrNull(file) {
  try {
    return fs.openSync(file, 'r');
  } catch (_e) {
    return null;
  }
}

/** One `<attempt>.jsonl` (+ its stderr file), read incrementally. */
class AttemptReader {
  /**
   * @param {string} file the jsonl path
   * @param {number} attempt
   * @param {{windowBytes?: number, mapLine?: (line: string, ctx: any) => any}} [opts]
   */
  constructor(file, attempt, opts = {}) {
    this.file = file;
    this.attempt = attempt;
    this.windowBytes = opts.windowBytes || WINDOW_BYTES;
    this.mapLine = opts.mapLine || transcript.renderLine;
    /** @type {import('./transcript').RenderState} */
    this.state = transcript.newRenderState();
    this.offset = 0; // bytes consumed (through the last complete line)
    /** @type {Buffer} */
    this.partial = Buffer.alloc(0);
    /** @type {{bytes: number, rendered: any}[]} */
    this.window = [];
    this.windowSize = 0;
    this.skipped = 0;
    /** @type {string | null} */
    this.headLine = null;
    /** @type {Date | null} */
    this.startedAt = null;
    this.stderrFile = file.replace(/\.jsonl$/, '.stderr.txt');
    this.stderrSize = -1;
    this.stderr = '';
  }

  _ctx() {
    return { attempt: this.attempt, startedAt: this.startedAt, state: this.state };
  }

  /** @param {Buffer} raw one line without its '\n' */
  _push(raw) {
    const text = raw.toString('utf8');
    if (this.headLine === null && this.offset === 0 && this.skipped === 0 && this.window.length === 0) {
      this.headLine = text.slice(0, HEAD_MAX);
    }
    this.window.push({ bytes: raw.length + 1, rendered: this.mapLine(text, this._ctx()) });
    this.windowSize += raw.length + 1;
    while (this.windowSize > this.windowBytes && this.window.length > 1) {
      const old = /** @type {{bytes: number}} */ (this.window.shift());
      this.windowSize -= old.bytes;
      this.skipped += 1;
    }
  }

  /**
   * Read the bytes appended since the last call. `final` also takes a last line without '\n'.
   * @param {{final?: boolean}} [opts]
   * @returns {boolean} whether anything visible changed
   */
  read(opts = {}) {
    let changed = this._readStderr();
    /** @type {fs.Stats} */
    let st;
    try {
      st = fs.statSync(this.file);
    } catch (_e) {
      return changed;
    }
    if (!this.startedAt) this.startedAt = st.birthtime && st.birthtime.getTime() > 0 ? st.birthtime : st.mtime;
    if (st.size < this.offset) { // replaced or truncated: start over
      Object.assign(this, { offset: 0, partial: Buffer.alloc(0), window: [], windowSize: 0, skipped: 0, headLine: null,
        state: transcript.newRenderState() });
      changed = true;
    }
    if (st.size > this.offset) {
      const fd = openOrNull(this.file);
      if (fd === null) return changed; // (2026-10-26) locked or gone since the stat: the next refresh reads it
      try {
        let pos = this.offset;
        if (pos === 0 && st.size > this.windowBytes) {
          // First open of a big file: keep the first line, count (do not parse) the middle.
          const head = Buffer.alloc(Math.min(HEAD_MAX, st.size));
          const got = fs.readSync(fd, head, 0, head.length, 0);
          const nl = head.subarray(0, got).indexOf(0x0a);
          if (nl >= 0) {
            this.headLine = head.subarray(0, nl).toString('utf8');
            // (2026-10-20) mapped for its effect on this.state only: the init line's billing
            this.mapLine(this.headLine, this._ctx());
            const start = st.size - this.windowBytes;
            let cut = Math.max(nl + 1, start);
            // align to a line start: everything before the first '\n' at/after `cut - 1` is skipped
            const probe = Buffer.alloc(Math.min(CHUNK, st.size - cut + 1));
            const pgot = fs.readSync(fd, probe, 0, probe.length, cut - 1);
            const pnl = probe.subarray(0, pgot).indexOf(0x0a);
            if (pnl >= 0) cut = cut - 1 + pnl + 1;
            this.skipped = 1 + countNewlines(fd, nl + 1, cut); // the head line itself + the middle
            pos = cut;
          }
        }
        const buf = Buffer.alloc(Math.min(CHUNK, st.size - pos));
        while (pos < st.size) {
          const got = fs.readSync(fd, buf, 0, Math.min(buf.length, st.size - pos), pos);
          if (got <= 0) break;
          let data = Buffer.concat([this.partial, buf.subarray(0, got)]);
          let nl;
          while ((nl = data.indexOf(0x0a)) >= 0) {
            this._push(data.subarray(0, nl));
            data = data.subarray(nl + 1);
          }
          this.partial = Buffer.from(data);
          pos += got;
        }
        this.offset = pos;
        changed = true;
      } finally {
        fs.closeSync(fd);
      }
    }
    if (opts.final && this.partial.length) {
      this._push(this.partial);
      this.partial = Buffer.alloc(0);
      changed = true;
    }
    return changed;
  }

  _readStderr() {
    /** @type {number} */
    let size;
    try {
      size = fs.statSync(this.stderrFile).size;
    } catch (_e) {
      return false;
    }
    if (size === this.stderrSize) return false;
    const fd = openOrNull(this.stderrFile);
    if (fd === null) return false; // (2026-10-26) the size is not taken: the next refresh reads it again
    this.stderrSize = size;
    try {
      const len = Math.min(size, STDERR_MAX);
      const buf = Buffer.alloc(len);
      const got = fs.readSync(fd, buf, 0, len, size - len);
      this.stderr = buf.subarray(0, got).toString('utf8');
    } finally {
      fs.closeSync(fd);
    }
    return true;
  }

  /** @returns {import('./transcript').Attempt} */
  view() {
    return {
      attempt: this.attempt, startedAt: this.startedAt, headLine: this.headLine,
      rendered: this.window.map((w) => w.rendered), skipped: this.skipped, stderr: this.stderr,
      state: this.state, // (2026-10-20) billing + result outcome for transcript.renderAttempt
    };
  }
}

/** Every attempt of one item: `<runDir>/<n>.jsonl`, discovered on each refresh. */
class ItemLog {
  /** @param {string} runDir @param {{windowBytes?: number, mapLine?: (line: string, ctx: any) => any}} [opts] */
  constructor(runDir, opts = {}) {
    this.runDir = runDir;
    this.opts = opts;
    /** @type {Map<number, AttemptReader>} */
    this.readers = new Map();
  }

  /** @param {{final?: boolean}} [opts] @returns {boolean} changed */
  refresh(opts = {}) {
    let names = [];
    try {
      names = fs.readdirSync(this.runDir);
    } catch (_e) {
      return false;
    }
    let changed = false;
    for (const name of names) {
      const m = ATTEMPT_RE.exec(name);
      if (!m) continue;
      const n = Number(m[1]);
      if (!this.readers.has(n)) {
        this.readers.set(n, new AttemptReader(path.join(this.runDir, name), n, this.opts));
        changed = true;
      }
    }
    for (const r of this.readers.values()) if (r.read(opts)) changed = true;
    return changed;
  }

  /** Every attempt's view (`rendered` = what `mapLine` returned per line), in attempt order. */
  views() {
    return [...this.readers.values()].map((r) => r.view()).sort((a, b) => a.attempt - b.attempt);
  }

  /** @param {import('./transcript').Reply[]} [replies] */
  render(replies) {
    const attempts = this.views();
    return transcript.renderTranscript(attempts, { replies: replies || [] });
  }
}

module.exports = { WINDOW_BYTES, AttemptReader, ItemLog, countNewlines };

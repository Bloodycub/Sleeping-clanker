// @ts-check
// Child processes for the core CLI, without the vscode API (2026-10-13, SPEC_VSCODE sections 1, 3, 4).
// Only execFile/spawn with an args array and shell:false; no exec with a command string.
'use strict';

const childProcess = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { VERSION_PROBE, parseVersion, versionOk } = require('./argv');
// (2026-10-27, i18n fix) the error texts a toast shows (no Python found, temp cleanup) are in the UI language
const { t } = require('./i18n');

const CLI_TIMEOUT_MS = 60000;
/** (2026-10-17, 0.3.3) A quiet call slower than this is logged anyway, with a "slow" note. */
const SLOW_CALL_MS = 5000;
const MAX_BUFFER = 64 * 1024 * 1024;

/** @param {string} a */
const quote = (a) => (a === '' || /[\s"]/.test(a) ? JSON.stringify(a) : a);

/** @param {string} command @param {string[]} args */
function formatArgv(command, args) {
  return [command, ...args].map(quote).join(' ');
}

/** @param {Date} [d] */
function stamp(d = new Date()) {
  return d.toISOString();
}

/**
 * @typedef {{code: number | null, stdout: string, stderr: string, ms: number, spawnError: string | null}} CliResult
 */

/**
 * Run `<command> <args>` to completion and log `argv, exit code, elapsed ms` with a timestamp.
 * `display` is the argv shown in the log (prompt shortened). Never rejects.
 * (2026-10-17, 0.3.3) `quiet` (the poller's read-only calls): log ONLY on a non-zero exit, a spawn
 * error, or a call slower than SLOW_CALL_MS (then with a "slow" note); otherwise nothing.
 * @param {{command: string, args: string[], display?: string[], cwd: string,
 *   env: Record<string, string | undefined>, log: (line: string) => void, timeoutMs?: number,
 *   quiet?: boolean, slowMs?: number, execFile?: typeof childProcess.execFile}} o  `slowMs`: tests only
 * @returns {Promise<CliResult>}
 */
function runCli(o) {
  const execFile = o.execFile || childProcess.execFile;
  const t0 = Date.now();
  const shown = formatArgv(o.command, o.display || o.args);
  return new Promise((resolve) => {
    execFile(o.command, o.args, {
      cwd: o.cwd,
      env: o.env,
      shell: false,
      windowsHide: true,
      maxBuffer: MAX_BUFFER,
      timeout: o.timeoutMs || CLI_TIMEOUT_MS,
      encoding: 'utf8',
    }, (err, stdout, stderr) => {
      const ms = Date.now() - t0;
      /** @type {number | null} */
      let code = 0;
      /** @type {string | null} */
      let spawnError = null;
      if (err) {
        const e = /** @type {any} */ (err);
        if (typeof e.code === 'number') code = e.code;
        else {
          code = null;
          spawnError = e.killed ? `killed (${e.signal || 'timeout'})` : String(e.code || e.message);
        }
      }
      const slow = ms > (o.slowMs === undefined ? SLOW_CALL_MS : o.slowMs);
      if (!o.quiet || code !== 0 || slow) {
        o.log(`${stamp()} cli ${shown} -> exit=${code === null ? spawnError : code} ${ms}ms${o.quiet && slow ? ' (slow)' : ''}`);
      }
      resolve({ code, stdout: String(stdout || ''), stderr: String(stderr || ''), ms, spawnError });
    });
  });
}

/**
 * Pick the first interpreter >= 3.11. Rejects with every attempt's reason.
 * @param {{command: string, args: string[]}[]} candidates
 * @param {(c: {command: string, args: string[]}) => Promise<CliResult>} probe runs VERSION_PROBE
 * @returns {Promise<{python: {command: string, args: string[]}, version: number[]}>}
 */
async function detectPython(candidates, probe) {
  /** @type {string[]} */
  const reasons = [];
  for (const c of candidates) {
    const r = await probe(c);
    const label = formatArgv(c.command, c.args);
    if (r.code !== 0) {
      reasons.push(`${label}: ${r.spawnError || t('exit {0}', r.code)} ${r.stderr.trim().slice(0, 200)}`.trim());
      continue;
    }
    const v = parseVersion(r.stdout);
    if (!versionOk(v)) {
      reasons.push(t('{0}: version {1} is older than 3.11', label, v ? v.join('.') : '?'));
      continue;
    }
    return { python: c, version: /** @type {number[]} */ (v) };
  }
  throw new Error(t('no Python >= 3.11 found ({0})', reasons.join('; ')));
}

/** argv for the version probe. @param {{command: string, args: string[]}} c */
function versionProbeArgs(c) {
  return [...c.args, '-c', VERSION_PROBE];
}

/**
 * Write a long prompt to a private temp file; returns its path. Remove it with removeTempPrompt.
 * @param {string} text
 * @param {string} [baseDir]
 */
function writeTempPrompt(text, baseDir = os.tmpdir()) {
  const dir = fs.mkdtempSync(path.join(baseDir, 'claud-autopilot-'));
  const file = path.join(dir, 'prompt.md');
  fs.writeFileSync(file, text, { encoding: 'utf8', mode: 0o600 });
  return file;
}

/** Remove a temp prompt file and its private directory; errors are returned, not swallowed.
 * @param {string} file @returns {string | null} error text */
function removeTempPrompt(file) {
  try {
    fs.rmSync(path.dirname(file), { recursive: true, force: true });
    return null;
  } catch (err) {
    return t('could not delete temp prompt {0}: {1}', file, err instanceof Error ? err.message : String(err));
  }
}

/**
 * Split a byte/char stream into lines (CRLF or LF); `flush` emits a trailing partial line.
 * @param {(line: string) => void} onLine
 */
function lineSplitter(onLine) {
  let buf = '';
  return {
    /** @param {string | Buffer} chunk */
    push(chunk) {
      buf += String(chunk);
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        onLine(buf.slice(0, i).replace(/\r$/, ''));
        buf = buf.slice(i + 1);
      }
    },
    flush() {
      if (buf) onLine(buf.replace(/\r$/, ''));
      buf = '';
    },
  };
}

/**
 * Spawn the long-running `run --forever` child; stdout/stderr lines go to `onLine`.
 * @param {{command: string, args: string[], cwd: string, env: Record<string, string | undefined>,
 *   onLine: (line: string) => void, onExit: (code: number | null, signal: string | null) => void,
 *   onError: (err: Error) => void, spawn?: typeof childProcess.spawn}} o
 */
function spawnRunner(o) {
  const spawn = o.spawn || childProcess.spawn;
  const child = spawn(o.command, o.args, {
    cwd: o.cwd, env: o.env, shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const out = lineSplitter(o.onLine);
  const err = lineSplitter(o.onLine);
  if (child.stdout) { child.stdout.setEncoding('utf8'); child.stdout.on('data', (c) => out.push(c)); }
  if (child.stderr) { child.stderr.setEncoding('utf8'); child.stderr.on('data', (c) => err.push(c)); }
  child.on('error', (e) => o.onError(e));
  child.on('close', (code, signal) => {
    out.flush();
    err.flush();
    o.onExit(code, signal);
  });
  return child;
}

/** Absolute taskkill.exe, so a planted taskkill in cwd/PATH is never run. */
function taskkillPath(env = process.env) {
  return path.join(env.SystemRoot || env.SYSTEMROOT || 'C:\\Windows', 'System32', 'taskkill.exe');
}

/** `taskkill /PID <pid> /T /F` argv. @param {number} pid */
function taskkillArgs(pid) {
  if (!Number.isInteger(pid) || pid <= 0) throw new Error(`invalid pid ${pid}`);
  return ['/PID', String(pid), '/T', '/F'];
}

module.exports = {
  CLI_TIMEOUT_MS,
  SLOW_CALL_MS,
  formatArgv,
  stamp,
  runCli,
  detectPython,
  versionProbeArgs,
  writeTempPrompt,
  removeTempPrompt,
  lineSplitter,
  spawnRunner,
  taskkillPath,
  taskkillArgs,
};

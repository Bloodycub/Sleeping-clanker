// @ts-check
// The Task view's locked-down webview shell and its message gate (2026-10-17, 0.4.0,
// docs/SPEC_V04_CHAT.md 2). Pure: no vscode API. The host (lib/taskview.js) sets the HTML once per
// panel load; everything after that is postMessage. Every message from the webview goes through
// validateMessage: only known types with the expected shapes survive, anything else is dropped.
'use strict';

const crypto = require('crypto');
const path = require('path');
const { PROFILES, ID_RE, LANE_RE, DEFAULT_LANE, QUICK_PROMPTS_MAX, QUICK_PROMPT_MAX,
  validAgentsValue } = require('./argv');
const { escapeHtml, codeBlocksOf } = require('./markdown');
const i18n = require('./i18n'); // (2026-10-27, i18n) the "not available" page's texts (pure, like this file)

const REPLY_MAX = 100000;
const HREF_MAX = 2048;

/** A fresh nonce for one render (base64url of 18 random bytes). */
function makeNonce() {
  return crypto.randomBytes(18).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * The Content-Security-Policy: nothing by default, our styles, ONLY the nonce'd script, images and
 * fonts from the extension (or data: images). No remote origin, no 'unsafe-eval', no inline script.
 * @param {string} cspSource webview.cspSource @param {string} nonce
 */
function csp(cspSource, nonce) {
  return `default-src 'none'; style-src ${cspSource} 'unsafe-inline'; script-src 'nonce-${nonce}'; `
    + `img-src ${cspSource} data:; font-src ${cspSource};`;
}

/**
 * The panel HTML: an empty shell; media/chat.js builds everything from the host's messages.
 * (0.4.3) `ident` = which item this panel shows (id, project root or null, queue); media/chat.js
 * keeps exactly that in the webview state, so the panel can be restored after a window reload
 * (validateRestoreState + the core decide whether it still exists).
 * (2026-10-21, UI language) `lang` = the UI language; `strings` = its translations (English text ->
 * text), put in `data-i18n` for media/chat.js; none (English) = no attribute.
 * @param {{cspSource: string, nonce: string, scriptUri: string, styleUri: string, title: string,
 *   ident?: {itemId: string, project: string | null, lane: string}, lang?: string,
 *   strings?: Record<string, string> | null}} o
 */
function panelHtml(o) {
  const nonce = String(o.nonce);
  if (!/^[A-Za-z0-9_-]{16,}$/.test(nonce)) throw new Error('bad nonce');
  const id = o.ident;
  const attrs = (id ? ` data-item="${escapeHtml(id.itemId)}" data-lane="${escapeHtml(id.lane)}"`
    + (id.project ? ` data-project="${escapeHtml(id.project)}"` : '') : '')
    + (o.strings && Object.keys(o.strings).length ? ` data-i18n="${escapeHtml(JSON.stringify(o.strings))}"` : '');
  const lang = /^[a-z]{2}$/.test(String(o.lang || '')) ? o.lang : 'en';
  return `<!DOCTYPE html>
<html lang="${lang}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp(String(o.cspSource).replace(/["<>]/g, ''), nonce)}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${escapeHtml(o.styleUri)}">
<title>${escapeHtml(o.title)}</title>
</head>
<body${attrs}>
<header id="hdr"></header>
<main id="log" aria-live="polite"></main>
<footer id="composer"></footer>
<script nonce="${nonce}" src="${escapeHtml(o.scriptUri)}"></script>
</body>
</html>
`;
}

// (2026-10-27, i18n) the "not available" page's inline style (kept out of the page's text)
const UNAVAILABLE_STYLE = 'body{font-family:var(--vscode-font-family);color:var(--vscode-foreground);padding:24px}p.why{opacity:.75}';

/**
 * (0.4.3) The page of a restored Task view whose item cannot be shown any more: static text, no
 * script, no resource (CSP `default-src 'none'`, inline style only).
 * (2026-10-27, i18n) The texts are in the UI language (i18n.t) and `lang` is that language, as for
 * panelHtml; `reason` comes translated from the caller.
 * @param {string} reason @param {string} [lang] the UI language (default: i18n's)
 */
function unavailableHtml(reason, lang) {
  const want = String(lang || i18n.language());
  const l = /^[a-z]{2}$/.test(want) ? want : 'en';
  return `<!DOCTYPE html>
<html lang="${l}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${escapeHtml(i18n.t('Task view'))}</title>
<style>${UNAVAILABLE_STYLE}</style>
</head>
<body>
<h3>${escapeHtml(i18n.t('This task is no longer available'))}</h3>
<p>${escapeHtml(i18n.t('The Task view could not be restored after the window reload.'))}</p>
<p class="why">${escapeHtml(String(reason || '').slice(0, 300))}</p>
<p>${escapeHtml(i18n.t('Close this tab; double-click the task in the Queue view to open it again.'))}</p>
</body>
</html>
`;
}

const PROJECT_MAX = 4096;

/**
 * (0.4.3) The webview state of a Task view after a window reload, structurally checked. Returns
 * {itemId, project, lane} or null. This is ONLY the shape check: the host then asks the core
 * whether that project is known, the queue exists and the item is in it (TaskViews.restore).
 * - itemId: the core's id pattern (hex, 1–32);
 * - lane: the core's queue-name pattern (absent = `default`);
 * - project: null/absent/'' = the shared queue; else an absolute path with no control character and
 *   no `.` or `..` segment, at most 4096 chars (never used as a path: only compared with the roots
 *   the core lists).
 * @param {unknown} raw
 * @returns {{itemId: string, project: string | null, lane: string} | null}
 */
function validateRestoreState(raw) {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const s = /** @type {Record<string, unknown>} */ (raw);
  if (typeof s.itemId !== 'string' || !ID_RE.test(s.itemId)) return null;
  const lane = s.lane === undefined || s.lane === null ? DEFAULT_LANE : s.lane;
  if (typeof lane !== 'string' || !LANE_RE.test(lane)) return null;
  let project = null;
  if (s.project !== undefined && s.project !== null && s.project !== '') {
    const p = s.project;
    if (typeof p !== 'string' || p.length > PROJECT_MAX || /[\u0000-\u001f\u007f]/.test(p)) return null;
    if (!(path.win32.isAbsolute(p) || path.posix.isAbsolute(p))) return null;
    if (p.split(/[\\/]/).some((seg) => seg === '..' || seg === '.')) return null;
    project = p;
  }
  return { itemId: s.itemId, project, lane };
}

/**
 * (2026-10-20, 0.4.6) The chat's own "Auto-pick best" box out of the same (untrusted) webview state:
 * `true`/`false` = the owner set it in THAT chat and it survives the reload; anything else
 * (absent, null, a string, …) = `null` = follow `claudAutopilot.autoPickBestOption`. Kept separate
 * from validateRestoreState, like `draft`, so a junk value can never cost the panel its restore.
 * @param {unknown} raw
 * @returns {boolean | null}
 */
function restoreAutoPick(raw) {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const v = /** @type {Record<string, unknown>} */ (raw).autoPick;
  return typeof v === 'boolean' ? v : null;
}

/**
 * The https URL to open, or null (only https:, no credentials, ≤ 2048 chars).
 * @param {unknown} href
 */
function httpsOnly(href) {
  if (typeof href !== 'string' || !href || href.length > HREF_MAX) return null;
  let u;
  try {
    u = new URL(href);
  } catch (_e) {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password) return null;
  return u.toString();
}

/**
 * A profile from the webview: null (keep the item's), a known profile, or undefined (invalid).
 * @param {unknown} v
 * @returns {string | null | undefined}
 */
function profileOf(v) {
  const p = v === undefined || v === null ? '' : v;
  if (typeof p !== 'string' || (p !== '' && !PROFILES.includes(p))) return undefined;
  return p || null;
}

// (2026-10-21) a code block's Copy / Run in terminal buttons: the block's text and its fence language
const CODE_MAX = 100000;
const LANG_RE = /^[\w#+.-]{0,32}$/;

/**
 * (2026-10-21) Is `text` exactly one fenced code block of the conversation the host sent? `sigs` are
 * the JSON blocks of PanelState.sent; lib/markdown.js writes a block as <code…>escaped</code></pre>.
 * @param {Iterable<string>} sigs @param {string} text
 */
function codeShown(sigs, text) {
  const want = `>${escapeHtml(text)}</code></pre>`;
  for (const sig of sigs) {
    let b;
    try {
      b = JSON.parse(sig);
    } catch (_e) {
      continue;
    }
    if (b && typeof b.html === 'string' && b.html.includes(want)) return true;
  }
  return false;
}

// (2026-10-21, security review L7) characters that make the shown code differ from what runs:
// zero-width (U+200B-U+200D, U+2060-U+2064, U+FEFF, soft hyphen), bidi marks/embeddings/isolates
// (U+200E/F, U+061C, U+202A-U+202E, U+2066-U+2069) and the C0/C1 controls except tab and newline.
// (2026-10-26, security I4) + the blank-looking Hangul fillers (U+115F, U+1160, U+3164, U+FFA0) and the
// Mongolian vowel separator (U+180E)
const HIDDEN_CODE_RE = /[\u200B-\u200F\u2060-\u2064\uFEFF\u00AD\u061C\u202A-\u202E\u2066-\u2069\u0000-\u0008\u000B-\u001F\u007F-\u009F\u115F\u1160\u3164\uFFA0\u180E]/g;

/**
 * (2026-10-21, security review L7) A code block as it will run: CRLF -> LF, the hidden characters
 * above removed, trailing blanks trimmed. `removed` counts what was taken out; `multiline` = more than
 * one line (pasted only, never run with Enter).
 * @param {string} text
 * @returns {{text: string, removed: number, multiline: boolean}}
 */
function cleanCode(text) {
  // (2026-10-21, tester round 2 L-1) U+2028/U+2029 are line breaks: they become "\n" (so the block
  // counts as several lines), and the invisible Unicode tag characters U+E0000-U+E007F are removed
  const unix = String(text || '').replace(/\r\n?|[\u2028\u2029]/g, '\n');
  const first = unix.replace(HIDDEN_CODE_RE, '').replace(TAG_CHARS_RE, '');
  // (2026-10-27, SEC-L14) then every other format (Cf) or control (Cc) code point but tab and newline, the
  // variation selectors and the invisible letters/marks (MORE_HIDDEN_RE)
  const more = (first.match(MORE_HIDDEN_RE) || []).length;
  const hidden = (unix.match(HIDDEN_CODE_RE) || []).length + (unix.match(TAG_CHARS_RE) || []).length + more;
  const clean = first.replace(MORE_HIDDEN_RE, '').replace(/\s+$/, '');
  return { text: clean, removed: hidden, multiline: clean.includes('\n') };
}
const TAG_CHARS_RE = /[\u{E0000}-\u{E007F}]/gu;
// (2026-10-27, SEC-L14) the rest of what renders as nothing: \p{Cf} (e.g. U+0600-U+0605, U+06DD, U+070F,
// U+08E2, U+110BD, U+1BCA0-U+1BCA3, U+1D173-U+1D17A, U+FFF9-U+FFFB), \p{Cc} but tab and newline, the variation
// selectors (U+FE00-U+FE0F, U+E0100-U+E01EF), the combining grapheme joiner U+034F, the Khmer inherent vowels
// U+17B4/U+17B5, the Mongolian free variation selectors U+180B-U+180D/U+180F and the braille blank U+2800
// (2026-10-27, SEC-L14 review) + every Default_Ignorable_Code_Point, the unassigned ones too (U+2065,
// U+FFF0-U+FFF8, U+E0080-U+E00FF, U+E01F0-U+E0FFF): HarfBuzz draws nothing for them either
// (2026-10-27, SEC-L14 review 2) + U+1D159 MUSICAL SYMBOL NULL NOTEHEAD (So): a glyph with no ink in many fonts
// (2026-10-27, SEC-L14 review 4) + the nonspacing marks (Mn) that are fillers, selectors or joiners and draw
// nothing: U+16FE4 KHITAN SMALL SCRIPT FILLER, U+1BC9D DUPLOYAN THICK LETTER SELECTOR, U+2D7F TIFINAGH CONSONANT
// JOINER, U+1107F BRAHMI NUMBER JOINER, U+11A47/U+11A99 the Zanabazar Square/Soyombo subjoiners and U+11F42 KAWI
// CONJOINER (every Mn whose Unicode 15 name says FILLER, SELECTOR or JOINER; the others are the variation
// selectors and Default_Ignorable ones above). Every Cf is gone already (\p{Cf})
const MORE_HIDDEN_RE = /(?![\t\n])[\p{Cf}\p{Cc}\p{Default_Ignorable_Code_Point}\ufe00-\ufe0f\u{E0100}-\u{E01EF}\u034f\u17b4\u17b5\u180b-\u180d\u180f\u2800\u{1D159}\u2d7f\u{1107f}\u{11a47}\u{11a99}\u{11f42}\u{16fe4}\u{1bc9d}]/gu;

/**
 * (2026-10-27, SEC-L11) The env of a terminal Run in terminal / Run silently / an auto-run creates: VS Code's
 * own env would hand it every key the window has, so each variable with a secret-shaped name (SECRET_ENV_RE)
 * is removed (null = delete, VS Code's TerminalOptions.env), and NoDefaultCurrentDirectoryInExePath=1 makes
 * cmd.exe stop running a git.cmd or npm.bat planted in the task's folder (the terminal's cwd) instead of the
 * real program. Names only: no value is read or kept.
 * (2026-10-27, SEC-L11 review 3) Also a variable whose VALUE carries a credential (SECRET_ENV_VALUE_RE, as the
 * core's is_secret_env): the value is only tested, never copied into the result.
 * @param {Record<string, string | undefined>} env usually process.env
 * @returns {Record<string, string | null>}
 */
function terminalEnv(env) {
  /** @type {Record<string, string | null>} */
  const out = {};
  for (const name of Object.keys(env || {})) {
    if (isTerminalSecret(name, env[name])) out[name] = null;
  }
  out.NoDefaultCurrentDirectoryInExePath = '1';
  return out;
}
// (2026-10-27, SEC-L11 review) secret-shaped variable names, the core's word rule (SEC-M8, providers.py): a
// "word" is a run of letters/digits between `_`, `-`, `.` (or the ends).
// (2026-10-27, SEC-L11 review 3) the rule is S6's round-3 rule (providers.py SECRET_ENV_NAME_RE, COUNT_TOKENS_RE,
// SECRET_ENV_VALUE_RE, PROXY_ENV_VARS, is_secret_env), ported as is, not an older copy: a name is secret when a
// word is PW, PAT or CRED(S); ends in PWD(S), DSN(S) or PASS (not ASKPASS, BYPASS, COMPASS); holds SECRET,
// PASSWORD, PASSWD, PASSPHRASE, APIKEY, CREDENTIAL, WEBHOOK, AUTHORISATION/AUTHORIZATION, CONNSTR, COOKIE (not
// COOKIECUTTER), KEY (not KEYBOARD, KEYRING), TOKEN (not TOKENIZER/TOKENISER) or AUTH (not AUTHOR) anywhere;
// is CONNECTION(_)STRING(S) or AzureWebJobsStorage/-Dashboard; or is a database/broker URL (DATABASE_URL,
// CELERY_BROKER_URL, JDBC_URL, ...); any of them with trailing digits (GITHUB_TOKEN2, OPENAI_API_KEY2). A token
// COUNT (MAX_THINKING_TOKENS) is not a secret. So GITHUB_TOKENS, SECRETKEYBASE, BASICAUTH, DBPASS, STRIPEKEY and
// SECRETVALUE go; KEYBOARD_LAYOUT, KEYRING_PROVIDER, TOKENIZERS_PARALLELISM, GIT_AUTHOR_NAME and GIT_ASKPASS stay.
// A value is a secret when it holds a URL with a password (amqp://user:pass@host) or a password/secret/token/
// key/signature field (AccountKey=, ;Password=, &sig=); the proxy variables keep theirs (as in the core: git and
// npm need the proxy). Python's (?i) also folds U+0130/U+0131 to I, which /iu does not: they are made I first.
// Python's \s and \w are spelled out (PY_SPACE; \w as [\p{L}\p{N}_], a superset in Node's newer Unicode); the
// \b before a URL scheme looks at ASCII word characters only, so this drops a little more than the core there,
// never less. NOT_SECRET_ENV are the shell's own names; the core's other exceptions (credential-FILE paths,
// claude.exe's own switches) are deleted here too: a terminal needs none of them. sec_ui.test.js checks that
// every name and value the core calls secret is deleted here (when the core has this rule).
// (2026-10-27, SEC-L11 review 4) S6's round-4 rule (providers.py SECRET_NAME_ANYWHERE): these match anywhere in a
// name, with no word bounds: CONNECTION(_)STRING (RedisConnectionString, ConnectionStrings__X), BW_SESSION,
// OP_SESSION_*, CI_JOB_JWT, SONAR_LOGIN, SLACK_HOOK_URL, GHPAT, SMTPPW and DATABASEURL; and a value field may
// follow a `,` too (StackExchange.Redis's host:6380,password=x,ssl=True)
const SECRET_ENV_RE = /(?<![A-Za-z0-9])(?:PW|PAT|CREDS?|[A-Z0-9]*(?:PWD|DSN)S?|[A-Z0-9]*(?<!ASK)(?<!BY)(?<!COM)PASS|[A-Z0-9]*(?:SECRET|PASSWORD|PASSWD|PASSPHRASE|APIKEY|CREDENTIAL|WEBHOOK|AUTHORI[SZ]ATION|CONNSTR|KEY(?!BOARD|RING)|TOKEN(?!I[SZ])|AUTH(?!OR)|COOKIE(?!CUTTER))[A-Z0-9]*|AZUREWEBJOBS(?:STORAGE|DASHBOARD)|(?:DATABASE|DB|REDIS|MONGO|MONGODB|POSTGRES|POSTGRESQL|MYSQL|AMQPS?|BROKER|RABBITMQ|JDBC|CACHE)_UR[LI])[0-9]*(?![A-Za-z0-9])|(?:CONNECTION_?STRING|BW_SESSION|OP_SESSION_|CI_JOB_JWT|SONAR_LOGIN|SLACK_HOOK_URL|GHPAT|SMTPPW|DATABASEURL)/iu;
const COUNT_TOKENS_RE = /(?<![A-Za-z0-9])(?:MAX|MIN|OUTPUT|INPUT|THINKING|CONTEXT|BUDGET|NUM|TOTAL)[_.-]?TOKENS(?![A-Za-z0-9])/giu;
const PY_SPACE = String.raw`\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const SECRET_ENV_VALUE_RE = new RegExp(String.raw`(?<![A-Za-z0-9_])[a-z][a-z0-9+.\-]*://[^/${PY_SPACE}@:]*:[^/${PY_SPACE}@]+@`
  + String.raw`|(?:^|[;?&,${PY_SPACE}])(?:sig|[\p{L}\p{N}_.\-]*(?:password|passwd|pwd|passphrase|secret|token|accountkey`
  + String.raw`|accesskey|accesssignature|apikey|api[_\-]key))[${PY_SPACE}]*=[${PY_SPACE}]*[^${PY_SPACE};&,]`, 'iu');
const PROXY_ENV = new Set(['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY']);
const NOT_SECRET_ENV = new Set(['PWD', 'OLDPWD', 'SSH_AUTH_SOCK']);
// (2026-10-27, integrate S1 x S6) git's command-line config key NAMES (GIT_CONFIG_KEY_<n>, e.g. core.pager) are no
// secret by name: the core's GIT_CONFIG_KEY_NAME_RE (deleting them while GIT_CONFIG_COUNT stays breaks every git);
// their values still go through the value rule, as in the core
const GIT_CONFIG_KEY_NAME_RE = /^GIT_CONFIG_KEY_[0-9]+$/;
/** @param {string} s */
const pyCaseless = (s) => s.replace(/[\u0130\u0131]/g, 'I');
/**
 * (2026-10-27, SEC-L11 review 3) Whether a terminal must not get this variable: the core's is_secret_env.
 * @param {string} name @param {string | undefined} value
 */
function isTerminalSecret(name, value) {
  const upper = name.toUpperCase();
  if (NOT_SECRET_ENV.has(upper)) return false;
  if (!GIT_CONFIG_KEY_NAME_RE.test(upper)
      && SECRET_ENV_RE.test(pyCaseless(name).replace(COUNT_TOKENS_RE, '_'))) return true;
  return !PROXY_ENV.has(upper) && SECRET_ENV_VALUE_RE.test(pyCaseless(String(value ?? '')));
}

/**
 * (2026-10-27, SEC-L11) A message's rendered html without its lists and quotes (lib/markdown.js writes them
 * as <ul>/<ol>/<blockquote>), so codeBlocksOf finds only its TOP-LEVEL code blocks: what an auto-run may
 * run. A tag inside a code block is escaped (&lt;ul&gt;), so it never counts.
 * @param {string} html
 */
function topLevelHtml(html) {
  let depth = 0;
  let out = '';
  let at = 0;
  const s = String(html || '');
  const re = /<(\/?)(?:ul|ol|blockquote)\b[^>]*>/g;
  let m;
  while ((m = re.exec(s))) {
    if (depth === 0) out += s.slice(at, m.index);
    depth = Math.max(0, depth + (m[1] ? -1 : 1));
    at = m.index + m[0].length;
  }
  return depth === 0 ? out + s.slice(at) : out;
}

/**
 * (2026-10-21) Which shell runs a code block: its fence language, else cmd for `set NAME=value` lines
 * (they mean something else in PowerShell), else null = the owner's default terminal. Windows only.
 * @param {string} lang @param {string} text @param {string} [platform]
 * @returns {'cmd' | 'powershell' | null}
 */
function shellFor(lang, text, platform = process.platform) {
  if (platform !== 'win32') return null;
  const l = lang.toLowerCase();
  if (['cmd', 'bat', 'batch', 'dos'].includes(l)) return 'cmd';
  if (['powershell', 'pwsh', 'ps', 'ps1'].includes(l)) return 'powershell';
  if (!l && /^\s*set\s+[A-Za-z_]\w*=/im.test(text) && !/^\s*\$env:/im.test(text)) return 'cmd';
  return null;
}

/**
 * (2026-10-26, silent run) A silent run of several lines: the lines go to a script file and the hidden
 * terminal gets ONE line that runs it (sendText would turn every newline into an Enter, and a
 * PowerShell block or a here-string would break). `shell` null on Windows = PowerShell (the silent
 * terminal is ours, so its shell is known). The path is quoted for its shell.
 * @param {'cmd' | 'powershell' | null} shell @param {string} platform
 * @returns {{ext: string, shellPath: string | undefined, line: (file: string) => string}}
 */
function silentScript(shell, platform = process.platform) {
  if (platform !== 'win32') return { ext: '.sh', shellPath: undefined, line: (f) => `sh '${f.replace(/'/g, "'\\''")}'` };
  if (shell === 'cmd') return { ext: '.cmd', shellPath: 'cmd.exe', line: (f) => `call "${f}"` };
  return { ext: '.ps1', shellPath: 'powershell.exe',
    line: (f) => `powershell -NoProfile -ExecutionPolicy Bypass -File '${f.replace(/'/g, "''")}'` };
}

// (2026-10-21, attach) the composer's "Add picture or file": the dialog's Images filter, and at most
// ATTACH_MAX paths per pick
const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'];
const ATTACH_MAX = 20;

/**
 * (2026-10-21, attach) The picked paths that go into the reply draft: non-empty strings on one line,
 * no duplicates, at most ATTACH_MAX.
 * @param {unknown[]} paths
 * @returns {string[]}
 */
function attachPaths(paths) {
  /** @type {string[]} */
  const out = [];
  for (const p of Array.isArray(paths) ? paths : []) {
    if (typeof p !== 'string' || !p.trim() || /[\r\n]/.test(p) || out.includes(p)) continue;
    out.push(p);
    if (out.length >= ATTACH_MAX) break;
  }
  return out;
}

// (2026-10-20, 0.4.4 logfix A) the composer's action buttons (chatmodel.blockedHelp)
// (2026-10-21) + "What did you change?" (chatmodel.composerState)
// (2026-10-21) + "Edit task…" (it was offered but dropped here), "Edit reply…", "Withdraw reply"
// (2026-10-21, start now) + "Start now" (a pending task: to the front of its queue, runner started)
// (2026-10-21, v049 dontask_hold) + "Retry with <the denial's profile>" and an allow-list entry's "Copy"
// (2026-10-27, U4, SPEC_V05_PARALLEL 8.6) + the merge card's "Open copy" (the host opens its own copy of the item's path)
const CHAT_ACTIONS = ['retry-shell-dev', 'edit-allowed', 'ask-changes', 'edit-task', 'edit-reply', 'withdraw-reply', 'start-now',
  'retry-suggested', 'copy-allow', 'open-copy'];
const MERGE_PATH_MAX = 4096; // a card path is relative and short; longer = not from the card

/**
 * Validate one message from the webview. Returns a clean copy, or null (dropped).
 * @param {unknown} msg
 * @returns {{type: 'ready'} | {type: 'openRaw'} | {type: 'openLink', href: string}
 *   | {type: 'reply', text: string, profile: string | null} | {type: 'choose', n: number, profile: string | null}
 *   | {type: 'autoPick', on: boolean} | {type: 'action', id: string, n?: number} | {type: 'viewMode', adhd: boolean}
 *   | {type: 'autorun', on: boolean}
 *   | {type: 'maxAgents', value: string}
 *   | {type: 'prompt', i: number, text: string, profile: string | null}
 *   | {type: 'copyCode' | 'runCode', text: string, lang: string, silent?: true} | {type: 'attach'} | {type: 'chatSettings'}
 *   | {type: 'saveEdit', text: string} | {type: 'cancelEdit'}
 *   | {type: 'merge', action: string, gen: number} | {type: 'compare', path: string} | null}
 * (2026-10-27, U4, 8.6) `card` = the merge card the host shows now (format.mergeCardModel; null = none): a `merge`
 * answer needs one of ITS actions and an integer gen; a `compare` needs one of ITS paths and a preview. No card = dropped.
 * @param {{actions: {action: string}[], paths: string[], preview: boolean, gen: number} | null} [card]
 */
function validateMessage(msg, card = null) {
  if (typeof msg !== 'object' || msg === null || Array.isArray(msg)) return null;
  const m = /** @type {Record<string, unknown>} */ (msg);
  switch (m.type) {
    case 'ready':
      return { type: 'ready' };
    case 'openRaw':
      return { type: 'openRaw' };
    case 'openLink': {
      const href = httpsOnly(m.href);
      return href ? { type: 'openLink', href } : null;
    }
    case 'reply': {
      if (typeof m.text !== 'string' || m.text.length > REPLY_MAX || !m.text.trim()) return null;
      const p = profileOf(m.profile);
      if (p === undefined) return null;
      return { type: 'reply', text: m.text, profile: p };
    }
    case 'choose': {
      // (0.4.1) an option button: only its number; the host looks the option up in its own copy of
      // the item (format.choiceReplyText), so no label text from the webview is ever sent
      if (!Number.isInteger(m.n) || /** @type {number} */ (m.n) < 1 || /** @type {number} */ (m.n) > 4) return null;
      const p = profileOf(m.profile);
      if (p === undefined) return null;
      return { type: 'choose', n: /** @type {number} */ (m.n), profile: p };
    }
    case 'prompt': {
      // (2026-10-21) a quick-prompt button: its index plus the text it showed; the host sends its OWN
      // copy of the setting, and only when that still equals `text` (the setting may have changed)
      const i = /** @type {number} */ (m.i);
      if (!Number.isInteger(i) || i < 0 || i >= QUICK_PROMPTS_MAX) return null;
      if (typeof m.text !== 'string' || m.text.length > QUICK_PROMPT_MAX || !m.text.trim()) return null;
      const p = profileOf(m.profile);
      if (p === undefined) return null;
      return { type: 'prompt', i, text: m.text, profile: p };
    }
    case 'autoPick':
      // (0.4.6) the chat's "Auto-pick best" box; a boolean is the whole message
      if (typeof m.on !== 'boolean') return null;
      return { type: 'autoPick', on: m.on };
    case 'queued':
      // (2026-10-26, write while working) the webview holds a queued message: auto-pick leaves the turn to it
      if (typeof m.on !== 'boolean') return null;
      return { type: 'queued', on: m.on };
    case 'autorun':
      // (2026-10-27, modes fix round 2) the composer's Autorun box: this task's own flag (`edit --autorun`)
      if (typeof m.on !== 'boolean') return null;
      return { type: 'autorun', on: m.on };
    case 'maxAgents':
      // (2026-10-21, subagents) the bottom bar's subagent dropdown: one value of argv.AGENT_LIMITS
      if (!validAgentsValue(m.value)) return null;
      return { type: 'maxAgents', value: /** @type {string} */ (m.value) };
    case 'viewMode':
      // (2026-10-21) the header's View dropdown: ADHD mode on or off; a boolean is the whole message
      if (typeof m.adhd !== 'boolean') return null;
      return { type: 'viewMode', adhd: m.adhd };
    case 'copyCode':
    case 'runCode': {
      // (2026-10-21) a code block's button: its text (the host checks it is in the conversation) + language
      if (typeof m.text !== 'string' || m.text.length > CODE_MAX || !m.text.trim()) return null;
      const lang = m.lang === undefined || m.lang === null ? '' : m.lang;
      if (typeof lang !== 'string' || !LANG_RE.test(lang)) return null;
      // (2026-10-26, silent run) runCode may ask for a hidden terminal: `silent: true`, nothing else
      if (m.silent !== undefined && (m.type !== 'runCode' || typeof m.silent !== 'boolean')) return null;
      return m.silent === true ? { type: m.type, text: m.text, lang, silent: true } : { type: m.type, text: m.text, lang };
    }
    case 'action':
      // (2026-10-20, 0.4.4 logfix A) a blocked item's action button: only a known id, the host does the rest
      if (typeof m.id !== 'string' || !CHAT_ACTIONS.includes(m.id)) return null;
      // (2026-10-21, v049 dontask_hold) "Copy" of an allow-list entry: only its index; the host copies
      // its own copy of the item's denial_block.allow[n]
      if (m.id === 'copy-allow') {
        return Number.isInteger(m.n) && /** @type {number} */ (m.n) >= 0 && /** @type {number} */ (m.n) < 3
          ? { type: 'action', id: m.id, n: /** @type {number} */ (m.n) } : null;
      }
      return { type: 'action', id: m.id };
    case 'attach':
      // (2026-10-21, attach) the composer's "Add picture or file" button: the host opens the file
      // dialog; nothing else in the message is used
      return { type: 'attach' };
    case 'saveEdit':
      // (2026-10-26, edit in box) Send while the box holds an edit: the new text only; the host knows the item
      if (typeof m.text !== 'string' || m.text.length > REPLY_MAX || !m.text.trim()) return null;
      return { type: 'saveEdit', text: m.text };
    case 'cancelEdit':
      return { type: 'cancelEdit' };
    case 'chatSettings':
      // (2026-10-21, chat settings) the composer's ⚙: the host opens the chat box settings (a QuickPick)
      return { type: 'chatSettings' };
    case 'merge': {
      // (2026-10-27, U4, 8.6) a merge card button: the action must be on the current card, gen an integer
      if (!card || typeof m.action !== 'string' || !Number.isInteger(m.gen) || /** @type {number} */ (m.gen) < 0) return null;
      if (!card.actions.some((a) => a.action === m.action)) return null;
      return { type: 'merge', action: m.action, gen: /** @type {number} */ (m.gen) };
    }
    case 'compare': {
      // (2026-10-27, U4, 8.6) Preview of one conflicted path: only a path of the current card (the host joins
      // it to its own merge.preview folder and checks the result stays inside it)
      if (!card || !card.preview || typeof m.path !== 'string' || !m.path || m.path.length > MERGE_PATH_MAX) return null;
      if (!card.paths.includes(m.path)) return null;
      return { type: 'compare', path: m.path };
    }
    default:
      return null;
  }
}

/**
 * The patch for the webview: blocks whose JSON changed since `sent`, plus the key order.
 * `sent` (key -> JSON) is updated in place.
 * @param {{blocks: {key: string}[]}} view @param {Map<string, string>} sent
 * @returns {{upserts: {key: string}[], order: string[]}}
 */
function diffBlocks(view, sent) {
  /** @type {{key: string}[]} */
  const upserts = [];
  const order = [];
  const seen = new Set();
  for (const b of view.blocks) {
    if (seen.has(b.key)) continue;
    seen.add(b.key);
    order.push(b.key);
    const sig = JSON.stringify(b);
    if (sent.get(b.key) !== sig) {
      sent.set(b.key, sig);
      upserts.push(b);
    }
  }
  for (const k of [...sent.keys()]) if (!seen.has(k)) sent.delete(k);
  return { upserts, order };
}

module.exports = { REPLY_MAX, HREF_MAX, CODE_MAX, makeNonce, csp, panelHtml, unavailableHtml, validateRestoreState, restoreAutoPick,
  httpsOnly, validateMessage, diffBlocks, codeShown, cleanCode, shellFor, codeBlocksOf, silentScript, IMAGE_EXTS, ATTACH_MAX, attachPaths,
  terminalEnv, topLevelHtml };

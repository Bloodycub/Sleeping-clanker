// @ts-check
// A small escape-first Markdown renderer for the Task view (2026-10-17, 0.4.0, docs/SPEC_V04_CHAT.md 2).
// No vscode API, no dependency. Every character of the input is HTML-escaped BEFORE any markup is
// added, so raw HTML in Claude's output (a <script>, an <img onerror>) can only ever come out as text;
// the only tags produced are the ones written below. A link gets an href only for https:// (0.4.1);
// (2026-10-27, SEC-M1) that URL is in data-href, and the href itself is "#" (see linkOpen).
// Supported: ATX headings, paragraphs (line breaks kept), **bold** / __bold__, *italic* / _italic_,
// ~~strike~~, `code`, fenced code (``` / ~~~), block quotes, nested - / * / + / 1. lists (task boxes
// shown as ☐ / ☑), GFM pipe tables, horizontal rules, [text](url) links and bare https:// URLs.
// (2026-10-19, 0.4.1) Only https:// links get an href (the webview opens https only); an http link
// stays plain text with its URL visible. <https://…> autolinks; ![alt](url) is the text
// "alt (image: url)" (no image is ever loaded). Pathological input cannot stall or crash the host:
// nesting deeper than MAX_DEPTH is plain escaped text, inline markup is parsed on at most INLINE_MAX
// characters of a line (the rest is escaped), every pass is linear (no backtracking regex), and any
// exception falls back to the escaped text in a <pre>.
'use strict';

const NUL = '\u0000';
// (2026-10-27, SEC-M1) https only and no '@' before the path (a "known-host@other-host" URL goes to
// other-host), so a link's visible text cannot hide its real host behind a user-info part.
const SAFE_HREF = /^https:\/\/[^\s/?#@]+(?:[/?#]\S*)?$/i;
// (2026-10-27, SEC-M1) the same cap as chatview.HREF_MAX: a longer link would never open, so it is text
const HREF_MAX = 2048;
const MAX_DEPTH = 20;
const INLINE_MAX = 20000;

/** @param {string} s */
function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

/** @param {string} s */
function unescapeHtml(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&');
}

/**
 * The href for a link target, or null (anything but https: http:, javascript:, data:, file:, relative…).
 * @param {string} url the RAW (unescaped) url
 */
function safeHref(url) {
  const u = String(url || '').trim();
  // eslint-disable-next-line no-control-regex
  if (u.length > HREF_MAX || /[\u0000-\u001f\u007f]/.test(u) || !SAFE_HREF.test(u)) return null;
  // (2026-10-27, SEC-M1) and only what chatview.httpsOnly will open: it parses, and has no user:password
  let p;
  try {
    p = new URL(u);
  } catch (_e) {
    return null;
  }
  return p.protocol === 'https:' && !p.username && !p.password ? u : null;
}

/**
 * (2026-10-27, SEC-M1) The opening tag of a link. It has NO navigable href: VS Code's webview host opens
 * a clicked <a href="https://…"> itself (and with no prompt in a trusted workspace), past the Task
 * view's confirm modal. The URL is in data-href; media/chat.js posts it as openLink and stops the click.
 * @param {string} href a safeHref() result
 */
function linkOpen(href) {
  return `<a href="#" data-href="${escapeHtml(href)}" role="link">`;
}

/**
 * Pair `delim` runs left to right in ONE pass (what the old lazy regex did, without its quadratic
 * retries): the earliest pending opener (followed by a non-space) closes on the first later delimiter
 * preceded by a non-space. `word` adds the `__` rule: an opener is not preceded by a word character
 * and a closer is not followed by one.
 * @param {string} s @param {string} delim @param {string} tag @param {boolean} [word]
 */
function pairDelims(s, delim, tag, word = false) {
  const n = delim.length;
  /** @type {[number, number][]} */
  const pairs = [];
  let open = -1;
  for (let i = s.indexOf(delim); i !== -1; i = s.indexOf(delim, i)) {
    const before = i > 0 ? s[i - 1] : '';
    const after = i + n < s.length ? s[i + n] : '';
    if (open !== -1 && i > open + n && /\S/.test(before) && !(word && /\w/.test(after))) {
      pairs.push([open, i]);
      open = -1;
    } else if (open === -1 && /\S/.test(after) && !(word && /\w/.test(before))) {
      open = i;
    }
    i += n;
  }
  if (!pairs.length) return s;
  let out = '';
  let at = 0;
  for (const [a, b] of pairs) {
    out += `${s.slice(at, a)}<${tag}>${s.slice(a + n, b)}</${tag}>`;
    at = b + n;
  }
  return out + s.slice(at);
}

/**
 * `code` spans in one pass: a run of N backticks closes on the next run of exactly N; an opener with
 * no closer stays literal. Code spans become `keep(html)` slots.
 * @param {string} s @param {(html: string) => string} keep
 */
function codeSpans(s, keep) {
  /** @type {{pos: number, len: number}[]} */
  const runs = [];
  for (let i = s.indexOf('`'); i !== -1; i = s.indexOf('`', i)) {
    let j = i;
    while (j < s.length && s[j] === '`') j += 1;
    runs.push({ pos: i, len: j - i });
    i = j;
  }
  if (runs.length < 2) return s;
  // next[k] = the next run with the same length (filled backwards: linear)
  const next = new Array(runs.length).fill(-1);
  /** @type {Map<number, number>} */
  const seen = new Map();
  for (let k = runs.length - 1; k >= 0; k -= 1) {
    const at = seen.get(runs[k].len);
    if (at !== undefined) next[k] = at;
    seen.set(runs[k].len, k);
  }
  let out = '';
  let at = 0;
  for (let k = 0; k < runs.length; k += 1) {
    const j = next[k];
    if (j === -1) continue;
    const r = runs[k];
    const body = s.slice(r.pos + r.len, runs[j].pos);
    out += s.slice(at, r.pos) + keep(`<code>${escapeHtml(body.replace(/^ ([\s\S]*) $/, '$1'))}</code>`);
    at = runs[j].pos + runs[j].len;
    k = j;
  }
  return out + s.slice(at);
}

// A URL inside ESCAPED text: no whitespace, and it stops before an escaped <, >, " or '.
const URL_BODY = "(?:(?!&lt;|&gt;|&quot;|&#39;)[^\\s<])*";
const AUTOLINK_RE = new RegExp(`&lt;(https:\\/\\/${URL_BODY})&gt;`, 'gi');
const BARE_URL_RE = new RegExp(`(^|[\\s(])(https?:\\/\\/${URL_BODY}[^\\s<.,;:!?)'"&])`, 'gi');

/**
 * Inline markup on ONE raw line or cell: code spans first (their content is escaped and protected),
 * then escape everything else, then images (as text), links, autolinks, bare URLs, emphasis, strike.
 * Only the first INLINE_MAX characters are parsed; the rest of a longer line is escaped as it is.
 * @param {string} raw
 * @returns {string}
 */
function renderInline(raw) {
  const str = String(raw).split(NUL).join('');
  if (str.length > INLINE_MAX) return renderInline(str.slice(0, INLINE_MAX)) + escapeHtml(str.slice(INLINE_MAX));
  /** @type {string[]} */
  const slots = [];
  const keep = (/** @type {string} */ html) => `${NUL}${slots.push(html) - 1}${NUL}`;
  let s = escapeHtml(codeSpans(str, keep));
  // ![alt](url): never loaded, never a link: the text "alt (image: url)" (both already escaped)
  s = s.replace(/!\[([^[\]\n]*)\]\(([^()\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_m, alt, url) => keep(`${alt ? `${alt} ` : ''}(image: ${url})`));
  // [text](url) — the url is checked on its unescaped form; the text keeps its inline markup.
  // An http:// link is its text plus the visible URL (nothing that looks clickable but is not).
  s = s.replace(/\[([^[\]\n]+)\]\(([^()\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g, (_m, text, url) => {
    const href = safeHref(unescapeHtml(url));
    if (href) return keep(linkOpen(href)) + text + keep('</a>');
    return /^http:\/\//i.test(unescapeHtml(url)) ? `${text} (${keep(url)})` : text;
  });
  // <https://…> autolinks (escaped to &lt;…&gt; by now)
  s = s.replace(AUTOLINK_RE, (m, url) => {
    const href = safeHref(unescapeHtml(url));
    return href ? keep(`${linkOpen(href)}${url}</a>`) : m;
  });
  // bare URLs (links made above are slots now). The whole URL is ONE slot, so emphasis never applies
  // inside it; an http:// URL is kept as plain text.
  s = s.replace(BARE_URL_RE, (_m, pre, url) => {
    const href = safeHref(unescapeHtml(url));
    return `${pre}${keep(href ? `${linkOpen(href)}${url}</a>` : url)}`;
  });
  s = pairDelims(s, '**', 'strong');
  s = pairDelims(s, '__', 'strong', true);
  s = s.replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/(^|[^\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>');
  s = pairDelims(s, '~~', 'del');
  // eslint-disable-next-line no-control-regex
  return s.replace(/\u0000(\d+)\u0000/g, (_m, i) => slots[Number(i)]);
}

// (0.4.1) every block pattern is unambiguous (linear on a 200 KB line): the fence run is maximal,
// headings and table separators are parsed by code below
const FENCE_RE = /^ {0,3}(`{3,}(?!`)|~{3,}(?!~))([^`]*)$/;
const HEADING_RE = /^ {0,3}(#{1,6})\s+/;
const HR_RE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const QUOTE_RE = /^ {0,3}>\s?(.*)$/;

/**
 * An ATX heading: [level, text] (the closing #s and the spaces around them dropped), or null.
 * @param {string} line
 * @returns {[number, string] | null}
 */
function heading(line) {
  const m = HEADING_RE.exec(line);
  if (!m) return null;
  let t = line.slice(m[0].length).trimEnd();
  let k = t.length;
  while (k > 0 && t[k - 1] === '#') k -= 1;
  t = t.slice(0, k).trimEnd();
  return [m[1].length, t];
}

/**
 * A GFM table separator row (`|---|:--:|`).
 * @param {string | undefined} line
 */
function isTableSep(line) {
  if (line === undefined || !line.includes('-')) return false;
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|')) t = t.slice(0, -1);
  return t.trim() !== '' && t.split('|').every((c) => /^\s*:?-+:?\s*$/.test(c));
}

/**
 * Split a table row on `|`, except inside `code` and after a backslash.
 * @param {string} line
 */
function splitRow(line) {
  let t = line.trim();
  if (t.startsWith('|')) t = t.slice(1);
  if (t.endsWith('|') && !t.endsWith('\\|')) t = t.slice(0, -1);
  /** @type {string[]} */
  const cells = [];
  let cur = '';
  let inCode = false;
  for (let i = 0; i < t.length; i += 1) {
    const c = t[i];
    if (c === '\\' && t[i + 1] === '|') { cur += '|'; i += 1; continue; }
    if (c === '`') inCode = !inCode;
    if (c === '|' && !inCode) { cells.push(cur.trim()); cur = ''; continue; }
    cur += c;
  }
  cells.push(cur.trim());
  return cells;
}

/** @param {string} line */
const isTableRow = (line) => line.includes('|') && line.trim() !== '';

/** @param {string} line @param {string | undefined} next */
function startsBlock(line, next) {
  return FENCE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line) || LIST_RE.test(line)
    || QUOTE_RE.test(line) || (isTableRow(line) && isTableSep(next));
}

/** The leading indent (a tab counts 4). @param {string} line */
function indentOf(line) {
  const lead = (/^[ \t]*/.exec(line) || [''])[0];
  return lead.includes('\t') ? lead.replace(/\t/g, '    ').length : lead.length;
}

/**
 * A list starting at lines[i]; nested lists by indentation. Returns [html, next index].
 * @param {string[]} lines @param {number} i @param {number} depth
 * @returns {[string, number]}
 */
function renderList(lines, i, depth) {
  const first = /** @type {RegExpExecArray} */ (LIST_RE.exec(lines[i]));
  const base = indentOf(first[1]);
  const ordered = /\d/.test(first[2]);
  const startNum = ordered ? parseInt(first[2], 10) : 1;
  /** @type {string[]} */
  const items = [];
  while (i < lines.length) {
    const m = LIST_RE.exec(lines[i]);
    if (!m || indentOf(m[1]) !== base || /\d/.test(m[2]) !== ordered) break;
    /** @type {string[]} */
    const body = [m[3]];
    /** @type {number[]} the indent of each continuation line (-1 = blank), computed once */
    const ind = [];
    i += 1;
    // continuation: deeper-indented lines (nested lists included), blank lines inside the item
    while (i < lines.length) {
      const l = lines[i];
      if (!/\S/.test(l)) {
        const nxt = lines[i + 1];
        if (nxt !== undefined && /\S/.test(nxt) && indentOf(nxt) > base) { body.push(''); ind.push(-1); i += 1; continue; }
        break;
      }
      const n = indentOf(l);
      if (n > base) { body.push(l); ind.push(n); i += 1; continue; }
      if (!LIST_RE.test(l) && !startsBlock(l, lines[i + 1]) && body.length === 1) { body.push(l); ind.push(n); i += 1; continue; } // lazy line
      break;
    }
    let head = body[0];
    let box = '';
    const task = /^\[([ xX])\]\s+(.*)$/.exec(head);
    if (task) { box = task[1] === ' ' ? '☐ ' : '☑ '; head = task[2]; }
    const rest = body.slice(1);
    const restIndent = ind.reduce((min, n) => (n >= 0 && n < min ? n : min), Infinity);
    const cut = restIndent === Infinity ? 0 : restIndent;
    const inner = rest.length ? renderBlocks(rest.map((l, k) => l.slice(Math.min(cut, Math.max(ind[k], 0)))), depth + 1) : '';
    items.push(`<li>${escapeHtml(box)}${renderInline(head)}${inner}</li>`);
  }
  const tag = ordered ? 'ol' : 'ul';
  const start = ordered && startNum !== 1 ? ` start="${startNum}"` : '';
  return [`<${tag}${start}>${items.join('')}</${tag}>`, i];
}

/**
 * @param {string[]} lines
 * @param {number} [depth] the list / quote nesting; from MAX_DEPTH on, the lines are plain escaped text
 * @returns {string}
 */
function renderBlocks(lines, depth = 0) {
  if (depth >= MAX_DEPTH) return `<p>${lines.filter((l) => l.trim()).map((l) => escapeHtml(l.trim())).join('<br>')}</p>`;
  /** @type {string[]} */
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    const fence = FENCE_RE.exec(line);
    if (fence) {
      const mark = fence[1];
      const lang = (/^[\w+#.-]*/.exec(fence[2].trim()) || [''])[0];
      /** @type {string[]} */
      const code = [];
      i += 1;
      while (i < lines.length && !new RegExp(`^ {0,3}${mark[0] === '`' ? '`' : '~'}{${mark.length},}\\s*$`).test(lines[i])) {
        code.push(lines[i]);
        i += 1;
      }
      i += 1; // the closing fence (or the end)
      const cls = lang ? ` class="language-${escapeHtml(lang)}"` : '';
      out.push(`<pre><code${cls}>${escapeHtml(code.join('\n'))}</code></pre>`);
      continue;
    }
    const h = heading(line);
    if (h) {
      const n = h[0];
      out.push(`<h${n}>${renderInline(h[1])}</h${n}>`);
      i += 1;
      continue;
    }
    if (HR_RE.test(line)) {
      out.push('<hr>');
      i += 1;
      continue;
    }
    if (isTableRow(line) && isTableSep(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      /** @type {string[][]} */
      const rows = [];
      while (i < lines.length && isTableRow(lines[i]) && !FENCE_RE.test(lines[i])) {
        rows.push(splitRow(lines[i]));
        i += 1;
      }
      const cols = head.length;
      const cell = (/** @type {string} */ tag, /** @type {string[]} */ r) => {
        const cells = [];
        for (let c = 0; c < cols; c += 1) cells.push(`<${tag}>${renderInline(r[c] || '')}</${tag}>`);
        return `<tr>${cells.join('')}</tr>`;
      };
      out.push(`<table><thead>${cell('th', head)}</thead><tbody>${rows.map((r) => cell('td', r)).join('')}</tbody></table>`);
      continue;
    }
    if (QUOTE_RE.test(line)) {
      /** @type {string[]} */
      const q = [];
      while (i < lines.length && lines[i].trim()) {
        const m = QUOTE_RE.exec(lines[i]);
        q.push(m ? m[1] : lines[i]);
        i += 1;
      }
      out.push(`<blockquote>${renderBlocks(q, depth + 1)}</blockquote>`);
      continue;
    }
    if (LIST_RE.test(line)) {
      const [html, next] = renderList(lines, i, depth);
      out.push(html);
      i = next;
      continue;
    }
    /** @type {string[]} */
    const para = [line];
    i += 1;
    while (i < lines.length && lines[i].trim() && !startsBlock(lines[i], lines[i + 1])) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${para.map((l) => renderInline(l.trim())).join('<br>')}</p>`);
  }
  return out.join('\n');
}

/**
 * Markdown -> HTML (escape-first; safe to put into innerHTML of the locked-down Task view). Never
 * throws: on any error the text comes back escaped in a <pre>.
 * @param {string} md
 */
function renderMarkdown(md) {
  const text = String(md == null ? '' : md).replace(/\r\n?/g, '\n');
  try {
    return renderBlocks(text.split('\n'));
  } catch (_e) {
    return `<pre>${escapeHtml(text)}</pre>`;
  }
}

/**
 * (2026-10-26, silent run) The fenced code blocks of one message's html as renderBlocks writes them
 * (<pre><code class="language-x">escaped</code></pre>), unescaped, in order: what the "run to continue"
 * auto-run in a full-access task would run (chatview.codeShown still checks each one).
 * @param {string} html
 * @returns {{text: string, lang: string}[]}
 */
function codeBlocksOf(html) {
  const out = [];
  const re = /<pre><code(?: class="language-([\w#+.-]{0,32})")?>([\s\S]*?)<\/code><\/pre>/g;
  let m;
  while ((m = re.exec(String(html || '')))) {
    const text = m[2].replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&amp;/g, '&');
    if (text.trim()) out.push({ text, lang: m[1] || '' });
  }
  return out;
}

// (2026-10-26, silent run, review 1/7) fence languages a "run to continue" block may have: a shell or
// none. python, json, diff, yaml, logs … are shown, never run by themselves. On Windows the silent
// terminal is PowerShell or cmd (chatview.silentScript), so a bash block is not one of them there.
const SHELL_LANGS = ['', 'sh', 'bash', 'shell', 'console', 'zsh'];
const WIN_SHELL_LANGS = ['', 'cmd', 'bat', 'batch', 'dos', 'powershell', 'pwsh', 'ps', 'ps1'];

/** @param {string} lang @param {string} [platform] */
function runnableLang(lang, platform = process.platform) {
  return (platform === 'win32' ? WIN_SHELL_LANGS : SHELL_LANGS).includes(String(lang || '').toLowerCase());
}

module.exports = { MAX_DEPTH, INLINE_MAX, renderMarkdown, renderInline, renderBlocks, escapeHtml, safeHref, splitRow,
  codeBlocksOf, runnableLang };

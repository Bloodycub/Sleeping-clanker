// @ts-check
// (2026-10-21, split tasks) One new-task prompt that lists several tasks becomes one queue item per
// task (setting `claudAutopilot.splitTasks`, default on). Pure: no vscode API.
// Heuristic, deliberately conservative (bias: it misses lists written as prose, "do X and Y", rather
// than splitting one task's requirement list by accident):
//  - a list: 2+ lines that start with the same kind of marker (`1.`/`1)` or `-`/`*`/`•`) at the
//    smallest marker indent; lines below an item (deeper bullets, wrapped text) stay with that item.
//    A bullet list under other text ("Add a setting with:\n- …") is one task's requirements, so it
//    splits only when there is no text above it or that text names tasks (TASKS_RE);
//  - or inline numbering on one line: `1. … 2. … 3. …` / `1) … 2) …`, numbered 1, 2, 3… in order,
//    where `1` starts the text or follows a colon (so "run step 1. then step 2." never splits).
// (2026-10-21, tester round 2 M-4) Never a list right under a "Steps to reproduce" / "Repro" /
// "Toistaminen" / "Vaiheet" heading (one bug's steps, not tasks), never an indented list under text
// (a sub-list) or one indented 4+ spaces (a code block), and never a quoted (`>`) block. The host
// never splits silently: it shows the proposed titles and asks Split / Keep as one (proposeSplit).
// Every item needs MIN_WORDS words ("- on" / "- off" are options, not tasks). Text in a ``` code block
// never counts. The text before the list (and after it, past a blank line) is shared context: every
// part gets it, plus one line saying the other parts are queued separately.
'use strict';

const MAX_PARTS = 20; // more items than this = a spec or a checklist, not a batch of tasks: no split
const MIN_WORDS = 2;
const TASKS_RE = /\b(tasks?|todos?)\b|tehtäv|задач/i;

const LINE_RE = /^([ \t]*)(?:(\d{1,2})[.)]|([-*•]))[ \t]+(\S.*)$/;
const FENCE_RE = /^[ \t]*(```|~~~)/;
// (tester round 2 M-4) the line right above a list that makes it one bug's steps, not tasks
const STEPS_RE = /\b(?:steps?|repro\w*|reproduc\w*|toistaminen|toisto|toista|vaiheet|vaihe)\b|шаг|воспроизв/i;
const TITLE_MAX = 80;
const INLINE_RE = /(^|[\s,;:])(\d{1,2})[.)][ \t]+(?=\S)/g;

/**
 * @param {string} prompt
 * @returns {{intro: string, items: string[], outro: string} | null}
 */
function findList(prompt) {
  const lines = prompt.replace(/\r\n?/g, '\n').split('\n');
  /** @type {{i: number, indent: number, numbered: boolean, text: string}[]} */
  const marks = [];
  let fence = false;
  lines.forEach((line, i) => {
    if (FENCE_RE.test(line)) {
      fence = !fence;
      return;
    }
    if (fence) return;
    const m = LINE_RE.exec(line);
    if (m) marks.push({ i, indent: m[1].replace(/\t/g, '    ').length, numbered: m[2] !== undefined, text: m[4] });
  });
  if (marks.length < 2) return null;
  const indent = Math.min(...marks.map((m) => m.indent));
  if (indent >= 4) return null; // (tester round 2 M-4) an indented code block
  const first = marks.find((m) => m.indent === indent);
  const top = marks.filter((m) => m.indent === indent && m.numbered === (first ? first.numbered : false));
  if (top.length < 2) return null;
  const intro = lines.slice(0, top[0].i).join('\n').trim();
  if (!top[0].numbered && intro && !TASKS_RE.test(intro)) return null;
  if (indent > 0 && intro) return null; // (tester round 2 M-4) an indented list under text is its sub-list
  /** @type {string[]} */
  const items = [];
  let outro = '';
  top.forEach((m, k) => {
    const end = k + 1 < top.length ? top[k + 1].i : lines.length;
    const body = [m.text];
    for (let j = m.i + 1; j < end; j += 1) {
      const line = lines[j];
      // after the LAST item: a blank line, then unindented non-list text = shared closing context
      if (k === top.length - 1 && !line.trim() && j + 1 < end) {
        const next = lines.slice(j + 1).find((l) => l.trim());
        const lead = next ? (next.match(/^[ \t]*/) || [''])[0].replace(/\t/g, '    ').length : 0;
        if (next && lead <= indent && !LINE_RE.test(next)) {
          outro = lines.slice(j + 1).join('\n').trim();
          break;
        }
      }
      body.push(line);
    }
    items.push(body.join('\n').trim());
  });
  return { intro, items, outro };
}

/**
 * @param {string} prompt
 * @returns {{intro: string, items: string[], outro: string} | null}
 */
function findInline(prompt) {
  if (FENCE_RE.test(prompt) || /```/.test(prompt)) return null;
  /** @type {{start: number, end: number}[]} */
  const marks = [];
  let want = 1;
  for (const m of prompt.matchAll(INLINE_RE)) {
    if (Number(m[2]) !== want) continue;
    const start = /** @type {number} */ (m.index) + m[1].length;
    if (want === 1 && !/(^|:)\s*$/.test(prompt.slice(0, start))) continue;
    marks.push({ start, end: /** @type {number} */ (m.index) + m[0].length });
    want += 1;
  }
  if (marks.length < 2) return null;
  // (tester round 2 M-4) one line only: a list over several lines is findList's (it may have refused it)
  if (prompt.slice(marks[0].start, marks[marks.length - 1].start).includes('\n')) return null;
  const items = marks.map((m, k) => prompt.slice(m.end, k + 1 < marks.length ? marks[k + 1].start : prompt.length)
    .trim().replace(/(?:\s*[,;]|\s+(?:and|ja|и))+$/i, '').trim());
  return { intro: prompt.slice(0, marks[0].start).trim(), items, outro: '' };
}

/**
 * The tasks in one prompt: 2+ prompts when it lists several tasks, else `[prompt]` unchanged.
 * @param {string} prompt
 * @returns {string[]}
 */
function splitTasks(prompt) {
  const found = proposeSplit(prompt);
  return found ? found.parts : [String(prompt || '')];
}

/**
 * (tester round 2 M-4) The proposed split of one prompt: the prompts and a short title per part (the
 * first line of its item), or null when the prompt is one task. The host shows the titles and asks.
 * @param {string} prompt
 * @returns {{parts: string[], titles: string[]} | null}
 */
function proposeSplit(prompt) {
  const text = String(prompt || '');
  const found = findList(text) || findInline(text);
  if (!found || found.items.length < 2 || found.items.length > MAX_PARTS) return null;
  if (found.items.some((s) => s.split(/\s+/).filter(Boolean).length < MIN_WORDS)) return null;
  const lastIntro = found.intro.split('\n').map((l) => l.trim()).filter(Boolean).pop() || '';
  if (STEPS_RE.test(lastIntro)) return null;
  const n = found.items.length;
  const others = n === 2 ? 'the other one is queued as its own task' : `the other ${n - 1} are queued as their own tasks`;
  const parts = found.items.map((item, k) => [
    found.intro, item, found.outro, `(Task ${k + 1} of ${n} split from one prompt; ${others}: do only this one.)`,
  ].filter(Boolean).join('\n\n'));
  const titles = found.items.map((item) => {
    const line = item.split('\n')[0].trim();
    return line.length > TITLE_MAX ? `${line.slice(0, TITLE_MAX - 3)}...` : line;
  });
  return { parts, titles };
}

module.exports = { MAX_PARTS, MIN_WORDS, proposeSplit, splitTasks };

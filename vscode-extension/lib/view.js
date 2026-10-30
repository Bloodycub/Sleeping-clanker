// @ts-check
// Tree model, project grouping, the Add menu and the render fingerprint, without the vscode API
// (2026-10-13, v0.2: SPEC_GOALS 5.1, SPEC_PROJECTS 3.1, SPEC_HOVER 1). extension.js turns these
// plain descriptors into TreeItems; the fingerprint of the descriptors decides whether the tree is
// re-rendered at all, so an open hover is not closed by a poll that changed nothing.
// (2026-10-15, v0.3, SPEC_V03 2.3) Named queues: lane nodes (`default` shown as `Main`) appear only
// when a project has two or more queues; every item node carries its (project, lane).
// (2026-10-20, 0.4.5, SPEC_V045_DEFAULTS) The "Defaults for +" section and its row pickers: leaf
// nodes rendered BELOW the queue (extension.js keeps them in the provider's `footer`).
'use strict';

const argv = require('./argv');
const format = require('./format');
const i18n = require('./i18n');
const { findMarker } = require('./chatmodel'); // (2026-10-26) TASK_DONE on the last line
const stats = require('./stats'); // (2026-10-26, owner task f60cec52) the Today counter
const { t } = i18n; // (2026-10-21) the UI language; the English text is the key

const DEFAULT_LANE = 'default';
const ACTIVE = ['pending', 'running', 'waiting', 'paused'];
// (2026-10-20, 0.4.5, SPEC_V045_DEFAULTS) the "Defaults for +" rows at the bottom of the view
const ASK = 'ask every time';
// (2026-10-21, owner "Lisää varoituksella") the last Permissions choice of "Defaults for +" and the panel
const FULL_DEFAULT_LABEL = 'full — allow all: any command, no permission checks';
const KIND_LABELS = Object.freeze({
  ask: ASK, task: 'Task', goal: 'Goal', plan: 'Plan mode', files: 'Files and folders',
});
const KIND_ICONS = Object.freeze({
  ask: 'question', task: 'checklist', goal: 'target', plan: 'lightbulb', files: 'file-directory',
});
const KIND_HINTS = Object.freeze({
  ask: 'Show the Add menu, as before.',
  task: 'Add a one-shot task.',
  goal: 'Set a goal to keep pursuing.',
  plan: 'A task that only plans and changes nothing.',
  files: 'Attach files or folders first.',
});

// (2026-10-21) the status groups of a queue, in this order; Complete starts collapsed. A main task and
// its subtasks form one family that sits in ONE group (familyGroup).
const STATUS_GROUPS = Object.freeze([
  // (2026-10-26, owner "question in not done yet. should be own category and go on top of all") a
  // family with a blocked task (it waits for your reply) sits here, above every other group
  { key: 'questions', label: 'Questions', icon: 'question', color: 'charts.yellow', expanded: true,
    hint: 'waiting for your answer' },
  { key: 'running', label: 'Running', icon: 'sync', color: null, expanded: true,
    hint: 'running now, or waiting for a usage-limit reset' },
  { key: 'todo', label: 'Not done yet', icon: 'circle-large-outline', color: null, expanded: true,
    hint: 'pending or paused' },
  { key: 'errors', label: 'Errors', icon: 'error', color: 'list.errorForeground', expanded: true,
    hint: 'stopped with an error' },
  // (2026-10-26, owner "failed or not complete tasks own subcategory") done without TASK_DONE: no longer
  // under "Completed"; (2026-10-26, owner task 91e164df) cancelled moved to "Completed > Cancelled"
  { key: 'unfinished', label: 'Not completed', icon: 'warning', color: 'list.warningForeground', expanded: true,
    hint: 'ended without TASK_DONE' },
  // (2026-10-21, owner "drop dones inside of completed task by date") ONE "Completed (N)" node, open,
  // with a child per local date (today open, older dates closed); newest date and task first
  { key: 'done', label: 'Completed', icon: 'pass', color: 'testing.iconPassed', expanded: true,
    hint: 'done, by date, newest first' },
]);
const EN_MONTHS = Object.freeze(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']);
const FINISHED = ['done', 'cancelled'];
// (2026-10-26, owner task 91e164df "do subcategory completed-Canceled") the last child of "Completed"
const CANCELLED_GROUP = Object.freeze({ key: 'cancelled', label: 'Cancelled', icon: 'circle-slash', hint: 'cancelled, newest first' });
const RESULT_MAX = 4000; // store.py RESULT_MAX: a stored result this long may have lost its marker line

/**
 * @typedef {{id: string, label: string, description: string, tooltip: string,
 *   icon: {id: string, color: string | null}, contextValue: string, expanded?: boolean}} Render
 * @typedef {{kind: 'item', item: import('./parse').Item, project: string | null, dataDir: string | null,
 *   lane: string, render: Render, children?: ItemNode[]}} ItemNode `children` = its subtasks (2026-10-21)
 * @typedef {{kind: 'group', key: string, project: string | null, dataDir: string | null,
 *   render: Render, children: ItemNode[]}} GroupNode (2026-10-21) one status group of a queue
 * @typedef {{kind: 'lane', lane: import('./parse').Lane, project: string | null, projectName: string,
 *   render: Render, children: (ItemNode | GroupNode)[]}} LaneNode
 * @typedef {{kind: 'project', row: import('./parse').Project, isThis: boolean, render: Render,
 *   children: (ItemNode | GroupNode | LaneNode)[]}} ProjectNode
 * @typedef {{kind: 'setting', key: string, render: Render}} SettingNode
 * @typedef {{kind: 'defaults', rows: DefaultRow[], render: Render, children: SettingNode[]}} DefaultsNode
 * @typedef {{kind: 'hint', render: Render}} HintNode (2026-10-21) the "queue is empty" row of an empty tree
 * @typedef {{kind: 'day', key: string, render: Render, children: ItemNode[]}} DayNode (2026-10-21) one
 *   local date inside "Completed"
 * @typedef {{kind: 'counter', counts: import('./stats').Counter, render: Render}} CounterNode (2026-10-26) the Today counter
 * @typedef {{kind: 'leftover', path: string, render: Render}} LeftoverNode (2026-10-27, U4, 8.10) one leftover copy folder
 * @typedef {{kind: 'leftovers', render: Render, children: LeftoverNode[]}} LeftoversNode (2026-10-27, U4) "Leftover copies (n)"
 * @typedef {ItemNode | GroupNode | LaneNode | ProjectNode | SettingNode | DefaultsNode | HintNode | DayNode | CounterNode
 *   | LeftoverNode | LeftoversNode} Node
 */

// ---- the "+ Add" menu (SPEC_GOALS 5.1) --------------------------------------------------------
/**
 * The Add menu entries, sections as separators. Editor entries appear only when they apply.
 * @param {{hasSelection?: boolean, hasFile?: boolean}} [ctx]
 * @returns {{separator?: true, label: string, description?: string, id?: string}[]}
 */
function addMenuItems(ctx = {}) {
  /** @type {{separator?: true, label: string, description?: string, id?: string}[]} */
  const items = [
    { separator: true, label: t('Add') },
    { label: `$(checklist) ${t('Task')}`, description: t('Add a one-shot task.'), id: 'task' },
    { label: `$(target) ${t('Goal')}`, description: t('Set a goal to keep pursuing.'), id: 'goal' },
    { label: `$(file-directory) ${t('Files and folders')}`, description: t('Attach files or folders to a new task or goal.'), id: 'files' },
    { label: `$(lightbulb) ${t('Plan mode')}`, description: t('A task that only plans and changes nothing.'), id: 'plan' },
  ];
  /** @type {typeof items} */
  const editor = [];
  if (ctx.hasSelection) editor.push({ label: `$(selection) ${t('Selection')}`, description: t('Queue the selected text.'), id: 'selection' });
  if (ctx.hasFile) editor.push({ label: `$(file) ${t('Current file')}`, description: t('Queue the active file as the prompt.'), id: 'file' });
  if (editor.length) items.push({ separator: true, label: t('From editor') }, ...editor);
  // (2026-10-15, SPEC_V03 2.3) a new named queue, from the same menu
  items.push({ separator: true, label: t('Queues') },
    { label: `$(add) ${t('New queue…')}`, description: t('A second queue with its own runner, in parallel.'), id: 'lane' });
  return items;
}

// ---- "Defaults for +" (2026-10-20, 0.4.5, SPEC_V045_DEFAULTS) ---------------------------------
/**
 * @typedef {{kind: string, profile: string, askProfile: boolean, lane: string,
 *   askGoalCaps: boolean, ultracode?: boolean, autorun?: boolean, maxAgents?: string}} AddDefaults what `+` uses WITHOUT asking; `kind: 'ask'`, `askProfile`,
 *   an empty `lane` and `askGoalCaps` all mean "ask, as before 0.4.5"; (2026-10-26, modes) `ultracode`/`autorun`: on or off
 * @typedef {{lanes?: string[], goalMaxRounds?: number, goalMaxHours?: number}} DefaultsCtx
 * @typedef {{key: string, label: string, value: string, hint: string, icon: string, quiet?: boolean, summary?: string}} DefaultRow
 *   (2026-10-26, modes) `quiet`: an Off switch, left out of the section's summary; `summary`: its text there
 */

/** @param {string} kind */
const kindLabel = (kind) => t(KIND_LABELS[/** @type {'ask'} */ (kind)] || ASK);

/** @param {DefaultsCtx} ctx */
const capsValue = (ctx) => t('{0} rounds · {1} h', ctx.goalMaxRounds === -1 ? '∞' : ctx.goalMaxRounds, ctx.goalMaxHours);

/**
 * One row per preselection, in a fixed order; the Queue row appears only when the project has two
 * or more queues (with one queue nothing is ever asked).
 * @param {AddDefaults} d @param {DefaultsCtx} [ctx]
 * @returns {DefaultRow[]}
 */
function defaultsRows(d, ctx = {}) {
  const lanes = ctx.lanes || [];
  const ask = t(ASK);
  /** @type {DefaultRow[]} */
  const rows = [
    { key: 'kind', label: t('Add'), value: kindLabel(d.kind), icon: KIND_ICONS[/** @type {'ask'} */ (d.kind)] || 'question',
      hint: t('What + adds without asking.') },
    // (2026-10-21) a confirmed `full` default shows its warning in the row
    { key: 'profile', label: t('Permissions'),
      value: d.askProfile ? ask : d.profile === argv.FULL_PROFILE ? `⚠ ${d.profile}` : d.profile,
      icon: d.askProfile ? 'question' : d.profile === argv.FULL_PROFILE ? 'warning' : 'shield',
      hint: t('The permission profile a new task gets without asking.') },
  ];
  if (lanes.length >= 2) {
    const found = lanes.find((n) => n.toLowerCase() === String(d.lane || '').toLowerCase());
    rows.push({ key: 'lane', label: t('Queue'), icon: d.lane ? 'layers' : 'question',
      value: !d.lane ? ask : found ? format.laneLabel(found) : t('{0} (gone)', d.lane),
      hint: t('The queue a new task goes to without asking.') });
  }
  rows.push({ key: 'caps', label: t('Goal caps'), value: d.askGoalCaps ? ask : capsValue(ctx),
    icon: d.askGoalCaps ? 'question' : 'watch', hint: t('The round and time cap a new goal gets without asking.') });
  // (2026-10-26, modes) two on/off switches of every new task
  for (const [key, label, tip] of [['ultracode', 'Ultracode', argv.ULTRACODE_TIP], ['autorun', 'Autorun', argv.AUTORUN_TIP]]) {
    // (2026-10-27, modes fix HIGH) Ultracode only with a `full` default and subagents: else Off with the reason
    const why = key === 'ultracode' ? argv.ultracodeRefusal(d.askProfile ? '' : d.profile, d.maxAgents) : '';
    const on = !why && !!d[/** @type {'autorun'} */ (key)];
    rows.push({ key, label: t(label), value: on ? t('On') : t('Off'), summary: t(label), icon: on ? 'rocket' : 'circle-slash',
      hint: t(why || tip), quiet: !on });
  }
  return rows;
}

/** Only what is preselected, so the collapsed section still shows it. @param {DefaultRow[]} rows */
function defaultsSummary(rows) {
  const ask = t(ASK);
  const set = rows.filter((r) => r.value !== ask && !r.quiet).map((r) => r.summary || r.value);
  return set.length ? set.join(' · ') : ask;
}

/**
 * The section and its rows. Every row is a leaf whose click opens its own picker
 * (`claudAutopilot.setAddDefault`, wired in extension.js).
 * @param {AddDefaults} d @param {DefaultsCtx} [ctx]
 * @returns {DefaultsNode}
 */
function defaultsNode(d, ctx = {}) {
  const rows = defaultsRows(d, ctx);
  /** @type {SettingNode[]} */
  const children = rows.map((r) => ({
    kind: /** @type {'setting'} */ ('setting'),
    key: r.key,
    render: {
      id: `defaults:${r.key}`,
      label: r.label,
      description: `${r.value} ▾`,
      tooltip: `${r.hint}\n${t('Click to change it. It is saved in your user settings, for every + from now on.')}`,
      icon: { id: r.icon, color: null },
      contextValue: `default.${r.key}`,
    },
  }));
  return {
    kind: /** @type {'defaults'} */ ('defaults'),
    rows,
    children,
    render: {
      id: 'defaults',
      label: t('Defaults for +'),
      description: defaultsSummary(rows),
      tooltip: [t('What the + button uses without asking you.'),
        ...rows.map((r) => `${r.label}: ${r.value}`),
        t('Click a row to change it; it is saved in your user settings.')].join('\n'),
      icon: { id: 'settings-gear', color: null },
      contextValue: 'defaults',
      expanded: true,
    },
  };
}

/**
 * The QuickPick for one row: a fixed order, the current value marked `current`, and every entry
 * carrying the settings `patch` to write (`edit: true` = ask for the numbers first).
 * @param {string} key @param {AddDefaults} d @param {DefaultsCtx} [ctx]
 * @returns {{label: string, description: string, patch?: Record<string, any>, edit?: true, confirmFull?: true}[]}
 */
function defaultChoices(key, d, ctx = {}) {
  /** @param {boolean} on @param {string} text */
  const mark = (on, text) => (on ? `${text ? `${text} · ` : ''}${t('current')}` : text);
  const ask = t(ASK);
  if (key === 'kind') {
    return argv.ADD_KINDS.map((id) => ({
      label: `$(${KIND_ICONS[/** @type {'ask'} */ (id)]}) ${kindLabel(id)}`,
      description: mark(d.kind === id, t(KIND_HINTS[/** @type {'ask'} */ (id)])),
      patch: { defaultKind: id },
    }));
  }
  if (key === 'profile') {
    return [
      { label: `$(question) ${ask}`, description: mark(d.askProfile, t('Show the permission picker.')), patch: { askProfile: true } },
      // (2026-10-21, security review B1) the safe profiles first; `full` never among them
      ...argv.DEFAULT_PROFILES.map((p) => ({
        label: `$(shield) ${p}`,
        description: mark(!d.askProfile && d.profile === p, t(format.PROFILE_LINES[/** @type {'safe'} */ (p)])), // (2026-10-26) translated
        patch: { askProfile: false, defaultProfile: p },
      })),
      // (2026-10-21, owner "Lisää varoituksella") `full` ALWAYS last, with the warning icon; choosing it
      // asks one modal confirmation first (extension.js _updateDefaults), cancel = the old default stays
      {
        label: `$(warning) ${t(FULL_DEFAULT_LABEL)}`,
        description: mark(!d.askProfile && d.profile === argv.FULL_PROFILE, t(format.PROFILE_LINES.full)),
        patch: { askProfile: false, defaultProfile: argv.FULL_PROFILE },
        confirmFull: /** @type {true} */ (true),
      },
    ];
  }
  if (key === 'lane') {
    return [
      { label: `$(question) ${ask}`, description: mark(!d.lane, t('Show the queue picker, last used first.')), patch: { defaultLane: '' } },
      ...(ctx.lanes || []).map((n) => ({
        label: `$(layers) ${format.laneLabel(n)}`,
        description: mark(!!d.lane && String(d.lane).toLowerCase() === n.toLowerCase(), ''),
        patch: { defaultLane: n },
      })),
    ];
  }
  if (key === 'caps') {
    return [
      { label: `$(question) ${ask}`, description: mark(d.askGoalCaps, t('Ask the round and time cap for every goal.')), patch: { askGoalCaps: true } },
      { label: `$(watch) ${capsValue(ctx)}`, description: mark(!d.askGoalCaps, t('Use these caps without asking.')), patch: { askGoalCaps: false } },
      { label: `$(pencil) ${t('Change these caps…')}`, description: t('Ask the numbers now, then use them without asking.'), edit: /** @type {true} */ (true) },
    ];
  }
  // (2026-10-26, modes) Off / On; Autorun never touches the permission profile (no confirmFull here)
  if (key === 'ultracode' || key === 'autorun') {
    const setting = key === 'ultracode' ? 'defaultUltracode' : 'defaultAutorun';
    // (2026-10-27, modes fix HIGH) a refused Ultracode: its On row says why and changes nothing
    const why = key === 'ultracode' ? argv.ultracodeRefusal(d.askProfile ? '' : d.profile, d.maxAgents) : '';
    const on = !why && !!d[key];
    if (why) {
      return [
        { label: `$(circle-slash) ${t('Off')}`, description: mark(true, ''), patch: { [setting]: false } },
        { label: `$(lock) ${t('On')}`, description: t(why), patch: {} },
      ];
    }
    return [
      { label: `$(circle-slash) ${t('Off')}`, description: mark(!on, ''), patch: { [setting]: false } },
      { label: `$(rocket) ${t('On')}`, description: mark(on, t(key === 'ultracode' ? argv.ULTRACODE_TIP : argv.AUTORUN_TIP)), patch: { [setting]: true } },
    ];
  }
  return [];
}

// ---- item nodes ------------------------------------------------------------------------------
/**
 * @param {import('./parse').Item} item
 * @param {{project?: string | null, dataDir?: string | null, scoped?: boolean, lane?: string | null,
 *   activity?: (dataDir: string, id: string) => string | null} & import('./format').Ctx} ctx
 *   scoped = the tree shows several projects or queues, so the node id carries the data dir;
 *   lane = the item's queue (default: the item's own `lane`, else `default`);
 *   activity = the live activity line of a running item (extension.js, lib/activity.js)
 * @returns {ItemNode}
 */
function itemNode(item, ctx = {}) {
  const icon = format.itemIcon(item);
  // (2026-10-26, owner) a running task shows what Claude does now, like the terminal: `Read(view.js)`
  const now = item.status === 'running' && ctx.activity && ctx.dataDir ? ctx.activity(ctx.dataDir, item.id) : null;
  return {
    kind: 'item',
    item,
    project: ctx.project || null,
    dataDir: ctx.dataDir || null,
    lane: item.lane || DEFAULT_LANE,
    render: {
      id: ctx.scoped ? `${ctx.dataDir || ''}#${item.id}` : item.id,
      label: format.itemLabel(item),
      // (2026-10-21) kind emblem; (2026-10-29, integrate round 8) ctx.parallel: why a task runs alone
      description: `${format.itemEmblem(item)} ${format.itemDescription(item, ctx.now, ctx.parallel)}${now ? ` · ▶ ${now}` : ''}`,
      tooltip: format.itemTooltip(item, ctx) + (now ? `\n${t('Now: {0}', now)}` : ''),
      icon: { id: icon.id, color: icon.color },
      contextValue: format.itemContext(item),
    },
  };
}

// ---- subtasks and status groups (2026-10-21) -------------------------------------------------
/**
 * The group of one family (a main task and its subtasks): Questions while any member waits for your
 * answer (blocked, 2026-10-26), Running while any member runs or waits,
 * Errors while any failed, Not done yet while any is unfinished, Completed > Cancelled while any was
 * cancelled (2026-10-26, owner task 91e164df), Not completed while any ended without TASK_DONE
 * (2026-10-26), else Complete.
 * @param {(string | import('./parse').Item)[]} members statuses, or the items themselves
 */
function familyGroup(members) {
  const statuses = members.map((m) => (typeof m === 'string' ? m : m.status));
  if (statuses.some((s) => s === 'blocked')) return 'questions';
  if (statuses.some((s) => s === 'running' || s === 'waiting')) return 'running';
  if (statuses.some((s) => s === 'failed')) return 'errors';
  if (statuses.some((s) => !FINISHED.includes(s))) return 'todo';
  if (statuses.some((s) => s === 'cancelled')) return CANCELLED_GROUP.key;
  if (members.some((m) => typeof m !== 'string' && noMarker(m))) return 'unfinished';
  return 'done';
}

/** (2026-10-26) The task's last result had no TASK_DONE: the core's `no_marker` flag (0.4.14); an older
 * item without the flag is checked by its stored result when that is not cut (RESULT_MAX).
 * @param {import('./parse').Item} it */
function noMarker(it) {
  if (it.status !== 'done' || it.kind === 'goal') return false;
  const raw = it.raw || {};
  if (typeof raw.no_marker === 'boolean') return raw.no_marker;
  const result = typeof it.result === 'string' ? it.result : '';
  if (!result.trim() || result.length >= RESULT_MAX) return false;
  const marker = findMarker(result);
  return !marker || marker.kind !== 'done'; // GOAL_* lines are not TASK_DONE (goals.parse_task_marker)
}

/**
 * (2026-10-21, Completed by date) When a finished task finished: `finished_at`, else the end of its last
 * attempt (`last_ended_at`), else `created_at`; '' = none of them.
 * (2026-10-22, v049 tester LOW-14) a stamp that is not a date (a hand-edited queue) is skipped, so the
 * next one is used instead of "No date".
 * @param {import('./parse').Item} it
 */
function finishedStamp(it) {
  const raw = it.raw || {};
  for (const v of [it.finished_at, raw.last_ended_at, it.created_at]) if (stampMs(v) !== null) return /** @type {string} */ (v);
  return '';
}

/** (2026-10-22, v049 tester LOW-14) An ISO stamp as epoch ms, or null when it is not a date. @param {unknown} v */
function stampMs(v) {
  if (typeof v !== 'string' || !v) return null;
  const ms = Date.parse(v);
  return Number.isFinite(ms) ? ms : null;
}

/** The local calendar day of an ISO stamp as YYYY-MM-DD, or '' when it is not a date. @param {string} iso */
function localDay(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return '';
  const p2 = (/** @type {number} */ n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
}

/**
 * A date group's label in the UI language: fi "21.10.", ru "21.10", en "Oct 21"; the year only when it
 * is not the current one ("21.10.2025", "Oct 21, 2025").
 * (2026-10-27, i18n audit L12) ru has no trailing dot after the month (the dot is the Finnish style).
 * @param {string} day YYYY-MM-DD @param {Date} now @param {string} lang
 */
function dayLabel(day, now, lang) {
  const [y, m, d] = day.split('-').map(Number);
  const year = y !== now.getFullYear();
  if (lang === 'en') return `${EN_MONTHS[m - 1]} ${d}${year ? `, ${y}` : ''}`;
  if (lang === 'ru') return `${d}.${m}${year ? `.${y}` : ''}`;
  return `${d}.${m}.${year ? y : ''}`;
}

/** A subtask's node: as any item, plus "after main task" while its main task is not done.
 * @param {import('./parse').Item} sub @param {import('./parse').Item} main @param {any} ctx */
function subtaskNode(sub, main, ctx) {
  const node = itemNode(sub, ctx);
  const held = sub.status === 'pending' && main.status !== 'done';
  if (held) node.render.description += ` · ${t('after main task')}`;
  node.render.tooltip += `\n${t('Subtask of "{0}"', format.itemLabel(main))}`
    + (held ? t('; it starts once the main task is done.') : '.');
  return node;
}

/**
 * The item nodes of one list (a queue, or a project shown without lanes): each subtask under its
 * main task (one level; anything deeper or orphaned is shown at the top), and with `ctx.groups` the
 * families sorted into the status groups (empty groups left out).
 * @param {import('./parse').Item[]} items
 * @param {{project?: string | null, dataDir?: string | null, scoped?: boolean, lane?: string | null} & import('./format').Ctx} ctx
 * @returns {(ItemNode | GroupNode)[]}
 */
function itemList(items, ctx = {}) {
  const list = items || [];
  const byId = new Map(list.map((i) => [i.id, i]));
  /** @param {import('./parse').Item} it */
  const parentOf = (it) => (it.parent && it.parent !== it.id ? byId.get(it.parent) : undefined);
  /** @param {import('./parse').Item} it */
  const isTop = (it) => !parentOf(it);
  /** @type {Map<string, import('./parse').Item[]>} */
  const subs = new Map();
  /** @type {import('./parse').Item[]} */
  const tops = [];
  for (const it of list) {
    const main = parentOf(it);
    if (main && isTop(main)) subs.set(main.id, [...(subs.get(main.id) || []), it]);
    else tops.push(it);
  }
  const families = tops.map((main) => {
    const kids = subs.get(main.id) || [];
    const node = itemNode(main, ctx);
    if (kids.length) {
      node.children = kids.map((s) => subtaskNode(s, main, ctx));
      const done = kids.filter((s) => s.status === 'done').length;
      node.render.description += ` · ${t(kids.length === 1 ? '{0}/{1} subtask done' : '{0}/{1} subtasks done', done, kids.length)}`;
      node.render.expanded = true;
    }
    const members = [main, ...kids];
    // (2026-10-21) the family's newest finish; without finished_at its last attempt's end or creation
    // (2026-10-22, v049 tester LOW-14) by parsed time, not as text ("+03:00" and ".5Z" stamps sort wrong)
    let finished = '';
    let finishedMs = -Infinity;
    for (const stamp of members.map(finishedStamp)) {
      const ms = stampMs(stamp);
      if (ms !== null && ms > finishedMs) { finished = stamp; finishedMs = ms; }
    }
    return { node, group: familyGroup(members), finished, finishedMs, count: members.length };
  });
  if (!ctx.groups) return families.map((f) => f.node);
  const scope = ctx.dataDir || ctx.project || '';
  /** @type {GroupNode[]} */
  const groups = [];
  for (const g of STATUS_GROUPS) {
    let fams = families.filter((f) => f.group === g.key);
    // (2026-10-26, owner task 91e164df) the cancelled families go inside "Completed", after its dates
    const cancelled = g.key === 'done' ? families.filter((f) => f.group === CANCELLED_GROUP.key) : [];
    if (!fams.length && !cancelled.length) continue;
    // finished work: the newest first, by parsed time (2026-10-22, v049 tester LOW-14: not as text); a
    // family without a date goes last; the rest keep the queue order
    if (g.key === 'done' || g.key === 'errors' || g.key === 'unfinished') {
      fams = fams.slice().sort((a, b) => (a.finishedMs < b.finishedMs ? 1 : a.finishedMs > b.finishedMs ? -1 : 0));
    }
    const n = fams.reduce((sum, f) => sum + f.count, 0);
    const label = t(g.label);
    // (2026-10-21) Completed: "Completed (N)" holding one node per local date of the finish
    const byDay = g.key === 'done';
    groups.push({
      kind: 'group',
      key: g.key,
      project: ctx.project || null,
      dataDir: ctx.dataDir || null,
      children: byDay ? [...dayGroups(fams, scope, ctx), ...cancelledGroup(cancelled, scope, ctx)] : fams.map((f) => f.node),
      render: {
        id: `group:${scope}:${g.key}`,
        label: byDay ? `${label} (${n})` : label,
        description: byDay ? '' : String(n),
        tooltip: t(n === 1 ? '{0}: {1} task ({2}).' : '{0}: {1} tasks ({2}).', label, n, t(g.hint))
          + `\n${t('Click the arrow to hide or show them.')}`
          + (g.key === 'done' || g.key === 'errors' || g.key === 'unfinished' ? `\n${t('The trash button deletes them all.')}` : ''),
        icon: { id: g.icon, color: g.color },
        contextValue: `group.${g.key}`,
        expanded: g.expanded,
      },
    });
  }
  return groups;
}

/**
 * (2026-10-21, owner "drop dones inside of completed task by date") The children of "Completed": one
 * node per local date of the families' finish (`fams` come newest first, so the dates and the tasks in
 * each date do too). Today's date starts open, older ones closed; the ids are stable
 * (`group:<scope>:done:<YYYY-MM-DD>`), so VS Code keeps the owner's own open/closed state.
 * @param {{node: ItemNode, finished: string, count: number}[]} fams
 * @param {string} scope @param {{now?: Date}} ctx
 * @returns {DayNode[]}
 */
function dayGroups(fams, scope, ctx) {
  const now = ctx.now instanceof Date ? ctx.now : new Date();
  const today = localDay(now.toISOString());
  const lang = i18n.language();
  /** @type {Map<string, {node: ItemNode, finished: string, count: number}[]>} */
  const days = new Map();
  for (const f of fams) {
    const day = localDay(f.finished) || 'undated';
    days.set(day, [...(days.get(day) || []), f]);
  }
  // newest date first; a family without any date goes last
  const keys = [...days.keys()].sort((a, b) => (a === 'undated' ? 1 : b === 'undated' ? -1 : a < b ? 1 : a > b ? -1 : 0));
  return keys.map((day) => {
    const list = /** @type {{node: ItemNode, finished: string, count: number}[]} */ (days.get(day));
    const n = list.reduce((sum, f) => sum + f.count, 0);
    const label = day === 'undated' ? t('No date') : dayLabel(day, now, lang);
    return {
      kind: /** @type {'day'} */ ('day'),
      key: day,
      children: list.map((f) => f.node),
      render: {
        id: `group:${scope}:done:${day}`,
        label: `${label} (${n})`,
        description: '',
        tooltip: t(n === 1 ? '{0}: {1} task finished.' : '{0}: {1} tasks finished.', label, n),
        icon: { id: 'calendar', color: null },
        contextValue: 'group.day',
        expanded: day === today,
      },
    };
  });
}

/**
 * (2026-10-26, owner task 91e164df "do subcategory completed-Canceled") The last child of "Completed":
 * "Cancelled (N)", closed, holding the finished families with a cancelled member, newest first (as the
 * dates); [] when there is none. Its own trash button (`group.cancelled`) deletes them; the trash of
 * "Completed" deletes only its done tasks, the N of its label.
 * @param {{node: ItemNode, finishedMs: number, count: number}[]} fams
 * @param {string} scope @param {{project?: string | null, dataDir?: string | null}} ctx
 * @returns {GroupNode[]}
 */
function cancelledGroup(fams, scope, ctx) {
  if (!fams.length) return [];
  const list = fams.slice().sort((a, b) => (a.finishedMs < b.finishedMs ? 1 : a.finishedMs > b.finishedMs ? -1 : 0));
  const n = list.reduce((sum, f) => sum + f.count, 0);
  const g = CANCELLED_GROUP;
  const label = t(g.label);
  return [{
    kind: 'group',
    key: g.key,
    project: ctx.project || null,
    dataDir: ctx.dataDir || null,
    children: list.map((f) => f.node),
    render: {
      id: `group:${scope}:done:${g.key}`,
      label: `${label} (${n})`,
      description: '',
      tooltip: t(n === 1 ? '{0}: {1} task ({2}).' : '{0}: {1} tasks ({2}).', label, n, t(g.hint))
        + `\n${t('Click the arrow to hide or show them.')}\n${t('The trash button deletes them all.')}`,
      icon: { id: g.icon, color: null },
      contextValue: `group.${g.key}`,
      expanded: false,
    },
  }];
}

// ---- lane nodes (2026-10-15, SPEC_V03 2.3) ----------------------------------------------------
/**
 * Counts and runner state, as for project nodes: `▶ running 1 · 3 pending`, `waiting until 15:45`.
 * @param {{counts: Record<string, number>, runnerAlive: boolean, nextWaitUntil: string | null}} x
 * @param {Date} [now] @param {string[]} [lead] parts that come first
 */
function countsDescription(x, now, lead = []) {
  const c = x.counts;
  const parts = lead.slice();
  if (c.running > 0) parts.push(`▶ ${t('running {0}', c.running)}`);
  else if (c.waiting > 0) {
    const at = format.localTime(x.nextWaitUntil, now);
    parts.push(at ? t('waiting until {0}', at) : t('waiting'));
  } else if (x.runnerAlive) parts.push(`▶ ${t('runner idle')}`);
  if (c.pending > 0) parts.push(t('{0} pending', c.pending));
  if (c.paused > 0) parts.push(t('{0} paused', c.paused));
  if (c.blocked > 0) parts.push(t('{0} need you', c.blocked));
  if (parts.length === lead.length) parts.push(t('idle'));
  return parts.join(' · ');
}

/** "Runner is running (pid N)." or "Runner is stopped." @param {{runnerAlive: boolean, runnerPid?: number | null}} x */
function runnerLine(x) {
  if (!x.runnerAlive) return t('Runner is stopped.');
  return x.runnerPid ? t('Runner is running (pid {0}).', x.runnerPid) : t('Runner is running.');
}

/** @param {import('./parse').Lane} lane @param {Date} [now] */
function laneDescription(lane, now) {
  // (2026-10-21, v049 dontask_hold) a held queue says so first
  // (2026-10-27, usage forecast fix round 2 LOW) a timed hold ends by itself: "⏸ until HH:MM"
  const until = lane.hold && lane.hold.until ? format.localTime(lane.hold.until, now) : null;
  const line = countsDescription(lane, now, lane.hold ? [`⏸ ${until ? t('until {0}', until) : t('on hold')}`] : []);
  // (2026-10-27, U4, 8.10) tasks at once: "Tasks at once: 3 (2 working, 1 merging)", why fewer run, or "runner not responding"
  const par = format.parallelLine(lane, now);
  return par ? `${line} · ${par}` : line;
}

/** A lane can be removed when it is not `default`, has no active item and no live runner (as the core). @param {import('./parse').Lane} lane */
function laneRemovable(lane) {
  return lane.name.toLowerCase() !== DEFAULT_LANE && !lane.runnerAlive
    && ACTIVE.every((s) => !(lane.counts[s] > 0));
}

/**
 * @param {import('./parse').Lane} lane
 * @param {{project?: string | null, projectName?: string} & import('./format').Ctx} [ctx]
 * @returns {LaneNode}
 */
function laneNode(lane, ctx = {}) {
  const project = ctx.project || null;
  const children = itemList(lane.items, {
    ...ctx, runnerAlive: lane.runnerAlive, project, dataDir: lane.dataDir, scoped: true, lane: lane.name,
    limitHeld: !!(lane.hold && lane.hold.reason === 'limit'), // (2026-10-27, limit_continue fix LOW-4)
    parallel: lane.parallel || null, // (2026-10-29, integrate round 8) why a task runs alone
  });
  const label = format.laneLabel(lane.name);
  const tip = [`${t('Queue {0}', label)}${lane.name !== label ? ` (${lane.name})` : ''}`, runnerLine(lane)];
  if (lane.error) tip.push(t('Error: {0}', lane.error));
  // (2026-10-21, v049 dontask_hold)
  if (lane.hold && lane.hold.until) { // (2026-10-27, usage forecast fix round 2 LOW) as the status bar's heldLines
    tip.push(t('Queue {0} waits until {1} (queued for after the usage-limit reset).', label, format.localTime(lane.hold.until, ctx.now) || '?'));
  } else if (lane.hold) tip.push(t('On hold since {0}: no runner starts this queue until you release the hold.', lane.hold.since || '?'));
  const par = format.parallelLine(lane, ctx.now); // (2026-10-27, U4, 8.10)
  if (par) tip.push(par);
  tip.push(t('Data: {0}', lane.dataDir));
  const state = lane.runnerAlive ? 'running' : 'stopped';
  return {
    kind: 'lane',
    lane,
    project,
    projectName: ctx.projectName || '',
    children,
    render: {
      id: `lane:${lane.dataDir}`,
      label,
      description: laneDescription(lane, ctx.now),
      tooltip: tip.join('\n'),
      icon: { id: 'layers', color: null },
      // (2026-10-21, v049 dontask_hold) `.held` between the state and `.removable`: the old regexes
      // (^lane\.stopped, ^lane\.running, ^lane\., \.removable$) still match
      // (2026-10-27, fix round 2 MED) `.held.timed`: Hold queue (makes it indefinite) and Release hold both apply
      contextValue: `lane.${state}${lane.hold ? (lane.hold.until ? '.held.timed' : '.held') : ''}${laneRemovable(lane) ? '.removable' : ''}`,
      expanded: true,
    },
  };
}

/**
 * This window's tree: flat items with one queue, lane nodes with two or more.
 * @param {import('./parse').Lane[]} lanes
 * @param {{project?: string | null, projectName?: string} & import('./format').Ctx} [ctx]
 * @returns {(ItemNode | GroupNode | LaneNode)[]}
 */
function laneTree(lanes, ctx = {}) {
  const list = lanes || [];
  if (list.length >= 2) return list.map((l) => laneNode(l, ctx));
  const only = list[0];
  if (!only) return [];
  return itemList(only.items, { ...ctx, runnerAlive: only.runnerAlive, dataDir: only.dataDir, lane: only.name,
    limitHeld: !!(only.hold && only.hold.reason === 'limit'), // (2026-10-27, limit_continue fix LOW-4)
    parallel: only.parallel || null }); // (2026-10-29, integrate round 8) why a task runs alone
}

/**
 * The lane QuickPick when adding (SPEC_V03 2.3): the last used queue first, then the others in the
 * core's order (`default` first), then `$(add) New queue…`.
 * @param {string[]} names @param {string | null} [lastUsed]
 * @returns {{label: string, lane: string | null, description?: string}[]}
 */
function laneChoices(names, lastUsed) {
  const list = (names || []).slice();
  const i = lastUsed ? list.findIndex((n) => n.toLowerCase() === String(lastUsed).toLowerCase()) : -1;
  if (i > 0) list.unshift(...list.splice(i, 1));
  return [
    ...list.map((n, j) => ({ label: format.laneLabel(n), lane: n, ...(j === 0 && i >= 0 ? { description: t('last used') } : {}) })),
    { label: `$(add) ${t('New queue…')}`, lane: null },
  ];
}

// ---- project nodes (SPEC_PROJECTS 3.1) --------------------------------------------------------
/** @param {import('./parse').Project} row */
function projectLabel(row) {
  return row.shared ? t('(shared queue)') : row.name;
}

/**
 * `▶ running 1 · 3 pending`, `waiting until 15:45`, `idle`; a missing root says so first.
 * @param {import('./parse').Project} row
 * @param {Date} [now]
 */
function projectDescription(row, now) {
  return countsDescription(row, now, !row.shared && !row.rootExists ? [t('folder missing')] : []);
}

/** @param {import('./parse').Project} row */
const isActive = (row) => row.counts.running > 0 || row.counts.waiting > 0;

/**
 * This window's project is expanded; others only when they have running or waiting items.
 * @param {import('./parse').Project} row @param {boolean} isThis
 */
function projectExpanded(row, isThis) {
  return isThis || isActive(row);
}

/** @param {import('./parse').Project} row */
function projectTooltip(row) {
  if (row.shared) return `${t('Shared queue (no project folder)')}\n${t('Data: {0}', row.dataDir)}`;
  const lines = [row.root || row.name];
  if (!row.rootExists) lines.push(t('This folder no longer exists. Its items stay readable and removable.'));
  lines.push(runnerLine(row));
  if (row.error) lines.push(t('Error: {0}', row.error));
  lines.push(t('Data: {0}', row.dataDir));
  return lines.join('\n');
}

/**
 * Sort: this window's project first, then projects with running or waiting items, then the rest by
 * name (case-insensitive). "This project" is matched by the data dir the CORE resolved (amendment
 * M9: no path comparison in the extension).
 * @param {import('./parse').Project[]} rows
 * @param {string | null} thisDataDir
 */
function sortProjects(rows, thisDataDir) {
  const rank = (/** @type {import('./parse').Project} */ r) => (r.dataDir === thisDataDir ? 0 : isActive(r) ? 1 : 2);
  return rows.slice().sort((a, b) => rank(a) - rank(b)
    || projectLabel(a).toLowerCase().localeCompare(projectLabel(b).toLowerCase())
    || a.dataDir.localeCompare(b.dataDir));
}

/**
 * The all-projects tree. This window's project is added (empty) when it has no queue yet.
 * @param {import('./parse').Project[]} rows
 * @param {{thisDataDir: string | null, thisRoot: string | null, thisName: string | null}} self
 * @param {import('./format').Ctx} [ctx]
 * @returns {ProjectNode[]}
 */
function projectTree(rows, self, ctx = {}) {
  const all = rows.slice();
  if (self.thisDataDir && !all.some((r) => r.dataDir === self.thisDataDir)) {
    all.push({
      name: self.thisName || self.thisRoot || t('(no folder)'), dataDir: self.thisDataDir, root: self.thisRoot, // (2026-10-27) translated
      shared: !self.thisRoot, rootExists: true, error: null,
      counts: { pending: 0, running: 0, waiting: 0, paused: 0, done: 0, failed: 0, cancelled: 0, blocked: 0 },
      runnerAlive: false, runnerPid: null, nextWaitUntil: null, items: [], lanes: [],
    });
  }
  return sortProjects(all, self.thisDataDir).map((row) => {
    const isThis = row.dataDir === self.thisDataDir;
    const missing = !row.shared && !row.rootExists;
    const project = row.shared ? null : row.root;
    const lanes = row.lanes || [];
    // (2026-10-15, SPEC_V03 2.3) project -> lane -> items, the lane level only with two or more queues
    /** @type {(ItemNode | GroupNode | LaneNode)[]} */
    const children = lanes.length >= 2
      ? lanes.map((l) => laneNode(l, { ...ctx, project, projectName: projectLabel(row) }))
      : itemList(row.items, {
        ...ctx, runnerAlive: row.runnerAlive, project, dataDir: lanes.length ? lanes[0].dataDir : row.dataDir, scoped: true,
        limitHeld: !!(lanes[0] && lanes[0].hold && lanes[0].hold.reason === 'limit'), // (2026-10-27, fix LOW-4)
        parallel: (lanes[0] && lanes[0].parallel) || null, // (2026-10-29, integrate round 8) why a task runs alone
      });
    return {
      kind: /** @type {'project'} */ ('project'),
      row,
      isThis,
      children,
      render: {
        id: `project:${row.dataDir}`,
        label: isThis ? t('{0} (this window)', projectLabel(row)) : projectLabel(row), // (2026-10-27) one {0} key
        description: projectDescription(row, ctx.now),
        tooltip: projectTooltip(row),
        icon: missing ? { id: 'warning', color: 'list.warningForeground' }
          : { id: row.shared ? 'inbox' : 'root-folder', color: null },
        contextValue: missing ? 'project.missing' : row.runnerAlive ? 'project.running' : 'project.stopped',
        expanded: projectExpanded(row, isThis),
      },
    };
  });
}

/** A setting row, or an item without subtasks (2026-10-21: a main task has children). @param {Node} n */
function isLeaf(n) {
  return n.kind === 'setting' || n.kind === 'hint' || n.kind === 'counter' // (2026-10-26) the Today counter
    || n.kind === 'leftover' // (2026-10-27, U4) a leftover copy folder
    || (n.kind === 'item' && !(n.children && n.children.length));
}

/**
 * (2026-10-26, owner task f60cec52) The Today counter, the first row of the Queue view: this window's
 * project, "Today: 5 done · 3 pending" (lib/stats.js counter); its click opens the statistics.
 * @param {import('./stats').Counter} c @param {string | null} [project]
 * @returns {CounterNode}
 */
function counterNode(c, project) {
  return {
    kind: 'counter',
    counts: c,
    render: {
      id: 'counter',
      label: stats.counterLabel(c),
      description: '',
      tooltip: [project ? t('Today in {0}', project) : t('Today'),
        t('Done and failed: finished today (local day). Pending, running and need you: now.'),
        t('Click for statistics: today and the last 7 days.')].join('\n'),
      icon: { id: 'graph', color: null },
      contextValue: 'counter',
    },
  };
}

/**
 * (2026-10-21, owner "move default for bottom") The row an EMPTY tree shows above the "Defaults for +"
 * section: the section is now always the last node, so VS Code's view welcome (shown only for a tree
 * without nodes) no longer appears; this row says the same and its click is Add….
 * @returns {HintNode}
 */
function emptyNode() {
  return {
    kind: 'hint',
    render: {
      id: 'empty',
      label: t("This project's queue is empty."),
      description: t('Add…'),
      tooltip: t('Click to add a task. The "+ Add new task" panel below and the + in the title bar do the same.'),
      icon: { id: 'info', color: null },
      contextValue: 'empty',
    },
  };
}

// ---- leftover copies (2026-10-27, U4, SPEC_V05_PARALLEL 8.10) -----------------------------------------
/** Why a copy folder is left over (`worktrees --json` leftovers[].reason, worktree.leftovers); an unknown reason shows none. */
const LEFTOVER_REASONS = Object.freeze({
  orphan: 'no task uses it',
  kept: 'an older copy, moved aside',
  removing: 'its removal did not finish',
  missing: 'its task lost this copy',
});
const LEFTOVERS_SHOWN = 50; // more rows help nobody; the folder count is in the label

/**
 * (2026-10-27, U4, 8.10) "Leftover copies (n)": copy folders no task uses (`worktrees --json` leftovers), each with
 * Open folder only (a click; Sleeping Clanker deletes nothing from here). null = none.
 * @param {{path: string, reason: string | null}[] | null | undefined} leftovers
 * @returns {LeftoversNode | null}
 */
function leftoversNode(leftovers) {
  const list = (leftovers || []).filter((l) => l && typeof l.path === 'string' && l.path);
  if (!list.length) return null;
  /** @type {LeftoverNode[]} */
  const children = list.slice(0, LEFTOVERS_SHOWN).map((l) => {
    const base = l.path.split(/[\\/]/).filter(Boolean).pop() || l.path;
    const why = l.reason && Object.prototype.hasOwnProperty.call(LEFTOVER_REASONS, l.reason)
      ? t(LEFTOVER_REASONS[/** @type {keyof typeof LEFTOVER_REASONS} */ (l.reason)]) : '';
    return {
      kind: 'leftover',
      path: l.path,
      render: {
        id: `leftover:${l.path}`,
        label: base,
        description: why,
        tooltip: [l.path, t('A private copy no task uses any more. Double-click: Open folder.')].join('\n'),
        icon: { id: 'folder', color: null },
        contextValue: 'leftover',
      },
    };
  });
  return {
    kind: 'leftovers',
    children,
    render: {
      id: 'leftovers',
      label: t('Leftover copies ({0})', list.length),
      description: '',
      tooltip: t('Private copies of the project that no task uses any more (for example after a crash). Open one to look at it; delete it yourself when you no longer need it.'),
      icon: { id: 'files', color: null },
      contextValue: 'leftovers',
      expanded: false,
    },
  };
}

// ---- the render fingerprint (SPEC_HOVER 1) ---------------------------------------------------
/**
 * A stable string of everything the tree renders. Fire onDidChangeTreeData only when it differs.
 * @param {Node[]} nodes
 */
function fingerprint(nodes) {
  /** @param {Node} n @returns {any} */
  const walk = (n) => (isLeaf(n) // leaves; projects, lanes, groups, main tasks AND the defaults section recurse
    ? [n.render] : [n.render, (n.children || []).map(walk)]);
  return JSON.stringify((nodes || []).map(walk));
}

module.exports = {
  addMenuItems,
  ASK,
  FULL_DEFAULT_LABEL,
  KIND_LABELS,
  defaultsRows,
  defaultsSummary,
  defaultsNode,
  defaultChoices,
  emptyNode,
  counterNode, // (2026-10-26)
  leftoversNode, // (2026-10-27, U4)
  LEFTOVER_REASONS, // (2026-10-27, U4)
  itemNode,
  STATUS_GROUPS,
  CANCELLED_GROUP, // (2026-10-26)
  familyGroup,
  itemList,
  isLeaf,
  countsDescription,
  laneDescription,
  laneRemovable,
  laneNode,
  laneTree,
  laneChoices,
  projectLabel,
  projectDescription,
  projectExpanded,
  projectTooltip,
  sortProjects,
  projectTree,
  fingerprint,
};

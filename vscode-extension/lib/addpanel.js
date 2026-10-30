// @ts-check
// The "+ Add new task" panel (2026-10-21, docs/SPEC_V049_ADD_PANEL.md): a webview view under the Queue
// view with a full-width "+ Add new task" button, the Model row [Provider][Model][Effort] and the
// collapsible "Other settings" (permissions, what + adds, queue, subagents).
// One source of truth: every dropdown reads and writes the user settings the title-bar + already uses
// ("Defaults for +", SPEC_V045_DEFAULTS, plus defaultProvider/defaultModel/defaultEffort), so the panel
// button and the title-bar + run the same add flow with the same choices.
// Security as the Task view (lib/chatview.js): a CSP with a nonce, no remote resource, the webview
// builds its DOM from text only, and EVERY message it posts is checked against the option lists the
// host itself sent (validateMessage). The provider list comes from `providers --json` through
// parse.parseProviders, which keeps names, labels and a key-present boolean: a key never gets here.
// The pure parts need no vscode API; AddPanel takes the vscode module and a host object.
// (2026-10-26, owner 087176fa "show all task possibilities", 32215c07 "change permissions goals etc")
// "Other settings" also holds the `add` options the panel had no row for, for THIS task only (never a
// setting): name (--name), folder (--cwd), attachments (--attach) and, for a goal, --max-rounds,
// --max-hours, --fresh-at-ctx and --fresh-every. The host keeps them (AddPanel.once) and checks every
// value again; one left at its default adds no flag; once the task is queued they go back to the
// defaults. They reach the add flow through an AsyncLocalStorage scope (withOnce/currentOnce), so the
// title-bar + and every other add never see them. Plus "⚙ Chat settings" (moved here from the Task view).
'use strict';

const { AsyncLocalStorage } = require('async_hooks');
const path = require('path');
const argv = require('./argv');
const chatview = require('./chatview');
const format = require('./format');
const view = require('./view');
const { escapeHtml } = require('./markdown');
const { t } = require('./i18n');
const i18n = require('./i18n'); // (2026-10-26) the Language select: AUTO, LANGUAGES and the native NAMES

const VIEW_ID = 'claudAutopilot.addPanel';
const SUBSCRIPTION = 'claude';
// Providers that run Claude's own models: they offer exactly argv.TASK_MODELS (== providers.CLAUDE_MODELS)
const CLAUDE_FAMILY = Object.freeze(['claude', 'claude-api']);
/** Short names for a ~250px dropdown; an unknown provider shows its name. */
const SHORT_LABELS = Object.freeze({
  claude: 'Claude (subscription)', 'claude-api': 'Claude (API key)', deepseek: 'DeepSeek', kimi: 'Kimi',
  glm: 'GLM', qwen: 'Qwen', codex: 'Codex', bedrock: 'Bedrock', custom: 'Custom',
});
// (2026-10-27, U4, SPEC_V05_PARALLEL 8.2) + start: claudAutopilot.newTaskStart (shown only with tasks at once > 1)
const SET_KEYS = Object.freeze(['provider', 'model', 'effort', 'profile', 'kind', 'lane', 'agents', 'ultracode', 'autorun', 'start']);
const ASK_PROFILE = 'ask';
// (2026-10-26) the per-task options; ONCE_NUMBERS are the goal-only ones
// (2026-10-27, U4, 8.2) + where: copy | alone (-> --in-place), shown only with tasks at once > 1
const ONCE_KEYS = Object.freeze(['name', 'cwd', 'maxRounds', 'maxHours', 'freshAtCtx', 'freshEvery', 'where']);
const WHERE_VALUES = Object.freeze(['copy', 'alone']);
const ONCE_NUMBERS = Object.freeze(['maxRounds', 'maxHours', 'freshAtCtx', 'freshEvery']);
const ONCE_TEXT_MAX = 1000;  // a longer typed value is not even read (the name's own limit is argv.NAME_MAX)
const ONCE_PATH_MAX = 4096;
const BROWSE_CWD = ':browse'; // the folder dropdown's "Other folder…": the host opens a folder dialog
const ATTACH_KINDS = Object.freeze(['files', 'folders']);
// Add kinds that never become a goal: the goal rows are hidden and their values not used
const NO_GOAL_KINDS = Object.freeze(['task', 'plan']);
// (2026-10-26, edit in panel) the longest task/reply text the panel's edit box sends back
const EDIT_TEXT_MAX = 200000;

/** The list when `providers --json` cannot be read: only the subscription, which needs nothing. */
const FALLBACK = Object.freeze({
  selected: SUBSCRIPTION,
  providers: Object.freeze([Object.freeze({
    name: SUBSCRIPTION, label: SUBSCRIPTION, needsKey: false, keySet: false, ready: true, missing: Object.freeze([]),
    supportsEffort: true, models: Object.freeze([]), model: null,
  })]),
});

/**
 * @typedef {import('./parse').Providers} Providers
 * @typedef {import('./parse').ProviderInfo} ProviderInfo
 * @typedef {{value: string, label: string, disabled?: boolean, title?: string}} Option
 * @typedef {{value: string, options: Option[], disabled?: boolean, title?: string}} Select
 * @typedef {{labels: Record<string, string>, provider: Select, model: Select, effort: Select,
 *   profile: Select, kind: Select, lane: Select | null, agents: Select, ultracode: Select, autorun: Select, start: Select | null, summary: string,
 *   warning: string, note: string, once: OnceState, language: Select, lang: string}} PanelState
 *   (2026-10-27) lang = the resolved UI language (en | fi | ru): the webview sets <html lang> from it
 * @typedef {{text: Record<string, string>, cwd: string, extraCwd: string, attachments: string[]}} Once
 *   (2026-10-26) what the owner set for the next task: `text` = the typed name and numbers (a key that is
 *   not in it shows its default), `cwd` = the chosen folder ('' = as + does), `extraCwd` = a folder from
 *   "Other folder…", `attachments` = local paths from the host's own file dialog (never from the webview)
 * @typedef {{path: string, name: string}} Folder
 * @typedef {{value: string, placeholder: string, error: string, set: boolean, title: string}} OnceField
 * @typedef {{fields: Record<string, OnceField>, cwd: Select, where: Select | null, attachments: {label: string, title: string}[],
 *   goal: boolean, errors: string[]}} OnceState
 *   (2026-10-27, U4) where = copy | alone; null = one task at a time (no choice)
 * @typedef {{name: string | null, cwd: string | null, attachments: string[], maxRounds: number | null,
 *   maxHours: number | null, freshAtCtx: number | null, freshEvery: number | null, inPlace?: boolean}} OnceOptions
 *   what one add from the panel uses; null / [] = the default (no flag)
 */

/** @param {Providers | null | undefined} cat */
const catalogue = (cat) => /** @type {Providers} */ (cat && cat.providers && cat.providers.length ? cat : FALLBACK);

/** @param {Providers | null | undefined} cat @param {string} name @returns {ProviderInfo | null} */
function providerInfo(cat, name) {
  return catalogue(cat).providers.find((p) => p.name === name) || null;
}

/** The provider `run` uses without --provider (providers.env's choice), so '' means it. @param {Providers | null | undefined} cat */
function runnerProvider(cat) {
  const c = catalogue(cat);
  return c.providers.some((p) => p.name === c.selected) ? c.selected : c.providers[0].name;
}

/** The provider a new task runs on: its own setting, else the runner's. @param {{defaultProvider?: string}} s @param {Providers | null | undefined} cat */
function effectiveProvider(s, cat) {
  return (s && s.defaultProvider) || runnerProvider(cat);
}

/** @param {string} name */
function providerLabel(name) {
  const short = SHORT_LABELS[/** @type {'claude'} */ (name)];
  return short ? t(short) : name;
}

/**
 * The model ids offered for a provider: the core's list (providers.py) and its configured model
 * (<PREFIX>_MODEL in providers.env, e.g. bedrock/custom). (2026-10-21, v049 merge) Claude's own
 * providers offer only the one current list, argv.TASK_MODELS (== providers.CLAUDE_MODELS): an older
 * id from an older core or providers.env is not offered (a task or setting that holds one keeps it).
 * @param {Providers | null | undefined} cat @param {string} name
 * @returns {string[]}
 */
function modelsFor(cat, name) {
  if (CLAUDE_FAMILY.includes(name)) return [...argv.TASK_MODELS];
  const p = providerInfo(cat, name);
  /** @type {string[]} */
  const list = p ? [...p.models] : [];
  if (p && p.model && !list.includes(p.model)) list.push(p.model);
  return list.filter((m) => argv.validModel(m));
}

/**
 * What '' (the default) runs on, for its label: the runner's model setting on the runner's provider,
 * else the provider's configured or first model.
 * @param {Providers | null | undefined} cat @param {string} name @param {{model?: string}} s
 */
function defaultModelOf(cat, name, s) {
  const p = providerInfo(cat, name);
  if (name === runnerProvider(cat) && s && s.model) return s.model;
  return (p && (p.model || p.models[0])) || '';
}

/** @param {boolean} on @param {string} text */
const optional = (on, text) => (on ? text : '');

// ---- (2026-10-26) the per-task options of "Other settings" ------------------------------------------
/** @returns {Once} nothing set: every option at its default */
const freshOnce = () => ({ text: {}, cwd: '', extraCwd: '', attachments: [] });

/** @param {Record<string, string>} o @param {string} k */
const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

/** A path's last part, for a ~250px row (the full path is the title). @param {string} p */
const baseName = (p) => String(p).split(/[\\/]/).filter(Boolean).pop() || String(p);

/** May this add become a goal? Not when + adds a task or a plan. @param {{defaultKind?: string}} s */
const goalRows = (s) => !NO_GOAL_KINDS.includes(String((s && s.defaultKind) || ''));

/**
 * The defaults each field starts from: the goal caps of "Defaults for +" and the core's session caps.
 * @param {import('./argv').Settings} s @returns {Record<string, string>}
 */
function onceDefaults(s) {
  return { name: '', maxRounds: String(s.goalMaxRounds), maxHours: String(s.goalMaxHours),
    freshAtCtx: String(argv.FRESH_AT_CTX_DEFAULT), freshEvery: String(argv.FRESH_EVERY_DEFAULT) };
}

/**
 * One typed per-task value, checked as the core checks it. '' or the default = null (no flag).
 * @param {string} key name or one of ONCE_NUMBERS @param {unknown} text @param {import('./argv').Settings} s
 * @returns {{value: string | number | null, error: string}}
 */
function parseOnce(key, text, s) {
  const v = String(text == null ? '' : text).trim();
  if (!v) return { value: null, error: '' };
  if (key === 'name') {
    return argv.validName(v) ? { value: v, error: '' }
      : { value: null, error: t('Name: one line of at most {0} characters.', String(argv.NAME_MAX)) };
  }
  let n = NaN;
  let ok = false;
  let error = '';
  if (key === 'maxRounds') {
    n = /^-?\d{1,6}$/.test(v) ? Number(v) : NaN;
    ok = argv.validRounds(n);
    error = t('Max rounds: a whole number from 1 to {0}, or -1 for unlimited.', String(argv.MAX_ROUNDS_LIMIT));
  } else if (key === 'maxHours') {
    n = /^\d{1,6}(\.\d{1,3})?$/.test(v) ? Number(v) : NaN;
    ok = argv.validHours(n);
    error = t('Max hours: a number above 0 and at most {0}.', String(argv.MAX_HOURS_LIMIT));
  } else if (key === 'freshAtCtx') {
    n = /^\d{1,9}$/.test(v) ? Number(v) : NaN;
    ok = argv.validFresh(n, argv.FRESH_AT_CTX_LIMIT);
    error = t('New session at context: a whole number of tokens from 0 (off) to {0}.', String(argv.FRESH_AT_CTX_LIMIT));
  } else if (key === 'freshEvery') {
    n = /^\d{1,6}$/.test(v) ? Number(v) : NaN;
    ok = argv.validFresh(n, argv.FRESH_EVERY_LIMIT);
    error = t('New session every: a whole number of rounds from 0 (off) to {0}.', String(argv.FRESH_EVERY_LIMIT));
  } else {
    return { value: null, error: '' };
  }
  if (!ok) return { value: null, error };
  return { value: n === Number(onceDefaults(s)[key]) ? null : n, error: '' };
}

/**
 * Store one accepted 'once' message. Pure. @param {Once} once @param {string} key @param {string} value
 * @returns {Once}
 */
function setOnce(once, key, value) {
  if (key === 'cwd') return { ...once, cwd: value };
  return { ...once, text: { ...once.text, [key]: value } };
}

/** The folders a task may run in: the local workspace folders and one picked with "Other folder…". @param {Folder[]} folders @param {string} extra */
function cwdChoices(folders, extra) {
  const list = folders.map((f) => f.path);
  if (extra && !list.includes(extra)) list.push(extra);
  return list;
}

/**
 * The "This task only" rows of Other settings. Pure.
 * @param {Once | null | undefined} once @param {import('./argv').Settings} s @param {Folder[]} [folders]
 * @returns {OnceState}
 */
function onceState(once, s, folders = []) {
  const o = once || freshOnce();
  const d = onceDefaults(s);
  const goal = goalRows(s);
  const titles = /** @type {Record<string, string>} */ ({
    name: t('A short label for the task in the queue (--name).'),
    maxRounds: t('The goal stops after this many rounds; -1 = unlimited, until it is stopped (--max-rounds).'),
    maxHours: t('The goal stops after this many hours (--max-hours).'),
    freshAtCtx: t('Start the next round in a new session once the context reached this many tokens; 0 = off (--fresh-at-ctx).'),
    freshEvery: t('Start a new session every N rounds; 0 = off (--fresh-every).'),
  });
  /** @type {Record<string, OnceField>} */
  const fields = {};
  for (const key of ['name', ...ONCE_NUMBERS]) {
    const typed = has(o.text, key);
    const r = typed && (key === 'name' || goal) ? parseOnce(key, o.text[key], s) : { value: null, error: '' };
    fields[key] = { value: typed ? o.text[key] : d[key], placeholder: d[key], error: r.error, set: r.value !== null, title: titles[key] };
  }
  /** @type {Option[]} */
  const cwdOpts = [{ value: '', label: folders.length === 1 ? t('Default ({0})', folders[0].name) : t(view.ASK),
    title: t('As +: the workspace folder, asked when there are several.') }];
  for (const f of folders) cwdOpts.push({ value: f.path, label: f.name, title: f.path });
  if (o.extraCwd && !folders.some((f) => f.path === o.extraCwd)) cwdOpts.push({ value: o.extraCwd, label: baseName(o.extraCwd), title: o.extraCwd });
  cwdOpts.push({ value: BROWSE_CWD, label: t('Other folder…'), title: t('Run this task in another folder (--cwd).') });
  const cwd = o.cwd && o.cwd !== BROWSE_CWD && cwdOpts.some((x) => x.value === o.cwd) ? o.cwd : '';
  const errors = Object.values(fields).map((f) => f.error).filter(Boolean);
  return { fields, cwd: { value: cwd, options: cwdOpts }, where: whereSelect(o, s),
    attachments: o.attachments.map((p) => ({ label: baseName(p), title: p })), goal, errors };
}

/** (2026-10-27, U4, 8.2) Tasks at once > 1: several tasks of a queue run together. @param {{tasksAtOnce?: number}} s */
const parallelOn = (s) => Number.isInteger(s && s.tasksAtOnce) && /** @type {number} */ (s.tasksAtOnce) > 1;

/**
 * (2026-10-27, U4, SPEC_V05_PARALLEL 8.2) "Runs" of this task: in its own private copy (the default) or alone in the
 * project folder (`--in-place`: nothing else runs beside it). null = tasks at once is 1.
 * @param {Once} o @param {import('./argv').Settings} s @returns {Select | null}
 */
function whereSelect(o, s) {
  if (!parallelOn(s)) return null;
  return { value: o.text.where === 'alone' ? 'alone' : 'copy',
    title: t('Where this task runs while several tasks run at once.'),
    options: [{ value: 'copy', label: t('In its own copy'), title: t('A private copy of the project; its changes are merged back when it finishes.') },
      { value: 'alone', label: t('Alone in the project folder'), title: t('Nothing else runs beside it (--in-place).') }] };
}

/**
 * (2026-10-26, owner "Valinta + VS Code-kieli") The Language select of Other settings: the GLOBAL UI
 * language setting (auto | en | ru | fi; the gear's changeLanguage path), never a per-task option.
 * "Auto (VS Code)" in the UI language, the three languages in their native names. Pure.
 * @param {unknown} setting @returns {Select}
 */
function languageSelect(setting) {
  const all = [i18n.AUTO, ...i18n.LANGUAGES];
  const value = all.includes(String(setting)) ? String(setting) : i18n.AUTO;
  return {
    value, title: t('The language of the Queue view, the status bar, the hovers, the New Task panel and the Task view.'),
    options: all.map((id) => (id === i18n.AUTO ? { value: id, label: t('Auto (VS Code)'), title: t('the VS Code display language') }
      : { value: id, label: i18n.NAMES[/** @type {'en'} */ (id)] })),
  };
}

/** The summary bits of what is set for this task (accurate: only what the next add uses). @param {OnceState} st */
function onceSummary(st) {
  /** @type {string[]} */
  const bits = [];
  const f = st.fields;
  if (f.name.set) bits.push(`"${f.name.value.trim().length > 24 ? `${f.name.value.trim().slice(0, 23)}…` : f.name.value.trim()}"`);
  if (st.cwd.value) bits.push(t('in {0}', baseName(st.cwd.value)));
  if (st.attachments.length) bits.push(t('{0} attached', String(st.attachments.length)));
  if (st.where && st.where.value === 'alone') bits.push(t('alone in the project folder')); // (2026-10-27, U4)
  if (st.goal) {
    if (f.maxRounds.set) bits.push(t('{0} rounds', f.maxRounds.value.trim()));
    if (f.maxHours.set) bits.push(t('{0} h', f.maxHours.value.trim()));
    if (f.freshAtCtx.set) bits.push(t('new session at {0}', f.freshAtCtx.value.trim()));
    if (f.freshEvery.set) bits.push(t('new session every {0}', f.freshEvery.value.trim()));
  }
  if (st.errors.length) bits.push(`⚠ ${t('check the options')}`);
  return bits;
}

/**
 * The options one add from the panel uses, checked again in the host. Throws the first field's message
 * (nothing is added); goal-only values are not read when + adds a task or a plan.
 * @param {Once | null | undefined} once @param {import('./argv').Settings} s @param {Folder[]} [folders]
 * @returns {OnceOptions}
 */
function onceOptions(once, s, folders = []) {
  const o = once || freshOnce();
  /** @type {OnceOptions} */
  const out = { name: null, cwd: null, attachments: [], maxRounds: null, maxHours: null, freshAtCtx: null, freshEvery: null };
  for (const key of ['name', ...(goalRows(s) ? ONCE_NUMBERS : [])]) {
    if (!has(o.text, key)) continue;
    const r = parseOnce(key, o.text[key], s);
    if (r.error) throw new Error(r.error);
    /** @type {any} */ (out)[key] = r.value;
  }
  if (o.cwd) {
    if (!cwdChoices(folders, o.extraCwd).includes(o.cwd)) throw new Error(t('The folder {0} is not offered any more: choose the folder again.', o.cwd));
    out.cwd = o.cwd;
  }
  for (const a of o.attachments) if (typeof a === 'string' && a && !out.attachments.includes(a)) out.attachments.push(a);
  if (parallelOn(s) && o.text.where === 'alone') out.inPlace = true; // (2026-10-27, U4, 8.2) -> --in-place
  return out;
}

/**
 * What addPrompt/addFile add to argv.addArgs for this task; {} when nothing is set (the argv stays as
 * it was). The goal-only session caps only for a goal; attachments are added to the flow's own.
 * @param {OnceOptions | null} once @param {boolean} goal @param {string[]} [attachments]
 * @returns {{name?: string, attachments?: string[], freshAtCtx?: number, freshEvery?: number, inPlace?: boolean}}
 */
function onceTask(once, goal, attachments = []) {
  /** @type {{name?: string, attachments?: string[], freshAtCtx?: number, freshEvery?: number, inPlace?: boolean}} */
  const out = {};
  if (!once) return out;
  if (once.name) out.name = once.name;
  if (once.inPlace === true) out.inPlace = true; // (2026-10-27, U4, 8.2)
  if (once.attachments.length) out.attachments = [...new Set([...(attachments || []), ...once.attachments])];
  if (goal && once.freshAtCtx !== null) out.freshAtCtx = once.freshAtCtx;
  if (goal && once.freshEvery !== null) out.freshEvery = once.freshEvery;
  return out;
}

/**
 * A goal's caps set in the panel, else null: + then asks or uses the settings exactly as before
 * (pickGoalCaps). One set cap takes the other from the settings and asks nothing.
 * @param {OnceOptions | null} once @param {import('./argv').Settings} s
 * @returns {{maxRounds: number, maxHours: number} | null}
 */
function onceCaps(once, s) {
  if (!once || (once.maxRounds === null && once.maxHours === null)) return null;
  return { maxRounds: once.maxRounds !== null ? once.maxRounds : s.goalMaxRounds, maxHours: once.maxHours !== null ? once.maxHours : s.goalMaxHours };
}

// The scope of one add from the panel: its options and whether a task was queued.
const onceScope = new AsyncLocalStorage();

/**
 * Run the host's add flow with `opts` as this add's options (currentOnce() inside it). The scope is
 * emptied when the flow ends, so a timer or child started inside it never hands them to a later add.
 * @param {OnceOptions} opts @param {() => Promise<void>} fn @returns {Promise<boolean>} a task was queued
 */
async function withOnce(opts, fn) {
  const box = { opts: /** @type {OnceOptions | null} */ (opts), queued: false };
  try {
    await onceScope.run(box, fn);
  } finally {
    box.opts = null;
  }
  return box.queued;
}

/** The panel's options for the add running now, or null (any other add). @returns {OnceOptions | null} */
function currentOnce() {
  const box = onceScope.getStore();
  return box && box.opts ? box.opts : null;
}

/** The add running now queued its task(s): the panel's options go back to the defaults. */
function onceQueued() {
  const box = onceScope.getStore();
  if (box && box.opts) box.queued = true;
}

/**
 * The panel's state: every dropdown with its options (values the host accepts back) and the value
 * shown. Pure; `lanes` = the project's queue names (the Queue row only appears with two or more).
 * @param {import('./argv').Settings} s @param {Providers | null | undefined} cat @param {string[]} [lanes]
 * @param {{failed?: boolean, once?: Once | null, folders?: Folder[]}} [opts] failed = `providers --json` could not be
 *   read; (2026-10-26) once/folders = this task's options and the workspace folders
 * @returns {PanelState}
 */
function panelState(s, cat, lanes = [], opts = {}) {
  const c = catalogue(cat);
  const runner = runnerProvider(c);
  const prov = effectiveProvider(s, c);
  const info = providerInfo(c, prov);
  const ordered = [...c.providers].sort((a, b) => Number(b.name === SUBSCRIPTION) - Number(a.name === SUBSCRIPTION));
  /** @type {Option[]} */
  const providers = ordered.map((p) => {
    const missing = !p.ready;
    const label = missing ? t(p.needsKey && !p.keySet ? '{0} (no key)' : '{0} (not set up)', providerLabel(p.name)) : providerLabel(p.name);
    const title = [p.label, optional(p.name === runner, t("The runner's provider: new tasks use it when nothing else is chosen.")),
      optional(missing && p.missing.length > 0, t('Needs {0} in providers.env.', p.missing.join(', ')))].filter(Boolean).join('\n');
    return { value: p.name, label, disabled: missing && p.name !== prov, title };
  });
  if (!info) providers.push({ value: prov, label: t('{0} (unknown)', prov), disabled: true, title: t('Not in the core\'s provider list.') });

  const models = modelsFor(c, prov);
  const model = s.defaultModel || '';
  const dflt = defaultModelOf(c, prov, s);
  /** @type {Option[]} */
  const modelOpts = [{ value: '', label: dflt ? t('Default ({0})', dflt) : t('Default'), title: t("The provider's default model.") }];
  for (const m of models) modelOpts.push({ value: m, label: m });
  if (model && !models.includes(model)) modelOpts.push({ value: model, label: model, title: t('Not in this provider\'s list.') });

  const effortOk = !info || info.supportsEffort;
  const effort = effortOk ? (s.defaultEffort || '') : '';
  /** @type {Select} */
  const effortSel = {
    value: effort,
    options: [{ value: '', label: t('Default') }, ...argv.EFFORTS.map((e) => ({ value: e, label: e }))],
    disabled: !effortOk,
    title: effortOk ? t('How hard the model thinks (--effort). Default = the runner\'s.')
      : t('{0} takes no effort setting (an Anthropic-model feature).', providerLabel(prov)),
  };

  const keyed = !!info && info.needsKey;
  const ask = t(view.ASK);
  /** @type {Option[]} */
  const profileOpts = [{ value: ASK_PROFILE, label: ask, title: t('Show the permission picker.') }];
  for (const p of argv.DEFAULT_PROFILES) {
    const refused = keyed && !argv.NO_SHELL_PROFILES.includes(p);
    // a profile the provider refuses cannot be picked (the add would be refused: argv.providerRefusal)
    profileOpts.push({ value: p, label: p, disabled: refused, title: refused ? t('Refused with a provider that needs an API key.')
      : t(format.PROFILE_LINES[/** @type {'safe'} */ (p)] || '') }); // (2026-10-26) translated
  }
  // (2026-10-21, owner "Lisää varoituksella") `full` ALWAYS last, with its warning; the host asks one
  // modal confirmation before it is saved (cancel = the old default stays)
  profileOpts.push({ value: argv.FULL_PROFILE, label: `⚠ ${t(view.FULL_DEFAULT_LABEL)}`, disabled: keyed,
    title: keyed ? t('Refused with a provider that needs an API key.') : t(format.PROFILE_LINES.full) });
  const profile = s.askProfile ? ASK_PROFILE : s.defaultProfile;

  /** @type {Option[]} */
  const kindOpts = argv.ADD_KINDS.map((k) => ({ value: k, label: t(view.KIND_LABELS[/** @type {'ask'} */ (k)] || view.ASK) }));

  /** @type {Select | null} */
  let laneSel = null;
  if (lanes.length >= 2) {
    /** @type {Option[]} */
    const laneOpts = [{ value: '', label: ask }, ...lanes.map((n) => ({ value: n, label: format.laneLabel(n) }))];
    const found = lanes.find((n) => n.toLowerCase() === String(s.defaultLane || '').toLowerCase());
    if (s.defaultLane && !found) laneOpts.push({ value: s.defaultLane, label: t('{0} (gone)', s.defaultLane), disabled: true });
    laneSel = { value: found || s.defaultLane || '', options: laneOpts };
  }

  /** @type {Option[]} */
  const agentOpts = argv.AGENT_LIMITS.map((a) => ({ value: a.value, label: t(a.label) }));
  const agents = s.defaultMaxAgents || 'default';
  if (!agentOpts.some((o) => o.value === agents)) agentOpts.push({ value: agents, label: `${t('Subagents')}: ${agents}` });

  // (2026-10-26, modes) Ultracode next to the effort, Autorun in Other settings: Off / On of every new task
  const onOff = (/** @type {boolean} */ on, /** @type {string} */ tip) => ({ value: on ? 'on' : 'off', title: t(tip),
    options: [{ value: 'off', label: t('Off') }, { value: 'on', label: t('On') }] });
  const pick = (/** @type {Option[]} */ list, /** @type {string} */ v) => (v === argv.FULL_PROFILE && list === profileOpts
    ? `⚠ ${v}` : (list.find((o) => o.value === v) || { label: v }).label);
  const once = onceState(opts.once, s, opts.folders || []); // (2026-10-26) this task's options, in the summary too
  const summary = [pick(profileOpts, profile), pick(kindOpts, s.defaultKind),
    ...(laneSel && laneSel.value ? [pick(laneSel.options, laneSel.value)] : []), pick(agentOpts, agents),
    ...(s.defaultAutorun ? [t('Autorun')] : []), ...onceSummary(once)].join(' · ');
  const warning = keyed && !s.askProfile && !argv.NO_SHELL_PROFILES.includes(s.defaultProfile)
    ? t('Profile {0} is refused with a provider that needs an API key: choose safe, edits or plan.', s.defaultProfile) : '';
  return {
    labels: {
      add: t('+ Add new task'), addTitle: t('Adds a task with these choices, like the + in the title bar. They are saved in your user settings.'),
      model: t('Model:'), provider: t('Provider'), modelSel: t('Model'), effort: t('Effort'),
      other: t('Other settings: {0}', summary), profile: t('Permissions'), kind: t('Add'), lane: t('Queue'), agents: t('Subagents'),
      ultracode: t('Ultracode'), autorun: t('Autorun'), // (2026-10-26, modes)
      start: t('New tasks start'), where: t('Runs'), // (2026-10-27, U4, 8.2)
      // (2026-10-26) the chat settings (moved from the Task view) and this task's own options
      chatSettings: `⚙ ${t('Chat settings')}`,
      chatSettingsTitle: t('ADHD mode, Auto-pick best in new chats, quick prompts, the "What did you change?" text and the language. Global settings… has the rest.'),
      once: t('This task only (back to the defaults once it is added):'), name: t('Name'), cwd: t('Folder'), attach: t('Attach'),
      attachFiles: t('Files…'), attachFolders: t('Folder…'), unattach: t('Clear'), goal: t('Goal only:'),
      maxRounds: t('Max rounds'), maxHours: t('Max hours'), freshAtCtx: t('New session at tokens'), freshEvery: t('New session every N rounds'),
      reset: t('Reset'), resetTitle: t('Set this task\'s options back to the defaults.'),
      language: t('Language'), // (2026-10-26) a global setting, not this task's
    },
    provider: { value: prov, options: providers },
    model: { value: model, options: modelOpts },
    effort: effortSel,
    profile: { value: profile, options: profileOpts },
    kind: { value: s.defaultKind, options: kindOpts },
    lane: laneSel,
    agents: { value: agents, options: agentOpts },
    // (2026-10-27, modes fix HIGH) only with a `full` default and subagents (argv.ultracodeRefusal): else Off, disabled
    ultracode: argv.ultracodeRefusal(s.askProfile ? '' : s.defaultProfile, agents)
      ? { ...onOff(false, argv.ultracodeRefusal(s.askProfile ? '' : s.defaultProfile, agents)), disabled: true }
      : onOff(!!s.defaultUltracode, argv.ULTRACODE_TIP),
    autorun: onOff(!!s.defaultAutorun, argv.AUTORUN_TIP),
    start: startSelect(s), // (2026-10-27, U4, 8.2)
    summary,
    warning,
    note: opts.failed ? t('Could not read the provider list; only Claude (subscription) is offered.') : '',
    once,
    language: languageSelect(s.language), // (2026-10-26)
    // (2026-10-27, i18n audit L5) panelHtml sets <html lang> once; a language switch re-sends the state
    lang: i18n.language(),
  };
}

/**
 * (2026-10-27, U4, SPEC_V05_PARALLEL 8.2) When a new task may start (claudAutopilot.newTaskStart): as soon as a slot is
 * free, or only after the task added before it. null = tasks at once is 1 (they run one after another anyway).
 * @param {import('./argv').Settings} s @returns {Select | null}
 */
function startSelect(s) {
  if (!parallelOn(s)) return null;
  return { value: s.newTaskStart === 'afterPrevious' ? 'afterPrevious' : 'whenFree',
    title: t('When several tasks run at once: may a new task start beside the others, or only after the task added before it?'),
    options: [{ value: 'whenFree', label: t('When a slot is free') },
      { value: 'afterPrevious', label: t('After the previous task') }] };
}

/**
 * Accept a webview message only when it is one of ours and every value is one the host offered (not
 * disabled) in `state`; anything else = null, and nothing happens.
 * (2026-10-26) Also: chatSettings, reset, unattach, attach {what: files|folders} (the host opens its own
 * dialog: a path never comes from the webview) and once {key, value}: a folder must be one the host
 * offered; a name or number is any short text here, checked by parseOnce (a bad one shows its message).
 * (2026-10-26, edit in panel) `saveEdit`/`cancelEdit` only for the edit open now (`editSeq`), with a
 * non-empty text of at most EDIT_TEXT_MAX characters; the host saves on its own target, never an id
 * from the webview.
 * @param {unknown} raw @param {PanelState | null} state @param {number | null} [editSeq]
 * @returns {{type: 'ready'} | {type: 'add'} | {type: 'set', key: string, value: string} | {type: 'chatSettings'}
 *   | {type: 'reset'} | {type: 'unattach'} | {type: 'attach', what: string} | {type: 'once', key: string, value: string}
 *   | {type: 'saveEdit', text: string} | {type: 'cancelEdit'} | {type: 'language', value: string} | null}
 */
function validateMessage(raw, state, editSeq = null) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const m = /** @type {Record<string, unknown>} */ (raw);
  if (m.type === 'ready') return { type: 'ready' };
  if (m.type === 'add') return { type: 'add' };
  if (m.type === 'chatSettings' || m.type === 'reset' || m.type === 'unattach') return { type: m.type };
  if (m.type === 'attach') return typeof m.what === 'string' && ATTACH_KINDS.includes(m.what) ? { type: 'attach', what: m.what } : null;
  if (m.type === 'language') { // (2026-10-26) only a language the host offered
    const sel = state && state.language;
    return sel && typeof m.value === 'string' && sel.options.some((o) => o.value === m.value) ? { type: 'language', value: m.value } : null;
  }
  if (m.type === 'once') {
    const { key, value } = m;
    if (!state || !state.once || typeof key !== 'string' || !ONCE_KEYS.includes(key) || typeof value !== 'string') return null;
    if (key === 'cwd') {
      const opt = value.length <= ONCE_PATH_MAX ? state.once.cwd.options.find((o) => o.value === value) : null;
      return opt && !opt.disabled ? { type: 'once', key, value } : null;
    }
    // (2026-10-27, U4, 8.2) where: only copy | alone, and only while the panel offers it
    if (key === 'where') return state.once.where && WHERE_VALUES.includes(value) ? { type: 'once', key, value } : null;
    if (value.length > ONCE_TEXT_MAX || (key !== 'name' && !state.once.goal)) return null;
    return { type: 'once', key, value };
  }
  if (m.type === 'saveEdit' || m.type === 'cancelEdit') {
    if (editSeq === null || m.seq !== editSeq) return null;
    if (m.type === 'cancelEdit') return { type: 'cancelEdit' };
    const text = m.text;
    if (typeof text !== 'string' || !text.trim() || text.length > EDIT_TEXT_MAX) return null;
    return { type: 'saveEdit', text };
  }
  if (m.type !== 'set' || !state) return null;
  const { key, value } = m;
  if (typeof key !== 'string' || !SET_KEYS.includes(key) || typeof value !== 'string' || value.length > 200) return null;
  const sel = /** @type {Select | null} */ (/** @type {any} */ (state)[key]);
  if (!sel || sel.disabled) return null;
  const opt = sel.options.find((o) => o.value === value);
  if (!opt || opt.disabled) return null;
  return { type: 'set', key, value };
}

/**
 * The user-settings patch one accepted choice writes. A provider that is the runner's is stored as ''
 * (= follow the runner); a new provider resets the model (another list) and drops an effort it cannot
 * take. `full` is passed on as asked: the host's update (_updateDefaults) asks its one modal confirmation
 * and writes the machine-local marker; without both it is saved as safe.
 * @param {string} key @param {string} value @param {import('./argv').Settings} s @param {Providers | null | undefined} cat
 * @returns {Record<string, string | boolean>}
 */
function settingsPatch(key, value, s, cat) {
  if (key === 'provider') {
    const stored = value === runnerProvider(cat) ? '' : value;
    /** @type {Record<string, string | boolean>} */
    const patch = { defaultProvider: stored };
    if (value !== effectiveProvider(s, cat)) {
      if (s.defaultModel && !modelsFor(cat, value).includes(s.defaultModel)) patch.defaultModel = '';
      const info = providerInfo(cat, value);
      if (s.defaultEffort && info && !info.supportsEffort) patch.defaultEffort = '';
    }
    return patch;
  }
  if (key === 'model') return { defaultModel: value };
  if (key === 'effort') return { defaultEffort: value };
  if (key === 'profile') return value === ASK_PROFILE ? { askProfile: true } : { askProfile: false, defaultProfile: value };
  if (key === 'kind') return { defaultKind: value };
  if (key === 'lane') return { defaultLane: value };
  if (key === 'agents') return { defaultMaxAgents: value };
  if (key === 'ultracode') return { defaultUltracode: value === 'on' }; // (2026-10-26, modes)
  if (key === 'autorun') return { defaultAutorun: value === 'on' };
  if (key === 'start') return { newTaskStart: value === 'afterPrevious' ? 'afterPrevious' : 'whenFree' }; // (2026-10-27, U4)
  return {};
}

/**
 * The page: an empty form; media/addpanel.js fills it from the host's `state` message.
 * @param {{cspSource: string, nonce: string, scriptUri: string, styleUri: string, lang?: string, addLabel?: string}} o
 */
function panelHtml(o) {
  const nonce = String(o.nonce);
  if (!/^[A-Za-z0-9_-]{16,}$/.test(nonce)) throw new Error('bad nonce');
  const lang = /^[a-z]{2}$/.test(String(o.lang || '')) ? o.lang : 'en';
  const field = (/** @type {string} */ id) => `<label class="field" id="${id}Row"><span id="${id}Lbl"></span><select id="${id}"></select></label>`;
  // (2026-10-26) a typed per-task value and its error line (the host's message)
  const input = (/** @type {string} */ id, /** @type {string} */ attrs) => `<label class="field once" id="${id}Row"><span id="${id}Lbl"></span>`
    + `<input id="${id}" type="text" spellcheck="false" ${attrs}></label>\n<div class="err" id="${id}Err" hidden></div>`;
  return `<!DOCTYPE html>
<html lang="${lang}">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${chatview.csp(String(o.cspSource).replace(/["<>]/g, ''), nonce)}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${escapeHtml(o.styleUri)}">
</head>
<body>
<div id="edit" class="edit" hidden>
<div class="lbl" id="editTitle"></div>
<textarea id="editText" rows="6" spellcheck="true"></textarea>
<div id="editNote" class="note" hidden></div>
<div class="btns"><button id="editSave" type="button"></button><button id="editCancel" type="button" class="secondary"></button></div>
<div id="editErr" class="warn" hidden></div>
</div>
<button id="add" type="button">${escapeHtml(o.addLabel || t('+ Add new task'))}</button>
<div class="lbl" id="modelLbl"></div>
<div class="three"><select id="provider"></select><select id="model"></select><select id="effort"></select></div>
${field('ultracode')}
<div id="warn" class="warn" hidden></div>
<details id="other"><summary id="otherLbl"></summary>
${field('profile')}
${field('kind')}
${field('lane')}
${field('agents')}
${field('autorun')}
${field('start')}
<button id="chatSettings" type="button" class="secondary"></button>
${field('language')}
<div class="lbl" id="onceLbl"></div>
${input('name', `maxlength="${argv.NAME_MAX}"`)}
${field('cwd')}
${field('where')}
<div class="field" id="attachRow"><span id="attachLbl"></span><span class="btns"><button id="attachFiles" type="button" class="secondary"></button><button id="attachFolders" type="button" class="secondary"></button><button id="unattach" type="button" class="secondary"></button></span></div>
<ul id="attached" class="attached"></ul>
<div id="goalRows">
<div class="lbl" id="goalLbl"></div>
${ONCE_NUMBERS.map((k) => input(k, 'inputmode="decimal" class="num"')).join('\n')}
</div>
<button id="reset" type="button" class="secondary"></button>
</details>
<div id="note" class="note" hidden></div>
<script nonce="${nonce}" src="${escapeHtml(o.scriptUri)}"></script>
</body>
</html>
`;
}

/**
 * (2026-10-26, edit in panel) What the webview shows for an open edit: text only.
 * @param {{seq: number, id: string, reply?: boolean, restart?: boolean}} e @param {string} text
 */
function editView(e, text) {
  return {
    seq: e.seq, id: e.id, text, max: EDIT_TEXT_MAX,
    tooLong: t('Too long: at most {0} characters.', String(EDIT_TEXT_MAX)),
    title: e.reply ? t('Editing your reply to {0}: change it, then Save.', e.id) : t('Editing {0}: change the text, then Save.', e.id),
    note: e.restart ? t('It has started: Save starts it over with the new text when you resume it.') : '',
    save: t('Save'), cancel: t('Cancel edit'), hint: t('Ctrl+Enter saves, Esc cancels'),
  };
}

/**
 * @typedef {{settings: () => import('./argv').Settings, lanes: () => string[],
 *   providers: (reload: boolean) => Promise<Providers | null>, update: (patch: Record<string, any>) => Promise<void>,
 *   add: () => Promise<void>, log: (line: string) => void, guarded: (what: string, fn: () => Promise<void>) => Promise<void>,
 *   chatSettings?: () => Promise<void>, language?: (id: string) => Promise<void>,
 *   saveEdit?: (target: any, text: string) => Promise<void>, reportError?: (what: string, err: unknown) => void}} Host
 *   (2026-10-26) chatSettings: the chat settings QuickPick without a chat (TaskViews._chatSettings(null));
 *   language: save the GLOBAL UI language (Autopilot.setUiLanguage, the gear's path)
 */

/** The webview view provider (registered in extension.js). */
class AddPanel {
  /** @param {any} vscode @param {string} extensionPath @param {Host} host */
  constructor(vscode, extensionPath, host) {
    this.vscode = vscode;
    this.mediaDir = path.join(extensionPath, 'media');
    this.host = host;
    /** @type {any} */
    this.view = null;
    /** @type {Providers | null} */
    this.cat = null;
    this.failed = false;
    /** @type {PanelState | null} */
    this.state = null;
    /** (2026-10-26, speed) the providers read in flight (_readProviders) @type {Promise<void> | null} */
    this._loading = null;
    /** (2026-10-26) this task's own options; back to freshOnce() once a task is queued @type {Once} */
    this.once = freshOnce();
    /** (2026-10-26, edit in panel) the edit open in the panel; `target` stays on the host side @type {{seq: number, id: string, reply: boolean, restart: boolean, text: string, target: any} | null} */
    this.edit = null;
    // (tester MED) a new number per session: a draft the webview kept across a reload never matches a new edit
    this.editSeq = Date.now();
  }

  /** (2026-10-26) The local workspace folders (as pickCwd offers them). @returns {Folder[]} */
  folders() {
    const ws = this.vscode && this.vscode.workspace;
    return ((ws && ws.workspaceFolders) || []).filter((/** @type {any} */ f) => f && f.uri && f.uri.scheme === 'file')
      .map((/** @type {any} */ f) => ({ path: String(f.uri.fsPath), name: String(f.name) }));
  }

  /**
   * (2026-10-26, edit in panel) The Queue pencil: the task text (or the queued reply) goes into the
   * panel's edit box instead of a new editor or input box. A second edit replaces the first.
   * @param {any} target what saveEditText needs (host side only) @param {string} text
   */
  async startEdit(target, text) {
    this.editSeq += 1;
    const item = (target && target.item) || {};
    this.edit = { seq: this.editSeq, id: String(item.id || ''), reply: !!(target && target.reply), restart: !!(target && target.restart), text, target };
    await this.postEdit();
  }

  /** Close the edit box (saved, cancelled or replaced). */
  async endEdit() {
    this.edit = null;
    await this.postEdit();
  }

  async postEdit() {
    if (!this.view) return; // sent on `ready` when the view comes up
    await this.view.webview.postMessage({ type: 'edit', edit: this.edit ? editView(this.edit, this.edit.text) : null });
  }

  webviewOptions() {
    return { enableScripts: true, enableCommandUris: false, enableForms: false, localResourceRoots: [this.vscode.Uri.file(this.mediaDir)] };
  }

  /** @param {any} webviewView */
  resolveWebviewView(webviewView) {
    this.view = webviewView;
    const webview = webviewView.webview;
    webview.options = this.webviewOptions();
    const uri = (/** @type {string} */ f) => webview.asWebviewUri(this.vscode.Uri.file(path.join(this.mediaDir, f))).toString();
    webview.html = panelHtml({ cspSource: webview.cspSource, nonce: chatview.makeNonce(), scriptUri: uri('addpanel.js'),
      styleUri: uri('addpanel.css'), lang: require('./i18n').language(), addLabel: t('+ Add new task') });
    webview.onDidReceiveMessage((/** @type {unknown} */ raw) => this.onMessage(raw));
    // (2026-10-26, speed) read the providers while the webview loads; its `ready` waits for this read
    if (!this.cat) this._readProviders(false);
    if (webviewView.onDidChangeVisibility) {
      webviewView.onDidChangeVisibility(() => { if (webviewView.visible) this.refresh(true); });
    }
    if (webviewView.onDidDispose) webviewView.onDidDispose(() => { if (this.view === webviewView) this.view = null; });
  }

  /**
   * Build the state again and send it; `reload` = read `providers --json` again first (a key added to
   * providers.env shows up when the panel is shown again).
   * @param {boolean} [reload]
   */
  async refresh(reload = false) {
    let early = '';
    if (reload || !this.cat) {
      const loading = this._readProviders(reload);
      // (2026-10-26, speed) a panel shown again gets its cached lists at once, not after the core call;
      // the re-read ones follow only when they differ (an identical post would re-render the dropdowns)
      if (this.cat && this.view) {
        this.state = panelState(this.host.settings(), this.cat, this.host.lanes(), { failed: this.failed, once: this.once, folders: this.folders() });
        early = JSON.stringify(this.state);
        await this.view.webview.postMessage({ type: 'state', state: this.state });
      }
      await loading;
    }
    this.state = panelState(this.host.settings(), this.cat, this.host.lanes(), { failed: this.failed, once: this.once, folders: this.folders() });
    if (this.view && JSON.stringify(this.state) !== early) await this.view.webview.postMessage({ type: 'state', state: this.state });
  }

  /**
   * (2026-10-26, speed) `providers --json`, one read at a time: the read resolveWebviewView starts while
   * the webview loads is the one its `ready` waits for (no second core call). Never rejects.
   * @param {boolean} reload @returns {Promise<void>}
   */
  _readProviders(reload) {
    if (!this._loading) {
      const p = (async () => {
        try {
          this.cat = await this.host.providers(reload);
          this.failed = !this.cat;
        } catch (e) {
          this.host.log(`add panel: providers: ${e instanceof Error ? e.message : String(e)}`);
          this.cat = null;
          this.failed = true;
        }
      })();
      this._loading = p;
      p.then(() => { if (this._loading === p) this._loading = null; });
    }
    return this._loading;
  }

  /** @param {unknown} raw */
  async onMessage(raw) {
    const msg = validateMessage(raw, this.state, this.edit ? this.edit.seq : null);
    if (!msg) {
      this.host.log('add panel: a message from the webview was refused (unknown type or value)');
      return;
    }
    if (msg.type === 'ready') {
      await this.postEdit(); // no edit open: the webview drops a draft it kept from an earlier session
      await this.refresh(!this.cat);
      return;
    }
    if (msg.type === 'cancelEdit') {
      await this.endEdit();
      return;
    }
    if (msg.type === 'saveEdit') {
      const e = /** @type {NonNullable<AddPanel['edit']>} */ (this.edit);
      try {
        if (!this.host.saveEdit) throw new Error('no edit host');
        await this.host.saveEdit(e.target, msg.text);
      } catch (err) {
        // the edit stays open with the owner's text; the error is shown as any other
        e.text = msg.text;
        if (this.host.reportError) this.host.reportError('edit', err);
        if (this.view) await this.view.webview.postMessage({ type: 'editResult', seq: e.seq, ok: false,
          error: t('Not saved: {0}', err instanceof Error ? err.message : String(err)) });
        return;
      }
      if (this.edit === e) await this.endEdit();
      return;
    }
    if (msg.type === 'add') {
      await this.host.guarded('add', () => this.add());
      await this.refresh(false); // (2026-10-26) a queued task resets this task's options
      return;
    }
    if (msg.type === 'set') {
      const patch = settingsPatch(msg.key, msg.value, this.host.settings(), this.cat);
      await this.host.guarded('add panel', () => this.host.update(patch));
      await this.refresh(false);
      return;
    }
    // (2026-10-26) the chat settings (moved here from the Task view) and this task's own options
    if (msg.type === 'chatSettings') {
      const open = this.host.chatSettings;
      if (open) await this.host.guarded('chat settings', () => open());
      return;
    }
    // (2026-10-26) the UI language: a global setting (re-renders this panel too); this task's options stay
    if (msg.type === 'language') {
      const set = this.host.language;
      if (set) await this.host.guarded('language', () => set(msg.value));
      await this.refresh(false);
      return;
    }
    if (msg.type === 'once') {
      if (msg.key === 'cwd' && msg.value === BROWSE_CWD) await this.host.guarded('add panel', () => this.browseCwd());
      else this.once = setOnce(this.once, msg.key, msg.value);
    } else if (msg.type === 'attach') {
      await this.host.guarded('attach', () => this.attach(msg.what));
    } else if (msg.type === 'unattach') {
      this.once = { ...this.once, attachments: [] };
    } else if (msg.type === 'reset') {
      this.once = freshOnce();
    }
    await this.refresh(false);
  }

  /**
   * (2026-10-26) The panel's +: this task's options are checked again here (a bad value throws its
   * message and nothing is added), then the title-bar + flow runs with them; a queued task resets them.
   */
  async add() {
    const opts = onceOptions(this.once, this.host.settings(), this.folders());
    if (await withOnce(opts, () => this.host.add())) this.once = freshOnce();
  }

  /** (2026-10-26) "Other folder…": a folder dialog of the host; cancel keeps the old choice. */
  async browseCwd() {
    const picked = await this.vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
      title: `Sleeping Clanker: ${t('run this task in which folder?')}`, openLabel: t('Choose') });
    const uri = picked && picked[0];
    if (!uri || uri.scheme !== 'file' || !uri.fsPath) return;
    this.once = { ...this.once, cwd: String(uri.fsPath), extraCwd: String(uri.fsPath) };
  }

  /**
   * (2026-10-26) Attach files or folders: the host's own dialog (local paths only, at most
   * argv.ATTACH_MAX, as the core allows); a path never comes from the webview.
   * @param {string} what files | folders
   */
  async attach(what) {
    const files = what === 'files';
    const picked = await this.vscode.window.showOpenDialog({ canSelectFiles: files, canSelectFolders: !files, canSelectMany: true,
      title: `Sleeping Clanker: ${t('attach files or folders')}`, openLabel: t('Attach') });
    if (!picked || !picked.length) return;
    const list = [...this.once.attachments];
    for (const u of picked) {
      if (u && u.scheme === 'file' && u.fsPath && !list.includes(String(u.fsPath))) list.push(String(u.fsPath));
    }
    if (list.length > argv.ATTACH_MAX) {
      throw new Error(t('at most {0} attachments per task; attach their folder instead.', String(argv.ATTACH_MAX)));
    }
    this.once = { ...this.once, attachments: list };
  }
}

module.exports = {
  VIEW_ID, SUBSCRIPTION, CLAUDE_FAMILY, SHORT_LABELS, SET_KEYS, ASK_PROFILE, FALLBACK, EDIT_TEXT_MAX,
  editView, providerInfo, runnerProvider, effectiveProvider, providerLabel, modelsFor, defaultModelOf,
  panelState, validateMessage, settingsPatch, panelHtml, AddPanel,
  // (2026-10-26) this task's own options
  ONCE_KEYS, ONCE_NUMBERS, BROWSE_CWD, ATTACH_KINDS, freshOnce, onceDefaults, parseOnce, setOnce, onceState, onceSummary,
  onceOptions, onceTask, onceCaps, withOnce, currentOnce, onceQueued,
  languageSelect, // (2026-10-26) the UI language select
};

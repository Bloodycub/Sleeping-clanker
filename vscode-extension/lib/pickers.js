// @ts-check
// (2026-10-26, owner: "there is no X icon, I can't close this window") Drop-in replacements for
// vscode.window.showQuickPick / showInputBox with a close button (X) in the title bar. The pickers
// that pass `ignoreFocusOut: true` do not close on a click outside, and the plain APIs cannot show a
// title-bar button, so Esc was the only way out. These take the same arguments and resolve to the same
// values, built on createQuickPick / createInputBox; the X hides the picker and resolves undefined,
// exactly like Esc. The `vscode` module is passed in (this file never requires it), like a lib/ module:
// `pickers.forVscode(vscode).showQuickPick(items, options)`.
'use strict';

const i18n = require('./i18n');

// VS Code shows the title-bar button row only when the picker has a title.
const DEFAULT_TITLE = 'Sleeping Clanker';
// vscode.InputBoxValidationSeverity.Error: only an error blocks Enter (as in showInputBox).
const SEVERITY_ERROR = 3;

/** @param {any} x */
function isThenable(x) {
  return !!x && typeof x.then === 'function';
}

/**
 * The close button: a fresh object per picker (onDidTriggerButton compares it by identity).
 * @param {any} vscode
 */
function closeButton(vscode) {
  return { iconPath: new vscode.ThemeIcon('close'), tooltip: i18n.t('Close') };
}

/**
 * Whether a validateInput result blocks Enter: a non-empty string, or a message object whose severity
 * is Error (or not given). Info and Warning only show the message.
 * @param {any} msg
 */
function blocks(msg) {
  if (!msg) return false;
  if (typeof msg === 'string') return true;
  if (typeof msg === 'object' && msg.message) return msg.severity === undefined || msg.severity >= SEVERITY_ERROR;
  return false;
}

/** @param {any} vscode the vscode module (or a test double with window.createQuickPick/createInputBox and ThemeIcon) */
function forVscode(vscode) {
  /**
   * Like vscode.window.showQuickPick(items, options, token), with a close button in the title bar.
   * Strings are accepted as items (and returned as strings), like the original.
   * @param {any} items an array of QuickPickItems or strings, or a promise of one
   * @param {any} [options] title, placeHolder, ignoreFocusOut, matchOnDescription, matchOnDetail,
   *   canPickMany, activeItem
   * @param {any} [token] a CancellationToken
   * @returns {Promise<any>} the picked item (the same object), the picked items for canPickMany, or undefined
   */
  function showQuickPick(items, options, token) {
    const o = options || {};
    const qp = vscode.window.createQuickPick();
    const close = closeButton(vscode);
    qp.title = o.title || DEFAULT_TITLE;
    if (o.placeHolder !== undefined) qp.placeholder = o.placeHolder;
    qp.ignoreFocusOut = !!o.ignoreFocusOut;
    qp.matchOnDescription = !!o.matchOnDescription;
    qp.matchOnDetail = !!o.matchOnDetail;
    qp.canSelectMany = !!o.canPickMany;
    qp.buttons = [close];
    /** @type {Map<any, string>} wrapper item -> the string the caller passed */
    const strings = new Map();
    /** @param {any} it */
    const back = (it) => (strings.has(it) ? strings.get(it) : it);
    /** @param {any[]} list */
    const setItems = (list) => {
      const wrapped = (list || []).map((it) => {
        if (typeof it !== 'string') return it;
        const w = { label: it };
        strings.set(w, it);
        return w;
      });
      qp.items = wrapped;
      // showQuickPick pre-checks `picked` items; a created QuickPick needs selectedItems for that
      if (qp.canSelectMany) qp.selectedItems = wrapped.filter((it) => it && it.picked);
      if (o.activeItem) {
        const active = wrapped.find((it) => back(it) === o.activeItem);
        if (active) qp.activeItems = [active];
      }
    };
    return new Promise((resolve) => {
      let done = false;
      /** @type {any[]} */
      const subs = [];
      /** @param {any} value */
      const finish = (value) => {
        if (done) return;
        done = true;
        resolve(value);
        qp.hide();
      };
      subs.push(qp.onDidTriggerButton((/** @type {any} */ b) => { if (b === close) finish(undefined); }));
      subs.push(qp.onDidAccept(() => {
        if (qp.canSelectMany) { finish([...(qp.selectedItems || [])].map(back)); return; }
        const it = (qp.selectedItems && qp.selectedItems[0]) || (qp.activeItems && qp.activeItems[0]);
        if (it) finish(back(it)); // Enter with nothing to pick keeps the picker open, as showQuickPick does
      }));
      subs.push(qp.onDidHide(() => {
        if (!done) { done = true; resolve(undefined); }
        for (const s of subs) if (s && typeof s.dispose === 'function') s.dispose();
        qp.dispose();
      }));
      if (token && typeof token.onCancellationRequested === 'function') {
        subs.push(token.onCancellationRequested(() => finish(undefined)));
      }
      if (isThenable(items)) {
        qp.busy = true;
        qp.show();
        Promise.resolve(items).then((list) => {
          if (done) return;
          setItems(list);
          qp.busy = false;
        }, () => finish(undefined));
      } else {
        setItems(items);
        qp.show();
      }
    });
  }

  /**
   * Like vscode.window.showInputBox(options, token), with a close button in the title bar.
   * validateInput may return a string, an InputBoxValidationMessage, null/undefined, or a promise of
   * one; it runs on the initial value, on every change and again on Enter, which it blocks while it fails.
   * @param {any} [options] title, prompt, placeHolder, value, valueSelection, password, ignoreFocusOut, validateInput
   * @param {any} [token] a CancellationToken
   * @returns {Promise<string | undefined>} the accepted text, or undefined (Esc, the X, cancelled)
   */
  function showInputBox(options, token) {
    const o = options || {};
    const box = vscode.window.createInputBox();
    const close = closeButton(vscode);
    box.title = o.title || DEFAULT_TITLE;
    if (o.prompt !== undefined) box.prompt = o.prompt;
    if (o.placeHolder !== undefined) box.placeholder = o.placeHolder;
    if (o.value !== undefined) box.value = o.value;
    if (o.valueSelection !== undefined) box.valueSelection = o.valueSelection;
    box.password = !!o.password;
    box.ignoreFocusOut = !!o.ignoreFocusOut;
    box.buttons = [close];
    const validate = typeof o.validateInput === 'function' ? o.validateInput : null;
    let seq = 0; // a slow async check of an older value never overwrites a newer one
    /** @param {string} value @returns {Promise<any>} the validation message (null = valid) */
    const check = async (value) => {
      if (!validate) return null;
      const mine = ++seq;
      const msg = await validate(value);
      if (mine === seq) box.validationMessage = msg || undefined;
      return msg;
    };
    return new Promise((resolve) => {
      let done = false;
      /** @type {any[]} */
      const subs = [];
      /** @param {string | undefined} value */
      const finish = (value) => {
        if (done) return;
        done = true;
        resolve(value);
        box.hide();
      };
      subs.push(box.onDidTriggerButton((/** @type {any} */ b) => { if (b === close) finish(undefined); }));
      subs.push(box.onDidChangeValue((/** @type {string} */ v) => { check(v).catch(() => {}); }));
      subs.push(box.onDidAccept(async () => {
        const value = box.value;
        let msg;
        try { msg = await check(value); } catch { return; }
        if (!blocks(msg)) finish(value);
      }));
      subs.push(box.onDidHide(() => {
        if (!done) { done = true; resolve(undefined); }
        for (const s of subs) if (s && typeof s.dispose === 'function') s.dispose();
        box.dispose();
      }));
      if (token && typeof token.onCancellationRequested === 'function') {
        subs.push(token.onCancellationRequested(() => finish(undefined)));
      }
      box.show();
      check(box.value || '').catch(() => {}); // showInputBox validates the initial value too
    });
  }

  return { showQuickPick, showInputBox };
}

module.exports = { forVscode, DEFAULT_TITLE };

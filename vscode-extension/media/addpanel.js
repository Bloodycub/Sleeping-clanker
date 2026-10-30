// @ts-check
// The "+ Add new task" panel's script (2026-10-21, docs/SPEC_V049_ADD_PANEL.md). It only renders the
// host's `state` message (text only: textContent and option values, never markup) and posts the owner's
// choices back; lib/addpanel.js validates every message against the options it sent. Nothing here
// talks to the network or reads anything but the host's messages.
'use strict';

(function () {
  // eslint-disable-next-line no-undef
  const vscode = acquireVsCodeApi();
  // (2026-10-27, U4, SPEC_V05_PARALLEL 8.2) + start (claudAutopilot.newTaskStart; only with tasks at once > 1)
  const KEYS = ['provider', 'model', 'effort', 'profile', 'kind', 'lane', 'agents', 'start', 'ultracode', 'autorun'];
  // (2026-10-26) this task's own options (lib/addpanel.js onceState): a typed value goes to the host on
  // change; the host checks it and sends back the value and its error line
  const ONCE_INPUTS = ['name', 'maxRounds', 'maxHours', 'freshAtCtx', 'freshEvery'];
  /** @param {string} id */
  const $ = (id) => /** @type {HTMLElement} */ (document.getElementById(id));
  /** @param {string} id @param {unknown} text */
  const setText = (id, text) => { $(id).textContent = typeof text === 'string' ? text : ''; };

  /**
   * @param {HTMLSelectElement} sel
   * @param {{value: string, options: {value: string, label: string, disabled?: boolean, title?: string}[], disabled?: boolean, title?: string} | null} spec
   */
  function fill(sel, spec) {
    sel.textContent = '';
    if (!spec || !Array.isArray(spec.options)) return;
    for (const o of spec.options) {
      const opt = document.createElement('option');
      opt.value = String(o.value);
      opt.textContent = String(o.label);
      if (o.disabled) opt.disabled = true;
      if (o.title) opt.title = String(o.title);
      sel.appendChild(opt);
    }
    sel.value = String(spec.value);
    sel.disabled = !!spec.disabled;
    sel.title = spec.title ? String(spec.title) : '';
  }

  /** @param {any} s the host's PanelState */
  function render(s) {
    const L = s.labels || {};
    // (2026-10-27, i18n audit L5) <html lang> follows the UI language (the page is built once)
    if (typeof s.lang === 'string' && /^[a-z]{2}$/.test(s.lang) && document.documentElement) document.documentElement.lang = s.lang;
    setText('add', L.add);
    $('add').title = typeof L.addTitle === 'string' ? L.addTitle : '';
    setText('modelLbl', L.model);
    setText('otherLbl', L.other);
    for (const [k, label] of [['provider', L.provider], ['model', L.modelSel], ['effort', L.effort]]) {
      $(k).setAttribute('aria-label', typeof label === 'string' ? label : k);
    }
    for (const k of KEYS) fill(/** @type {HTMLSelectElement} */ ($(k)), s[k] || null);
    // the effort's own tooltip says why it is off; the provider/model ones show the option's
    for (const k of ['profile', 'kind', 'lane', 'agents', 'ultracode', 'autorun', 'start']) setText(`${k}Lbl`, L[k]);
    $('laneRow').hidden = !s.lane;
    $('startRow').hidden = !s.start; // (2026-10-27, U4)
    setText('warn', s.warning);
    $('warn').hidden = !s.warning;
    setText('note', s.note);
    $('note').hidden = !s.note;
    renderOnce(s.once || null, L);
    // (2026-10-26) the UI language (a global setting; the host re-sends the state in the new language)
    fill(/** @type {HTMLSelectElement} */ ($('language')), s.language || null);
    setText('languageLbl', L.language);
    $('languageRow').hidden = !s.language;
  }

  /** (2026-10-26) ⚙ Chat settings and the "This task only" rows. @param {any} o @param {any} L */
  function renderOnce(o, L) {
    for (const k of ['chatSettings', 'attachFiles', 'attachFolders', 'unattach', 'reset']) setText(k, L[k]);
    $('chatSettings').title = typeof L.chatSettingsTitle === 'string' ? L.chatSettingsTitle : '';
    $('reset').title = typeof L.resetTitle === 'string' ? L.resetTitle : '';
    for (const k of ['once', 'cwd', 'where', 'attach', 'goal', ...ONCE_INPUTS]) setText(`${k}Lbl`, L[k]);
    if (!o || typeof o !== 'object') return;
    for (const k of ONCE_INPUTS) {
      const f = o.fields && o.fields[k];
      const inp = /** @type {HTMLInputElement} */ ($(k));
      if (!f) continue;
      if (document.activeElement !== inp) inp.value = String(f.value);
      inp.placeholder = String(f.placeholder || '');
      inp.title = String(f.title || '');
      inp.classList.toggle('bad', !!f.error);
      setText(`${k}Err`, f.error);
      $(`${k}Err`).hidden = !f.error;
    }
    fill(/** @type {HTMLSelectElement} */ ($('cwd')), o.cwd || null);
    // (2026-10-27, U4, 8.2) where this task runs: its own copy, or alone in the project folder
    fill(/** @type {HTMLSelectElement} */ ($('where')), o.where || null);
    $('whereRow').hidden = !o.where;
    const list = Array.isArray(o.attachments) ? o.attachments : [];
    const ul = $('attached');
    ul.textContent = '';
    for (const a of list) {
      const li = document.createElement('li');
      li.textContent = String(a.label);
      li.title = String(a.title);
      ul.appendChild(li);
    }
    ul.hidden = !list.length;
    $('unattach').hidden = !list.length;
    $('goalRows').hidden = !o.goal;
  }

  // (2026-10-26, edit in panel) the Queue pencil's edit box: the host sends {type:'edit', edit} (null =
  // closed); Save posts the box's text with the edit's seq, the host checks both
  const editText = /** @type {HTMLTextAreaElement} */ ($('editText'));
  /** @type {number | null} */
  let editSeq = null;
  let editMax = 0;
  let tooLong = '';
  let editId = '';
  /** @param {any} e */
  function renderEdit(e) {
    $('editErr').hidden = true;
    if (!e || typeof e !== 'object' || typeof e.seq !== 'number') {
      editSeq = null;
      $('edit').hidden = true;
      editText.value = '';
      saveState({ draft: null });
      return;
    }
    const st = vscode.getState() || {};
    const same = editSeq === e.seq;
    editSeq = e.seq;
    editMax = typeof e.max === 'number' ? e.max : 0;
    tooLong = typeof e.tooLong === 'string' ? e.tooLong : '';
    /** @type {HTMLButtonElement} */ ($('editSave')).disabled = false;
    setText('editTitle', e.title);
    setText('editNote', e.note);
    $('editNote').hidden = !e.note;
    setText('editSave', e.save);
    $('editSave').title = typeof e.hint === 'string' ? e.hint : '';
    setText('editCancel', e.cancel);
    // a reload of the view keeps what the owner had typed into this same edit
    if (!same) {
      editText.value = st.draft && st.draft.seq === e.seq && st.draft.id === e.id ? String(st.draft.text) : (typeof e.text === 'string' ? e.text : '');
    }
    editId = typeof e.id === 'string' ? e.id : '';
    $('edit').hidden = false;
    editText.focus();
    editText.setSelectionRange(editText.value.length, editText.value.length);
  }
  /** @param {Record<string, unknown>} patch */
  function saveState(patch) { vscode.setState({ ...(vscode.getState() || {}), ...patch }); }
  function save() {
    const btn = /** @type {HTMLButtonElement} */ ($('editSave'));
    if (editSeq === null || !editText.value.trim() || btn.disabled) return;
    if (editMax && editText.value.length > editMax) {
      setText('editErr', tooLong);
      $('editErr').hidden = false;
      return;
    }
    btn.disabled = true; // one save at a time; editResult or the closed box ends it
    vscode.postMessage({ type: 'saveEdit', seq: editSeq, text: editText.value });
  }
  editText.addEventListener('input', () => { if (editSeq !== null) saveState({ draft: { seq: editSeq, id: editId, text: editText.value } }); });
  editText.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); save(); }
    else if (ev.key === 'Escape' && editSeq !== null) { ev.preventDefault(); vscode.postMessage({ type: 'cancelEdit', seq: editSeq }); }
  });
  $('editSave').addEventListener('click', save);
  $('editCancel').addEventListener('click', () => { if (editSeq !== null) vscode.postMessage({ type: 'cancelEdit', seq: editSeq }); });

  window.addEventListener('message', (ev) => {
    const m = ev.data;
    if (m && m.type === 'state' && m.state && typeof m.state === 'object') render(m.state);
    else if (m && m.type === 'edit') renderEdit(m.edit);
    else if (m && m.type === 'editResult' && m.seq === editSeq && !m.ok) {
      /** @type {HTMLButtonElement} */ ($('editSave')).disabled = false;
      setText('editErr', m.error);
      $('editErr').hidden = false;
    }
  });
  for (const k of KEYS) {
    const sel = /** @type {HTMLSelectElement} */ ($(k));
    sel.addEventListener('change', () => vscode.postMessage({ type: 'set', key: k, value: sel.value }));
  }
  $('add').addEventListener('click', () => vscode.postMessage({ type: 'add' }));
  // (2026-10-26) ⚙ Chat settings (moved here from the Task view) and this task's own options
  $('chatSettings').addEventListener('click', () => vscode.postMessage({ type: 'chatSettings' }));
  for (const k of ONCE_INPUTS) {
    const inp = /** @type {HTMLInputElement} */ ($(k));
    inp.addEventListener('change', () => vscode.postMessage({ type: 'once', key: k, value: inp.value }));
  }
  const cwdSel = /** @type {HTMLSelectElement} */ ($('cwd'));
  cwdSel.addEventListener('change', () => vscode.postMessage({ type: 'once', key: 'cwd', value: cwdSel.value }));
  const whereSel = /** @type {HTMLSelectElement} */ ($('where')); // (2026-10-27, U4)
  whereSel.addEventListener('change', () => vscode.postMessage({ type: 'once', key: 'where', value: whereSel.value }));
  $('attachFiles').addEventListener('click', () => vscode.postMessage({ type: 'attach', what: 'files' }));
  $('attachFolders').addEventListener('click', () => vscode.postMessage({ type: 'attach', what: 'folders' }));
  $('unattach').addEventListener('click', () => vscode.postMessage({ type: 'unattach' }));
  $('reset').addEventListener('click', () => vscode.postMessage({ type: 'reset' }));
  const langSel = /** @type {HTMLSelectElement} */ ($('language')); // (2026-10-26)
  langSel.addEventListener('change', () => vscode.postMessage({ type: 'language', value: langSel.value }));
  // "Other settings" stays open or closed across a reload of the view
  const other = /** @type {HTMLDetailsElement} */ ($('other'));
  const saved = vscode.getState();
  other.open = !!(saved && saved.otherOpen);
  other.addEventListener('toggle', () => saveState({ otherOpen: other.open }));
  vscode.postMessage({ type: 'ready' });
}());

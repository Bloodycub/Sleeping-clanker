// @ts-check
// (2026-10-26, owner task 07254297 "Add posibility not done yet drag in priority example from bottom to
// top") Reordering a queue from the Queue view: drag a not-done task onto another task (it goes BEFORE
// that task; dropped on the empty space below the rows, to the bottom), or Move to Top / Move to Bottom
// in its context menu. The core's `move ID --top|--bottom|--before ID --json` does the work (only seqs
// change, so the runner follows the new order; a subtask moves only among its main task's subtasks).
// Pure: the vscode API is passed in, so the controller is tested with the plain mocks.
'use strict';

const { ID_RE, DEFAULT_LANE } = require('./argv');
const { parseJsonOutput } = require('./parse');
const { t } = require('./i18n');

/** Statuses the core's `move` refuses (cli.MOVE_REFUSED). */
const NOT_MOVABLE = Object.freeze(['running', 'done', 'cancelled']);

/** The MIME type of a tree's own items (VS Code: application/vnd.code.tree.<view id in lower case>). @param {string} viewId */
function treeMime(viewId) {
  return `application/vnd.code.tree.${viewId.toLowerCase()}`;
}

/** A task the core can move: not running, done or cancelled. @param {any} item */
function canMove(item) {
  return !!item && typeof item.id === 'string' && typeof item.status === 'string' && !NOT_MOVABLE.includes(item.status);
}

/** @param {unknown} id */
function checkId(id) {
  if (typeof id !== 'string' || !ID_RE.test(id)) throw new Error(`invalid item id ${JSON.stringify(id)}`);
  return id;
}

/**
 * `move ID (--top | --bottom | --before REF) --json` as an argv list (never a shell string).
 * @param {string} id @param {'top' | 'bottom' | {before: string}} where
 */
function moveArgs(id, where) {
  checkId(id);
  if (where === 'top') return ['move', id, '--top', '--json'];
  if (where === 'bottom') return ['move', id, '--bottom', '--json'];
  if (where && typeof where === 'object' && 'before' in where) return ['move', id, '--before', checkId(where.before), '--json'];
  throw new Error(`invalid place ${JSON.stringify(where)}`);
}

/**
 * `move --json` -> {id, moved, ahead}: `ahead` = the pending tasks that still run before it.
 * @param {string} stdout
 * @returns {{ok: true, value: {id: string, moved: boolean, ahead: number}} | {ok: false, error: string}}
 */
function parseMove(stdout) {
  const r = parseJsonOutput(stdout);
  if (!r.ok) return r;
  const v = /** @type {any} */ (r.value);
  if (!v || typeof v !== 'object' || typeof v.id !== 'string' || !v.id) return { ok: false, error: 'move --json: expected {"id", "moved", "ahead"}' };
  return { ok: true, value: { id: v.id, moved: v.moved === true, ahead: Number.isInteger(v.ahead) ? v.ahead : 0 } };
}

/** Two nodes of one queue: the same data dir (or both unscoped), project and lane. @param {any} a @param {any} b */
function sameQueue(a, b) {
  return (a.dataDir || '') === (b.dataDir || '') && (a.project || '') === (b.project || '')
    && (a.lane || DEFAULT_LANE) === (b.lane || DEFAULT_LANE);
}

/**
 * What a drop does: onto a task of the same queue, the dragged task goes before it; onto the empty
 * space below the rows (no target), to the bottom. `message` = why nothing happens, for the owner
 * (null = nothing to say, e.g. dropped onto itself or onto a group row).
 * @param {any} source the dragged node @param {any} target the node it was dropped on
 * @returns {{ok: true, where: 'bottom' | {before: string}} | {ok: false, message: string | null}}
 */
function dropPlan(source, target) {
  if (!source || source.kind !== 'item' || !canMove(source.item)) return { ok: false, message: null };
  if (target === undefined || target === null) return { ok: true, where: 'bottom' };
  if (target.kind !== 'item' || !target.item || typeof target.item.id !== 'string') return { ok: false, message: null };
  if (target.item.id === source.item.id) return { ok: false, message: null };
  if (!sameQueue(source, target)) return { ok: false, message: t('A task can only be moved within its own queue.') };
  if (target.item.status === 'done' || target.item.status === 'cancelled') {
    return { ok: false, message: t('Drop the task onto a task that is not done yet: it goes before that task.') };
  }
  return { ok: true, where: { before: target.item.id } };
}

/** The Queue view's TreeDragAndDropController: one not-done task at a time, within its queue. */
class QueueDragAndDrop {
  /**
   * @param {any} vscode the API (DataTransferItem)
   * @param {string} viewId the tree view's id
   * @param {{move: (node: any, where: 'bottom' | {before: string}) => Promise<void>, info: (message: string) => void}} host
   */
  constructor(vscode, viewId, host) {
    this.vscode = vscode;
    this.host = host;
    this.mime = treeMime(viewId);
    this.dragMimeTypes = [this.mime];
    this.dropMimeTypes = [this.mime];
  }

  /** @param {readonly any[]} source @param {any} dataTransfer */
  handleDrag(source, dataTransfer) {
    const nodes = source || [];
    if (nodes.length !== 1 || !nodes[0] || nodes[0].kind !== 'item' || !canMove(nodes[0].item)) return;
    dataTransfer.set(this.mime, new this.vscode.DataTransferItem(nodes[0]));
  }

  /** @param {any} target @param {any} dataTransfer */
  async handleDrop(target, dataTransfer) {
    const entry = dataTransfer && dataTransfer.get(this.mime);
    if (!entry) return;
    // our own item, or (when handleDrag set nothing) VS Code's list of the dragged nodes
    const value = entry.value;
    const source = Array.isArray(value) ? (value.length === 1 ? value[0] : null) : value;
    const plan = dropPlan(source, target);
    if (!plan.ok) {
      if (plan.message) this.host.info(plan.message);
      return;
    }
    await this.host.move(source, plan.where);
  }
}

module.exports = { NOT_MOVABLE, treeMime, canMove, moveArgs, parseMove, dropPlan, QueueDragAndDrop };

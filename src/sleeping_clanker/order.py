"""Start order of the parallel runner (2026-10-27, "Tasks at once", SPEC_V05_PARALLEL section 5).

Pure functions over queue items, no I/O: which pending item may start next (pick), why one may not
(start_block, also the CLI's `waits` field, so the UI and the scheduler agree), and where it runs
(item_mode: in a private copy, alone in the project folder, or shared read-only beside copies).
`after` (a full id in the same lane) is honoured by the parallel scheduler only; the serial run_loop
is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import iso
from .store import held_by_parent, run_order_key

MODES = ("copy", "alone", "shared")
BLOCK_CODES = ("not-pending", "waits-after", "held-by-parent", "family", "goal", "alone-barrier",
               "prepare-backoff", "limit-gate", "hold")
GATES = ("limit-gate", "hold")
# merge states that let an `after` dependent start once its target is done (spec 5.2)
AFTER_OK_MERGE = (None, "merged", "nothing")
PLAN_PROFILE = "plan"


@dataclass
class PickContext:
    """What the coordinator knows at one pick (spec 1.3, 5.5).
    running: ids in a worker slot (prepare or attempt); merging: ids whose merge is pending, waiting or
    merging; alone_running: an "alone" item holds a slot; merge_due: a merge job is queued or running;
    n_eff: the effective tasks at once; now: aware datetime; copy_ok: copies can be made
    (Shadow.availability() is None); alone_ids: items a worker turned "alone" this run (a gate code or
    3 failed prepares); backoff: {id: datetime} no prepare before then; gate: "limit-gate" | "hold" |
    None (no new starts)."""
    running: set = field(default_factory=set)
    merging: set = field(default_factory=set)
    alone_running: bool = False
    merge_due: bool = False
    n_eff: int = 1
    now: datetime | None = None
    copy_ok: bool = True
    alone_ids: set = field(default_factory=set)
    backoff: dict = field(default_factory=dict)
    gate: str | None = None


def _block(code: str, **args) -> dict:
    return {"code": code, "args": args}


def _runnable(items: list[dict]) -> list[dict]:
    """loop.runnable_pending without importing loop: pending, not held by its main task, run order."""
    by_id = {i["id"]: i for i in items}
    return sorted((i for i in items if i.get("status") == "pending" and not held_by_parent(i, by_id)),
                  key=lambda i: run_order_key(i, by_id))


def _live_record(item: dict) -> dict | None:
    rec = item.get("worktree")
    return rec if isinstance(rec, dict) and rec.get("state") != "removed" else None


def _is_running(item: dict, ctx: PickContext) -> bool:
    return item.get("status") == "running" or item["id"] in ctx.running


def after_satisfied(item: dict, by_id: dict[str, dict]) -> bool:
    """(spec 5.2) True when the item has no `after`, its target is gone (removed: no wait), or the
    target is done and its merge ended (state null, merged or nothing)."""
    after = item.get("after")
    if not after:
        return True
    target = by_id.get(after)
    if target is None:
        return True
    return target.get("status") == "done" and (target.get("merge") or {}).get("state") in AFTER_OK_MERGE


def item_mode(item: dict, ctx: PickContext) -> str:
    """(spec 5.7) "copy" | "alone" | "shared", first match wins: a live worktree record -> copy;
    in_place -> alone; turned alone this run (ctx.alone_ids) -> alone; a removed record with a session
    -> copy (spec 4.6: recreated at the same path, so --resume finds the session; when copies can be
    made); profile plan -> shared; a session -> alone; no copies possible -> alone; n_eff 1 -> alone;
    else copy."""
    if _live_record(item) is not None:
        return "copy"
    if item.get("in_place"):
        return "alone"
    if item["id"] in ctx.alone_ids:
        return "alone"
    rec = item.get("worktree")
    if isinstance(rec, dict) and rec.get("path") and item.get("session_id") and ctx.copy_ok:
        return "copy"
    if item.get("profile") == PLAN_PROFILE:
        return "shared"
    if item.get("session_id"):
        return "alone"
    if not ctx.copy_ok:
        return "alone"
    if ctx.n_eff <= 1:
        return "alone"
    return "copy"


def _own_block(item: dict, items: list[dict], by_id: dict[str, dict], ctx: PickContext) -> dict | None:
    """Every start_block code except the alone barrier."""
    if item.get("status") != "pending":
        return _block("not-pending", status=item.get("status"))
    if held_by_parent(item, by_id):
        return _block("held-by-parent", id8=(item.get("parent") or "")[:8])
    if not after_satisfied(item, by_id):
        target = by_id[item["after"]]
        return _block("waits-after", id8=item["after"][:8], status=target.get("status"),
                      merge=(target.get("merge") or {}).get("state"))
    parent = item.get("parent")
    for other in items:
        if other is item or other["id"] == item["id"] or not _is_running(other, ctx):
            continue
        if other["id"] == parent or other.get("parent") == item["id"] or (parent and other.get("parent") == parent):
            return _block("family", id8=other["id"][:8])
    if item.get("kind") == "goal":
        for other in items:
            if other["id"] != item["id"] and other.get("kind") == "goal" and _is_running(other, ctx):
                return _block("goal", id8=other["id"][:8])
    until = ctx.backoff.get(item["id"])
    if until is not None and (ctx.now is None or until > ctx.now):
        return _block("prepare-backoff", until=iso(until) if isinstance(until, datetime) else until)
    if ctx.gate in GATES:
        return _block(ctx.gate)
    return None


def _busy(ctx: PickContext) -> bool:
    return bool(ctx.running) or bool(ctx.merging) or ctx.merge_due or ctx.alone_running


def start_block(item: dict, items: list[dict], ctx: PickContext) -> dict | None:
    """(spec 5.5) None when the item may start now, else {"code", "args"}; codes in order: not-pending,
    held-by-parent, waits-after ({id8, status, merge}), family ({id8}), goal ({id8}), alone-barrier
    ({id8} of the alone item: an alone item runs, an earlier alone item waits to run alone, or the item
    itself is alone and waits for the others), prepare-backoff ({until}), then limit-gate / hold."""
    by_id = {i["id"]: i for i in items}
    own = _own_block(item, items, by_id, ctx)
    if own is not None and own["code"] not in GATES and own["code"] != "prepare-backoff":
        return own
    barrier = _barrier(item, items, by_id, ctx)
    return barrier or own


def _barrier(item: dict, items: list[dict], by_id: dict[str, dict], ctx: PickContext) -> dict | None:
    if ctx.alone_running:
        alone = next((i for i in items if i["id"] in ctx.running and item_mode(i, ctx) == "alone"), None)
        return _block("alone-barrier", id8=alone["id"][:8] if alone else None)
    for other in _runnable(items):
        if other["id"] == item["id"]:
            break
        if other["id"] in ctx.running or item_mode(other, ctx) != "alone":
            continue
        if _own_block(other, items, by_id, ctx) is None:
            return _block("alone-barrier", id8=other["id"][:8])
    if item_mode(item, ctx) == "alone" and _busy(ctx):
        return _block("alone-barrier", id8=item["id"][:8])
    return None


def pick(items: list[dict], ctx: PickContext) -> dict | None:
    """(spec 5.6) The next item to start, or None. Walks the runnable pending items in run order; the
    first unblocked "alone" item is returned only when nothing runs and no merge is due, else the walk
    STOPS there (a barrier: nothing after it starts, so it cannot starve). Items with their own block
    are skipped (an alone item too: one waiting for its `after` must not stop everything). While an
    alone item runs, or a gate is on, nothing starts."""
    if ctx.gate in GATES or ctx.alone_running:
        return None
    by_id = {i["id"]: i for i in items}
    for item in _runnable(items):
        if item["id"] in ctx.running:
            continue
        if _own_block(item, items, by_id, ctx) is not None:
            continue
        if item_mode(item, ctx) == "alone":
            return None if _busy(ctx) else item
        return item
    return None


def resolve_previous(items: list[dict]) -> str | None:
    """(spec 5.1, --after-previous) The id of the last item in RUN order (store.run_order_key) that is
    not done or cancelled; None when there is none."""
    by_id = {i["id"]: i for i in items}
    live = [i for i in items if i.get("status") not in ("done", "cancelled")]
    if not live:
        return None
    return max(live, key=lambda i: run_order_key(i, by_id))["id"]


def after_cycle(items: list[dict], item_id: str, after: str | None) -> bool:
    """(spec 5.2) True when `item_id` waiting for `after` would close a cycle (after is the item itself,
    or after's own chain of `after`s reaches it). The caller refuses it (exit 1)."""
    if not after:
        return False
    by_id = {i["id"]: i for i in items}
    seen: set[str] = set()
    cur: str | None = after
    while cur and cur not in seen:
        if cur == item_id:
            return True
        seen.add(cur)
        cur = (by_id.get(cur) or {}).get("after")
    return False


def inherit_after(items: list[dict], removed_id: str) -> list[str]:
    """(spec 5.2) Inside the remove transaction: every item waiting for `removed_id` waits for the removed
    item's own `after` instead (None when it had none, is gone, or would be the item itself). Changes
    the items in place; returns the changed ids."""
    removed = next((i for i in items if i["id"] == removed_id), None)
    inherited = (removed or {}).get("after")
    if inherited and not any(i["id"] == inherited for i in items):
        inherited = None
    changed = []
    for it in items:
        if it["id"] == removed_id or it.get("after") != removed_id:
            continue
        new = inherited if inherited != it["id"] else None
        it["after"] = new
        if new is None:
            it["after_source"] = None
        changed.append(it["id"])
    return changed

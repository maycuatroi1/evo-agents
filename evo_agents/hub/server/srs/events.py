"""The event ``srs.version.approved`` (plan loop-engine, criterion a3) and the plugin's own handler of it.

``version_approved`` emits it when a version of a document is approved, in the transaction of the approval
(``reviews.review``): the document (slug and id), the version (number and id), the version approved before it (None
the first time), the approver (login and id), and the codes of the items the version added, changed (their text,
fields or place) and dropped since that one, by kind; every coded item is added the first time. Plugins granted
``srs:read`` get it, and the plugin srs itself ungranted (``plugin_events.OWN``), at least once; the hub keeps it in
hub_events, where the Loop reads it by cursor (``evo_agents.hub.plugin_events``).

``on_version_approved`` is the plugin's events contribution ``approvals``: it calls ``auto_tasks.approved`` with the
event's payload when the hub delivers it, in a transaction of its own, so the tasks of an approved version (frame
tasks, comments, cancellations and @ba's split run) come from the event in every project that enables the plugin
tasks, whether its Loop is on or off. The hub delivers it again after a failure; ``approved`` is idempotent by
version, so a second delivery makes nothing. A delivery comes a minute or more after the approval: a project that
enabled the plugin tasks only after the version was approved gets nothing from it, as the approval made nothing then.

``use_cases_approved`` emits ``srs.use_case.approved`` (plan srs-backlog-loop, criterion a1) of each use case a review
just made approved: the document, the version, the use case's code and key, the code (or else the title) of its direct
parent item, its ``claim`` field when it has one, and the approver. A use case of a version is emitted once, whatever
reviews follow; the event of the version stays as it is, and a version approved emits the use cases it needs reviewed
that no event told of yet, before its own event.

A payload carries ids, codes and states, never the text of a document: a plugin that needs more reads the document
through the API with its grant. ``v`` is the version of its shape (``hub_events.VERSION``).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import feature_rule, hub_events, plugin_events, tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.srs import auto_tasks, store
from evo_agents.hub.server.srs.diff import diff
from evo_agents.hub.server.srs.doc import Document, Item
from evo_agents.hub.server.srs.store import DocumentRow, VersionRow
from evo_agents.hub.server.srs.use_case_reviews import APPROVED as APPROVED_STATE
from evo_agents.hub.server.srs.use_case_reviews import REMOVED, CaseState

APPROVED = hub_events.SRS_APPROVED
USE_CASE_APPROVED = hub_events.SRS_USE_CASE_APPROVED
ADDED, CHANGED, DROPPED = "added", "changed", "dropped"
_CHANGES = {"added": ADDED, "changed": CHANGED, "moved": CHANGED, "removed": DROPPED}  # of ``diff.ItemChange``


def item_changes(old: Document | None, new: Document) -> dict[str, dict[str, list[str]]]:
    """The codes of the items ``new`` added, changed and dropped since ``old`` (None: every coded item is added), each
    by kind, in the order of ``diff``; an item with no code is left out."""
    if old is None:
        changes = [(item.kind, item.code, ADDED) for item in new.items]
    else:
        changes = [(change.kind, change.code, _CHANGES[change.change]) for change in diff(old, new)]
    found: dict[str, dict[str, list[str]]] = {ADDED: {}, CHANGED: {}, DROPPED: {}}
    for kind, code, what in changes:
        codes = found[what].setdefault(kind, []) if code else None
        if codes is not None and code not in codes:
            codes.append(code)
    return found


async def version_approved(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    version: VersionRow,
    previous: int | None,
    approver: Principal,
) -> int:
    """Emit ``srs.version.approved`` of ``version`` of ``doc``, which ``approver`` just approved after version
    ``previous`` (None: none was approved before), in the approval's transaction; how many plugins take it."""
    now = await store.snapshot(conn, version)
    before = await store.snapshot(conn, await store.version(conn, doc, previous)) if previous else None
    payload = {
        "v": hub_events.VERSION,
        "document": doc.slug,
        "document_id": doc.id,
        "version": version.number,
        "version_id": version.id,
        "previous": previous,
        "approver": approver.login,
        "approver_id": approver.user_id,
        "items": item_changes(before.doc if before else None, now.doc),
    }
    return await plugin_events.emit(conn, APPROVED, project_id, payload)


async def _emitted(conn: AsyncConnection, document_id: int, version: int) -> set[str]:
    """The keys of the use cases of version ``version`` of document ``document_id`` an event told of already."""
    e = tables.hub_events
    query = select(e.c.payload["key"].astext).where(
        e.c.kind == USE_CASE_APPROVED,
        e.c.payload["document_id"].astext == str(document_id),
        e.c.payload["version"].astext == str(version),
    )
    return set((await conn.execute(query)).scalars())


def _parent(doc: Document, item: Item) -> str | None:
    """The code, else the title, of the direct parent of ``item`` in ``doc``."""
    if item.parent is None:
        return None
    parent = doc.items[item.parent]
    return parent.code or parent.title


async def use_cases_approved(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    version: VersionRow,
    approver: Principal,
    cases: Sequence[CaseState],
) -> int:
    """Emit ``srs.use_case.approved`` for each of ``cases``, the use cases of ``version`` to review as a review of
    ``approver`` leaves them, that is approved and no event told of yet, in the review's transaction; how many were
    emitted. A use case ``version`` dropped reads from the version approved before it, as it has no place in it."""
    done = await _emitted(conn, doc.id, version.number)
    found = [case for case in cases if case.state == APPROVED_STATE and case.key not in done]
    if not found:
        return 0
    now = (await store.snapshot(conn, version)).doc
    before = (
        (await store.snapshot(conn, await store.version(conn, doc, doc.approved_version))).doc
        if doc.approved_version and any(case.change == REMOVED for case in found)
        else None
    )
    for case in found:
        home = before if case.change == REMOVED else now  # the document the use case is in
        item = next((item for item in home.items if item.key == case.key), None) if home else None
        claim = item.fields.get("claim") if item else None
        payload = {
            "v": hub_events.VERSION,
            "document": doc.slug,
            "document_id": doc.id,
            "version": version.number,
            "version_id": version.id,
            "code": case.code,
            "key": case.key,
            "change": case.change,
            "parent": _parent(home, item) if item else None,
            "claim": claim,
            "approver": approver.login,
            "approver_id": approver.user_id,
        }
        await plugin_events.emit(conn, USE_CASE_APPROVED, project_id, payload)
    return len(found)


def _ids(payload: dict) -> tuple[int, int, int | None, int] | None:
    """(document, version, version approved before, approver) of a payload, or None when one is not an id."""
    found = payload.get("document_id"), payload.get("version"), payload.get("previous"), payload.get("approver_id")
    document_id, version, previous, approver_id = found
    if not all(isinstance(value, int) for value in (document_id, version, approver_id)):
        return None
    return found if previous is None or isinstance(previous, int) else None


async def _tasks_since(conn: AsyncConnection, project_id: int, document_id: int, version: int) -> bool:
    """Whether project ``project_id`` turned the feature tasks on after version ``version`` of document
    ``document_id`` was approved."""
    v = tables.srs.versions
    decided = select(v.c.decided_at).where(v.c.document_id == document_id, v.c.number == version)
    decided_at = await conn.scalar(decided)
    since = await feature_rule.on_since(conn, auto_tasks.TASKS, project_id)
    return since is not None and decided_at is not None and since > decided_at


async def on_version_approved(params: dict, context: CoreContext) -> dict:
    """The contribution ``approvals``: ``params`` is plugin/1's ``event`` (the payload of ``version_approved`` and
    its delivery). A document of another project than the delivery's is left alone."""
    ids = _ids(params.get("payload") or {})
    if params.get("kind") != APPROVED or ids is None or context.engine is None or context.project_id is None:
        return {}
    document_id, version, previous, approver_id = ids
    d = tables.srs.documents
    async with context.engine.begin() as conn:
        if await conn.scalar(select(d.c.project_id).where(d.c.id == document_id)) != context.project_id:
            return {}
        if not await _tasks_since(conn, context.project_id, document_id, version):
            await auto_tasks.approved(conn, document_id, version, previous, approver_id)
    return {}


__all__ = [
    "APPROVED",
    "USE_CASE_APPROVED",
    "item_changes",
    "on_version_approved",
    "use_cases_approved",
    "version_approved",
]

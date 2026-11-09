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

``use_cases_released`` releases each use case a review just made approved into the document's approved baseline
(``revisions.release``: its revision there, never older than the one the baseline holds) and emits
``srs.use_case.approved`` (plan srs-backlog-loop, criterion a1) for each release: the document, the version, the use
case's code and key, its revision and the one released before it with the version that one was approved on, what the
release did (added, changed, removed), the code (or else the title) of its direct parent item, its ``claim`` field when
it has one, and the approver. A revision is released and emitted once, whatever reviews follow; the event of the
version stays as it is, and a version approved emits the use cases it needs reviewed that no release told of yet,
before its own event.

``on_use_case_approved`` is the plugin's events contribution ``releases``: it calls ``auto_tasks.released`` with the
release the event names, in a transaction of its own, so the frame task of a new use case, the comments on the tasks of
one that changed and the cancellations of one removed come from each use case's approval, not from its version's.

A payload carries ids, codes and states, never the text of a document: a plugin that needs more reads the document
through the API with its grant. ``v`` is the version of its shape (``hub_events.VERSION``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import feature_rule, hub_events, plugin_events, tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.srs import auto_tasks, revisions, store
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


def _parent(doc: Document, item: Item) -> str | None:
    """The code, else the title, of the direct parent of ``item`` in ``doc``."""
    if item.parent is None:
        return None
    parent = doc.items[item.parent]
    return parent.code or parent.title


async def _home(conn: AsyncConnection, doc: DocumentRow, version: VersionRow, key: str) -> tuple[Document, Item] | None:
    """The document a use case ``version`` dropped was in, and the use case there: the newest version before it that
    holds it."""
    v = tables.srs.versions
    numbers = select(v.c.number).where(v.c.document_id == doc.id, v.c.number < version.number)
    for number in sorted((await conn.execute(numbers)).scalars(), reverse=True):
        content = (await store.snapshot(conn, await store.version(conn, doc, number))).doc
        item = next((item for item in content.items if item.key == key), None)
        if item is not None:
            return content, item
    return None


@dataclass(frozen=True)
class Released:
    """A use case a review released: the case as the review left it, the release's id and revision."""

    case: CaseState
    release_id: int
    rev: int


async def use_cases_released(
    conn: AsyncConnection,
    project_id: int,
    doc: DocumentRow,
    version: VersionRow,
    approver: Principal,
    cases: Sequence[CaseState],
) -> list[Released]:
    """Release each of ``cases``, the use cases of ``version`` to review as a review of ``approver`` leaves them, that is
    approved at a revision newer than the one the baseline holds, and emit ``srs.use_case.approved`` of each, in the
    review's transaction; what was released. A use case ``version`` dropped reads from the newest version that held
    it."""
    found = [case for case in cases if case.state == APPROVED_STATE and case.rev is not None]
    if not found:
        return []
    known = await revisions.history(conn, doc.id)
    now = (await store.snapshot(conn, version)).doc
    made = []
    for case in found:
        revision = known.at(case.key, version.number)
        before = known.release(case.key)
        if revision is None or (before is not None and before.rev >= revision.rev):
            continue
        change = known.change(case.key, revision)
        release_id = await revisions.release(conn, doc.id, revision, version.id, change, approver.user_id)
        if release_id is not None:
            made.append((case, revision, before, change, release_id))
    batch = [release_id for *_, release_id in made]  # the releases of this review: one split run for their frames
    done = []
    for case, revision, before, change, release_id in made:
        held = (now, next((item for item in now.items if item.key == case.key), None))
        if case.change == REMOVED or held[1] is None:
            held = await _home(conn, doc, version, case.key) or (now, None)
        home, item = held
        payload = {
            "v": hub_events.VERSION,
            "document": doc.slug,
            "document_id": doc.id,
            "version": version.number,
            "version_id": version.id,
            "code": case.code,
            "key": case.key,
            "change": case.change,
            "release": change,
            "release_id": release_id,
            "batch": batch,
            "rev": revision.rev,
            "previous_rev": before.rev if before is not None else None,
            "previous_version": before.version if before is not None else None,
            "parent": _parent(home, item) if item else None,
            "claim": item.fields.get("claim") if item else None,
            "approver": approver.login,
            "approver_id": approver.user_id,
        }
        await plugin_events.emit(conn, USE_CASE_APPROVED, project_id, payload)
        done.append(Released(case, release_id, revision.rev))
    return done


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


async def on_use_case_approved(params: dict, context: CoreContext) -> dict:
    """The contribution ``releases``: ``params`` is plugin/1's ``event`` (the payload of ``use_cases_released``). A
    document of another project than the delivery's is left alone, and an event of an older shape (no release) too."""
    payload = params.get("payload") or {}
    release_id, document_id = payload.get("release_id"), payload.get("document_id")
    if params.get("kind") != USE_CASE_APPROVED or context.engine is None or context.project_id is None:
        return {}
    if not isinstance(release_id, int) or not isinstance(document_id, int):
        return {}
    d = tables.srs.documents
    async with context.engine.begin() as conn:
        if await conn.scalar(select(d.c.project_id).where(d.c.id == document_id)) != context.project_id:
            return {}
        batch = [one for one in payload.get("batch") or [] if isinstance(one, int)]
        await auto_tasks.released(conn, release_id, batch or [release_id])
    return {}


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
    "Released",
    "item_changes",
    "on_use_case_approved",
    "on_version_approved",
    "use_cases_released",
    "version_approved",
]

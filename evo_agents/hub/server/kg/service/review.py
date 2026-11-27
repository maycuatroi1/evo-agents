"""The review queue of a project's graph (``evo_agents.hub.server.kg_review``): who may read and change it, the
dismissals laid over the items the member sees, and the fixes a writer's loop applies as decisions.

The routes pass in ``read``, which reads the latest build's queue for the member (``kg_web.read_graph``: data and
GraphRef, None for no build), and ``decide``, which takes fixes as decisions (``kg_decisions.service.apply_fixes``
bound to the request). Each transaction is a UnitOfWork; the graph is read between them, as a web read is."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.kg_decisions import plugin_actor
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.review import (
    NO_BUILD,
    AppliedDecision,
    AppliedItem,
    ApplyBody,
    ApplyResult,
    Dismissal,
    ReportItem,
    ReviewItem,
    ReviewQueue,
    RuleCount,
    Undismissed,
)
from evo_agents.hub.server.kg.models.web import GraphRef
from evo_agents.hub.server.kg.repository import review as repository
from evo_agents.hub.server.kg.service.web import require_reader
from evo_agents.hub.server.kg_decisions.models import Fix, KgDecision
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg.review import RULES

Read = Callable[[], Awaitable[tuple[Any, GraphRef | None]]]  # the data is a dict whenever the GraphRef is set
Decide = Callable[..., Awaitable[tuple[list[tuple[Fix, KgDecision | None]], list[Fix], int]]]


async def _dismissals(conn: AsyncConnection, project_id: int) -> dict[str, Dismissal]:
    return {
        row.fingerprint: Dismissal(
            reason=row.reason,
            by=row.login or plugin_actor(row.dismissed_by_plugin),
            at=row.dismissed_at,
            build_id=row.build_id,
        )
        for row in await repository.dismissals(conn, project_id)
    }


async def _writer(engine: AsyncEngine, user, project: str, doing: str) -> ProjectAccess:
    """The member's access to the project, 403 unless they are a writer of it; ``doing`` names what needs it."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"{doing} of project {access.name} needs the writer role on it")
    return access


async def queue(
    engine: AsyncEngine, read: Read, user, project: str, rule: str | None, cluster: str | None, state: str
) -> ReviewQueue:
    """The items of the review queue the member sees, grouped by rule, most connected first, with counts per rule."""
    data, ref = await read()
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        dismissed = await _dismissals(conn, access.project_id) if ref is not None else {}
    can_dismiss = holds(access.role, "writer")
    if ref is None:
        empty = [RuleCount(rule=r, open=0, dismissed=0) for r in RULES]
        return ReviewQueue(graph=None, computed=False, reason=NO_BUILD, can_dismiss=can_dismiss, counts=empty, items=[])
    items = [ReviewItem(**item, dismissal=dismissed.get(item["fingerprint"])) for item in data["items"]]
    counts = [
        RuleCount(
            rule=r,
            open=sum(1 for i in items if i.rule == r and i.dismissal is None),
            dismissed=sum(1 for i in items if i.rule == r and i.dismissal is not None),
        )
        for r in RULES
    ]
    shown = [
        i
        for i in items
        if (rule is None or i.rule == rule)
        and (cluster is None or i.cluster == cluster)
        and (state == "all" or (i.dismissal is None) == (state == "open"))
    ]
    return ReviewQueue(
        graph=ref,
        computed=data["computed"],
        reason=data["reason"],
        can_dismiss=can_dismiss,
        counts=counts,
        items=shown,
    )


def _not_visible(project: str) -> HTTPException:
    return HTTPException(404, f"no review item with that fingerprint is visible to you in project {project}")


async def dismiss(engine: AsyncEngine, read: Read, user, project: str, fingerprint: str, reason: str) -> Dismissal:
    """Dismiss an item the writer sees, with a reason; ``read`` gives a graph (404 for none)."""
    access = await _writer(engine, user, project, "dismissing a review item")
    data, ref = await read()
    assert ref is not None, "read answers 404 for a project without a graph"
    item = next((i for i in data["items"] if i["fingerprint"] == fingerprint), None)
    if item is None:
        raise _not_visible(project)
    async with UnitOfWork(engine) as conn:
        at = await repository.save_dismissal(
            conn,
            project_id=access.project_id,
            fingerprint=fingerprint,
            rule=item["rule"],
            reason=reason,
            user_id=user.user_id,
            build_id=ref.build_id,
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_REVIEW,
            target=f"{project} item={fingerprint}",
            project_id=access.project_id,
        )
    return Dismissal(reason=reason, by=user.login, at=at, build_id=ref.build_id)


async def _apply_access(engine: AsyncEngine, user, project: str, run_id: int | None):
    """The writer's access, after checking ``run_id`` is a run of the project, and the dismissals."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"applying the review queue of project {access.name} needs the writer role on it")
        if run_id is not None and await repository.run_project(conn, run_id) != access.project_id:
            raise HTTPException(404, f"no run {run_id} in project {access.name}")
        return access, await _dismissals(conn, access.project_id)


def _items(asked: list[dict], taken, skipped) -> tuple[list[AppliedItem], dict[str, int]]:
    """The items whose fix took a decision, and per rule the open items with a fix not wholly taken."""
    done: dict[str, list[AppliedDecision]] = {}
    for fix, made in taken:
        number = made.number if isinstance(made, KgDecision) else None
        shown = fix.model_dump(exclude={"fingerprint"})
        done.setdefault(fix.fingerprint, []).append(AppliedDecision(number=number, **shown))
    said: dict[str, int] = {}
    for fix in skipped:
        said[fix.fingerprint] = said.get(fix.fingerprint, 0) + 1
    items, left = [], {}
    for i in asked:
        fix = i["detail"].get("fix") or []
        whole = bool(fix) and len(done.get(i["fingerprint"], [])) + said.get(i["fingerprint"], 0) == len(fix)
        if i["fingerprint"] in done:
            items.append(
                AppliedItem(fingerprint=i["fingerprint"], rule=i["rule"], whole=whole, decisions=done[i["fingerprint"]])
            )
        if fix and not whole:
            left[i["rule"]] = left.get(i["rule"], 0) + 1
    return items, left


async def apply(engine: AsyncEngine, read: Read, decide: Decide, user, project: str, body: ApplyBody) -> ApplyResult:
    """Apply the fixes of the open review items the writer sees as decisions, at most body.max_decisions, through
    ``decide``; ``read`` gives a graph (404 for none)."""
    access, dismissed = await _apply_access(engine, user, project, body.run_id)
    data, ref = await read()
    assert ref is not None, "read answers 404 for a project without a graph"
    asked = [
        i
        for i in data["items"]
        if i["fingerprint"] not in dismissed and (body.rules is None or i["rule"] in body.rules)
    ]
    fixes = [Fix(**d, fingerprint=i["fingerprint"]) for i in asked for d in i["detail"].get("fix") or []]
    taken, skipped, rev = await decide(
        user,
        access,
        fixes,
        cap=body.max_decisions,
        origin=body.origin,
        run_id=body.run_id,
        build_id=ref.build_id,
        dry_run=body.dry_run,
    )
    items, left = _items(asked, taken, skipped)
    report = [
        ReportItem(
            fingerprint=i["fingerprint"],
            rule=i["rule"],
            cluster=i["cluster"],
            cluster_label=i["cluster_label"],
            nodes=i["nodes"],
        )
        for i in asked
        if i["detail"].get("report")
    ]
    return ApplyResult(
        graph=ref,
        rev=rev,
        dry_run=body.dry_run,
        max_decisions=body.max_decisions,
        decided=len(taken),
        skipped=len(skipped),
        items=items,
        report=report,
        left=left,
    )


async def undismiss(engine: AsyncEngine, read: Read, user, project: str, fingerprint: str) -> Undismissed:
    """Take a dismissal of an item the writer sees back, whoever made it; ``read`` gives a graph (404 for none)."""
    access = await _writer(engine, user, project, "reopening a review item")
    data, _ = await read()
    if not any(i["fingerprint"] == fingerprint for i in data["items"]):
        raise _not_visible(project)
    async with UnitOfWork(engine) as conn:
        row = await repository.locked_dismissal(conn, access.project_id, fingerprint)
        if row is None:
            raise HTTPException(404, f"no review item with that fingerprint is dismissed in project {project}")
        by = row.login or plugin_actor(row.dismissed_by_plugin)
        await repository.delete_dismissal(conn, access.project_id, fingerprint)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_REVIEW,
            target=f"{project} item={fingerprint} reopened dismissed_by={by}",
            project_id=access.project_id,
        )
    return Undismissed(fingerprint=fingerprint, was_by=by)

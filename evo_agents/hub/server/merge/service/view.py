"""What the routes of the hub's merge queue read and answer (``routes.view``): who may read the queue, the merge
requests of a project with their ledger, its audit samples and breakers, its figures (``post_merge.figures``), and the
admin's answers (``service.post_merge``, ``service.passes``, ``service.breaker``) with the row they leave."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, post_merge
from evo_agents.hub.server.autonomy.service.policies import effective_policy
from evo_agents.hub.server.merge.models.view import (
    BreakerReset,
    MergeBreaker,
    MergeEvent,
    MergeFigure,
    MergeFigures,
    MergeQueueView,
    MergeSample,
    QueuedMergeRequest,
)
from evo_agents.hub.server.merge.repository import breaker as breaker_rows
from evo_agents.hub.server.merge.repository import view as views
from evo_agents.hub.server.merge.service import breaker, passes
from evo_agents.hub.server.merge.service import post_merge as after_merge
from evo_agents.hub.server.projects import project_access


async def reader(conn: AsyncConnection, user, project: str):
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the merge queue of project {access.name} needs a grant on it")
    return access


def entry(row, events: list) -> QueuedMergeRequest:
    keys = set(QueuedMergeRequest.model_fields) - {"events"}
    return QueuedMergeRequest(
        **{key: getattr(row, key) for key in keys},
        events=[
            MergeEvent(
                id=event.id,
                kind=event.kind,
                gate=event.gate,
                gate_name=merge_gate.GATES.get(event.gate) if event.gate is not None else None,
                passed=event.passed,
                reason=event.reason,
                sha=event.sha,
                run_id=event.run_id,
                detail=event.detail,
                created_at=event.created_at,
            )
            for event in events
        ],
    )


def breaker_of(row) -> MergeBreaker:
    keys = set(MergeBreaker.model_fields) - {"resumed_by"}
    return MergeBreaker(**{key: getattr(row, key) for key in keys}, resumed_by=row.resumed_login)


def sample_of(row) -> MergeSample:
    return MergeSample(
        id=row.id,
        queue_id=row.queue_id,
        repo=row.repo,
        iid=row.iid,
        title=row.title,
        web_url=row.web_url,
        result=row.result,
        note=row.note,
        answered_by=row.answered_login,
        answered_at=row.answered_at,
        created_at=row.created_at,
    )


async def queue_view(conn: AsyncConnection, user, project: str, repo: str | None, limit: int) -> MergeQueueView:
    """The merge requests of the project's queue (``repo``'s alone when given), newest first, each with its ledger,
    its audit samples and the breaker of each repo."""
    access = await reader(conn, user, project)
    rows = await views.requests(conn, access.project_id, repo, limit)
    events = await views.events(conn, [row.id for row in rows])
    found = await views.samples(conn, access.project_id, repo)
    breakers = await breaker_rows.breakers(conn, access.project_id, repo)
    return MergeQueueView(
        project=access.name,
        requests=[entry(row, events[row.id]) for row in rows],
        samples=[sample_of(row) for row in found],
        breakers=[breaker_of(row) for row in breakers],
    )


async def figures(conn: AsyncConnection, user, project: str, repo: str | None, days: int) -> MergeFigures:
    """For each repo, agent and model, the merges of the last ``days`` (``post_merge.figures``)."""
    access = await reader(conn, user, project)
    rows, rolled_back, found_faulty = await views.figure_rows(conn, access.project_id, repo, days)
    for row in rows:
        row["judged"] = bool(row["judged"])
        for key in ("waited", "to_uat", "to_prod"):
            row[key] = float(row[key]) if row[key] is not None else None
    counted = post_merge.figures(rows, rolled_back=rolled_back, faulty=found_faulty)
    return MergeFigures(project=access.name, days=days, figures=[MergeFigure(**item) for item in counted])


async def answered(conn: AsyncConnection, row) -> QueuedMergeRequest:
    return entry(row, (await views.events(conn, [row.id]))[row.id])


async def answer_sample(
    conn: AsyncConnection, user, project: str, sample_id: int, result: str, note: str | None
) -> MergeSample:
    """An admin's answer to an audit sample, and the sample it leaves."""
    access = await project_access(conn, user, project)
    await after_merge.answer_sample(conn, user, access, sample_id, result, note)
    return sample_of(await views.sample(conn, access.project_id, sample_id))


async def approve_release(conn: AsyncConnection, user, project: str, queue_id: int) -> QueuedMergeRequest:
    """An admin approves the PROD release of a merge, under the ``prod_release`` of its repo in the policy."""
    access = await project_access(conn, user, project)
    repo = await views.repo_of(conn, queue_id)
    policy = await effective_policy(conn, access.project_id)
    release = policy["repos"].get(repo, {}).get("prod_release", "immediate") if repo else "immediate"
    row = await after_merge.approve_release(conn, user, access, queue_id, release)
    return await answered(conn, row)


async def ask_revert(conn: AsyncConnection, user, project: str, queue_id: int) -> QueuedMergeRequest:
    access = await project_access(conn, user, project)
    row = await after_merge.ask_revert(conn, user, access, queue_id)
    return await answered(conn, row)


async def pass_hold(conn: AsyncConnection, user, project: str, queue_id: int, reason: str) -> QueuedMergeRequest:
    access = await project_access(conn, user, project)
    row = await passes.pass_hold(conn, user, access, queue_id, reason)
    return await answered(conn, row)


async def resume_repo(conn: AsyncConnection, user, project: str, repo: str) -> BreakerReset:
    """An admin resets the breaker of ``repo``; the breaker it leaves and the iids ready again."""
    access = await project_access(conn, user, project)
    resumed = await breaker.resume(conn, user, access, repo)
    (row,) = await breaker_rows.breakers(conn, access.project_id, repo)
    return BreakerReset(**breaker_of(row).model_dump(), resumed=resumed)

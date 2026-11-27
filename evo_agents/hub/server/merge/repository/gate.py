"""The queries of the merge queue's rows and their ledger (merge_queue, merge_events), and what the gate reads to
take, open and move a merge request (``service.gate``, ``service.gate_pass``): the policy, the charter and the plan
of a project, the runs whose work is done, the builder of a run."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import merge_gate, tables
from evo_agents.hub.server.merge.models.gate import MAX_REVISIONS, OPEN_RETRY

# The rows and their ledger


async def record(
    conn: AsyncConnection,
    row,
    kind: str,
    *,
    gate: int | None = None,
    passed: bool | None = None,
    reason: str | None = None,
    sha: str | None = None,
    run_id: int | None = None,
    detail: dict | None = None,
) -> None:
    """Add a line to the ledger of merge request ``row``."""
    await conn.execute(
        insert(tables.merge_events).values(
            project_id=row.project_id,
            queue_id=row.id,
            kind=kind,
            gate=gate,
            passed=passed,
            reason=merge_gate.reason_line(reason) if reason else None,
            sha=sha,
            run_id=run_id,
            detail=detail or {},
        )
    )


async def set_row(conn: AsyncConnection, row_id: int, *, stamp: tuple[str, ...] = (), **values) -> None:
    """Write ``values`` to merge request ``row_id``, and the database's now to each column of ``stamp``."""
    q = tables.merge_queue
    now = {column: func.now() for column in stamp}
    await conn.execute(update(q).values(updated_at=func.now(), **now, **values).where(q.c.id == row_id))


async def set_ready(conn: AsyncConnection, row_id: int) -> None:
    """Merge request ``row_id`` waits for its turn: at the place it had when its turn sent it back (``queued_at``
    kept), else last."""
    q = tables.merge_queue
    await set_row(
        conn,
        row_id,
        state="ready",
        reason=None,
        queued_at=func.coalesce(q.c.queued_at, func.now()),
        rebases=0,
        judge_run_id=None,
        judge_key=None,
    )


async def row_of(conn: AsyncConnection, row_id: int, *, lock: bool = False):
    q = tables.merge_queue
    query = select(q).where(q.c.id == row_id)
    return (await conn.execute(query.with_for_update() if lock else query)).one_or_none()


async def insert_row(conn: AsyncConnection, values: dict) -> int:
    """A new row of the queue with ``values``; its id."""
    q = tables.merge_queue
    return (await conn.execute(insert(q).values(**values).returning(q.c.id))).scalar_one()


async def older(conn: AsyncConnection, moment: datetime, span: timedelta = OPEN_RETRY) -> bool:
    """Whether ``moment`` is more than ``span`` ago, by the database's clock."""
    return bool((await conn.execute(select(func.now() - moment > span))).scalar_one())


# What the gate reads of a project


async def charter_of(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def plan_body(conn: AsyncConnection, project_id: int, plan_id: str | None) -> dict | None:
    if not plan_id:
        return None
    p = tables.plans
    query = select(p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan_id)
    found = (await conn.execute(query)).scalar_one_or_none()
    return found if isinstance(found, dict) else None


async def newest_policy(conn: AsyncConnection, project_id: int) -> dict | None:
    """The body of the newest revision of the project's autonomy policy, None without one."""
    a = tables.autonomy_policies
    query = select(a.c.body).where(a.c.project_id == project_id).order_by(a.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def policy_revisions(conn: AsyncConnection, project_id: int) -> list:
    """The newest MAX_REVISIONS revisions of the project's policy, the newest first: when, the body and who wrote it."""
    a = tables.autonomy_policies
    query = (
        select(a.c.created_at, a.c.body, a.c.created_by)
        .where(a.c.project_id == project_id)
        .order_by(a.c.revision.desc())
        .limit(MAX_REVISIONS)
    )
    return list((await conn.execute(query)).all())


async def project_name(conn: AsyncConnection, project_id: int) -> str:
    p = tables.projects
    return (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()


async def project_ids(conn: AsyncConnection) -> set[int]:
    """The projects with a policy, or with merge requests still in their queue."""
    a, q = tables.autonomy_policies, tables.merge_queue
    with_policy = {row[0] for row in await conn.execute(select(a.c.project_id).distinct())}
    queued = select(q.c.project_id).where(q.c.state.in_(merge_gate.QUEUED_STATES)).distinct()
    return with_policy | {row[0] for row in await conn.execute(queued)}


async def builder(conn: AsyncConnection, run_id: int | None) -> tuple[str | None, str | None]:
    """(the agent profile as @handle, the model) of run ``run_id``."""
    if run_id is None:
        return None, None
    r, ap = tables.runs, tables.agent_profiles
    query = select(r.c.model, ap.c.handle).join_from(r, ap, ap.c.id == r.c.profile_id, isouter=True)
    found = (await conn.execute(query.where(r.c.id == run_id))).one_or_none()
    if found is None:
        return None, None
    return (f"@{found.handle}" if found.handle else None), found.model


async def change_queued(conn: AsyncConnection, change_id: int) -> bool:
    """Whether the Curator's change ``change_id`` has a row in the queue."""
    q = tables.merge_queue
    return (await conn.execute(select(q.c.id).where(q.c.change_id == change_id))).first() is not None


async def run_state(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.state).where(r.c.id == run_id))).scalar_one_or_none()


async def judged_by(conn: AsyncConnection, run_id: int):
    """(the repo, the iid) of the merge request judge run ``run_id`` judged, or None."""
    q = tables.merge_queue
    return (await conn.execute(select(q.c.repo, q.c.iid).where(q.c.judge_run_id == run_id))).first()


# The merge requests of a repo


async def waiting_rows(conn: AsyncConnection, project_id: int, repo: str, states) -> list:
    """The rows of ``repo`` in ``states``, locked."""
    q = tables.merge_queue
    query = select(q).where(q.c.project_id == project_id, q.c.repo == repo, q.c.state.in_(states))
    return list((await conn.execute(query.with_for_update())).all())


async def open_ids(conn: AsyncConnection, project_id: int, repo: str) -> list[int]:
    """The ids of the merge requests of ``repo`` the queue has open on GitLab, the oldest first."""
    q = tables.merge_queue
    query = select(q.c.id).where(
        q.c.project_id == project_id,
        q.c.repo == repo,
        q.c.state.in_(merge_gate.OPEN_STATES),
        q.c.iid.is_not(None),
    )
    return [row.id for row in await conn.execute(query.order_by(q.c.id))]


async def done_plans(conn: AsyncConnection, project_id: int, since: datetime) -> list:
    """(plan_id, run_id) of the newest plan or step run of each plan that ended done after ``since``."""
    r = tables.runs
    latest = (
        select(r.c.plan_id, func.max(r.c.id).label("run_id"))
        .where(
            r.c.project_id == project_id,
            r.c.kind.in_(("plan", "step")),
            r.c.state == "done",
            r.c.plan_id.is_not(None),
            r.c.finished_at > since,
        )
        .group_by(r.c.plan_id)
    )
    return list((await conn.execute(latest)).all())


async def plan_row(conn: AsyncConnection, project_id: int, repo: str, plan_id: str):
    """(id, iid, updated_at) of the row of the merge request of plan ``plan_id`` in ``repo``, or None."""
    q = tables.merge_queue
    query = select(q.c.id, q.c.iid, q.c.updated_at).where(
        q.c.project_id == project_id, q.c.repo == repo, q.c.source == "plan", q.c.plan_id == plan_id
    )
    return (await conn.execute(query)).one_or_none()


async def done_tasks(conn: AsyncConnection, project_id: int, repo: str, since: datetime) -> list:
    """The task runs that ended done after ``since`` and pushed their branch of ``repo``, with no row yet."""
    r, q, e = tables.runs, tables.merge_queue, tables.run_events
    pushed = exists().where(e.c.run_id == r.c.id, e.c.kind == "system", e.c.body["pull"]["repo"].astext == repo)
    taken = exists().where(q.c.project_id == project_id, q.c.repo == repo, q.c.source == "task", q.c.run_id == r.c.id)
    query = select(r.c.id, r.c.branch, r.c.title, r.c.request).where(
        r.c.project_id == project_id,
        r.c.kind == "task",
        r.c.state == "done",
        r.c.finished_at > since,
        pushed,
        ~taken,
    )
    return list((await conn.execute(query.order_by(r.c.id))).all())


async def rows_by_iid(conn: AsyncConnection, project_id: int, repo: str, iids: list[int]) -> dict:
    """The rows of the merge requests ``iids`` of ``repo`` (iid, id, state, source), by iid."""
    q = tables.merge_queue
    query = select(q.c.iid, q.c.id, q.c.state, q.c.source).where(
        q.c.project_id == project_id, q.c.repo == repo, q.c.iid.in_(iids)
    )
    return {row.iid: row for row in await conn.execute(query)}


async def waiting_builders(conn: AsyncConnection, project_id: int, repo: str) -> list:
    """(id, source_branch) of each Curator's change of ``repo`` handed to the queue whose merge request it has not
    found yet."""
    q = tables.merge_queue
    query = select(q.c.id, q.c.source_branch).where(
        q.c.project_id == project_id,
        q.c.repo == repo,
        q.c.source == "builder",
        q.c.iid.is_(None),
        q.c.state == "gating",
    )
    return list((await conn.execute(query)).all())

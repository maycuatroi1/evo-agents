"""The rows of a member's task run: its run in no repo, its member_tasks row (0263) with the input and the assets, and
the task as its latest attempt has it."""

from __future__ import annotations

from sqlalchemy import insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, task_runs
from evo_agents.hub.server.member_tasks.models import MemberTask


class TaskStore:
    async def add(self, conn: AsyncConnection, run_id: int, project_id: int, input: dict, assets: list[str]) -> None:
        """Make run ``run_id`` a task in no repo, on its own branch, and keep its input and assets."""
        r = tables.runs
        await conn.execute(
            update(r).values(branch=task_runs.branch_of(run_id), repos=null(), pulls=[]).where(r.c.id == run_id)
        )
        task = {"task_id": run_id, "project_id": project_id, "input": input, "assets": assets}
        await conn.execute(insert(tables.member_tasks).values(**task))

    async def shown(self, conn: AsyncConnection, project: str, task: int) -> MemberTask | None:
        """Task ``task`` of ``project`` as its latest attempt has it; None for no such task."""
        r, m, p, u = tables.runs, tables.member_tasks, tables.projects, tables.users
        query = (
            select(
                r.c.id,
                r.c.attempt,
                p.c.name.label("project"),
                r.c.title,
                r.c.state,
                u.c.login.label("dispatched_by"),
                m.c.input,
                m.c.assets,
                r.c.evidence,
                r.c.error,
                r.c.failure_cause,
                r.c.usage,
                r.c.queued_at,
                r.c.finished_at,
            )
            .join_from(r, p, p.c.id == r.c.project_id)
            .join(m, m.c.task_id == task)
            .outerjoin(u, u.c.id == r.c.dispatched_by)
            .where(
                p.c.name == project,
                m.c.project_id == r.c.project_id,
                r.c.kind == "task",
                r.c.agent_type_id.is_(None),
                r.c.branch == task_runs.branch_of(task),
            )
            .order_by(r.c.attempt.desc())
            .limit(1)
        )
        row = (await conn.execute(query)).one_or_none()
        if row is None:
            return None
        return MemberTask(
            task_id=task,
            run_id=row.id,
            attempt=row.attempt,
            project=row.project,
            title=row.title,
            state=row.state,
            dispatched_by=row.dispatched_by,
            input=dict(row.input or {}),
            assets=list(row.assets or []),
            output=row.evidence,
            error=row.error,
            failure_cause=row.failure_cause,
            usage=row.usage if isinstance(row.usage, dict) else None,
            queued_at=row.queued_at,
            finished_at=row.finished_at,
        )


STORE = TaskStore()

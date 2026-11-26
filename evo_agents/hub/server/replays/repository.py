"""The SQL of the Curator's replays: the changes a replay may measure, the suite's tasks in replay_tasks, the
replays in curator_replays, and the charter and the changes they read."""

from __future__ import annotations

from sqlalchemy import func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import replay, tables


async def replayable(conn: AsyncConnection, project_id: int) -> list:
    """The project's changes a replay may measure, by their state, oldest first, with their proposal's paths and
    kind."""
    c, pr = tables.curator_changes, tables.proposals
    query = (
        select(c, pr.c.paths, pr.c.kind.label("kind"), pr.c.title.label("proposal_title"))
        .join_from(c, pr, pr.c.id == c.c.proposal_id)
        .where(
            c.c.project_id == project_id,
            c.c.state.in_(replay.REPLAY_STATES),
            c.c.passed.is_not(False),  # the Judge or the hub's detector failed it: no replay gives it its merge
            c.c.builder_run_id.is_not(None),
            c.c.branch.is_not(None),
            c.c.plan_id.is_not(None),
        )
        .order_by(c.c.id)
    )
    return list((await conn.execute(query)).all())


async def replays_of_change(conn: AsyncConnection, change_id: int) -> list:
    """The replays of the change, oldest first, each with whether its task is unusable."""
    rp, t = tables.curator_replays, tables.replay_tasks
    query = select(rp, t.c.unusable).join(t, t.c.id == rp.c.task_id).where(rp.c.change_id == change_id)
    return list((await conn.execute(query.order_by(rp.c.id))).all())


async def fail_replay(conn: AsyncConnection, replay_id: int, reason: str | None) -> None:
    rp = tables.curator_replays
    await conn.execute(
        update(rp).values(state="failed", reason=reason, updated_at=func.now()).where(rp.c.id == replay_id)
    )


async def task(conn: AsyncConnection, task_id: int):
    t = tables.replay_tasks
    return (await conn.execute(select(t).where(t.c.id == task_id))).one()


async def holdout_tasks(conn: AsyncConnection, project_id: int, used: list[int]):
    """The usable tasks of the project's holdout split but ``used``, in the order they came in."""
    t = tables.replay_tasks
    return await conn.execute(
        select(t)
        .where(t.c.project_id == project_id, t.c.split == "holdout", t.c.unusable.is_(None), t.c.id.not_in(used))
        .order_by(t.c.id)
    )


async def start_replay(conn: AsyncConnection, project_id: int, change_id: int, task_id: int, run_id: int) -> None:
    await conn.execute(
        insert(tables.curator_replays).values(
            project_id=project_id, change_id=change_id, task_id=task_id, run_id=run_id, state="running"
        )
    )


async def restart_replay(conn: AsyncConnection, replay_id: int, run_id: int) -> None:
    rp = tables.curator_replays
    await conn.execute(
        update(rp)
        .values(state="running", run_id=run_id, result_a=null(), result_b=null(), reason=None, updated_at=func.now())
        .where(rp.c.id == replay_id)
    )


async def lock_running(conn: AsyncConnection, run_id: int):
    """The id and task of the replay run ``run_id`` replays now, locked; None when it replays nothing."""
    rp = tables.curator_replays
    query = select(rp.c.id, rp.c.task_id).where(rp.c.run_id == run_id, rp.c.state == "running").with_for_update()
    return (await conn.execute(query)).one_or_none()


async def set_key(conn: AsyncConnection, replay_id: int, digest: str) -> None:
    rp = tables.curator_replays
    await conn.execute(update(rp).values(replay_key=digest, updated_at=func.now()).where(rp.c.id == replay_id))


async def task_of_run(conn: AsyncConnection, run_id: int):
    """The task replay run ``run_id`` replays; None when it replays none."""
    rp, t = tables.curator_replays, tables.replay_tasks
    return (await conn.execute(select(t).join(rp, rp.c.task_id == t.c.id).where(rp.c.run_id == run_id))).one_or_none()


async def lock_run(conn: AsyncConnection, run_id: int):
    """The state, worker and kind of the run, locked; None when there is no such run."""
    r = tables.runs
    query = select(r.c.state, r.c.worker_id, r.c.kind).where(r.c.id == run_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def lock_replay_of_run(conn: AsyncConnection, run_id: int):
    """The replay of run ``run_id``, locked; None without one."""
    rp = tables.curator_replays
    return (await conn.execute(select(rp).where(rp.c.run_id == run_id).with_for_update())).one_or_none()


async def task_unusable(conn: AsyncConnection, task_id: int, why: str | None) -> None:
    t = tables.replay_tasks
    await conn.execute(update(t).values(unusable=why).where(t.c.id == task_id, t.c.unusable.is_(None)))


async def write_replay(conn: AsyncConnection, replay_id: int, values: dict) -> None:
    rp = tables.curator_replays
    await conn.execute(update(rp).values(**values, updated_at=func.now()).where(rp.c.id == replay_id))


async def task_base(conn: AsyncConnection, task_id: int, base_sha: str | None) -> None:
    """Set the task's base commit, once."""
    t = tables.replay_tasks
    await conn.execute(update(t).values(base_sha=base_sha).where(t.c.id == task_id, t.c.base_sha.is_(None)))


async def lock_change(conn: AsyncConnection, change_id: int):
    c = tables.curator_changes
    return (await conn.execute(select(c).where(c.c.id == change_id).with_for_update())).one()


async def judge_again(conn: AsyncConnection, change_id: int, values: dict) -> None:
    c = tables.curator_changes
    await conn.execute(update(c).values(**values, updated_at=func.now()).where(c.c.id == change_id))


async def replays(conn: AsyncConnection, ids: list[int]):
    """The replays of ``ids``, each with its task's plan, step, repo, title and split, in order of id."""
    rp, t = tables.curator_replays, tables.replay_tasks
    query = (
        select(rp, t.c.plan_id, t.c.step_key, t.c.repo, t.c.title, t.c.split)
        .join(t, t.c.id == rp.c.task_id)
        .where(rp.c.id.in_(ids))
        .order_by(rp.c.id)
    )
    return await conn.execute(query)


async def tasks(conn: AsyncConnection, project_id: int) -> list:
    """The project's suite, in the order its tasks came in."""
    t = tables.replay_tasks
    return list((await conn.execute(select(t).where(t.c.project_id == project_id).order_by(t.c.id))).all())


async def change_of_project(conn: AsyncConnection, change_id: int, project_id: int):
    """The id, head and label of the project's change; None when it has no such change."""
    c, pr = tables.curator_changes, tables.proposals
    query = (
        select(c.c.id, c.c.head_sha, pr.c.label)
        .join_from(c, pr, pr.c.id == c.c.proposal_id)
        .where(c.c.id == change_id, c.c.project_id == project_id)
    )
    return (await conn.execute(query)).one_or_none()


async def replay_labels(conn: AsyncConnection, change_id: int):
    """The id of each replay of the change with its task's label, in order of id."""
    rp, t = tables.curator_replays, tables.replay_tasks
    return await conn.execute(
        select(rp.c.id, t.c.label).join(t, t.c.id == rp.c.task_id).where(rp.c.change_id == change_id).order_by(rp.c.id)
    )


async def newest_charter_body(conn: AsyncConnection, project_id: int):
    ch = tables.charters
    query = select(ch.c.body).where(ch.c.project_id == project_id).order_by(ch.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()

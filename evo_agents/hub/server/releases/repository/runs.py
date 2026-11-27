"""The SQL of the release runs (release_runs): queuing one's row, one with its cycle, schedule and project for its
claim, the branch its worker pushed, those that wait to land or that a landing left behind, and what each landing
keeps on it."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import releases, tables
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.tables import RUN_ACTIVE


async def insert_release_run(
    conn: AsyncConnection, run_id: int, cycle_id: int, mode: str, *, tag: str | None = None
) -> None:
    await conn.execute(insert(tables.release_runs).values(run_id=run_id, cycle_id=cycle_id, mode=mode, tag=tag))


def _release_run(run_id: int):
    """Run ``run_id``'s row of release_runs with its cycle, schedule and project."""
    rr, c, s, p = tables.release_runs, tables.release_cycles, tables.release_schedules, tables.projects
    return (
        select(
            rr.c.run_id,
            rr.c.cycle_id,
            rr.c.mode,
            rr.c.tag.label("red_tag"),
            rr.c.land_state,
            c.c.state.label("cycle_state"),
            c.c.base_tag,
            c.c.base_commit,
            c.c.target_ref,
            c.c.target_commit,
            c.c.span,
            s.c.repo,
            s.c.tag_pattern,
            s.c.paused_at,
            p.c.name.label("project"),
        )
        .join_from(rr, c, c.c.id == rr.c.cycle_id)
        .join(s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .where(rr.c.run_id == run_id)
    )


async def release_run(conn: AsyncConnection, run_id: int):
    """Run ``run_id``'s row of release_runs with its cycle, schedule and project, or None."""
    return (await conn.execute(_release_run(run_id))).one_or_none()


async def tagged_merge(conn: AsyncConnection, cycle_id: int, tag: str | None) -> str | None:
    """The merge commit the hub tagged ``tag`` on in cycle ``cycle_id``, or None."""
    rr = tables.release_runs
    query = select(rr.c.merge_commit).where(rr.c.cycle_id == cycle_id, rr.c.tagged == tag)
    return (await conn.execute(query.limit(1))).scalar_one_or_none()


async def locked_run(conn: AsyncConnection, run_id: int):
    """Run ``run_id``'s worker, state and kind, its row locked; or None."""
    r = tables.runs
    query = select(r.c.worker_id, r.c.state, r.c.kind).where(r.c.id == run_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def lock_run(conn: AsyncConnection, run_id: int) -> None:
    r = tables.runs
    await conn.execute(select(r.c.id).where(r.c.id == run_id).with_for_update())


async def branch_of(conn: AsyncConnection, run_id: int) -> str | None:
    rr = tables.release_runs
    return (await conn.execute(select(rr.c.branch).where(rr.c.run_id == run_id))).scalar_one()


async def save_release_run(conn: AsyncConnection, run_id: int, **values) -> None:
    rr = tables.release_runs
    await conn.execute(update(rr).where(rr.c.run_id == run_id).values(**values))


async def mark_tagged(conn: AsyncConnection, run_id: int, merge: str, tag: str) -> None:
    """Run ``run_id`` tagged ``tag`` now on ``merge``."""
    rr = tables.release_runs
    await conn.execute(
        update(rr)
        .where(rr.c.run_id == run_id)
        .values(merge_commit=merge, tagged=tag, tagged_at=func.now(), land_state="tagged", land_reason=None)
    )


async def count_try(conn: AsyncConnection, run_id: int, reason: str) -> int:
    """One more pass GitHub failed for the landing of ``run_id``, with ``reason``; the passes in a row."""
    rr = tables.release_runs
    statement = (
        update(rr)
        .where(rr.c.run_id == run_id)
        .values(land_tries=rr.c.land_tries + 1, land_reason=releases.clip_reason(reason))
        .returning(rr.c.land_tries)
    )
    return (await conn.execute(statement)).scalar_one()


async def newest_run(conn: AsyncConnection, cycle_id: int):
    """The newest row of release_runs of cycle ``cycle_id``, or None."""
    rr = tables.release_runs
    query = select(rr).where(rr.c.cycle_id == cycle_id).order_by(rr.c.run_id.desc()).limit(1)
    return (await conn.execute(query)).one_or_none()


async def cycle_runs(conn: AsyncConnection, cycle_id: int) -> list:
    """The runs of cycle ``cycle_id`` with their mode, tag, state and landing, oldest first."""
    rr, r = tables.release_runs, tables.runs
    query = (
        select(rr.c.run_id, rr.c.mode, rr.c.tag, r.c.state, rr.c.tagged, rr.c.land_state)
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .where(rr.c.cycle_id == cycle_id)
        .order_by(rr.c.run_id)
    )
    return list((await conn.execute(query)).all())


def runs_of(cycle_ids: list[int]):
    """The runs of the cycles ``cycle_ids`` with their state, cost and landing, by cycle then run."""
    rr, r = tables.release_runs, tables.runs
    landed = (rr.c.version, rr.c.branch, rr.c.decision, rr.c.pull_url, rr.c.merge_commit, rr.c.tagged, rr.c.tagged_at)
    return (
        select(
            rr.c.cycle_id,
            rr.c.run_id,
            rr.c.mode,
            rr.c.tag,
            r.c.state,
            usage_cost(r.c.usage).label("cost"),
            *landed,
            rr.c.land_state,
            rr.c.land_reason,
        )
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .where(rr.c.cycle_id.in_(cycle_ids))
        .order_by(rr.c.cycle_id, rr.c.run_id)
    )


async def runs_of_cycles(conn: AsyncConnection, cycle_ids: list[int]) -> list:
    return list((await conn.execute(runs_of(cycle_ids))).all())


async def busy_or_fixing(conn: AsyncConnection, cycle_id: int, tag: str) -> tuple[bool, bool]:
    """(whether a run of cycle ``cycle_id`` is active, whether a fix run of ``tag`` was queued in it)."""
    rr, r = tables.release_runs, tables.runs
    active = exists().where(rr.c.cycle_id == cycle_id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    asked = exists().where(rr.c.cycle_id == cycle_id, rr.c.mode == "fix", rr.c.tag == tag)
    busy, fixing = (await conn.execute(select(active, asked))).one()
    return busy, fixing


async def queued_release_runs(conn: AsyncConnection, project_id: int) -> list[int]:
    """The release runs of the project still queued, locked, skipping those another pass holds."""
    r = tables.runs
    query = (
        select(r.c.id)
        .where(r.c.project_id == project_id, r.c.kind == releases.RELEASE_KIND, r.c.state == "queued")
        .order_by(r.c.id)
        .with_for_update(skip_locked=True)
    )
    return list((await conn.execute(query)).scalars().all())


# Landing


def landing_rows(*where):
    """The release runs with their branch, done, that ``where`` keeps, with their cycle, schedule, project and repo."""
    rr, r, c, s, p, pr, u = (
        tables.release_runs,
        tables.runs,
        tables.release_cycles,
        tables.release_schedules,
        tables.projects,
        tables.project_repos,
        tables.users,
    )
    return (
        select(
            rr.c.run_id,
            rr.c.cycle_id,
            c.c.project_id,
            p.c.name.label("project"),
            c.c.owner_id,
            u.c.login.label("owner"),
            s.c.repo,
            pr.c.origin,
            s.c.tag_pattern,
            s.c.workflow,
            c.c.target_ref,
            c.c.target_commit,
            c.c.base_tag,
            c.c.state.label("cycle_state"),
            rr.c.version,
            rr.c.branch,
            rr.c.head_commit,
            rr.c.decision,
            rr.c.pull_number,
            rr.c.merge_commit,
        )
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .join(c, c.c.id == rr.c.cycle_id)
        .join(s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .join(u, u.c.id == c.c.owner_id)
        .outerjoin(pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == s.c.repo))
        .where(r.c.state == "done", rr.c.branch.is_not(None), *where)
        .order_by(rr.c.run_id)
    )


async def waiting(conn: AsyncConnection) -> list:
    """The release runs that wait to land: done, with their branch, not landed, of an open cycle."""
    query = landing_rows(tables.release_runs.c.land_state.is_(None), tables.release_cycles.c.state == "open")
    return list((await conn.execute(query)).all())


async def left_behind(conn: AsyncConnection) -> list:
    """The release runs a landing left behind: refused or failed, no tag, of a cycle open or stopped, whose branch no
    other run has taken since: one that waits to land it or landed it."""
    rr, other = tables.release_runs, tables.release_runs.alias("other")
    c = tables.release_cycles
    taken = exists().where(
        other.c.branch == rr.c.branch,
        other.c.run_id != rr.c.run_id,
        other.c.land_state.is_(None) | (other.c.land_state == "tagged"),
    )
    query = landing_rows(
        rr.c.land_state.in_(("refused", "failed")), rr.c.tagged.is_(None), c.c.state.in_(("open", "stopped")), ~taken
    )
    return list((await conn.execute(query)).all())

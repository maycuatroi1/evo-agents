"""The SQL of the release cycles: one with its schedule's repo, project, owner and cost, the one of a schedule that is
open or stopped, the open ones, the ended ones of a project, whether a run of one is active, and each move of its
state (opened, stopped, asked, open again, closed, published), with the decision a stop asks."""

from __future__ import annotations

from sqlalchemy import Text, and_, cast, exists, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import releases, tables
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.releases.models.releases import ENDED
from evo_agents.hub.tables import RUN_ACTIVE


def cost_of(cycle_id: int):
    """What the runs of cycle ``cycle_id`` cost, each agent session once at its largest total, as a scalar query."""
    rr, r = tables.release_runs, tables.runs
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(func.max(usage_cost(r.c.usage)).label("cost"))
        .join_from(rr, r, r.c.id == rr.c.run_id)
        .where(rr.c.cycle_id == cycle_id)
        .group_by(session)
        .subquery()
    )
    return select(func.coalesce(func.sum(per_session.c.cost), 0)).scalar_subquery()


def _cycle(cycle_id: int):
    """Cycle ``cycle_id`` with its schedule's repo, its project's name and its owner's login."""
    c, s, p, u = tables.release_cycles, tables.release_schedules, tables.projects, tables.users
    return (
        select(
            c,
            s.c.repo,
            s.c.tag_pattern,
            p.c.name.label("project"),
            u.c.login.label("owner"),
            cost_of(cycle_id).label("cost"),
        )
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .join(u, u.c.id == c.c.owner_id)
        .where(c.c.id == cycle_id)
    )


async def cycle_of(conn: AsyncConnection, cycle_id: int):
    return (await conn.execute(_cycle(cycle_id))).one()


async def unfinished(conn: AsyncConnection, schedule_id: int):
    """The cycle of ``schedule_id`` that is open or stopped, (id, state), or None."""
    c = tables.release_cycles
    query = select(c.c.id, c.c.state).where(c.c.schedule_id == schedule_id, c.c.state.in_(releases.UNFINISHED))
    return (await conn.execute(query)).one_or_none()


async def run_active(conn: AsyncConnection, cycle_id: int) -> bool:
    """Whether a run of cycle ``cycle_id`` is active."""
    rr, r = tables.release_runs, tables.runs
    active = exists().where(rr.c.cycle_id == cycle_id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    return (await conn.execute(select(active))).scalar_one()


async def insert_cycle(conn: AsyncConnection, values: dict) -> int:
    c = tables.release_cycles
    return (await conn.execute(insert(c).values(**values).returning(c.c.id))).scalar_one()


async def stop(conn: AsyncConnection, cycle_id: int, reason: str, cause: str) -> bool:
    """Stop cycle ``cycle_id`` while it is open, with ``cause`` and ``reason``; whether it stopped now."""
    c = tables.release_cycles
    statement = (
        update(c)
        .values(state="stopped", stop_reason=releases.clip_reason(reason), stop_cause=cause, updated_at=func.now())
        .where(c.c.id == cycle_id, c.c.state == "open")
        .returning(c.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none() is not None


async def insert_decision(conn: AsyncConnection, values: dict) -> int:
    d = tables.decisions
    return (await conn.execute(insert(d).values(**values).returning(d.c.id))).scalar_one()


async def set_asked(
    conn: AsyncConnection, cycle_id: int, decision_id: int, chat_id: int | None, root_id: int | None
) -> None:
    """Keep the decision a stop of cycle ``cycle_id`` asked and its thread in the triage chat."""
    c = tables.release_cycles
    await conn.execute(
        update(c)
        .where(c.c.id == cycle_id)
        .values(decision_id=decision_id, triage_chat_id=chat_id, triage_root_id=root_id)
    )


async def decision_state(conn: AsyncConnection, decision_id: int) -> str | None:
    d = tables.decisions
    return (await conn.execute(select(d.c.state).where(d.c.id == decision_id))).scalar_one_or_none()


async def cancel_decision(conn: AsyncConnection, decision_id: int) -> None:
    """Cancel decision ``decision_id`` while it is open, and mark its notifications read."""
    d, n = tables.decisions, tables.notifications
    await conn.execute(update(d).values(state="cancelled").where(d.c.id == decision_id, d.c.state == "open"))
    unread = (n.c.decision_id == decision_id, n.c.read_at.is_(None))
    await conn.execute(update(n).values(read_at=func.now()).where(*unread))


async def asked_by(conn: AsyncConnection, decision_id: int):
    """The cycle that asked decision ``decision_id``, (id, state), locked; or None."""
    c = tables.release_cycles
    query = select(c.c.id, c.c.state).where(c.c.decision_id == decision_id).with_for_update()
    return (await conn.execute(query)).one_or_none()


async def lock_stopped(conn: AsyncConnection, cycle_id: int):
    """Stopped cycle ``cycle_id``'s row, locked."""
    c = tables.release_cycles
    return (await conn.execute(select(c).where(c.c.id == cycle_id, c.c.state == "stopped").with_for_update())).one()


async def lock_state(conn: AsyncConnection, cycle_id: int) -> str:
    """The state of cycle ``cycle_id``, its row locked."""
    c = tables.release_cycles
    return (await conn.execute(select(c.c.state).where(c.c.id == cycle_id).with_for_update())).scalar_one()


async def state_of(conn: AsyncConnection, cycle_id: int) -> str:
    c = tables.release_cycles
    return (await conn.execute(select(c.c.state).where(c.c.id == cycle_id))).scalar_one()


async def reopen(conn: AsyncConnection, cycle_id: int) -> None:
    """Cycle ``cycle_id`` open again, its stop forgotten."""
    c = tables.release_cycles
    await conn.execute(
        update(c)
        .where(c.c.id == cycle_id)
        .values(state="open", stop_reason=None, stop_cause=None, updated_at=func.now())
    )


async def close(conn: AsyncConnection, cycle_id: int, reason: str) -> None:
    """Cycle ``cycle_id`` closed now with ``reason``."""
    c = tables.release_cycles
    closed = {"state": "closed", "stop_reason": reason, "ended_at": func.now(), "updated_at": func.now()}
    await conn.execute(update(c).where(c.c.id == cycle_id).values(**closed))


async def close_idle(conn: AsyncConnection, schedule_id: int, reason: str) -> int | None:
    """Close the open cycle of ``schedule_id`` that has no run active; its id, or None."""
    c, rr, r = tables.release_cycles, tables.release_runs, tables.runs
    active = exists().where(rr.c.cycle_id == c.c.id, r.c.id == rr.c.run_id, r.c.state.in_(RUN_ACTIVE))
    statement = (
        update(c)
        .values(state="closed", stop_reason=releases.clip_reason(reason), ended_at=func.now(), updated_at=func.now())
        .where(and_(c.c.schedule_id == schedule_id, c.c.state == "open", ~active))
        .returning(c.c.id)
    )
    return (await conn.execute(statement)).scalar_one_or_none()


async def publish(conn: AsyncConnection, cycle_id: int) -> None:
    c = tables.release_cycles
    await conn.execute(
        update(c).where(c.c.id == cycle_id).values(state="published", ended_at=func.now(), updated_at=func.now())
    )


async def open_cycles(conn: AsyncConnection) -> list:
    """The open cycles with their base tag, their schedule's workflow, pattern and repo, its origin and the project."""
    c, s, p, pr = tables.release_cycles, tables.release_schedules, tables.projects, tables.project_repos
    query = (
        select(c.c.id, c.c.base_tag, s.c.workflow, s.c.tag_pattern, s.c.repo, pr.c.origin, p.c.name.label("project"))
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(p, p.c.id == c.c.project_id)
        .outerjoin(pr, (pr.c.project_id == c.c.project_id) & (pr.c.name == s.c.repo))
        .where(c.c.state == "open")
        .order_by(c.c.id)
    )
    return list((await conn.execute(query)).all())


# As the API shows them


def _cycles():
    """The cycles with their owner's login, for ``service.releases.cycle_views``."""
    c, u = tables.release_cycles, tables.users
    return select(c, u.c.login.label("owner")).join_from(c, u, u.c.id == c.c.owner_id)


async def shown_cycle(conn: AsyncConnection, cycle_id: int):
    """Cycle ``cycle_id`` with its owner's login."""
    return (await conn.execute(_cycles().where(tables.release_cycles.c.id == cycle_id))).one()


async def ended_cycles(conn: AsyncConnection, project_id: int, limit: int, offset: int) -> tuple[int, list]:
    """(how many cycles of the project ended, published or closed; a page of them, newest first, with their owner's
    login)."""
    c = tables.release_cycles
    ended = (c.c.project_id == project_id, c.c.state.in_(ENDED))
    total = (await conn.execute(select(func.count()).select_from(c).where(*ended))).scalar_one()
    page = _cycles().where(*ended).order_by(c.c.id.desc()).limit(limit).offset(offset)
    return total, list((await conn.execute(page)).all())

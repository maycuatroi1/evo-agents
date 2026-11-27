"""Every query of the threads of a failed or stale run (``triage.service.threads`` is the design): the thread a run
has, the triage chats of a project and the people in them, the facts of a run its first message names, and the
runs that stopped moving."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import case, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.triage.models.threads import KINDS, STALE_MINUTES, STALE_STATES


async def triage_row(conn: AsyncConnection, run_id: int):
    """(chat_id, root_message_id) of the thread run ``run_id`` opened; None for none."""
    t = tables.run_triage
    return (await conn.execute(select(t.c.chat_id, t.c.root_message_id).where(t.c.run_id == run_id))).one_or_none()


async def agent_run_of(conn: AsyncConnection, run_id: int) -> int | None:
    """The chat run the thread of run ``run_id`` started for its agent; None for none."""
    t = tables.run_triage
    return (await conn.execute(select(t.c.agent_run_id).where(t.c.run_id == run_id))).scalar_one_or_none()


async def insert_run_triage(conn: AsyncConnection, **values) -> None:
    """Write the ``run_triage`` row of a thread."""
    await conn.execute(insert(tables.run_triage).values(**values))


async def triage_enabled(conn: AsyncConnection, project_id: int) -> bool:
    p = tables.projects
    return bool((await conn.execute(select(p.c.triage_enabled).where(p.c.id == project_id))).scalar_one())


async def lock(conn: AsyncConnection, project_id: int) -> None:
    """The lock of the triage chats of project ``project_id``, until the transaction ends."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"triage:{project_id}", 0))))


# The chats


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def plan_label(conn: AsyncConnection, project_id: int, plan_id: str | None):
    """The stored label of plan ``plan_id`` of the project; None when the plan is not on the hub."""
    pl = tables.plans
    query = select(pl.c.label).where(pl.c.project_id == project_id, pl.c.plan_id == plan_id)
    return (await conn.execute(query)).scalar_one_or_none()


async def insert_chat(conn: AsyncConnection, title: str, owner_id: int) -> int:
    c = tables.chats
    return (await conn.execute(insert(c).values(title=title, created_by=owner_id).returning(c.c.id))).scalar_one()


async def insert_triage_chat(conn: AsyncConnection, project_id: int, plan_id: str | None, chat_id: int) -> None:
    await conn.execute(insert(tables.triage_chats).values(project_id=project_id, plan_id=plan_id, chat_id=chat_id))


async def triage_chat_id(conn: AsyncConnection, project_id: int, plan_id: str | None) -> int | None:
    """The triage chat of ``plan_id`` of the project (the project's for None); None for none."""
    tc = tables.triage_chats
    query = select(tc.c.chat_id).where(tc.c.project_id == project_id, tc.c.plan_id.is_not_distinct_from(plan_id))
    return (await conn.execute(query)).scalar_one_or_none()


async def member_ids(conn: AsyncConnection, chat_id: int) -> list[int]:
    """The people in chat ``chat_id`` now: its creator first, then by when they came."""
    c, m = tables.chats, tables.chat_members
    query = (
        select(m.c.user_id)
        .join_from(m, c, c.c.id == m.c.chat_id)
        .where(m.c.chat_id == chat_id, m.c.user_id.is_not(None), m.c.removed_at.is_(None))
        .order_by(case((m.c.user_id == c.c.created_by, 0), else_=1), m.c.added_at, m.c.id)
    )
    return list((await conn.execute(query)).scalars().all())


# The facts of a run


async def cause_inferred(conn: AsyncConnection, run_id: int) -> bool:
    """Whether the hub guessed the failure cause of run ``run_id`` from its error."""
    e = tables.run_events
    guessed = e.c.body["failure_cause_inferred"].astext == "true"
    return bool(
        (await conn.execute(select(exists().where(e.c.run_id == run_id, e.c.kind == "system", guessed)))).scalar()
    )


async def stopped_env_vars(conn: AsyncConnection, run_id: int) -> list[str]:
    """The variables whose accounts the hub stopped run ``run_id`` on (an ``account_event`` stop), by name."""
    e = tables.run_events
    stopped = e.c.body["account_event"].astext == "stop"
    names = select(e.c.body["env_var"].astext).where(e.c.run_id == run_id, e.c.kind == "system", stopped)
    return sorted({name for name in (await conn.execute(names)).scalars() if name})


async def account_rows(conn: AsyncConnection, owner_id: int, project_id: int, env_var: str) -> list:
    """(expires_at, oauth_sealed, paused_at, cooldown_until) of each account of ``owner_id`` for ``env_var`` bound to
    the project."""
    s, a, b = tables.secrets, tables.accounts, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == project_id)
    query = (
        select(s.c.expires_at, a.c.oauth_sealed, a.c.paused_at, a.c.cooldown_until)
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .where(
            s.c.owner_id == owner_id,
            s.c.kind == "env",
            s.c.env_var == env_var,
            s.c.deleted_at.is_(None),
            bound,
        )
    )
    return list((await conn.execute(query)).all())


async def role_of(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    return (
        await conn.execute(select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id))
    ).scalar_one_or_none()


async def lease_row(conn: AsyncConnection, run_id: int):
    """(state, lease_expires_at, finished_at, the worker's last_heartbeat_at, now) of run ``run_id``."""
    r, w = tables.runs, tables.workers
    return (
        await conn.execute(
            select(r.c.state, r.c.lease_expires_at, r.c.finished_at, w.c.last_heartbeat_at, func.now().label("now"))
            .select_from(r.outerjoin(w, w.c.id == r.c.worker_id))
            .where(r.c.id == run_id)
        )
    ).one()


async def last_event(conn: AsyncConnection, run_id: int, *, ended: bool):
    """(seq, at, kind, body) of the last event of run ``run_id``, the hub's move to the end it is in and what it wrote
    with it left out when ``ended``; None for none."""
    e = tables.run_events
    events = select(e.c.seq, e.c.at, e.c.kind, e.c.body).where(e.c.run_id == run_id)
    if ended:
        end = select(func.max(e.c.seq)).where(e.c.run_id == run_id, e.c.kind == "state").scalar_subquery()
        events = events.where(e.c.seq < end)
    tail = (await conn.execute(events.order_by(e.c.seq.desc()).limit(1))).all()
    return tail[0] if tail else None


async def parent_of(conn: AsyncConnection, run_id: int) -> int | None:
    r = tables.runs
    return (await conn.execute(select(r.c.parent_run_id).where(r.c.id == run_id))).scalar_one_or_none()


async def reruns_of(conn: AsyncConnection, run_id: int):
    """(cause, auto_reruns) of the thread of run ``run_id``; None for none."""
    t = tables.run_triage
    return (await conn.execute(select(t.c.cause, t.c.auto_reruns).where(t.c.run_id == run_id))).one_or_none()


async def dispatched_via(conn: AsyncConnection, run_id: int, machine: str) -> str:
    """The credential run ``run_id`` was dispatched with; ``machine`` without one."""
    r = tables.runs
    query = select(func.coalesce(r.c.dispatched_via, machine)).where(r.c.id == run_id)
    return (await conn.execute(query)).scalar_one()


async def workers_of(conn: AsyncConnection, worker_ids) -> list:
    """(id, name, owner_id) of workers ``worker_ids``."""
    w = tables.workers
    return list((await conn.execute(select(w.c.id, w.c.name, w.c.owner_id).where(w.c.id.in_(worker_ids)))).all())


# The runs that stopped moving


def stale_runs():
    """The runs of KINDS running or verifying on a live worker that still heartbeats (within
    ``runs.OFFLINE_AFTER_SECONDS``), of a project with triage on, with no thread yet, whose last event, else start, is
    more than STALE_MINUTES old."""
    r, w, e, p, t = tables.runs, tables.workers, tables.run_events, tables.projects, tables.run_triage
    last = select(e.c.at).where(e.c.run_id == r.c.id).order_by(e.c.seq.desc()).limit(1).scalar_subquery()
    quiet = func.coalesce(last, r.c.started_at, r.c.leased_at)
    return (
        select(r.c.id)
        .join_from(r, w, w.c.id == r.c.worker_id)
        .join(p, p.c.id == r.c.project_id)
        .where(
            r.c.state.in_(STALE_STATES),
            r.c.kind.in_(KINDS),
            p.c.triage_enabled,
            w.c.revoked_at.is_(None),
            w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS),
            quiet < func.now() - timedelta(minutes=STALE_MINUTES),
            ~exists().where(t.c.run_id == r.c.id),
        )
    )


async def stale_ids(conn: AsyncConnection, batch: int) -> list[int]:
    """The first ``batch`` stale runs (``stale_runs``), by id."""
    r = tables.runs
    return list((await conn.execute(stale_runs().order_by(r.c.id).limit(batch))).scalars().all())


async def lock_stale(conn: AsyncConnection, run_id: int) -> bool:
    """Lock run ``run_id`` when it is still stale; whether it is (one another transaction holds is skipped)."""
    r = tables.runs
    query = stale_runs().where(r.c.id == run_id).with_for_update(of=r, skip_locked=True)
    return (await conn.execute(query)).one_or_none() is not None

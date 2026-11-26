"""Decisions in the database: table decisions with the runs they belong to, the inbox an answer goes to (run_inbox),
the notifications that told the person asked (notifications, notification_deliveries, notification_channels), and the
run that resumes a parked one (runs)."""

from __future__ import annotations

from sqlalchemy import BigInteger, and_, case, false, func, insert, literal, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import Select

from evo_agents.hub import runs, tables

# Reading


def asked_person():
    """Who answers a decision, of a query that joins its run: the person it was asked of, or before schema 0115 a
    gate's decision of autonomy the run's principal and any other the run's owner."""
    d, r = tables.decisions, tables.runs
    gate = and_(d.c.category == runs.AUTONOMY_DECISION_CATEGORY, r.c.principal_id.is_not(None))
    return func.coalesce(d.c.asked_of, case((gate, r.c.principal_id), else_=r.c.dispatched_by))


def shown():
    """The columns of Decision but its options as stored, named as its fields: the decision with its project, its
    run's state and owner, who it is asked of, who answered, and the run whose inbox the answer went to (the first
    message naming it)."""
    d, p, r, i = tables.decisions, tables.projects, tables.runs, tables.run_inbox
    owner, answerer, asked = tables.users.alias("o"), tables.users.alias("a"), tables.users.alias("q")
    answer_run = select(i.c.run_id).where(i.c.decision_id == d.c.id).order_by(i.c.id).limit(1).scalar_subquery()
    return select(
        d.c.id,
        p.c.name.label("project"),
        d.c.run_id,
        r.c.state.label("run_state"),
        d.c.plan_id,
        d.c.step_key,
        d.c.category,
        d.c.question,
        d.c.context,
        d.c.options,
        d.c.recommended_reason,
        d.c.reversible,
        d.c.undo,
        d.c.evidence,
        d.c.state,
        owner.c.login.label("owner"),
        asked.c.login.label("asked_of"),
        d.c.answer_option,
        d.c.answer_text,
        answerer.c.login.label("answered_by"),
        d.c.answered_via,
        answer_run.label("answer_run_id"),
        d.c.asked_at,
        d.c.answered_at,
        d.c.delivered_at,
        d.c.held_at,
        d.c.wake_at,
        d.c.wake_on,
        d.c.credential_request,
        d.c.approval,
        d.c.label.label("label_given"),
        p.c.default_label,
        p.c.levels,
    ).select_from(
        d.join(p, p.c.id == d.c.project_id)
        .join(r, r.c.id == d.c.run_id)
        .join(owner, owner.c.id == r.c.dispatched_by)
        .join(asked, asked.c.id == asked_person())
        .outerjoin(answerer, answerer.c.id == d.c.answered_by)
    )


def _readable(plans: list[str], unplanned: bool):
    """Whether the caller reads a decision, of a query that joins its run: the run's plan is one of ``plans``, or the
    run has none and ``unplanned`` (``views.sees_unplanned``), as ``views.readable_run`` reads the run."""
    d, r = tables.decisions, tables.runs
    plan = func.coalesce(d.c.plan_id, r.c.plan_id)
    return or_(plan.in_(plans), and_(plan.is_(None), true() if unplanned else false()))


def listed(
    project_id: int,
    plans: list[str],
    unplanned: bool,
    states: list[str],
    run_id: int | None,
    plan_id: str | None,
    viewer: int,
) -> list:
    """The filters of the decision list, of a query that joins each decision's run: the project's decisions the
    caller reads (``_readable``), in any of ``states`` (any state when empty), of run ``run_id`` and plan ``plan_id``
    when given; a local run's decisions are its owner's alone (``viewer`` is the caller)."""
    d, r = tables.decisions, tables.runs
    where = [
        d.c.project_id == project_id,
        _readable(plans, unplanned),
        or_(r.c.kind != "local", r.c.dispatched_by == viewer),
    ]
    if states:
        where.append(d.c.state.in_(states))
    if run_id is not None:
        where.append(d.c.run_id == run_id)
    if plan_id is not None:
        where.append(d.c.plan_id == plan_id)
    return where


async def one(conn: AsyncConnection, decision_id: int):
    """Decision ``decision_id`` as ``shown`` reads it."""
    return (await conn.execute(shown().where(tables.decisions.c.id == decision_id))).one()


async def page(conn: AsyncConnection, where: list, limit: int, offset: int) -> list:
    """The decisions ``where`` keeps, newest first, ``limit`` of them after the first ``offset``."""
    query = shown().where(*where).order_by(tables.decisions.c.id.desc()).limit(limit).offset(offset)
    return list((await conn.execute(query)).all())


async def count(conn: AsyncConnection, where: list) -> int:
    d, r = tables.decisions, tables.runs
    counted = select(func.count()).select_from(d.join(r, r.c.id == d.c.run_id)).where(*where)
    return (await conn.execute(counted)).scalar_one()


async def run_of(conn: AsyncConnection, project_id: int, decision_id: int):
    """The plan of decision ``decision_id`` of the project (its run's when it names none), its run's kind and owner;
    None without such a decision."""
    d, r = tables.decisions, tables.runs
    query = (
        select(func.coalesce(d.c.plan_id, r.c.plan_id), r.c.kind, r.c.dispatched_by)
        .join_from(d, r, r.c.id == d.c.run_id)
        .where(d.c.id == decision_id, d.c.project_id == project_id)
    )
    return (await conn.execute(query)).one_or_none()


# Asking


async def plan_of(conn: AsyncConnection, project_id: int, plan_id: str):
    """Plan ``plan_id`` of the project as it is now, its body; None once it is gone."""
    pl = tables.plans
    held = select(pl.c.body).where(pl.c.project_id == project_id, pl.c.plan_id == plan_id)
    return (await conn.execute(held)).one_or_none()


async def run_title(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.title).where(r.c.id == run_id))).scalar_one()


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def open_count(conn: AsyncConnection, run_id: int) -> int:
    """How many decisions of run ``run_id`` are open."""
    d = tables.decisions
    still_open = select(func.count()).select_from(d).where(d.c.run_id == run_id, d.c.state == "open")
    return (await conn.execute(still_open)).scalar_one()


async def insert_decision(conn: AsyncConnection, values: dict) -> int:
    """Write a decision of ``values``; its id."""
    d = tables.decisions
    return (await conn.execute(insert(d).values(**values).returning(d.c.id))).scalar_one()


async def label_reach(conn: AsyncConnection, run_id: int):
    """The name and the levels of the project of run ``run_id``, and the level the grant of the run's owner on it
    reaches (``max_level``, None without one)."""
    r, p, g = tables.runs, tables.projects, tables.grants
    query = (
        select(p.c.name, p.c.levels, g.c.max_level)
        .select_from(
            r.join(p, p.c.id == r.c.project_id).outerjoin(
                g, and_(g.c.project_id == r.c.project_id, g.c.user_id == r.c.dispatched_by)
            )
        )
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one()


async def set_context(conn: AsyncConnection, decision_id: int, context: str | None) -> None:
    d = tables.decisions
    await conn.execute(update(d).values(context=context).where(d.c.id == decision_id))


# Answering


async def asker(conn: AsyncConnection, project_id: int, decision_id: int):
    """Of decision ``decision_id`` of the project: its plan and step, the id and login of the person asked, and the
    id and login of its run's owner (``dispatched_by``, ``owner``)."""
    d, r, u, owner = tables.decisions, tables.runs, tables.users, tables.users.alias("owner")
    query = (
        select(d.c.plan_id, d.c.step_key, u.c.id, u.c.login, r.c.dispatched_by, owner.c.login.label("owner"))
        .select_from(
            d.join(r, r.c.id == d.c.run_id)
            .join(u, u.c.id == asked_person())
            .join(owner, owner.c.id == r.c.dispatched_by)
        )
        .where(d.c.id == decision_id, d.c.project_id == project_id)
    )
    return (await conn.execute(query)).one()


async def locked(conn: AsyncConnection, decision_id: int):
    """The state, category, question, options and credential request of decision ``decision_id``, its row locked
    until the transaction ends."""
    d = tables.decisions
    query = select(d.c.state, d.c.category, d.c.question, d.c.options, d.c.credential_request)
    return (await conn.execute(query.where(d.c.id == decision_id).with_for_update())).one()


async def options_of(conn: AsyncConnection, decision_id: int) -> list[dict]:
    d = tables.decisions
    return (await conn.execute(select(d.c.options).where(d.c.id == decision_id))).scalar_one()


async def answered(
    conn: AsyncConnection,
    decision_id: int,
    *,
    option: str | None,
    words: str | None,
    by: int | None,
    via: str | None = None,
    options: list[dict] | None = None,
) -> None:
    """Mark decision ``decision_id`` answered with ``option`` and ``words`` by ``by`` (None for the hub), through the
    channel ``via``; with ``options``, its options become those."""
    d = tables.decisions
    values = {"state": "answered", "answer_option": option, "answer_text": words, "answered_at": func.now()}
    if by is not None:
        values |= {"answered_by": by, "answered_via": via}
    if options is not None:
        values["options"] = options
    await conn.execute(update(d).values(**values).where(d.c.id == decision_id))


async def held(conn: AsyncConnection, decision_id: int) -> None:
    """Decision ``decision_id`` stays open, held now."""
    d = tables.decisions
    await conn.execute(update(d).values(held_at=func.now()).where(d.c.id == decision_id))


async def read_notification(conn: AsyncConnection, decision_id: int, user_id: int) -> None:
    """The notification of decision ``decision_id`` to ``user_id`` read, unless it was already."""
    n = tables.notifications
    await conn.execute(
        update(n)
        .values(read_at=func.now())
        .where(n.c.decision_id == decision_id, n.c.user_id == user_id, n.c.read_at.is_(None))
    )


async def insert_inbox(conn: AsyncConnection, run_id: int, sent_by: int, body: str, decision_id: int) -> int:
    """Leave ``body``, the answer to decision ``decision_id``, in the inbox of run ``run_id``; the message's id."""
    i = tables.run_inbox
    left = insert(i).values(run_id=run_id, sent_by=sent_by, body=body, decision_id=decision_id).returning(i.c.id)
    return (await conn.execute(left)).scalar_one()


async def go_on(conn: AsyncConnection, run_id: int) -> None:
    """Run ``run_id``, a local session that waited, runs again."""
    r = tables.runs
    await conn.execute(update(r).values(state="running", waiting_since=None).where(r.c.id == run_id))


async def label_of(conn: AsyncConnection, decision_id: int):
    """The label of decision ``decision_id``, and the default label and the levels of its project."""
    d, p = tables.decisions, tables.projects
    query = select(d.c.label, p.c.default_label, p.c.levels).join_from(d, p, p.c.id == d.c.project_id)
    return (await conn.execute(query.where(d.c.id == decision_id))).one()


# Channels


async def told(conn: AsyncConnection, decision_ids: list[int]) -> list:
    """The notification of each of the decisions to the person asked: its decision, its id and its user."""
    d, r, n = tables.decisions, tables.runs, tables.notifications
    query = (
        select(n.c.decision_id, n.c.id.label("notification_id"), n.c.user_id)
        .select_from(n.join(d, d.c.id == n.c.decision_id).join(r, r.c.id == d.c.run_id))
        .where(n.c.decision_id.in_(decision_ids), n.c.user_id == asked_person())
    )
    return list((await conn.execute(query)).all())


async def deliveries(conn: AsyncConnection, notification_ids: list[int]) -> list:
    """The deliveries of the notifications to a channel, in order, each with the kind of its channel and its edit."""
    dl, c = tables.notification_deliveries, tables.notification_channels
    edit = dl.alias("edit")
    query = (
        select(
            dl.c.notification_id,
            c.c.kind,
            dl.c.state,
            dl.c.external_id,
            dl.c.last_error,
            dl.c.attempts,
            dl.c.created_at,
            dl.c.delivered_at,
            dl.c.withheld,
            edit.c.state.label("edit_state"),
            edit.c.last_error.label("edit_error"),
            edit.c.delivered_at.label("edit_at"),
        )
        .select_from(dl.join(c, c.c.id == dl.c.channel_id).outerjoin(edit, edit.c.edit_of == dl.c.id))
        .where(dl.c.notification_id.in_(notification_ids), dl.c.edit_of.is_(None))
        .order_by(dl.c.id)
    )
    return list((await conn.execute(query)).all())


async def linked(conn: AsyncConnection, user_ids: set[int], kinds: list[str]) -> list:
    """The channels of ``kinds`` the users linked, with whether each is on."""
    c = tables.notification_channels
    query = select(c.c.user_id, c.c.kind, c.c.enabled).where(c.c.user_id.in_(user_ids), c.c.kind.in_(kinds))
    return list((await conn.execute(query)).all())


# Resuming


class _OverridingSystemValue(Select):
    """The SELECT of an INSERT ... SELECT that gives a value for runs.id, which is GENERATED ALWAYS AS IDENTITY:
    Postgres takes one only after OVERRIDING SYSTEM VALUE, which SQLAlchemy has no construct for, so this select
    renders it in front of itself. Only ``insert_resume`` needs it: it takes the id ``reserve_run_id`` reserved with
    nextval, so the parked run can end done "resumed as #N" before runs_active_plan_key lets the new plan run of its
    plan in."""

    inherit_cache = True


@compiles(_OverridingSystemValue)
def _overriding_system_value(element, compiler, **kw):
    return "OVERRIDING SYSTEM VALUE " + compiler.visit_select(element, **kw)


def _resume_run(new_id: int, parked_id: int):
    """The run that resumes parked run ``parked_id`` (a plan run or an author run, ``runs.RESUME_KINDS``), with id
    ``new_id``, of its kind: pinned to its worker, in its session, with the agent time it used and the credential it
    was dispatched with, at the plan's current revision (the one it was dispatched from when the plan is gone; none for
    an author run that has put no plan); a run the night shift queued keeps its schedule, night and caps, an author
    run its request, a plan run its session per step, and every run its agent profile and version, and whether it runs
    as its owner (``run_access``), with a token of its own."""
    r, pl = tables.runs, tables.plans
    source = _OverridingSystemValue(
        literal(new_id, BigInteger).label("id"),
        r.c.kind,
        r.c.project_id,
        r.c.plan_id,
        r.c.title,
        func.coalesce(pl.c.revision, r.c.plan_revision).label("plan_revision"),
        r.c.dispatched_by,
        r.c.dispatched_via,
        r.c.worker_id.label("pinned_worker_id"),
        r.c.runtime.label("requested_runtime"),
        r.c.runtime,
        r.c.model,
        r.c.mode,
        r.c.approval,
        r.c.timeout_s,
        r.c.max_attempts,
        r.c.repos,
        r.c.id.label("resume_of_run_id"),
        r.c.run_seconds,
        r.c.session_id,
        r.c.schedule_id,
        r.c.schedule_night,
        r.c.budget,
        r.c.request,
        r.c.session_per_step,
        r.c.browser,
        r.c.browser_identities,
        r.c.profile_id,
        r.c.profile_version,
        r.c.principal_id,
        r.c.autonomy,
        r.c.as_owner,
        r.c.as_owner_since,
    )
    source = source.select_from(
        r.outerjoin(pl, (pl.c.project_id == r.c.project_id) & (pl.c.plan_id == r.c.plan_id))
    ).where(r.c.id == parked_id)
    return insert(r).from_select([column.name for column in source.selected_columns], source)


async def run_kind(conn: AsyncConnection, run_id: int) -> str:
    return (await conn.execute(select(tables.runs.c.kind).where(tables.runs.c.id == run_id))).scalar_one()


async def reserve_run_id(conn: AsyncConnection) -> int:
    """The id of a run not made yet, taken from the sequence of runs.id."""
    reserved = select(func.nextval(func.pg_get_serial_sequence("runs", "id")))
    return (await conn.execute(reserved)).scalar_one()


async def insert_resume(conn: AsyncConnection, new_id: int, parked_id: int) -> None:
    """Write run ``new_id``, which resumes parked run ``parked_id`` (``_resume_run``)."""
    await conn.execute(_resume_run(new_id, parked_id))


async def move_open(conn: AsyncConnection, parked_id: int, new_id: int, *, inbox: bool) -> None:
    """The open decisions of run ``parked_id`` go to run ``new_id``, and with ``inbox`` the messages of its inbox the
    worker has not handed to an agent too."""
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    if inbox:
        undelivered = (i.c.run_id == parked_id, i.c.delivered_at.is_(None))
        await conn.execute(update(i).values(run_id=new_id).where(*undelivered))


async def run_of_decision(conn: AsyncConnection, decision_id: int) -> int:
    d = tables.decisions
    return (await conn.execute(select(d.c.run_id).where(d.c.id == decision_id))).scalar_one()


async def lock_run(conn: AsyncConnection, run_id: int):
    """The state and kind of run ``run_id``, its row locked until the transaction ends."""
    r = tables.runs
    return (await conn.execute(select(r.c.state, r.c.kind).where(r.c.id == run_id).with_for_update())).one()

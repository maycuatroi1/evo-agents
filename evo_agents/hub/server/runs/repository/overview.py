"""The queries of the caller's overview (``evo_agents.hub.server.overview``) and of Today
(``evo_agents.hub.server.today``), on SQLAlchemy Core: the visible (project, plan) pairs bound as arrays, the checks a
run or decision of them passes (``of_visible``, ``shown_run``, ``decision_visible``), and the statements the overview
runs, built once with bind parameters (``parameters``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    ColumnElement,
    Date,
    Row,
    Text,
    and_,
    bindparam,
    case,
    cast,
    column,
    func,
    literal,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import author, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.decisions.repository import asked_person
from evo_agents.hub.server.plans.repository import step_counts
from evo_agents.hub.server.runs.models.overview import (
    AUTHOR_WAITING_STATES,
    ENDED_COUNTED,
    IN_FLIGHT,
    MAX_ACTIVE,
    MAX_AUTHOR_WAITING,
    MAX_DECISIONS,
    MAX_RECENT,
    RECENT_STATES,
    RUNNING_STATES,
)


def _utc_day(moment):
    """The day in UTC of the timestamp ``moment``, whatever the session's time zone."""
    return cast(func.timezone("UTC", moment), Date)


def _visible():
    """The CTE visible: the (project_id, plan_id) pairs the caller sees, bound as two arrays of one length,
    :visible_projects and :visible_plans."""
    pair = func.unnest(
        bindparam("visible_projects", type_=ARRAY(BigInteger)),
        bindparam("visible_plans", type_=ARRAY(Text)),
    ).table_valued(column("project_id", BigInteger), column("plan_id", Text))
    found = pair.render_derived()
    return select(found.c.project_id, found.c.plan_id).cte("visible")


def of_visible(visible, table):
    """``table``'s rows of a visible plan: its project_id and plan_id are a pair of ``visible``."""
    return and_(visible.c.project_id == table.c.project_id, visible.c.plan_id == table.c.plan_id)


def shown_run(visible, user_id: ColumnElement):
    """Whether the caller sees a run, of a query that outer joins ``visible`` to it: its plan is a visible one, or it
    is a local run of ``user_id``'s in a project the caller holds a grant on (:granted): a local run is its member's
    alone, whatever its plan."""
    runs_ = tables.runs
    mine = and_(runs_.c.kind == "local", runs_.c.dispatched_by == user_id, one_of(runs_.c.project_id, name="granted"))
    return or_(and_(runs_.c.kind != "local", visible.c.plan_id.is_not(None)), mine)


def decision_visible(visible, unplanned: ColumnElement[bool], user_id: ColumnElement):
    """Whether the caller reads a decision, of a query that joins its run, as it reads the run: the run's plan is a
    visible one, or it has none and ``unplanned`` selects its project (the caller reads its runs on no plan); a local
    run's decisions are its member's alone (``user_id``) in a project the caller holds a grant on (:granted)."""
    decisions, runs_ = tables.decisions, tables.runs
    plan = func.coalesce(decisions.c.plan_id, runs_.c.plan_id)
    seen = (
        select(visible.c.plan_id)
        .where(visible.c.project_id == decisions.c.project_id, visible.c.plan_id == plan)
        .exists()
    )
    mine = and_(runs_.c.dispatched_by == user_id, one_of(decisions.c.project_id, name="granted"))
    return or_(
        and_(runs_.c.kind != "local", or_(seen, and_(plan.is_(None), unplanned))), and_(runs_.c.kind == "local", mine)
    )


def _granted(user_id: ColumnElement):
    """The names of the projects member ``user_id`` holds a grant on, by name."""
    grants, projects = tables.grants, tables.projects
    return (
        select(projects.c.name)
        .join_from(grants, projects, projects.c.id == grants.c.project_id)
        .where(grants.c.user_id == user_id)
        .order_by(projects.c.name)
    )


def _run_counts(visible, since: ColumnElement):
    """The visible runs in flight, and those that ended counted since ``since``, by state and UTC day of their end."""
    runs_ = tables.runs
    day = _utc_day(runs_.c.finished_at)
    return (
        select(runs_.c.state, day.label("day"), func.count().label("count"))
        .join_from(runs_, visible, of_visible(visible, runs_), isouter=True)
        .where(
            shown_run(visible, bindparam("user_id")),
            or_(
                one_of(runs_.c.state, IN_FLIGHT),
                and_(one_of(runs_.c.state, ENDED_COUNTED), runs_.c.finished_at >= since),
            ),
        )
        .group_by(runs_.c.state, day)
    )


def _active_order() -> tuple:
    """The agents at work first, then IN_FLIGHT's order; the newest first within each."""
    runs_ = tables.runs
    rank = case(
        (one_of(runs_.c.state, RUNNING_STATES), 0),
        else_=func.array_position(literal(list(IN_FLIGHT), ARRAY(Text)), runs_.c.state),
    )
    return rank, runs_.c.id.desc()


def _recent_order() -> tuple:
    runs_ = tables.runs
    return runs_.c.finished_at.desc(), runs_.c.id.desc()


def _run_rows(visible, states: tuple[str, ...], order: tuple, limit: int):
    """The first ``limit`` runs by ``order`` in ``states``, of the visible plans, with OverviewRun's columns: picked
    from runs alone, so the joins that name their project, owner, worker and plan run for those few only."""
    runs_, projects, users, workers, plans = tables.runs, tables.projects, tables.users, tables.workers, tables.plans
    picked = (
        select(runs_.c.id)
        .join_from(runs_, visible, of_visible(visible, runs_), isouter=True)
        .where(shown_run(visible, bindparam("user_id")), one_of(runs_.c.state, states))
        .order_by(*order)
        .limit(limit)
        .cte("picked")
    )
    steps_total, steps_done = step_counts(plans.c.body)
    return (
        select(
            runs_.c.id,
            runs_.c.kind,
            projects.c.name.label("project"),
            runs_.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            runs_.c.step_key,
            runs_.c.title,
            runs_.c.state,
            users.c.login.label("dispatched_by"),
            runs_.c.worker_id,
            workers.c.name.label("worker"),
            runs_.c.runtime,
            runs_.c.model,
            steps_total,
            steps_done,
            runs_.c.run_seconds,
            runs_.c.queued_at,
            runs_.c.started_at,
            runs_.c.finished_at,
            runs_.c.error,
            runs_.c.machine,
            runs_.c.cwd,
            runs_.c.claude_email,
            runs_.c.claude_profile,
            runs_.c.last_activity_at,
        )
        .select_from(
            picked.join(runs_, runs_.c.id == picked.c.id)
            .join(projects, projects.c.id == runs_.c.project_id)
            .join(users, users.c.id == runs_.c.dispatched_by)
            .outerjoin(workers, workers.c.id == runs_.c.worker_id)
            .outerjoin(plans, and_(plans.c.project_id == runs_.c.project_id, plans.c.plan_id == runs_.c.plan_id))
        )
        .order_by(*order)
    )


def _open_decisions(visible, user_id: ColumnElement, unplanned: ColumnElement[bool]):
    """The first MAX_DECISIONS open decisions the caller reads (``decision_visible``), those asked of ``user_id``
    first, then the oldest first, with OverviewDecision's columns and what its parks_at is made of."""
    decisions, runs_, projects, users, plans = (
        tables.decisions,
        tables.runs,
        tables.projects,
        tables.users,
        tables.plans,
    )
    asked = tables.users.alias("asked")
    yours = asked_person() == user_id
    return (
        select(
            decisions.c.id,
            projects.c.name.label("project"),
            decisions.c.run_id,
            runs_.c.state.label("run_state"),
            runs_.c.kind.label("run_kind"),
            decisions.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            decisions.c.step_key,
            decisions.c.category,
            decisions.c.question,
            users.c.login.label("owner"),
            asked.c.login.label("asked_of"),
            yours.label("yours"),
            decisions.c.asked_at,
            runs_.c.waiting_since,
            runs_.c.parked_at,
        )
        .select_from(
            decisions.join(runs_, runs_.c.id == decisions.c.run_id)
            .join(projects, projects.c.id == decisions.c.project_id)
            .join(users, users.c.id == runs_.c.dispatched_by)
            .join(asked, asked.c.id == asked_person())
            .outerjoin(
                plans, and_(plans.c.project_id == decisions.c.project_id, plans.c.plan_id == decisions.c.plan_id)
            )
        )
        .where(decisions.c.state == "open", decision_visible(visible, unplanned, user_id))
        .order_by(yours.desc(), decisions.c.asked_at, decisions.c.id)
        .limit(MAX_DECISIONS)
    )


def _author_waiting(visible, user_id, granted: ColumnElement[bool]):
    """The first MAX_AUTHOR_WAITING author runs of ``user_id`` waiting for its reply, in the projects ``granted``
    selects, of a new plan or of a visible one, the latest first, with OverviewAuthorWait's columns."""
    runs_, projects, workers, plans, events = (
        tables.runs,
        tables.projects,
        tables.workers,
        tables.plans,
        tables.run_events,
    )
    said = (
        select(events.c.body["text"].astext)
        .where(
            events.c.run_id == runs_.c.id,
            events.c.kind == "system",
            events.c.body["chat"].astext == author.CHAT_AGENT,
        )
        .order_by(events.c.seq.desc())
        .limit(1)
        .correlate(runs_)
        .scalar_subquery()
    )
    seen = select(visible.c.plan_id).where(of_visible(visible, runs_)).exists()
    decisions = tables.decisions
    asks = select(decisions.c.id).where(decisions.c.run_id == runs_.c.id, decisions.c.state == "open").exists()
    return (
        select(
            runs_.c.id,
            projects.c.name.label("project"),
            runs_.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            runs_.c.title,
            runs_.c.state,
            workers.c.name.label("worker"),
            runs_.c.waiting_since,
            runs_.c.parked_at,
            said.label("message"),
        )
        .select_from(
            runs_.join(projects, projects.c.id == runs_.c.project_id)
            .outerjoin(workers, workers.c.id == runs_.c.worker_id)
            .outerjoin(plans, and_(plans.c.project_id == runs_.c.project_id, plans.c.plan_id == runs_.c.plan_id))
        )
        .where(
            runs_.c.kind == "author",
            runs_.c.dispatched_by == user_id,
            one_of(runs_.c.state, AUTHOR_WAITING_STATES),
            granted,
            or_(runs_.c.plan_id.is_(None), seen),
            ~asks,  # open_decisions lists it through its decision
        )
        .order_by(runs_.c.id.desc())
        .limit(MAX_AUTHOR_WAITING)
    )


def _decision_counts(visible, user_id: ColumnElement, unplanned: ColumnElement[bool]):
    """For each project, its open decisions the caller reads (``decision_visible``), and how many of those are asked
    of ``user_id``. A wait (``runs.WAIT_DECISION_CATEGORY``) asks nobody anything, so it is not one the caller
    answers: it counts in the total, not in the caller's."""
    decisions, runs_ = tables.decisions, tables.runs
    asks = decisions.c.category != runs.WAIT_DECISION_CATEGORY
    return (
        select(
            decisions.c.project_id,
            func.count().label("total"),
            func.count().filter(asked_person() == user_id, asks).label("yours"),
        )
        .select_from(decisions.join(runs_, runs_.c.id == decisions.c.run_id))
        .where(decisions.c.state == "open", decision_visible(visible, unplanned, user_id))
        .group_by(decisions.c.project_id)
    )


def _project_counts(visible, granted: ColumnElement[bool]):
    """For each project ``granted`` selects, its repos and its active plans that are visible."""
    projects, repos, plans = tables.projects, tables.project_repos, tables.plans
    repo_count = select(func.count()).where(repos.c.project_id == projects.c.id).scalar_subquery()
    plan_count = (
        select(func.count())
        .select_from(plans.join(visible, of_visible(visible, plans)))
        .where(plans.c.project_id == projects.c.id, plans.c.area == "active")
        .scalar_subquery()
    )
    return select(projects.c.id, repo_count.label("repos"), plan_count.label("plans")).where(granted)


def _running_by_member(visible):
    """The visible runs in RUNNING_STATES, counted by the login of their owner and their project."""
    runs_, users, projects = tables.runs, tables.users, tables.projects
    return (
        select(users.c.login, projects.c.name.label("project"), func.count().label("running"))
        .join_from(runs_, visible, of_visible(visible, runs_), isouter=True)
        .join(users, users.c.id == runs_.c.dispatched_by)
        .join(projects, projects.c.id == runs_.c.project_id)
        .where(shown_run(visible, bindparam("user_id")), one_of(runs_.c.state, RUNNING_STATES))
        .group_by(users.c.login, projects.c.name)
        .order_by(users.c.login, projects.c.name)
    )


# Built once with bind parameters: an overview runs them all, and building them on each request cost more than
# their round trips (docs/hub.md, Data access). Each takes the parameters of ``parameters``.
VISIBLE = _visible()
_TODAY = select(_utc_day(func.now()))
_GRANTED = _granted(bindparam("user_id"))
_RUN_COUNTS = _run_counts(VISIBLE, bindparam("since"))
_ACTIVE_RUNS = _run_rows(VISIBLE, IN_FLIGHT, _active_order(), MAX_ACTIVE)
_RECENT_RUNS = _run_rows(VISIBLE, RECENT_STATES, _recent_order(), MAX_RECENT)
UNPLANNED = one_of(tables.decisions.c.project_id, name="unplanned")
_OPEN_DECISIONS = _open_decisions(VISIBLE, bindparam("user_id"), UNPLANNED)
_DECISION_COUNTS = _decision_counts(VISIBLE, bindparam("user_id"), UNPLANNED)
_PROJECT_COUNTS = _project_counts(VISIBLE, one_of(tables.projects.c.id, name="granted"))
_RUNNING_BY_MEMBER = _running_by_member(VISIBLE)
_AUTHOR_WAITING = _author_waiting(VISIBLE, bindparam("user_id"), one_of(tables.runs.c.project_id, name="granted"))


def parameters(
    user_id: int, pairs: list[tuple[int, str]], granted: list[int], unplanned: list[int], since: datetime
) -> dict:
    """The parameters of the statements above: the visible (project_id, plan_id) ``pairs``, the projects
    ``granted``, those of them whose runs on no plan the caller sees (``unplanned``), the caller and the start of the
    counted days."""
    return {
        "visible_projects": [project_id for project_id, _ in pairs],
        "visible_plans": [plan_id for _, plan_id in pairs],
        "granted": granted,
        "unplanned": unplanned,
        "user_id": user_id,
        "since": since,
    }


@dataclass(frozen=True)
class OverviewRows:
    """The rows of one overview, each statement's."""

    counted: list[Row]
    active: list[Row]
    recent: list[Row]
    decisions: list[Row]
    decision_counts: list[Row]
    project_counts: list[Row]
    author_waiting: list[Row]


async def utc_today(conn: AsyncConnection) -> date:
    """The database's today in UTC."""
    return (await conn.execute(_TODAY)).scalar_one()


async def granted_names(conn: AsyncConnection, user_id: int) -> list[str]:
    """The names of the projects member ``user_id`` holds a grant on, by name."""
    return list((await conn.execute(_GRANTED, {"user_id": user_id})).scalars().all())


async def overview_rows(conn: AsyncConnection, bound: dict) -> OverviewRows:
    """The rows of the overview's statements, with the parameters ``bound`` (``parameters``)."""
    return OverviewRows(
        counted=list((await conn.execute(_RUN_COUNTS, bound)).all()),
        active=list((await conn.execute(_ACTIVE_RUNS, bound)).all()),
        recent=list((await conn.execute(_RECENT_RUNS, bound)).all()),
        decisions=list((await conn.execute(_OPEN_DECISIONS, bound)).all()),
        decision_counts=list((await conn.execute(_DECISION_COUNTS, bound)).all()),
        project_counts=list((await conn.execute(_PROJECT_COUNTS, bound)).all()),
        author_waiting=list((await conn.execute(_AUTHOR_WAITING, bound)).all()),
    )


async def running_rows(conn: AsyncConnection, bound: dict) -> list[Row]:
    """The visible runs in RUNNING_STATES, counted by the login of their owner and their project."""
    return list((await conn.execute(_RUNNING_BY_MEMBER, bound)).all())

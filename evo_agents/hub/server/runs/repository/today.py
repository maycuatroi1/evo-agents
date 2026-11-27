"""The queries of Today (``evo_agents.hub.server.today``), on SQLAlchemy Core: what waits for the caller, each
source of the window's items, and the caller's seen mark. The statements are built once with bind parameters, as the
overview's (``runs.repository.overview``); each takes ``overview.parameters`` and :now."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Row, Text, and_, bindparam, case, cast, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import JSONPATH, aggregate_order_by, array_agg
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import problems, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.decisions.repository import asked_person
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.notifications.repository.listing import report_notice
from evo_agents.hub.server.runs.models.today import DONE_IDS, FAILED, MAX_ITEMS, MAX_NEEDS_YOU, MAX_PROPOSALS
from evo_agents.hub.server.runs.repository.overview import UNPLANNED, VISIBLE, decision_visible, of_visible, shown_run


def _needs_you(visible, user_id, unplanned):
    """The first MAX_NEEDS_YOU open decisions asked of ``user_id`` that the caller reads (``decision_visible``), the
    oldest first, with TodayDecision's columns, what its parks_at is made of, and how many there are in all."""
    d, r, projects, plans = tables.decisions, tables.runs, tables.projects, tables.plans
    return (
        select(
            d.c.id,
            projects.c.name.label("project"),
            d.c.run_id,
            r.c.state.label("run_state"),
            r.c.kind.label("run_kind"),
            d.c.plan_id,
            plans.c.body["title"].astext.label("plan_title"),
            d.c.step_key,
            d.c.category,
            d.c.question,
            d.c.asked_at,
            r.c.waiting_since,
            r.c.parked_at,
            func.count().over().label("total"),
        )
        .select_from(
            d.join(r, r.c.id == d.c.run_id)
            .join(projects, projects.c.id == d.c.project_id)
            .outerjoin(plans, and_(plans.c.project_id == d.c.project_id, plans.c.plan_id == d.c.plan_id))
        )
        .where(d.c.state == "open", asked_person() == user_id, decision_visible(visible, unplanned, user_id))
        .order_by(d.c.asked_at, d.c.id)
        .limit(MAX_NEEDS_YOU)
    )


def _in_window(moment):
    """``moment`` falls in the window: after :since, and not after :now."""
    return and_(moment > bindparam("since"), moment <= bindparam("now"))


def _releases():
    """The release cycles of the projects :granted that were published in the window, with the tag that passed."""
    c, s, t, projects = tables.release_cycles, tables.release_schedules, tables.release_tags, tables.projects
    tag = (
        select(t.c.tag)
        .where(t.c.cycle_id == c.c.id, t.c.ci_state == "passed")
        .order_by(t.c.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(c.c.id, projects.c.name.label("project"), s.c.repo, tag.label("tag"), c.c.ended_at.label("at"))
        .join_from(c, s, s.c.id == c.c.schedule_id)
        .join(projects, projects.c.id == c.c.project_id)
        .where(one_of(c.c.project_id, name="granted"), c.c.state == "published", _in_window(c.c.ended_at))
        .order_by(c.c.ended_at, c.c.id)
        .limit(MAX_ITEMS + 1)
    )


def _runs(visible):
    """The runs of :user_id the caller sees that failed, were lost or parked in the window, but the night shift's."""
    r, projects = tables.runs, tables.projects
    user_id = bindparam("user_id")
    at = case((r.c.state == "parked", r.c.parked_at), else_=r.c.finished_at)
    failed = and_(one_of(r.c.state, FAILED), _in_window(r.c.finished_at))
    parked = and_(r.c.state == "parked", _in_window(r.c.parked_at))
    return (
        select(
            r.c.id,
            projects.c.name.label("project"),
            r.c.state,
            r.c.title,
            r.c.plan_id,
            r.c.error,
            at.label("at"),
        )
        .join_from(r, visible, of_visible(visible, r), isouter=True)
        .join(projects, projects.c.id == r.c.project_id)
        .where(
            shown_run(visible, user_id),
            r.c.dispatched_by == user_id,
            ~one_of(r.c.kind, runs.CURATOR_KINDS),
            or_(failed, parked),
        )
        .order_by(at, r.c.id)
        .limit(MAX_ITEMS + 1)
    )


def _costs(visible):
    """What the runs of :user_id the caller sees that ended in the window cost, a project at a time (each agent
    session once, at its largest total), with how many ended and when the last did; projects of no cost left out."""
    r, projects = tables.runs, tables.projects
    user_id = bindparam("user_id")
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(
            r.c.project_id,
            func.max(usage_cost(r.c.usage)).label("cost"),
            func.max(r.c.finished_at).label("at"),
            func.count().label("runs"),
        )
        .join_from(r, visible, of_visible(visible, r), isouter=True)
        .where(shown_run(visible, user_id), r.c.dispatched_by == user_id, _in_window(r.c.finished_at))
        .group_by(r.c.project_id, session)
        .subquery()
    )
    total = func.sum(per_session.c.cost)
    return (
        select(
            projects.c.name.label("project"),
            total.label("cost"),
            func.max(per_session.c.at).label("at"),
            func.sum(per_session.c.runs).label("runs"),
        )
        .join_from(per_session, projects, projects.c.id == per_session.c.project_id)
        .group_by(projects.c.name)
        .having(total > 0)
        .order_by(func.max(per_session.c.at))
        .limit(MAX_ITEMS + 1)
    )


def _reports():
    """The notices that only report which :user_id got in the window in the projects :granted, a project and kind of
    notice at a time: how many, when the newest came, and its title, body and link."""
    n, projects = tables.notifications, tables.projects
    newest = (n.c.created_at.desc(), n.c.id.desc())

    def latest(column):
        return array_agg(aggregate_order_by(column, *newest))[1]

    return (
        select(
            projects.c.name.label("project"),
            n.c.notice_kind,
            func.count().label("count"),
            func.max(n.c.created_at).label("at"),
            latest(n.c.title).label("title"),
            latest(n.c.body).label("body"),
            latest(n.c.link).label("link"),
        )
        .join_from(n, projects, projects.c.id == n.c.project_id)
        .where(
            n.c.user_id == bindparam("user_id"),
            report_notice(n),
            one_of(n.c.project_id, name="granted"),
            _in_window(n.c.created_at),
        )
        .group_by(projects.c.name, n.c.notice_kind)
        .order_by(func.max(n.c.created_at), projects.c.name, n.c.notice_kind)
        .limit(MAX_ITEMS + 1)
    )


def _changed_plans(visible):
    """The visible plans changed in the window: their project, title and steps."""
    p, projects = tables.plans, tables.projects
    return (
        select(
            p.c.project_id,
            projects.c.name.label("project"),
            p.c.plan_id,
            p.c.body["title"].astext.label("title"),
            p.c.body["steps"].label("steps"),
        )
        .join_from(p, visible, of_visible(visible, p))
        .join(projects, projects.c.id == p.c.project_id)
        .where(_in_window(p.c.updated_at))
    )


def _revisions(keys: list[tuple[int, str]]):
    """The revisions of the plans ``keys`` ((project_id, plan_id) each) the window needs, by plan and revision: the
    last one at or before :since, then each one after it, each with the ids of its steps that are done."""
    r = tables.plan_revisions
    earlier = r.alias("earlier")
    base = (
        select(func.max(earlier.c.revision))
        .where(
            earlier.c.project_id == r.c.project_id,
            earlier.c.plan_id == r.c.plan_id,
            earlier.c.created_at <= bindparam("since"),
        )
        .scalar_subquery()
    )
    done = func.jsonb_path_query_array(r.c.body, cast(literal(DONE_IDS), JSONPATH))
    return (
        select(r.c.project_id, r.c.plan_id, r.c.revision, r.c.created_at, done.label("done"))
        .where(
            tuple_(r.c.project_id, r.c.plan_id).in_(keys),
            or_(_in_window(r.c.created_at), r.c.revision == base),
        )
        .order_by(r.c.project_id, r.c.plan_id, r.c.revision)
    )


def _owned_problems():
    """The open problems of the projects :granted that :user_id owns and that name a metric, at their newest
    revision."""
    p, rv, projects = tables.problems, tables.problem_revisions, tables.projects
    return (
        select(p.c.project_id, projects.c.name.label("project"), p.c.number, rv.c.statement, rv.c.metric)
        .join_from(p, rv, and_(rv.c.problem_id == p.c.id, rv.c.revision == p.c.revision))
        .join(projects, projects.c.id == p.c.project_id)
        .where(
            one_of(p.c.project_id, name="granted"),
            rv.c.owner_id == bindparam("user_id"),
            rv.c.status == problems.OPEN,
            rv.c.metric.is_not(None),
        )
        .order_by(projects.c.name, p.c.number)
    )


def _last_measured(project_id: int, metric: dict, now: datetime):
    """When the key of ``metric`` was last measured up to ``now``, of its tenant when it names one."""
    k, s = tables.signal_keys, tables.signals
    key = select(k.c.id).where(k.c.project_id == project_id, k.c.name == metric.get("key")).scalar_subquery()
    where = [s.c.key_id == key, s.c.value.is_not(None), s.c.measured_at <= now]
    if metric.get("tenant"):
        where.append(s.c.tenant == metric["tenant"])
    return select(func.max(s.c.measured_at)).where(*where)


# Built once with bind parameters, as the overview's: each takes ``overview.parameters`` and :now.
_NEEDS_YOU = _needs_you(VISIBLE, bindparam("user_id"), UNPLANNED)
_RELEASES = _releases()
_RUNS = _runs(VISIBLE)
_COSTS = _costs(VISIBLE)
_REPORTS = _reports()
_CHANGED_PLANS = _changed_plans(VISIBLE)
_OWNED_PROBLEMS = _owned_problems()
_NOW = select(func.now())
_SOURCES = {"release": _RELEASES, "run": _RUNS, "cost": _COSTS, "report": _REPORTS}


async def now(conn: AsyncConnection) -> datetime:
    """The database's now."""
    return (await conn.execute(_NOW)).scalar_one()


async def seen_mark(conn: AsyncConnection, user_id: int) -> datetime | None:
    t = tables.today_marks
    return (await conn.execute(select(t.c.seen_at).where(t.c.user_id == user_id))).scalar_one_or_none()


async def needs_you(conn: AsyncConnection, bound: dict) -> list[Row]:
    """The open decisions asked of the caller, with how many there are in all (``total``)."""
    return list((await conn.execute(_NEEDS_YOU, bound)).all())


async def source(conn: AsyncConnection, name: str, bound: dict) -> list[Row]:
    """The rows of one source of items read by a single statement: release, run, cost or report."""
    return list((await conn.execute(_SOURCES[name], bound)).all())


async def changed_plans(conn: AsyncConnection, bound: dict) -> list[Row]:
    return list((await conn.execute(_CHANGED_PLANS, bound)).all())


async def revisions(conn: AsyncConnection, keys: list[tuple[int, str]], bound: dict) -> list[Row]:
    """The revisions of the plans ``keys`` the window needs (``_revisions``)."""
    return list((await conn.execute(_revisions(keys), {"since": bound["since"], "now": bound["now"]})).all())


async def owned_problems(conn: AsyncConnection, bound: dict) -> list[Row]:
    return list((await conn.execute(_OWNED_PROBLEMS, bound)).all())


async def last_measured(conn: AsyncConnection, project_id: int, metric: dict, at: datetime) -> datetime | None:
    return (await conn.execute(_last_measured(project_id, metric, at))).scalar_one_or_none()


async def proposals(conn: AsyncConnection, project_ids: list[int], bound: dict) -> list[Row]:
    """The open proposals of the projects ``project_ids`` recorded in the window, at most MAX_PROPOSALS."""
    p = tables.proposals
    query = (
        select(p.c.id, p.c.project_id, p.c.title, p.c.tier, p.c.evidence_count, p.c.created_at, p.c.label)
        .where(one_of(p.c.project_id, project_ids), p.c.state == "open", _in_window(p.c.created_at))
        .order_by(p.c.created_at, p.c.id)
        .limit(MAX_PROPOSALS)
    )
    return list((await conn.execute(query, {"since": bound["since"], "now": bound["now"]})).all())


async def mark_seen(conn: AsyncConnection, user_id: int, seen_at: datetime | None) -> datetime:
    """Move member ``user_id``'s seen mark forward to ``seen_at`` (now when None, never past now); the mark now."""
    t = tables.today_marks
    at = func.now() if seen_at is None else func.least(literal(seen_at, DateTime(timezone=True)), func.now())
    moved = pg_insert(t).values(user_id=user_id, seen_at=at)
    moved = moved.on_conflict_do_update(
        constraint="today_marks_pkey",
        set_={"seen_at": func.greatest(t.c.seen_at, moved.excluded.seen_at), "updated_at": func.now()},
    )
    return (await conn.execute(moved.returning(t.c.seen_at))).scalar_one()

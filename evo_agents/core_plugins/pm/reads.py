"""Reading the facts of a project from the hub's tables (``tables.tasks``, ``tables.srs``, runs, decisions, plans and
their revisions): the tasks the daily report is made of, and what each shows of the work on it. pm owns no table and
imports neither tasks nor srs; it reads the tables the way tasks reads those of srs.

A task, a use case and a plan carry a label, and a reader sees them through the sink X-Evo-Sink names, else the
project's hub sink, as the routes of tasks show them. What the reader may not see is still read, with ``visible``
false, so the figures count it and the lists leave it out. The activity of a plan the reader may not see counts for
nothing: it keeps no task alive in their report.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.pm import settings
from evo_agents.core_plugins.pm.facts import (
    DECISION,
    MANUAL,
    Blocker,
    Config,
    Event,
    Inputs,
    Member,
    Sprint,
    Task,
    UseCase,
)
from evo_agents.hub import tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else None)
    return through is not None and access.visible(label, through)


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise settings.PmError(403, f"reading the daily report of project {access.name} needs a grant on it")


async def _rows(conn: AsyncConnection, project_id: int):
    t, s = tables.tasks.items, tables.tasks.sprints
    query = (
        select(
            t.c.id,
            t.c.number,
            t.c.title,
            t.c.status,
            t.c.due_on,
            t.c.description_md,
            t.c.outcome_md,
            t.c.label,
            t.c.external_key,
            t.c.created_at,
            s.c.name.label("sprint"),
        )
        .select_from(t.outerjoin(s, s.c.id == t.c.sprint_id))
        .where(t.c.project_id == project_id)
        .order_by(t.c.number)
    )
    return list((await conn.execute(query)).all())


async def _people(conn: AsyncConnection, ids: list[int]) -> dict[int, dict[str, list[str]]]:
    p, u = tables.tasks.people, tables.users
    query = (
        select(p.c.task_id, p.c.role, u.c.login)
        .join_from(p, u, u.c.id == p.c.user_id)
        .where(p.c.task_id.in_(ids), p.c.role.in_(("assignee", "qa")))
        .order_by(p.c.task_id, u.c.login)
    )
    found: dict[int, dict[str, list[str]]] = {}
    for row in await conn.execute(query):
        found.setdefault(row.task_id, {"assignee": [], "qa": []})[row.role].append(row.login)
    return found


async def _events(conn: AsyncConnection, ids: list[int]) -> dict[int, list[Event]]:
    e = tables.tasks.events
    query = (
        select(e.c.task_id, e.c.field, e.c.from_value, e.c.to_value, e.c.at)
        .where(e.c.task_id.in_(ids))
        .order_by(e.c.id)
    )
    found: dict[int, list[Event]] = {}
    for row in await conn.execute(query):
        found.setdefault(row.task_id, []).append(Event(row.field, row.from_value, row.to_value, row.at))
    return found


async def _commented(conn: AsyncConnection, ids: list[int]) -> dict[int, datetime]:
    c = tables.tasks.comments
    query = select(c.c.task_id, func.max(c.c.created_at).label("at")).where(c.c.task_id.in_(ids)).group_by(c.c.task_id)
    return {row.task_id: row.at for row in await conn.execute(query)}


async def _runs(conn: AsyncConnection, ids: list[int]) -> dict[int, datetime]:
    """task id -> the latest time of a run that belongs to it: one it links, or one of a plan step it links."""
    k, r = tables.tasks.links, tables.runs
    moved = func.greatest(r.c.queued_at, r.c.leased_at, r.c.started_at, r.c.finished_at)
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    query = (
        select(k.c.task_id, func.max(moved).label("at"))
        .select_from(k.join(r, or_(by_run, by_step)))
        .where(k.c.task_id.in_(ids))
        .group_by(k.c.task_id)
    )
    return {row.task_id: row.at for row in await conn.execute(query)}


def _status_of(body: dict, step: str) -> str | None:
    steps = body.get("steps") if isinstance(body.get("steps"), list) else []
    for index, item in enumerate(steps):
        if step_key(item, index) == step and isinstance(item, dict):
            return item.get("status")
    return None


async def _revisions(conn: AsyncConnection, project_id: int, plans: list[str], since: datetime) -> dict[str, list]:
    """plan id -> its revisions made since ``since`` and the one before them, oldest first."""
    rv = tables.plan_revisions
    query = (
        select(rv.c.plan_id, rv.c.revision, rv.c.created_at, rv.c.body)
        .where(rv.c.project_id == project_id, rv.c.plan_id.in_(plans), rv.c.created_at >= since)
        .order_by(rv.c.plan_id, rv.c.revision)
    )
    found: dict[str, list] = {}
    for row in await conn.execute(query):
        found.setdefault(row.plan_id, []).append(row)
    for plan_id, rows in list(found.items()):
        before = await conn.execute(
            select(rv.c.plan_id, rv.c.revision, rv.c.created_at, rv.c.body).where(
                rv.c.project_id == project_id, rv.c.plan_id == plan_id, rv.c.revision == rows[0].revision - 1
            )
        )
        found[plan_id] = [*before.all(), *rows]
    return found


async def _plan_steps(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, ids: list[int], since: datetime
) -> dict[int, datetime]:
    """task id -> the latest time the status of a plan step it links changed, in a plan the reader sees."""
    k, p = tables.tasks.links, tables.plans
    links = list(
        await conn.execute(
            select(k.c.task_id, k.c.plan_id, k.c.step_id).where(k.c.task_id.in_(ids), k.c.kind == "plan_step")
        )
    )
    if not links:
        return {}
    wanted = sorted({link.plan_id for link in links})
    labels = {
        row.plan_id: row.label
        for row in await conn.execute(
            select(p.c.plan_id, p.c.label).where(p.c.project_id == access.project_id, p.c.plan_id.in_(wanted))
        )
    }
    seen = [plan_id for plan_id, label in labels.items() if visible(access, label, sink)]
    revisions = await _revisions(conn, access.project_id, seen, since)
    found: dict[int, datetime] = {}
    for link in links:
        rows = revisions.get(link.plan_id, [])
        for before, after in zip(rows, rows[1:], strict=False):
            if _status_of(before.body, link.step_id) != _status_of(after.body, link.step_id):
                found[link.task_id] = max(found.get(link.task_id, after.created_at), after.created_at)
    return found


async def _blockers(conn: AsyncConnection, prefix: str, ids: list[int]) -> dict[int, list[Blocker]]:
    """task id -> its open blockers: those set by hand, then the open decisions of its runs, as tasks shows them."""
    b, u, d, r, k = tables.tasks.blockers, tables.users, tables.decisions, tables.runs, tables.tasks.links
    setter, waiter, other = u.alias("setter"), u.alias("waiter"), tables.tasks.items.alias("other")
    manual = (
        select(
            b.c.id,
            b.c.task_id,
            b.c.reason,
            b.c.since,
            b.c.waiting_text,
            setter.c.login.label("set_by"),
            waiter.c.login.label("waiting_login"),
            other.c.number.label("waiting_number"),
        )
        .select_from(
            b.join(setter, setter.c.id == b.c.created_by)
            .outerjoin(waiter, waiter.c.id == b.c.waiting_user_id)
            .outerjoin(other, other.c.id == b.c.waiting_task_id)
        )
        .where(b.c.task_id.in_(ids), b.c.cleared_at.is_(None))
        .order_by(b.c.since, b.c.id)
    )
    found: dict[int, list[Blocker]] = {}
    for row in await conn.execute(manual):
        if row.waiting_login is not None:
            kind, on = "member", row.waiting_login
        elif row.waiting_number is not None:
            kind, on = "task", f"{prefix}-{row.waiting_number}"
        else:
            kind, on = "text", row.waiting_text
        found.setdefault(row.task_id, []).append(
            Blocker(MANUAL, row.reason, kind, on, row.since, row.set_by, None, None, row.id)
        )
    by_run = and_(k.c.kind == "run", k.c.run_id == r.c.id)
    by_step = and_(k.c.kind == "plan_step", k.c.plan_id == r.c.plan_id, k.c.step_id == r.c.step_key)
    asked = (
        select(k.c.task_id, d.c.id.label("decision_id"), d.c.run_id, d.c.asked_at, d.c.question, u.c.login)
        .select_from(d.join(r, r.c.id == d.c.run_id).join(k, or_(by_run, by_step)).join(u, u.c.id == r.c.dispatched_by))
        .where(d.c.state == "open", k.c.task_id.in_(ids))
        .distinct()
        .order_by(d.c.asked_at, d.c.id)
    )
    for row in await conn.execute(asked):
        view = Blocker(DECISION, row.question, "member", row.login, row.asked_at, None, row.decision_id, row.run_id)
        found.setdefault(row.task_id, []).append(view)
    return found


async def _use_cases(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, ids: list[int]
) -> dict[int, list[UseCase]]:
    """task id -> the use cases of the plugin srs it links to."""
    k, i, doc, iv, v = (
        tables.tasks.links,
        tables.srs.items,
        tables.srs.documents,
        tables.srs.item_versions,
        tables.srs.versions,
    )
    title = (
        select(iv.c.title)
        .join(v, v.c.id == iv.c.version_id)
        .where(iv.c.item_id == i.c.id)
        .order_by(v.c.number.desc())
        .limit(1)
        .scalar_subquery()
    )
    query = (
        select(k.c.task_id, i.c.code, doc.c.label, title.label("title"))
        .select_from(k.join(i, i.c.id == k.c.srs_item_id).join(doc, doc.c.id == i.c.document_id))
        .where(k.c.task_id.in_(ids), k.c.kind == "srs_item", i.c.kind == "use_case", i.c.retired_in.is_(None))
        .order_by(k.c.task_id, k.c.id)
    )
    found: dict[int, list[UseCase]] = {}
    for row in await conn.execute(query):
        if row.code:
            case = UseCase(row.code, row.title or "", visible(access, row.label, sink))
            found.setdefault(row.task_id, []).append(case)
    return found


async def _members(conn: AsyncConnection, project_id: int) -> tuple[Member, ...]:
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, g.c.role)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == project_id)
        .order_by(u.c.login)
    )
    return tuple(Member(row.login, row.role) for row in await conn.execute(query))


async def _sprint(conn: AsyncConnection, project_id: int) -> Sprint | None:
    s = tables.tasks.sprints
    query = select(s.c.name, s.c.created_at, s.c.starts_on, s.c.ends_on, s.c.activated_at).where(
        s.c.project_id == project_id, s.c.state == "active"
    )
    row = (await conn.execute(query)).first()
    return Sprint(row.name, row.created_at, row.starts_on, row.ends_on, row.activated_at) if row else None


def _earlier(config: Config, today: date) -> datetime:
    """The start of the working day ``stale_days`` + 1 working days before ``today``: what plan revisions are read from."""
    day = today
    for _ in range(config.stale_days + 1):
        day = config.calendar.previous(day)
    return config.calendar.start_of(day)


async def load(conn: AsyncConnection, access: ProjectAccess, sink: str | None, config: Config, today: date) -> Inputs:
    """The facts of the project for the report of ``today``, as the reader sees them."""
    reader(access)
    prefix, groups = await settings.task_groups(conn, access.project_id, access.name)
    rows = await _rows(conn, access.project_id)
    ids = [row.id for row in rows]
    people, events = await _people(conn, ids), await _events(conn, ids)
    commented, runs = await _commented(conn, ids), await _runs(conn, ids)
    steps = await _plan_steps(conn, access, sink, ids, _earlier(config, today))
    blockers = await _blockers(conn, prefix, ids)
    cases = await _use_cases(conn, access, sink, ids) if await settings.srs_enabled(conn, access.project_id) else {}
    tasks = []
    for row in rows:
        held = people.get(row.id, {"assignee": [], "qa": []})
        activity = {"comment": commented.get(row.id), "run": runs.get(row.id), "plan_step": steps.get(row.id)}
        tasks.append(
            Task(
                ref=f"{prefix}-{row.number}",
                number=row.number,
                title=row.title,
                status=row.status,
                group=groups.get(row.status, "todo"),
                created_at=row.created_at,
                due_on=row.due_on,
                sprint=row.sprint,
                description_md=row.description_md,
                outcome_md=row.outcome_md,
                frame=(row.external_key or "").startswith(FRAME_TASK_PREFIX),
                visible=visible(access, row.label, sink),
                assignees=tuple(held["assignee"]),
                qa=tuple(held["qa"]),
                events=tuple(events.get(row.id, ())),
                activity={source: at for source, at in activity.items() if at is not None},
                blockers=tuple(blockers.get(row.id, ())),
                use_cases=tuple(cases.get(row.id, ())),
            )
        )
    return Inputs(
        access.name,
        config,
        groups,
        tuple(tasks),
        await _sprint(conn, access.project_id),
        await _members(conn, access.project_id),
    )


__all__ = ["SINK_HEADER", "load", "reader", "visible"]

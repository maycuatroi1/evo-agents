"""A member's notifications in SQL: the FROM and the columns every view of them reads (``shown``,
``notification_rows``), the notices that only report (``report_notice``), the list's filters, the bell's count and
the mark of a read."""

from __future__ import annotations

from sqlalchemy import and_, exists, false, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server.notifications.models import Notification, notifications_of
from evo_agents.hub.server.ops.repository import views as ops_views


def report_notice(stored=None):
    """The SQL of ``runs.report_only`` over ``stored`` (tables.notifications when None)."""
    stored = tables.notifications if stored is None else stored
    return and_(
        stored.c.kind == "notice",
        stored.c.notice_kind.in_(runs.REPORT_NOTICE_KINDS),
        stored.c.details[runs.REPORT_ASK].astext.is_(None),
    )


def shown():
    """Notifications with their project's name, their decision's state and their proposal's: the FROM of the member's
    routes."""
    stored, projects, decisions, proposals = tables.notifications, tables.projects, tables.decisions, tables.proposals
    return (
        stored.outerjoin(projects, projects.c.id == stored.c.project_id)
        .outerjoin(decisions, decisions.c.id == stored.c.decision_id)
        .outerjoin(proposals, proposals.c.id == stored.c.proposal_id)
    )


def notification_rows():
    """The columns of Notification, by its field names, over ``_shown``."""
    stored = tables.notifications
    return select(
        stored.c.id,
        stored.c.kind,
        stored.c.notice_kind,
        tables.projects.c.name.label("project"),
        stored.c.run_id,
        stored.c.decision_id,
        tables.decisions.c.state.label("decision_state"),
        stored.c.proposal_id,
        tables.proposals.c.state.label("proposal_state"),
        stored.c.title,
        stored.c.body,
        stored.c.details,
        stored.c.link,
        stored.c.created_at,
        stored.c.read_at,
        func.coalesce(report_notice(stored), false()).label("report"),
    ).select_from(shown())


async def notification(conn: AsyncConnection, notification_id: int) -> Notification:
    """Notification ``notification_id`` as the member's routes show it."""
    stored = notification_rows().where(tables.notifications.c.id == notification_id)
    (view,) = notifications_of((await conn.execute(stored)).all())
    return view


def visible(user_id: int):
    """The notifications of member ``user_id`` that are of no project, or of a project they hold a grant on."""
    stored, grants = tables.notifications, tables.grants
    granted = exists().where(grants.c.user_id == stored.c.user_id, grants.c.project_id == stored.c.project_id)
    return and_(stored.c.user_id == user_id, or_(stored.c.project_id.is_(None), granted))


def open_decision():
    """A notification of a decision that still waits for its answer."""
    return and_(tables.notifications.c.kind == "decision", tables.decisions.c.state == "open")


def open_proposal():
    """A notification of a proposal that still waits for its answer."""
    return and_(tables.notifications.c.kind == "proposal", tables.proposals.c.state == "open")


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def listed(
    user_id: int,
    unread: bool,
    kind: str | None,
    project: str | None,
    run_id: int | None = None,
    text: str | None = None,
    handler: str | None = None,
) -> list:
    """The conditions of the list's filters, over ``_shown``: ``text`` matches anywhere in the title or the body."""
    stored = tables.notifications
    found = [visible(user_id)]
    if unread:
        found.append(stored.c.read_at.is_(None))
    if kind is not None:
        found.append(stored.c.kind == kind)
    if project is not None:
        found.append(tables.projects.c.name == project)
    if run_id is not None:
        found.append(stored.c.run_id == run_id)
    if text:
        pattern = _like(text)
        found.append(or_(stored.c.title.ilike(pattern), stored.c.body.ilike(pattern)))
    if handler is not None:
        found.append(ops_views.held() if handler == "ops" else ~ops_views.held())
    return found


async def page(conn: AsyncConnection, found: list, limit: int, offset: int) -> tuple[list[Notification], int]:
    """The notifications that match ``found``, open decisions and proposals first, then newest first, a page of
    ``limit`` from ``offset``, and how many match."""
    waiting = or_(open_decision(), open_proposal())
    order = (func.coalesce(waiting, false()).desc(), tables.notifications.c.id.desc())
    listed_ = notification_rows().where(*found).order_by(*order).limit(limit).offset(offset)
    total = select(func.count()).select_from(shown()).where(*found)
    rows = notifications_of((await conn.execute(listed_)).all())
    return rows, (await conn.execute(total)).scalar_one()


def counts(user_id: int):
    """How many of the member's notifications are unread, and how many are of a decision or a proposal still open."""
    stored = tables.notifications
    return (
        select(
            func.count().filter(stored.c.read_at.is_(None)).label("unread"),
            func.count().filter(open_decision()).label("open_decisions"),
            func.count().filter(open_proposal()).label("open_proposals"),
        )
        .select_from(shown())
        .where(visible(user_id))
    )


async def mark_read(conn: AsyncConnection, user_id: int, ids: list[int] | None) -> list[int]:
    """Mark the member's unread notifications read, those of ``ids`` or all of them when it is None; their ids,
    sorted."""
    stored = tables.notifications
    marked = update(stored).values(read_at=func.now()).where(visible(user_id), stored.c.read_at.is_(None))
    if ids is not None:
        marked = marked.where(stored.c.id.in_(ids))
    return sorted((await conn.execute(marked.returning(stored.c.id))).scalars().all())

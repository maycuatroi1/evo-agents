"""What the Loop of a project tells people on its own clock, and its part of the morning brief (plan loop-engine,
criterion a8; the report's sections Goal and Trang Quyết định).

``facts`` is the Loop's part of a morning brief at a moment: the runs it handed out and the work items it finished in
the 24 hours before, the work items that wait for a person now by reason, and its breaker; None when the project's Loop
is off, so the brief of a project without the Loop has no such part. ``blocks`` writes it in markdown. The Curator's
morning brief carries it (``evo_agents.hub.server.brief``).

``send`` is the job ``hub.loop_notices``, every 5 minutes in the hub's worker. For each project whose Loop is on, in a
transaction of its own that holds the project's row of loop_states, at the local time of the Loop's zone
(``loop.timezone``, else the charter's, else UTC):

- ``goal_review``, gate 1: on the first day of a month (or the first pass after it, within REVIEW_DAYS, for a hub that
  was down), each admin of the project and each owner of one of its active goals gets the notice once that month: the
  active goals with their metric, whether it meets its target, and their deadline; it links to the decision board,
  where it stays until a goal gets a revision or the month ends. Nobody has to answer: a goal left alone stays.
- ``loop_brief``: a project whose Curator sends no morning brief (no night_shift schedule under a charter, or the
  feature curator off) gets the Loop's part of it at BRIEF_AT, within ``curator.BRIEF_GRACE``, once a local day, for
  each owner of an active goal.

Each goes to a member who holds a grant on the project, once: the hub looks for the notice of that month (or day) it
sent them already, by its details. The web, Telegram and the other channels get them as any notice
(``notifications.notify``); Telegram shows a kind it does not know as a notice with its title and link.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import exists, func, literal, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, goals, tables
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.autonomy_policies import effective_policy
from evo_agents.hub.server.loop.board_items import board_path, line
from evo_agents.hub.server.loop.model import DISPATCHED, DONE_EVENT, WAITING_HUMAN
from evo_agents.hub.server.loop.view import loop_zone, reached
from evo_agents.hub.server.notifications import notify

log = logging.getLogger(__name__)

GOAL_REVIEW, LOOP_BRIEF = "goal_review", "loop_brief"  # runs.NOTICE_KINDS
BRIEF_AT = "07:00"  # the local time of a loop_brief
REVIEW_DAYS = 7  # the first days of a month a goal_review may still go out in
WINDOW = timedelta(hours=24)  # what a brief counts as handed out and finished
MAX_LISTED = 10  # work items a brief names
NIGHT_SHIFT = "night_shift"  # schedules.kind of the Curator's night shift, whose brief carries the Loop's part
REASON_WORDS = {  # model.REASONS of waiting_human, as a brief says them
    "srs_review": "an SRS version to review",
    "srs_finding": "a settle chat of a finding",
    "srs_changes": "changes asked of an SRS version",
    "author_question": "an author run's question",
    "run_decision": "a plan run's decision",
    "loop_decision": "a decision after the third failure",
    "merge_blocked": "a merge request held",
    "merge_manual": "a merge by hand",
}


def _plural(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


# The Loop's part of a brief


async def _counted(conn: AsyncConnection, project_id: int, kind: str, since: datetime, until: datetime) -> dict:
    """Ledger rows of ``kind`` of the project's work items between ``since`` and ``until``, by kind of work item."""
    e, i = tables.loop_item_events, tables.loop_items
    query = (
        select(i.c.kind, func.count())
        .join_from(e, i, i.c.id == e.c.item_id)
        .where(i.c.project_id == project_id, e.c.kind == kind, e.c.at > since, e.c.at <= until)
        .group_by(i.c.kind)
    )
    return {found: count for found, count in await conn.execute(query)}


async def facts(conn: AsyncConnection, project_id: int, until: datetime) -> dict | None:
    """The Loop's part of a brief at ``until``; None when the project's Loop is off."""
    s, i = tables.loop_states, tables.loop_items
    state = (await conn.execute(select(s.c.enabled, s.c.breaker).where(s.c.project_id == project_id))).first()
    if state is None or not state.enabled:
        return None
    waiting_now = (i.c.project_id == project_id) & (i.c.state == WAITING_HUMAN)
    by_reason = select(i.c.reason, func.count()).where(waiting_now).group_by(i.c.reason)
    listed = select(i.c.id, i.c.kind, i.c.reason).where(waiting_now).order_by(i.c.updated_at, i.c.id).limit(MAX_LISTED)
    return {
        "handed_out": await _counted(conn, project_id, DISPATCHED, until - WINDOW, until),
        "done": await _counted(conn, project_id, DONE_EVENT, until - WINDOW, until),
        "waiting": {reason: count for reason, count in await conn.execute(by_reason)},
        "listed": [{"id": row.id, "kind": row.kind, "reason": row.reason} for row in await conn.execute(listed)],
        "breaker": state.breaker,
    }


def totals(found: dict) -> tuple[int, int, int]:
    """(runs handed out, work items done, work items waiting for a person) of ``facts``."""
    return tuple(sum(found[key].values()) for key in ("handed_out", "done", "waiting"))


def blocks(project: str, found: dict | None) -> list[str]:
    """The Loop's part of a brief in markdown, a paragraph and its list; none when the Loop is off."""
    if found is None:
        return []
    handed, done, waiting = totals(found)
    head = (
        f"**Loop**: {_plural(handed, 'run')} handed out and {_plural(done, 'work item')} done in the last 24 hours; "
        f"{waiting} waiting for a person"
    )
    reasons = "; ".join(
        f"{count} {REASON_WORDS.get(reason, reason)}" for reason, count in sorted(found["waiting"].items())
    )
    head += f" ({reasons})." if reasons else "."
    if found.get("breaker") == "tripped":
        head += " Its breaker tripped: it hands out no run until an admin resets it."
    lines = [
        f"- work item #{entry['id']} ({entry['kind']}): {REASON_WORDS.get(entry['reason'], entry['reason'])}"
        for entry in found["listed"]
    ]
    return [head, *(["\n".join(lines)] if lines else []), f"Decide on the decision board: {board_path(project)}."]


# The notices of the Loop's clock


async def _holders(conn: AsyncConnection, project_id: int, user_ids, *, admins: bool = False) -> list[int]:
    """Those of ``user_ids`` who hold a grant on the project, with its admins when ``admins``; by id."""
    g = tables.grants
    wanted = g.c.user_id.in_(sorted(set(user_ids)))
    query = select(g.c.user_id).where(
        g.c.project_id == project_id, (wanted | (g.c.role == "admin")) if admins else wanted
    )
    return sorted(set(await conn.scalars(query)))


async def _told(conn: AsyncConnection, project_id: int, kind: str, key: str, value: str) -> set[int]:
    """The members the hub sent the notice ``kind`` of the project whose details' ``key`` is ``value``."""
    n = tables.notifications
    query = select(n.c.user_id).where(
        n.c.project_id == project_id, n.c.notice_kind == kind, n.c.details[key].astext == value
    )
    return set(await conn.scalars(query))


async def _active_goals(conn: AsyncConnection, project_id: int) -> list:
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.number, r.c.statement, r.c.metric, r.c.deadline, r.c.rank, r.c.owner_id)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id, r.c.status == "active")
        .order_by(r.c.rank, g.c.number)
    )
    return list(await conn.execute(query))


async def _goal_lines(conn: AsyncConnection, project_id: int, active: list, now: datetime, today) -> list[str]:
    found = []
    for row in active:
        met = await reached(conn, project_id, row.metric, now)
        standing = {True: "meets its target", False: "below its target", None: "no value yet"}[met]
        deadline = row.deadline.isoformat() if row.deadline else "none"
        late = " (past)" if row.deadline and row.deadline < today else ""
        found.append(
            f"- {goals.code(row.number)} (rank {row.rank}): {line(row.statement)}; {goals.metric_text(row.metric)}, "
            f"{standing}; deadline {deadline}{late}"
        )
    return found


async def _goal_review(conn: AsyncConnection, project_id: int, project: str, local_now: datetime, now) -> int:
    """The month's goal_review (see the module); how many members it told."""
    if local_now.day > REVIEW_DAYS:
        return 0
    active = await _active_goals(conn, project_id)
    if not active:
        return 0
    month = local_now.strftime("%Y-%m")
    told = await _told(conn, project_id, GOAL_REVIEW, "month", month)
    holders = await _holders(conn, project_id, [row.owner_id for row in active], admins=True)
    to = [user for user in holders if user not in told]
    if not to:
        return 0
    today = local_now.date()
    late = sum(1 for row in active if row.deadline and row.deadline < today)
    body = "\n".join(
        [
            f"Review the goals of project {project} for {month} on the decision board: an admin writes the next "
            "revision of a goal (`evo-agents hub goal set`), and a goal nobody changes stays as it is.",
            "",
            *await _goal_lines(conn, project_id, active, now, today),
        ]
    )
    details = {"month": month, "goals": len(active), "past_deadline": late}
    title = f"Review the goals of {project} for {month}: {_plural(len(active), 'active goal')}"
    title += f", {late} past the deadline" if late else ""
    for user_id in to:
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=GOAL_REVIEW,
            project_id=project_id,
            title=title,
            body=body,
            details=details,
            link=board_path(project),
        )
    return len(to)


async def curator_briefs(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the project's Curator sends a morning brief, which then carries the Loop's part: a night_shift
    schedule, a charter, and the feature curator on (``curator._due``)."""
    s, c = tables.schedules, tables.charters
    charter = exists(select(c.c.project_id).where(c.c.project_id == project_id))
    query = select(s.c.id).where(
        s.c.project_id == project_id,
        s.c.kind == NIGHT_SHIFT,
        charter,
        hub_settings.feature_on("curator", s.c.project_id),
    )
    return await conn.scalar(query.limit(1)) is not None


async def _loop_brief(conn: AsyncConnection, project_id: int, project: str, local_now: datetime, now) -> int:
    """The day's loop_brief (see the module); how many members it told."""
    if not curator.brief_due(local_now, BRIEF_AT) or await curator_briefs(conn, project_id):
        return 0
    day = local_now.date().isoformat()
    told = await _told(conn, project_id, LOOP_BRIEF, "day", day)
    holders = await _holders(conn, project_id, [row.owner_id for row in await _active_goals(conn, project_id)])
    to = [user for user in holders if user not in told]
    found = await facts(conn, project_id, now)
    if not to or found is None:
        return 0
    handed, done, waiting = totals(found)
    title = f"Loop of {project}: {handed} handed out, {done} done, {waiting} waiting for a person"
    details = {"day": day, "handed_out": handed, "done": done, "waiting": waiting}
    for user_id in to:
        await notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=LOOP_BRIEF,
            project_id=project_id,
            title=title,
            body="\n\n".join(blocks(project, found)),
            details=details,
            link=board_path(project),
        )
    return len(to)


async def _project(conn: AsyncConnection, project_id: int, project: str, now: datetime | None) -> Counter:
    """The notices of one project whose Loop is on, in the caller's transaction, at ``now`` (the database's when
    None)."""
    s = tables.loop_states
    held = select(s.c.enabled).where(s.c.project_id == project_id).with_for_update()
    if not await conn.scalar(held):
        return Counter(off=1)
    zone = await loop_zone(conn, project_id, (await effective_policy(conn, project_id))["loop"])
    moment = await conn.scalar(select(func.now() if now is None else literal(now)))
    local_now = await conn.scalar(select(func.timezone(zone, literal(moment))))
    told = Counter()
    told[GOAL_REVIEW] += await _goal_review(conn, project_id, project, local_now, moment)
    told[LOOP_BRIEF] += await _loop_brief(conn, project_id, project, local_now, moment)
    return told


async def send(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass of ``hub.loop_notices`` over every project whose Loop is on, each in a transaction of its own, at
    ``now`` (the database's now when None): how many members got each notice, and the projects that failed."""
    s, p = tables.loop_states, tables.projects
    async with engine.begin() as conn:
        query = select(s.c.project_id, p.c.name).join_from(s, p, p.c.id == s.c.project_id).where(s.c.enabled)
        found = list(await conn.execute(query.order_by(s.c.project_id)))
    outcomes: Counter = Counter()
    for project_id, project in found:
        try:
            async with engine.begin() as conn:
                outcomes.update(await _project(conn, project_id, project, now))
        except Exception:  # one project failing leaves the others their notices
            log.exception("the Loop's notices failed", extra={"project": project})
            outcomes["failed"] += 1
    sent = {key: count for key, count in sorted(outcomes.items()) if count}
    if sent:
        log.info("loop notices", extra=sent)
    return sent


__all__ = ["BRIEF_AT", "GOAL_REVIEW", "LOOP_BRIEF", "blocks", "curator_briefs", "facts", "send", "totals"]

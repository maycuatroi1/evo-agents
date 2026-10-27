"""The three gates of a project's decision board (plan loop-engine, criterion a8; the report's sections Goal and Trang
Quyết định): where a person decides the direction of the project.

- gate 1, ``goal``: the month's review of the goals once the hub asked for it (the notice ``goal_review`` of
  ``loop.notices``), until a goal of the project gets a revision after it, or the month ends; and each active goal past
  its deadline in the Loop's zone. An admin writes a goal.
- gate 2, ``canvas``: each baseline of a canvas still proposed with an item to decide (a change of status, a status
  the value check proposed, a test card past its deadline to extend or retire), for the writers who changed none of
  those claims (any writer once the plugin canvas's self_approval is on); and each open proposal of a claim (a brainstorm's, the value check's, the Curator's lens product), for
  any writer. Both are read under their labels, as the plugin canvas reads them; an archived canvas has none.
- gate 3, ``policy``: each pending proposal of @builder of kind policy or goal, each repo of the merge queue whose
  breaker tripped, each active goal that aims at an SRS document whose baseline is not confirmed (``srs_baseline``,
  plan srs-backlog-loop; ``evo-agents hub loop baseline``), and the Loop's own breaker tripped; an admin decides each.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, goals, tables
from evo_agents.hub.server.loop import baseline
from evo_agents.hub.server.loop.board_items import (
    ADMIN,
    WRITER,
    BoardItem,
    People,
    Reading,
    canvas_path,
    item,
    line,
    project_path,
    self_approval,
)

GOAL_REVIEW = "goal_review"  # runs.NOTICE_KINDS: loop.notices sends it
ACTIVE = "active"  # goals.STATUSES
PROPOSED, OPEN = "proposed", "open"  # tables_canvas.BASELINE_STATUSES, PROPOSAL_STATUSES
DECIDED_ITEMS = ("status", "value_check", "overdue")  # canvas.baselines.DECIDES: the item kinds a member decides
OVERDUE = "overdue"
PENDING = "pending"  # tables_settings.PROPOSAL_STATES
BUILDER_KINDS = {"policy": "policy_proposal", "goal": "goal_proposal"}  # of @builder's proposals, gate 3's
TRIPPED = "tripped"  # merge_breaker.STATES, loop_states.breaker
SOURCES = {"hub": "the value check", "agent": "an agent", "person": "a member"}  # tables_canvas.ACTORS, run aside
CANVAS = "canvas"  # the core plugin whose config says self_approval


def _plural(number: int, word: str) -> str:
    return f"{number} {word}" + ("" if number == 1 else "s")


# Gate 1: goals


def _day_after(day: date, zone: str) -> datetime:
    """The start of the day after ``day`` in ``zone`` (UTC when Python does not know the zone)."""
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = ZoneInfo("UTC")
    return datetime.combine(day + timedelta(days=1), time(), tzinfo=tz)


async def review_asked(conn: AsyncConnection, project_id: int, month: str) -> datetime | None:
    """When the hub asked for the review of the goals of ``month`` (YYYY-MM), while no goal of the project got a
    revision since; None otherwise."""
    n, g, r = tables.notifications, tables.goals, tables.goal_revisions
    asked = await conn.scalar(
        select(func.min(n.c.created_at)).where(
            n.c.project_id == project_id, n.c.notice_kind == GOAL_REVIEW, n.c.details["month"].astext == month
        )
    )
    if asked is None:
        return None
    revised = select(r.c.goal_id).join_from(r, g, g.c.id == r.c.goal_id)
    revised = revised.where(g.c.project_id == project_id, r.c.created_at > asked).limit(1)
    return None if await conn.scalar(revised) is not None else asked


async def goal_items(conn: AsyncConnection, reading: Reading, crowd: People, zone: str, today: date) -> list[BoardItem]:
    """Gate 1 on the local day ``today`` of the Loop's zone ``zone``."""
    g, r = tables.goals, tables.goal_revisions
    admins = crowd.holding(ADMIN)
    query = (
        select(g.c.number, r.c.statement, r.c.deadline)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == reading.project_id, r.c.status == ACTIVE, r.c.deadline < today)
        .order_by(r.c.deadline, g.c.number)
    )
    found = []
    for row in await conn.execute(query):
        code = goals.code(row.number)
        title = f"{code} is past its deadline {row.deadline.isoformat()}: {line(row.statement)}"
        link = project_path(reading.project, "goals", code)
        found.append(
            item("goal_overdue", title, _day_after(row.deadline, zone), ADMIN, admins, link, refs={"goal": code})
        )
    month = today.strftime("%Y-%m")
    asked = await review_asked(conn, reading.project_id, month)
    if asked is not None:
        link = project_path(reading.project, "goals")
        title = f"Review the goals of {reading.project} for {month}: an admin writes the next revision of a goal"
        found.append(item(GOAL_REVIEW, title, asked, ADMIN, admins, link, refs={"month": month}))
    return found


# Gate 2: canvases


async def _baselines(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    b, k, i = tables.canvas.baselines, tables.canvas.canvases, tables.canvas.baseline_items
    query = (
        select(b.c.id, b.c.number, b.c.week, b.c.created_at, b.c.label, k.c.slug, k.c.label.label("canvas_label"))
        .join_from(b, k, k.c.id == b.c.canvas_id)
        .where(k.c.project_id == reading.project_id, k.c.archived_at.is_(None), b.c.status == PROPOSED)
        .order_by(b.c.id)
    )
    rows = [row for row in await conn.execute(query) if reading.label(row.label, row.canvas_label)]
    if not rows:
        return []
    waiting: dict[int, list] = defaultdict(list)
    query = select(i.c.baseline_id, i.c.kind, i.c.proposers).where(
        i.c.baseline_id.in_([row.id for row in rows]), i.c.decision.is_(None), i.c.kind.in_(DECIDED_ITEMS)
    )
    anyone = await self_approval(conn, CANVAS, reading.project_id)  # a proposer may decide the item too
    for baseline_id, kind, proposers in await conn.execute(query):
        waiting[baseline_id].append((kind, [] if anyone else proposers or []))
    found = []
    for row in rows:
        asks = waiting.get(row.id)
        if not asks:
            continue  # nothing left to decide: the plugin approves it
        deciders = {login for _, proposers in asks for login in crowd.holding(WRITER, but=proposers)}
        overdue = sum(1 for kind, _ in asks if kind == OVERDUE)
        title = f"Baseline {row.number} of canvas {row.slug}" + (f" ({row.week})" if row.week else "")
        title += f": {_plural(len(asks), 'item')} to decide"
        title += f", {_plural(overdue, 'test card')} past the deadline" if overdue else ""
        link = canvas_path(reading.project, row.slug, "baselines", str(row.number))
        refs = {"baseline": row.id, "canvas": row.slug, "number": row.number}
        found.append(item("baseline", title, row.created_at, WRITER, deciders, link, refs=refs))
    return found


def _proposed(row) -> str:
    """What a proposal of a claim asks, in words."""
    if row.claim is None:
        return "a new claim"
    fields = row.fields if isinstance(row.fields, dict) else {}
    if isinstance(fields.get("status"), str):
        return f"CL-{row.claim} to {fields['status']}"
    return f"CL-{row.claim}: {', '.join(sorted(fields)) or 'a change'}"


async def _proposals(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    p, k, c = tables.canvas.proposals, tables.canvas.canvases, tables.canvas.claims
    query = (
        select(
            p.c.id,
            p.c.fields,
            p.c.label,
            p.c.actor,
            p.c.run_id,
            p.c.created_at,
            k.c.slug,
            k.c.label.label("canvas_label"),
            c.c.number.label("claim"),
        )
        .join_from(p, k, k.c.id == p.c.canvas_id)
        .outerjoin(c, c.c.id == p.c.claim_id)
        .where(k.c.project_id == reading.project_id, k.c.archived_at.is_(None), p.c.status == OPEN)
        .order_by(p.c.id)
    )
    writers = crowd.holding(WRITER)
    found = []
    for row in await conn.execute(query):
        if not reading.label(row.label, row.canvas_label):
            continue
        source = (
            SOURCES.get(row.actor, "a member") if row.run_id is None or row.actor == "hub" else f"run #{row.run_id}"
        )
        title = f"Proposal #{row.id} on canvas {row.slug}: {_proposed(row)}, from {source}"
        link = canvas_path(reading.project, row.slug)
        refs = {"proposal": row.id, "canvas": row.slug}
        found.append(item("claim_proposal", title, row.created_at, WRITER, writers, link, refs=refs))
    return found


async def canvas_items(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    """Gate 2."""
    return [*await _baselines(conn, reading, crowd), *await _proposals(conn, reading, crowd)]


# Gate 3: policy


async def _builder(conn: AsyncConnection, reading: Reading, admins: list[str]) -> list[BoardItem]:
    bp = tables.builder_proposals
    query = (
        select(bp.c.id, bp.c.kind, bp.c.summary, bp.c.proposed_at, bp.c.chat_id, bp.c.message_id, bp.c.run_id)
        .where(bp.c.project_id == reading.project_id, bp.c.state == PENDING, bp.c.kind.in_(tuple(BUILDER_KINDS)))
        .order_by(bp.c.id)
    )
    found = []
    for row in await conn.execute(query):
        if row.chat_id is not None:
            link = chats.chat_link(row.chat_id, row.message_id)
        else:
            link = project_path(reading.project, "runs", str(row.run_id)) if row.run_id else None
        title = f"@builder proposal #{row.id} ({row.kind}): {line(row.summary)}"
        refs = {"proposal": row.id, **({"run": row.run_id} if row.run_id else {})}
        found.append(item(BUILDER_KINDS[row.kind], title, row.proposed_at, ADMIN, admins, link, refs=refs))
    return found


async def policy_items(conn: AsyncConnection, reading: Reading, crowd: People) -> list[BoardItem]:
    """Gate 3."""
    admins = crowd.holding(ADMIN)
    found = await _builder(conn, reading, admins)
    mb = tables.merge_breakers
    query = select(mb.c.repo, mb.c.cause, mb.c.reason, mb.c.tripped_at).where(
        mb.c.project_id == reading.project_id, mb.c.state == TRIPPED
    )
    queue = project_path(reading.project, "merge-queue")
    for row in await conn.execute(query.order_by(mb.c.repo)):
        title = f"The merge queue of {row.repo} stopped: {line(row.reason or row.cause or 'its breaker tripped')}"
        found.append(item("merge_breaker", title, row.tripped_at, ADMIN, admins, queue, refs={"repo": row.repo}))
    for row in await baseline.waiting(conn, reading.project_id):
        title = (
            f"Baseline of goal G-{row.number} on SRS document {row.slug} waits for an admin: "
            "the Loop makes no build for it until it is confirmed "
            f"(evo-agents hub loop baseline --goal G-{row.number})"
        )
        link = project_path(reading.project, "goals")
        refs = {"goal": f"G-{row.number}", "document": row.slug}
        found.append(item("srs_baseline", title, row.since, ADMIN, admins, link, refs=refs))
    s = tables.loop_states
    query = select(s.c.breaker_reason, s.c.tripped_at).where(
        s.c.project_id == reading.project_id, s.c.breaker == TRIPPED
    )
    for row in await conn.execute(query):
        title = f"The Loop stopped: {line(row.breaker_reason or 'its breaker tripped')}; an admin resets it"
        found.append(item("loop_breaker", title, row.tripped_at, ADMIN, admins, project_path(reading.project, "loop")))
    return found


__all__ = ["canvas_items", "goal_items", "policy_items", "review_asked"]

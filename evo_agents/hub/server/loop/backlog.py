"""The backlog of a goal that aims at an SRS document, read from the hub (plan srs-backlog-loop, criterion a2): the
approved use cases of the goal's document that the ledger does not keep out, in groups. The rules are pure
(``evo_agents.hub.srs_backlog``); this reads what they need.

A use case is approved when the document's approved version holds it, or when a review approved it in a version still
in review and ``srs.use_case.approved`` told of it (the newest event of a use case wins: one that says it was removed
takes it out). The goal's heading limits them to those under it. The ledger is ``delivery``: a use case it says
delivered, delivering or unknown is out of the backlog. A goal with no SRS document, or one that is not there any more,
has no backlog: ``scope`` is None and ``open_of`` None, so a Loop with no goal of that kind does as it did.

A group of the backlog that a plan of the project names one use case of (``srs_backlog.held``) gets no build: the plan
is active or completed, no work item of the Loop has it (the deliver of a build or of ``loop adopt``), and the use
case's ledger row, if any, is not an admin's. Such a plan delivers the use cases already, mostly one written before they
were approved; the goal's owner adopts it (``loop adopt``, the decision board's ``srs_adopt``), or an admin sets the use
case undelivered for a build to take it.

``use_cases`` reads a document's approved use cases whatever a goal says, for the baseline.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Integer, Text, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.hub_events import SRS_USE_CASE_APPROVED
from evo_agents.hub.server.loop import delivery
from evo_agents.hub.srs_backlog import (
    ADMIN,
    Group,
    Held,
    UseCase,
    backlog,
    groups,
    held,
    names,
    plan_text,
    ref_key,
    under,
)

USE_CASE = "use_case"
PLAN_AREAS = ("active", "completed")  # plans.AREAS
REMOVED = "removed"  # use_case_reviews.REMOVED: the change an event tells of a use case a version drops


async def _from_version(conn: AsyncConnection, document_id: int, number: int) -> list[UseCase]:
    """The use cases of version ``number`` of the document, with the codes or titles of their ancestors; the tables of
    srs are read here, as the feature srs is not imported by the Loop's callers (docs/structure.md)."""
    v, iv, i = tables.srs.versions, tables.srs.item_versions, tables.srs.items
    query = (
        select(iv.c.item_id, iv.c.parent_item_id, iv.c.title, iv.c.fields, i.c.kind, i.c.key, i.c.code)
        .join_from(iv, v, v.c.id == iv.c.version_id)
        .join(i, i.c.id == iv.c.item_id)
        .where(v.c.document_id == document_id, v.c.number == number)
        .order_by(iv.c.position)
    )
    rows = (await conn.execute(query)).all()
    by_id = {row.item_id: row for row in rows}
    found = []
    for row in rows:
        if row.kind != USE_CASE or not (row.code or row.key):
            continue
        names, up = [], row.parent_item_id
        while up in by_id:
            names.append(by_id[up].code or by_id[up].title)
            up = by_id[up].parent_item_id
        names.reverse()
        claim = (row.fields or {}).get("claim") or None
        found.append(UseCase(row.code or row.key, row.title, claim, names[-1] if names else None, tuple(names), number))
    return found


async def _told(conn: AsyncConnection, document_id: int, since: int) -> list[UseCase]:
    """The use cases ``srs.use_case.approved`` told of in versions after ``since``, those a later event removes left
    out, in the order they were told."""
    e = tables.hub_events
    query = (
        select(e.c.payload)
        .where(
            e.c.kind == SRS_USE_CASE_APPROVED,
            e.c.payload["document_id"].astext == str(document_id),
            cast(e.c.payload["version"].astext, Integer) > since,
        )
        .order_by(e.c.id)
    )
    found: dict[str, UseCase | None] = {}
    for payload in (await conn.execute(query)).scalars():
        ref = payload.get("code") or payload.get("key")
        if not ref:
            continue
        parent = payload.get("parent")
        found[ref_key(ref)] = (
            None
            if payload.get("change") == REMOVED
            else UseCase(
                ref,
                payload.get("title") or ref,
                payload.get("claim") or None,
                parent,
                (parent,) if parent else (),
                payload.get("version"),
            )
        )
    return [case for case in found.values() if case is not None]


async def use_cases(conn: AsyncConnection, project_id: int, document_id: int) -> list[UseCase]:
    """The approved use cases of document ``document_id``, those of its approved version first, then those a review
    approved in a newer version, once each."""
    d = tables.srs.documents
    number = await conn.scalar(select(d.c.approved_version).where(d.c.id == document_id, d.c.project_id == project_id))
    found = await _from_version(conn, document_id, number) if number else []
    seen = {ref_key(case.ref) for case in found}
    return found + [case for case in await _told(conn, document_id, number or 0) if ref_key(case.ref) not in seen]


async def scope(conn: AsyncConnection, goal_id: int) -> tuple[int, int, str | None] | None:
    """(project, document, heading) a goal aims at in its newest revision; None for a goal with no SRS document."""
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(g.c.project_id, r.c.srs_document_id, r.c.srs_heading)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.id == goal_id, r.c.srs_document_id.is_not(None))
    )
    row = (await conn.execute(query)).first()
    return None if row is None else (row.project_id, row.srs_document_id, row.srs_heading)


async def backlog_of(conn: AsyncConnection, goal_id: int) -> list[UseCase] | None:
    """The backlog of goal ``goal_id``: its approved use cases the ledger does not keep out; None without a document."""
    aimed = await scope(conn, goal_id)
    if aimed is None:
        return None
    project_id, document_id, heading = aimed
    approved = under(await use_cases(conn, project_id, document_id), heading)
    return backlog(approved, await delivery.states(conn, document_id))


async def naming(conn: AsyncConnection, project_id: int, cases: Sequence[UseCase]) -> dict[str, tuple[str, ...]]:
    """The plans of the project, active or completed and of no work item of the Loop, that name each of ``cases``, by
    the case's ``ref_key``; the database keeps the plans whose JSON holds the last word of a ref."""
    if not cases:
        return {}
    p, i = tables.plans, tables.loop_items
    words = sorted({case.ref.split()[-1].casefold() for case in cases})
    text = func.lower(cast(p.c.body, Text), type_=Text)
    loop_plans = select(i.c.plan_id).where(i.c.project_id == project_id, i.c.plan_id.is_not(None))
    query = (
        select(p.c.plan_id, p.c.body)
        .where(
            p.c.project_id == project_id,
            p.c.area.in_(PLAN_AREAS),
            p.c.plan_id.not_in(loop_plans),
            or_(*(text.contains(word, autoescape=True) for word in words)),
        )
        .order_by(p.c.plan_id)
    )
    found: dict[str, list[str]] = {}
    for row in await conn.execute(query):
        words_of_plan = plan_text(row.body)
        for case in cases:
            if names(words_of_plan, case.ref):
                found.setdefault(ref_key(case.ref), []).append(row.plan_id)
    return {key: tuple(plans) for key, plans in found.items()}


async def split_of(conn: AsyncConnection, goal_id: int) -> tuple[list[Group], list[Held]]:
    """The backlog of goal ``goal_id`` in groups: those a build may take, and those a plan holds (see the module);
    none without a document."""
    aimed = await scope(conn, goal_id)
    if aimed is None:
        return [], []
    project_id, document_id, heading = aimed
    ledger = await delivery.read(conn, document_id)
    approved = under(await use_cases(conn, project_id, document_id), heading)
    found = backlog(approved, {key: row.state for key, row in ledger.items()})
    by_admin = {key for key, row in ledger.items() if row.source == ADMIN}
    asked = [case for case in found if ref_key(case.ref) not in by_admin]
    return held(groups(found), await naming(conn, project_id, asked))


async def groups_of(conn: AsyncConnection, goal_id: int) -> list[Group]:
    """The groups of the backlog of goal ``goal_id`` a build may take, one build each (``srs_backlog.groups``), those
    a plan holds left out; none without a document."""
    return (await split_of(conn, goal_id))[0]


async def open_of(conn: AsyncConnection, goal_id: int) -> int | None:
    """The value of ``srs.backlog.open`` for goal ``goal_id``: its backlog's size; None without a document."""
    found = await backlog_of(conn, goal_id)
    return None if found is None else len(found)


__all__ = ["backlog_of", "groups_of", "naming", "open_of", "scope", "split_of", "use_cases"]

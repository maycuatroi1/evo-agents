"""The work items of a project's Loop as its pages read them (plan loop-engine, criterion a10): GET
/v1/projects/{p}/loop/items lists them, newest first, and GET /v1/projects/{p}/loop/goals measures each goal against
its target with its work items by state, for the hub's web at ``/p/<project>/loop`` and ``/p/<project>/goals``.

A work item says what it is (kind, cycle), where it stands and why (``state``, ``reason`` and the figures of that
reason, ``waiting``), how often a run was handed out for it and failed, who it stands for, and its ancestors by their
codes: the goal, the claim of a canvas, the use case of an SRS document and its version, the frame task, the plan and
the run in flight, the work item it came of and the first of its chain (``root``). Each ancestor shows as its own
route shows it: a claim, a document and a task under their labels through the reader's sink, a plan and its run when
the reader reads that plan's runs (``board_items.Reading``); one the reader may not read is left out and named in
``hidden``. Nothing here writes, and a project whose Loop is off reads as one with no work item.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, tables
from evo_agents.hub.server.loop.board_items import Reading
from evo_agents.hub.server.loop.model import ALL_REASONS, CYCLES, FINISHED, KINDS, STATES
from evo_agents.hub.server.loop.view import COMPARE, measure

MAX_ITEMS = 500  # the most work items one read lists
DEFAULT_ITEMS = 200
MAX_DEPTH = 8  # how far up a work item's chain is followed to its first (a use_case, a build, a deliver: 3)
STAGES = ("claim", "use_case", "task", "plan", "run")  # the ancestors a reader may be kept from
# The figures of each reason a work item shows (``admit.refusal`` and the engine's transitions write them into its
# detail, which keeps those of earlier reasons too)
FIGURES = {
    "concurrency": ("cycle", "running", "max_running", "repos", "repo_max_running"),
    "rate": ("dispatched", "dispatches_per_hour"),
    "worker": ("worker", "why"),
    "type_refused": ("why",),
    "stable": ("stable_at",),
    "split": ("task",),
    "triage": ("failures",),
    "loop_decision": ("failures",),
    "merge_manual": ("by_hand",),
}
Figure = int | float | str | list[str]


class LoopClaimRef(BaseModel):
    canvas: str = Field(description="the slug of the claim's canvas")
    code: str = Field(description="CL-<number>")


class LoopUseCaseRef(BaseModel):
    document: str = Field(description="the slug of the SRS document")
    code: str | None = Field(description="the use case's code; null before the document has one for it")
    version: int | None = Field(description="the version of the document it was read from")


class LoopItem(BaseModel):
    """A work item of the Loop, as a member of the project reads it."""

    id: int
    kind: Literal[KINDS]
    cycle: Literal[CYCLES]
    state: Literal[STATES]
    reason: Literal[ALL_REASONS] | None = Field(description="why it stands where it does; done has none")
    waiting: dict[str, Figure] = Field(
        description="the figures of its reason: the cap and the count for concurrency or rate, the worker and why..."
    )
    attempts: int = Field(description="how often a run was handed out for it")
    failures: int = Field(description="its failures since a person last let it go on")
    owner: str | None = Field(description="the agent type or the member it stands for")
    goal: str | None = Field(description="the code of its goal")
    claim: LoopClaimRef | None
    use_case: LoopUseCaseRef | None
    task: int | None = Field(description="the number of its frame task in the plugin tasks")
    plan: str | None
    run: int | None = Field(description="the run handed out for it last")
    parent: int | None = Field(description="the work item it came of")
    root: int = Field(description="the first work item of its chain: itself when it came of none")
    hidden: list[Literal[STAGES]] = Field(description="the ancestors it has that the reader may not read")
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class LoopItems(BaseModel):
    project: str
    items: list[LoopItem] = Field(description="the newest first")
    more: bool = Field(description="true when more work items match than were listed")


class LoopGoal(BaseModel):
    """A goal as the Loop measures it now."""

    code: str
    value: float | None = Field(description="its metric's value now; null without a metric or a signal in its window")
    reached: bool | None = Field(description="whether that value meets its target; null without a value")
    items: dict[Literal[STATES], int] = Field(description="its work items by state")


def figures(reason: str | None, detail: Mapping) -> dict[str, Figure]:
    """The figures of ``reason`` among what a work item's ``detail`` holds: numbers, words and lists of words only."""
    found: dict[str, Figure] = {}
    for key in FIGURES.get(reason or "", ()):
        value = detail.get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int | float | str):
            found[key] = value
        elif isinstance(value, list) and all(isinstance(entry, str) for entry in value):
            found[key] = list(value)
    if reason == "merge_blocked" and isinstance(detail.get("merges"), list):
        found["merges"] = len(detail["merges"])
    if reason == "srs_finding" and isinstance((detail.get("srs") or {}).get("findings"), list):
        found["findings"] = len(detail["srs"]["findings"])
    return found


def _query():
    i, g, c, k = tables.loop_items, tables.goals, tables.canvas.claims, tables.canvas.canvases
    d, t, r = tables.srs.documents, tables.tasks.items, tables.runs
    joined = (
        i.outerjoin(g, g.c.id == i.c.goal_id)
        .outerjoin(c, c.c.id == i.c.claim_id)
        .outerjoin(k, k.c.id == c.c.canvas_id)
        .outerjoin(d, d.c.id == i.c.srs_document_id)
        .outerjoin(t, t.c.id == i.c.task_id)
        .outerjoin(r, r.c.id == i.c.run_id)
    )
    return select(
        i,
        g.c.number.label("goal_number"),
        c.c.number.label("claim_number"),
        c.c.label.label("claim_label"),
        k.c.slug.label("canvas_slug"),
        k.c.label.label("canvas_label"),
        d.c.slug.label("document_slug"),
        d.c.label.label("document_label"),
        t.c.number.label("task_number"),
        t.c.label.label("task_label"),
        r.c.plan_id.label("run_plan"),
    ).select_from(joined)


async def rows_of(conn: AsyncConnection, project_id: int, *where, limit: int | None = None) -> list:
    """The project's work items matching ``where``, the newest first, with what their ancestors are named by."""
    i = tables.loop_items
    query = _query().where(i.c.project_id == project_id, *where).order_by(i.c.id.desc())
    return list(await conn.execute(query if limit is None else query.limit(limit)))


async def roots_of(conn: AsyncConnection, rows: Sequence) -> dict[int, int]:
    """Each work item of ``rows`` -> the first of its chain, followed up its parents MAX_DEPTH deep at most."""
    i = tables.loop_items
    parents = {row.id: row.parent_id for row in rows}
    for _ in range(MAX_DEPTH):
        wanted = sorted({parent for parent in parents.values() if parent is not None and parent not in parents})
        if not wanted:
            break
        for found, parent in await conn.execute(select(i.c.id, i.c.parent_id).where(i.c.id.in_(wanted))):
            parents[found] = parent
        parents.update({missing: None for missing in wanted if missing not in parents})
    roots = {}
    for row in rows:
        at, seen = row.id, {row.id}
        while parents.get(at) is not None and parents[at] not in seen:
            at = parents[at]
            seen.add(at)
        roots[row.id] = at
    return roots


async def _documents(conn: AsyncConnection, project_id: int, slugs: Iterable[str]) -> dict[str, Mapping]:
    """The labels of the SRS documents of ``slugs`` (a use_case names its document in its detail)."""
    wanted = sorted(set(slugs))
    if not wanted:
        return {}
    d = tables.srs.documents
    query = select(d.c.slug, d.c.label).where(d.c.project_id == project_id, d.c.slug.in_(wanted))
    return {slug: label for slug, label in await conn.execute(query)}


def _drafted(row) -> tuple[str | None, int | None]:
    """The document and version a use_case drafted, from its detail (``srs`` while it waits, then the approval's)."""
    detail = row.detail or {}
    srs = detail.get("srs") if isinstance(detail.get("srs"), dict) else {}
    document = row.document_slug or srs.get("document") or detail.get("document")
    version = row.srs_version or srs.get("version") or detail.get("version")
    return (document if isinstance(document, str) else None, version if isinstance(version, int) else None)


def _item(reading: Reading, row, root: int, labels: Mapping[str, Mapping]) -> LoopItem:
    hidden: list[str] = []
    claim = None
    if row.claim_number is not None:
        if reading.label(row.claim_label, row.canvas_label):
            claim = LoopClaimRef(canvas=row.canvas_slug, code=f"CL-{row.claim_number}")
        else:
            hidden.append("claim")
    use_case = None
    document, version = _drafted(row)
    if document is not None:
        label = row.document_label if row.document_slug else labels.get(document)
        if label is not None and reading.label(label):
            use_case = LoopUseCaseRef(document=document, code=row.use_case, version=version)
        else:
            hidden.append("use_case")
    task = row.task_number if row.task_number is None or reading.label(row.task_label) else None
    if row.task_number is not None and task is None:
        hidden.append("task")
    plan = row.plan_id if row.plan_id is None or row.plan_id in reading.plans else None
    if row.plan_id is not None and plan is None:
        hidden.append("plan")
    run = row.run_id if row.run_id is None or reading.run(row.run_plan) else None
    if row.run_id is not None and run is None:
        hidden.append("run")
    return LoopItem(
        id=row.id,
        kind=row.kind,
        cycle=row.cycle,
        state=row.state,
        reason=row.reason,
        waiting=figures(row.reason, row.detail or {}),
        attempts=row.attempts,
        failures=row.failures,
        owner=row.owner,
        goal=goals.code(row.goal_number) if row.goal_number is not None else None,
        claim=claim,
        use_case=use_case,
        task=task,
        plan=plan,
        run=run,
        parent=row.parent_id,
        root=root,
        hidden=hidden,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


async def described(conn: AsyncConnection, reading: Reading, rows: Sequence) -> list[LoopItem]:
    """``rows`` (of ``rows_of``) as ``reading`` reads them."""
    roots = await roots_of(conn, rows)
    named = [_drafted(row)[0] for row in rows if not row.document_slug]
    labels = await _documents(conn, reading.project_id, [slug for slug in named if slug])
    return [_item(reading, row, roots[row.id], labels) for row in rows]


async def listed(
    conn: AsyncConnection,
    reading: Reading,
    *,
    state: str | None = None,
    kind: str | None = None,
    goal: str | None = None,
    open_only: bool = False,
    limit: int = DEFAULT_ITEMS,
) -> LoopItems:
    """The project's work items of ``state``, ``kind`` and goal code ``goal`` (each when given; only unfinished ones
    with ``open_only``), the newest ``limit``."""
    i, g = tables.loop_items, tables.goals
    where = []
    if state is not None:
        where.append(i.c.state == state)
    if kind is not None:
        where.append(i.c.kind == kind)
    if open_only:
        where.append(i.c.state.not_in(FINISHED))
    if goal is not None:
        number = goals.number_of(goal)
        where.append(g.c.number == number if number is not None else g.c.alias == goal)
    rows = await rows_of(conn, reading.project_id, *where, limit=limit + 1)
    items = await described(conn, reading, rows[:limit])
    return LoopItems(project=reading.project, items=items, more=len(rows) > limit)


async def goal_figures(conn: AsyncConnection, project_id: int) -> list[LoopGoal]:
    """Every goal of the project, by rank then code, with its metric measured now and its work items by state."""
    g, r, i = tables.goals, tables.goal_revisions, tables.loop_items
    query = (
        select(g.c.id, g.c.number, r.c.metric, r.c.rank)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id)
        .order_by(r.c.rank, g.c.number)
    )
    rows = list(await conn.execute(query))
    counted: dict[int, dict[str, int]] = {row.id: dict.fromkeys(STATES, 0) for row in rows}
    by_state = select(i.c.goal_id, i.c.state, func.count()).where(i.c.project_id == project_id)
    for goal_id, state, count in await conn.execute(by_state.group_by(i.c.goal_id, i.c.state)):
        if goal_id in counted and state in counted[goal_id]:
            counted[goal_id][state] = count
    now = datetime.now(UTC)
    found = []
    for row in rows:
        metric = row.metric or None
        value = await measure(conn, project_id, metric, now) if metric else None
        comparator, target = (metric or {}).get("comparator"), (metric or {}).get("target")
        reached = None if value is None or comparator not in COMPARE else COMPARE[comparator](value, target)
        found.append(LoopGoal(code=goals.code(row.number), value=value, reached=reached, items=counted[row.id]))
    return found


__all__ = [
    "DEFAULT_ITEMS",
    "MAX_ITEMS",
    "LoopClaimRef",
    "LoopGoal",
    "LoopItem",
    "LoopItems",
    "LoopUseCaseRef",
    "described",
    "figures",
    "goal_figures",
    "listed",
    "roots_of",
    "rows_of",
]

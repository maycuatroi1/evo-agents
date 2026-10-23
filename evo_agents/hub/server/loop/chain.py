"""One work item of a project's Loop with its chain (plan loop-engine, criterion a10): GET
/v1/projects/{p}/loop/items/{id}, which the hub's web shows at ``/p/<project>/loop/items/<id>``.

The chain is every work item that came of the first one (``LoopItem.root``), in the order they came of each other; its
path is the first one, down to this work item, then on through the one work item each came of it, while there is only
one. The stages of the path run from the problem the chain goes from (plan problem-oriented-hub) and the goal to the
claim, the use case, the frame task, the plan, the runs handed out, the merge requests of the plan and the release,
each as its own route shows it (``items``): an ancestor the reader
may not read is left out and named in ``hidden``, and so are the merge requests of a plan they may not read. The
ledger is what happened to this work item, oldest first. Nothing here writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals, problems, tables
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server.loop import items
from evo_agents.hub.server.loop.board_items import Reading, line
from evo_agents.hub.server.loop.items import LoopItem
from evo_agents.hub.server.loop.model import ALL_REASONS, DISPATCHED, EVENT_KINDS, STATES
from evo_agents.hub.server.loop.view import TASKS, run_chains, status_groups

CHAIN_STAGES = ("claim", "use_case", "task", "plan", "runs", "merges")  # what a reader may be kept from
PLAN_LINE_CHARS = 160


class LoopChainGoal(BaseModel):
    code: str
    statement: str
    status: Literal[goals.STATUSES]


class LoopChainProblem(BaseModel):
    code: str = Field(description="P-<number>")
    statement: str = Field(description="its statement now, on one line")
    status: Literal[problems.STATUSES]


class LoopChainClaim(BaseModel):
    canvas: str
    code: str
    status: str = Field(description="the claim's status now")


class LoopChainUseCase(BaseModel):
    document: str
    code: str | None
    version: int | None
    state: str | None = Field(description="the state of that version of the document")


class LoopChainTask(BaseModel):
    number: int
    title: str
    status: str = Field(description="the task's status, a key of the plugin tasks' config")
    group: str | None = Field(description="its group: todo, doing, done or cancelled")


class LoopChainPlan(BaseModel):
    id: str
    goal: str | None = Field(description="the plan's goal, on one line")


class LoopChainRun(BaseModel):
    id: int = Field(description="the newest run that goes on from the one handed out (resumed, retried)")
    kind: str
    state: str
    work_item: int


class LoopChainMerge(BaseModel):
    id: int
    repo: str
    iid: int | None
    state: str
    web_url: str | None
    merged_at: datetime | None


class LoopChainStages(BaseModel):
    """The path of a chain from its goal to its release."""

    problem: LoopChainProblem | None = Field(
        default=None, description="the problem the chain goes from, as it stands now; null for none"
    )
    goal: LoopChainGoal | None
    proposals: int | None = Field(description="the claims a brainstorm proposed")
    claim: LoopChainClaim | None
    use_case: LoopChainUseCase | None
    task: LoopChainTask | None
    plan: LoopChainPlan | None
    runs: list[LoopChainRun]
    merges: list[LoopChainMerge]
    released_at: datetime | None = Field(description="when the last merge request of the delivered plan merged")
    after_sha: list[str] = Field(description="the commits the release merged")
    hidden: list[Literal[CHAIN_STAGES]] = Field(description="the stages the reader may not read")


class LoopItemEvent(BaseModel):
    id: int
    kind: Literal[EVENT_KINDS]
    state: Literal[STATES]
    reason: Literal[ALL_REASONS] | None
    run: int | None
    figures: dict[str, items.Figure]
    at: datetime


class LoopItemChain(BaseModel):
    project: str
    item: LoopItem
    chain: list[LoopItem] = Field(description="every work item of its chain, each after the one it came of")
    path: list[int] = Field(description="the ids of the chain's work items from its first to the end of this one's")
    stages: LoopChainStages
    events: list[LoopItemEvent] = Field(description="what happened to this work item, oldest first")


async def _tree(conn: AsyncConnection, project_id: int, root: int) -> list:
    """The rows of every work item that came of ``root``, it first, then each after the one it came of."""
    i = tables.loop_items
    rows = await items.rows_of(conn, project_id, i.c.id == root)
    children: dict[int, list] = {}
    seen = {row.id for row in rows}
    frontier = sorted(seen)
    for _ in range(items.MAX_DEPTH):
        if not frontier:
            break
        found = [
            row for row in await items.rows_of(conn, project_id, i.c.parent_id.in_(frontier)) if row.id not in seen
        ]
        for row in sorted(found, key=lambda row: row.id):
            children.setdefault(row.parent_id, []).append(row)
        seen.update(row.id for row in found)
        frontier = sorted(row.id for row in found)
    ordered: list = []

    def walk(row) -> None:
        ordered.append(row)
        for child in children.get(row.id, []):
            walk(child)

    for row in rows:
        walk(row)
    return ordered


def _path(chain: Sequence[LoopItem], item_id: int) -> list[LoopItem]:
    """The chain's first work item down to ``item_id``, then on through each only child."""
    by_id = {entry.id: entry for entry in chain}
    up, at = [], by_id[item_id]
    while at is not None:
        up.append(at)
        at = by_id.get(at.parent) if at.parent is not None and at.parent not in {e.id for e in up} else None
    path = list(reversed(up))
    while True:
        below = [entry for entry in chain if entry.parent == path[-1].id]
        if len(below) != 1:
            return path
        path.append(below[0])


def _deepest(path: Sequence[LoopItem], attribute: str):
    return next((getattr(entry, attribute) for entry in reversed(path) if getattr(entry, attribute)), None)


async def _problem(conn: AsyncConnection, project_id: int, code: str | None) -> LoopChainProblem | None:
    number = problems.number_of(code) if code else None
    if number is None:
        return None
    p, r = tables.problems, tables.problem_revisions
    query = (
        select(r.c.statement, r.c.status)
        .join_from(p, r, (r.c.problem_id == p.c.id) & (r.c.revision == p.c.revision))
        .where(p.c.project_id == project_id, p.c.number == number)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else LoopChainProblem(code=code, statement=line(row.statement), status=row.status)


async def _goal(conn: AsyncConnection, project_id: int, code: str | None) -> LoopChainGoal | None:
    number = goals.number_of(code) if code else None
    if number is None:
        return None
    g, r = tables.goals, tables.goal_revisions
    query = (
        select(r.c.statement, r.c.status)
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .where(g.c.project_id == project_id, g.c.number == number)
    )
    row = (await conn.execute(query)).first()
    return None if row is None else LoopChainGoal(code=code, statement=line(row.statement), status=row.status)


async def _claim(conn: AsyncConnection, project_id: int, path: Sequence[LoopItem]) -> LoopChainClaim | None:
    ref = _deepest(path, "claim")
    if ref is None:
        return None
    c = tables.canvas.claims
    number = int(ref.code.removeprefix("CL-"))
    status = await conn.scalar(select(c.c.status).where(c.c.project_id == project_id, c.c.number == number))
    return LoopChainClaim(canvas=ref.canvas, code=ref.code, status=status or "assumption")


async def _use_case(conn: AsyncConnection, project_id: int, path: Sequence[LoopItem]) -> LoopChainUseCase | None:
    ref = _deepest(path, "use_case")
    if ref is None:
        return None
    d, v = tables.srs.documents, tables.srs.versions
    state = None
    if ref.version is not None:
        query = select(v.c.state).join_from(v, d, d.c.id == v.c.document_id)
        query = query.where(d.c.project_id == project_id, d.c.slug == ref.document, v.c.number == ref.version)
        state = await conn.scalar(query)
    return LoopChainUseCase(document=ref.document, code=ref.code, version=ref.version, state=state)


async def _task(conn: AsyncConnection, project_id: int, number: int | None) -> LoopChainTask | None:
    if number is None:
        return None
    t = tables.tasks.items
    row = (
        await conn.execute(select(t.c.title, t.c.status).where(t.c.project_id == project_id, t.c.number == number))
    ).first()
    if row is None:
        return None
    plugin = await enabled_in(conn, TASKS, project_id)
    groups = await status_groups(conn, plugin, project_id) if plugin is not None else {}
    return LoopChainTask(number=number, title=line(row.title), status=row.status, group=groups.get(row.status))


async def _plan(conn: AsyncConnection, project_id: int, plan_id: str | None) -> LoopChainPlan | None:
    if plan_id is None:
        return None
    p = tables.plans
    body = await conn.scalar(select(p.c.body).where(p.c.project_id == project_id, p.c.plan_id == plan_id))
    goal = (body or {}).get("goal") if isinstance(body, dict) else None
    return LoopChainPlan(id=plan_id, goal=line(goal, PLAN_LINE_CHARS) if isinstance(goal, str) else None)


async def _runs(conn: AsyncConnection, reading: Reading, path: Sequence[LoopItem]) -> tuple[list[LoopChainRun], bool]:
    """The runs handed out for the path's work items, each as the newest run that goes on from it; and whether the
    reader is kept from one of them."""
    e = tables.loop_item_events
    ids = [entry.id for entry in path]
    query = select(e.c.item_id, e.c.run_id).where(e.c.item_id.in_(ids), e.c.kind == DISPATCHED, e.c.run_id.is_not(None))
    handed = {run_id: item_id for item_id, run_id in await conn.execute(query.order_by(e.c.id))}
    if not handed:
        return [], False
    chains = await run_chains(conn, handed)
    newest = {chain[-1]: handed[origin] for origin, chain in chains.items()}
    r = tables.runs
    rows = await conn.execute(select(r.c.id, r.c.kind, r.c.state, r.c.plan_id).where(r.c.id.in_(sorted(newest))))
    found, kept = [], False
    for row in sorted(rows, key=lambda row: row.id):
        if reading.run(row.plan_id):
            found.append(LoopChainRun(id=row.id, kind=row.kind, state=row.state, work_item=newest[row.id]))
        else:
            kept = True
    return found, kept


async def _merges(conn: AsyncConnection, project_id: int, plan_id: str | None) -> list[LoopChainMerge]:
    if plan_id is None:
        return []
    q = tables.merge_queue
    query = select(q.c.id, q.c.repo, q.c.iid, q.c.state, q.c.web_url, q.c.merged_at)
    query = query.where(q.c.project_id == project_id, q.c.plan_id == plan_id).order_by(q.c.id)
    return [LoopChainMerge(**row._mapping) for row in await conn.execute(query)]


def _released(rows: Mapping[int, object], path: Sequence[LoopItem]) -> tuple[datetime | None, list[str]]:
    for entry in reversed(path):
        detail = getattr(rows.get(entry.id), "detail", None) or {}
        if entry.kind == "deliver" and entry.state == "done" and detail.get("released_at"):
            shas = [sha for sha in detail.get("after_sha") or [] if isinstance(sha, str)]
            return datetime.fromisoformat(detail["released_at"]), shas
    return None, []


def _proposals(rows: Mapping[int, object], path: Sequence[LoopItem]) -> int | None:
    for entry in path:
        detail = getattr(rows.get(entry.id), "detail", None) or {}
        if entry.kind == "brainstorm" and isinstance(detail.get("proposals"), int):
            return detail["proposals"]
    return None


async def _stages(conn: AsyncConnection, reading: Reading, path: Sequence[LoopItem], rows: Mapping) -> LoopChainStages:
    pid = reading.project_id
    hidden = [stage for stage in ("claim", "use_case", "task", "plan") if any(stage in e.hidden for e in path)]
    runs, kept = await _runs(conn, reading, path)
    plan_id = _deepest(path, "plan")
    released_at, after_sha = _released(rows, path)
    if kept:
        hidden.append("runs")
    if "plan" in hidden:
        hidden.append("merges")
    return LoopChainStages(
        problem=await _problem(conn, pid, _deepest(path, "problem")),
        goal=await _goal(conn, pid, _deepest(path, "goal")),
        proposals=_proposals(rows, path),
        claim=await _claim(conn, pid, path),
        use_case=await _use_case(conn, pid, path),
        task=await _task(conn, pid, _deepest(path, "task")),
        plan=await _plan(conn, pid, plan_id),
        runs=runs,
        merges=await _merges(conn, pid, plan_id),
        released_at=released_at,
        after_sha=after_sha,
        hidden=hidden,
    )


async def _events(conn: AsyncConnection, reading: Reading, item_id: int) -> list[LoopItemEvent]:
    e, r = tables.loop_item_events, tables.runs
    query = (
        select(e.c.id, e.c.kind, e.c.state, e.c.reason, e.c.run_id, e.c.detail, e.c.at, r.c.plan_id)
        .join_from(e, r, r.c.id == e.c.run_id, isouter=True)
        .where(e.c.item_id == item_id)
        .order_by(e.c.id)
    )
    return [
        LoopItemEvent(
            id=row.id,
            kind=row.kind,
            state=row.state,
            reason=row.reason,
            run=row.run_id if row.run_id is not None and reading.run(row.plan_id) else None,
            figures=items.figures(row.reason, row.detail or {}),
            at=row.at,
        )
        for row in await conn.execute(query)
    ]


async def shown(conn: AsyncConnection, reading: Reading, item_id: int) -> LoopItemChain:
    """Work item ``item_id`` of the project with its chain, the stages of its path and its ledger; 404 when the project
    has no such work item."""
    i = tables.loop_items
    found = await items.rows_of(conn, reading.project_id, i.c.id == item_id)
    if not found:
        raise HTTPException(404, f"project {reading.project} has no work item {item_id}")
    root = (await items.roots_of(conn, found))[item_id]
    rows = await _tree(conn, reading.project_id, root)
    chain = await items.described(conn, reading, rows)
    path = _path(chain, item_id)
    return LoopItemChain(
        project=reading.project,
        item=next(entry for entry in chain if entry.id == item_id),
        chain=chain,
        path=[entry.id for entry in path],
        stages=await _stages(conn, reading, path, {row.id: row for row in rows}),
        events=await _events(conn, reading, item_id),
    )


__all__ = ["LoopChainProblem", "LoopChainStages", "LoopItemChain", "LoopItemEvent", "shown"]

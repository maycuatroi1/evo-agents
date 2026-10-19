"""What a task links to: finding the target a member names (``Target``, ``resolve``), and showing each link with what
it points at now (``shown``, ``progress``, ``codes``).

- ``srs_item``: a coded item of the plugin srs (a use case, a business rule, a screen, an NFR, a common rule), named
  by any code or alias it holds (compared as ``srs_codes.code_key`` holds them) while the project enables srs. The link
  keeps the item's id, so a new code or title keeps it; it shows the item's code and its title in the latest version
  that has it. A project that turned srs off shows no such link.
- ``plan_step``: a step of a plan of the project, by plan id and step id; its title and status are read from the plan
  the hub holds each time, with the latest runs of that step.
- ``run``: a run of the project, with its kind and state.
- ``pull``: a pull or merge request, by URL.

A target the member cannot see (a document or a plan whose label is above the sink's clearance) is not linked; a link
to one shows ``hidden`` and nothing of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.tasks.settings import TasksError, srs_enabled
from evo_agents.core_plugins.tasks.store import visible
from evo_agents.hub import tables
from evo_agents.hub.plans import step_key
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.tables_srs import code_key
from evo_agents.hub.tables_tasks import MAX_STEP_CHARS, MAX_URL_CHARS

STEP_RUNS = 3  # the latest runs of a linked step a link shows
DONE = "done"  # a plan step's status once it is finished


@dataclass(frozen=True)
class Target:
    """A link's target, its columns as tasks_links holds them."""

    kind: str
    srs_item_id: int | None = None
    plan_id: str | None = None
    step_id: str | None = None
    run_id: int | None = None
    url: str | None = None
    name: str | None = field(default=None, compare=False)  # how the history names it

    def columns(self) -> dict:
        return {
            "kind": self.kind,
            "srs_item_id": self.srs_item_id,
            "plan_id": self.plan_id,
            "step_id": self.step_id,
            "run_id": self.run_id,
            "url": self.url,
        }


@dataclass(frozen=True)
class StepState:
    """A plan's step as the hub holds it now: None for what the member cannot see or the plan no longer has."""

    title: str | None
    status: str | None
    hidden: bool = False


@dataclass
class LinkView:
    """A link as a task shows it."""

    id: int
    kind: str
    created_by: str
    created_at: datetime
    hidden: bool = False
    code: str | None = None
    item_kind: str | None = None
    item_title: str | None = None
    document: str | None = None
    retired: bool = False
    plan_id: str | None = None
    step: str | None = None
    step_title: str | None = None
    step_status: str | None = None
    runs: list[dict] = field(default_factory=list)
    run_id: int | None = None
    run_kind: str | None = None
    run_state: str | None = None
    url: str | None = None


# Finding a target


async def srs_item(conn: AsyncConnection, access: ProjectAccess, sink: str | None, code: str) -> Target:
    """The coded SRS item of the project holding ``code`` (or an alias) that the member sees."""
    if not await srs_enabled(conn, access.project_id):
        raise TasksError(422, f"project {access.name} does not enable plugin srs: a task links to no SRS item there")
    c, i, d = tables.srs.codes, tables.srs.items, tables.srs.documents
    query = (
        select(i.c.id, i.c.code, i.c.retired_in, d.c.label)
        .select_from(c.join(i, i.c.id == c.c.item_id).join(d, d.c.id == i.c.document_id))
        .where(c.c.project_id == access.project_id, c.c.code_key == code_key(code))
    )
    row = (await conn.execute(query)).first()
    if row is None or not visible(access, row.label, sink):
        raise TasksError(422, f"no SRS item {code} in project {access.name} that you can see")
    if row.retired_in is not None:
        raise TasksError(422, f"SRS item {row.code} was dropped in version {row.retired_in}: link a current one")
    return Target("srs_item", srs_item_id=row.id, name=row.code)


async def plan_step(conn: AsyncConnection, access: ProjectAccess, sink: str | None, plan_id: str, step: str) -> Target:
    """Step ``step`` of plan ``plan_id`` of the project, which the member sees."""
    p = tables.plans
    query = select(p.c.label, p.c.body).where(p.c.project_id == access.project_id, p.c.plan_id == plan_id)
    row = (await conn.execute(query)).first()
    if row is None or not visible(access, row.label, sink):
        raise TasksError(422, f"no plan {plan_id} in project {access.name} that you can see")
    steps = row.body.get("steps") if isinstance(row.body.get("steps"), list) else []
    keys = [step_key(item, index) for index, item in enumerate(steps)]
    wanted = str(step).strip()
    if wanted not in keys or len(wanted) > MAX_STEP_CHARS:
        raise TasksError(422, f"plan {plan_id} has no step {wanted}; its steps are {', '.join(keys) or 'none'}")
    return Target("plan_step", plan_id=plan_id, step_id=wanted, name=f"{plan_id}#{wanted}")


async def run(conn: AsyncConnection, access: ProjectAccess, run_id: int) -> Target:
    r = tables.runs
    found = await conn.scalar(select(r.c.id).where(r.c.id == run_id, r.c.project_id == access.project_id))
    if found is None:
        raise TasksError(422, f"no run {run_id} in project {access.name}")
    return Target("run", run_id=run_id, name=str(run_id))


def pull(url: str) -> Target:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url.strip()) > MAX_URL_CHARS:
        raise TasksError(422, f"a pull or merge request is linked by its http(s) URL; {url!r} is none")
    return Target("pull", url=url.strip(), name=url.strip())


def split_step(given: str) -> tuple[str, str]:
    """``plan#step`` as (plan id, step id); 422 for any other shape."""
    plan_id, sep, step = given.partition("#")
    if not sep or not plan_id.strip() or not step.strip():
        raise TasksError(422, f"a plan step is named plan#step, as order-cancel#2; {given!r} is not")
    return plan_id.strip(), step.strip()


# Showing links


async def step_states(
    conn: AsyncConnection, project_id: int, pairs: set[tuple[str, str]], access: ProjectAccess | None, sink: str | None
) -> dict[tuple[str, str], StepState]:
    """(plan id, step id) -> the step as the plan holds it now. With ``access``, a plan the member cannot see is
    hidden; without, as the plugin reads it (granted plans:read)."""
    if not pairs:
        return {}
    p = tables.plans
    plan_ids = sorted({plan_id for plan_id, _ in pairs})
    query = select(p.c.plan_id, p.c.label, p.c.body).where(p.c.project_id == project_id, p.c.plan_id.in_(plan_ids))
    plans = {row.plan_id: row for row in await conn.execute(query)}
    found = {}
    for plan_id, step in pairs:
        row = plans.get(plan_id)
        if row is not None and access is not None and not visible(access, row.label, sink):
            found[(plan_id, step)] = StepState(None, None, hidden=True)
            continue
        steps = row.body.get("steps") if row is not None and isinstance(row.body.get("steps"), list) else []
        held = next((item for index, item in enumerate(steps) if step_key(item, index) == step), None)
        held = held if isinstance(held, dict) else {}
        found[(plan_id, step)] = StepState(held.get("title"), held.get("status"))
    return found


async def _step_links(conn: AsyncConnection, task_ids: list[int]) -> list:
    k = tables.tasks.links
    query = select(k.c.task_id, k.c.plan_id, k.c.step_id).where(
        k.c.task_id.in_(task_ids or [0]), k.c.kind == "plan_step"
    )
    return list(await conn.execute(query))


async def progress(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, task_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """task id -> (how many of the plan steps it links are done, how many it links that the member sees)."""
    rows = await _step_links(conn, task_ids)
    states = await step_states(conn, access.project_id, {(r.plan_id, r.step_id) for r in rows}, access, sink)
    found = {task_id: (0, 0) for task_id in task_ids}
    for row in rows:
        state = states[(row.plan_id, row.step_id)]
        if state.hidden:
            continue
        done, total = found[row.task_id]
        found[row.task_id] = (done + (state.status == DONE), total + 1)
    return found


async def codes(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, task_ids: list[int]
) -> dict[int, list[str]]:
    """task id -> the codes of the SRS items it links to that the member sees, in the order linked; none while the
    project does not enable srs."""
    found: dict[int, list[str]] = {task_id: [] for task_id in task_ids}
    if not task_ids or not await srs_enabled(conn, access.project_id):
        return found
    k, i, d = tables.tasks.links, tables.srs.items, tables.srs.documents
    query = (
        select(k.c.task_id, i.c.code, d.c.label)
        .join_from(k, i, i.c.id == k.c.srs_item_id)
        .join(d, d.c.id == i.c.document_id)
        .where(k.c.task_id.in_(task_ids), k.c.kind == "srs_item")
        .order_by(k.c.task_id, k.c.id)
    )
    for row in await conn.execute(query):
        if row.code and visible(access, row.label, sink) and row.code not in found[row.task_id]:
            found[row.task_id].append(row.code)
    return found


def _srs_query(item_ids: list[int]):
    i, d, iv, v = tables.srs.items, tables.srs.documents, tables.srs.item_versions, tables.srs.versions
    title = (
        select(iv.c.title)
        .join(v, v.c.id == iv.c.version_id)
        .where(iv.c.item_id == i.c.id)
        .order_by(v.c.number.desc())
        .limit(1)
        .scalar_subquery()
    )
    return (
        select(i.c.id, i.c.code, i.c.kind, i.c.retired_in, d.c.slug, d.c.label, title.label("title"))
        .join_from(i, d, d.c.id == i.c.document_id)
        .where(i.c.id.in_(item_ids or [0]))
    )


async def _step_runs(conn: AsyncConnection, project_id: int, pairs: set[tuple[str, str]]) -> dict:
    """(plan id, step id) -> its latest STEP_RUNS runs, the newest first."""
    if not pairs:
        return {}
    r = tables.runs
    plan_ids = sorted({plan_id for plan_id, _ in pairs})
    ranked = func.row_number().over(partition_by=(r.c.plan_id, r.c.step_key), order_by=r.c.id.desc()).label("rank")
    inner = (
        select(r.c.id, r.c.state, r.c.plan_id, r.c.step_key, ranked)
        .where(r.c.project_id == project_id, r.c.plan_id.in_(plan_ids))
        .subquery()
    )
    query = select(inner).where(inner.c.rank <= STEP_RUNS).order_by(inner.c.id.desc())
    found: dict[tuple[str, str], list[dict]] = {}
    for row in await conn.execute(query):
        if (row.plan_id, row.step_key) in pairs:
            found.setdefault((row.plan_id, row.step_key), []).append({"id": row.id, "state": row.state})
    return found


async def _rows(conn: AsyncConnection, task_id: int, with_srs: bool) -> list:
    k, u = tables.tasks.links, tables.users
    query = (
        select(k, u.c.login.label("created_by_login"))
        .join_from(k, u, u.c.id == k.c.created_by)
        .where(k.c.task_id == task_id)
        .order_by(k.c.id)
    )
    if not with_srs:
        query = query.where(k.c.kind != "srs_item")
    return list(await conn.execute(query))


async def shown(conn: AsyncConnection, access: ProjectAccess, sink: str | None, task_id: int) -> list[LinkView]:
    """The links of a task, in the order they were made, each with what it points at now."""
    rows = await _rows(conn, task_id, await srs_enabled(conn, access.project_id))
    item_ids = [row.srs_item_id for row in rows if row.kind == "srs_item"]
    items = {row.id: row for row in await conn.execute(_srs_query(item_ids))} if item_ids else {}
    pairs = {(row.plan_id, row.step_id) for row in rows if row.kind == "plan_step"}
    states = await step_states(conn, access.project_id, pairs, access, sink)
    step_runs = await _step_runs(conn, access.project_id, pairs)
    r = tables.runs
    run_ids = [row.run_id for row in rows if row.kind == "run"]
    runs = {row.id: row for row in await conn.execute(select(r.c.id, r.c.kind, r.c.state).where(r.c.id.in_(run_ids)))}
    views = []
    for row in rows:
        view = LinkView(row.id, row.kind, row.created_by_login, row.created_at)
        if row.kind == "srs_item":
            item = items[row.srs_item_id]
            if visible(access, item.label, sink):
                view.code, view.item_kind, view.item_title, view.document = item.code, item.kind, item.title, item.slug
                view.retired = item.retired_in is not None
            else:
                view.hidden = True
        elif row.kind == "plan_step":
            state = states[(row.plan_id, row.step_id)]
            view.hidden = state.hidden
            if not state.hidden:
                view.plan_id, view.step, view.step_title, view.step_status = (
                    row.plan_id,
                    row.step_id,
                    state.title,
                    state.status,
                )
                view.runs = step_runs.get((row.plan_id, row.step_id), [])
        elif row.kind == "run":
            held = runs.get(row.run_id)
            view.run_id = row.run_id
            view.run_kind, view.run_state = (held.kind, held.state) if held else (None, None)
        else:
            view.url = row.url
        views.append(view)
    return views


__all__ = [
    "DONE",
    "LinkView",
    "StepState",
    "Target",
    "codes",
    "plan_step",
    "progress",
    "pull",
    "run",
    "shown",
    "split_step",
    "srs_item",
    "step_states",
]

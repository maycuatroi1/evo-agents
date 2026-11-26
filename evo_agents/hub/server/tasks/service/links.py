"""What a task links to: finding the target a member names (``srs_item``, ``plan_step``, ``run``, ``pull``), and
showing each link with what it points at now (``shown``, ``progress``, ``codes``).

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

from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.plans import step_key
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.tasks.models.rows import DONE, LinkView, StepState, Target
from evo_agents.hub.server.tasks.models.values import TasksError, visible
from evo_agents.hub.server.tasks.repository import links as repository
from evo_agents.hub.server.tasks.service.project import srs_enabled
from evo_agents.hub.tables_tasks import MAX_STEP_CHARS, MAX_URL_CHARS

# Finding a target


async def srs_item(conn: AsyncConnection, access: ProjectAccess, sink: str | None, code: str) -> Target:
    """The coded SRS item of the project holding ``code`` (or an alias) that the member sees."""
    if not await srs_enabled(conn, access.project_id):
        raise TasksError(422, f"project {access.name} does not enable plugin srs: a task links to no SRS item there")
    row = await repository.srs_item(conn, access.project_id, code)
    if row is None or not visible(access, row.label, sink):
        raise TasksError(422, f"no SRS item {code} in project {access.name} that you can see")
    if row.retired_in is not None:
        raise TasksError(422, f"SRS item {row.code} was dropped in version {row.retired_in}: link a current one")
    return Target("srs_item", srs_item_id=row.id, name=row.code)


async def plan_step(conn: AsyncConnection, access: ProjectAccess, sink: str | None, plan_id: str, step: str) -> Target:
    """Step ``step`` of plan ``plan_id`` of the project, which the member sees."""
    row = await repository.plan(conn, access.project_id, plan_id)
    if row is None or not visible(access, row.label, sink):
        raise TasksError(422, f"no plan {plan_id} in project {access.name} that you can see")
    steps = row.body.get("steps") if isinstance(row.body.get("steps"), list) else []
    keys = [step_key(item, index) for index, item in enumerate(steps)]
    wanted = str(step).strip()
    if wanted not in keys or len(wanted) > MAX_STEP_CHARS:
        raise TasksError(422, f"plan {plan_id} has no step {wanted}; its steps are {', '.join(keys) or 'none'}")
    return Target("plan_step", plan_id=plan_id, step_id=wanted, name=f"{plan_id}#{wanted}")


async def run(conn: AsyncConnection, access: ProjectAccess, run_id: int) -> Target:
    if await repository.run_in(conn, access.project_id, run_id) is None:
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
    plans = await repository.plans(conn, project_id, sorted({plan_id for plan_id, _ in pairs}))
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


async def progress(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, task_ids: list[int]
) -> dict[int, tuple[int, int]]:
    """task id -> (how many of the plan steps it links are done, how many it links that the member sees)."""
    rows = await repository.step_links(conn, task_ids)
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
    for row in await repository.srs_codes(conn, task_ids):
        if row.code and visible(access, row.label, sink) and row.code not in found[row.task_id]:
            found[row.task_id].append(row.code)
    return found


def _view(row, items: dict, states: dict, step_runs: dict, runs: dict, access: ProjectAccess, sink) -> LinkView:
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
    return view


async def shown(conn: AsyncConnection, access: ProjectAccess, sink: str | None, task_id: int) -> list[LinkView]:
    """The links of a task, in the order they were made, each with what it points at now."""
    rows = await repository.rows(conn, task_id, await srs_enabled(conn, access.project_id))
    items = await repository.srs_items(conn, [row.srs_item_id for row in rows if row.kind == "srs_item"])
    pairs = {(row.plan_id, row.step_id) for row in rows if row.kind == "plan_step"}
    states = await step_states(conn, access.project_id, pairs, access, sink)
    step_runs = await repository.step_runs(conn, access.project_id, pairs)
    runs = await repository.runs(conn, [row.run_id for row in rows if row.kind == "run"])
    return [_view(row, items, states, step_runs, runs, access, sink) for row in rows]


__all__ = [
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

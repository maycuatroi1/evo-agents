"""Listing and showing runs: GET /v1/projects/{p}/runs, a page of the project's runs newest first with how many are
in each state under the other filters, and GET .../runs/{id}."""

from __future__ import annotations

import functools
import re

from sqlalchemy import Boolean, Integer, String, and_, bindparam, func, or_, select

from evo_agents.hub import runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import Run, RunList, StateCounts
from evo_agents.hub.server.runs.service.views import (
    readable_run,
    run_select,
    run_view,
    runs_of,
    sees_unplanned,
    visible_plans,
)
from evo_agents.hub.server.security import Principal

RUN_NUMBER = re.compile(r"#?([0-9]{1,18})")


def _like(text: str) -> str:
    """``text`` as an ILIKE pattern that matches it anywhere, its own wildcards taken literally."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _list_conditions(*, plan_id: bool, step: bool, worker_id: bool, login: bool, text: bool, number: bool) -> list:
    """The filters of GET .../runs but the state's, which the counts by state leave out: the runs of project
    :project_id and of the plans in :plans, and its author runs of a new plan when :unplanned, then each filter given,
    by its bind parameter (one not given leaves every run): :plan_id, :step, :worker_id, :login, and the text as the
    ILIKE :pattern, or as the run :number."""
    r, u, w = tables.runs, tables.users, tables.workers
    unplanned = and_(r.c.kind == "author", r.c.plan_id.is_(None), bindparam("unplanned", type_=Boolean))
    found = [r.c.project_id == bindparam("project_id"), or_(one_of(r.c.plan_id, name="plans"), unplanned)]
    if plan_id:
        found.append(r.c.plan_id == bindparam("plan_id"))
    if step:
        found.append(r.c.step_key == bindparam("step"))
    if worker_id:
        found.append(r.c.worker_id == bindparam("worker_id"))
    if login:
        found.append(func.lower(u.c.login) == func.lower(bindparam("login", type_=String)))
    if text:
        pattern = bindparam("pattern")
        searched = (r.c.title, r.c.step_key, r.c.plan_id, r.c.repo, r.c.branch, w.c.name, u.c.login, r.c.error)
        matches = [searched_column.ilike(pattern) for searched_column in searched]
        if number:
            matches.append(r.c.id == bindparam("number"))
        found.append(or_(*matches))
    return found


@functools.cache
def _list_statements(states: bool, **shape: bool):
    """The page and the counts by state of GET .../runs for the filters ``shape`` names, built once per shape: a
    page takes :limit and :offset, and :states when ``states``."""
    r, u, w = tables.runs, tables.users, tables.workers
    conditions = _list_conditions(**shape)
    page = run_select().where(*conditions)
    if states:
        page = page.where(one_of(r.c.state, name="states"))
    limit, offset = bindparam("limit", type_=Integer), bindparam("offset", type_=Integer)
    page = page.order_by(r.c.id.desc()).limit(limit).offset(offset)
    counts = (
        select(r.c.state, func.count().label("runs"))
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .outerjoin(w, w.c.id == r.c.worker_id)
        .where(*conditions)
        .group_by(r.c.state)
    )
    return page, counts


async def run_list(
    engine,
    user: Principal,
    project: str,
    *,
    state: list[str],
    plan_id: str | None,
    step: str | None,
    worker_id: int | None,
    dispatched_by: str | None,
    q: str | None,
    limit: int,
    offset: int,
    sink: str | None,
) -> RunList:
    """The project's runs, newest first, of the plans the caller may read, with how many are in each state."""
    filters = {"plan_id": plan_id, "step": step, "worker_id": worker_id, "login": dispatched_by}
    states = list(dict.fromkeys(state))
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        text = q.strip() if q else None
        number = RUN_NUMBER.fullmatch(text) if text else None
        shape = {name: value is not None for name, value in filters.items()}
        page_query, counts_query = _list_statements(bool(states), **shape, text=bool(text), number=bool(number))
        bound = {
            **filters,
            "project_id": access.project_id,
            "plans": plans,
            "unplanned": sees_unplanned(access, sink),
            "pattern": _like(text) if text else None,
            "number": int(number[1]) if number else None,
            "states": states,
            "limit": limit,
            "offset": offset,
        }
        page = runs_of(await conn.execute(page_query, bound))
        counts = {row.state: row.runs for row in await conn.execute(counts_query, bound)}
    wanted = states or runs.RUN_STATES
    return RunList(
        runs=page,
        total=sum(counts.get(name, 0) for name in wanted),
        counts=StateCounts(**counts),
        limit=limit,
        offset=offset,
    )


async def shown_run(engine, user: Principal, project: str, run_id: int, sink: str | None) -> Run:
    """Run ``run_id`` of ``project``, when the caller may read it (``readable_run``)."""
    async with engine.begin() as conn:
        await readable_run(conn, user, project, run_id, sink)
        return await run_view(conn, run_id)

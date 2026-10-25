"""Listing and showing runs: GET /v1/projects/{p}/runs, a page of the project's runs newest first with how many are
in each state under the other filters, and GET .../runs/{id}.

With ``group=session`` the list holds each chat session once, as its latest run: a session is listed when any of its
runs matches the search, and its state is its latest run's, which the state filter and the counts read, so the counts
and the total count what the pages list. Every other run is listed as it is. ``session_run_id`` lists the runs of one
chat session alone, the turns a grouped row stands for."""

from __future__ import annotations

import functools
import re

from sqlalchemy import Boolean, Integer, String, and_, bindparam, func, or_, select

from evo_agents.hub import chat_runs, runs, tables
from evo_agents.hub.db import one_of
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models import PlanProgress, Run, RunList, StateCounts
from evo_agents.hub.server.runs.service.views import (
    chat_session,
    current_plan,
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


def _searched(number: bool):
    """Whether a run matches the text of GET .../runs: the ILIKE :pattern in one of its fields, or, when ``number``, its
    id is the run :number."""
    r, u, w = tables.runs, tables.users, tables.workers
    pattern = bindparam("pattern")
    searched = (
        r.c.title,
        r.c.step_key,
        r.c.plan_id,
        r.c.repo,
        r.c.branch,
        w.c.name,
        u.c.login,
        r.c.error,
        r.c.machine,
        r.c.cwd,
        r.c.claude_email,
    )
    matches = [searched_column.ilike(pattern) for searched_column in searched]
    if number:
        matches.append(r.c.id == bindparam("number"))
    return or_(*matches)


def _list_conditions(
    *,
    plan_id: bool,
    step: bool,
    worker_id: bool,
    login: bool,
    kind: bool,
    account: bool,
    session: bool,
    text: bool,
    number: bool,
) -> list:
    """The filters of GET .../runs but the state's, which the counts by state leave out: the runs of project
    :project_id and of the plans in :plans, and its author runs of a new plan and chat, task and browser runs on no plan when
    :unplanned, and the local runs of :viewer, then each filter given,
    by its bind parameter (one not given leaves every run): :plan_id, :step, :worker_id, :login, :kind, :account (the
    Claude account of a local run, by email), :session (the branch of a chat session, the runs of that session), and
    the text (``_searched``)."""
    r, u = tables.runs, tables.users
    unplanned = and_(
        r.c.kind.in_(("author", "chat", "task", "browser")),
        r.c.plan_id.is_(None),
        bindparam("unplanned", type_=Boolean),
    )
    # A local run is its member's alone (:viewer): only they list it, whatever its plan.
    mine = and_(r.c.kind == "local", r.c.dispatched_by == bindparam("viewer"))
    others = and_(r.c.kind != "local", or_(one_of(r.c.plan_id, name="plans"), unplanned))
    found = [r.c.project_id == bindparam("project_id"), or_(mine, others)]
    if plan_id:
        found.append(r.c.plan_id == bindparam("plan_id"))
    if step:
        found.append(r.c.step_key == bindparam("step"))
    if worker_id:
        found.append(r.c.worker_id == bindparam("worker_id"))
    if login:
        found.append(func.lower(u.c.login) == func.lower(bindparam("login", type_=String)))
    if kind:
        found.append(r.c.kind == bindparam("kind", type_=String))
    if account:
        found.append(func.lower(r.c.claude_email) == func.lower(bindparam("account", type_=String)))
    if session:
        found.extend([r.c.kind == "chat", r.c.branch == bindparam("session", type_=String)])
    if text:
        found.append(_searched(number))
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


@functools.cache
def _grouped_statements(states: bool, *, text: bool, number: bool, **shape: bool):
    """``_list_statements`` of ``group=session``: the latest run of each chat session that matches the filters, and
    every other run that does, the search matched by any run of a session; the page and the counts by state of them."""
    r, u, w = tables.runs, tables.users, tables.workers
    entry = func.coalesce(chat_session(r), r.c.id)  # a chat session, or a run of its own
    columns = [r.c.id, r.c.state, func.row_number().over(partition_by=entry, order_by=r.c.id.desc()).label("nth")]
    if text:  # a session any of whose runs the text finds
        columns.append(func.bool_or(_searched(number)).over(partition_by=entry).label("matched"))
    listed = (
        select(*columns)
        .join_from(r, u, u.c.id == r.c.dispatched_by)
        .outerjoin(w, w.c.id == r.c.worker_id)
        .where(*_list_conditions(**shape, text=False, number=False))
        .cte("listed")
    )
    shown = [listed.c.nth == 1, *([listed.c.matched] if text else [])]
    latest = select(listed.c.id).where(*shown)
    if states:
        latest = latest.where(one_of(listed.c.state, name="states"))
    limit, offset = bindparam("limit", type_=Integer), bindparam("offset", type_=Integer)
    page = run_select().where(r.c.id.in_(latest)).order_by(r.c.id.desc()).limit(limit).offset(offset)
    counts = select(listed.c.state, func.count().label("runs")).where(*shown).group_by(listed.c.state)
    return page, counts


def plan_progress(plan: dict) -> PlanProgress | None:
    steps = [step for step in plan.get("steps") or [] if isinstance(step, dict)]
    if not steps:
        return None
    status = [str(step.get("status", "pending")) for step in steps]
    done = status.count("done")
    at = next((i for i, s in enumerate(status) if s == "in_progress"), None)
    if at is None:
        at = next((i for i, s in enumerate(status) if s != "done"), None)
    step = None if at is None else steps[at]
    name = None if step is None else str(step.get("title") or step.get("id") or step.get("order") or at + 1)
    return PlanProgress(
        done=done,
        total=len(steps),
        percent=done * 100 // len(steps),
        current_step=name,
        current_status=None if at is None else status[at],
    )


async def _with_progress(conn, project_id: int, page: list[Run]) -> list[Run]:
    """``page`` with the progress of the plan of each plan run in it."""
    progress: dict[str, PlanProgress | None] = {}
    for run in page:
        if run.kind == "plan" and run.plan_id and run.plan_id not in progress:
            found = await current_plan(conn, project_id, run.plan_id)
            body = found[0] if found else None
            progress[run.plan_id] = plan_progress(body) if isinstance(body, dict) else None
    return [
        run.model_copy(update={"plan_progress": progress.get(run.plan_id)}) if run.kind == "plan" else run
        for run in page
    ]


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
    kind: str | None = None,
    account: str | None = None,
    session_run_id: int | None = None,
    group: str | None = None,
) -> RunList:
    """The project's runs, newest first, of the plans the caller may read, with how many are in each state; with
    ``group`` "session", each chat session once (see the module's docstring)."""
    filters = {
        "plan_id": plan_id,
        "step": step,
        "worker_id": worker_id,
        "login": dispatched_by,
        "kind": kind,
        "account": account,
        "session": None if session_run_id is None else chat_runs.branch_of(session_run_id),
    }
    states = list(dict.fromkeys(state))
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        plans = await visible_plans(conn, access, sink)
        text = q.strip() if q else None
        number = RUN_NUMBER.fullmatch(text) if text else None
        shape = {name: value is not None for name, value in filters.items()}
        statements = _grouped_statements if group == "session" else _list_statements
        page_query, counts_query = statements(bool(states), **shape, text=bool(text), number=bool(number))
        bound = {
            **filters,
            "project_id": access.project_id,
            "plans": plans,
            "unplanned": sees_unplanned(access, sink),
            "viewer": user.user_id,
            "pattern": _like(text) if text else None,
            "number": int(number[1]) if number else None,
            "states": states,
            "limit": limit,
            "offset": offset,
        }
        page = runs_of(await conn.execute(page_query, bound))
        counts = {row.state: row.runs for row in await conn.execute(counts_query, bound)}
        page = await _with_progress(conn, access.project_id, page)
    wanted = states or runs.SHOWN_STATES
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

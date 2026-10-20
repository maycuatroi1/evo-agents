"""The job curator.replay_suite: each project's suite of replay tasks, made from its own history
(``evo_agents.hub.replay``).

Every hour it reads the plans of each project that has a charter, and adds a row of replay_tasks for each step that
gives a task (``replay.task_of``) and has none yet: a step done, with a verify of one line and a repo, whose evidence
names the commits it made. A step whose evidence names none takes the commits the runs of it reported: a run of that one
step that ended done, or a step report of a plan run (``step_report`` of its events, while they are kept). A task once
in the suite is never changed by the job, nor dropped when its plan changes or goes, so the split of the suite holds:
new tasks are only ever added, each with the split its plan and step give under the hub's session secret
(``replay.split_of``), and the plan's label then. At most MAX_NEW_TASKS are added to a project in one pass; the next
pass adds the rest. A hub without EVO_HUB_SESSION_SECRET adds none, since a split without the secret could be
computed from the code.

It lives outside ``evo_agents.hub.server``, so ``hub worker`` runs it without importing the server.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import replay, tables
from evo_agents.hub.plans import step_key

log = logging.getLogger(__name__)

MAX_NEW_TASKS = 500  # tasks one pass adds to one project


def _projects():
    c = tables.charters
    return select(c.c.project_id).distinct().order_by(c.c.project_id)


async def _known(conn: AsyncConnection, project_id: int) -> set[tuple[str, str]]:
    t = tables.replay_tasks
    rows = await conn.execute(select(t.c.plan_id, t.c.step_key).where(t.c.project_id == project_id))
    return {(row.plan_id, row.step_key) for row in rows}


async def run_commits(conn: AsyncConnection, project_id: int) -> dict[tuple[str, str], list[str]]:
    """The commits the runs of each step of the project's plans reported, oldest run first: a run of one step that
    ended done with a commit, then the step reports of plan runs that say done with one."""
    r, e = tables.runs, tables.run_events
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    steps = (
        select(r.c.plan_id, r.c.step_key, r.c.commit_sha)
        .where(
            r.c.project_id == project_id,
            r.c.kind == "step",
            r.c.state == "done",
            r.c.commit_sha.is_not(None),
            r.c.plan_id.is_not(None),
        )
        .order_by(r.c.id)
    )
    for row in await conn.execute(steps):
        found[(row.plan_id, row.step_key)].append(row.commit_sha)
    report = e.c.body["step_report"]
    reports = (
        select(r.c.plan_id, report["step"].astext.label("step"), report["commit_sha"].astext.label("sha"))
        .join_from(e, r, r.c.id == e.c.run_id)
        .where(
            r.c.project_id == project_id,
            r.c.kind == "plan",
            r.c.plan_id.is_not(None),
            report["status"].astext == "done",
            report["commit_sha"].astext.is_not(None),
        )
        .order_by(r.c.id, e.c.seq)
    )
    for row in await conn.execute(reports):
        if row.step is not None and row.sha:
            found[(row.plan_id, row.step)].append(row.sha)
    return found


def new_tasks(plans: Iterable, known: set[tuple[str, str]], commits: dict, secret: str) -> list[dict]:
    """The tasks of ``plans`` (rows of plan_id, body and label) that ``known`` does not have yet, in plan order, split
    under ``secret``."""
    found = []
    for plan in plans:
        steps = plan.body.get("steps") if isinstance(plan.body, dict) else None
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            if (plan.plan_id, step_key(step, index)) in known:
                continue
            done = commits.get((plan.plan_id, step_key(step, index)), ())
            task = replay.task_of(plan.plan_id, plan.body, index, done, secret=secret)
            if task is not None:
                found.append({**task, "label": plan.label})
    return found


async def build_suite(conn: AsyncConnection, project_id: int, secret: str) -> int:
    """Add the new tasks of project ``project_id`` to its suite, split under ``secret``, at most MAX_NEW_TASKS; how
    many were added."""
    pl = tables.plans
    plans = (
        await conn.execute(
            select(pl.c.plan_id, pl.c.body, pl.c.label).where(pl.c.project_id == project_id).order_by(pl.c.plan_id)
        )
    ).all()
    known, commits = await _known(conn, project_id), await run_commits(conn, project_id)
    tasks = new_tasks(plans, known, commits, secret)[:MAX_NEW_TASKS]
    if not tasks:
        return 0
    t = tables.replay_tasks
    statement = (
        pg_insert(t)
        .values([{"project_id": project_id, **task} for task in tasks])
        .on_conflict_do_nothing(index_elements=[t.c.project_id, t.c.plan_id, t.c.step_key])
        .returning(t.c.id)
    )
    return len((await conn.execute(statement)).all())


async def build_suites(engine: AsyncEngine, secret: str | None) -> dict:
    """One pass of curator.replay_suite over each project that has a charter, each in a transaction of its own, its
    tasks split under ``secret`` (the hub's session secret); how many projects it read and how many tasks it added.
    Without the secret it reads none."""
    if not secret:
        log.warning("replay suites not built: EVO_HUB_SESSION_SECRET keys their split and is not set")
        return {"projects": 0, "added": 0, "failed": 0}
    async with engine.begin() as conn:
        projects = [row.project_id for row in await conn.execute(_projects())]
    added = failed = 0
    for project_id in projects:
        try:
            async with engine.begin() as conn:
                added += await build_suite(conn, project_id, secret)
        except Exception:  # one project failing leaves the others their turn
            log.exception("a project's replay suite was not built", extra={"project_id": project_id})
            failed += 1
    result = {"projects": len(projects), "added": added, "failed": failed}
    log.info("replay suites built", extra=result)
    return result

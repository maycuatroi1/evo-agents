"""The caller's overview, for the web's Home (``evo_agents.hub.server.overview``): what the caller sees
(``scope``), the runs whose agent works by member (``running_by_member``), and the overview itself (``overview``), in
one transaction of a ``UnitOfWork``."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta

from fastapi import HTTPException
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.curator.service.status import curator_overview
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models.overview import (
    DAYS,
    RUNNING_STATES,
    DayCount,
    Overview,
    OverviewAuthorWait,
    OverviewCounts,
    OverviewDecision,
    OverviewProject,
    OverviewRun,
)
from evo_agents.hub.server.runs.repository import overview as repository
from evo_agents.hub.server.runs.service.views import sees_unplanned, visible_plans

log = logging.getLogger("evo_agents.hub.server.overview")


async def granted_access(conn: AsyncConnection, user) -> list[ProjectAccess]:
    """The caller's access to each project it holds a grant on, by the check of the project's runs and decisions; a
    grant revoked since the list was read leaves its project out."""
    accesses = []
    for name in await repository.granted_names(conn, user.user_id):
        try:
            accesses.append(await project_access(conn, user, name))
        except HTTPException:
            continue
    return [access for access in accesses if access.role is not None]


async def scope(conn: AsyncConnection, user) -> tuple[list[ProjectAccess], list[tuple[int, str]], list[int], list[int]]:
    """What the caller sees: their access to each project they hold a grant on, the visible (project_id, plan_id)
    pairs, the projects granted and those whose runs on no plan they see; the parameters ``parameters`` takes."""
    accesses = await granted_access(conn, user)
    pairs = []
    for access in accesses:
        pairs += [(access.project_id, plan_id) for plan_id in await visible_plans(conn, access, None)]
    granted = [access.project_id for access in accesses]
    unplanned = [access.project_id for access in accesses if sees_unplanned(access, None)]
    return accesses, pairs, granted, unplanned


async def running_by_member(conn: AsyncConnection, user) -> list[tuple[str, str, int]]:
    """The runs whose agent works now (RUNNING_STATES) that the caller sees, as the overview counts them, over the
    projects they hold a grant on: (login of the owner, project, runs), by login and project. The org chart's page
    shows them on each person's seat (GET /v1/org/runs)."""
    _, pairs, granted, unplanned = await scope(conn, user)
    bound = repository.parameters(user.user_id, pairs, granted, unplanned, datetime.now(UTC))
    rows = await repository.running_rows(conn, bound)
    return [(row.login, row.project, row.running) for row in rows]


def _run(row) -> OverviewRun:
    fields = dict(row._mapping)
    if fields["kind"] != "plan":
        fields["steps_done"] = fields["steps_total"] = None
    fields["plan_id"] = fields["plan_id"] or ""  # a local run on no plan, as Run shows one
    return OverviewRun(**fields)


def _parks_at(state: str, waiting_since: datetime | None, parked_at: datetime | None, wait: timedelta):
    if state == "waiting" and waiting_since is not None:
        return waiting_since + wait
    return parked_at if state == "parked" else None


def parks_at(fields: dict, wait: timedelta) -> datetime | None:
    """When the run of a decision parks for want of an answer, from its row's ``run_kind``, ``run_state``,
    ``waiting_since`` and ``parked_at``, which it takes out of ``fields``: ``wait`` after it started waiting, when it
    parked, or None while its agent works and for a local run."""
    waiting_since, parked_at = fields.pop("waiting_since"), fields.pop("parked_at")
    kind = fields.pop("run_kind")
    if kind == "local":  # no worker holds it and nothing parks it: it waits as long as it takes
        return None
    return _parks_at(fields["run_state"], waiting_since, parked_at, wait)


def _decision(row, wait: timedelta) -> OverviewDecision:
    fields = dict(row._mapping)
    parks = parks_at(fields, wait)
    return OverviewDecision(**fields, parks_at=parks)


def _counts(counted: list[Row], decision_counts: list[Row]) -> tuple[OverviewCounts, dict[date, int]]:
    """The overview's counts, and the runs that ended done on each day."""
    by_state: dict[str, int] = {}
    done_on: dict[date, int] = {}
    for state, day, count in counted:
        by_state[state] = by_state.get(state, 0) + count
        if state == "done":
            done_on[day] = count
    counts = OverviewCounts(
        waiting_on_you=sum(yours for _, _, yours in decision_counts),
        running=sum(by_state.get(state, 0) for state in RUNNING_STATES),
        queued=by_state.get("queued", 0),
        done_7d=by_state.get("done", 0),
        failed_7d=by_state.get("failed", 0),
        lost_7d=by_state.get("lost", 0),
    )
    return counts, done_on


def _projects(accesses: list[ProjectAccess], rows: repository.OverviewRows, curators: dict) -> list[OverviewProject]:
    open_by_project = {project_id: total for project_id, total, _ in rows.decision_counts}
    repos_and_plans = {project_id: (repos, plans) for project_id, repos, plans in rows.project_counts}
    return [
        OverviewProject(
            name=access.name,
            role=access.role,  # pyright: ignore[reportArgumentType]
            max_level=access.max_level,  # pyright: ignore[reportArgumentType]
            repos=repos_and_plans.get(access.project_id, (0, 0))[0],
            active_plans=repos_and_plans.get(access.project_id, (0, 0))[1],
            open_decisions=open_by_project.get(access.project_id, 0),
            curator=curators.get(access.project_id),
        )
        for access in accesses
    ]


async def overview(engine: AsyncEngine, user, wait: timedelta) -> Overview:
    """What waits for ``user``, what runs and what ended lately, over the projects they hold a grant on, on a hub
    whose decisions park after ``wait``."""
    async with UnitOfWork(engine) as conn:
        today = await repository.utc_today(conn)
        accesses, pairs, granted, unplanned = await scope(conn, user)
        since = datetime.combine(today - timedelta(days=DAYS - 1), time.min, tzinfo=UTC)
        bound = repository.parameters(user.user_id, pairs, granted, unplanned, since)
        rows = await repository.overview_rows(conn, bound)
        curators = await curator_overview(conn, accesses)

    counts, done_on = _counts(rows.counted, rows.decision_counts)
    days = [today - timedelta(days=back) for back in range(DAYS - 1, -1, -1)]
    projects = _projects(accesses, rows, curators)
    log.debug("overview", extra={"login": user.login, "projects": len(projects), "plans": len(pairs)})
    return Overview(
        counts=counts,
        done_by_day=[DayCount(day=day, done=done_on.get(day, 0)) for day in days],
        active_runs=[_run(row) for row in rows.active],
        recent_runs=[_run(row) for row in rows.recent],
        open_decisions=[_decision(row, wait) for row in rows.decisions],
        author_waiting=[OverviewAuthorWait(**row._mapping) for row in rows.author_waiting],
        projects=projects,
    )

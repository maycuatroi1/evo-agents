"""Where a project's night shift stands: its state in a word, the proposals waiting, the night now and the latest
nights, and pausing and resuming its schedules (see the package)."""

from __future__ import annotations

from collections import Counter

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator
from evo_agents.hub.access import has_role
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden
from evo_agents.hub.server.curator.models.charter import (
    NIGHT_SHIFT,
    PAUSE,
    RESUME,
    BriefSummary,
    CuratorOverview,
    CuratorState,
    CuratorStatus,
    Night,
    NightList,
    NightReview,
    NightSummary,
    ReviewRunSummary,
    Schedule,
)
from evo_agents.hub.server.curator.repository import charter as charters
from evo_agents.hub.server.curator.repository import status as store
from evo_agents.hub.server.curator.service.charter import charter_view, read_access, reader
from evo_agents.hub.server.night_cost import NightFigures, night_figures
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.shift_hours import cancel_queued


def curator_state(paused: bool, active_run_id: int | None, in_window: bool) -> CuratorState:
    """One of CURATOR_STATES: paused first, then a run in flight, then the window."""
    if paused:
        return "paused"
    if active_run_id is not None:
        return "running"
    return "on_duty" if in_window else "idle"


async def open_proposals(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, int]:
    """Per project of ``accesses``, its open proposals whose label the access reaches through the project's hub
    sink, as GET .../curator/proposals reads them."""
    by_id = {access.project_id: access for access in accesses}
    if not by_id:
        return {}
    counted: Counter[int] = Counter()
    for project_id, label in await store.open_proposal_labels(conn, list(by_id)):
        access = by_id[project_id]
        if plan_service.sees(access, label, None):
            counted[project_id] += 1
    return dict(counted)


async def curator_overview(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, CuratorOverview]:
    """Where the Curator of each project of ``accesses`` that has a charter stands, in three statements built once
    for all of them."""
    ids = [access.project_id for access in accesses]
    if not ids:
        return {}
    charters_of = {row.project_id: row for row in await store.newest_charters(conn, ids)}
    if not charters_of:
        return {}
    found = await store.schedules_held(conn, list(charters_of))
    waiting = await open_proposals(conn, [access for access in accesses if access.project_id in charters_of])
    states = {}
    for project_id, charter in charters_of.items():
        inside, _ = curator.shift_state(charter.body, charter.local_now)
        mine = [row for row in found if row.project_id == project_id]
        paused = bool(mine) and all(row.paused_at is not None for row in mine)
        run_id = min((row.run_id for row in mine if row.run_id is not None), default=None)
        states[project_id] = CuratorOverview(
            state=curator_state(paused, run_id, inside),
            in_window=inside,
            active_run_id=run_id,
            open_proposals=waiting.get(project_id, 0),
        )
    return states


async def status_of(conn: AsyncConnection, access: ProjectAccess) -> CuratorStatus:
    """The charter, the schedules and the night of the project's night shift."""
    row = await charters.charter_row(conn, access.project_id)
    found = await store.schedules_of(conn, access.project_id)
    schedules = [Schedule(**{name: item._mapping[name] for name in Schedule.model_fields}) for item in found]
    night = None
    if row is not None:
        local_now = await store.local_now(conn, curator.zone_of(row.body))
        inside, which = curator.shift_state(row.body, local_now)
        duty = next((item for item in found if item.kind == NIGHT_SHIFT), None)
        figures = NightFigures(0, 0.0, None) if duty is None else await night_figures(conn, duty.id, which)
        night = Night(
            night=which,
            in_window=inside,
            local_time=local_now.strftime("%H:%M"),
            runs=figures.runs,
            max_runs=row.body["max_runs_per_night"],
            cost_usd=round(figures.cost_usd, 6),
            budget_usd=curator.period_budget(row.body),
            active_run_id=figures.active_run_id,
        )
    paused = bool(schedules) and all(item.paused_at is not None for item in schedules)
    return CuratorStatus(
        project=access.name,
        charter=None if row is None else charter_view(access.name, row, admin=has_role(access.role, "admin")),
        paused=paused,
        schedules=schedules,
        night=night,
        last_review_run=await _last_review(conn, access.project_id),
        state=None if night is None else curator_state(paused, night.active_run_id, night.in_window),
        open_proposals=(await open_proposals(conn, [access])).get(access.project_id, 0),
        last_brief=await last_brief(conn, access.project_id),
    )


async def status(conn: AsyncConnection, user: Principal, project: str) -> CuratorStatus:
    return await status_of(conn, await read_access(conn, user, project))


async def last_brief(conn: AsyncConnection, project_id: int) -> BriefSummary | None:
    row = await store.last_brief(conn, project_id)
    if row is None:
        return None
    day, night, sent_at, login, notification_id, title = row[1:]
    return BriefSummary(
        id=row.id,
        day=day,
        night=night,
        sent_at=sent_at,
        to=login,
        notification_id=notification_id,
        title=title or f"Morning brief of {day.isoformat()}",
    )


async def _last_review(conn: AsyncConnection, project_id: int) -> ReviewRunSummary | None:
    row = await store.last_review(conn, project_id)
    if row is None:
        return None
    found = dict(row._mapping)
    found["lenses"] = [name for name in found["lenses"] or [] if isinstance(name, str)]
    return ReviewRunSummary(**found)


# The latest nights


def _night_summary(night, row, cost, review_row, counted: bool) -> NightSummary:
    return NightSummary(
        night=night,
        runs=row.runs if row else 0,
        done=row.done if row else 0,
        failed=row.failed if row else 0,
        cancelled=row.cancelled if row else 0,
        active=row.active if row else 0,
        cost_usd=round(float(cost or 0), 6),
        review_run=None
        if review_row is None
        else NightReview(
            id=review_row.id,
            state=review_row.state,
            findings=review_row.findings,
            proposals=review_row.proposals,
        ),
        figures=counted,
    )


async def nights(conn: AsyncConnection, user: Principal, project: str, limit: int) -> NightList:
    """The latest nights of the project's night shift, each with its runs, their cost and its review run."""
    access = await read_access(conn, user, project)
    charter = await charters.charter_row(conn, access.project_id)
    which = await store.latest_nights(conn, access.project_id, limit)
    counts, costs, reviews, counted = {}, {}, {}, set()
    if which:
        counts = await store.night_runs(conn, access.project_id, which)
        costs = await store.night_costs(conn, access.project_id, which)
        for row in await store.night_reviews(conn, access.project_id, which):
            reviews.setdefault(row.night, row)
        counted = await store.nights_counted(conn, access.project_id, which)
    listed = [
        _night_summary(night, counts.get(night), costs.get(night), reviews.get(night), night in counted)
        for night in which
    ]
    return NightList(
        project=access.name,
        nights=listed,
        budget_usd=None if charter is None else curator.period_budget(charter.body),
        max_runs=None if charter is None else charter.body["max_runs_per_night"],
    )


# Pausing


async def _pausable(conn: AsyncConnection, user: Principal, project: str, doing: str):
    """(access, ids of the project's schedules, locked) for an admin of the project or the owner of one of them."""
    access = await project_access(conn, user, project)
    reader(access)
    rows = await store.lock_schedules(conn, access.project_id)
    if not rows:
        raise Conflict(f"project {access.name} has no schedule to {doing}: an admin writes its charter first")
    if not has_role(access.role, "admin") and all(row.owner_id != user.user_id for row in rows):
        raise Forbidden(
            f"{doing} the night shift of project {access.name} needs the admin role on it, or one of its schedules"
        )
    return access, [row.id for row in rows]


async def pause(conn: AsyncConnection, user: Principal, project: str) -> tuple[CuratorStatus, list[int]]:
    """Pause every schedule of the project, and cancel the runs they queued that are still queued: where the night
    shift stands then, and the ids of the runs cancelled."""
    access, ids = await _pausable(conn, user, project, "pause")
    await store.pause(conn, ids, user.user_id)
    cancelled = await cancel_queued(conn, ids, f"the night shift of {access.name} was paused by {user.login}")
    await Auditor.of(conn, user, access).record(PAUSE, access.name)
    return await status_of(conn, access), cancelled


async def resume(conn: AsyncConnection, user: Principal, project: str) -> CuratorStatus:
    """Let every schedule of the project run again."""
    access, ids = await _pausable(conn, user, project, "resume")
    await store.resume(conn, ids)
    await Auditor.of(conn, user, access).record(RESUME, access.name)
    return await status_of(conn, access)

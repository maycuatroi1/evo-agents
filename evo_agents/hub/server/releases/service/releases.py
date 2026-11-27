"""What the routes of a project's daily release do (``routes.releases``, whose docstring says what each route
answers): the status of the release with its schedule, checks and cycles as the API shows them, setting, pausing and
resuming the schedule, and checking now."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, releases
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import github_repo
from evo_agents.hub.server import audit, run_access_rules
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.releases.models.releases import (
    PAUSE,
    RESUME,
    RUN,
    SET,
    ReleaseCheck,
    ReleaseCycle,
    ReleaseCycleList,
    ReleaseCycleRun,
    ReleaseSchedule,
    ReleaseScheduleWrite,
    ReleaseStatus,
    ReleaseTag,
)
from evo_agents.hub.server.releases.repository import checks as checks_repo
from evo_agents.hub.server.releases.repository import cycles as cycles_repo
from evo_agents.hub.server.releases.repository import runs as runs_repo
from evo_agents.hub.server.releases.repository import schedules as schedules_repo
from evo_agents.hub.server.releases.repository import tags as tags_repo
from evo_agents.hub.server.releases.repository.cycles import unfinished
from evo_agents.hub.server.releases.service.check import cancel_queued, check, close_idle
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


# Reading


def require_admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} of project {access.name} needs the admin role on it; you hold {held}")


def require_reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the release of project {access.name} needs a grant on it")


async def _schedule_view(conn: AsyncConnection, row) -> ReleaseSchedule:
    writer = await schedules_repo.login_of(conn, row.updated_by)
    found = github_repo(await schedules_repo.origin(conn, row.project_id, row.repo) or "")
    return ReleaseSchedule(
        repo=row.repo,
        github_repo="/".join(found) if found else None,
        at=row.check_at,
        timezone=row.timezone,
        target_ref=row.target_ref,
        tag_pattern=row.tag_pattern,
        workflow=row.workflow,
        paths=list(row.paths),
        owner=row.owner,
        updated_by=writer,
        created_at=row.created_at,
        updated_at=row.updated_at,
        paused_at=row.paused_at,
        paused_by=row.paused_by_login,
    )


def check_view(row) -> ReleaseCheck:
    return ReleaseCheck(
        id=row.id,
        trigger=row.trigger,
        local_day=row.local_day,
        checked_at=row.checked_at,
        requested_by=row.requested_login,
        outcome=row.outcome,
        reason=row.reason,
        base_tag=row.base_tag,
        base_commit=row.base_commit,
        target_commit=row.target_commit,
        changed_paths=None if row.changed_paths is None else list(row.changed_paths),
        cycle_id=row.cycle_id,
        run_id=row.run_id,
    )


def _run_view(row) -> ReleaseCycleRun:
    values = {key: value for key, value in row._mapping.items() if key not in ("cycle_id", "cost")}
    return ReleaseCycleRun(**values, cost_usd=float(row.cost))


def _cycle_model(found, items: list[ReleaseCycleRun], tags: list[ReleaseTag]) -> ReleaseCycle:
    thread = chats.chat_link(found.triage_chat_id, found.triage_root_id) if found.triage_root_id else None
    return ReleaseCycle(
        id=found.id,
        state=found.state,
        start=found.start,
        owner=found.owner,
        local_day=found.local_day,
        base_tag=found.base_tag,
        base_commit=found.base_commit,
        target_ref=found.target_ref,
        target_commit=found.target_commit,
        changed_paths=list(found.changed_paths),
        cost_usd=round(sum(item.cost_usd for item in items), 4),
        stop_reason=found.stop_reason,
        stop_cause=found.stop_cause,
        decision_id=found.decision_id,
        thread=thread,
        opened_at=found.opened_at,
        ended_at=found.ended_at,
        runs=items,
        tags=tags,
    )


async def cycle_views(conn: AsyncConnection, found: list) -> list[ReleaseCycle]:
    """The cycle rows ``found`` (of ``repository.cycles``) as the API shows them, in their order, with their runs and tags read
    in one query each whatever their number."""
    ids = [row.id for row in found]
    if not ids:
        return []
    runs: dict[int, list[ReleaseCycleRun]] = {cycle_id: [] for cycle_id in ids}
    for row in await runs_repo.runs_of_cycles(conn, ids):
        runs[row.cycle_id].append(_run_view(row))
    tags: dict[int, list[ReleaseTag]] = {cycle_id: [] for cycle_id in ids}
    for row in await tags_repo.tags_of_cycles(conn, ids):
        tags[row.cycle_id].append(_tag_view(row))
    return [_cycle_model(row, runs[row.id], tags[row.id]) for row in found]


async def _cycle_view(conn: AsyncConnection, cycle_id: int) -> ReleaseCycle:
    found = await cycles_repo.shown_cycle(conn, cycle_id)
    return (await cycle_views(conn, [found]))[0]


def _tag_view(row) -> ReleaseTag:
    failures = row.failures or {}
    return ReleaseTag(
        tag=row.tag,
        commit=row.commit,
        run_id=row.run_id,
        ci_state=row.ci_state,
        workflow_url=row.workflow_url,
        run_attempt=row.run_attempt,
        reruns=row.reruns,
        failed_tests=list(failures.get("ids") or []),
        failed_jobs=[str(job.get("job")) for job in failures.get("jobs") or [] if isinstance(job, dict)],
        infra=failures.get("infra") is True,
        problem=row.problem,
        watched_since=row.watched_since,
        checked_at=row.checked_at,
    )


async def release_status(conn: AsyncConnection, access: ProjectAccess) -> ReleaseStatus:
    row = await schedules_repo.project_schedule(conn, access.project_id)
    if row is None:
        return ReleaseStatus(
            project=access.name, schedule=None, paused=False, next_check_at=None, last_check=None, checks=[], cycle=None
        )
    shown = [check_view(item) for item in await checks_repo.latest_checks(conn, row.id, releases.SHOWN_CHECKS)]
    cycle = await unfinished(conn, row.id)
    return ReleaseStatus(
        project=access.name,
        schedule=await _schedule_view(conn, row),
        paused=row.paused_at is not None,
        next_check_at=await schedules_repo.next_check_at(conn, row),
        last_check=shown[0] if shown else None,
        checks=shown,
        cycle=None if cycle is None else await _cycle_view(conn, cycle.id),
    )


async def status(engine: AsyncEngine, user: Principal, project: str) -> ReleaseStatus:
    """Where the project's daily release stands, for a reader of it."""
    async with engine.connect() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        return await release_status(conn, access)


async def ended(engine: AsyncEngine, user: Principal, project: str, limit: int, offset: int) -> ReleaseCycleList:
    """A page of the project's release cycles that ended, for a reader of it."""
    async with engine.connect() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        total, found = await cycles_repo.ended_cycles(conn, access.project_id, limit, offset)
        cycles = await cycle_views(conn, found)
    return ReleaseCycleList(project=access.name, cycles=cycles, total=total, limit=limit, offset=offset)


# Writing


async def _repo(conn: AsyncConnection, access: ProjectAccess, asked: str | None, current) -> str:
    on_github = await schedules_repo.github_repos(conn, access.project_id)
    name = asked or (current.repo if current is not None else None)
    if name is None:
        if len(on_github) != 1:
            listed = ", ".join(sorted(on_github)) or "none"
            raise HTTPException(422, f"name the repo of the release: the repos of {access.name} on GitHub are {listed}")
        name = next(iter(on_github))
    if name not in on_github:
        raise HTTPException(
            422, f"{name} is not a repo of {access.name} with an origin on GitHub: a release is cut and tagged there"
        )
    return name


async def _time_zone(conn: AsyncConnection, name: str) -> str:
    if not await schedules_repo.time_zone_known(conn, name):
        raise HTTPException(422, f"{name!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo")
    return name


async def _values(conn: AsyncConnection, access: ProjectAccess, body: ReleaseScheduleWrite, current) -> dict:
    """The schedule's columns once ``body`` is set over ``current`` (None: the defaults)."""

    def kept(asked, column_name: str, default):
        if asked is not None:
            return asked
        return getattr(current, column_name) if current is not None else default

    values = {
        "repo": await _repo(conn, access, body.repo, current),
        "check_at": kept(body.at, "check_at", releases.DEFAULT_AT),
        "timezone": kept(body.timezone, "timezone", releases.DEFAULT_TIMEZONE),
        "target_ref": kept(body.target_ref, "target_ref", releases.DEFAULT_TARGET_REF),
        "tag_pattern": kept(body.tag_pattern, "tag_pattern", releases.DEFAULT_TAG_PATTERN),
        "workflow": kept(body.workflow, "workflow", releases.DEFAULT_WORKFLOW),
        "paths": list(kept(body.paths, "paths", list(releases.DEFAULT_PATHS))),
    }
    await _time_zone(conn, values["timezone"])
    return values


async def set_schedule(engine: AsyncEngine, user: Principal, project: str, body: ReleaseScheduleWrite) -> ReleaseStatus:
    """Set the project's release schedule; the caller becomes its owner."""
    async with engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_admin(access, "setting the release schedule")
        current = await schedules_repo.project_schedule(conn, access.project_id, lock=True)
        values = await _values(conn, access, body, current) | {"owner_id": user.user_id, "updated_by": user.user_id}
        if current is None:
            await schedules_repo.insert_schedule(conn, access.project_id, values)
        else:
            await schedules_repo.update_schedule(conn, current.id, values)
        target = f"{access.name} repo={values['repo']} at={values['check_at']} {values['timezone']}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=SET, target=target, project_id=access.project_id
        )
        found = await release_status(conn, access)
    log.info("release schedule set", extra={"project": project, "login": user.login, "repo": values["repo"]})
    return found


async def admin_schedule(conn: AsyncConnection, user: Principal, project: str, doing: str):
    access = await project_access(conn, user, project)
    require_admin(access, f"{doing} the release")
    row = await schedules_repo.project_schedule(conn, access.project_id, lock=True)
    if row is None:
        raise HTTPException(
            409, f"project {access.name} has no release schedule: an admin sets one with `evo-agents hub release set`"
        )
    return access, row


async def pause(engine: AsyncEngine, user: Principal, project: str) -> ReleaseStatus:
    """Pause the release schedule, cancel the release runs still queued and close an open cycle left idle."""
    async with engine.begin() as conn:
        access, row = await admin_schedule(conn, user, project, "pausing")
        await schedules_repo.pause(conn, row.id, user.user_id)
        reason = f"the release schedule of {access.name} was paused by {user.login}"
        cancelled = await cancel_queued(conn, access.project_id, reason)
        closed = await close_idle(conn, row.id, reason)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PAUSE,
            target=access.name,
            project_id=access.project_id,
        )
        found = await release_status(conn, access)
    log.info(
        "release paused", extra={"project": project, "login": user.login, "cancelled": cancelled, "closed": closed}
    )
    return found


async def resume(engine: AsyncEngine, user: Principal, project: str) -> ReleaseStatus:
    """Let the release schedule check again."""
    async with engine.begin() as conn:
        access, row = await admin_schedule(conn, user, project, "resuming")
        await schedules_repo.resume(conn, row.id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=RESUME,
            target=access.name,
            project_id=access.project_id,
        )
        found = await release_status(conn, access)
    log.info("release resumed", extra={"project": project, "login": user.login})
    return found


async def run_now(engine: AsyncEngine, app, user: Principal, project: str) -> ReleaseCheck:
    """Check now, through the gates of the daily check, and answer the check."""
    async with engine.begin() as conn:
        access, row = await admin_schedule(conn, user, project, "checking")
    with run_access_rules.release_dispatch():  # a run's token got here only with its owner's releases reach
        found = await check(engine, app, row.id, trigger="manual", requested_by=user.user_id)
    if found is None:  # the schedule went in between: nothing was checked
        raise HTTPException(409, f"project {access.name} has no release schedule any more: nothing was checked")
    async with engine.begin() as conn:
        target = f"{access.name} check:{found.id}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=RUN, target=target, project_id=access.project_id
        )
        shown = await checks_repo.check_row(conn, row.id, found.id)
    log.info("release checked", extra={"project": project, "login": user.login, "outcome": found.outcome})
    return check_view(shown)

"""The night shift on the hub: a project's charter, its schedules, pausing them, and the job that queues their runs.
``evo_agents.hub.curator`` holds the model: the window, the night, the caps and how a run's cost is read.

GET /v1/projects/{p}/curator/charter (reader) shows the project's charter at its newest revision, or at ``?revision``;
404 when it has none. The checks of the Judge (``judge.hidden_checks``) are shown to the project's admins alone, and as
null to anyone else. GET .../curator/charter/revisions lists every revision, newest first: none is ever deleted.

PUT .../curator/charter (admin of the project) writes a new revision: the window's time zone must be one Postgres
knows; the charter names no worker and no pool: the night shift runs on the pool of the project's default agent
(``evo_agents.hub.server.night_duty``). The project's schedule (one per kind, here ``night_shift``) follows it: its
owner, the member it dispatches as, is that pool's owner. A body equal to
the newest revision's writes nothing and answers that revision. ``judge.hidden_checks`` null keeps the checks of the
newest revision. The keys ``charter show --json`` prints besides the body (``curator.CHARTER_META``) are ignored, so
what was shown can be written back. Each revision is audited (curator.charter). A member without the admin role gets
403, a worker token 403 from the guard of /v1, before any of this.

GET /v1/projects/{p}/curator (reader) says where the night shift stands: the charter, whether it is paused, each
schedule with its owner and worker, and the night now (or the last one, outside the window): its runs and its cost,
the run of the schedule that is queued or held, if any. POST .../curator/pause (an admin of the project, or the owner of
one of its schedules) pauses every schedule of the project at once and cancels each run they queued that is still
queued and resumes no parked run; .../curator/resume lets them run again, a night shift the circuit breaker paused
included, and starts its count of jobs gone wrong again. Each is audited (curator.pause,
curator.resume); a second pause keeps the first one's time. GET .../curator says the night shift's ``state`` in a word
(CURATOR_STATES) and how many of the project's proposals wait for an answer; ``curator_overview`` says the same of
every project of the caller for GET /v1/me/overview. GET .../curator/nights (reader) lists the latest nights, newest
first: for each, the runs the schedules queued for it by how they stand, their cost as a night's cost is counted, its
review run with what it wrote, and whether curator.collect counted its figures.

``fire_schedules`` (``evo_agents.hub.server.curator_shift``) is the job ``hub.fire_schedules``, run every minute by
the hub's worker: for each schedule of kind night_shift, under its row's lock (a schedule another run of the job holds
is passed over), it first lets the circuit breaker look at the night (``outcomes.check_circuit``: once the charter's
``circuit_breaker.max_failed_in_a_row`` jobs of the night in a row failed or were reverted, every schedule of the
project is paused with the reason in ``pause_reason`` and no member in ``paused_by``, and their owner gets the notice
curator_paused), then it cancels its queued runs when it is paused, and outside its window those of the night
(``shift_hours``), and otherwise queues at most one run (``gate_of`` says when it may): the night's review run first,
when the night has none yet and the worker runs review runs (``evo_agents.hub.server.collect``), else the night's
history run once its review run was queued (``evo_agents.hub.server.history``), these two inside the window alone,
else the judge run of a change of the Curator that waits for one, else the Builder of a change the Curator planned
(``evo_agents.hub.server.changes``), else the replay run of a change that waits for one
(``evo_agents.hub.server.replays``), else, inside the window, a plan run of the charter's night_plans, as
``evo_agents.hub.curator`` says, audited as curator.dispatch with the owner as actor and no token, else, when no plan
of night_plans has work, the probe run of a probe and a model the week has not measured, within the charter's
probes.budget_usd (``evo_agents.hub.server.probe_runs``). GET .../curator also names the last review run of the
project, with how many findings and proposals it wrote, and its last morning brief (``evo_agents.hub.server.brief``).
A night's cost is that of its runs (their ``usage``), each agent session once at its largest total, as Claude Code
reports a session's running total. The charter also names what the Curator's runs get of the owner's secrets
(``git_secret``, ``env_secrets``): no other secret is leased to them.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import (
    Text,
    and_,
    cast,
    column,
    exists,
    func,
    insert,
    literal,
    select,
    table,
    union,
    update,
)
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.db import one_of
from evo_agents.hub.server import agent_placement, audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.curator_models import (
    BriefSummary,
    Charter,
    CharterRevision,
    CharterWrite,
    CuratorOverview,
    CuratorStatus,
    Night,
    NightList,
    NightReview,
    NightSummary,
    ReviewRunSummary,
    Schedule,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.night_cost import QUEUED_OR_HELD, NightFigures, night_figures, usage_cost
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.shift_hours import cancel_queued

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})

CHARTER = "curator.charter"  # an admin wrote a revision: "<project> revision=<n>"
PAUSE = "curator.pause"  # every schedule of the project paused: "<project>"
RESUME = "curator.resume"  # and let run again: "<project>"
NIGHT_SHIFT = "night_shift"
MAX_CHARTER_REVISIONS = 1000  # the revisions GET .../charter/revisions lists at most
DEFAULT_NIGHTS, MAX_NIGHTS = 14, 90  # the nights GET .../curator/nights lists, by default and at most
PG_TIMEZONES = table("pg_timezone_names", column("name", Text))


# Reading


def _admin(access: ProjectAccess, doing: str) -> None:
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(403, f"{doing} of project {access.name} needs the admin role on it; you hold {held}")


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the night shift of project {access.name} needs a grant on it")


def _charters():
    """A charter revision with its writer's login and the login of the owner of the project's night shift schedule."""
    c, s, writer, owner = tables.charters, tables.schedules, tables.users.alias("writer"), tables.users.alias("owner")
    return (
        select(
            c.c.project_id,
            c.c.revision,
            c.c.body,
            c.c.created_at.label("updated_at"),
            writer.c.login.label("updated_by"),
            func.coalesce(owner.c.login, writer.c.login).label("schedule_owner"),
        )
        .join_from(c, writer, writer.c.id == c.c.created_by)
        .outerjoin(s, and_(s.c.project_id == c.c.project_id, s.c.kind == NIGHT_SHIFT))
        .outerjoin(owner, owner.c.id == s.c.owner_id)
    )


async def charter_row(conn: AsyncConnection, project_id: int, revision: int | None = None):
    c = tables.charters
    query = _charters().where(c.c.project_id == project_id)
    if revision is not None:
        query = query.where(c.c.revision == revision)
    return (await conn.execute(query.order_by(c.c.revision.desc()).limit(1))).one_or_none()


def _charter_view(project: str, row, *, admin: bool) -> Charter:
    body = dict(row.body)
    if not admin:
        body["judge"] = {**body.get("judge", {}), "hidden_checks": None}
    return Charter(
        **body,
        project=project,
        revision=row.revision,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        schedule_owner=row.schedule_owner,
    )


def _no_charter(project: str) -> HTTPException:
    return HTTPException(
        404, f"project {project} has no charter: an admin writes one with `evo-agents hub curator charter set`"
    )


@router.get(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def show_charter(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    revision: Annotated[int | None, Query(ge=1, le=2**31 - 1, description="an older revision")] = None,
) -> Charter:
    """The project's charter, at its newest revision or at ``revision``."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        row = await charter_row(conn, access.project_id, revision)
    if row is None:
        if revision is not None:
            raise HTTPException(404, f"project {project} has no charter revision {revision}")
        raise _no_charter(project)
    return _charter_view(access.name, row, admin=has_role(access.role, "admin"))


@router.get(
    "/{project}/curator/charter/revisions",
    response_model=list[CharterRevision],
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def charter_revisions(request: Request, project: ProjectName, user: CurrentUser) -> list[CharterRevision]:
    """Every revision of the project's charter, newest first; empty without one."""
    c = tables.charters
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        query = _charters().where(c.c.project_id == access.project_id).order_by(c.c.revision.desc())
        rows = (await conn.execute(query.limit(MAX_CHARTER_REVISIONS))).all()
    return [
        CharterRevision(
            revision=row.revision,
            updated_by=row.updated_by,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


# Writing


async def _time_zone_known(conn: AsyncConnection, name: str) -> None:
    known = (await conn.execute(select(exists().where(PG_TIMEZONES.c.name == name)))).scalar_one()
    if not known:
        raise HTTPException(422, f"{name!r} is not a time zone the hub knows: give an IANA name such as Asia/Tokyo")


def _stored(body: CharterWrite, current) -> dict:
    """The body to keep: the Judge's checks of ``current`` when the write gives none."""
    stored = body.model_dump(mode="json")
    if stored["judge"]["hidden_checks"] is None:
        kept = (current.body.get("judge") or {}).get("hidden_checks") if current is not None else None
        stored["judge"]["hidden_checks"] = kept or []
    return stored


async def _duty_owner(conn: AsyncConnection, user: Principal, project_id: int) -> int:
    """The owner of the pool of the project's default agent, whose runs the night shift dispatches as; the writer when
    the agent has no pool."""
    home = await agent_placement.default_home(conn, project_id)
    if home is None:
        return user.user_id
    p = tables.worker_pools
    return (await conn.execute(select(p.c.owner_id).where(p.c.id == home.pool_id))).scalar_one()


async def _keep_schedule(conn: AsyncConnection, project_id: int, owner_id: int) -> None:
    s = tables.schedules
    statement = pg_insert(s).values(project_id=project_id, kind=NIGHT_SHIFT, owner_id=owner_id)
    statement = statement.on_conflict_do_update(
        constraint="schedules_project_id_kind_key",
        set_={"owner_id": owner_id, "updated_at": func.now()},
        where=s.c.owner_id.is_distinct_from(owner_id),
    )
    await conn.execute(statement)


@router.put(
    "/{project}/curator/charter",
    response_model=Charter,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def write_charter(request: Request, project: ProjectName, body: CharterWrite, user: CurrentUser) -> Charter:
    """Write a new revision of the project's charter; the admins of the project alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "changing the charter")
        current = await charter_row(conn, access.project_id)
        await _time_zone_known(conn, body.window.timezone)
        stored = _stored(body, current)
        if len(json.dumps(stored, ensure_ascii=False).encode()) > curator.MAX_CHARTER_BYTES:
            raise HTTPException(413, f"the charter is over {curator.MAX_CHARTER_BYTES // 1024} KiB as JSON")
        owner_id = await _duty_owner(conn, user, access.project_id)
        if current is not None and current.body == stored:
            await _keep_schedule(conn, access.project_id, owner_id)
            return _charter_view(access.name, current, admin=True)
        revision = 1 if current is None else current.revision + 1
        values = {
            "project_id": access.project_id,
            "revision": revision,
            "body": stored,
            "created_by": user.user_id,
        }
        try:
            async with conn.begin_nested():
                await conn.execute(insert(tables.charters).values(**values))
        except IntegrityError:
            raise HTTPException(
                409, f"the charter of project {project} changed while this write was made: read it again"
            ) from None
        await _keep_schedule(conn, access.project_id, owner_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=CHARTER,
            target=f"{access.name} revision={revision}",
            project_id=access.project_id,
        )
        row = await charter_row(conn, access.project_id, revision)
    log.info("charter written", extra={"project": project, "revision": revision, "login": user.login})
    return _charter_view(access.name, row, admin=True)


# Where the night shift stands


def _schedules(project_id: int):
    s, owner, pauser = tables.schedules, tables.users.alias("owner"), tables.users.alias("pauser")
    return (
        select(
            s.c.id,
            s.c.kind,
            s.c.owner_id,
            owner.c.login.label("owner"),
            s.c.paused_at,
            pauser.c.login.label("paused_by"),
            s.c.pause_reason,
        )
        .join_from(s, owner, owner.c.id == s.c.owner_id)
        .outerjoin(pauser, pauser.c.id == s.c.paused_by)
        .where(s.c.project_id == project_id)
        .order_by(s.c.kind)
    )


def curator_state(paused: bool, active_run_id: int | None, in_window: bool) -> str:
    """One of CURATOR_STATES: paused first, then a run in flight, then the window."""
    if paused:
        return "paused"
    if active_run_id is not None:
        return "running"
    return "on_duty" if in_window else "idle"


def _newest_charters():
    """The newest charter of each project of the list bound as ``projects``, with the time now in its time zone."""
    c = tables.charters
    local_now = func.timezone(c.c.body["window"]["timezone"].astext, func.now())
    return (
        select(c.c.project_id, c.c.body, local_now.label("local_now"))
        .where(one_of(c.c.project_id, name="projects"))
        .ext(distinct_on(c.c.project_id))
        .order_by(c.c.project_id, c.c.revision.desc())
    )


def _schedules_held():
    """Each schedule of the projects bound as ``projects``: its project, when it was paused, and its first run queued
    or held now (``run_id``, NULL without one)."""
    s, r = tables.schedules, tables.runs
    held = (
        select(func.min(r.c.id)).where(r.c.schedule_id == s.c.id, one_of(r.c.state, QUEUED_OR_HELD)).scalar_subquery()
    )
    return select(s.c.id, s.c.project_id, s.c.paused_at, held.label("run_id")).where(
        one_of(s.c.project_id, name="projects")
    )


def _open_proposal_labels():
    """The label of each open proposal of the projects bound as ``projects``."""
    p = tables.proposals
    return select(p.c.project_id, p.c.label).where(one_of(p.c.project_id, name="projects"), p.c.state == "open")


# Built once with bind parameters: GET /v1/me/overview runs them on every request (docs/hub.md, Data access).
_NEWEST_CHARTERS = _newest_charters()
_SCHEDULES_HELD = _schedules_held()
_OPEN_PROPOSAL_LABELS = _open_proposal_labels()


async def open_proposals(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, int]:
    """Per project of ``accesses``, its open proposals whose label the access reaches through the project's hub
    sink, as GET .../curator/proposals reads them."""
    by_id = {access.project_id: access for access in accesses}
    if not by_id:
        return {}
    counted: Counter[int] = Counter()
    for project_id, label in (await conn.execute(_OPEN_PROPOSAL_LABELS, {"projects": list(by_id)})).all():
        access = by_id[project_id]
        if access.visible(label, plan_routes._sink(access, None)):
            counted[project_id] += 1
    return dict(counted)


async def curator_overview(conn: AsyncConnection, accesses: list[ProjectAccess]) -> dict[int, CuratorOverview]:
    """Where the Curator of each project of ``accesses`` that has a charter stands, in three statements built once
    for all of them."""
    ids = [access.project_id for access in accesses]
    if not ids:
        return {}
    charters = {row.project_id: row for row in (await conn.execute(_NEWEST_CHARTERS, {"projects": ids})).all()}
    if not charters:
        return {}
    found = (await conn.execute(_SCHEDULES_HELD, {"projects": list(charters)})).all()
    waiting = await open_proposals(conn, [access for access in accesses if access.project_id in charters])
    states = {}
    for project_id, charter in charters.items():
        window = charter.body["window"]
        inside, _ = curator.window_state(charter.local_now, window["start"], window["end"])
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


async def _status(conn: AsyncConnection, access: ProjectAccess) -> CuratorStatus:
    row = await charter_row(conn, access.project_id)
    found = (await conn.execute(_schedules(access.project_id))).all()
    schedules = [Schedule(**{name: item._mapping[name] for name in Schedule.model_fields}) for item in found]
    night = None
    if row is not None:
        window = row.body["window"]
        local_now = (await conn.execute(select(func.timezone(window["timezone"], func.now())))).scalar_one()
        inside, which = curator.window_state(local_now, window["start"], window["end"])
        duty = next((item for item in found if item.kind == NIGHT_SHIFT), None)
        figures = NightFigures(0, 0.0, None) if duty is None else await night_figures(conn, duty.id, which)
        night = Night(
            night=which,
            in_window=inside,
            local_time=local_now.strftime("%H:%M"),
            runs=figures.runs,
            max_runs=row.body["max_runs_per_night"],
            cost_usd=round(figures.cost_usd, 6),
            budget_usd=row.body["night_budget_usd"],
            active_run_id=figures.active_run_id,
        )
    paused = bool(schedules) and all(item.paused_at is not None for item in schedules)
    return CuratorStatus(
        project=access.name,
        charter=None if row is None else _charter_view(access.name, row, admin=has_role(access.role, "admin")),
        paused=paused,
        schedules=schedules,
        night=night,
        last_review_run=await _last_review(conn, access.project_id),
        state=None if night is None else curator_state(paused, night.active_run_id, night.in_window),
        open_proposals=(await open_proposals(conn, [access])).get(access.project_id, 0),
        last_brief=await last_brief(conn, access.project_id),
    )


async def last_brief(conn: AsyncConnection, project_id: int) -> BriefSummary | None:
    b, u = tables.curator_briefs, tables.users
    query = (
        select(b.c.id, b.c.day, b.c.night, b.c.created_at, u.c.login, b.c.notification_id, b.c.body["title"].astext)
        .join_from(b, u, u.c.id == b.c.user_id)
        .where(b.c.project_id == project_id)
        .order_by(b.c.day.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
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
    """The last review run of a night; a review run of the knowledge graph (starter questions, kg_init, an answer
    batch) belongs to no night and is not one."""
    r, f, p, cf = tables.runs, tables.findings, tables.proposals, tables.curator_figures
    findings = select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery()
    proposals = select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery()
    lenses = select(cf.c.figures["lenses"]).where(cf.c.run_id == r.c.id).limit(1).scalar_subquery()
    query = (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            lenses.label("lenses"),
            findings.label("findings"),
            proposals.label("proposals"),
            r.c.queued_at,
            r.c.finished_at,
        )
        .where(r.c.project_id == project_id, r.c.kind == "review", r.c.schedule_night.is_not(None))
        .order_by(r.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    found = dict(row._mapping)
    found["lenses"] = [name for name in found["lenses"] or [] if isinstance(name, str)]
    return ReviewRunSummary(**found)


@router.get(
    "/{project}/curator",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def status(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """The charter, the schedules and the night of the project's night shift."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        return await _status(conn, access)


def _nights_of(project_id: int, limit: int):
    """The latest ``limit`` nights of the project: those its schedules queued runs for, and those curator.collect
    counted figures for."""
    r, s, cf = tables.runs, tables.schedules, tables.curator_figures
    queued = select(r.c.schedule_night.label("night")).where(
        r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)), r.c.schedule_night.is_not(None)
    )
    counted = select(cf.c.night.label("night")).where(cf.c.project_id == project_id)
    nights = union(queued, counted).subquery("nights")
    return select(nights.c.night).order_by(nights.c.night.desc()).limit(limit)


def _night_runs(project_id: int, nights: list[date]):
    """Per night of ``nights``: the runs the project's schedules queued for it, by how they stand."""
    r, s = tables.runs, tables.schedules
    return (
        select(
            r.c.schedule_night.label("night"),
            func.count().label("runs"),
            func.count().filter(r.c.state == "done").label("done"),
            func.count().filter(r.c.state.in_(("failed", "lost"))).label("failed"),
            func.count().filter(r.c.state == "cancelled").label("cancelled"),
            func.count().filter(r.c.state.in_(runs.ACTIVE_STATES)).label("active"),
        )
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night)
    )


def _night_costs(project_id: int, nights: list[date]):
    """Per night of ``nights``: what its runs cost, each agent session once at its largest total, as
    ``_night_figures`` counts one night."""
    r, s = tables.runs, tables.schedules
    session = func.coalesce(r.c.session_id, literal("run:") + cast(r.c.id, Text))
    per_session = (
        select(r.c.schedule_night.label("night"), func.max(usage_cost(r.c.usage)).label("cost"))
        .where(
            r.c.schedule_id.in_(select(s.c.id).where(s.c.project_id == project_id)),
            r.c.schedule_night.in_(nights),
        )
        .group_by(r.c.schedule_night, session)
        .subquery("per_session")
    )
    return select(per_session.c.night, func.sum(per_session.c.cost).label("cost")).group_by(per_session.c.night)


def _night_reviews(project_id: int, nights: list[date]):
    """The review runs of the project for ``nights``, with what each wrote, the latest first."""
    r, f, p = tables.runs, tables.findings, tables.proposals
    return (
        select(
            r.c.id,
            r.c.state,
            r.c.schedule_night.label("night"),
            select(func.count()).where(f.c.run_id == r.c.id).scalar_subquery().label("findings"),
            select(func.count()).where(p.c.run_id == r.c.id).scalar_subquery().label("proposals"),
        )
        .where(r.c.project_id == project_id, r.c.kind == "review", r.c.schedule_night.in_(nights))
        .order_by(r.c.id.desc())
    )


@router.get(
    "/{project}/curator/nights",
    response_model=NightList,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def nights(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_NIGHTS, description="the latest nights")] = DEFAULT_NIGHTS,
) -> NightList:
    """The latest nights of the project's night shift, each with its runs, their cost and its review run."""
    cf = tables.curator_figures
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        charter = await charter_row(conn, access.project_id)
        which = list((await conn.execute(_nights_of(access.project_id, limit))).scalars())
        counts, costs, reviews, counted = {}, {}, {}, set()
        if which:
            counts = {row.night: row for row in (await conn.execute(_night_runs(access.project_id, which))).all()}
            costs = {row.night: row.cost for row in (await conn.execute(_night_costs(access.project_id, which))).all()}
            for row in (await conn.execute(_night_reviews(access.project_id, which))).all():
                reviews.setdefault(row.night, row)
            figures = select(cf.c.night).where(cf.c.project_id == access.project_id, cf.c.night.in_(which))
            counted = set((await conn.execute(figures)).scalars())
    listed = []
    for night in which:
        row, review_row = counts.get(night), reviews.get(night)
        listed.append(
            NightSummary(
                night=night,
                runs=row.runs if row else 0,
                done=row.done if row else 0,
                failed=row.failed if row else 0,
                cancelled=row.cancelled if row else 0,
                active=row.active if row else 0,
                cost_usd=round(float(costs.get(night) or 0), 6),
                review_run=None
                if review_row is None
                else NightReview(
                    id=review_row.id,
                    state=review_row.state,
                    findings=review_row.findings,
                    proposals=review_row.proposals,
                ),
                figures=night in counted,
            )
        )
    return NightList(
        project=access.name,
        nights=listed,
        budget_usd=None if charter is None else charter.body["night_budget_usd"],
        max_runs=None if charter is None else charter.body["max_runs_per_night"],
    )


# Pausing


async def _pausable(conn: AsyncConnection, user: Principal, project: str, doing: str):
    """(access, ids of the project's schedules, locked) for an admin of the project or the owner of one of them."""
    access = await project_access(conn, user, project)
    _reader(access)
    s = tables.schedules
    rows = (
        await conn.execute(
            select(s.c.id, s.c.owner_id).where(s.c.project_id == access.project_id).order_by(s.c.id).with_for_update()
        )
    ).all()
    if not rows:
        raise HTTPException(409, f"project {access.name} has no schedule to {doing}: an admin writes its charter first")
    if not has_role(access.role, "admin") and all(row.owner_id != user.user_id for row in rows):
        raise HTTPException(
            403, f"{doing} the night shift of project {access.name} needs the admin role on it, or one of its schedules"
        )
    return access, [row.id for row in rows]


@router.post(
    "/{project}/curator/pause",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def pause(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Pause every schedule of the project, and cancel the runs they queued that are still queued."""
    s = tables.schedules
    async with request.app.state.engine.begin() as conn:
        access, ids = await _pausable(conn, user, project, "pause")
        await conn.execute(
            update(s)
            .values(paused_at=func.now(), paused_by=user.user_id, updated_at=func.now())
            .where(s.c.id.in_(ids), s.c.paused_at.is_(None))
        )
        cancelled = await cancel_queued(conn, ids, f"the night shift of {access.name} was paused by {user.login}")
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=PAUSE,
            target=access.name,
            project_id=access.project_id,
        )
        found = await _status(conn, access)
    log.info("night shift paused", extra={"project": project, "login": user.login, "cancelled": cancelled})
    return found


@router.post(
    "/{project}/curator/resume",
    response_model=CuratorStatus,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
async def resume(request: Request, project: ProjectName, user: CurrentUser) -> CuratorStatus:
    """Let every schedule of the project run again."""
    s = tables.schedules
    async with request.app.state.engine.begin() as conn:
        access, ids = await _pausable(conn, user, project, "resume")
        await conn.execute(
            update(s)
            .values(paused_at=None, paused_by=None, pause_reason=None, updated_at=func.now())
            .where(s.c.id.in_(ids), s.c.paused_at.is_not(None))
        )
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=RESUME,
            target=access.name,
            project_id=access.project_id,
        )
        found = await _status(conn, access)
    log.info("night shift resumed", extra={"project": project, "login": user.login})
    return found

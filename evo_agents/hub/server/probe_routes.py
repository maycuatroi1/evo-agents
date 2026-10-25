"""The routes of the Curator's probes (``evo_agents.hub.server.probes`` holds the models and the store).

GET /v1/projects/{p}/curator/probes (reader) lists the probes whose label the caller's grant reaches through the sink
X-Evo-Sink names (else the project's hub sink), newest first, filtered by change, proposal and state; GET
.../curator/probes/{id} shows one. Their verify is null unless the caller is an admin of the project and not the agent
of a run (``probes.verify_shown``).

The project's admins write them (403 otherwise), each write audited as curator.probe: POST .../curator/probes adds one
to a change or to a proposal of the project (404 for one it does not have, 409 when it has an active probe already:
retire that one first); PATCH .../curator/probes/{id} changes its repo, commit, task or verify (409 once retired); POST
.../curator/probes/{id}/retire retires it, with a note (409 when it is retired already).

POST /v1/worker/runs/{id}/probes takes a probe from the agent of a review run the worker holds (404 for any other run),
as the member who dispatched it, who must still hold writer on the project, for a merged change of the project that has
no active probe (409 otherwise): ``evo-agents worker probe add``. Its answer shows no verify. With ``replaces``, the
probe takes the place of the change's active probe that the weekly report found not reproduced on one model two weeks
in a row (``probe_fixes``; 422 for any other probe), which the hub retires with a note.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes as probe_shape
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, probe_fixes, probes
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.memory_audience import Reader, label_reaches
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.proposals import ItemId, held_review_run, project_repos, writer_of
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_LIST = 200

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}


def _admin(access: ProjectAccess, what: str) -> None:
    if not has_role(access.role, "admin"):
        raise HTTPException(
            403, f"{what} of project {access.name} needs the admin role on it; you hold {access.role or 'no role'}"
        )


async def _readable(conn: AsyncConnection, access: ProjectAccess, user: Principal, row, sink: str | None) -> bool:
    return await label_reaches(Reader(conn, user), access, row.label, plan_routes._sink(access, sink))


async def _probe_row(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, sink: str | None, *, lock=False
):
    """Probe ``probe_id`` of the project when its label reaches ``user``; 404 otherwise."""
    pr = tables.curator_probes
    query = probes.query().where(pr.c.project_id == access.project_id, pr.c.id == probe_id)
    if lock:
        query = query.with_for_update(of=pr)
    row = (await conn.execute(query)).one_or_none()
    if row is None or not await _readable(conn, access, user, row, sink):
        raise HTTPException(404, f"project {access.name} has no probe {probe_id}")
    return row


async def _change_target(conn: AsyncConnection, access_project_id: int, project: str, change_id: int):
    """The change ``change_id`` of the project (id, state, repo, proposal_id) with its proposal's label; 404 without."""
    c, p = tables.curator_changes, tables.proposals
    row = (
        await conn.execute(
            select(c.c.id, c.c.state, c.c.repo, c.c.proposal_id, p.c.label)
            .join_from(c, p, p.c.id == c.c.proposal_id)
            .where(c.c.project_id == access_project_id, c.c.id == change_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, f"project {project} has no change {change_id}")
    return row


async def _active_on_proposal(conn: AsyncConnection, proposal_id: int) -> int | None:
    pr = tables.curator_probes
    return (
        await conn.execute(select(pr.c.id).where(pr.c.proposal_id == proposal_id, pr.c.state == "active").limit(1))
    ).scalar_one_or_none()


async def _audited(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int, what: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=probes.PROBE,
        target=f"{access.name} probe:{probe_id} {what}",
        project_id=access.project_id,
    )


async def _shown(conn: AsyncConnection, access: ProjectAccess, user: Principal, probe_id: int) -> probes.Probe:
    pr = tables.curator_probes
    row = (await conn.execute(probes.query().where(pr.c.id == probe_id))).one()
    return probes.view(access.name, row, shown=probes.verify_shown(access, user))


# Reading


@router.get("/{project}/curator/probes", response_model=probes.ProbeList, responses=READ_REFUSALS)
async def list_probes(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    change: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the probes of this change")] = None,
    proposal: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the probes of this proposal")] = None,
    state: Annotated[
        Literal[probe_shape.STATES] | None, Query(description="active or retired; both by default")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> probes.ProbeList:
    """The project's probes the caller may read, newest first."""
    pr = tables.curator_probes
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        query = probes.query().where(pr.c.project_id == access.project_id)
        if change is not None:
            query = query.where(pr.c.change_id == change)
        if proposal is not None:
            query = query.where(pr.c.proposal_id == proposal)
        if state is not None:
            query = query.where(pr.c.state == state)
        rows = (await conn.execute(query.order_by(pr.c.id.desc()))).all()
        readable = [row for row in rows if await _readable(conn, access, user, row, sink)]
        shown = probes.verify_shown(access, user)
    listed = [probes.view(access.name, row, shown=shown) for row in readable[:limit]]
    return probes.ProbeList(probes=listed, total=len(readable))


@router.get("/{project}/curator/probes/{probe_id}", response_model=probes.Probe, responses=READ_REFUSALS)
async def show_probe(
    request: Request,
    project: ProjectName,
    probe_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> probes.Probe:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        row = await _probe_row(conn, access, user, probe_id, sink)
    return probes.view(access.name, row, shown=probes.verify_shown(access, user))


# The admins' writes


@router.post("/{project}/curator/probes", status_code=201, response_model=probes.Probe, responses=REFUSALS)
async def add_probe(request: Request, project: ProjectName, body: probes.ProbeAdd, user: CurrentUser) -> probes.Probe:
    """Add a probe to a change or a proposal of the project; its admins alone may."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "adding a probe")
        if body.repo not in await project_repos(conn, access.project_id):
            raise HTTPException(422, f"project {access.name} has no repo {body.repo}; nothing was recorded")
        if body.change_id is not None:
            change = await _change_target(conn, access.project_id, access.name, body.change_id)
            if (found := await probes.active_on_change(conn, change.id)) is not None:
                raise HTTPException(409, f"change {change.id} has active probe #{found}: retire it first")
            target = {"change_id": change.id, "proposal_id": change.proposal_id}
            label, what = change.label, f"add change:{change.id}"
        else:
            p, c = tables.proposals, tables.curator_changes
            proposal = (
                await conn.execute(
                    select(p.c.id, p.c.label).where(p.c.project_id == access.project_id, p.c.id == body.proposal_id)
                )
            ).one_or_none()
            if proposal is None:
                raise HTTPException(404, f"project {access.name} has no proposal {body.proposal_id}")
            if (found := await _active_on_proposal(conn, proposal.id)) is not None:
                raise HTTPException(409, f"proposal {proposal.id} has active probe #{found}: retire it first")
            change_id = (await conn.execute(select(c.c.id).where(c.c.proposal_id == proposal.id))).scalar_one_or_none()
            target = {"proposal_id": proposal.id, "change_id": change_id}
            label, what = proposal.label, f"add proposal:{proposal.id}"
        probe_id = await probes.store(
            conn, project_id=access.project_id, probe=body, source="admin", label=label, user_id=user.user_id, **target
        )
        await _audited(conn, access, user, probe_id, what)
        view = await _shown(conn, access, user, probe_id)
    log.info("probe added", extra={"project": access.name, "probe_id": probe_id, "login": user.login})
    return view


@router.patch("/{project}/curator/probes/{probe_id}", response_model=probes.Probe, responses=REFUSALS)
async def edit_probe(
    request: Request, project: ProjectName, probe_id: ItemId, body: probes.ProbeEdit, user: CurrentUser
) -> probes.Probe:
    """Change the repo, commit, task or verify of an active probe; the project's admins alone may."""
    pr = tables.curator_probes
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "editing a probe")
        row = await _probe_row(conn, access, user, probe_id, None, lock=True)
        if row.state != "active":
            raise HTTPException(409, f"probe {probe_id} is {row.state}: only an active probe is edited")
        if body.repo is not None and body.repo not in await project_repos(conn, access.project_id):
            raise HTTPException(422, f"project {access.name} has no repo {body.repo}; nothing was changed")
        values = {{"commit": "base_sha"}.get(key, key): getattr(body, key) for key in sorted(body.model_fields_set)}
        await conn.execute(
            update(pr).values(**values, updated_by=user.user_id, updated_at=func.now()).where(pr.c.id == probe_id)
        )
        await _audited(conn, access, user, probe_id, f"edit {','.join(sorted(body.model_fields_set))}")
        view = await _shown(conn, access, user, probe_id)
    log.info("probe edited", extra={"project": access.name, "probe_id": probe_id, "login": user.login})
    return view


@router.post("/{project}/curator/probes/{probe_id}/retire", response_model=probes.Probe, responses=REFUSALS)
async def retire_probe(
    request: Request, project: ProjectName, probe_id: ItemId, body: probes.ProbeRetire, user: CurrentUser
) -> probes.Probe:
    """Retire a probe: no probe run takes it from then on; the project's admins alone may."""
    pr = tables.curator_probes
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _admin(access, "retiring a probe")
        row = await _probe_row(conn, access, user, probe_id, None, lock=True)
        if row.state == "retired":
            raise HTTPException(409, f"probe {probe_id} is retired already")
        values = {"state": "retired", "retired_at": func.now(), "retired_note": body.note}
        await conn.execute(
            update(pr).values(**values, updated_by=user.user_id, updated_at=func.now()).where(pr.c.id == probe_id)
        )
        await _audited(conn, access, user, probe_id, "retire")
        view = await _shown(conn, access, user, probe_id)
    log.info("probe retired", extra={"project": access.name, "probe_id": probe_id, "login": user.login})
    return view


# A review run's probe of a merged change


async def _replaceable(conn: AsyncConnection, run, access: ProjectAccess, change_id: int, probe_id: int) -> None:
    """422 unless the weekly probe report asks review run ``run`` to replace probe ``probe_id`` of change
    ``change_id`` (``probe_fixes.replace_assignment``)."""
    night = await probe_fixes.run_night(conn, run.id)
    found = await probe_fixes.replace_assignment(conn, access, run.owner, night, probe_id)
    if found is None or found.change_id != change_id:
        raise HTTPException(
            422,
            f"probe {probe_id} is not an active probe of change {change_id} that was not reproduced on one model in "
            "the two weekly reports before this run's week: the hub asks for a replacement only then; nothing was "
            "recorded",
        )


@worker_router.post("/runs/{run_id}/probes", status_code=201, response_model=probes.Probe, responses=REFUSALS)
async def record_probe(request: Request, run_id: RunId, body: probes.ReviewProbe, user: CurrentUser) -> probes.Probe:
    """A probe from the agent of the review run this worker holds, for a merged change that has none."""
    async with request.app.state.engine.begin() as conn:
        run = await held_review_run(conn, user, run_id)
        access = await writer_of(conn, run)
        change = await _change_target(conn, run.project_id, run.project, body.change_id)
        if change.state != "merged":
            raise HTTPException(
                409, f"change {change.id} is {change.state}: a review run adds a probe to a merged change alone"
            )
        found = await probes.active_on_change(conn, change.id)
        if body.replaces is not None:
            await _replaceable(conn, run, access, change.id, body.replaces)
        elif found is not None:
            raise HTTPException(409, f"change {change.id} has probe #{found} already")
        if body.repo not in await project_repos(conn, run.project_id):
            raise HTTPException(422, f"project {run.project} has no repo {body.repo}; nothing was recorded")
        pr = tables.curator_probes
        if body.replaces is not None:
            note = f"not reproduced two weekly reports in a row: review run #{run_id} replaced it"
            await conn.execute(
                update(pr)
                .values(state="retired", retired_at=func.now(), retired_note=note)
                .values(updated_by=run.owner.user_id, updated_at=func.now())
                .where(pr.c.id == body.replaces)
            )
        probe_id = await probes.store(
            conn,
            project_id=run.project_id,
            probe=body,
            source="review",
            label=change.label,
            proposal_id=change.proposal_id,
            change_id=change.id,
            user_id=run.owner.user_id,
            run_id=run_id,
            replaces=body.replaces,
        )
        what = f"probe #{probe_id} of change #{change.id}"
        what += f", in place of probe #{body.replaces}" if body.replaces is not None else ""
        await write_event(conn, run_id, {"text": what, "probe": probe_id})
        row = (await conn.execute(probes.query().where(pr.c.id == probe_id))).one()
    log.info("probe recorded", extra={"run_id": run_id, "probe_id": probe_id, "change_id": change.id})
    return probes.view(access.name, row, shown=False)

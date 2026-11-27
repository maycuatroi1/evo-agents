"""The routes of the Curator's probes (``models.probe`` holds the models, ``service.probes`` the store and the reads,
``service.writes`` the writes).

GET /v1/projects/{p}/curator/probes (reader) lists the probes whose label the caller's grant reaches through the sink
X-Evo-Sink names (else the project's hub sink), newest first, filtered by change, proposal and state; GET
.../curator/probes/{id} shows one. Their verify is null unless the caller is an admin of the project and not the agent
of a run (``service.probes.verify_shown``).

The project's admins write them (403 otherwise), each write audited as curator.probe: POST .../curator/probes adds one
to a change or to a proposal of the project (404 for one it does not have, 409 when it has an active probe already:
retire that one first); PATCH .../curator/probes/{id} changes its repo, commit, task or verify (409 once retired); POST
.../curator/probes/{id}/retire retires it, with a note (409 when it is retired already); POST
.../curator/probes/{id}/requeue puts the rows of the ISO week in progress (the charter's time zone) of an active probe
that are stale, failed or unmeasured back to queued with no attempt, for the next night, and keeps any other row and
every recorded session as they are (``service.runs.requeue``; 409 for a retired probe).

POST /v1/worker/runs/{id}/probes takes a probe from the agent of a review run the worker holds (404 for any other run),
as the member who dispatched it, who must still hold writer on the project, for a merged change of the project that has
no active probe (409 otherwise): ``evo-agents worker probe add``. Its answer shows no verify. With ``replaces``, the
probe takes the place of the change's active probe that the weekly report found not reproduced, or stale, on one model two weeks
in a row (``service.fixes``; 422 for any other probe), which the hub retires with a note.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request

from evo_agents.hub import probes as probe_shape
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.probes.models import probe as probes
from evo_agents.hub.server.probes.models import runs as probe_runs
from evo_agents.hub.server.probes.service import probes as service
from evo_agents.hub.server.probes.service import writes
from evo_agents.hub.server.proposals.models import ItemId
from evo_agents.hub.server.runs.models import MAX_ID, RunId
from evo_agents.hub.server.security import CurrentUser

MAX_LIST = 200

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}


# Reading


@router.get("/{project}/curator/probes", response_model=probes.ProbeList, responses=READ_REFUSALS)
async def list_probes(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    change: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the probes of this change")] = None,
    proposal: Annotated[int | None, Query(ge=1, le=MAX_ID, description="the probes of this proposal")] = None,
    state: Annotated[
        Literal[probe_shape.STATES] | None,  # pyright: ignore[reportInvalidTypeForm]
        Query(description="active or retired; both by default"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIST)] = 50,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> probes.ProbeList:
    """The project's probes the caller may read, newest first."""
    filters = {"change": change, "proposal": proposal, "state": state, "limit": limit, "sink": sink}
    return await service.listed(request.app.state.engine, user, project, **filters)


@router.get("/{project}/curator/probes/{probe_id}", response_model=probes.Probe, responses=READ_REFUSALS)
async def show_probe(
    request: Request,
    project: ProjectName,
    probe_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_models.SINK_HEADER)] = None,
) -> probes.Probe:
    return await service.one(request.app.state.engine, user, project, probe_id, sink)


# The admins' writes


@router.post("/{project}/curator/probes", status_code=201, response_model=probes.Probe, responses=REFUSALS)
async def add_probe(request: Request, project: ProjectName, body: probes.ProbeAdd, user: CurrentUser) -> probes.Probe:
    """Add a probe to a change or a proposal of the project; its admins alone may."""
    return await writes.add_probe(request.app.state.engine, user, project, body)


@router.patch("/{project}/curator/probes/{probe_id}", response_model=probes.Probe, responses=REFUSALS)
async def edit_probe(
    request: Request, project: ProjectName, probe_id: ItemId, body: probes.ProbeEdit, user: CurrentUser
) -> probes.Probe:
    """Change the repo, commit, task or verify of an active probe; the project's admins alone may."""
    return await writes.edit_probe(request.app.state.engine, user, project, probe_id, body)


@router.post("/{project}/curator/probes/{probe_id}/retire", response_model=probes.Probe, responses=REFUSALS)
async def retire_probe(
    request: Request, project: ProjectName, probe_id: ItemId, body: probes.ProbeRetire, user: CurrentUser
) -> probes.Probe:
    """Retire a probe: no probe run takes it from then on; the project's admins alone may."""
    return await writes.retire_probe(request.app.state.engine, user, project, probe_id, body)


@router.post("/{project}/curator/probes/{probe_id}/requeue", response_model=probe_runs.ProbeRequeue, responses=REFUSALS)
async def requeue_probe(
    request: Request, project: ProjectName, probe_id: ItemId, user: CurrentUser
) -> probe_runs.ProbeRequeue:
    """Measure an active probe again this ISO week: its rows of the week that are stale, failed or unmeasured go back to
    queued with no attempt, for the next night; the project's admins alone may."""
    return await writes.requeue_probe(request.app.state.engine, user, project, probe_id)


# A review run's probe of a merged change


@worker_router.post("/runs/{run_id}/probes", status_code=201, response_model=probes.Probe, responses=REFUSALS)
async def record_probe(request: Request, run_id: RunId, body: probes.ReviewProbe, user: CurrentUser) -> probes.Probe:
    """A probe from the agent of the review run this worker holds, for a merged change that has none."""
    return await writes.record_probe(request.app.state.engine, user, run_id, body)

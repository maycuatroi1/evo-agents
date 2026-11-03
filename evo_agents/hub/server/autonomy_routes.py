"""The routes of the autonomy levels (plan autonomy-levels, criteria a2 and a3), under /v1/autonomy.

- GET /v1/autonomy: the effective autonomy of a person (the caller without ``login``), running an agent profile
  (``agent``) on a repo (``repo``) of a project when those are named: the level, each class K1 to K12 with its mode and
  the source that sets it, and the environments and workers allowed. The person sees their own, a lead those of the
  people in the units they lead, a hub admin everyone's; anyone else gets 403.
- PUT /v1/autonomy/prefs: the next revision of the caller's own preference.
- GET and PUT /v1/autonomy/run-reach: the caller's own run reach, the groups of ``run_access_rules.REACH_GROUPS`` the
  tokens of their runs that run as them reach; PUT replaces it whole and audits ``run_access.reach``. No run's token
  reaches either (the group run access of ``run_access_rules``), so no run widens its own reach.

A ceiling, with its class overrides, environments and workers, is written at PUT /v1/org/ceilings
(``evo_agents.hub.server.org``); both writes audit an ``autonomy.`` action.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub.server import autonomy_store, route_autonomy
from evo_agents.hub.server.autonomy_models import (
    AutonomyView,
    PrefSet,
    PrefWritten,
    RunReach,
    RunReachSet,
    RunReachWritten,
)
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.org_models import HANDLE_PATTERN, LOGIN_PATTERN
from evo_agents.hub.server.security import CurrentUser

Name = Annotated[str | None, Query(min_length=1, max_length=100)]  # a project or a repo, as the hub names them
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 422)}

router = APIRouter(
    prefix="/v1/autonomy", tags=["autonomy"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)


@router.get("", response_model=AutonomyView, responses=REFUSALS)
async def show_autonomy(
    request: Request,
    user: CurrentUser,
    login: Annotated[str | None, Query(pattern=LOGIN_PATTERN)] = None,
    project: Name = None,
    agent: Annotated[str | None, Query(pattern=HANDLE_PATTERN)] = None,
    repo: Name = None,
) -> AutonomyView:
    """The effective autonomy of a person, alone or running an agent profile on a repo, with the source of each mode."""
    async with request.app.state.engine.begin() as conn:
        return await autonomy_store.view(conn, user, login, project=project, agent=agent, repo=repo)


@router.put("/prefs", response_model=PrefWritten, responses=REFUSALS)
async def set_pref(request: Request, body: PrefSet, user: CurrentUser) -> PrefWritten:
    """The next revision of the caller's own preference; one above their ceilings is stored, the ceilings still
    decide."""
    async with request.app.state.engine.begin() as conn:
        return await autonomy_store.write_pref(conn, user, body)


@router.get("/run-reach", response_model=RunReach, responses=REFUSALS)
async def show_run_reach(request: Request, user: CurrentUser) -> RunReach:
    """The groups the caller's runs that run as them reach besides what every run's token reaches."""
    async with request.app.state.engine.begin() as conn:
        return await autonomy_store.run_reach(conn, user)


@router.put("/run-reach", response_model=RunReachWritten, responses=REFUSALS)
async def set_run_reach(request: Request, body: RunReachSet, user: CurrentUser) -> RunReachWritten:
    """Replace the caller's own run reach; an empty list clears it. The next request of each of their runs reads it."""
    async with request.app.state.engine.begin() as conn:
        return await autonomy_store.write_run_reach(conn, user, body)

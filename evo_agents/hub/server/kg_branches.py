"""Draft branches of a project's graph (``docs/kg.md``, Draft branches; ``evo_agents.hub.kg_branches``).

- GET /v1/kg/{project}/branches?status=: the project's branches, newest first.
- POST /v1/kg/{project}/branches {name}: a writer, or the agent of a run of the project, opens one from main.
- GET /v1/kg/{project}/branches/{name}: one branch.
- POST /v1/kg/{project}/branches/{name}/ops {ops}: applies operations to it; main does not change.
- GET /v1/kg/{project}/branches/{name}/diff: its structural diff from main.
- GET /v1/kg/{project}/branches/{name}/check: the ontology rules it breaks and whether it would merge now.
- POST /v1/kg/{project}/branches/{name}/merge: merges it into main, 409 on a conflict with main or a new violation of
  a hard rule.
- DELETE /v1/kg/{project}/branches/{name}: drops an open branch.

The kg tools (``branch``) and the web's graph routes (``?branch=``) read a branch as main with its operations applied.
Every write is audited as ``kg.branch``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse

from evo_agents.hub import kg_branches as store
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.kg.models.branches import BranchCreate, BranchesOut, BranchOps, BranchOut, BranchResult
from evo_agents.hub.server.kg.service import branches as service
from evo_agents.hub.server.kg_graphs import may_change
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)},
)

BranchName = Annotated[str, PathParam(min_length=1, max_length=63)]


def _refused(request: Request, exc: store.BranchError) -> JSONResponse:
    """The error a refused branch operation answers, with what it conflicts with or the hard violations as detail."""
    detail = [item for items in exc.detail.values() for item in items] or None
    return error_response(request, exc.status, str(exc), detail=detail)


@router.get("/{project}/branches", response_model=BranchesOut)
async def list_branches(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal["open", "merged", "closed"] | None, Query()] = None,
) -> BranchesOut:
    """The draft branches of the project's graph, newest first."""
    return await service.branches(request.app.state.engine, user, project, status)


@router.post("/{project}/branches", response_model=BranchOut, status_code=201)
async def create_branch(
    request: Request, project: ProjectName, body: BranchCreate, user: CurrentUser
) -> BranchOut | JSONResponse:
    """Open a draft branch of the project's graph from main."""
    try:
        return await service.create(request.app.state.engine, user, project, body.name, may_change)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.get("/{project}/branches/{name}", response_model=BranchOut)
async def show_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchOut | JSONResponse:
    """One draft branch of the project's graph."""
    try:
        return await service.show(request.app.state.engine, user, project, name)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.post("/{project}/branches/{name}/ops", response_model=BranchResult)
async def apply_ops(
    request: Request, project: ProjectName, name: BranchName, body: BranchOps, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Apply node, edge, entity map and decision operations to an open branch, all or none; main does not change."""
    try:
        return await service.apply(request.app.state.engine, user, project, name, list(body.ops), may_change)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.get("/{project}/branches/{name}/diff", response_model=BranchResult)
async def diff_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """The nodes and edges a branch adds, changes and removes from main."""
    try:
        return await service.diff(request.app.state.engine, user, project, name)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.get("/{project}/branches/{name}/check", response_model=BranchResult)
async def check_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """The ontology rules a branch breaks, those main does not, its conflicts with main, and whether it would merge."""
    try:
        return await service.check(request.app.state.engine, user, project, name)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.post("/{project}/branches/{name}/merge", response_model=BranchResult)
async def merge_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Merge an open branch into main in one transaction: 409 when main changed what it changes, or it adds a
    violation of a hard ontology rule."""
    try:
        return await service.merge(request.app.state.engine, user, project, name, may_change)
    except store.BranchError as exc:
        return _refused(request, exc)


@router.delete("/{project}/branches/{name}", response_model=BranchResult)
async def drop_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Drop an open branch and its operations; main does not change."""
    try:
        return await service.drop(request.app.state.engine, user, project, name, may_change)
    except store.BranchError as exc:
        return _refused(request, exc)

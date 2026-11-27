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

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from evo_agents.hub import kg_branches as store
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody, error_response
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.kg_graphs import may_change
from evo_agents.hub.server.kg_web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg import branch_ops

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 409, 422)},
)

BranchName = Annotated[str, PathParam(min_length=1, max_length=63)]


class BranchOut(BaseModel):
    id: int
    name: str
    status: str = Field(description="open, merged or closed")
    base_build_id: int | None = Field(description="the build of main it was opened at")
    created_by: int | None
    created_at: str
    ops: int = Field(description="operations applied to it")


class BranchesOut(BaseModel):
    branches: list[BranchOut]


class BranchCreate(BaseModel):
    name: str = Field(min_length=1, max_length=63, description="a-z, 0-9, '.', '_', '-'; not 'main'")


class BranchOps(BaseModel):
    ops: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=branch_ops.MAX_OPS,
        description="operations, each with op: " + ", ".join(branch_ops.OPS),
    )


class BranchResult(BaseModel):
    """What a write, the diff or the check returned (``evo_agents.hub.kg_branches``)."""

    model_config = {"extra": "allow"}


def _refused(request: Request, exc: store.BranchError) -> JSONResponse:
    """The error a refused branch operation answers, with what it conflicts with or the hard violations as detail."""
    detail = [item for items in exc.detail.values() for item in items] or None
    return error_response(request, exc.status, str(exc), detail=detail)


async def _audit(conn, user, access, project: str, name: str, kind: str, ops: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_BRANCH,
        target=f"{project} branch={name} kind={kind} ops={ops}",
        project_id=access.project_id,
    )


@router.get("/{project}/branches", response_model=BranchesOut)
async def list_branches(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal["open", "merged", "closed"] | None, Query()] = None,
) -> BranchesOut:
    """The draft branches of the project's graph, newest first."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        found = await store.branches(conn, access.project_id, status)
    return BranchesOut(branches=[BranchOut(**b.as_dict()) for b in found])


@router.post("/{project}/branches", response_model=BranchOut, status_code=201)
async def create_branch(
    request: Request, project: ProjectName, body: BranchCreate, user: CurrentUser
) -> BranchOut | JSONResponse:
    """Open a draft branch of the project's graph from main."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            made = await store.create(conn, access.project_id, body.name, user.user_id)
        except store.BranchError as exc:
            return _refused(request, exc)
        await _audit(conn, user, access, project, made.name, "create", 0)
    return BranchOut(**made.as_dict())


@router.get("/{project}/branches/{name}", response_model=BranchOut)
async def show_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchOut | JSONResponse:
    """One draft branch of the project's graph."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        try:
            found = await store.get(conn, access.project_id, name)
        except store.BranchError as exc:
            return _refused(request, exc)
    return BranchOut(**found.as_dict())


@router.post("/{project}/branches/{name}/ops", response_model=BranchResult)
async def apply_ops(
    request: Request, project: ProjectName, name: BranchName, body: BranchOps, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Apply node, edge, entity map and decision operations to an open branch, all or none; main does not change."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            writer = await _reader(conn, user, access)
            done = await store.apply(conn, access.project_id, name, list(body.ops), user.user_id, writer)
        except store.BranchError as exc:
            return _refused(request, exc)
        await _audit(conn, user, access, project, name, "apply", done["applied"])
    return BranchResult(**done)


async def _reader(conn, user, access) -> store.Reader:
    """The audience and ceiling (level, location) of ``user``'s grant, as the kg reads of ``kg_web`` take them."""
    audience = await reader_audience(conn, user)
    ceiling = access.rules.grant_label(access.max_level, audience)
    return audience, None if ceiling is None else (ceiling.level, ceiling.location)


@router.get("/{project}/branches/{name}/diff", response_model=BranchResult)
async def diff_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """The nodes and edges a branch adds, changes and removes from main."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        reader = await _reader(conn, user, access)
        try:
            return BranchResult(**await store.diff(conn, access.project_id, name, reader))
        except store.BranchError as exc:
            return _refused(request, exc)


@router.get("/{project}/branches/{name}/check", response_model=BranchResult)
async def check_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """The ontology rules a branch breaks, those main does not, its conflicts with main, and whether it would merge."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        reader = await _reader(conn, user, access)
        try:
            return BranchResult(**await store.check(conn, access.project_id, name, reader))
        except store.BranchError as exc:
            return _refused(request, exc)


@router.post("/{project}/branches/{name}/merge", response_model=BranchResult)
async def merge_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Merge an open branch into main in one transaction: 409 when main changed what it changes, or it adds a
    violation of a hard ontology rule."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            done = await store.merge(conn, access.project_id, name, user.user_id)
        except store.BranchError as exc:
            return _refused(request, exc)
        await _audit(conn, user, access, project, name, "merge", done["ops"])
    return BranchResult(**done)


@router.delete("/{project}/branches/{name}", response_model=BranchResult)
async def drop_branch(
    request: Request, project: ProjectName, name: BranchName, user: CurrentUser
) -> BranchResult | JSONResponse:
    """Drop an open branch and its operations; main does not change."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            done = await store.drop(conn, access.project_id, name)
        except store.BranchError as exc:
            return _refused(request, exc)
        await _audit(conn, user, access, project, name, "drop", done["ops"])
    return BranchResult(**done)

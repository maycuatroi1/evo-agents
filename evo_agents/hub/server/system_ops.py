"""The default system of a project and the ops of a system (plan hub-steward, criteria a7 and a11; ``docs/hub.md``,
Agent systems).

- GET /v1/projects/{p}/default-system: the project's default system (``slug`` null when it has none), its ``ops`` and
  ``limits``.
- PUT /v1/projects/{p}/default-system {slug}: make that system the default, and no other (admin; audited as
  ``system.default``).
- GET /v1/projects/{p}/systems/{slug}/ops: the ops of a system: its spec's ``ops`` (``system_spec.ops_of``), its
  ``limits.ops_daily_usd``, whether they are paused, and the batches of the project's queue by state when it is the
  default.
- POST .../systems/{slug}/ops/pause {reason} and .../ops/resume: pause or resume them (admin; audited as
  ``system.ops-pause`` and ``system.ops-resume``). Paused, the queue still gathers; a closed batch opens no thread.
- GET .../systems/{slug}/ops/batches[?state=&limit=]: the batches of the project's queue, the newest first; empty for
  a system that is not the default.

Every member of the project reads; changing needs the admin role on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ops_queue, system_spec, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, route_autonomy, system_store
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_systems import Slug, admin_only, not_found, reader_only
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(
    prefix="/v1/projects", tags=["systems"], dependencies=[route_autonomy.K12], responses={401: {"model": ErrorBody}}
)
REFUSALS: dict = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
BatchState = Literal["open", "ready", "empty", "running", "done"]
MAX_REASON_CHARS = 200


class OpsSettings(BaseModel):
    enabled: bool = Field(description="the project's operations events go to the queue of its default system")
    debounce_minutes: int = Field(description="a batch closes this long after it opened")
    max_items: int = Field(description="or once it holds this many items")


class DefaultSystem(BaseModel):
    project: str
    slug: str | None = Field(description="the default system; null when the project has none")
    name: str | None
    ops: OpsSettings | None = Field(description="the ops of its spec, defaults filled in; off for a spec with none")
    limits: dict | None = Field(description="the limits of its spec, ops_daily_usd filled in")
    can_change: bool = Field(description="the caller holds the admin role on the project")


class DefaultPut(BaseModel):
    slug: str = Field(min_length=1, max_length=63, pattern=system_spec.SLUG_PATTERN)


class SystemOps(BaseModel):
    project: str
    slug: str
    default: bool = Field(description="it is the project's default system, whose queue the hub fills")
    ops: OpsSettings
    ops_daily_usd: float = Field(description="what the runs of its ops threads may spend in 24 hours")
    paused: bool
    paused_at: datetime | None
    paused_reason: str | None
    batches: dict[str, int] = Field(description="the batches of the project's queue by state; empty when not default")


class OpsPause(BaseModel):
    reason: str | None = Field(None, max_length=MAX_REASON_CHARS, description="why, shown with the pause")


class OpsBatch(BaseModel):
    id: int
    key: str = Field(description="kind|cause|subject: the cause key its items share")
    kind: str
    cause: str
    subject: str
    state: BatchState
    items: int
    pending: int = Field(description="its items tier 0 has not handled")
    opened_at: datetime
    closes_at: datetime
    closed_at: datetime | None
    run_id: int | None


def _limits(spec: dict) -> dict:
    found = dict(spec.get("limits") or {})
    found["ops_daily_usd"] = system_spec.limit_of(spec, "ops_daily_usd")
    return found


def _default_out(access: ProjectAccess, held: system_store.System | None) -> DefaultSystem:
    can_change = has_role(access.role, "admin")
    if held is None:
        return DefaultSystem(project=access.name, slug=None, name=None, ops=None, limits=None, can_change=can_change)
    return DefaultSystem(
        project=access.name,
        slug=held.slug,
        name=held.name,
        ops=OpsSettings(**system_spec.ops_of(held.spec)),
        limits=_limits(held.spec),
        can_change=can_change,
    )


async def _counts(conn: AsyncConnection, project_id: int) -> dict[str, int]:
    b = tables.ops_batches
    query = select(b.c.state, func.count()).where(b.c.project_id == project_id).group_by(b.c.state)
    found = {state: 0 for state in ops_queue.BATCH_STATES}
    found.update({state: n for state, n in await conn.execute(query)})
    return found


async def _ops_out(conn: AsyncConnection, access: ProjectAccess, held: system_store.System) -> SystemOps:
    return SystemOps(
        project=access.name,
        slug=held.slug,
        default=held.is_default,
        ops=OpsSettings(**system_spec.ops_of(held.spec)),
        ops_daily_usd=system_spec.limit_of(held.spec, "ops_daily_usd"),
        paused=held.ops_paused_at is not None,
        paused_at=held.ops_paused_at,
        paused_reason=held.ops_paused_reason,
        batches=await _counts(conn, access.project_id) if held.is_default else {},
    )


async def _record(conn, user, access: ProjectAccess, action: str, held: system_store.System) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=action,
        target=f"{access.name} system={held.slug} revision={held.revision}",
        project_id=access.project_id,
    )


async def _held(conn, access: ProjectAccess, slug: str, *, lock: bool = False) -> system_store.System:
    held = await system_store.one(conn, access.project_id, slug, lock=lock)
    if held is None:
        raise not_found(access, slug)
    return held


# The default system


@router.get("/{project}/default-system", response_model=DefaultSystem, responses=REFUSALS)
async def show_default(request: Request, project: ProjectName, user: CurrentUser) -> DefaultSystem:
    """The project's default system, its ops and limits; ``slug`` null when it has none."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        reader_only(access)
        held = await system_store.default_of(conn, access.project_id)
    return _default_out(access, held)


@router.put("/{project}/default-system", response_model=DefaultSystem, responses=REFUSALS)
async def set_default(request: Request, project: ProjectName, body: DefaultPut, user: CurrentUser) -> DefaultSystem:
    """Make system ``slug`` the project's default system, and no other."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "setting the default of")
        await system_store.default_of(conn, access.project_id, lock=True)
        held = await _held(conn, access, body.slug, lock=True)
        if not held.is_default:
            await system_store.set_default(conn, access.project_id, held.id)
            await _record(conn, user, access, audit.SYSTEM_DEFAULT, held)
        held = await system_store.default_of(conn, access.project_id)
    return _default_out(access, held)


# The ops of a system


@router.get("/{project}/systems/{slug}/ops", response_model=SystemOps, responses=REFUSALS)
async def show_ops(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemOps:
    """The ops of a system: its spec's ops, its ops budget, its pause, and the batches of its queue by state."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        reader_only(access)
        return await _ops_out(conn, access, await _held(conn, access, slug))


async def _pause(request: Request, project: str, slug: str, user, reason: str | None) -> SystemOps:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "pausing or resuming the ops of")
        held = await _held(conn, access, slug, lock=True)
        if (held.ops_paused_at is None) != (reason is None):
            await system_store.set_paused(conn, held.id, reason)
            action = audit.SYSTEM_OPS_RESUME if reason is None else audit.SYSTEM_OPS_PAUSE
            await _record(conn, user, access, action, held)
        return await _ops_out(conn, access, await _held(conn, access, slug))


@router.post("/{project}/systems/{slug}/ops/pause", response_model=SystemOps, responses=REFUSALS)
async def pause_ops(request: Request, project: ProjectName, slug: Slug, body: OpsPause, user: CurrentUser) -> SystemOps:
    """Pause the ops of a system: the queue still gathers, and a closed batch opens no thread until resumed."""
    reason = (body.reason or "").strip() or f"paused by {user.login}"
    return await _pause(request, project, slug, user, reason)


@router.post("/{project}/systems/{slug}/ops/resume", response_model=SystemOps, responses=REFUSALS)
async def resume_ops(request: Request, project: ProjectName, slug: Slug, user: CurrentUser) -> SystemOps:
    """Resume the ops of a system, paused by an admin or by the hub."""
    return await _pause(request, project, slug, user, None)


@router.get("/{project}/systems/{slug}/ops/batches", response_model=list[OpsBatch], responses=REFUSALS)
async def list_batches(
    request: Request,
    project: ProjectName,
    slug: Slug,
    user: CurrentUser,
    state: Annotated[BatchState | None, Query(description="only the batches in this state")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[OpsBatch]:
    """The batches of the project's queue, the newest first; none for a system that is not the default."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        reader_only(access)
        held = await _held(conn, access, slug)
        if not held.is_default:
            return []
        b, i = tables.ops_batches, tables.ops_items
        items = select(func.count()).where(i.c.batch_id == b.c.id).scalar_subquery()
        pending = select(func.count()).where(i.c.batch_id == b.c.id, i.c.state == "pending").scalar_subquery()
        query = select(b, items.label("items"), pending.label("pending")).where(b.c.project_id == access.project_id)
        if state is not None:
            query = query.where(b.c.state == state)
        rows = await conn.execute(query.order_by(b.c.id.desc()).limit(limit))
        fields = set(OpsBatch.model_fields)
        return [OpsBatch(**{k: v for k, v in row._mapping.items() if k in fields}) for row in rows]

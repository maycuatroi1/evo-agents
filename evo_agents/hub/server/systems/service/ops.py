"""The default system of a project and the ops of a system (plan hub-steward, criteria a7 and a11; ``docs/hub.md``,
Agent systems): what the routes of ``routes.ops`` read and change.

Every member of the project reads; changing needs the admin role on it, and is audited as ``system.default``,
``system.ops-pause`` or ``system.ops-resume``. Paused, the queue still gathers; a closed batch opens no thread.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import system_spec
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.ops.models import views as ops_views
from evo_agents.hub.server.ops.repository import threads as ops_threads
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.systems.models.ops import DefaultSystem, OpsBatch, OpsSettings, SystemOps
from evo_agents.hub.server.systems.repository import ops as ops_rows
from evo_agents.hub.server.systems.repository import store as system_store
from evo_agents.hub.server.systems.service.systems import admin_only, held_system, reader


def _limits(spec: dict) -> dict:
    found = dict(spec.get("limits") or {})
    found["ops_daily_usd"] = system_spec.limit_of(spec, "ops_daily_usd")
    return found


def _default_out(access: ProjectAccess, held: system_store.System | None) -> DefaultSystem:
    can_change = holds(access.role, "admin")
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
        spent_usd=await ops_threads.spent(conn, held.id),
        batches=await ops_rows.counts(conn, access.project_id) if held.is_default else {},
    )


async def _record(conn, user, access: ProjectAccess, action: str, held: system_store.System) -> None:
    await Auditor.of(conn, user, access).record(action, f"{access.name} system={held.slug} revision={held.revision}")


# The default system


async def default_shown(conn: AsyncConnection, user: Principal, project: str) -> DefaultSystem:
    """The project's default system, its ops and limits; ``slug`` null when it has none."""
    access = await reader(conn, user, project)
    return _default_out(access, await system_store.default_of(conn, access.project_id))


async def set_default(engine: AsyncEngine, user: Principal, project: str, slug: str) -> DefaultSystem:
    """Make system ``slug`` the project's default system, and no other."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "setting the default of")
        await system_store.default_of(conn, access.project_id, lock=True)
        held = await held_system(conn, access, slug, lock=True)
        if not held.is_default:
            await system_store.set_default(conn, access.project_id, held.id)
            await _record(conn, user, access, audit.SYSTEM_DEFAULT, held)
        return _default_out(access, await system_store.default_of(conn, access.project_id))


# The ops of a system


async def ops_shown(conn: AsyncConnection, user: Principal, project: str, slug: str) -> SystemOps:
    """The ops of a system: its spec's ops, its ops budget, its pause, and the batches of its queue by state."""
    access = await reader(conn, user, project)
    return await _ops_out(conn, access, await held_system(conn, access, slug))


async def pause(engine: AsyncEngine, user: Principal, project: str, slug: str, reason: str | None) -> SystemOps:
    """Pause the ops of a system for ``reason``, or resume them when it is None."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        admin_only(access, "pausing or resuming the ops of")
        held = await held_system(conn, access, slug, lock=True)
        if (held.ops_paused_at is None) != (reason is None):
            await system_store.set_paused(conn, held.id, reason)
            action = audit.SYSTEM_OPS_RESUME if reason is None else audit.SYSTEM_OPS_PAUSE
            await _record(conn, user, access, action, held)
        return await _ops_out(conn, access, await held_system(conn, access, slug))


async def batches(
    conn: AsyncConnection, user: Principal, project: str, slug: str, state: str | None, limit: int
) -> list[OpsBatch]:
    """The batches of the project's queue, the newest first; none for a system that is not the default."""
    access = await reader(conn, user, project)
    held = await held_system(conn, access, slug)
    if not held.is_default:
        return []
    fields = set(OpsBatch.model_fields)
    return [
        OpsBatch(
            **{k: v for k, v in row.items() if k in fields},
            thread=ops_views.thread_path(row["chat_id"], row["root_message_id"]),
        )
        for row in await ops_rows.batches(conn, access.project_id, state, limit)
    ]

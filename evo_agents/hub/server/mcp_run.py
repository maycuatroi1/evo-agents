"""The agent of a run on /mcp (``evo_agents.hub.server.mcp``): the run a worker token names in X-Evo-Run, which that
worker must hold now, and the principal its agent acts as, the run's owner within the run's scope. The hub runtime's
turn (``hub_runtime``) holds a run of no worker, and its tools act as the same principal (``hub_principal``)."""

from __future__ import annotations

from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import question_runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.runs import HELD_STATES
from evo_agents.hub.server.security import Principal, RunScope, admin_login

MOUNT = "/mcp"  # where the hub mounts its MCP server (``evo_agents.hub.server.mcp``)
NOT_HELD = (
    f"this worker does not hold run {{run}} now: it ended, or another worker holds it, and {MOUNT} opens for the "
    "agent of a run its worker holds only"
)


def _held_run(token_id: int | None, run_id: int):
    """The run ``run_id`` that the live worker of token ``token_id`` holds: its state, its project, the grant of its
    owner there (role and max_level None without one), its kind, and its owner, the member who dispatched it, by id and
    login, and the project's ladder and default label. That is the worker's owner, or another writer of the project on
    a shared worker (``docs/workers.md``). Then what its memories need: its profile, version, the handle of a profile
    that is not its project's default, its hop, chat and chat message. With ``token_id`` None, the run of no worker
    the hub runtime holds."""
    workers, runs, projects, grants, users = tables.workers, tables.runs, tables.projects, tables.grants, tables.users
    profiles = tables.agent_profiles
    query = (
        select(
            runs.c.state,
            projects.c.name,
            grants.c.role,
            grants.c.max_level,
            runs.c.kind,
            runs.c.dispatched_by,
            users.c.login,
            admin_login(users).label("admin_login"),
            runs.c.profile_id,
            runs.c.profile_version,
            case((profiles.c.is_default, None), else_=profiles.c.handle).label("agent"),
            runs.c.hop,
            runs.c.chat_id,
            runs.c.chat_message_id,
            projects.c.levels,
            projects.c.default_label,
        )
        .select_from(
            runs.join(projects, projects.c.id == runs.c.project_id)
            .join(users, users.c.id == runs.c.dispatched_by)
            .outerjoin(grants, and_(grants.c.project_id == runs.c.project_id, grants.c.user_id == runs.c.dispatched_by))
            .outerjoin(profiles, profiles.c.id == runs.c.profile_id)
        )
        .where(runs.c.id == run_id)
    )
    if token_id is None:
        return query.where(runs.c.worker_id.is_(None))
    live = select(workers.c.id).where(workers.c.token_id == token_id, workers.c.revoked_at.is_(None))
    return query.where(runs.c.worker_id.in_(live))


async def run_principal(conn: AsyncConnection, user: Principal, run_id: int) -> Principal:
    """The agent of run ``run_id`` for the worker whose token ``user`` authenticated: the run's owner, the member who
    dispatched it, never a hub admin, within the run's scope (its project, the owner's grant there with its role capped
    at writer; a question run's reader, up to the level of the project's default label). On a shared worker the owner
    of the run may be another member than the worker's. HTTPException 403 when that worker does not hold the run now
    (another worker's, one that ended, none) or the run's owner holds no grant on the project any more."""
    row = (await conn.execute(_held_run(user.token_id, run_id))).first()
    if row is None or row.state not in HELD_STATES:
        raise HTTPException(403, NOT_HELD.format(run=run_id))
    scope, owner = _scoped(row, run_id)
    return replace(user, **owner, admin=False, scope=scope)


async def hub_principal(conn: AsyncConnection, run_id: int, kind: str) -> Principal:
    """``run_principal`` of run ``run_id``, which the hub runtime holds with no worker, acting as a credential of
    ``kind`` without a token."""
    row = (await conn.execute(_held_run(None, run_id))).first()
    if row is None or row.state not in HELD_STATES:
        raise HTTPException(403, f"the hub runtime does not hold run {run_id} now")
    scope, owner = _scoped(row, run_id)
    return Principal(**owner, admin=False, token_id=None, kind=kind, token_hash="", scope=scope)


def _scoped(row, run_id: int) -> tuple[RunScope, dict]:
    """(the scope, the owner's fields of a Principal) of the run of ``row``; 403 when its owner holds no grant on its
    project any more."""
    project, role, max_level, kind = row.name, row.role, row.max_level, row.kind
    if role is None:
        raise HTTPException(403, f"the owner of run {run_id} holds no grant on project {project} any more")
    role = "writer" if has_role(role, "writer") else role
    if kind == "question":  # its answer leaves the hub: it reads, up to the label the answer carries
        role, max_level = "reader", question_runs.lowered_level(row.levels, max_level, row.default_label)
    memory = (row.profile_id, row.profile_version, row.agent, row.hop, row.chat_id, row.chat_message_id)
    scope = RunScope(run_id, project, role, max_level, kind, *memory)
    return scope, {"user_id": row.dispatched_by, "login": row.login, "admin_login": row.admin_login}

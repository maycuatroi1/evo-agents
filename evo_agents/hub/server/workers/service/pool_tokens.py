"""The join tokens of a pool: ``evj_`` and TOKEN_BYTES random bytes in base64url, shown once, kept as their hex
SHA-256 alone; making, listing and revoking them, and what a node joining with one takes from its pool
(``take_token``, ``node_name``)."""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.security import Principal, hash_token
from evo_agents.hub.server.workers.models.pools import TOKEN_PREFIX, IssuedJoinToken, JoinToken, JoinTokenRequest
from evo_agents.hub.server.workers.models.workers import WORKER_NAME
from evo_agents.hub.server.workers.repository import pools as store
from evo_agents.hub.server.workers.service.pools import pool_row, target

log = logging.getLogger(__name__)

TOKEN_BYTES = 32
MAX_NODE_NAME = 100  # as WORKER_NAME takes it


def new_join_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def join_token(row, pool: str) -> JoinToken:
    fields = dict(row._mapping)
    expired = fields.pop("expired")
    if fields["revoked_at"] is not None:
        status = "revoked"
    elif fields["uses"] >= fields["max_uses"]:
        status = "used"
    elif expired:
        status = "expired"
    else:
        status = "live"
    return JoinToken(**fields, pool=pool, status=status)


async def create(conn: AsyncConnection, user: Principal, name: str, body: JoinTokenRequest) -> IssuedJoinToken:
    """A join token of the pool ``name``; the token shown once."""
    token = new_join_token()
    pool = await pool_row(conn, user, name)
    token_id = await store.insert_token(conn, pool.id, hash_token(token), user.user_id, body.hours, body.max_uses)
    shown = f"{target(pool.id, pool.name)} join_token:{token_id} hours={body.hours} max_uses={body.max_uses}"
    await Auditor(conn, None, user).record(audit.WORKER_POOL_TOKEN, shown)
    row = await store.token_row(conn, pool.id, token_id)
    log.info("worker pool join token created", extra={"pool_id": pool.id, "join_token_id": token_id})
    return IssuedJoinToken(**join_token(row, pool.name).model_dump(), token=token)


async def listed(conn: AsyncConnection, user: Principal, name: str) -> list[JoinToken]:
    pool = await pool_row(conn, user, name)
    return [join_token(row, pool.name) for row in await store.token_rows(conn, pool.id)]


async def revoke(conn: AsyncConnection, user: Principal, name: str, token_id: int) -> JoinToken:
    """Revoke join token ``token_id`` of the pool ``name``; 404 for none, 409 when it is revoked already."""
    pool = await pool_row(conn, user, name)
    found = await store.locked_token(conn, pool.id, token_id)
    if found is None:
        raise NotFound(f"pool {pool.name} has no join token {token_id}: see its token list")
    if found.revoked_at is not None:
        raise Conflict(f"join token {token_id} was revoked already, at {found.revoked_at.isoformat()}")
    await store.revoke_token(conn, token_id)
    shown = f"{target(pool.id, pool.name)} join_token:{token_id}"
    await Auditor(conn, None, user).record(audit.WORKER_POOL_TOKEN_REVOKE, shown)
    row = await store.token_row(conn, pool.id, token_id)
    log.info("worker pool join token revoked", extra={"pool_id": pool.id, "join_token_id": token_id})
    return join_token(row, pool.name)


# Joining with a token (``join``)


@dataclass(frozen=True)
class Joining:
    """What a node joining a pool takes from it."""

    pool_id: int
    pool: str
    owner_id: int
    slots: int
    project_ids: tuple[int, ...]
    token_id: int


async def take_token(conn: AsyncConnection, token: str) -> Joining | None:
    """The pool a live join token ``token`` joins, with the join counted against it, its row and its pool's locked
    (so the joins of one pool name their nodes one at a time); None for a token that is unknown, expired, revoked or
    used up, which changes nothing."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = await store.take_token(conn, hash_token(token))
    if row is None:
        return None
    projects = await store.pool_projects(conn, row.pool_id)
    return Joining(row.pool_id, row.name, row.owner_id, row.slots, tuple(projects.values()), row.id)


def host_part(hostname: str) -> str:
    """The first label of ``hostname`` as a worker's name takes it: lowercase, other characters as hyphens."""
    first = hostname.strip().split(".")[0].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", first).strip("-_") or "node"


async def node_name(conn: AsyncConnection, joining: Joining, hostname: str) -> str:
    """The name of a node of ``joining`` on ``hostname``: <pool>-<host>, or the host's own name when it starts with
    the pool's; then -2, -3 and on while a live worker of the owner has the name already, in any case."""
    host = host_part(hostname)
    base = (
        host
        if host == joining.pool.lower() or host.startswith(f"{joining.pool.lower()}-")
        else f"{joining.pool}-{host}"
    )
    base = base[: MAX_NODE_NAME - 4].rstrip("-._")
    taken = await store.node_names(conn, joining.owner_id, base)
    name, suffix = base, 1
    while name.lower() in taken:
        suffix += 1
        name = f"{base}-{suffix}"
    assert re.match(WORKER_NAME, name), name
    return name

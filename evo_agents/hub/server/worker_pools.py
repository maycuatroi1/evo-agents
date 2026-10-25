"""Worker pools: machines a member adds with one join token instead of a pairing code each.

A member creates a pool (POST /v1/worker-pools, with a web session and its CSRF header or with their machine token)
naming it, unique among their pools in any case, with the projects its nodes serve (the writer role on each, as a
pairing takes them), the slots each node holds, labels and taints, the codes of doctor findings the pool accepts, and
the days a node stays offline before the hub revokes it (1 to 90, 7 by default). GET /v1/worker-pools lists the
caller's pools, GET /v1/worker-pools/{name} shows one with its nodes, and PATCH /v1/worker-pools/{name} changes its
settings: the nodes take the new slots and projects in their rows (``worker_projects.serve`` for each, with its
refusals) and the new labels through ``node_labels``, so each daemon learns them from its next heartbeat answer as it
learns a worker's settings. A pool belongs to the member who made it, as a worker does; only they see or change it, and
another member's pool is 404, as a name the hub never had. Each of its nodes is a worker of that member, shared or not
by the rules of shared workers.

A join token (POST /v1/worker-pools/{name}/tokens) is ``evj_`` and 32 random bytes in base64url, shown once: the hub
keeps its hex SHA-256 alone. It lasts 1 hour to 30 days, takes 1 to 100 machines, and its owner revokes it with POST
.../tokens/{id}/revoke; the nodes that joined with it stay. A machine sends it to POST /v1/worker/join instead of a code
(``worker_join``): ``take_token`` finds it, locks it and its pool, and counts the join; a token that is unknown,
expired, revoked or used up gets the same 403 as a wrong code, counted against the address as wrong codes are. The node
is a new worker of the pool's owner with an ``evw_`` token of its own, named ``node_name`` (the pool's name and the
machine's hostname, with a suffix when a live worker of the owner has that name already), with the pool's slots and
projects; its labels are its own (none at first), the pool's and ``pool=<name>``.

Every pool created or changed and every join token created or revoked adds an audit row naming the pool and the
token's id, never the token, which never reaches a log line either.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

import psycopg
from fastapi import APIRouter, HTTPException, Path, Request, Response
from sqlalchemy import Text, func, insert, literal, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser, Principal, hash_token
from evo_agents.hub.server.worker_models import WORKER_NAME
from evo_agents.hub.server.worker_pool_models import (
    POOL_NAME,
    IssuedJoinToken,
    JoinToken,
    JoinTokenRequest,
    Pool,
    PoolNode,
    PoolRequest,
    PoolUpdate,
)
from evo_agents.hub.server.worker_projects import serve, served, writable

log = logging.getLogger(__name__)

TOKEN_PREFIX = "evj_"
TOKEN_BYTES = 32
POOL_LABEL_KEY = "pool"
MAX_NODE_NAME = 100  # as WORKER_NAME takes it
REFUSED_TOKEN = (
    "the join token is wrong, expired, revoked or used up: create a new one (`evo-agents worker pool token create`)"
)
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}
SETTINGS = ("projects", "slots", "labels", "taints", "accepted_findings", "reap_after_days")

router = APIRouter(prefix="/v1/worker-pools", tags=["worker pools"], responses={401: {"model": ErrorBody}})

PoolName = Path(pattern=POOL_NAME, description="the pool's name")


# The labels of a node


def node_labels(w=None):
    """The labels a worker row of ``w`` (tables.workers by default) has from its pool: the pool's labels and
    pool=<name>; an empty array for a worker of no pool. Add them to w.labels for all its labels."""
    w = tables.workers if w is None else w
    p = tables.worker_pools
    of_pool = (
        select(func.array_append(p.c.labels, literal(f"{POOL_LABEL_KEY}=") + p.c.name))
        .where(p.c.id == w.c.pool_id)
        .scalar_subquery()
    )
    return func.coalesce(of_pool, literal([], ARRAY(Text)))


def all_labels(w=None):
    """All the labels of a worker row: its own, then its pool's (``node_labels``)."""
    w = tables.workers if w is None else w
    return func.array_cat(w.c.labels, node_labels(w))


def pool_name(w=None):
    """The name of the pool of a worker row, NULL for a worker of no pool."""
    w = tables.workers if w is None else w
    p = tables.worker_pools
    return select(p.c.name).where(p.c.id == w.c.pool_id).scalar_subquery()


# Reading pools


def _no_pool(name: str) -> HTTPException:
    return HTTPException(404, f"you have no pool named {name} on this hub: see GET /v1/worker-pools")


async def _pool_row(conn: AsyncConnection, user: Principal, name: str, *, lock: bool = False):
    """The row of the caller's pool ``name``, in any case; 404 for none."""
    p = tables.worker_pools
    statement = select(p).where(p.c.owner_id == user.user_id, func.lower(p.c.name) == name.lower())
    if lock:
        statement = statement.with_for_update()
    row = (await conn.execute(statement)).one_or_none()
    if row is None:
        raise _no_pool(name)
    return row


async def _pool_projects(conn: AsyncConnection, pool_id: int) -> dict[str, int]:
    """The projects of a pool, name -> id, by name."""
    pp, projects = tables.worker_pool_projects, tables.projects
    names = (
        select(projects.c.name, projects.c.id)
        .join_from(pp, projects, projects.c.id == pp.c.project_id)
        .where(pp.c.pool_id == pool_id)
        .order_by(projects.c.name)
    )
    return dict((await conn.execute(names)).all())


async def _nodes(conn: AsyncConnection, pool_id: int) -> list[PoolNode]:
    w = tables.workers
    statement = (
        select(
            w.c.id,
            w.c.name,
            w.c.hostname,
            w.c.agent_version,
            all_labels(w).label("labels"),
            w.c.created_at,
            w.c.last_heartbeat_at,
            w.c.drained_at,
            w.c.revoked_at,
            func.now().label("now"),
        )
        .where(w.c.pool_id == pool_id, w.c.revoked_at.is_(None))
        .order_by(w.c.created_at.desc(), w.c.id.desc())
    )
    nodes = []
    for row in await conn.execute(statement):
        fields = dict(row._mapping)
        status = runs.worker_status(
            last_heartbeat_at=fields["last_heartbeat_at"],
            drained_at=fields.pop("drained_at"),
            revoked_at=fields.pop("revoked_at"),
            now=fields.pop("now"),
        )
        fields["labels"] = list(dict.fromkeys(fields["labels"]))
        nodes.append(PoolNode(**fields, status=status))
    return nodes


async def _shown(conn: AsyncConnection, user: Principal, row) -> Pool:
    fields = {name: row._mapping[name] for name in ("id", "name", "slots", "reap_after_days", "created_at")}
    return Pool(
        **fields,
        updated_at=row.updated_at,
        owner=user.login,
        projects=list(await _pool_projects(conn, row.id)),
        labels=list(row.labels),
        taints=list(row.taints),
        accepted_findings=list(row.accepted_findings),
        nodes=await _nodes(conn, row.id),
    )


@router.get("", response_model=list[Pool])
async def list_pools(request: Request, user: CurrentUser) -> list[Pool]:
    """The caller's pools, by name, each with its nodes that are not revoked."""
    p = tables.worker_pools
    statement = select(p).where(p.c.owner_id == user.user_id).order_by(func.lower(p.c.name))
    async with request.app.state.engine.begin() as conn:
        return [await _shown(conn, user, row) for row in (await conn.execute(statement)).all()]


@router.get("/{name}", response_model=Pool, responses={404: {"model": ErrorBody}})
async def show_pool(request: Request, user: CurrentUser, name: str = PoolName) -> Pool:
    """One of the caller's pools with its nodes that are not revoked."""
    async with request.app.state.engine.begin() as conn:
        return await _shown(conn, user, await _pool_row(conn, user, name))


# Creating and changing pools


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(409, f"you have a pool named {name} already: choose another name")


def _target(pool_id: int, name: str) -> str:
    return f"pool:{pool_id} name={name}"


@router.post("", status_code=201, response_model=Pool, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def create_pool(request: Request, body: PoolRequest, user: CurrentUser) -> Pool:
    """A new pool of the caller's, without nodes until a machine joins with one of its join tokens."""
    p, pp = tables.worker_pools, tables.worker_pool_projects
    values = body.model_dump(include={"name", "slots", "labels", "taints", "accepted_findings", "reap_after_days"})
    async with request.app.state.engine.begin() as conn:
        project_ids = await writable(conn, user, body.projects)
        taken = select(p.c.id).where(p.c.owner_id == user.user_id, func.lower(p.c.name) == body.name.lower())
        if (await conn.execute(taken)).first() is not None:
            raise _name_conflict(body.name)
        try:
            pool_id = (
                await conn.execute(insert(p).values(owner_id=user.user_id, **values).returning(p.c.id))
            ).scalar_one()
        except sa_exc.IntegrityError as exc:  # another request took the name meanwhile
            if not isinstance(exc.orig, psycopg.errors.UniqueViolation):
                raise
            raise _name_conflict(body.name) from None
        await conn.execute(insert(pp), [{"pool_id": pool_id, "project_id": project_id} for project_id in project_ids])
        target, action = _target(pool_id, body.name), audit.WORKER_POOL_CREATE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        log.info("worker pool created", extra={"pool_id": pool_id, "login": user.login})
        return await _shown(conn, user, await _pool_row(conn, user, body.name))


def _shown_value(value) -> str:
    return f"[{','.join(value)}]" if isinstance(value, list) else str(value)


async def _live_nodes(conn: AsyncConnection, pool_id: int) -> list[tuple[int, str]]:
    """(id, name) of the pool's nodes that are not revoked, their rows locked as a worker's PATCH locks it."""
    w = tables.workers
    statement = (
        select(w.c.id, w.c.name).where(w.c.pool_id == pool_id, w.c.revoked_at.is_(None)).order_by(w.c.id)
    ).with_for_update()
    return [tuple(row) for row in await conn.execute(statement)]


async def _serve_projects(conn: AsyncConnection, user: Principal, pool_id: int, was: dict, projects: list[str]) -> None:
    """The pool and each of its live nodes serve exactly ``projects``: the writer role on each one added, and none
    dropped that a node still needs (``worker_projects.serve``, whose 409 changes nothing)."""
    added_ids = await writable(conn, user, [name for name in projects if name not in was])
    for node_id, node in await _live_nodes(conn, pool_id):
        await serve(conn, user, node_id, node, await served(conn, node_id), projects)
    pp = tables.worker_pool_projects
    dropped = [project_id for name, project_id in was.items() if name not in projects]
    if dropped:
        await conn.execute(pp.delete().where(pp.c.pool_id == pool_id, pp.c.project_id.in_(dropped)))
    if added_ids:
        await conn.execute(insert(pp), [{"pool_id": pool_id, "project_id": project_id} for project_id in added_ids])


@router.patch("/{name}", response_model=Pool, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def update_pool(request: Request, body: PoolUpdate, user: CurrentUser, name: str = PoolName) -> Pool:
    """Change the pool's settings; its nodes take them, and their daemons learn them from their next heartbeat."""
    wanted = body.model_dump(exclude_none=True)
    if "projects" in wanted:  # a set: the order says nothing
        wanted["projects"] = sorted(set(wanted["projects"]))
    p, w = tables.worker_pools, tables.workers
    async with request.app.state.engine.begin() as conn:
        row = await _pool_row(conn, user, name, lock=True)
        was_projects = await _pool_projects(conn, row.id)
        now = {field: row._mapping[field] for field in SETTINGS if field != "projects"}
        now = {field: list(value) if isinstance(value, list) else value for field, value in now.items()}
        now["projects"] = sorted(was_projects)
        changed = {field: value for field, value in wanted.items() if value != now[field]}
        if changed:  # the values it has already: nothing changes, nothing is audited
            if "projects" in changed:
                await _serve_projects(conn, user, row.id, was_projects, changed["projects"])
            columns = {field: value for field, value in changed.items() if field != "projects"}
            await conn.execute(update(p).values(**columns, updated_at=func.now()).where(p.c.id == row.id))
            if "slots" in changed:
                live = (w.c.pool_id == row.id) & w.c.revoked_at.is_(None)
                await conn.execute(update(w).values(slots=changed["slots"]).where(live))
            changes = " ".join(
                f"{field}={_shown_value(now[field])}->{_shown_value(value)}" for field, value in changed.items()
            )
            target, action = f"{_target(row.id, row.name)} {changes}", audit.WORKER_POOL_UPDATE
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
            log.info("worker pool changed", extra={"pool_id": row.id, "fields": sorted(changed)})
        return await _shown(conn, user, await _pool_row(conn, user, name))


# Join tokens


def new_join_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def _token_columns():
    t, users = tables.worker_join_tokens, tables.users
    return (
        t.c.id,
        users.c.login.label("created_by"),
        t.c.created_at,
        t.c.expires_at,
        t.c.max_uses,
        t.c.uses,
        t.c.last_used_at,
        t.c.revoked_at,
        (t.c.expires_at <= func.now()).label("expired"),
    )


def _join_token(row, pool: str) -> JoinToken:
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


def _tokens_of(pool_id: int):
    t, users = tables.worker_join_tokens, tables.users
    return select(*_token_columns()).join_from(t, users, users.c.id == t.c.created_by).where(t.c.pool_id == pool_id)


@router.post(
    "/{name}/tokens", status_code=201, response_model=IssuedJoinToken, responses={**REFUSALS, 422: {"model": ErrorBody}}
)
async def create_join_token(
    request: Request, body: JoinTokenRequest, user: CurrentUser, response: Response, name: str = PoolName
) -> IssuedJoinToken:
    """A join token of the pool, which machines join it with (`evo-agents worker join --token`); shown once."""
    token, t = new_join_token(), tables.worker_join_tokens
    async with request.app.state.engine.begin() as conn:
        pool = await _pool_row(conn, user, name)
        statement = insert(t).values(
            pool_id=pool.id,
            token_hash=hash_token(token),
            created_by=user.user_id,
            expires_at=func.now() + timedelta(hours=body.hours),
            max_uses=body.max_uses,
        )
        token_id = (await conn.execute(statement.returning(t.c.id))).scalar_one()
        target = f"{_target(pool.id, pool.name)} join_token:{token_id} hours={body.hours} max_uses={body.max_uses}"
        action = audit.WORKER_POOL_TOKEN
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        row = (await conn.execute(_tokens_of(pool.id).where(t.c.id == token_id))).one()
    log.info("worker pool join token created", extra={"pool_id": pool.id, "join_token_id": token_id})
    response.headers.update(NO_STORE)
    return IssuedJoinToken(**_join_token(row, pool.name).model_dump(), token=token)


@router.get("/{name}/tokens", response_model=list[JoinToken], responses={404: {"model": ErrorBody}})
async def list_join_tokens(request: Request, user: CurrentUser, name: str = PoolName) -> list[JoinToken]:
    """The join tokens of the pool, newest first, without the tokens themselves."""
    t = tables.worker_join_tokens
    async with request.app.state.engine.begin() as conn:
        pool = await _pool_row(conn, user, name)
        rows = await conn.execute(_tokens_of(pool.id).order_by(t.c.created_at.desc(), t.c.id.desc()))
        return [_join_token(row, pool.name) for row in rows]


@router.post("/{name}/tokens/{token_id}/revoke", response_model=JoinToken, responses=REFUSALS)
async def revoke_join_token(
    request: Request, user: CurrentUser, name: str = PoolName, token_id: int = Path(ge=1, le=2**63 - 1)
) -> JoinToken:
    """Revoke a join token of the pool: no machine joins with it any more; the nodes that joined with it stay."""
    t = tables.worker_join_tokens
    async with request.app.state.engine.begin() as conn:
        pool = await _pool_row(conn, user, name)
        locked = select(t.c.revoked_at).where(t.c.id == token_id, t.c.pool_id == pool.id).with_for_update()
        found = (await conn.execute(locked)).one_or_none()
        if found is None:
            raise HTTPException(404, f"pool {pool.name} has no join token {token_id}: see its token list")
        if found.revoked_at is not None:
            raise HTTPException(409, f"join token {token_id} was revoked already, at {found.revoked_at.isoformat()}")
        await conn.execute(update(t).values(revoked_at=func.now()).where(t.c.id == token_id))
        target, action = f"{_target(pool.id, pool.name)} join_token:{token_id}", audit.WORKER_POOL_TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        row = (await conn.execute(_tokens_of(pool.id).where(t.c.id == token_id))).one()
    log.info("worker pool join token revoked", extra={"pool_id": pool.id, "join_token_id": token_id})
    return _join_token(row, pool.name)


# Joining with a token (``worker_join``)


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
    t, p = tables.worker_join_tokens, tables.worker_pools
    statement = (
        select(t.c.id, p.c.id.label("pool_id"), p.c.name, p.c.owner_id, p.c.slots)
        .join_from(t, p, p.c.id == t.c.pool_id)
        .where(
            t.c.token_hash == hash_token(token),
            t.c.revoked_at.is_(None),
            t.c.expires_at > func.now(),
            t.c.uses < t.c.max_uses,
        )
        .with_for_update(of=[t, p])
    )
    row = (await conn.execute(statement)).one_or_none()
    if row is None:
        return None
    await conn.execute(update(t).values(uses=t.c.uses + 1, last_used_at=func.now()).where(t.c.id == row.id))
    projects = await _pool_projects(conn, row.pool_id)
    return Joining(row.pool_id, row.name, row.owner_id, row.slots, tuple(projects.values()), row.id)


def _host_part(hostname: str) -> str:
    """The first label of ``hostname`` as a worker's name takes it: lowercase, other characters as hyphens."""
    first = hostname.strip().split(".")[0].lower()
    return re.sub(r"[^a-z0-9_-]+", "-", first).strip("-_") or "node"


async def node_name(conn: AsyncConnection, joining: Joining, hostname: str) -> str:
    """The name of a node of ``joining`` on ``hostname``: <pool>-<host>, or the host's own name when it starts with
    the pool's; then -2, -3 and on while a live worker of the owner has the name already, in any case."""
    host = _host_part(hostname)
    base = (
        host
        if host == joining.pool.lower() or host.startswith(f"{joining.pool.lower()}-")
        else f"{joining.pool}-{host}"
    )
    base = base[: MAX_NODE_NAME - 4].rstrip("-._")
    w = tables.workers
    like = func.lower(w.c.name).startswith(base.lower(), autoescape=True)
    names = select(func.lower(w.c.name)).where(w.c.owner_id == joining.owner_id, w.c.revoked_at.is_(None), like)
    taken = set((await conn.execute(names)).scalars())
    name, suffix = base, 1
    while name.lower() in taken:
        suffix += 1
        name = f"{base}-{suffix}"
    assert re.match(WORKER_NAME, name), name
    return name

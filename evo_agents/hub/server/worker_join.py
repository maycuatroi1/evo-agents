"""POST /v1/worker/join, the one public worker route: a machine trades a pairing code (``workers``) or a pool's join
token (``worker_pools``) for the ``evw_`` token of a new worker, shown once.

Both refuse alike: an unknown, wrong, expired, used or locked code and an unknown, expired, revoked or used-up token
get 403, whose message names none of these, and every refusal counts against the address in the one ``RefusalLimit``
of the process (``request.app.state.join_refusals``), so an address refused JOIN_REFUSALS times within JOIN_WINDOW
gets 429 with Retry-After, whichever it sent. A code joins the worker its pairing describes; a token joins a new node
of its pool, named by ``worker_pools.node_name``, with the pool's slots and projects. Either way the owner must still
hold the writer role on every project the worker serves (409 otherwise), and the join adds an audit row naming the
worker and the pairing, or the pool and the join token's id, never the code or the token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import BigInteger, any_, func, literal, select, update
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit, worker_pools
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import same
from evo_agents.hub.server.worker_models import JoinRequest, WorkerCredential
from evo_agents.hub.server.workers import (
    CODE_LENGTH,
    MAX_WRONG_TRIES,
    REFUSED_CODE,
    SELECTOR_LENGTH,
    WRITER_ROLES,
    RefusalLimit,
    client_address,
    code_hash,
    create_worker,
    credential_of,
    normal_code,
    pairing_secret,
)

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])


class Refused(Exception):
    """The join is refused with 403 ``message``, counted against the address."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _find_pairing(selector: str):
    """The unused pairing that holds ``selector``, its row locked, and whether it has not expired."""
    wp = tables.worker_pairings
    return (
        select(
            wp.c.id,
            wp.c.code_hash,
            wp.c.owner_id,
            wp.c.name,
            wp.c.projects,
            wp.c.slots,
            wp.c.labels,
            wp.c.allow_web_terminal,
            wp.c.attempts,
            (wp.c.expires_at > func.now()).label("live"),
        )
        .where(wp.c.code_selector == selector, wp.c.used_at.is_(None))
        .with_for_update()
    )


@worker_router.post(
    "/join",
    status_code=201,
    response_model=WorkerCredential,
    responses={
        403: {"model": ErrorBody},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
        429: {"model": ErrorBody, "description": "too many refused codes from this address; see Retry-After"},
        503: {"model": ErrorBody},
    },
)
async def join(request: Request, body: JoinRequest, response: Response) -> WorkerCredential:
    """Trade a pairing code, or a pool's join token, for the token of a new worker; the token is shown once."""
    limit: RefusalLimit = request.app.state.join_refusals
    client = client_address(request)
    wait = limit.retry_after(client)
    if wait is not None:
        raise HTTPException(
            429,
            f"too many refused pairing codes from this address: try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )
    try:
        if body.token is not None:
            credential = await _join_pool(request, body)
        else:
            credential = await _join_pairing(request, body)
    except Refused as refused:
        limit.refuse(client)
        if limit.retry_after(client) is not None:
            log.warning("pairing joins limited", extra={"refusals": limit.limit, "window_s": limit.window})
        raise HTTPException(403, refused.message) from None
    response.headers.update(NO_STORE)
    return credential


async def _join_pairing(request: Request, body: JoinRequest) -> WorkerCredential:
    code = normal_code(body.code)
    if code is None:
        raise HTTPException(422, f"a pairing code is {CODE_LENGTH} characters of Crockford base32, written XXXX-XXXX")
    secret = pairing_secret(request)
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(_find_pairing(code[:SELECTOR_LENGTH]))).one_or_none()
        live = row is not None and row.attempts < MAX_WRONG_TRIES and row.live
        if live and same(code_hash(secret, code), row.code_hash):
            return await _join(conn, row, body)
        if live:  # counted in this transaction, which commits: a refusal must not roll the wrong try back
            wp = tables.worker_pairings
            await conn.execute(update(wp).values(attempts=wp.c.attempts + 1).where(wp.c.id == row.id))
            tries_left = MAX_WRONG_TRIES - row.attempts - 1
            log.warning("wrong pairing code", extra={"pairing_id": row.id, "tries_left": tries_left})
    raise Refused(REFUSED_CODE)  # after the commit: the wrong try counted stays


async def _check_writes(conn: AsyncConnection, owner_id: int, project_ids, whose: str, remedy: str) -> None:
    """409 unless ``owner_id`` holds the writer role on every project of ``project_ids`` still."""
    grants = tables.grants
    owner_writes = (
        select(func.count())
        .select_from(grants)
        .where(
            grants.c.user_id == owner_id,
            grants.c.project_id == any_(literal(list(project_ids), ARRAY(BigInteger))),
            grants.c.role.in_(WRITER_ROLES),
        )
    )
    writes = (await conn.execute(owner_writes)).scalar_one()
    if writes != len(set(project_ids)):
        raise HTTPException(
            409,
            f"the member who made this {whose} no longer holds the writer role on every project it names: "
            f"they need to {remedy}",
        )


async def _join(conn: AsyncConnection, pairing, host: JoinRequest) -> WorkerCredential:
    pairing_id, owner_id, name, project_ids = pairing.id, pairing.owner_id, pairing.name, pairing.projects
    await _check_writes(conn, owner_id, project_ids, "pairing", "create a new pairing")
    issued, worker_id = await create_worker(
        conn,
        owner_id,
        name=name,
        slots=pairing.slots,
        labels=list(pairing.labels),
        allow_web_terminal=pairing.allow_web_terminal,
        project_ids=project_ids,
        host=host,
    )
    wp = tables.worker_pairings
    await conn.execute(update(wp).values(used_at=func.now(), worker_id=worker_id).where(wp.c.id == pairing_id))
    target = f"worker:{worker_id} name={name} pairing:{pairing_id}"
    await audit.record(conn, actor_id=owner_id, token_id=issued.token_id, action=audit.WORKER_JOIN, target=target)
    log.info("worker joined", extra={"worker_id": worker_id, "pairing_id": pairing_id, "token_id": issued.token_id})
    return await credential_of(conn, issued, worker_id)


async def _join_pool(request: Request, body: JoinRequest) -> WorkerCredential:
    async with request.app.state.engine.begin() as conn:
        joining = await worker_pools.take_token(conn, body.token)
        if joining is None:
            raise Refused(worker_pools.REFUSED_TOKEN)
        remedy = "get the writer role back, or drop the project from the pool (`evo-agents worker pool set`)"
        await _check_writes(conn, joining.owner_id, joining.project_ids, "pool", remedy)
        name = await worker_pools.node_name(conn, joining, body.hostname)
        issued, worker_id = await create_worker(
            conn,
            joining.owner_id,
            name=name,
            slots=joining.slots,
            labels=[],
            allow_web_terminal=False,
            project_ids=joining.project_ids,
            host=body,
            pool_id=joining.pool_id,
        )
        target = f"worker:{worker_id} name={name} pool:{joining.pool_id} join_token:{joining.token_id}"
        await audit.record(
            conn, actor_id=joining.owner_id, token_id=issued.token_id, action=audit.WORKER_JOIN, target=target
        )
        log.info(
            "worker joined a pool",
            extra={"worker_id": worker_id, "pool_id": joining.pool_id, "join_token_id": joining.token_id},
        )
        return await credential_of(conn, issued, worker_id)

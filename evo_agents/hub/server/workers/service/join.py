"""POST /v1/worker/join, the one public worker route: a machine trades a pairing code (``pairings``) or a pool's join
token (``pool_tokens``) for the ``evw_`` token of a new worker, shown once.

Both refuse alike: an unknown, wrong, expired, used or locked code and an unknown, expired, revoked or used-up token
get 403, whose message names none of these, and every refusal counts against the address in the app's sliding window
(``request.app.state.join_limit``: on Redis when the cache is shared, else the process's ``RefusalLimit``), so an
address refused JOIN_REFUSALS times within JOIN_WINDOW
gets 429 with Retry-After, whichever it sent. A code joins the worker its pairing describes; a token joins a new node
of its pool, named by ``pool_tokens.node_name``, with the pool's slots and projects. Either way the owner must still
hold the writer role on every project the worker serves (409 otherwise), and the join adds an audit row naming the
worker and the pairing, or the pool and the join token's id, never the code or the token.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.security import same
from evo_agents.hub.server.workers.models.codes import (
    CODE_LENGTH,
    MAX_WRONG_TRIES,
    REFUSED_CODE,
    SELECTOR_LENGTH,
    normal_code,
)
from evo_agents.hub.server.workers.models.pools import REFUSED_TOKEN
from evo_agents.hub.server.workers.models.workers import JoinRequest, WorkerCredential
from evo_agents.hub.server.workers.repository import pairings
from evo_agents.hub.server.workers.repository import projects as project_store
from evo_agents.hub.server.workers.service import pool_tokens
from evo_agents.hub.server.workers.service.codes import code_hash
from evo_agents.hub.server.workers.service.views import credential_of
from evo_agents.hub.server.workers.service.workers import create_worker

log = logging.getLogger(__name__)


class Refused(Exception):
    """The join is refused with 403 ``message``, counted against the address."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def pairing_code(text: str) -> str:
    """The code ``text`` names (``normal_code``); 422 when it is none."""
    code = normal_code(text)
    if code is None:
        raise Unprocessable(f"a pairing code is {CODE_LENGTH} characters of Crockford base32, written XXXX-XXXX")
    return code


async def join_pairing(engine: AsyncEngine, secret: str, code: str, body: JoinRequest) -> WorkerCredential:
    """The worker the pairing of ``code`` describes, made in a transaction of its own; Refused for a code that is
    unknown, wrong, expired, used or locked, after a wrong try against a live pairing is counted and committed."""
    async with engine.begin() as conn:
        row = await pairings.find(conn, code[:SELECTOR_LENGTH])
        live = row is not None and row.attempts < MAX_WRONG_TRIES and row.live
        if row is not None and live and same(code_hash(secret, code), row.code_hash):
            return await join(conn, row, body)
        if (
            live and row is not None
        ):  # counted in this transaction, which commits: a refusal must not roll the wrong try back
            await pairings.count_wrong_try(conn, row.id)
            tries_left = MAX_WRONG_TRIES - row.attempts - 1
            log.warning("wrong pairing code", extra={"pairing_id": row.id, "tries_left": tries_left})
    raise Refused(REFUSED_CODE)  # after the commit: the wrong try counted stays


async def check_writes(conn: AsyncConnection, owner_id: int, project_ids, whose: str, remedy: str) -> None:
    """409 unless ``owner_id`` holds the writer role on every project of ``project_ids`` still."""
    if await project_store.writer_count(conn, owner_id, project_ids) != len(set(project_ids)):
        raise Conflict(
            f"the member who made this {whose} no longer holds the writer role on every project it names: "
            f"they need to {remedy}"
        )


async def join(conn: AsyncConnection, pairing, host: JoinRequest) -> WorkerCredential:
    pairing_id, owner_id, name, project_ids = pairing.id, pairing.owner_id, pairing.name, pairing.projects
    await check_writes(conn, owner_id, project_ids, "pairing", "create a new pairing")
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
    await pairings.mark_used(conn, pairing_id, worker_id)
    target = f"worker:{worker_id} name={name} pairing:{pairing_id}"
    # the actor is the pairing's owner, the token the one the join issued: no Principal made the request
    await audit.record(conn, actor_id=owner_id, token_id=issued.token_id, action=audit.WORKER_JOIN, target=target)
    log.info("worker joined", extra={"worker_id": worker_id, "pairing_id": pairing_id, "token_id": issued.token_id})
    return await credential_of(conn, issued, worker_id)


async def join_pool(engine: AsyncEngine, token: str, body: JoinRequest) -> WorkerCredential:
    """A new node of the pool whose join token is ``token``; Refused for a token that is unknown, expired, revoked or
    used up."""
    async with engine.begin() as conn:
        joining = await pool_tokens.take_token(conn, token)
        if joining is None:
            raise Refused(REFUSED_TOKEN)
        remedy = "get the writer role back, or drop the project from the pool (`evo-agents worker pool set`)"
        await check_writes(conn, joining.owner_id, joining.project_ids, "pool", remedy)
        name = await pool_tokens.node_name(conn, joining, body.hostname)
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

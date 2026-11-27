"""POST /v1/worker/join, the one public worker route: a machine trades a pairing code or a pool's join token for the
token of a new worker (``service.join``); an address refused JOIN_REFUSALS times within JOIN_WINDOW gets 429 with
Retry-After."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response

from evo_agents.hub import cache
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.workers.models.workers import JoinRequest, WorkerCredential
from evo_agents.hub.server.workers.service import join as joins
from evo_agents.hub.server.workers.service.codes import client_address, pairing_secret

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])


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
    limit: cache.Window = request.app.state.join_limit
    client = client_address(request)
    wait = await limit.retry_after(client)
    if wait is not None:
        raise HTTPException(
            429,
            f"too many refused pairing codes from this address: try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )
    engine = request.app.state.engine
    try:
        if body.token is not None:
            credential = await joins.join_pool(engine, body.token, body)
        else:
            code = joins.pairing_code(body.code or "")
            credential = await joins.join_pairing(engine, pairing_secret(request), code, body)
    except joins.Refused as refused:
        await limit.hit(client)
        if await limit.retry_after(client) is not None:
            log.warning("pairing joins limited", extra={"refusals": limit.limit, "window_s": limit.window})
        raise HTTPException(403, refused.message) from None
    response.headers.update(NO_STORE)
    return credential

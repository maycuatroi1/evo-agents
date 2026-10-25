"""The hub's gateway route: the model calls of a run on a provider account's model, relayed to the model gateway
(``evo_agents.hub.gateway_runs``, ``docs/credentials.md``, section "Provider accounts").

POST /v1/messages (Anthropic's format), /v1/chat/completions (OpenAI's), /v1/responses (OpenAI's Responses) and GET
/v1/models. Each takes the run's token of its gateway lease (``gateway_leases``) as ``Authorization: Bearer`` or
``x-api-key``, in place of a credential of the hub (``security.PUBLIC_PATHS``): 401 unless the token is that of a key
row not revoked that still holds its key, of a run in a held state on the worker the lease was made for, whose
accounts' leases are none of them revoked. A call names a model of the run's prefix, ``<prefix>/<model>``, or gets
403; the list of models is that of the prefix alone. The request goes on with the run's key of the gateway in place
of the token and no other credential, and the answer comes back as the gateway streams it, chunk by chunk, server-sent
events unbuffered, with a long read timeout (``gateway_client.RELAY_TIMEOUT``). The routes are not in the hub's
OpenAPI document: they are the gateway's, which the runtimes speak. No log line holds a token, a key or a body.
"""

from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import exists, select
from starlette.background import BackgroundTask

from evo_agents.hub import runs, tables
from evo_agents.hub.gateway_runs import GATEWAY
from evo_agents.hub.server import gateway_sync
from evo_agents.hub.server.gateway_client import GatewayError, GatewayOff
from evo_agents.hub.server.gateway_leases import run_token, token_hash
from evo_agents.hub.server.sealing import Sealed, Unsealable, lease_aad

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", include_in_schema=False)

PASSED = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta", "user-agent")
RETURNED = ("content-type", "cache-control", "x-request-id", "retry-after")
REFUSED = "the run's token to the gateway route is missing, revoked, or of a run that is no longer held"


@dataclass(frozen=True)
class Holder:
    run_id: int
    prefix: str
    key: str


def _token(request: Request) -> str | None:
    scheme, _, value = (request.headers.get("authorization") or "").strip().partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.headers.get("x-api-key") or "").strip() or None


def _holder_query(digest: bytes):
    cl, r = tables.credential_leases, tables.runs
    reached = tables.credential_leases.alias("reached")
    lost = exists().where(
        reached.c.run_id == cl.c.run_id,
        reached.c.external_id == cl.c.external_id,
        reached.c.provider == GATEWAY,
        reached.c.secret_id.is_not(None),
        reached.c.revoked_at.is_not(None),
    )
    return (
        select(cl.c.id, cl.c.run_id, cl.c.sealed_value, cl.c.nonce, cl.c.key_id, r.c.gateway_provider)
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(
            cl.c.token_hash == digest,
            cl.c.provider == GATEWAY,
            cl.c.secret_id.is_(None),
            cl.c.revoked_at.is_(None),
            cl.c.sealed_value.is_not(None),
            r.c.state.in_(runs.HELD_STATES),
            r.c.worker_id == cl.c.worker_id,
            ~lost,
        )
    )


async def _holder(request: Request) -> Holder:
    """The run whose token the request carries, with its key of the gateway; 401 as the module says."""
    token = _token(request)
    state = request.app.state
    if token is None or state.sealer is None:
        raise HTTPException(401, REFUSED)
    async with state.engine.connect() as conn:
        row = (await conn.execute(_holder_query(token_hash(token)))).first()
    if row is None or row.gateway_provider is None:
        raise HTTPException(401, REFUSED)
    try:
        key = state.sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(row.id))
    except Unsealable:
        raise HTTPException(401, REFUSED) from None
    if not hmac.compare_digest(run_token(key, row.id), token):
        raise HTTPException(401, REFUSED)
    return Holder(row.run_id, row.gateway_provider, key)


def _headers(request: Request) -> dict[str, str]:
    sent = {name: request.headers[name] for name in PASSED if name in request.headers}
    return {**sent, "accept-encoding": "identity"}


async def _relay(request: Request, holder: Holder, path: str, content: bytes) -> Response:
    try:
        client = gateway_sync.client_of(request.app)
        answer = await client.relay(request.method, path, holder.key, _headers(request), content)
    except GatewayOff as exc:
        raise HTTPException(503, str(exc)) from None
    except GatewayError as exc:
        log.warning("gateway route: the gateway did not answer", extra={"run_id": holder.run_id, "why": str(exc)})
        raise HTTPException(502, "the model gateway did not answer; try again") from None
    headers = {name: answer.headers[name] for name in RETURNED if name in answer.headers}
    return StreamingResponse(
        answer.aiter_bytes(), status_code=answer.status_code, headers=headers, background=BackgroundTask(answer.aclose)
    )


async def _call(request: Request, path: str) -> Response:
    holder = await _holder(request)
    content = await request.body()
    try:
        model = json.loads(content or b"{}").get("model")
    except (ValueError, AttributeError):
        raise HTTPException(400, "the body must be a JSON object that names a model") from None
    if not isinstance(model, str) or not model.startswith(f"{holder.prefix}/"):
        raise HTTPException(403, f"this run may call the models of {holder.prefix} alone: <{holder.prefix}>/<model>")
    return await _relay(request, holder, path, content)


@router.post("/messages")
async def messages(request: Request) -> Response:
    return await _call(request, "/v1/messages")


@router.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _call(request, "/v1/chat/completions")


@router.post("/responses")
async def responses(request: Request) -> Response:
    return await _call(request, "/v1/responses")


@router.get("/models")
async def models(request: Request) -> Response:
    """The gateway's models of the run's prefix."""
    holder = await _holder(request)
    answer = await _relay(request, holder, "/v1/models", b"")
    body = b"".join([chunk async for chunk in answer.body_iterator])
    if answer.background is not None:
        await answer.background()
    try:
        found = json.loads(body)
    except ValueError:
        raise HTTPException(502, "the model gateway answered no list of models") from None
    if answer.status_code != 200 or not isinstance(found, dict):
        return Response(body, status_code=answer.status_code, media_type="application/json")
    listed = [item for item in found.get("data") or [] if str(item.get("id", "")).startswith(f"{holder.prefix}/")]
    return JSONResponse({**found, "data": listed})

"""The hub's gateway route: the model calls of a run on a provider account's model, relayed to the model gateway
(``evo_agents.hub.gateway_runs``, ``docs/credentials.md``, section "Provider accounts").

POST /v1/messages (Anthropic's format), /v1/chat/completions (OpenAI's), /v1/responses (OpenAI's Responses) and GET
/v1/models. Each takes the run's token of its gateway lease (``service.leases``) as ``Authorization: Bearer`` or
``x-api-key``, in place of a credential of the hub (``security.PUBLIC_PATHS``): 401 unless the token is that of a key
row not revoked that still holds its key, of a run in a held state on the worker the lease was made for, whose
accounts' leases are none of them revoked. A call names a model of the run's prefix, ``<prefix>/<model>``, or gets
403; the list of models is that of the prefix alone. The request goes on with the run's key of the gateway in place
of the token and no other credential, and the answer comes back as the gateway streams it, chunk by chunk, server-sent
events unbuffered, with a long read timeout (``client.omniroute.RELAY_TIMEOUT``). Before it goes, the data loss
prevention policy of the run's project reads its body (``dlp_policy.enforce``): it goes redacted, or a call the policy
blocks gets 422 and never leaves. The routes are not in the hub's OpenAPI document: they are the gateway's, which the
runtimes speak. No log line holds a token, a key or a body.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from evo_agents.hub.server.gateway.models.proxy import PASSED, RETURNED, Holder
from evo_agents.hub.server.gateway.service import proxy

router = APIRouter(prefix="/v1", include_in_schema=False)


def _token(request: Request) -> str | None:
    scheme, _, value = (request.headers.get("authorization") or "").strip().partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return (request.headers.get("x-api-key") or "").strip() or None


async def _holder(request: Request) -> Holder:
    state = request.app.state
    return await proxy.holder(state.engine, state.sealer, _token(request))


def _headers(request: Request) -> dict[str, str]:
    sent = {name: request.headers[name] for name in PASSED if name in request.headers}
    return {**sent, "accept-encoding": "identity"}


async def _relay(request: Request, holder: Holder, path: str, content: bytes) -> StreamingResponse:
    answer = await proxy.relay(request.app, holder, request.method, path, _headers(request), content)
    headers = {name: answer.headers[name] for name in RETURNED if name in answer.headers}
    return StreamingResponse(
        answer.aiter_bytes(), status_code=answer.status_code, headers=headers, background=BackgroundTask(answer.aclose)
    )


async def _call(request: Request, path: str) -> Response:
    holder = await _holder(request)
    content = await proxy.checked_body(request.app.state.engine, holder, await request.body())
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
    body = b"".join([chunk async for chunk in answer.body_iterator])  # pyright: ignore[reportArgumentType]
    if answer.background is not None:
        await answer.background()
    listed = proxy.listed_models(holder, answer.status_code, body)
    if listed is None:
        return Response(body, status_code=answer.status_code, media_type="application/json")
    return JSONResponse(listed)

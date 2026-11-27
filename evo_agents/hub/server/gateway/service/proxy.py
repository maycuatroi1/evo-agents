"""What the hub's gateway route (``routes.proxy``) decides of a run's model call: whose token it carries, whether its
body names a model of the run's prefix and passes the data loss prevention policy of its project, and the relay of it
to the model gateway with the run's key (``docs/credentials.md``, section "Provider accounts")."""

from __future__ import annotations

import hmac
import json
import logging

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server import dlp_policy
from evo_agents.hub.server.base.errors import Forbidden, Unprocessable
from evo_agents.hub.server.credentials.models.sealing import Sealed, Sealer, Unsealable, lease_aad
from evo_agents.hub.server.gateway.client.omniroute import GatewayError, GatewayOff
from evo_agents.hub.server.gateway.models.proxy import REFUSED, Holder
from evo_agents.hub.server.gateway.repository.proxy import holder_row
from evo_agents.hub.server.gateway.service.sync import client_of
from evo_agents.hub.server.gateway.service.tokens import run_token, token_hash

log = logging.getLogger(__name__)


async def holder(engine: AsyncEngine, sealer: Sealer | None, token: str | None) -> Holder:
    """The run whose token the request carries, with its key of the gateway; 401 unless the token is that of a key row
    not revoked that still holds its key, of a run held on the worker the lease was made for."""
    if token is None or sealer is None:
        raise HTTPException(401, REFUSED)
    row = await holder_row(engine, token_hash(token))
    if row is None or row.gateway_provider is None:
        raise HTTPException(401, REFUSED)
    try:
        key = sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(row.id))
    except Unsealable:
        raise HTTPException(401, REFUSED) from None
    if not hmac.compare_digest(run_token(key, row.id), token):
        raise HTTPException(401, REFUSED)
    return Holder(row.run_id, row.gateway_provider, key, row.project_id)


async def checked_body(engine: AsyncEngine, holder: Holder, content: bytes) -> bytes:
    """The body of a model call of ``holder``'s run as it may go: 400 for one that names no model, 403 for a model of
    another prefix, 422 for one the project's policy blocks; redacted as the policy says."""
    try:
        model = json.loads(content or b"{}").get("model")
    except (ValueError, AttributeError):
        raise HTTPException(400, "the body must be a JSON object that names a model") from None
    if not isinstance(model, str) or not model.startswith(f"{holder.prefix}/"):
        raise Forbidden(f"this run may call the models of {holder.prefix} alone: <{holder.prefix}>/<model>")
    try:
        return await dlp_policy.enforce(engine, holder.project_id, "gateway_proxy", content, run_id=holder.run_id)
    except dlp_policy.Blocked as exc:
        raise Unprocessable(str(exc)) from None


async def relay(
    app: FastAPI, holder: Holder, method: str, path: str, headers: dict[str, str], content: bytes
) -> httpx.Response:
    """The gateway's answer to the call, unread, to stream; 503 while the hub runs without a gateway, 502 when it does
    not answer."""
    try:
        return await client_of(app).relay(method, path, holder.key, headers, content)
    except GatewayOff as exc:
        raise HTTPException(503, str(exc)) from None
    except GatewayError as exc:
        log.warning("gateway route: the gateway did not answer", extra={"run_id": holder.run_id, "why": str(exc)})
        raise HTTPException(502, "the model gateway did not answer; try again") from None


def listed_models(holder: Holder, status: int, body: bytes) -> dict | None:
    """The gateway's list of models in ``body`` narrowed to the run's prefix; None to pass an answer other than a
    list as it came; 502 for one that is not JSON."""
    try:
        found = json.loads(body)
    except ValueError:
        raise HTTPException(502, "the model gateway answered no list of models") from None
    if status != 200 or not isinstance(found, dict):
        return None
    listed = [item for item in found.get("data") or [] if str(item.get("id", "")).startswith(f"{holder.prefix}/")]
    return {**found, "data": listed}

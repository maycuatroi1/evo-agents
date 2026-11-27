"""The delivery of a project's outbound webhooks, the job hub.deliver_webhooks (``evo_agents.hub.webhook_events``,
``docs/hub.md``, section "Outbound webhooks").

``deliver`` is a pass. It claims one due delivery with ``FOR UPDATE SKIP LOCKED`` and moves it CLAIM_SECONDS ahead in
a short transaction (``repository.claim``), tries it with no transaction open, writes the try, and claims the next, up
to BATCH a pass; it starts no try after PASS_SECONDS. A try takes TIMEOUT at most, so a delivery claimed is not due
again before its try is written, and two passes at once never send it twice.

A try checks the webhook's URL again (``public_endpoint.host_refusal``), resolves its host and refuses it unless every
address it resolves to is public (``public_endpoint.public_addresses``), as DNS may change after the write. The POST
goes to the first address checked, with the host name in the Host header and in TLS (``accounts.service.probe.pinned_url``),
so a second answer of the resolver cannot send it elsewhere. No redirect is followed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server.accounts.service.probe import pinned_url
from evo_agents.hub.server.public_endpoint import EndpointRefused, Resolver, host_refusal, public_addresses
from evo_agents.hub.server.webhooks import repository
from evo_agents.hub.server.webhooks.models import BATCH, PASS_SECONDS, TIMEOUT
from evo_agents.hub.webhook_events import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    KEEP_DAYS,
    MAX_ATTEMPTS,
    MAX_ERROR_CHARS,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    USER_AGENT,
    backoff,
    signature,
    webhook_aad,
)

log = logging.getLogger(__name__)


async def _claim(engine: AsyncEngine):
    async with engine.begin() as conn:
        return await repository.claim(conn)


def _body(row) -> bytes:
    sent = {
        "id": row.id,
        "event": row.event,
        "project": row.project,
        "created_at": row.created_at.isoformat(),
        "data": row.payload,
    }
    return json.dumps(sent, separators=(",", ":")).encode()


async def _post(client: httpx.AsyncClient, url: str, body: bytes, headers: dict, resolver: Resolver | None) -> int:
    """The HTTP status of a POST of ``body`` to ``url``, at the address checked; EndpointRefused when the host is not
    public."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    refusal = host_refusal(host)
    if refusal is not None:
        raise EndpointRefused(refusal)
    address = (await public_addresses(url, resolver))[0]
    sent = {**headers, "Host": parts.netloc.rsplit("@", 1)[-1]}
    request = client.build_request(
        "POST", pinned_url(url, address), content=body, headers=sent, extensions={"sni_hostname": host}
    )
    answer = await client.send(request)
    await answer.aclose()
    return answer.status_code


async def _try(
    client: httpx.AsyncClient, sealer, row, resolver: Resolver | None
) -> tuple[int | None, str | None, bool]:
    """(the HTTP status of one try of ``row``, the kind of error when it was not delivered, whether no later try can
    do better)."""
    if not row.active:
        return None, "the webhook is turned off", True
    if sealer is None:
        return None, "the hub keeps no secrets key (EVO_HUB_SECRETS_KEY): it cannot sign", False
    try:
        secret = sealer.open_stored(row.sealed_secret, webhook_aad(row.webhook_id))
    except Exception:  # Unsealable, under a key the hub no longer has
        return None, "the webhook's secret cannot be opened", True
    body = _body(row)
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        EVENT_HEADER: row.event,
        DELIVERY_HEADER: str(row.id),
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: signature(secret, timestamp, body),
    }
    try:
        async with asyncio.timeout(TIMEOUT):
            status = await _post(client, row.url, body, headers, resolver)
    except EndpointRefused as exc:
        return None, f"refused: {exc}", False
    except TimeoutError:
        return None, f"no answer within {TIMEOUT:g} seconds", False
    except httpx.HTTPError as exc:
        return None, f"no answer: {type(exc).__name__}", False
    if 200 <= status < 300:
        return status, None, False
    return status, f"answered HTTP {status}", False


async def _settle(engine: AsyncEngine, row, status: int | None, error: str | None, final: bool) -> str:
    attempts = row.attempts + 1
    if error is None:
        values = {"state": "delivered", "delivered_at": func.now(), "last_error": None}
    elif final or attempts >= MAX_ATTEMPTS:
        values = {"state": "failed", "last_error": error[:MAX_ERROR_CHARS]}
    else:
        values = {"next_at": func.now() + backoff(attempts), "last_error": error[:MAX_ERROR_CHARS]}
    async with engine.begin() as conn:
        await repository.settle(conn, row.id, {"attempts": attempts, "last_status": status, **values})
    return values.get("state", "retry")


async def deliver(
    engine: AsyncEngine,
    sealer,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    resolver: Resolver | None = None,
) -> dict:
    """One pass of the due deliveries of every webhook (see the module); how many came to what."""
    done = {"delivered": 0, "retry": 0, "failed": 0}
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, follow_redirects=False) as client:
        for _ in range(BATCH):
            if time.monotonic() - started >= PASS_SECONDS:
                break
            row = await _claim(engine)
            if row is None:
                break
            status, error, final = await _try(client, sealer, row, resolver)
            done[await _settle(engine, row, status, error, final)] += 1
            if error is not None:
                log.info(
                    "webhook not delivered",
                    extra={"delivery_id": row.id, "webhook_id": row.webhook_id, "status": status, "why": error},
                )
    async with engine.begin() as conn:
        await repository.purge(conn, KEEP_DAYS)
    return done

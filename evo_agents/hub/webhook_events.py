"""The events a project's outbound webhooks take, their outbox and their delivery (``docs/hub.md``, section
"Outbound webhooks").

A project's admin sets webhooks (project_webhooks, 0263; ``evo_agents.hub.server.project_webhooks``), each an https URL,
the EVENTS it takes and a signing secret the hub seals. ``enqueue`` stores, in the transaction that made an event, one
row of webhook_deliveries for each active webhook of the event's project that takes it, and queues the job
hub.deliver_webhooks for once that commits (``jobs.soon``): nothing goes out while the transaction is open, and
nothing is lost when the hub stops after the commit. ``plugin_events.emit`` calls it for ``run.state``,
``chat.message`` and ``task.result``; a task run with no agent type stores its ``task.result`` here alone
(``task_dispatch.task_run_ended``), and a proposal of the Curator stores ``curator.proposal``
(``evo_agents.hub.server.proposals``). A payload carries ids and states, as a plugin's does, never a text: a receiver
reads the rest from the hub, under the grant of its own credential.

``deliver`` is the job, every minute and on each commit that stored a delivery. It claims the due deliveries with
``FOR UPDATE SKIP LOCKED`` and moves them CLAIM_SECONDS ahead in a short transaction, then POSTs each with no
transaction open, a JSON body ``{id, event, project, created_at, data}`` and the headers:

- ``X-Evo-Event``: the event; ``X-Evo-Delivery``: the delivery's id, the same on every try;
- ``X-Evo-Timestamp``: the Unix time of the try;
- ``X-Evo-Signature``: ``sha256=`` and the hex HMAC-SHA256, under the webhook's secret, of the timestamp, a dot and the
  body (``signature``). A receiver checks it, and refuses a timestamp far from its clock.

A 2xx answer delivers it. Any other answer, or none within TIMEOUT, waits BACKOFF_SECONDS, doubling up to
BACKOFF_MAX_SECONDS, and the MAX_ATTEMPTS-th fails it; a webhook turned off or deleted fails what it left pending. The
row keeps the last HTTP status and the error's kind, never an answer's body. Deliveries that ended are kept KEEP_DAYS
days. No redirect is followed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import timedelta

import httpx
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import jobs, tables

log = logging.getLogger(__name__)

EVENTS = ("run.state", "task.result", "chat.message", "curator.proposal")  # project_webhooks.events (0263)
STATES = ("pending", "delivered", "failed")  # webhook_deliveries.state (0263)
MAX_URL_CHARS = 2048
MAX_ERROR_CHARS = 2000
MAX_ATTEMPTS = 8
BACKOFF_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
CLAIM_SECONDS = 120  # a claimed delivery is not due again for this long, unless its try is written first
TIMEOUT = 10.0
BATCH = 200
KEEP_DAYS = 14
EVENT_HEADER = "X-Evo-Event"
DELIVERY_HEADER = "X-Evo-Delivery"
TIMESTAMP_HEADER = "X-Evo-Timestamp"
SIGNATURE_HEADER = "X-Evo-Signature"
USER_AGENT = "evo-agents-hub-webhooks"


def webhook_aad(webhook_id: int) -> bytes:
    """The associated data of the secret of webhook ``webhook_id``: it opens only as that webhook's."""
    return f"project-webhook:{webhook_id}".encode()


def signature(secret: str, timestamp: str, body: bytes) -> str:
    """``sha256=`` and the hex HMAC-SHA256 under ``secret`` of ``timestamp``, a dot and ``body``."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def enqueue(conn: AsyncConnection, event: str, project_id: int | None, payload: dict) -> int:
    """Store a delivery of ``event`` of project ``project_id`` for each active webhook there that takes it, in the
    caller's transaction; how many."""
    if project_id is None or event not in EVENTS:
        return 0
    h = tables.project_webhooks
    query = select(h.c.id).where(h.c.project_id == project_id, h.c.active, h.c.events.contains([event]))
    ids = (await conn.execute(query.order_by(h.c.id))).scalars().all()
    if not ids:
        return 0
    rows = [{"webhook_id": webhook_id, "event": event, "payload": payload} for webhook_id in ids]
    await conn.execute(insert(tables.webhook_deliveries), rows)
    await jobs.soon(jobs.DELIVER_WEBHOOKS, conn)
    return len(ids)


def backoff(attempts: int) -> timedelta:
    """How long a delivery waits after its ``attempts``-th failed try."""
    return timedelta(seconds=min(BACKOFF_SECONDS * 2 ** max(0, attempts - 1), BACKOFF_MAX_SECONDS))


async def _claim(engine: AsyncEngine, batch: int) -> list:
    d, h, p = tables.webhook_deliveries, tables.project_webhooks, tables.projects
    due = (
        select(d.c.id)
        .where(d.c.state == "pending", d.c.next_at <= func.now())
        .order_by(d.c.next_at, d.c.id)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )
    async with engine.begin() as conn:
        ids = (await conn.execute(due)).scalars().all()
        if not ids:
            return []
        later = func.now() + timedelta(seconds=CLAIM_SECONDS)
        await conn.execute(update(d).values(next_at=later).where(d.c.id.in_(ids)))
        query = (
            select(
                d.c.id,
                d.c.event,
                d.c.payload,
                d.c.attempts,
                d.c.created_at,
                h.c.id.label("webhook_id"),
                h.c.url,
                h.c.sealed_secret,
                h.c.active,
                p.c.name.label("project"),
            )
            .join_from(d, h, h.c.id == d.c.webhook_id)
            .join(p, p.c.id == h.c.project_id)
            .where(d.c.id.in_(ids))
            .order_by(d.c.next_at, d.c.id)
        )
        return list((await conn.execute(query)).all())


def _body(row) -> bytes:
    sent = {
        "id": row.id,
        "event": row.event,
        "project": row.project,
        "created_at": row.created_at.isoformat(),
        "data": row.payload,
    }
    return json.dumps(sent, separators=(",", ":")).encode()


async def _try(client: httpx.AsyncClient, sealer, row) -> tuple[int | None, str | None, bool]:
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
        answer = await client.post(row.url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        return None, f"no answer: {type(exc).__name__}", False
    if 200 <= answer.status_code < 300:
        return answer.status_code, None, False
    return answer.status_code, f"answered HTTP {answer.status_code}", False


async def _settle(engine: AsyncEngine, row, status: int | None, error: str | None, final: bool) -> str:
    d = tables.webhook_deliveries
    attempts = row.attempts + 1
    if error is None:
        values = {"state": "delivered", "delivered_at": func.now(), "last_error": None}
    elif final or attempts >= MAX_ATTEMPTS:
        values = {"state": "failed", "last_error": error[:MAX_ERROR_CHARS]}
    else:
        values = {"next_at": func.now() + backoff(attempts), "last_error": error[:MAX_ERROR_CHARS]}
    async with engine.begin() as conn:
        await conn.execute(
            update(d)
            .values(attempts=attempts, last_status=status, **values)
            .where(d.c.id == row.id, d.c.state == "pending")
        )
    return values.get("state", "retry")


async def deliver(engine: AsyncEngine, sealer, *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """One pass of the due deliveries of every webhook (see the module); how many came to what."""
    done = {"delivered": 0, "retry": 0, "failed": 0}
    rows = await _claim(engine, BATCH)
    if rows:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport, follow_redirects=False) as client:
            for row in rows:
                status, error, final = await _try(client, sealer, row)
                outcome = await _settle(engine, row, status, error, final)
                done[outcome] += 1
                if error is not None:
                    log.info(
                        "webhook not delivered",
                        extra={"delivery_id": row.id, "webhook_id": row.webhook_id, "status": status, "why": error},
                    )
    d = tables.webhook_deliveries
    async with engine.begin() as conn:
        old = func.now() - timedelta(days=KEEP_DAYS)
        await conn.execute(delete(d).where(d.c.state != "pending", d.c.created_at < old))
    return done

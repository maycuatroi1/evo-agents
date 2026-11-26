"""The events a project's outbound webhooks take, their outbox and their delivery (``docs/hub.md``, section
"Outbound webhooks").

A project's admin sets webhooks (project_webhooks, 0263; ``evo_agents.hub.server.webhooks``), each an https URL,
the EVENTS it takes and a signing secret the hub seals. ``enqueue`` stores, in the transaction that made an event, one
row of webhook_deliveries for each active webhook of the event's project that takes it, and queues the job
hub.deliver_webhooks for once that commits (``jobs.soon``): nothing goes out while the transaction is open, and
nothing is lost when the hub stops after the commit. ``plugin_events.emit`` calls it for ``run.state``,
``chat.message`` and ``task.result``; a task run with no agent type stores its ``task.result`` here alone
(``task_dispatch.task_run_ended``), and a proposal of the Curator stores ``curator.proposal``
(``evo_agents.hub.server.proposals``). A payload carries ids and states, as a plugin's does, never a text: a receiver
reads the rest from the hub, under the grant of its own credential.

``evo_agents.hub.server.webhooks.service.delivery.deliver`` is the job, every minute and on each commit that stored a
delivery. It claims one due delivery at a time and POSTs it with no transaction open, to an address of the URL's host it
checked is public, with a JSON body ``{id, event, project, created_at, data}`` and the headers:

- ``X-Evo-Event``: the event; ``X-Evo-Delivery``: the delivery's id, the same on every try;
- ``X-Evo-Timestamp``: the Unix time of the try;
- ``X-Evo-Signature``: ``sha256=`` and the hex HMAC-SHA256, under the webhook's secret, of the timestamp, a dot and the
  body (``signature``). A receiver checks it, and refuses a timestamp far from its clock.

A 2xx answer delivers it. Any other answer, none within its TIMEOUT, or a host that is not public, waits BACKOFF_SECONDS, doubling up to
BACKOFF_MAX_SECONDS, and the MAX_ATTEMPTS-th fails it; a webhook turned off or deleted fails what it left pending. The
row keeps the last HTTP status and the error's kind, never an answer's body. Deliveries that ended are kept KEEP_DAYS
days. No redirect is followed.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import jobs, tables

EVENTS = ("run.state", "task.result", "chat.message", "curator.proposal")  # project_webhooks.events (0263)
STATES = ("pending", "delivered", "failed")  # webhook_deliveries.state (0263)
MAX_URL_CHARS = 2048
MAX_ERROR_CHARS = 2000
MAX_ATTEMPTS = 8
BACKOFF_SECONDS = 60
BACKOFF_MAX_SECONDS = 3600
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

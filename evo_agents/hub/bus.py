"""The hub's optional event bus on RabbitMQ: what a commit stores for plugins and members goes out within seconds, by
a broker, whatever the procrastinate queues are busy with (``docs/hub.md``, section Event bus).

The bus is on when EVO_HUB_BROKER_URL names a broker (``amqp://`` or ``amqps://``); without it nothing here opens a
connection, and the hub delivers through the outbox and its jobs alone (``evo_agents.hub.plugin_events``,
``evo_agents.hub.server.notifications``). The outbox in Postgres stays what is true either way: a delivery's state, when
it is due and its tries. The broker only carries the news that something is due.

``soon`` is what a transaction that stored something to go out at once calls. Without the bus it queues the job that
delivers it (``jobs.soon``); with it, it sends a notification on the Postgres channel CHANNEL instead, which reaches
the delivery process (``evo-agents hub deliver``, ``evo_agents.hub.deliverer``) once the transaction commits. That
process publishes on the topic exchange EXCHANGE: the id of each delivery to a plugin that is due, with the routing
key ``plugin.<plugin>.<kind>`` (``routing_key``), which the plugin's quorum queue (``queue_name``) takes with a single
active consumer, so one consumer at a time delivers a plugin's events, in order; and a pass of the notifications with
the key NOTIFICATIONS_KEY, to the queue NOTIFICATIONS_QUEUE.

``check`` is the ``broker`` of ``/v1/health``: ``off`` without the variable, else ``ok`` when the broker takes a
connection and ``error`` when it does not, which leaves the hub healthy, as it still delivers through Postgres.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # the config reads the variable here on a client install too, which has no SQLAlchemy
    from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

BROKER_VARIABLE = "EVO_HUB_BROKER_URL"
CHANNEL = "hub_bus"  # the Postgres channel a transaction tells the delivery process on what it stored to go out
EXCHANGE = "hub.events"  # topic exchange: plugin.<plugin>.<kind>, notifications
NOTIFICATIONS_KEY = "notifications"
NOTIFICATIONS_QUEUE = "hub.notifications"
QUEUE_PREFIX = "hub.plugin."
QUEUE_ARGUMENTS = {"x-queue-type": "quorum", "x-single-active-consumer": True}
CHECK_TIMEOUT = 3.0  # seconds for the broker to take the connection of a health check
CHECK_EVERY = 15.0  # seconds a health check's answer is kept, so a busy probe does not open a connection each time


class BrokerUrlError(ValueError):
    """EVO_HUB_BROKER_URL is set and is not an amqp(s) URL."""


def broker_url(env: Mapping[str, str] | None = None) -> str | None:
    """The broker's URL, or None while the bus is off. The URL may hold a password: never log it."""
    env = os.environ if env is None else env
    raw = (env.get(BROKER_VARIABLE) or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.scheme not in ("amqp", "amqps") or not parts.hostname:
        raise BrokerUrlError(f"{BROKER_VARIABLE} must be an amqp:// or amqps:// URL such as amqp://rabbitmq:5672/")
    return raw


def on() -> bool:
    """Whether the bus is on in this process."""
    return broker_url() is not None


def queue_name(plugin: str) -> str:
    """The quorum queue of plugin ``plugin``'s deliveries."""
    return f"{QUEUE_PREFIX}{plugin}"


def routing_key(plugin: str, kind: str) -> str:
    """The routing key of a delivery of the event ``kind`` (or a notification) to plugin ``plugin``."""
    return f"plugin.{plugin}.{kind}"


def binding(plugin: str) -> str:
    """The binding of plugin ``plugin``'s queue to EXCHANGE: every kind."""
    return f"plugin.{plugin}.#"


async def soon(task: str, conn: AsyncConnection) -> None:
    """Have what ``conn``'s transaction stored delivered once it commits, by ``task``
    (``jobs.DELIVER_PLUGIN_EVENTS`` or ``jobs.DELIVER_NOTIFICATIONS``): through the bus when it is on, else by queueing
    a job of ``task`` (``jobs.soon``)."""
    if not on():
        from evo_agents.hub import jobs  # procrastinate is read only when a delivery is to go out at once

        await jobs.soon(task, conn)
        return
    from sqlalchemy import func, select

    await conn.execute(select(func.pg_notify(CHANNEL, task)))


_checked: dict[str, tuple[float, str]] = {}


async def check(url: str | None = None, *, timeout: float = CHECK_TIMEOUT) -> str:
    """The bus's status for ``/v1/health``: ``off``, ``ok`` or ``error``, kept CHECK_EVERY seconds."""
    url = broker_url() if url is None else url
    if url is None:
        return "off"
    kept = _checked.get(url)
    if kept is not None and time.monotonic() - kept[0] < CHECK_EVERY:
        return kept[1]
    import aio_pika

    try:
        connection = await asyncio.wait_for(aio_pika.connect(url, timeout=timeout), timeout)
        await connection.close()
        status = "ok"
    except Exception as exc:  # any failure to reach the broker is its error, never the hub's
        log.warning("health check: broker unavailable", extra={"error": type(exc).__name__})
        status = "error"
    _checked[url] = (time.monotonic(), status)
    return status

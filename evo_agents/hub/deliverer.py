"""``evo-agents hub deliver``: the delivery process of the event bus (``evo_agents.hub.bus``), which carries what a
commit stores for plugins and notification channels to them within seconds, whatever the procrastinate queues are busy
with. Run it beside the api and the worker when EVO_HUB_BROKER_URL is set; several of it may run at once.

Three parts, in one process:

- The relay listens on the Postgres channel ``bus.CHANNEL``, which a transaction that stored something to go out
  notifies (``bus.soon``) and which reaches it once that commits. Then, and every SWEEP_SECONDS at most, or when the
  soonest delivery not due yet falls due, it marks the due deliveries the bus has not carried since they fell due
  (``plugin_events.publishable``, published_at), commits, and publishes each one's id on the topic exchange
  ``bus.EXCHANGE`` with the routing key of its plugin and kind, in the order they fell due, each confirmed by the broker
  (publisher confirms). A notification's news becomes a message to the notifications' queue. A delivery the bus carried
  that nobody claimed within ``plugin_events.REPUBLISH_SECONDS``, a message lost with the broker, is published again
  (the sweeper), so neither a broker nor a relay that dies loses an event: the outbox in Postgres is what is true.
- When the broker cannot be reached or refuses a message, the relay marks what it could not publish unpublished and
  queues the job that delivers it instead (``jobs.soon``: hub.deliver_plugin_events or hub.deliver_notifications), so
  the hub delivers through its jobs while the broker is away, as it does without the bus; it tries the broker again
  each sweep, and the connection reconnects on its own.
- The consumers take each plugin's quorum queue (``bus.queue_name``) with a single active consumer and one message at
  a time, so one process at a time delivers a plugin's events, in the order they were published, and the notifications'
  queue. A message of a plugin's queue names a delivery: the consumer claims it on the outbox and tries it once as a
  pass does (``plugin_events.deliver_one``), writes what came of it, then acks the message. A delivery that is not
  pending and due any more, a message sent twice or one a job's pass beat, is acked and dropped: the claim, with
  ``FOR UPDATE SKIP LOCKED``, is the one place that decides who delivers. A message of the notifications' queue runs a
  pass of the notifications (``notifications.deliver_notifications``). A message whose handling failed is rejected,
  not requeued: the sweeper publishes its delivery again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import signal
from datetime import UTC, datetime

import psycopg
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents import __version__
from evo_agents.hub import bus, jobs, plugin_events, tables
from evo_agents.hub.db import CONNECT_TIMEOUT

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub-deliver"
SWEEP_SECONDS = 5.0  # the relay looks for due deliveries at least this often
PLUGINS_SECONDS = 60.0  # the consumers look for plugins installed since at most this often
CONNECT_BACKOFF = (1.0, 30.0)  # seconds between tries to reach the broker, or to LISTEN again
PUBLISH_TIMEOUT = 5.0  # seconds for the broker to confirm one message


class Broker:
    """The connection to the broker, which reconnects on its own once it was made, a channel with publisher confirms
    on the exchange, and a channel of its own for each queue consumed."""

    def __init__(self, url: str):
        self._url = url
        self._connection = None
        self._exchange = None
        self._consumed: dict[str, object] = {}

    @property
    def ready(self) -> bool:
        return self._connection is not None and not self._connection.is_closed and self._exchange is not None

    async def connect(self) -> None:
        import aio_pika

        connection = await aio_pika.connect_robust(self._url, timeout=PUBLISH_TIMEOUT)
        try:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(bus.EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        except BaseException:
            await connection.close()
            raise
        self._connection, self._exchange = connection, exchange

    async def publish(self, key: str, body: dict) -> None:
        """Publish ``body`` with routing key ``key``, persistent; returns once the broker confirmed it."""
        import aio_pika

        if not self.ready:
            raise ConnectionError("the broker is not connected")
        message = aio_pika.Message(
            json.dumps(body).encode(), content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT
        )
        await self._exchange.publish(message, routing_key=key, timeout=PUBLISH_TIMEOUT)

    async def consume(self, queue: str, key: str, handler) -> None:
        """Declare quorum queue ``queue`` with a single active consumer, bound to the exchange by ``key``, and consume
        it one message at a time with ``handler``, once: the connection restores it after a reconnect."""
        if queue in self._consumed or not self.ready:
            return
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=1)
        declared = await channel.declare_queue(queue, durable=True, arguments=bus.QUEUE_ARGUMENTS)
        await declared.bind(bus.EXCHANGE, routing_key=key)
        await declared.consume(handler)
        self._consumed[queue] = channel

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection, self._exchange, self._consumed = None, None, {}


class Deliverer:
    """The relay, the sweeper and the consumers over ``engine`` and ``broker``."""

    def __init__(self, engine: AsyncEngine, broker: Broker, *, config=None, host=None, blobs=None, dsn: str):
        self.engine, self.broker = engine, broker
        self.config, self.host, self.blobs, self.dsn = config, host, blobs, dsn
        self.wake = asyncio.Event()
        self.notifications = False  # a commit stored notifications the relay has not passed on yet
        self.report = {"published": 0, "fallback": 0, "consumed": 0}

    # The relay

    def on_notify(self, payload: str | None) -> None:
        if payload == jobs.DELIVER_NOTIFICATIONS:
            self.notifications = True
        self.wake.set()

    async def listen(self) -> None:
        """Wake the relay on each notification of ``bus.CHANNEL``, and once each time the LISTEN is (re)made."""
        backoff = CONNECT_BACKOFF[0]
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self.dsn, autocommit=True, application_name=APPLICATION_NAME, connect_timeout=CONNECT_TIMEOUT
                )
                async with conn:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(bus.CHANNEL)))
                    backoff = CONNECT_BACKOFF[0]
                    self.on_notify(None)
                    async for notify in conn.notifies():
                        self.on_notify(notify.payload)
            except (psycopg.Error, OSError) as exc:
                log.warning(
                    "deliver cannot listen; the relay looks every few seconds instead",
                    extra={"error": type(exc).__name__, "retry_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, CONNECT_BACKOFF[1])

    async def relay_once(self) -> dict:
        """Publish what is due and the notifications' news; what could not be published goes to the jobs."""
        done = {"published": 0, "fallback": 0}
        async with self.engine.begin() as conn:
            rows = sorted((await conn.execute(plugin_events.publishable())).all(), key=lambda r: (r.next_at, r.id))
        left = []
        for number, row in enumerate(rows):
            try:
                await self.consume_plugin(row.name)
                await self.broker.publish(bus.routing_key(row.name, row.kind), {"id": row.id})
            except Exception as exc:  # the broker away or refusing: the rest goes to the jobs, in order
                log.warning("deliver cannot publish; the jobs deliver instead", extra={"error": type(exc).__name__})
                left = [r.id for r in rows[number:]]
                break
            done["published"] += 1
        if left:
            async with self.engine.begin() as conn:
                await conn.execute(plugin_events.unpublish(left))
                await jobs.soon(jobs.DELIVER_PLUGIN_EVENTS, conn)
            done["fallback"] += len(left)
        if self.notifications:
            self.notifications = False
            try:
                await self.broker.publish(bus.NOTIFICATIONS_KEY, {"at": datetime.now(UTC).isoformat()})
            except Exception as exc:
                log.warning(
                    "deliver cannot publish notifications; a job sends them", extra={"error": type(exc).__name__}
                )
                async with self.engine.begin() as conn:
                    await jobs.soon(jobs.DELIVER_NOTIFICATIONS, conn)
                done["fallback"] += 1
        for name, count in done.items():
            self.report[name] += count
        return done

    async def relay(self) -> None:
        """``relay_once`` on each wake, at least every SWEEP_SECONDS, and when the soonest delivery falls due."""
        while True:
            wait = SWEEP_SECONDS
            try:
                await self.relay_once()
                async with self.engine.begin() as conn:
                    when = await plugin_events.soonest(conn)
                if when is not None:
                    wait = min(wait, max(0.05, (when - datetime.now(UTC)).total_seconds()))
            except Exception:  # Postgres away: the next sweep tries again
                log.exception("deliver: the relay failed; it tries again")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), wait)
            self.wake.clear()

    # The consumers

    async def consume_plugin(self, name: str) -> None:
        await self.broker.consume(bus.queue_name(name), bus.binding(name), self.on_delivery)

    async def consume_all(self) -> None:
        """Consume the notifications' queue and each installed plugin's queue the process does not consume yet."""
        await self.broker.consume(bus.NOTIFICATIONS_QUEUE, bus.NOTIFICATIONS_KEY, self.on_notifications)
        p = tables.plugins
        async with self.engine.begin() as conn:
            names = (await conn.execute(select(p.c.name).where(p.c.installed_version.is_not(None)))).scalars().all()
        for name in names:
            await self.consume_plugin(name)

    async def on_delivery(self, message) -> None:
        try:
            delivery_id = int(json.loads(message.body)["id"])
        except (ValueError, KeyError, TypeError):
            log.warning("deliver: a message without a delivery id dropped")
            await message.reject(requeue=False)
            return
        try:
            outcome = await plugin_events.deliver_one(self.engine, delivery_id, config=self.config, host=self.host)
        except Exception:
            log.exception(
                "deliver: a delivery failed; the sweeper publishes it again", extra={"delivery_id": delivery_id}
            )
            await message.reject(requeue=False)
            return
        await message.ack()
        self.report["consumed"] += 1
        if outcome is not None:
            log.info("plugin delivery", extra={"delivery_id": delivery_id, "outcome": outcome})

    async def on_notifications(self, message) -> None:
        from evo_agents.hub.server.notifications import deliver_notifications

        try:
            await deliver_notifications(self.engine, config=self.config, blobs=self.blobs)
        except Exception:
            log.exception("deliver: a pass of the notifications failed; the minute's job tries again")
            await message.reject(requeue=False)
            return
        await message.ack()

    async def keep_connected(self) -> None:
        """Reach the broker, trying again with a backoff until it answers, then consume; look for plugins installed
        since every PLUGINS_SECONDS."""
        backoff = CONNECT_BACKOFF[0]
        while not self.broker.ready:
            try:
                await self.broker.connect()
            except Exception as exc:
                log.warning(
                    "deliver cannot reach the broker; the jobs deliver meanwhile",
                    extra={"error": type(exc).__name__, "retry_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, CONNECT_BACKOFF[1])
        log.info("deliver connected to the broker")
        self.wake.set()
        while True:
            try:
                await self.consume_all()
            except Exception as exc:
                log.warning("deliver cannot consume every queue yet", extra={"error": type(exc).__name__})
            await asyncio.sleep(PLUGINS_SECONDS)

    async def run(self, stop: asyncio.Event) -> None:
        tasks = [
            asyncio.create_task(self.keep_connected(), name="deliver-broker"),
            asyncio.create_task(self.listen(), name="deliver-listen"),
            asyncio.create_task(self.relay(), name="deliver-relay"),
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def run(config) -> int:
    from evo_agents.hub.blobs import BlobStore
    from evo_agents.hub.db import make_engine, open_pool
    from evo_agents.hub.log import redact_dsn
    from evo_agents.hub.plugin_calls import PluginHostClient

    target = redact_dsn(config.dsn)
    log.info("deliver starting", extra={"version": __version__, "db": target})
    try:
        pool = await open_pool(config)
    except Exception as exc:
        log.error("deliver cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
        return 1
    engine = make_engine(pool)
    broker = Broker(config.broker_url)
    host = PluginHostClient(config.plugin_host_url, config.plugin_host_token)
    deliverer = Deliverer(engine, broker, config=config, host=host, blobs=BlobStore.from_config(config), dsn=config.dsn)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows has no signal handlers on the loop
            loop.add_signal_handler(signum, stop.set)
    try:
        await deliverer.run(stop)
    finally:
        await broker.close()
        await host.aclose()
        await engine.dispose()
        await pool.close()
    log.info("deliver stopped", extra=deliverer.report)
    return 0


def main(config) -> int:
    return asyncio.run(run(config))

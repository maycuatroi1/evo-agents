"""Wake-ups between the hub's processes: the one notifier every write that wakes someone goes through, a run queued for
the claims waiting (``run_state.RUNS_CHANNEL``), an event of a run for its streams (``run_state.EVENTS_CHANNEL``), a
chat's change for its streams (``chat_store.CHANNEL``) and a span of a system's trace for its trace streams
(``system_traces.CHANNEL``). A wake-up is a channel and a payload, the id of what changed; it only tells whoever waits
to look again, so one too many costs a query and one lost costs the few seconds until they look on their own.

Without the event bus (``bus.on``) ``notify`` is Postgres's NOTIFY in the caller's transaction, which reaches each api
process's LISTEN once the transaction commits (``server.listen.Listener``), as before. With it, the wake-up is kept on
the connection and published on the direct exchange EXCHANGE, with the channel as routing key and the payload as body,
once the transaction committed: the connection's ``commit`` moves what its transaction kept to what is to go out, a
``rollback`` drops it, and what is to go out leaves when the connection begins its next transaction or goes back to
the pool, that is after the commit returned. Each api process takes the exchange on a queue of its own
(``Listener``), so a run queued in one process wakes the claims waiting in every other within a moment, whatever the
database's connections are. The equal wake-ups of one transaction go out as one, as NOTIFY folds them.

The publisher is one connection per process and event loop, opened by the first wake-up and kept (``close`` ends it).
A wake-up it cannot publish, the broker away, is logged and dropped, never retried: the waiting look again on their
own every few seconds (``runs.CLAIM_POLL_SECONDS``, the streams' pings).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import TYPE_CHECKING

from evo_agents.hub import bus

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncConnection

log = logging.getLogger(__name__)

EXCHANGE = "hub.wake"  # direct exchange: routing key the channel, body the payload
CONNECT_TIMEOUT = 5.0  # seconds for the broker to take the connection of the publisher or of a listener
BACKOFF = (1.0, 60.0)  # seconds the publisher waits before connecting again after it failed
_KEPT = "hub_wake_kept"  # the connection's info: the wake-ups of its open transaction
_DUE = "hub_wake_due"  # ...and those of a transaction that committed, to go out


async def notify(conn: AsyncConnection, channel: str, payload: str) -> None:
    """Wake whoever waits on ``channel`` with ``payload`` once ``conn``'s transaction commits."""
    if not bus.on():
        await conn.execute(_pg_notify(channel, payload))
        return
    _keep(conn.sync_connection, channel, payload)


def notify_sync(connection: Connection, channel: str, payload: str) -> None:
    """``notify`` on the sync side of a connection, for an engine event (``evo_agents.hub.cache``)."""
    if not bus.on():
        connection.execute(_pg_notify(channel, payload))
        return
    _keep(connection, channel, payload)


def _pg_notify(channel: str, payload: str):
    from sqlalchemy import func, select

    return select(func.pg_notify(channel, payload))


def _keep(connection: Connection, channel: str, payload: str) -> None:
    _watch(connection.engine)
    if not connection.in_transaction():
        _publish({(channel, payload)})
        return
    connection.info.setdefault(_KEPT, set()).add((channel, payload))


def _watch(engine) -> None:
    """Have ``engine``'s connections publish what their transactions kept after they commit; once per engine."""
    from sqlalchemy import event

    if event.contains(engine, "commit", _committed):
        return
    event.listen(engine, "commit", _committed)
    event.listen(engine, "rollback", _rolled_back)
    event.listen(engine, "begin", _began)
    event.listen(engine.pool, "checkin", _checked_in)  # kept by dispose(), which recreates the pool


def _committed(connection) -> None:
    kept = connection.info.pop(_KEPT, None)
    if kept:
        connection.info.setdefault(_DUE, set()).update(kept)


def _rolled_back(connection) -> None:
    connection.info.pop(_KEPT, None)


def _began(connection) -> None:
    due = connection.info.pop(_DUE, None)
    if due:
        _publish(due)


def _checked_in(dbapi_connection, record) -> None:
    if record is None:
        return
    record.info.pop(_KEPT, None)
    due = record.info.pop(_DUE, None)
    if due:
        _publish(due)


_publishers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _Publisher] = weakref.WeakKeyDictionary()


def _publish(wakes: set[tuple[str, str]]) -> None:
    url = bus.broker_url()
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("a wake-up outside an event loop is dropped", extra={"wakes": len(wakes)})
        return
    if url is None:
        return
    publisher = _publishers.get(loop)
    if publisher is None or publisher.url != url:
        publisher = _publishers[loop] = _Publisher(url)
    publisher.send(wakes)


class _Publisher:
    """The process's connection to the broker for wake-ups, on one event loop: a task that publishes what ``send``
    queued, in order, connecting on the first and again after a failure."""

    def __init__(self, url: str):
        self.url = url
        self._queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._connection = None

    def send(self, wakes: set[tuple[str, str]]) -> None:
        for wake in sorted(wakes):
            self._queue.put_nowait(wake)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="evo-hub-wake-publish")

    async def _run(self) -> None:
        import aio_pika

        backoff = BACKOFF[0]
        while True:
            channel, payload = await self._queue.get()
            try:
                if self._connection is None or self._connection.is_closed:
                    self._connection = await aio_pika.connect(self.url, timeout=CONNECT_TIMEOUT)
                    self._exchange = await (await self._connection.channel()).declare_exchange(
                        EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
                    )
                await self._exchange.publish(aio_pika.Message(payload.encode()), routing_key=channel)
                backoff = BACKOFF[0]
            except Exception as exc:  # the broker away: the waiting look again on their own
                dropped = 1 + self._queue.qsize()
                while not self._queue.empty():
                    self._queue.get_nowait()
                log.warning(
                    "cannot publish wake-ups; who waits looks again on their own",
                    extra={"error": type(exc).__name__, "dropped": dropped, "retry_s": backoff},
                )
                await self._drop_connection()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF[1])

    async def _drop_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:  # closing what already failed
                pass

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._drop_connection()


async def close() -> None:
    """End the publisher of the running event loop, when it has one."""
    publisher = _publishers.pop(asyncio.get_running_loop(), None)
    if publisher is not None:
        await publisher.close()

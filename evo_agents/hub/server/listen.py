"""The api process's one LISTEN connection to Postgres, or its one queue on the event bus, shared by everything in the process that waits for a
notification: the claims waiting for a queued run (``runs.RunWakeups`` on RUNS_CHANNEL), the event streams of
runs (``run_events.RunStreams`` on EVENTS_CHANNEL) and those of group chats (``chat_events.ChatStreams``).

Each subscriber names its channel and a handler before the listener starts; the first claim or stream starts it.
The connection is in autocommit, so a notification arrives as soon as the transaction that sent it commits. A
handler gets the notification's payload, or None right after the connection is (re)established, when whatever was
notified while nobody listened has to be looked for again. Handlers run on the event loop and must not block or
raise. When the connection fails, the listener tries again after a backoff growing from 1 to 60 seconds; the
subscribers look again on their own every few seconds besides, so a lost notification delays them and never loses
anything.

With the event bus on (``broker_url``), the wake-ups come from the broker instead (``evo_agents.hub.wake``): the
listener takes the direct exchange ``wake.EXCHANGE`` on a queue of its own, exclusive to its connection and deleted
with it, bound to each subscribed channel, and hands each message's body to the handlers of its routing key. Nothing
then listens on Postgres, since the writes no longer NOTIFY. A message's handlers are called as a notification's are,
None after each (re)connection included, and a lost connection is tried again with the same backoff.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psycopg
from psycopg import sql

from evo_agents.hub import wake
from evo_agents.hub.db import CONNECT_TIMEOUT

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub-listen"
BACKOFF = (1.0, 60.0)  # seconds between attempts to LISTEN again after the connection failed

Handler = Callable[[str | None], None]


class Listener:
    def __init__(self, dsn: str, broker_url: str | None = None):
        self._dsn = dsn
        self._broker_url = broker_url
        self._handlers: dict[str, list[Handler]] = {}
        self._task: asyncio.Task | None = None
        self.up = False  # whether it listens now: from its (re)connection until the connection fails

    def on(self, channel: str, handler: Handler) -> None:
        """Call ``handler`` with the payload of each notification on ``channel``; before the listener starts."""
        if self._task is not None:
            raise RuntimeError("the listener has started; subscribe before the first claim or stream")
        self._handlers.setdefault(channel, []).append(handler)

    def start(self) -> None:
        if self._task is None or self._task.done():
            listen = self._listen() if self._broker_url is None else self._consume(self._broker_url)
            self._task = asyncio.get_running_loop().create_task(listen, name="evo-hub-listen")

    def _dispatch(self, channel: str, payload: str | None) -> None:
        for handler in self._handlers.get(channel, ()):
            try:
                handler(payload)
            except Exception:  # one subscriber's bug must not stop the others' notifications
                log.exception("a notification handler failed", extra={"channel": channel})

    async def _listen(self) -> None:
        backoff = BACKOFF[0]
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    self._dsn, autocommit=True, application_name=APPLICATION_NAME, connect_timeout=CONNECT_TIMEOUT
                )
                async with conn:
                    for channel in self._handlers:
                        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
                    backoff, self.up = BACKOFF[0], True
                    for channel in self._handlers:  # what was notified while nobody listened is found by a look
                        self._dispatch(channel, None)
                    async for notify in conn.notifies():
                        self._dispatch(notify.channel, notify.payload)
            except (psycopg.Error, OSError) as exc:
                self.up = False
                log.warning(
                    "cannot listen for notifications; claims and run streams look every few seconds instead",
                    extra={"error": type(exc).__name__, "retry_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF[1])

    async def _consume(self, url: str) -> None:
        import aio_pika

        backoff = BACKOFF[0]
        while True:
            try:
                connection = await aio_pika.connect(url, timeout=wake.CONNECT_TIMEOUT)
                async with connection:
                    channel = await connection.channel()
                    exchange = await channel.declare_exchange(wake.EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
                    queue = await channel.declare_queue(exclusive=True, auto_delete=True)
                    for name in self._handlers:
                        await queue.bind(exchange, routing_key=name)
                    backoff, self.up = BACKOFF[0], True
                    for name in self._handlers:  # what was published while nobody listened is found by a look
                        self._dispatch(name, None)
                    async with queue.iterator(no_ack=True) as messages:
                        async for message in messages:
                            self._dispatch(message.routing_key or "", message.body.decode())
                error = "closed"
            except Exception as exc:  # the broker away or the connection lost: try again
                error = type(exc).__name__
            self.up = False
            log.warning(
                "cannot take wake-ups from the broker; claims and run streams look every few seconds instead",
                extra={"error": error, "retry_s": backoff},
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF[1])

    async def close(self) -> None:
        self.up = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

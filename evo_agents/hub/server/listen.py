"""The api process's one LISTEN connection to Postgres, shared by everything in the process that waits for a
notification: the claims waiting for a queued run (``runs.RunWakeups`` on RUNS_CHANNEL) and the event streams of
runs (``run_events.RunStreams`` on EVENTS_CHANNEL).

Each subscriber names its channel and a handler before the listener starts; the first claim or stream starts it.
The connection is in autocommit, so a notification arrives as soon as the transaction that sent it commits. A
handler gets the notification's payload, or None right after the connection is (re)established, when whatever was
notified while nobody listened has to be looked for again. Handlers run on the event loop and must not block or
raise. When the connection fails, the listener tries again after a backoff growing from 1 to 60 seconds; the
subscribers look again on their own every few seconds besides, so a lost notification delays them and never loses
anything.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import psycopg
from psycopg import sql

from evo_agents.hub.db import CONNECT_TIMEOUT

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub-listen"
BACKOFF = (1.0, 60.0)  # seconds between attempts to LISTEN again after the connection failed

Handler = Callable[[str | None], None]


class Listener:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._handlers: dict[str, list[Handler]] = {}
        self._task: asyncio.Task | None = None

    def on(self, channel: str, handler: Handler) -> None:
        """Call ``handler`` with the payload of each notification on ``channel``; before the listener starts."""
        if self._task is not None:
            raise RuntimeError("the listener has started; subscribe before the first claim or stream")
        self._handlers.setdefault(channel, []).append(handler)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._listen(), name="evo-hub-listen")

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
                    backoff = BACKOFF[0]
                    for channel in self._handlers:  # what was notified while nobody listened is found by a look
                        self._dispatch(channel, None)
                    async for notify in conn.notifies():
                        self._dispatch(notify.channel, notify.payload)
            except (psycopg.Error, OSError) as exc:
                log.warning(
                    "cannot listen for notifications; claims and run streams look every few seconds instead",
                    extra={"error": type(exc).__name__, "retry_s": backoff},
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF[1])

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

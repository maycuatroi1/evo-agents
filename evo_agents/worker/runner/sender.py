"""The events of a run on their way from its spool to the hub."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from evo_agents.worker.hubapi import Backoff, Outdated, Refused, Unreachable
from evo_agents.worker.runner.common import HARD_STOP_TRIES, log, wait_or
from evo_agents.worker.spool import Spool

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

FLUSH_DEBOUNCE = 0.2  # seconds of events gathered into one batch


class Sender:
    """Sends a spool's events to the hub in batches, from ``ack_seq + 1``, until it is closed and empty. A failed
    call is tried again with the backoff; a hub that will not take the run's events (404, the run's 20,000) ends
    the sending and drops what is left."""

    def __init__(self, daemon: Daemon, spool: Spool):
        self.daemon = daemon
        self.spool = spool
        self.wake = asyncio.Event()
        self.closed = False
        self.gave_up: str | None = None
        self._failures_logged = 0

    def poke(self) -> None:
        self.wake.set()

    def close(self) -> None:
        self.closed = True
        self.wake.set()

    @property
    def settled(self) -> bool:
        """Every event is acknowledged, or the sending gave up."""
        return self.gave_up is not None or not self.spool.pending

    async def run(self) -> None:
        try:
            await self._send()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken spool must not leave the run waiting for its events forever
            log.exception("sending events failed", extra={"run_id": self.spool.run_id})
            self.gave_up = f"{type(exc).__name__}: {exc}"

    async def _send(self) -> None:
        backoff = Backoff()
        run_id = self.spool.run_id
        while True:
            self.wake.clear()
            if not self.spool.pending:
                if self.closed:
                    return
                await self.wake.wait()
                if not self.closed:
                    await asyncio.sleep(FLUSH_DEBOUNCE)
                continue
            batch = self.spool.batch()
            try:
                answer = await self.daemon.hub.events(run_id, batch)
            except Unreachable as exc:
                delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                if self._failures_logged < 3 or backoff.failures % 10 == 0:
                    log.warning(
                        "events not sent; they wait in the spool",
                        extra={
                            "run_id": run_id,
                            "pending": self.spool.pending,
                            "retry_in_s": round(delay, 1),
                            "error": str(exc),
                        },
                    )
                self._failures_logged += 1
                if self.daemon.hard_stop.is_set() and backoff.failures > HARD_STOP_TRIES:
                    self.gave_up = "the daemon stopped"
                    return
                await wait_or(self.daemon.hard_stop, delay)
                continue
            except Refused as exc:
                if isinstance(exc, Outdated) or exc.status in (401, 403):
                    self.daemon.fatal(exc)
                self.gave_up = str(exc)
                log.warning(
                    "the hub does not take this run's events; dropping them",
                    extra={"run_id": run_id, "status": exc.status, "dropped": self.spool.pending, "error": str(exc)},
                )
                self.spool.drop_all()
                return
            if self._failures_logged:
                log.info("events sent again after the hub answered", extra={"run_id": run_id})
                self._failures_logged = 0
            backoff.reset()
            ack = answer.get("ack_seq") if isinstance(answer, dict) else None
            if not isinstance(ack, int):
                self.gave_up = "the hub answered events without ack_seq"
                self.spool.drop_all()
                return
            if ack < batch[0]["seq"] - 1:
                # The hub misses events before this spool's first: they are gone, and nothing after a gap is stored.
                self.gave_up = (
                    f"the hub lacks events {ack + 1} to {batch[0]['seq'] - 1}, which this worker no longer has"
                )
                log.error("event gap", extra={"run_id": run_id, "ack_seq": ack, "first": batch[0]["seq"]})
                self.spool.drop_all()
                return
            self.spool.acknowledge(ack)

    async def drained(self) -> bool:
        """Wait until every event is acknowledged or the sending gave up; whether everything was sent."""
        self.poke()
        while not self.settled:
            await asyncio.sleep(0.05)
        return self.gave_up is None

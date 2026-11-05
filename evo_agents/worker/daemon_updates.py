"""The self-update of a worker's daemon (``daemon``, ``selfupdate``): a turn each heartbeat period, and the update
the hub needs once it answered 426 and the runs held ended. Either one that updated the install has the daemon exit
``home.EXIT_UPDATED`` for its manager to start the new one."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from evo_agents.worker.home import EXIT_UPDATED

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


class Updates:
    """The turns of ``daemon``'s updater."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon

    def who(self) -> str:
        config = self.daemon.config
        return f"{config.name} (id {config.worker_id})"

    async def loop(self) -> None:
        """A turn of the self-update each heartbeat period; one that updated the install, or found it updated, stops
        the daemon to exit EXIT_UPDATED."""
        daemon = self.daemon
        while not daemon.stopping.is_set():
            try:
                held, orphans = len(daemon.runs), len(daemon.orphans)
                restart = await daemon.updater.turn(held=held, orphans=orphans, worker=self.who())
            except Exception:  # a bug here must not end the daemon
                log.exception("self-update: the turn failed")
                restart = False
            if restart and not daemon.stopping.is_set():
                daemon.exit_code = EXIT_UPDATED
                log.info("stopping to restart on the updated install", extra={"exit_code": EXIT_UPDATED})
                daemon.stopping.set()
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(daemon.stopping.wait(), daemon.heartbeat_s)

    async def for_hub(self, updates: asyncio.Task) -> None:
        """The hub answered 426 and the runs ended: update to nightly, if the daemon may, before it exits."""
        updates.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await updates
        try:
            updated = await self.daemon.updater.after_outdated(self.daemon.config.version_locked)
        except Exception:
            log.exception("self-update: the update the hub needs failed")
            updated = False
        if updated:
            self.daemon.exit_code = EXIT_UPDATED

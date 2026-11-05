"""The browser of a worker's daemon (``daemon``): what `evo-agents worker browser install` put on the machine, and its
owner's Install browser.

A daemon whose machine ran ``browser install`` (``browser.install``, read again with the runtimes) says the feature
browser and its Chromium in each heartbeat. The answer's ``browser_install`` (feature browser_install) has the daemon
install Chromium as that command does, in a thread next to the runs it holds, claims going on: once it is in, the next
heartbeat says the feature browser, which ends the request on the hub; an install that fails says why in the
heartbeat's ``browser_install`` until the hub ends the request. A request comes once per daemon: a new one, or the
same one after a restart, installs again, which changes nothing once Chromium is there.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from evo_agents.hub import runs
from evo_agents.worker.background import spawn
from evo_agents.worker.browser import install as browser_install

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon

log = logging.getLogger("evo_agents.worker")


class BrowserInstall:
    """The Chromium of ``daemon``'s machine, as heartbeats say it, and the install its owner asked for."""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon
        self.report: dict | None = None  # the Chromium `worker browser install` installed, as heartbeats say it
        # the owner's open Install browser, as heartbeats report it: {requested_at, ended, error}; None for none
        self.asked: dict | None = None
        self.installing: asyncio.Task | None = None  # the install of Chromium it carries out, while it runs

    async def look(self) -> None:
        """Read again what Chromium this machine holds."""
        self.report = await asyncio.to_thread(browser_install.report, self.daemon.home)

    def take(self, value) -> None:
        """Its owner's Install browser, as the answer carries it: a request new to the daemon installs Chromium in the
        background unless an install runs already, whose end then counts for it; none, the hub ended the request."""
        requested_at = value.get("requested_at") if isinstance(value, dict) else None
        if not isinstance(requested_at, str):
            self.asked = None
            return
        if self.asked is not None and self.asked["requested_at"] == requested_at:
            return
        self.asked = {"requested_at": requested_at, "ended": None, "error": None}
        if self.installing is None or self.installing.done():
            self.installing = spawn(self._install(), "browser-install")

    async def _install(self) -> None:
        """Install Chromium as `evo-agents worker browser install` does, in a thread, without its progress on the
        daemon's output; the heartbeats then say the feature browser, or why the install failed."""
        log.info("installing a browser, as the owner asked from the hub")
        try:
            installed, _ = await asyncio.to_thread(
                browser_install.install, self.daemon.home, self.daemon.env, say=log.info, quiet=True
            )
        except Exception as exc:  # an InstallError says why; anything else must not end the daemon either
            if not isinstance(exc, browser_install.InstallError):
                log.exception("the browser install broke")
            error = (str(exc) or type(exc).__name__)[: runs.MAX_BROWSER_INSTALL_ERROR]
            log.error("the browser could not be installed", extra={"error": error})
            if self.asked is not None:
                self.asked = {**self.asked, "ended": "failed", "error": error}
            return
        self.report = installed.report()
        log.info("browser installed", extra={"browser": self.report})

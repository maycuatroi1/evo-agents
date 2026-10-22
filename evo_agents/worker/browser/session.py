"""The browser of a run dispatched with browser (plan agent-browser, criterion a3): one Chromium a run, which the daemon
starts before the run's first agent and stops when the run leaves the states it holds a run in.

``BrowserSession.open`` starts the Chromium ``evo-agents worker browser install`` installed (``install.read``), with
``chromium.flags``, on a profile in ``runs/<run>/browser/profile``, and waits for the DevToolsActivePort it writes:
the agent gets ``http://127.0.0.1:<port>`` in EVO_BROWSER_CDP (``Agent.env``). The browser counts in the run's slot,
and the daemon never holds more of them at once than the max its heartbeat says (``Daemon.browsers``): the hub hands
it no more than that, and one more would fail with cause environment. So does a Chromium that does not start, with
the end of its log; no Chromium installed is cause missing_tool, which the preflight says first
(``install.problems``).

``BrowserSession.close``, when the run ends, is parked or the daemon stops (``Run._wind_down``): Chromium is asked to
close over its DevTools socket (Browser.close), so it ends writing its profile, then what is left of its process group
and of the processes it started in groups of their own is killed (``orphans.stop``), and ``runs/<run>/browser`` goes.
``browser/chromium.json`` names the process group while Chromium runs, so a daemon that starts after one that died
stops it and removes the directory of every run it does not hold (``sweep``).

A run dispatched with browser identities (criterion a4) starts on the profile of the first and with the cookies of the
others (``profiles.Profiles``), on an empty profile when Chromium does not start on that one; ``save``, as the run ends
and before its final report, sends each identity's profile back to the hub and closes Chromium.

``watch``, as soon as Chromium listens, starts the broker of the run's live view (``live.LiveView``, criterion a5),
which ``save`` and ``close`` stop first, closing its end on the hub."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from evo_agents.worker import orphans
from evo_agents.worker.browser import chromium, install, live, profiles
from evo_agents.worker.runner.common import RunFailed, log
from evo_agents.worker.runtimes.common import descendants, kill_tree

if TYPE_CHECKING:
    from evo_agents.worker.daemon import Daemon
    from evo_agents.worker.home import WorkerHome

DIRECTORY = "browser"  # in the run's directory
PROFILE = "profile"
LOG = "chromium.log"
NOTED = "chromium.json"  # the process group of the Chromium that runs, as orphans.describe says it
VARIABLE = "EVO_BROWSER_CDP"
START_TIMEOUT = 30.0  # seconds Chromium has to write its DevTools port
CLOSE_TIMEOUT = 10.0  # seconds Chromium has to end after Browser.close
STOP_GRACE = 5.0  # seconds what is left of it has to end on SIGTERM before SIGKILL
POLL = 0.1


def run_directory(home: WorkerHome, run_id: int) -> Path:
    return home.run_dir(run_id) / DIRECTORY


class BrowserSession:
    """The Chromium of run ``run_id`` on the daemon ``daemon``."""

    def __init__(self, daemon: Daemon, run_id: int, identities: Iterable[int] = ()):
        self.daemon = daemon
        self.run_id = run_id
        self.profiles = profiles.Profiles(self, identities)  # its browser identities (spec browser_identities)
        self.directory = run_directory(daemon.home, run_id)
        self.process: subprocess.Popen | None = None
        self.cdp: str | None = None
        self.socket: str | None = None  # the path of the browser's DevTools websocket
        self.noted: dict = {}  # its process group, as orphans.describe said it at the start
        self.live: live.LiveView | None = None  # the broker of its live view, while Chromium runs

    @property
    def profile(self) -> Path:
        return self.directory / PROFILE

    async def open(self) -> str:
        """Start Chromium for the run, once; its DevTools endpoint, http://127.0.0.1:<port>."""
        if self.cdp is not None:
            return self.cdp
        installed = await asyncio.to_thread(install.read, self.daemon.home)
        if installed is None:
            raise RunFailed(
                f"the run asks for a browser and Chromium is not installed on this worker: run `{install.COMMAND}`",
                cause="missing_tool",
            )
        held = self.daemon.browsers
        if len(held) >= installed.max and self.run_id not in held:
            raise RunFailed(
                f"this worker holds {len(held)} browsers already, its max: the hub hands it no more",
                cause="environment",
            )
        held[self.run_id] = self
        try:
            await self.profiles.prepare(installed)
            await self._start_on_profile(installed)
            await self.profiles.restore()
        except BaseException:
            await self.close()
            raise
        return self.cdp

    async def _start_on_profile(self, installed: install.Installed) -> None:
        """Start Chromium on the first identity's profile, or on an empty one when it does not start on that."""
        try:
            await self._start(installed)
        except (RunFailed, OSError):
            if not self.profiles.unpacked:
                raise
            process, self.process = self.process, None
            if process is not None:
                await asyncio.to_thread(chromium.kill, process)
            await asyncio.to_thread(shutil.rmtree, self.profile, True)
            await self._start(installed)
            await self.profiles.unopenable()

    def watch(self, state_of: Callable[[], str]) -> None:
        """Start the broker of the run's live view, once Chromium listens; ``state_of()`` is the run's state."""
        hub = getattr(self.daemon, "hub", None)
        if self.cdp is None or self.live is not None or hub is None:
            return
        devtools = self.cdp.replace("http://", "ws://", 1) + (self.socket or "")
        self.live = live.LiveView(self.run_id, devtools, hub.session, hub.url, self.daemon.token, state_of)
        self.live.start()

    async def _unwatch(self) -> None:
        watching, self.live = self.live, None
        if watching is not None:
            await watching.stop()

    async def save(self) -> None:
        """Stop the run's live view, then send the profiles of the run's browser identities back to the hub, once,
        closing its Chromium (``Profiles.save``); no profile for a run without identities."""
        await self._unwatch()  # the run ends: its live view with it
        await self.profiles.save()

    async def _start(self, installed: install.Installed) -> None:
        self.directory.mkdir(mode=chromium.DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.directory, chromium.DIR_MODE)
        self.process = await asyncio.to_thread(
            chromium.start,
            installed.executable,
            self.profile,
            installed.version,
            sandbox=installed.sandbox,
            libraries=installed.libraries,
            base_env=self.daemon.env,
            log_path=self.directory / LOG,
        )
        self.noted = await asyncio.to_thread(orphans.describe, self.process.pid)
        (self.directory / NOTED).write_text(json.dumps(self.noted), encoding="utf-8")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT
        while loop.time() < deadline:
            found = chromium.port(self.profile)
            if found is not None:
                port, self.socket = found
                self.cdp = f"http://127.0.0.1:{port}"
                log.info("browser started", extra={"run_id": self.run_id, "pid": self.process.pid, "port": port})
                return
            if self.process.poll() is not None:
                break
            await asyncio.sleep(POLL)
        said = chromium.tail(self.directory / LOG) or f"it wrote no DevTools port within {START_TIMEOUT:g}s"
        raise RunFailed(f"Chromium {installed.version} did not start for the run: {said}", cause="environment")

    async def close(self, grace: float = STOP_GRACE) -> None:
        """Close Chromium so it ends writing, kill what is left of it, and remove the run's browser directory."""
        await self._unwatch()
        process, self.process = self.process, None
        try:
            if process is not None:
                tools = await asyncio.to_thread(descendants, process.pid)
                if self.cdp is not None and process.poll() is None:
                    await self._ask_to_close()
                    await self._ended(process, CLOSE_TIMEOUT)
                said = await orphans.stop({**self.noted, "pid": process.pid, "pgid": process.pid}, grace)
                await asyncio.to_thread(kill_tree, [(pid, group) for pid, group in tools if _alive(pid)])
                with contextlib.suppress(subprocess.TimeoutExpired):
                    await asyncio.to_thread(process.wait, grace)
                log.info("browser closed", extra={"run_id": self.run_id, "stop": said})
        finally:
            self.cdp = self.socket = None
            if self.daemon.browsers.get(self.run_id) is self:
                del self.daemon.browsers[self.run_id]
            await asyncio.to_thread(shutil.rmtree, self.directory, True)

    async def _ask_to_close(self) -> None:
        """Browser.close over the browser's DevTools socket; nothing when it does not answer in time."""
        url = self.cdp.replace("http://", "ws://", 1) + (self.socket or "")
        timeout = aiohttp.ClientTimeout(total=CLOSE_TIMEOUT)
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError, OSError):
            async with aiohttp.ClientSession(timeout=timeout) as session, session.ws_connect(url) as socket:
                await socket.send_json({"id": 1, "method": "Browser.close"})
                with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
                    await socket.receive(timeout=CLOSE_TIMEOUT)

    @staticmethod
    async def _ended(process: subprocess.Popen, timeout: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if process.poll() is not None:
                return True
            await asyncio.sleep(POLL)
        return False


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def sweep(home: WorkerHome, held=()) -> list[int]:
    """At the daemon's start: stop the Chromium a daemon before it left for a run it does not hold (``held``), and
    remove the run's browser directory. The runs swept."""
    swept = []
    for directory in sorted(home.runs_dir.glob(f"*/{DIRECTORY}")) if home.runs_dir.is_dir() else ():
        name = directory.parent.name
        if not name.isdigit() or int(name) in held:
            continue
        with contextlib.suppress(OSError, ValueError):
            noted = json.loads((directory / NOTED).read_text(encoding="utf-8"))
            if isinstance(noted, dict):
                said = await orphans.stop(noted, STOP_GRACE)
                log.info("browser left by a daemon before stopped", extra={"run_id": int(name), "stop": said})
        await asyncio.to_thread(shutil.rmtree, directory, True)
        swept.append(int(name))
    return swept

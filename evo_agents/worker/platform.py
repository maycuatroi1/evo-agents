"""What the worker does differently on the system it runs on, each system a class of its own (``of``).

- ``MacOS``: launchd keeps the daemon (``service.Launchd``, ``selfupdate.supervisor.launchd``), the daemon makes power
  assertions (``power.MacPower``), the keychain may hold Claude Code's login, and Playwright caches its browsers in
  ~/Library/Caches.
- ``Linux``: systemd keeps the daemon (``service.Systemd``, ``selfupdate.supervisor.systemd``), Chromium needs a display
  and may need libraries unpacked beside it, and the clock that counts through a sleep is CLOCK_BOOTTIME.
- ``Platform`` itself, any other system: no service manager and no power assertions this worker knows.

``of`` reads ``sys.platform`` each time it is called, so a test that sets it gets that system's class. The services,
supervisors and power assertions are imported only once a system's class is asked for them: this module imports
nothing of the worker.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from pathlib import Path


class Platform:
    """The system named ``name`` (a value of ``sys.platform``); see the module."""

    macos = False
    linux = False
    keychain = False  # the macOS keychain may keep Claude Code's login
    needs_display = False  # Chromium's window needs DISPLAY or WAYLAND_DISPLAY
    locale = "C.UTF-8"  # the UTF-8 locale of a terminal UI whose environment names none
    browser_system = "linux"  # the system Chrome's user agent names (``browser.chromium.PLATFORMS``)

    def __init__(self, name: str):
        self.name = name

    def manager(self, run=None, sleep=None, agent: str = ""):
        """The service manager that keeps the daemon of ``agent`` running (``service.Manager``)."""
        from evo_agents.worker.service import ServiceError

        raise ServiceError(
            f"evo-agents worker service needs launchd (macOS) or systemd (Linux), not {self.name}; run "
            "`evo-agents worker run` under a supervisor of your own instead"
        )

    def supervision(self, env: Mapping[str, str], ppid: int | None, cgroup: str | None, agents: Path | None, run):
        """The service manager of this process (``selfupdate.supervisor.Supervision``)."""
        from evo_agents.worker.selfupdate.supervisor import Supervision

        return Supervision(None, reason=f"no service manager this daemon knows on {self.name}")

    def clock_through_sleep(self) -> int | None:
        """The id of the clock that counts on while the machine sleeps; None for none."""
        return None

    def power(self):
        """The power assertions of this system (``power.Power``); PowerUnavailable where it has none."""
        from evo_agents.worker.power import PowerUnavailable

        raise PowerUnavailable(f"the daemon makes power assertions on macOS only, not on {self.name}")

    def playwright_cache(self, env: Mapping[str, str]) -> Path:
        """This user's cache directory of Playwright's browsers."""
        return Path(env.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "ms-playwright"


class MacOS(Platform):
    macos = True
    keychain = True
    locale = "en_US.UTF-8"
    browser_system = "darwin"

    def manager(self, run=None, sleep=None, agent: str = ""):
        from evo_agents.worker.service import Launchd

        return Launchd(run, sleep, agent)

    def supervision(self, env, ppid, cgroup, agents, run):
        import os

        from evo_agents.worker.selfupdate.supervisor import launchd

        return launchd(env, os.getppid() if ppid is None else ppid, agents)

    def clock_through_sleep(self) -> int | None:
        return getattr(time, "CLOCK_MONOTONIC", None)

    def power(self):
        from evo_agents.worker.power import MacPower

        return MacPower()

    def playwright_cache(self, env: Mapping[str, str]) -> Path:
        return Path("~/Library/Caches/ms-playwright").expanduser()


class Linux(Platform):
    linux = True
    needs_display = True

    def manager(self, run=None, sleep=None, agent: str = ""):
        from evo_agents.worker.service import Systemd

        return Systemd(run, sleep, agent)

    def supervision(self, env, ppid, cgroup, agents, run):
        import subprocess

        from evo_agents.worker.selfupdate.supervisor import read_cgroup, systemd

        return systemd(env, read_cgroup() if cgroup is None else cgroup, run or subprocess.run)

    def clock_through_sleep(self) -> int | None:
        return getattr(time, "CLOCK_BOOTTIME", None)


def of(name: str | None = None) -> Platform:
    """The system named ``name``, this one (``sys.platform``) by default."""
    name = sys.platform if name is None else name
    if name == "darwin":
        return MacOS(name)
    if name.startswith("linux"):
        return Linux(name)
    return Platform(name)

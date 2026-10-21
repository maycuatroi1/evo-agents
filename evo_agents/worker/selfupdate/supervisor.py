"""Whether a service manager runs the daemon, and starts it again once it exits with ``home.EXIT_UPDATED``.

- launchd: the job's process is a child of launchd (pid 1), and launchd sets XPC_SERVICE_NAME to its label (a
  terminal sets 0, an app application.<id>). Its plist in ~/Library/LaunchAgents keeps it alive: KeepAlive true, or
  {SuccessfulExit: false} as ``evo-agents worker service install`` writes it.
- systemd: the unit's process has INVOCATION_ID set and lives in the cgroup of ``<unit>.service``
  (/proc/self/cgroup); ``systemctl show`` of the unit (``--user`` for one of the user's manager) says Restart=on-failure
  or always, and RestartPreventExitStatus does not list EXIT_UPDATED.

Anything else is a daemon run by hand, or one its manager would leave stopped: it does not update itself, since the
exit that puts the new install to work would end it for good.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from evo_agents.worker.home import EXIT_UPDATED

BY_HAND = "not started by launchd or systemd: a daemon run by hand does not update itself"
RESTARTS = ("on-failure", "always")
CALL_TIMEOUT = 30


@dataclass(frozen=True)
class Supervision:
    manager: str | None  # launchd or systemd; None for a daemon run by hand
    name: str | None = None  # the job's label, or the unit
    reason: str | None = None  # why the daemon cannot count on being started again; None when it can


def detect(
    env: Mapping[str, str],
    *,
    platform: str | None = None,
    ppid: int | None = None,
    cgroup: str | None = None,
    agents: Path | None = None,
    run=None,
) -> Supervision:
    """The service manager of this process; ``platform``, ``ppid``, ``cgroup`` (the text of /proc/self/cgroup),
    ``agents`` (~/Library/LaunchAgents) and ``run`` (subprocess.run) are for tests."""
    platform = sys.platform if platform is None else platform
    if platform == "darwin":
        return launchd(env, os.getppid() if ppid is None else ppid, agents)
    if platform.startswith("linux"):
        return systemd(env, read_cgroup() if cgroup is None else cgroup, run or subprocess.run)
    return Supervision(None, reason=f"no service manager this daemon knows on {platform}")


def launchd(env: Mapping[str, str], ppid: int, agents: Path | None = None) -> Supervision:
    label = env.get("XPC_SERVICE_NAME") or ""
    if ppid != 1 or label in ("", "0") or label.startswith("application."):
        return Supervision(None, reason=BY_HAND)
    plist = (agents or Path.home() / "Library" / "LaunchAgents") / f"{label}.plist"
    try:
        with open(plist, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return Supervision("launchd", label, f"launchd runs it as {label}, but {plist} does not say it starts it again")
    keep = data.get("KeepAlive") if isinstance(data, dict) else None
    if keep is True or (isinstance(keep, dict) and keep.get("SuccessfulExit") is False):
        return Supervision("launchd", label)
    return Supervision("launchd", label, f"{plist} does not have launchd start the daemon again (KeepAlive)")


def read_cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except OSError:
        return ""


def unit_of(cgroup: str) -> tuple[str | None, bool]:
    """The service unit in the text of /proc/self/cgroup, and whether a user's manager runs it."""
    for line in cgroup.splitlines():
        _, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if controllers not in ("", "name=systemd"):
            continue
        last = path.rstrip("/").rsplit("/", 1)[-1]
        if last.endswith(".service"):
            return last, "/user@" in path
    return None, False


def systemd(env: Mapping[str, str], cgroup: str, run) -> Supervision:
    unit, user = unit_of(cgroup)
    if not env.get("INVOCATION_ID") or unit is None:
        return Supervision(None, reason=BY_HAND)
    argv = ["systemctl", *(["--user"] if user else []), "show", unit, "--property=Restart,RestartPreventExitStatus"]
    try:
        proc = run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT, env=dict(env))
    except (OSError, subprocess.SubprocessError) as exc:
        return Supervision("systemd", unit, f"{shlex.join(argv)} failed: {exc}")
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200] or "no output"
        return Supervision("systemd", unit, f"{shlex.join(argv)} exited {proc.returncode}: {detail}")
    facts = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition("=")
        facts[key.strip()] = value.strip()
    restart = facts.get("Restart") or "no"
    if restart not in RESTARTS:
        return Supervision("systemd", unit, f"{unit} has Restart={restart}: systemd would not start the daemon again")
    if str(EXIT_UPDATED) in facts.get("RestartPreventExitStatus", "").split():
        return Supervision("systemd", unit, f"{unit} lists {EXIT_UPDATED} in RestartPreventExitStatus")
    return Supervision("systemd", unit)

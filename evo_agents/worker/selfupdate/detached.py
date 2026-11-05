"""``live_runs``: the processes of runs that live on in the daemon's systemd service once it holds no run (Linux).

An agent starts a long job with ``setsid nohup`` and parks its run on a wait (``worker wait``): the daemon then holds
no run, yet systemd kills every process left in the unit's cgroup when the daemon exits (``KillMode=mixed``), so an
update then kills the job the parked run waits for. A process counts when its environment names a run (EVO_RUN_ID)
and it started less than MAX_AGE ago; an older one no longer holds the update back, so a leaked process cannot stop
updates for good. The cgroup is read from /proc/self/cgroup (the v1 ``name=systemd`` hierarchy, else the unified one)
and must be a ``.service``. Anywhere else the answer is empty: launchd leaves a job that left its process group alone,
and a daemon run by hand never updates itself.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

RUN_VARIABLE = b"EVO_RUN_ID="
MAX_AGE = 24 * 3600.0  # seconds; the longest timeout a run has
PROC = Path("/proc")
CGROUPS = Path("/sys/fs/cgroup")


def _service_cgroup(proc: Path, cgroups: Path) -> Path | None:
    try:
        lines = (proc / "self" / "cgroup").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    paths = {}
    for line in lines:
        hierarchy, _, rest = line.partition(":")
        controllers, _, path = rest.partition(":")
        if controllers == "name=systemd":
            paths["v1"] = cgroups / "systemd" / path.lstrip("/")
        elif hierarchy == "0" and controllers == "":
            paths["v2"] = cgroups / path.lstrip("/")
    found = paths.get("v1") or paths.get("v2")
    return found if found is not None and found.name.endswith(".service") else None


def service_processes(proc: Path = PROC, cgroups: Path = CGROUPS) -> list[int]:
    """The pids in this process's systemd service cgroup; [] when it lives in none."""
    cgroup = _service_cgroup(proc, cgroups)
    if cgroup is None:
        return []
    try:
        text = (cgroup / "cgroup.procs").read_text(encoding="utf-8")
    except OSError:
        return []
    return [int(word) for word in text.split() if word.isdigit()]


def _run_of(place: Path) -> int | None:
    try:
        environ = (place / "environ").read_bytes()
    except OSError:
        return None
    for entry in environ.split(b"\0"):
        if entry.startswith(RUN_VARIABLE):
            value = entry[len(RUN_VARIABLE) :]
            return int(value) if value.isdigit() else None
    return None


def _boot_time(proc: Path) -> float | None:
    try:
        for line in (proc / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _started(place: Path, boot: float | None, tick: int) -> float | None:
    """When the process started, in seconds of the epoch; None when /proc does not say."""
    if boot is None:
        return None
    try:
        fields = (place / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
        return boot + int(fields[19]) / tick  # starttime, field 22 of proc(5), in clock ticks since boot
    except (OSError, ValueError, IndexError):
        return None


def live_runs(
    proc: Path = PROC, cgroups: Path = CGROUPS, *, now: Callable[[], float] = time.time, me: int | None = None
) -> dict[int, int]:
    """Run id -> how many of its processes live in this daemon's service and started less than MAX_AGE ago."""
    pids = service_processes(proc, cgroups)
    if not pids:
        return {}
    me = os.getpid() if me is None else me
    boot, tick = _boot_time(proc), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    found: dict[int, int] = {}
    for pid in pids:
        if pid == me:
            continue
        place = proc / str(pid)
        run_id = _run_of(place)
        if run_id is None:
            continue
        started = _started(place, boot, tick)
        if started is not None and now() - started > MAX_AGE:
            continue
        found[run_id] = found.get(run_id, 0) + 1
    return found

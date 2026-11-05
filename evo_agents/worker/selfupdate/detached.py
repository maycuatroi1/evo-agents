"""``live_runs``: the processes of runs that live on in the daemon's systemd service once it holds no run (Linux);
``line_of``: where the line of such a run stands in the worker's records, and ``holds_back``: whether its processes
keep the daemon from updating; ``Scopes``: the transient scopes of the user's manager that take such processes out of
the service, so its exit no longer kills them.

An agent starts a long job with ``setsid nohup`` and parks its run on a wait (``worker wait``): the daemon then holds
no run, yet systemd kills every process left in the unit's cgroup when the daemon exits (``KillMode=mixed``), so an
update then kills the job the parked run waits for. A process counts when its environment names a run (EVO_RUN_ID),
or when it descends in the service from one that does and names none (the backends of a Postgres show no environment),
and it started less than MAX_AGE ago. The cgroup is read from /proc/self/cgroup (the v1 ``name=systemd`` hierarchy,
else the unified one) and must be a ``.service``. Anywhere else the answer is empty: launchd leaves a job that left
its process group alone, and a daemon run by hand never updates itself.

A run's line is the run and the attempts that went on from it in its session (``resumed_by`` of each record), which
the processes of its first attempt may serve. Its processes hold the update back while the line is parked here (for
MAX_AGE after its last attempt ended at most), has not ended here, or ended less than LINE_GRACE ago; not once it
ended before that, nor when the worker has no record of the run. An idle daemon of a user manager's ``.service``
moves the processes of a parked line into ``evo-agents-run-<run>-<pid>.scope`` in the service's own slice
(StartTransientUnit with PIDs, through ``busctl --user``), and stops such a scope once its line no longer needs it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

RUN_VARIABLE = b"EVO_RUN_ID="
MAX_AGE = 24 * 3600.0  # seconds; the longest timeout a run has
LINE_GRACE = 3600.0  # seconds after its line ended here that the processes of a run still hold the update back
PROC = Path("/proc")
CGROUPS = Path("/sys/fs/cgroup")
USER_MANAGER = re.compile(r"/user@\d+\.service/")
SCOPE = re.compile(r"evo-agents-run-(\d+)-(\d+)\.scope")
MOST_ATTEMPTS = 1000  # of a line, followed through resumed_by
CALL_TIMEOUT = 30.0  # seconds of a busctl call


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


def _stat(place: Path) -> list[str]:
    """The fields of /proc/<pid>/stat after the command's name, from the state on; [] when /proc does not say."""
    try:
        return (place / "stat").read_text(encoding="utf-8").rpartition(")")[2].split()
    except OSError:
        return []


def _started(fields: list[str], boot: float | None, tick: int) -> float | None:
    """When the process started, in seconds of the epoch; None when /proc does not say."""
    if boot is None:
        return None
    try:
        return boot + int(fields[19]) / tick  # starttime, field 22 of proc(5), in clock ticks since boot
    except (ValueError, IndexError):
        return None


def _parent(fields: list[str]) -> int | None:
    try:
        return int(fields[1])  # ppid, field 4 of proc(5)
    except (ValueError, IndexError):
        return None


def live_runs(
    proc: Path = PROC, cgroups: Path = CGROUPS, *, now: Callable[[], float] = time.time, me: int | None = None
) -> dict[int, list[int]]:
    """Run id -> the pids of its processes that live in this daemon's service and started less than MAX_AGE ago: those
    whose environment names the run, and those that name none and descend in the service from one of them."""
    pids = service_processes(proc, cgroups)
    if not pids:
        return {}
    me = os.getpid() if me is None else me
    boot, tick = _boot_time(proc), os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    named: dict[int, int | None] = {}
    parents: dict[int, int | None] = {}
    young = []
    for pid in pids:
        if pid == me:
            continue
        place = proc / str(pid)
        fields = _stat(place)
        named[pid], parents[pid] = _run_of(place), _parent(fields)
        started = _started(fields, boot, tick)
        if started is None or now() - started <= MAX_AGE:
            young.append(pid)
    found: dict[int, list[int]] = {}
    for pid in young:
        run_id = _named_above(pid, named, parents)
        if run_id is not None:
            found.setdefault(run_id, []).append(pid)
    return {run_id: sorted(found[run_id]) for run_id in sorted(found)}


def _named_above(pid: int, named: Mapping[int, int | None], parents: Mapping[int, int | None]) -> int | None:
    """The run the process names, else the one its nearest ancestor in the service names; None when none does."""
    seen: set[int] = set()
    current: int | None = pid
    while current is not None and current in named and current not in seen:
        run_id = named[current]
        if run_id is not None:
            return run_id
        seen.add(current)
        current = parents[current]
    return None


@dataclass(frozen=True)
class Line:
    """Where the line of a run stands in this worker's records. ``state``: parked (its last attempt here is parked),
    ended (it ended here and nothing went on from it), open (its last attempt has not ended here, or a run this worker
    has no record of went on from it) or unknown (no record of the run here). ``since``: when its last attempt ended,
    in seconds of the epoch, for parked and ended."""

    state: str
    since: float | None = None


def _epoch(text) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp()


def line_of(run_id: int, load_run: Callable[[int], dict | None]) -> Line:
    """The line of ``run_id`` as the records ``load_run`` reads (``WorkerHome.load_run``) say it."""
    record = load_run(run_id)
    if record is None:
        return Line("unknown")
    for _ in range(MOST_ATTEMPTS):
        after = record.get("resumed_by")
        if not isinstance(after, int) or isinstance(after, bool):
            break
        following = load_run(after)
        if following is None:
            return Line("open")
        record = following
    since = _epoch(record.get("finished_at"))
    if since is None:
        return Line("open")
    return Line("parked" if str(record.get("state") or "").startswith("parked") else "ended", since)


def holds_back(line: Line, now: float) -> bool:
    """Whether processes of a run whose line is ``line`` keep the daemon from updating, at ``now``: its exit would kill
    them while a run of the line may still need them."""
    if line.state == "open":
        return True
    if line.since is None:
        return False
    if line.state == "parked":
        return now - line.since < MAX_AGE
    return line.state == "ended" and now - line.since < LINE_GRACE


class ScopeError(Exception):
    """The user manager refused a call, or busctl could not make it."""


class Scopes:
    """The scopes of runs (``evo-agents-run-<run>-<pid>.scope``) in the slice of this daemon's service, which its user
    manager makes and stops on calls of ``busctl --user``: ``move`` takes processes out of the service into a new one,
    ``found`` lists them, ``stop`` ends one with its processes."""

    def __init__(self, busctl: str, slice_dir: Path, *, env: Mapping[str, str] | None = None, run=subprocess.run):
        self.busctl, self.slice_dir, self.env, self.run = busctl, slice_dir, env, run

    @classmethod
    def here(
        cls, env: Mapping[str, str], proc: Path = PROC, cgroups: Path = CGROUPS, *, run=subprocess.run
    ) -> Scopes | None:
        """Those of this process's service: None outside a ``.service`` of a user manager in a slice, or without
        busctl on the service's PATH."""
        cgroup = _service_cgroup(proc, cgroups)
        if cgroup is None or not cgroup.parent.name.endswith(".slice") or not USER_MANAGER.search(f"{cgroup}/"):
            return None
        busctl = shutil.which("busctl", path=env.get("PATH"))
        return cls(busctl, cgroup.parent, env=env, run=run) if busctl else None

    def _call(self, method: str, signature: str, *args: str) -> None:
        argv = [
            self.busctl,
            "--user",
            "call",
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1",
            "org.freedesktop.systemd1.Manager",
            method,
            signature,
            *args,
        ]
        env = dict(self.env) if self.env is not None else None
        try:
            done = self.run(argv, capture_output=True, text=True, timeout=CALL_TIMEOUT, env=env, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ScopeError(f"busctl {method} failed: {exc}") from exc
        if done.returncode:
            detail = (done.stderr or done.stdout or "").strip()[:200]
            raise ScopeError(f"busctl {method} exited {done.returncode}: {detail}")

    def move(self, run_id: int, pids: list[int]) -> str:
        """Move ``pids`` into a new scope of run ``run_id``; its name."""
        name = f"evo-agents-run-{int(run_id)}-{min(pids)}.scope"
        about = f"processes of evo-agents run {int(run_id)}, out of the worker's service"
        properties = ["PIDs", "au", str(len(pids)), *map(str, pids), "Slice", "s", self.slice_dir.name]
        self._call(
            "StartTransientUnit", "ssa(sv)a(sa(sv))", name, "fail", "3", *properties, "Description", "s", about, "0"
        )
        return name

    def found(self) -> dict[str, int]:
        """Scope -> its run, for the scopes of runs in the slice."""
        try:
            names = sorted(path.name for path in self.slice_dir.iterdir())
        except OSError:
            return {}
        return {name: int(match.group(1)) for name in names if (match := SCOPE.fullmatch(name))}

    def stop(self, name: str) -> None:
        self._call("StopUnit", "ss", name, "replace")

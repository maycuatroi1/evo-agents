"""The memory a worker's machine has left for one more run, read from cgroup v2 and /proc (``Claims._short_of_memory``).

Several daemons can share a box and one memory limit: on Linux each runs in a cgroup whose ancestors may hold a
``memory.high`` or ``memory.max`` (a systemd slice with ``MemoryHigh=8G``, say). Their slots add up to more agents
than the limit fits, and a run claimed into a cgroup that sits at its limit stalls: Claude Code does not start within
the 60 seconds its SDK gives it, git hangs, and the lease runs out. Slots alone cannot follow a box whose load swings
with what each run's tests take, so before each claim the daemon reads:

- the room of its cgroup chain: for each ancestor with a limit, the lower of ``memory.high`` and ``memory.max`` less
  ``memory.current``, the least of them; and the machine's ``MemAvailable``; the room is the least of all these;
- the memory stall: ``full avg60`` of ``memory.pressure`` of the cgroup whose room is least, else of the machine
  (``/proc/pressure/memory``), the share of the last minute every task there waited on memory.

It holds back (claims nothing) while the stall is at least ``memory_stall_max`` percent, or while the room is under
``memory_floor_mb`` and the stall is at least ``ROOM_STALL_MIN``: page cache counts in ``memory.current`` and is given
back on demand, so a cgroup near its limit that does not stall still takes runs. 0 turns either test off;
``$EVO_WORKER_MEMORY_FLOOR_MB`` and ``$EVO_WORKER_MEMORY_STALL_MAX`` win over the config. A machine without cgroup v2
or /proc (macOS) reads nothing and holds nothing back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC = Path("/proc")
MEMORY_FLOOR_MB = 1024  # WorkerConfig.memory_floor_mb by default
MEMORY_STALL_MAX = 10.0  # WorkerConfig.memory_stall_max by default: percent of full avg60
ROOM_STALL_MIN = 1.0  # under the floor, a stall of at least this percent holds back; below it the room is page cache
MB = 1024 * 1024
FLOOR_VARIABLE = "EVO_WORKER_MEMORY_FLOOR_MB"  # wins over WorkerConfig.memory_floor_mb, as the disk reserve's does
STALL_VARIABLE = "EVO_WORKER_MEMORY_STALL_MAX"  # wins over WorkerConfig.memory_stall_max


@dataclass(frozen=True)
class Room:
    """What the daemon read: the room in MB (None when nothing gave one), the stall in percent (None when no
    memory.pressure was readable), and where the least room is (a cgroup path, ``machine``, or None)."""

    room_mb: float | None
    stall: float | None
    where: str | None


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def own_cgroup(proc: Path = PROC) -> str | None:
    """The cgroup v2 path of this process (``0::/user.slice/...``), None without cgroup v2."""
    for line in (_read(proc / "self" / "cgroup") or "").splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or "/"
    return None


def _number(text: str | None) -> int | None:
    value = (text or "").strip()
    return int(value) if value.isdigit() else None  # "max", empty or unreadable: no limit


def _limit(directory: Path) -> int | None:
    found = [n for n in (_number(_read(directory / "memory.high")), _number(_read(directory / "memory.max"))) if n]
    return min(found) if found else None


def stall(text: str | None) -> float | None:
    """``full avg60`` of a ``memory.pressure`` text, in percent."""
    for line in (text or "").splitlines():
        if line.startswith("full "):
            for field in line.split()[1:]:
                key, _, value = field.partition("=")
                if key == "avg60":
                    try:
                        return float(value)
                    except ValueError:
                        return None
    return None


def available_mb(proc: Path = PROC) -> float | None:
    for line in (_read(proc / "meminfo") or "").splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            return int(parts[1]) / 1024 if len(parts) > 1 and parts[1].isdigit() else None
    return None


def read_room(cgroup_root: Path = CGROUP_ROOT, proc: Path = PROC) -> Room:
    """The room and the stall as this process sees them now (module docstring)."""
    least: tuple[float, Path] | None = None
    path = own_cgroup(proc)
    if path is not None:
        directory = cgroup_root / path.lstrip("/")
        while True:
            limit, current = _limit(directory), _number(_read(directory / "memory.current"))
            if limit is not None and current is not None:
                room = (limit - current) / MB
                if least is None or room < least[0]:
                    least = (room, directory)
            if directory == cgroup_root or cgroup_root not in directory.parents:
                break
            directory = directory.parent
    machine = available_mb(proc)
    if least is not None and (machine is None or least[0] <= machine):
        where = "/" + str(least[1].relative_to(cgroup_root)) if least[1] != cgroup_root else "/"
        return Room(least[0], stall(_read(least[1] / "memory.pressure")), where)
    return Room(machine, stall(_read(proc / "pressure" / "memory")), "machine" if machine is not None else None)


def limits(floor_mb: float, stall_max: float, env) -> tuple[float, float]:
    """The floor and the stall limit: ``FLOOR_VARIABLE`` and ``STALL_VARIABLE`` of ``env`` when they hold a number of
    at least 0, else the config's."""

    def pick(name: str, configured: float) -> float:
        try:
            value = float(env.get(name, ""))
        except ValueError:
            return configured
        return value if value >= 0 else configured

    return pick(FLOOR_VARIABLE, floor_mb), pick(STALL_VARIABLE, stall_max)


def short_of(room: Room, floor_mb: float, stall_max: float) -> str | None:
    """Why the daemon must claim nothing now, or None (module docstring)."""
    if stall_max and room.stall is not None and room.stall >= stall_max:
        return f"memory stalls {room.stall:.0f}% of the last minute in {room.where} (at most {stall_max:g}%)"
    if floor_mb and room.room_mb is not None and room.room_mb < floor_mb:
        if room.stall is None or room.stall >= ROOM_STALL_MIN:
            return f"{max(room.room_mb, 0):.0f} MB left in {room.where} (at least {floor_mb:g} MB)"
    return None

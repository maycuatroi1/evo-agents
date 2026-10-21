"""One running backend of an isolated plugin, as the plugin host and ``evo-agents plugin test`` drive it over plugin/1.

``BackendProcess`` starts the command ``evo_agents.plugins.environment`` builds, with exactly the environment given
(nothing inherited), in a session and process group of its own. Its code runs with no file descriptor besides its
three pipes: the trampoline at the head of that command closes every other one before anything else, since a loop
may ignore ``close_fds`` (uvloop 0.23 does), and when the backend has a user of its own (``owner``), it takes it.
Neither the start nor ``kill_user`` passes an argument the event loop's subprocess_exec may lack (uvloop takes no
``user``, ``group`` or ``extra_groups``), so both work on any loop the host runs. ``call`` writes one request and
waits for its response at most
``timeout`` seconds: ``BackendTimeout`` when none comes, ``BackendGone`` when the process ends or closes its stdout
first, ``protocol.RemoteError`` for the error it answers. A line on stdout that is no plugin/1 response is kept in
``violations``; the last lines of stderr in ``stderr_tail``, each cut at STDERR_LINE characters. ``stop`` asks the
backend to shut down, closes its stdin, then sends SIGTERM and SIGKILL to its whole process group; when the backend
has a user of its own, it then kills every process of that user (``kill_user``), so a child that left the group with
setsid() goes too. The host does that itself, as root: it stops every process of the user, then kills them all
(``sweep_user``), and no process of the plugin may signal, renice or pin the host. A killer under a user of its own
then goes after what the sweep could not catch; it is killed after KILLER_WAIT, and ``kill_user`` gives up after
KILL_DEADLINE in all, so a stop never waits on it for long. ``reap_zombies`` reaps what a backend made the host's child
with clone(CLONE_PARENT), which the event loop never waits for.

Standard library only (asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import signal
import subprocess
import sys
import time
import weakref
from collections import deque
from pathlib import Path

from evo_agents.plugins import protocol
from evo_agents.plugins.environment import CLOSE_FDS, TAKE_USER, Identity, killer_uid

STDERR_LINES = 40
STDERR_LINE = 500  # characters kept of one line of stderr
MAX_VIOLATIONS = 20
STOP_GRACE = 5.0  # seconds a backend has to exit after shutdown
KILL_WAIT = 2.0  # seconds after SIGTERM before SIGKILL
KILL_ROUNDS = 10  # kill(-1) rounds before kill_user gives up on a user that keeps forking
KILLER_WAIT = 10.0  # seconds one killer may take, its Python's start included, on a CPU the plugin keeps busy
KILL_DEADLINE = 30.0  # seconds kill_user goes on in all; its callers try again later, so one stop never waits long
FREEZE_PASSES = 200  # passes of sweep_user over /proc before it kills what it holds, all stopped or not
FREEZE_PAUSE = 0.002  # seconds between two passes
SETTLE = 1.0  # seconds the processes a sweep killed have to end before a killer goes after what is left
HELD_STATES = ("T", "t")  # stopped, or stopped by a tracer: runs no code of the plugin until let go
AWAKE_STATES = ("R", "S")  # running, or asleep where a signal wakes it; a pass that finds none ends the stopping
KILLER = (  # real and saved user the killer's own, effective user the plugin's, then kill(-1); never as root
    "import os, sys\n" + CLOSE_FDS + TAKE_USER + "close_fds()\n"
    "killer, uid, sig = (int(arg) for arg in sys.argv[1:4])\n"
    "if killer == uid:\n"
    "    refuse('the killer needs a real user of its own')\n"
    "take_user(killer, uid, killer, killer)\n"
    "try:\n"
    "    os.kill(-1, sig)\n"
    "except ProcessLookupError:\n"  # no process but itself and pid 1: nothing to kill
    "    pass\n"
)
PROC = Path("/proc")

log = logging.getLogger(__name__)

_STARTED: weakref.WeakSet[BackendProcess] = weakref.WeakSet()  # the backends started here, which the loop waits for


class BackendGone(RuntimeError):
    """The backend ended, or closed its stdout, before it answered."""


class BackendTimeout(TimeoutError):
    """The backend did not answer within the time given."""


class BackendProcess:
    """One backend process and the requests waiting for its answers."""

    def __init__(self, command: list[str], env: dict[str, str], cwd: Path, owner: Identity | None = None):
        self.command, self.env, self.cwd, self.owner = command, env, cwd, owner
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_LINES)
        self.violations: list[str] = []
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._readers: list[asyncio.Task] = []
        self._ended = asyncio.Event()

    @property
    def pid(self) -> int | None:
        return None if self.proc is None else self.proc.pid

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.returncode

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and not self._ended.is_set()

    async def start(self) -> None:
        """Start the process; OSError when it cannot."""
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            cwd=str(self.cwd),
            start_new_session=True,
            close_fds=True,
            limit=protocol.MAX_LINE,
        )
        _STARTED.add(self)
        self._readers = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]

    def _violation(self, text: str) -> None:
        if len(self.violations) < MAX_VIOLATIONS:
            self.violations.append(text[:300])

    def _deliver(self, line: bytes) -> None:
        try:
            message = protocol.parse_line(line)
        except protocol.ProtocolError as exc:
            self._violation(f"{exc}: {line[:120]!r}")
            return
        waiting = self._pending.pop(message["id"], None) if isinstance(message["id"], int) else None
        if waiting is not None and not waiting.done():
            waiting.set_result(message)

    async def _read_stdout(self) -> None:
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except ValueError:  # a line over MAX_LINE: the stream cannot be read further
                    self._violation(f"a line over {protocol.MAX_LINE} bytes")
                    break
                if not line:
                    break
                if line.strip():
                    self._deliver(line)
        finally:
            self._ended.set()
            for waiting in self._pending.values():
                if not waiting.done():
                    waiting.set_exception(BackendGone("the backend closed its stdout"))
            self._pending.clear()

    async def _read_stderr(self) -> None:
        buffered = b""
        while chunk := await self.proc.stderr.read(65536):
            buffered += chunk
            *lines, buffered = buffered.split(b"\n")
            for line in lines:
                self.stderr_tail.append(line.decode("utf-8", "replace")[:STDERR_LINE])
            buffered = buffered[-STDERR_LINE * 4 :]
        if buffered:
            self.stderr_tail.append(buffered.decode("utf-8", "replace")[:STDERR_LINE])

    async def call(self, method: str, params: dict, timeout: float):
        """The result of ``method`` with ``params``; BackendTimeout, BackendGone or protocol.RemoteError."""
        if not self.alive:
            raise BackendGone("the backend is not running")
        request_id = next(self._ids)
        waiting = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiting
        try:
            async with asyncio.timeout(timeout):
                self.proc.stdin.write(protocol.request(request_id, method, params))
                await self.proc.stdin.drain()
                message = await waiting
        except TimeoutError:
            raise BackendTimeout(f"no answer to {method} within {timeout:g}s") from None
        except (BrokenPipeError, ConnectionResetError):
            raise BackendGone("the backend closed its stdin") from None
        finally:
            self._pending.pop(request_id, None)
        return protocol.outcome(message)

    async def write_raw(self, data: bytes) -> None:
        """Write bytes as they are, for the checks of ``evo-agents plugin test``."""
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    def _signal_group(self, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            try:
                os.killpg(self.proc.pid, sig)
            except PermissionError:
                self.proc.send_signal(sig)

    async def _wait(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self.proc.wait(), seconds)
        except TimeoutError:
            return False
        return True

    async def stop(self, grace: float = STOP_GRACE) -> int | None:
        """Shut the backend down: shutdown, then its stdin closed, then SIGTERM and SIGKILL to its process group."""
        if self.proc is None:
            return None
        if self.alive:
            with contextlib.suppress(BackendGone, BackendTimeout, protocol.RemoteError):
                await self.call("shutdown", {}, timeout=grace / 2)
        with contextlib.suppress(OSError, RuntimeError):
            self.proc.stdin.close()
        if not await self._wait(grace / 2):
            self._signal_group(signal.SIGTERM)
            if not await self._wait(KILL_WAIT):
                self._signal_group(signal.SIGKILL)
                await self.proc.wait()
        else:  # the process is gone; what it started in its group goes with it
            self._signal_group(signal.SIGKILL)
        if self.owner is not None:  # and what left the group, under the plugin's user
            await kill_user(self.owner)
        for task in self._readers:
            task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        return self.proc.returncode


def _parse_status(text: str) -> dict[str, str]:
    return dict(line.split(":", 1) for line in text.splitlines() if ":" in line)


def _status(pid: Path) -> dict[str, str]:
    try:
        text = (pid / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return _parse_status(text)


def _status_at(directory: int) -> dict[str, str]:
    """/proc/<pid>/status read through a descriptor of /proc/<pid>, so it is that process's even once its pid is free
    again; empty when the process is gone."""
    try:
        descriptor = os.open("status", os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    except OSError:
        return {}
    try:
        with os.fdopen(descriptor, "rb") as status:
            return _parse_status(status.read().decode("utf-8", "replace"))
    except OSError:
        return {}


def _state(status: dict[str, str]) -> str:
    return status.get("State", "").strip()[:1]


def _belongs(status: dict[str, str], uid: int) -> bool:
    """Whether the process's real or saved user is ``uid``: those are the processes of the plugin, the ones the kernel
    lets a process of that user signal and counts in its RLIMIT_NPROC. A killer has the plugin's user as its effective
    one only, and is not one of them."""
    ids = status.get("Uid", "").split()
    return len(ids) >= 3 and str(uid) in (ids[0], ids[2])


def _alive(status: dict[str, str]) -> bool:
    """Whether the process may still run code: not a zombie, unless its first thread exited while others of it run
    (its status then says Z, and Threads counts them)."""
    if not status:
        return False
    if _state(status) not in ("Z", "X"):
        return True
    with contextlib.suppress(ValueError):
        return int(status.get("Threads", "0").strip()) > 1
    return False


def _pids() -> list[int]:
    try:
        return [int(name) for name in os.listdir(PROC) if name.isdigit()]
    except OSError:
        return []


def processes_of(uid: int) -> list[int]:
    """The live processes whose real or saved user is ``uid``, from /proc (Linux); zombies left out, as they hold
    nothing. /proc/<pid>/status is readable even for a process that is not dumpable."""
    if not PROC.is_dir():
        return []
    found = []
    for pid in _pids():
        status = _status(PROC / str(pid))
        if _belongs(status, uid) and _alive(status):
            found.append(pid)
    return found


def _open_proc(pid: int) -> int | None:
    try:
        return os.open(PROC / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None


def _send(directory: int, pid: int, sig: int) -> bool:
    """Send ``sig`` to the process ``directory`` (its /proc directory, a pidfd for the kernel) holds; False when it is
    gone or may not be signalled. A pid freed and taken again by another process never gets the signal. Without
    pidfd_send_signal (an older kernel, or a seccomp profile that refuses it), the pid, once its status still reads."""
    try:
        signal.pidfd_send_signal(directory, sig)
        return True
    except ProcessLookupError:
        return False
    except (AttributeError, OSError):
        pass
    if not _status_at(directory):
        return False
    try:
        os.kill(pid, sig)
    except OSError:
        return False
    return True


def sweep_user(uid: int) -> int:
    """Stop, then kill, every process whose real or saved user is ``uid``, as root (CAP_KILL); how many it signalled.

    Each pass reads /proc and sends SIGSTOP to every process of ``uid`` not stopped yet, through a descriptor of its
    /proc directory taken before its status is read, so the status and the signal are of the same process. Passes go on
    until one finds no process of that user running or asleep (at most FREEZE_PASSES): a stopped process forks
    nothing, signals nothing and changes no one's scheduling, and one that a sibling woke (SIGCONT) is stopped again
    at the next pass. Then each gets SIGKILL, which nothing undoes. Nothing of the plugin can delay this: the host's
    user is root, which no process of the plugin may signal, trace, renice or move to another CPU, and SIGSTOP and
    SIGKILL can be neither caught nor blocked. It needs no new process, so a service at its pids limit still kills."""
    if uid <= 0 or not PROC.is_dir():
        return 0
    held: dict[int, int] = {}  # pid -> descriptor of /proc/<pid>
    try:
        for _ in range(FREEZE_PASSES):
            awake = 0
            for pid in _pids():
                directory = held.get(pid)
                fresh = directory is None
                if fresh and (directory := _open_proc(pid)) is None:
                    continue
                status = _status_at(directory)
                if not (_belongs(status, uid) and _alive(status)):
                    os.close(directory)
                    held.pop(pid, None)
                    awake += not fresh  # a process held before is gone: its pid may hold a new one next pass
                    continue
                held[pid] = directory
                if _state(status) not in HELD_STATES and _send(directory, pid, signal.SIGSTOP):
                    awake += _state(status) in AWAKE_STATES
            if not awake:
                break
            time.sleep(FREEZE_PAUSE)  # for the processes just stopped to reach their stop
        for pid, directory in held.items():
            _send(directory, pid, signal.SIGKILL)
        return len(held)
    finally:
        for directory in held.values():
            os.close(directory)


async def _off_loop(work, *args):
    """``work`` in a thread of the loop's executor, or here when no thread can start (a service at its pids limit)."""
    loop = asyncio.get_running_loop()
    try:
        running = loop.run_in_executor(None, work, *args)
    except RuntimeError:
        return work(*args)
    return await running


async def _gone(uid: int, seconds: float) -> bool:
    """Whether /proc shows no process of ``uid`` within ``seconds``: the processes just killed take a moment to end."""
    deadline = time.monotonic() + seconds
    while processes_of(uid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def kill_user(owner: Identity) -> bool:
    """SIGKILL every process of ``owner``'s user, again until /proc shows none; True when none is left.

    Each round, the host itself, as root, stops every process of that user and then kills them (``sweep_user``), so
    neither a signal nor a change of scheduling from the plugin can delay it. What the sweep cannot catch is a chain of
    processes each of which forks the next and exits at once, faster than a pass reads /proc; for that, a killer then
    calls kill(-1), which reaches every process its user may signal at once, so a fork under way fails rather than
    leave a child behind. The killer is the host's Python, isolated, so nothing of the plugin runs in it, and it takes
    its user itself (KILLER), like the trampoline of a backend: its effective user is the plugin's, so it may signal
    every process of the plugin, while its real and saved user is one of its own (``killer_uid``), so no process of the
    plugin may signal or trace it. It kills nothing unless it holds exactly those IDs and no capability. A killer that
    has not ended after KILLER_WAIT is SIGKILLed and its round fails. The rounds stop at KILL_DEADLINE, each killer's
    wait cut to what is left of it, so a process that escapes every round holds a stop or a start about KILL_DEADLINE
    and one KILL_WAIT, where KILL_ROUNDS rounds of SETTLE, KILLER_WAIT and KILL_WAIT took over two minutes; the caller
    tries again on its own schedule (``Host.retire``, a start's backoff).
    The host needs CAP_SETUID, CAP_SETGID and CAP_KILL, which it has when it gives backends users of their own."""
    killer, deadline = killer_uid(owner), time.monotonic() + KILL_DEADLINE
    for _ in range(KILL_ROUNDS):
        if PROC.is_dir() and not processes_of(owner.uid):
            return True
        await _off_loop(sweep_user, owner.uid)
        if not PROC.is_dir():  # nothing to look at (not Linux): the killer's word is all there is
            return killer is not None and await _kill_round(killer, owner, KILLER_WAIT) == 0
        if await _gone(owner.uid, min(SETTLE, deadline - time.monotonic())):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            break
        if killer is None:
            log.error("no killer user for this plugin user", extra={"uid": owner.uid})
        else:
            await _kill_round(killer, owner, min(KILLER_WAIT, left))
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.05)
    return not processes_of(owner.uid)


async def _kill_round(real: int, owner: Identity, wait: float) -> int | None:
    """One killer, waited for at most ``wait`` seconds: its exit code, None when it could not start or did not end."""
    try:
        killer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            "-c",
            KILLER,
            str(real),
            str(owner.uid),
            str(int(signal.SIGKILL)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={},
            cwd="/",
            close_fds=True,
        )
    except OSError as exc:
        log.warning("cannot start the killer of a plugin user", extra={"uid": owner.uid, "error": str(exc)})
        return None
    try:
        _, said = await asyncio.wait_for(killer.communicate(), wait)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            killer.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(killer.wait(), KILL_WAIT)
        log.warning("the killer of a plugin user did not end in time: killed it", extra={"uid": owner.uid})
        return None
    if killer.returncode != 0:
        why = said.decode("utf-8", "replace").strip()[-STDERR_LINE:]
        log.warning(
            "the killer of a plugin user failed",
            extra={"uid": owner.uid, "exit": describe_exit(killer.returncode), "error": why},
        )
    return killer.returncode


def reap_zombies(uid: int) -> list[int]:
    """Reap the zombies whose parent is this process and whose real or saved user is ``uid``; their pids.

    A backend may make a child with clone(CLONE_PARENT), whose parent is then the backend's own: the host. The event
    loop waits only for the processes it started, so once such a child ends (``kill_user`` kills it), nothing reaps it:
    it holds its pid, and one of the processes RLIMIT_NPROC lets the plugin's user have, for as long as the host runs.
    A backend this module started and the loop still waits for is left to the loop. Call it only when nothing else the
    host started under ``uid`` runs: pip, which ``subprocess.run`` waits for, would read as having exited 0."""
    if uid <= 0 or not PROC.is_dir():
        return []
    me = str(os.getpid())
    waited = {backend.pid for backend in list(_STARTED) if backend.pid is not None and backend.returncode is None}
    reaped = []
    for pid in _pids():
        if pid in waited:
            continue
        status = _status(PROC / str(pid))
        if _state(status) != "Z" or status.get("PPid", "").strip() != me or not _belongs(status, uid):
            continue
        with contextlib.suppress(ChildProcessError):  # not this process's child after all
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                reaped.append(pid)
    return reaped


def describe_exit(code: int | None) -> str:
    """How a process ended, for a person: its exit code, or the signal that ended it."""
    if code is None:
        return "still running"
    if code < 0:
        with contextlib.suppress(ValueError):
            return f"killed by {signal.Signals(-code).name}"
        return f"killed by signal {-code}"
    return f"exit {code}"


def compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:300]

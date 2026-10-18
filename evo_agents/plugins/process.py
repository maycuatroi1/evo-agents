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
setsid() goes too; no process of that user may signal the killer, and a killer that hangs anyway is killed after
KILLER_WAIT, so a stop never waits on it for long.

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


def _status(pid: Path) -> dict[str, str]:
    try:
        text = (pid / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return dict(line.split(":", 1) for line in text.splitlines() if ":" in line)


def processes_of(uid: int) -> list[int]:
    """The live processes whose real, effective or saved user is ``uid``, from /proc (Linux); zombies left out, as
    they hold nothing. /proc/<pid>/status is readable even for a process that is not dumpable."""
    if not PROC.is_dir():
        return []
    found = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        status = _status(entry)
        uids = status.get("Uid", "").split()[:3]
        if str(uid) in uids and not status.get("State", "").strip().startswith(("Z", "X")):
            found.append(int(entry.name))
    return found


async def kill_user(owner: Identity) -> bool:
    """SIGKILL every process of ``owner``'s user, again until /proc shows none; True when none is left.

    Each round, a killer calls kill(-1), which reaches every process its user may signal, at once: a fork under way
    fails rather than leave a child behind. The killer is the host's Python, isolated, so nothing of the plugin runs in
    it, and it takes its user itself (KILLER), like the trampoline of a backend: its effective user is the plugin's, so
    it may signal every process of the plugin, while its real and saved user is one of its own (``killer_uid``), so no
    process of the plugin may signal it (the kernel asks that the sender's user be the target's real or saved one) or
    trace it, and none can stop it before it kills. It kills nothing unless it holds exactly those IDs and no
    capability. A killer that has not ended after KILLER_WAIT is SIGKILLed and its round fails, so a stop or a start
    never waits on one for long. The host needs CAP_SETUID, CAP_SETGID and CAP_KILL, which it has when it gives
    backends users of their own."""
    real = killer_uid(owner)
    if real is None:
        log.error("no killer user for this plugin user", extra={"uid": owner.uid})
        return not processes_of(owner.uid) if PROC.is_dir() else False
    for _ in range(KILL_ROUNDS):
        if PROC.is_dir() and not processes_of(owner.uid):
            return True
        code = await _kill_round(real, owner)
        if not PROC.is_dir():  # nothing to look at again (not Linux): the round's word is all there is
            return code == 0
        await asyncio.sleep(0.05)
    return not processes_of(owner.uid)


async def _kill_round(real: int, owner: Identity) -> int | None:
    """One killer, waited for at most KILLER_WAIT: its exit code, None when it could not start or did not end."""
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
        _, said = await asyncio.wait_for(killer.communicate(), KILLER_WAIT)
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

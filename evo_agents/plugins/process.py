"""One running backend of an isolated plugin, as the plugin host and ``evo-agents plugin test`` drive it over plugin/1.

``BackendProcess`` starts the command ``evo_agents.plugins.environment`` builds, with exactly the environment given
(nothing inherited), in a session and process group of its own, under the user ``identity`` names when there is
one, with no file descriptor besides its three pipes. ``call`` writes one request and waits for its response at most
``timeout`` seconds: ``BackendTimeout`` when none comes, ``BackendGone`` when the process ends or closes its stdout
first, ``protocol.RemoteError`` for the error it answers. A line on stdout that is no plugin/1 response is kept in
``violations``; the last lines of stderr in ``stderr_tail``, each cut at STDERR_LINE characters. ``stop`` asks the
backend to shut down, closes its stdin, then sends SIGTERM and SIGKILL to its whole process group.

Standard library only (asyncio).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import signal
from collections import deque
from pathlib import Path

from evo_agents.plugins import protocol
from evo_agents.plugins.environment import Identity

STDERR_LINES = 40
STDERR_LINE = 500  # characters kept of one line of stderr
MAX_VIOLATIONS = 20
STOP_GRACE = 5.0  # seconds a backend has to exit after shutdown
KILL_WAIT = 2.0  # seconds after SIGTERM before SIGKILL


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
        extra = {}
        if self.owner is not None:
            extra = {"user": self.owner.uid, "group": self.owner.gid, "extra_groups": []}
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
            **extra,
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
        for task in self._readers:
            task.cancel()
        await asyncio.gather(*self._readers, return_exceptions=True)
        return self.proc.returncode


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

"""``tmux attach`` of a run's session on a PTY of the worker's own, for the web terminal
(``evo_agents.worker.interactive_web``), and the ring of what that terminal printed last."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import pty
import struct
import sys
import termios
from collections.abc import Iterable

from evo_agents.hub import terminal as frames
from evo_agents.worker.interactive_tmux import Tmux, exact_target

log = logging.getLogger("evo_agents.worker")

RING_BYTES = 256 * 1024  # what the web terminal printed last, sent first to a browser that connects again
HIGH_WATER = 1024 * 1024  # bytes of output waiting for the websocket before the PTY is no longer read
LOW_WATER = 256 * 1024
MAX_PENDING_INPUT = 1024 * 1024  # bytes typed that the PTY has not taken yet; more is dropped
# Run before tmux attach, on the PTY: a session of its own whose controlling terminal is the PTY, so a resize of the
# PTY reaches tmux as SIGWINCH.
_CONTROLLING_TTY = (
    "import fcntl, os, sys, termios\n"
    "os.setsid()\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


class Ring:
    """The last ``limit`` bytes of a terminal's output."""

    def __init__(self, limit: int = RING_BYTES):
        self.limit = limit
        self._data = bytearray()
        self.dropped = False  # whether older output fell out

    def add(self, data: bytes) -> None:
        self._data += data
        excess = len(self._data) - self.limit
        if excess > 0:
            del self._data[:excess]
            self.dropped = True

    def __len__(self) -> int:
        return len(self._data)

    def snapshot(self) -> bytes:
        """What is kept, from the start of a line when older output fell out, so the replay starts clean."""
        data = bytes(self._data)
        if self.dropped:
            newline = data.find(b"\n", 0, 4096)
            if newline >= 0:
                data = data[newline + 1 :]
        return data


def pieces(data: bytes, size: int = frames.MAX_FRAME_BYTES - 1) -> Iterable[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class Attached:
    """``tmux attach`` of one session on a PTY of its own: what it prints comes out of ``output`` (None once it has
    ended) and goes to ``ring``; ``write`` types into it and ``resize`` sizes it."""

    def __init__(self, master: int, proc, ring: Ring):
        self.master = master
        self.proc = proc
        self.ring = ring
        self.output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.queued = 0
        self._reading = False
        self._writing = False
        self._pending = bytearray()
        self._ended = False
        self._loop = asyncio.get_running_loop()

    @classmethod
    async def start(cls, tmux: Tmux, name: str, size: tuple[int, int], ring: Ring) -> Attached:
        master, slave = pty.openpty()
        try:
            _set_size(master, *size)
            env = {**tmux.env, "TERM": "xterm-256color"}
            proc = await asyncio.create_subprocess_exec(
                sys.executable or "python3",
                "-I",
                "-c",
                _CONTROLLING_TTY,
                *tmux.argv("-u", "attach-session", "-t", exact_target(name)),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        attached = cls(master, proc, ring)
        attached._read_on()
        return attached

    def _read_on(self) -> None:
        if not self._reading and not self._ended:
            self._loop.add_reader(self.master, self._readable)
            self._reading = True

    def _read_off(self) -> None:
        if self._reading:
            self._loop.remove_reader(self.master)
            self._reading = False

    def _readable(self) -> None:
        try:
            data = os.read(self.master, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the other side of the PTY closed, tmux attach has ended
            data = b""
        if not data:
            self._read_off()
            self._ended = True
            self.output.put_nowait(None)
            return
        self.ring.add(data)
        self.queued += len(data)
        self.output.put_nowait(data)
        if self.queued > HIGH_WATER:
            self._read_off()

    def sent(self, count: int) -> None:
        """``count`` bytes of output went to the websocket."""
        self.queued -= count
        if self.queued < LOW_WATER:
            self._read_on()

    def write(self, data: bytes) -> None:
        if self._ended:
            return
        room = MAX_PENDING_INPUT - len(self._pending)
        if len(data) > room:
            log.warning(
                "terminal input dropped: the terminal takes it slower than it comes", extra={"bytes": len(data) - room}
            )
            data = data[: max(room, 0)]
        self._pending += data
        self._flush()

    def _flush(self) -> None:
        while self._pending:
            try:
                written = os.write(self.master, self._pending)
            except BlockingIOError:
                written = 0
            except OSError:
                self._pending.clear()
                break
            if written == 0:
                if not self._writing:
                    self._loop.add_writer(self.master, self._flush)
                    self._writing = True
                return
            del self._pending[:written]
        if self._writing:
            self._loop.remove_writer(self.master)
            self._writing = False

    def resize(self, cols: int, rows: int) -> None:
        with contextlib.suppress(OSError):
            _set_size(self.master, cols, rows)

    async def close(self) -> None:
        self._read_off()
        if self._writing:
            self._loop.remove_writer(self.master)
            self._writing = False
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()  # tmux attach detaches; the session goes on
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()
        with contextlib.suppress(OSError):
            os.close(self.master)

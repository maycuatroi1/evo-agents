"""The daemons that run from one install: two workers of one machine may share a uv tool install, and an update
replaces the code of all of them. They meet in ``~/.evo/installs/<install>/`` (0700), where ``<install>`` is a hash of
the install's path:

```
daemons/<home>.json   each daemon's state, written at each of its turns: its worker, pid and home, whether it may
                      update now (``ready``) and why not (``why``); a file whose daemon no longer runs (its home's
                      daemon.pid is not locked by that pid) is left out and removed
busy.lock             each daemon holds it shared while it claims or holds runs; the updater takes it exclusive
gate.lock             the updater holds it exclusive from the moment it waits for the others until it exits; a daemon
                      claims nothing while it is closed
staging/              where the updater installs the new version first
backup/               a copy of the install while the updater installs over it (``backup``)
```

A daemon takes busy.lock shared before each claim, then looks at the gate; the updater closes the gate, then waits
for busy.lock exclusive. Whatever the order, a claim and an update never overlap: either the claim holds busy.lock and
the updater waits for it (and gives up when a run comes of it), or the gate is closed and the daemon does not claim.
The kernel drops the locks of a daemon that dies. Standard library only.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.hub.client import write_atomic
from evo_agents.worker.home import WorkerHome

DIR_MODE = 0o700
FILE_MODE = 0o600
POLL_SECONDS = 0.2


def key_of(path: Path) -> str:
    return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:16]


def root_of(prefix: Path) -> Path:
    """Where the daemons of the install at ``prefix`` meet."""
    return Path.home() / ".evo" / "installs" / key_of(prefix)


class Peers:
    def __init__(self, root: Path, home: Path):
        self.root = Path(root)
        self.home = Path(home)
        self.me = key_of(self.home)
        self._busy: int | None = None  # shared while this daemon claims or holds runs
        self._gate: int | None = None  # exclusive while this daemon updates the install
        self._update: int | None = None  # busy.lock exclusive, while it updates

    @property
    def daemons(self) -> Path:
        return self.root / "daemons"

    @property
    def staging(self) -> Path:
        return self.root / "staging"

    @property
    def backup(self) -> Path:
        return self.root / "backup"

    def _open(self, name: str) -> int:
        self.root.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        return os.open(self.root / name, os.O_RDWR | os.O_CREAT, FILE_MODE)

    # Each daemon's state

    def publish(self, state: dict) -> None:
        self.daemons.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        record = {**state, "pid": os.getpid(), "home": str(self.home), "at": datetime.now(UTC).isoformat()}
        write_atomic(self.daemons / f"{self.me}.json", json.dumps(record).encode() + b"\n", FILE_MODE)

    def others(self) -> list[dict]:
        """The state of every other daemon of the install that runs."""
        found = []
        for path in sorted(self.daemons.glob("*.json")) if self.daemons.is_dir() else []:
            if path.stem == self.me:
                continue
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                alive = WorkerHome(Path(state["home"])).read_pid() == state["pid"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if alive:
                found.append(state)
            else:
                with contextlib.suppress(OSError):
                    path.unlink()
        return found

    def forget(self) -> None:
        with contextlib.suppress(OSError):
            (self.daemons / f"{self.me}.json").unlink()

    # Claims

    def enter(self) -> bool:
        """Before a claim: hold the install shared, and say whether the gate is open. False when an update holds
        the install or waits for it; the caller lets go with ``idle`` once it holds no run."""
        try:
            if self._busy is None:
                self._busy = self._open("busy.lock")
            fcntl.flock(self._busy, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return False
        return self.gate_open()

    def idle(self) -> None:
        """This daemon neither claims nor holds a run."""
        if self._busy is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._busy, fcntl.LOCK_UN)

    def gate_open(self) -> bool:
        if self._gate is not None:
            return False
        try:
            fd = self._open("gate.lock")
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return False
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return True
        finally:
            os.close(fd)

    # The update

    def close_gate(self) -> bool:
        """Close the gate for an update; False when another daemon of the install holds it."""
        if self._gate is not None:
            return True
        fd = self._open("gate.lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._gate = fd
        return True

    async def wait_idle(self, timeout: float) -> bool:
        """With the gate closed: wait until no daemon of the install claims or holds a run, up to ``timeout``
        seconds; once it returns True, none can until the gate opens."""
        fd = self._open("busy.lock")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if asyncio.get_running_loop().time() >= deadline:
                    os.close(fd)
                    return False
                await asyncio.sleep(POLL_SECONDS)
                continue
            self._update = fd
            return True

    def open_gate(self) -> None:
        for name in ("_update", "_gate"):
            fd = getattr(self, name)
            if fd is not None:
                setattr(self, name, None)
                with contextlib.suppress(OSError):
                    os.close(fd)  # closing drops the lock

    def close(self) -> None:
        self.open_gate()
        if self._busy is not None:
            fd, self._busy = self._busy, None
            with contextlib.suppress(OSError):
                os.close(fd)

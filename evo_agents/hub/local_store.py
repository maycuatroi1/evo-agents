"""What a local session keeps on this machine (``evo_agents.hub.local_sync``): its file
``~/.evo/hub/local/<session id>.json``, the lock its hooks take on it, and the pruning of the files of old sessions.
Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

from evo_agents.hub.client import FILE_MODE, hub_dir, write_atomic

try:
    import fcntl
except ImportError:  # Windows: no lock, the writes stay atomic and the hooks of one session rarely meet
    fcntl = None  # type: ignore[assignment]

DIRECTORY = "local"  # under ~/.evo/hub
VERSION = 1
KEEP_DAYS = 7


def cut(text, limit: int) -> str:
    """``text`` as a string of at most ``limit`` characters."""
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def directory() -> Path:
    return hub_dir() / DIRECTORY


def read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("version") == VERSION else None


@contextlib.contextmanager
def locked(path: Path, wait: float) -> Iterator[bool]:
    """True when this process holds the lock of ``path`` (waiting up to ``wait`` seconds for it); False when another
    does, or when the file cannot be made. Without ``fcntl`` it is always True."""
    if fcntl is None:
        yield True
        return
    fd = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    except OSError:
        yield False
        return
    got = False
    try:
        deadline = time.monotonic() + wait
        while not got:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        yield got
    finally:
        if got:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class Store:
    """The file of one session for one hub."""

    def __init__(self, session_id: str, url: str):
        self.url = url
        self.path = directory() / f"{session_id}.json"
        self.lock = directory() / f"{session_id}.lock"

    def load(self) -> dict | None:
        rec = read_record(self.path)
        return rec if rec is not None and rec.get("hub") == self.url else None

    def save(self, rec: dict) -> None:
        with contextlib.suppress(OSError):  # only ever what waits for the hub
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_atomic(self.path, json.dumps(rec, ensure_ascii=False).encode(), FILE_MODE)

    def fresh(self, cwd: str) -> dict:
        return {
            "version": VERSION,
            "hub": self.url,
            "cwd": cwd,
            "project": None,
            "resolved": False,
            "run_id": None,
            "sent": None,
            "wanted": None,
            "seq": 0,
            "events": [],
            "offset": None,
            "flushed": 0.0,
            "touched": time.time(),
            "title": None,
            "account": None,
            "refused_at": None,
        }


def prune(now: float) -> None:
    """Delete the files of sessions untouched for KEEP_DAYS days."""
    with contextlib.suppress(OSError):
        for path in directory().iterdir():
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > KEEP_DAYS * 86400:
                    path.unlink()

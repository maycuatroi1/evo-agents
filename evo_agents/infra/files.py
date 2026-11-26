"""Files replaced whole, and JSON files read and written through them (docs/structure.md, section Shared
infrastructure).

``write_atomic`` writes a temporary file beside the target, flushed to disk, and renames it over the target with
``os.replace``, so a reader or a crash finds the old file or the new one, never half of one. ``JsonFile`` reads a JSON
document as a value that is never an exception (a file missing, broken or of another shape reads as the default)
and writes it with ``write_atomic`` in the format its owner chose.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

PRIVATE = 0o600  # a file only its owner reads: credentials, sessions, a worker's state
PRIVATE_DIR = 0o700
REPLACE_TRIES = 40  # of os.replace while Windows refuses it because another process has the file open a moment
REPLACE_POLL = 0.05


def fsync_dir(directory: Path) -> None:
    """Flush ``directory``'s entries to disk, so a rename in it outlives a crash; nothing where that cannot be done."""
    with contextlib.suppress(OSError):  # not every platform can open a directory
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def replace(source: Path | str, target: Path | str, *, tries: int = REPLACE_TRIES) -> None:
    """os.replace, tried again while Windows refuses it (WinError 5) because another process has ``target`` open
    for a moment, as a reader does; POSIX replaces an open file, so there a refusal is real and raises at once."""
    for attempt in range(tries):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == tries - 1:
                raise
            time.sleep(REPLACE_POLL)


def write_atomic(
    path: Path, data: bytes, mode: int | None = None, *, durable: bool = True, tries: int = REPLACE_TRIES
) -> None:
    """Replace ``path`` with ``data`` atomically: a temporary file in the same directory (made 0600), flushed to disk
    when ``durable`` and renamed over it, so a crash leaves the old file or the new one. ``mode`` exactly, whatever
    the umask; None leaves the file 0600."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # created 0600
    try:
        if mode is not None:
            os.chmod(tmp, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            if durable:
                handle.flush()
                os.fsync(handle.fileno())
        replace(tmp, path, tries=tries)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if durable:
        fsync_dir(path.parent)


def write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, mode 0600 from the first byte on."""
    write_atomic(path, data, PRIVATE)


class JsonFile:
    """One JSON document on disk.

    ``read`` gives the document, or ``default`` when the file is missing, cannot be read, is not JSON, or fails
    ``accept``; ``read_object`` gives a dict, ``{}`` for anything else. ``write`` replaces the file whole
    (``write_atomic``), making its directory (``dir_mode``) first, in ``mode`` (None: 0600) and in the format the
    keyword arguments of ``json.dumps`` given here say, with a newline at the end when ``newline``."""

    def __init__(
        self,
        path: Path | str,
        *,
        mode: int | None = None,
        dir_mode: int | None = None,
        newline: bool = False,
        durable: bool = True,
        tries: int = REPLACE_TRIES,
        **dumps: Any,
    ):
        self.path = Path(path)
        self.mode = mode
        self.dir_mode = dir_mode
        self.newline = newline
        self.durable = durable
        self.tries = tries
        self.dumps = dumps

    def __repr__(self) -> str:
        return f"JsonFile({str(self.path)!r})"

    def read(self, default: Any = None, *, accept: Callable[[Any], bool] | None = None) -> Any:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default
        return data if accept is None or accept(data) else default

    def read_object(self) -> dict:
        return self.read({}, accept=lambda data: isinstance(data, dict))

    def text(self, data: Any) -> str:
        return json.dumps(data, **self.dumps) + ("\n" if self.newline else "")

    def write(self, data: Any) -> None:
        if self.dir_mode is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self.path.parent.mkdir(mode=self.dir_mode, parents=True, exist_ok=True)
        write_atomic(self.path, self.text(data).encode("utf-8"), self.mode, durable=self.durable, tries=self.tries)

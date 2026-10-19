"""A copy of the install, made before ``uv tool install`` goes over it and put back when the update fails.

uv does not go over a tool's environment the same way in every release. uv 0.6.13 replaced the changed packages of an
environment made from the same interpreter in place, and a build that failed left it as it was. uv 0.12.24 deletes the
environment and builds it again on every ``--force`` install, with the same interpreter and the same commit. A build
that fails then, followed by a put-back from the network that fails too (the network is down), would leave no install
at all. Putting this copy back needs neither uv nor the network, whatever uv did to the environment.

```
backup/env/            the environment as it was, its symlinks (bin/python) kept as symlinks
backup/commands/<n>    each command of the receipt that was a file
backup/commands.json   where each command was, and the target of those that were symlinks
backup/KEPT            the copy could not be put back, nor the old version installed again: it stays for a person,
                       and no update runs until they remove it
```

The copy is made where the daemons of the install meet (``peers``), usually on the filesystem of the install, where
putting it back is a rename; across two filesystems it is a copy. Standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

DIR_MODE = 0o700
ENV = "env"
COMMANDS = "commands"
MANIFEST = "commands.json"
KEPT = "KEPT"


class Kept(OSError):
    """A copy that could not be put back after an earlier update is still there, and only a person may drop it."""


def kept(place: Path) -> str | None:
    """What the copy at ``place`` was kept for, when a failed put-back left it; None otherwise."""
    try:
        return (Path(place) / KEPT).read_text(encoding="utf-8").strip() or "a failed update"
    except OSError:
        return None


def drop(place: Path) -> None:
    """Remove the copy at ``place``, unless it was kept for a person."""
    if kept(place) is None:
        shutil.rmtree(place, ignore_errors=True)


def keep(place: Path, why: str) -> None:
    """Keep the copy at ``place`` for a person, when it is still whole: the next update stops before it instead of
    copying over it."""
    if (Path(place) / ENV).is_dir():
        (Path(place) / KEPT).write_text(why + "\n", encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def take(prefix: Path, commands: Iterable[Path], place: Path) -> None:
    """Copy the environment at ``prefix`` and the commands that run it to ``place``. OSError when it could not, and
    then nothing of the copy is left; ``Kept`` when a copy kept for a person is there."""
    place = Path(place)
    why = kept(place)
    if why is not None:
        raise Kept(f"a copy of the install kept after an update whose put-back failed is at {place} ({why})")
    shutil.rmtree(place, ignore_errors=True)  # what a daemon stopped in the middle of an update left
    try:
        place.mkdir(mode=DIR_MODE, parents=True)
        shutil.copytree(prefix, place / ENV, symlinks=True)
        manifest = []
        for index, command in enumerate(commands):
            if command.is_symlink():
                manifest.append({"path": str(command), "link": os.readlink(command)})
            elif command.is_file():
                (place / COMMANDS).mkdir(mode=DIR_MODE, exist_ok=True)
                shutil.copy2(command, place / COMMANDS / str(index))
                manifest.append({"path": str(command), "copy": str(index)})
        (place / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    except BaseException:
        shutil.rmtree(place, ignore_errors=True)
        raise


def restore(place: Path, prefix: Path) -> None:
    """Put the copy at ``place`` back at ``prefix`` and its commands where they were, in place of whatever is there
    now. OSError (ValueError for a manifest that does not read) when it could not."""
    place, prefix = Path(place), Path(prefix)
    manifest = json.loads((place / MANIFEST).read_text(encoding="utf-8"))
    if not (place / ENV).is_dir():
        raise FileNotFoundError(f"no copy of the install at {place / ENV}")
    _remove(prefix)
    shutil.move(place / ENV, prefix)  # a rename on one filesystem, a copy across two
    for item in manifest:
        path = Path(item["path"])
        _remove(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if item.get("link") is not None:
            os.symlink(item["link"], path)
        else:
            shutil.copy2(place / COMMANDS / item["copy"], path)

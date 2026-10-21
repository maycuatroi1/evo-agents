"""The Python environment the worker makes in a worktree of a plan run or a run of one step, for a repo that asks
for one in its pyproject.toml, at the branch the run works on (``docs/workers.md``, The Python environment of a
worktree):

```
[tool.evo-agents.worker]
venv = true                                   # the worker makes <worktree>/.venv
extras = ["test"]                             # the extras `uv sync` installs, --extra each
groups = ["lint"]                             # dependency groups beyond uv's default ones, --group each
requirements = ["deploy/plugins.txt"]         # requirement files installed after it, --no-deps, hashes checked
```

The daemon runs ``uv sync --frozen`` in the worktree once it is made, which installs the repo's own package in
editable mode into ``<worktree>/.venv`` from its uv.lock, then ``uv pip install --no-deps -r`` of each requirement
file, and puts ``<worktree>/.venv/bin`` first on the PATH of the agent and of the commands the daemon runs for the run
(``runner.venv``). So ``python -m pytest`` and ``python -m <package>`` read the metadata of the branch being worked on,
its entry points among them, not what the machine's own ``python`` has installed. ``evo-agents worker step`` runs the
same commands again before a done step's verify, so an entry point or a dependency the step added is there too, and
runs that verify with the step's repo's ``.venv/bin`` first. A repo that does not ask gets nothing of it.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

VENV_DIR = ".venv"
# What would make uv sync or install another environment than <worktree>/.venv: left out of uv's environment, and out
# of the agent's once the run has a venv.
FOREIGN = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # an extra or a group, as uv takes it
MAX_ITEMS = 32  # extras, groups or requirement files


@dataclass(frozen=True)
class Wanted:
    """What a repo asks of its environment (``[tool.evo-agents.worker]`` of its pyproject.toml)."""

    extras: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()  # paths relative to the worktree

    def commands(self, worktree: Path, uv: str = "uv") -> list[list[str]]:
        """The commands that make the environment in ``worktree``, in order, with ``uv`` the program."""
        sync = [uv, "sync", "--frozen"]
        for extra in self.extras:
            sync += ["--extra", extra]
        for group in self.groups:
            sync += ["--group", group]
        python = str(bin_dir(worktree) / "python")
        installs = [[uv, "pip", "install", "--python", python, "--no-deps", "-r", file] for file in self.requirements]
        return [sync, *installs]


def wanted(worktree: Path) -> Wanted | None:
    """What the repo of ``worktree`` asks of its environment; None when it asks for none: no pyproject.toml, no
    ``[tool.evo-agents.worker]``, or its ``venv`` not true. ValueError, saying why, when the file is not TOML or the
    table is not as the module says."""
    path = worktree / "pyproject.toml"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"pyproject.toml is not read: {exc}") from None
    table = data.get("tool", {}).get("evo-agents", {}).get("worker")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ValueError("[tool.evo-agents.worker] is not a table")
    venv = table.get("venv", False)
    if not isinstance(venv, bool):
        raise ValueError("venv of [tool.evo-agents.worker] is true or false")
    if not venv:
        return None
    extras = _names(table, "extras")
    groups = _names(table, "groups")
    requirements = _list(table, "requirements")
    for file in requirements:
        if not file.strip() or Path(file).is_absolute() or ".." in Path(file).parts:
            raise ValueError(f"requirements of [tool.evo-agents.worker]: {file!r} is not a path inside the repo")
    return Wanted(extras, groups, requirements)


def _list(table: dict, key: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value) or len(value) > MAX_ITEMS:
        raise ValueError(f"{key} of [tool.evo-agents.worker] is a list of at most {MAX_ITEMS} strings")
    return tuple(dict.fromkeys(value))


def _names(table: dict, key: str) -> tuple[str, ...]:
    names = _list(table, key)
    for name in names:
        if not NAME.match(name):
            raise ValueError(f"{key} of [tool.evo-agents.worker]: {name!r} is not a name uv takes")
    return names


def bin_dir(worktree: Path) -> Path:
    """The bin directory of the environment the worker makes in ``worktree``."""
    return worktree / VENV_DIR / "bin"


def shown(command: list[str]) -> str:
    """``command`` as the run's log and the agent read it: ``uv ...`` whatever path ``uv`` was found at."""
    return shlex.join(["uv", *command[1:]])


def uv_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without what would make uv use another environment than the worktree's (``FOREIGN``)."""
    return {key: value for key, value in env.items() if key not in FOREIGN}


def front(path: str | None, dirs: Iterable[str]) -> str:
    """``path`` (the shell's default one when None) with ``dirs`` first, in their order, each once."""
    first = list(dict.fromkeys(dirs))
    rest = [item for item in (path if path is not None else os.defpath).split(os.pathsep) if item and item not in first]
    return os.pathsep.join([*first, *rest])


async def make(worktree: Path, want: Wanted, uv: str, env: Mapping[str, str]) -> tuple[list[str], int, str]:
    """Run the commands of ``want`` in ``worktree`` with ``env`` (``uv_env`` of it), one after the other until one
    fails: the last that ran, its exit code and its output."""
    for command in want.commands(worktree, uv):
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(worktree),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=uv_env(env),
        )
        output, _ = await proc.communicate()
        if proc.returncode != 0:
            return command, proc.returncode, output.decode(errors="replace")
    return command, 0, ""

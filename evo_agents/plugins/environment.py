"""Where the backend of an isolated plugin lives on the plugin host's machine, and how it starts (docs/plugins.md).

Under the host's data directory ``root``, for plugin ``name`` at ``version``:

- ``code/<name>/<version>/``: the bundle's files, written once and read-only to the backend;
- ``envs/<name>/<version>/``: its own virtual environment, ``python -m venv --without-pip`` from the host's Python,
  then the requirements file its manifest names, installed by pip from wheels only, so that installing runs none of
  the plugin's code;
- ``data/<name>/``: the one directory the backend writes, its HOME and, under ``tmp``, its TMPDIR, kept across
  versions and restarts.

The backend starts as ``<env>/bin/python -E -s -B -u -X utf8 -c LAUNCHER <code>/<entry>`` in its data directory, with
an environment built from nothing (``backend_env``): PATH, HOME, TMPDIR, LANG, VIRTUAL_ENV and the plugin's name,
version and protocol. Nothing of the host's environment reaches it, and its secrets never travel in an environment
variable or an argument: they come on its stdin, in ``initialize``. LAUNCHER is the first code the process runs: on
Linux it marks the process not dumpable (``prctl(PR_SET_DUMPABLE, 0)``), so that another process of the same user can
neither read its /proc entries (environ, mem, fd) nor attach to it, and exits 70 when that fails; then it runs the
entry as ``__main__``. The host marks itself the same way (``set_not_dumpable``).

When the host runs as root (the plugins service of the hub's compose file, with only the capabilities SETUID,
SETGID, CHOWN and KILL), each backend runs under a user and group of its own, ``UID_BASE`` plus the plugin's id on
the hub, without supplementary groups (``identity``): the code and the environment stay root's and read-only to it,
its data directory is its own with mode 0700, and no backend can read, write or signal another's. Run as any other
user, the host starts every backend under its own user (isolation ``process``), which keeps the environment,
arguments, memory and secrets of each process from the others but not their files; ``isolation`` says which.

Standard library only.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from evo_agents.plugins.bundle import read_bundle
from evo_agents.plugins.protocol import PROTOCOL

UID_BASE = 200_000  # the user and group of the backend of plugin id N are UID_BASE + N, when the host runs as root
PR_SET_DUMPABLE = 4
NOT_DUMPABLE_EXIT = 70  # the launcher's exit code when it cannot mark the process not dumpable
READY = ".evo-ready"  # the marker of a directory written whole: the bundle's SHA-256, or the requirements'
INSTALL_TIMEOUT = 600  # seconds for one venv and its requirements
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
LAUNCHER = (
    "import ctypes, os, runpy, sys\n"
    "if sys.platform.startswith('linux'):\n"
    "    libc = ctypes.CDLL(None, use_errno=True)\n"
    "    if libc.prctl(4, 0, 0, 0, 0) != 0:\n"
    "        sys.stderr.write('evo-agents plugin launcher: cannot mark the process not dumpable\\n')\n"
    "        sys.exit(70)\n"
    "entry = os.path.abspath(sys.argv[1])\n"
    "sys.argv = sys.argv[1:]\n"
    "sys.path[0] = os.path.dirname(entry)\n"
    "runpy.run_path(entry, run_name='__main__')\n"
)


class PlacementError(RuntimeError):
    """A plugin that cannot be placed or started here: its bundle, its environment or its requirements."""


@dataclass(frozen=True)
class Placement:
    """The three directories of one version of one plugin, and the Python of its environment."""

    name: str
    version: str
    code: Path
    env: Path
    data: Path

    @property
    def python(self) -> Path:
        return self.env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@dataclass(frozen=True)
class Identity:
    """The user and group a backend runs under, when the host can give it its own."""

    uid: int
    gid: int


def placement(root: Path, name: str, version: str) -> Placement:
    where = root / "code" / name / version, root / "envs" / name / version, root / "data" / name
    return Placement(name, version, *where)


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def identity(plugin_id: int) -> Identity | None:
    """The user of the backend of plugin ``plugin_id``: its own as root, None (the host's) otherwise."""
    if not running_as_root():
        return None
    return Identity(UID_BASE + plugin_id, UID_BASE + plugin_id)


def isolation() -> str:
    """``uid`` when each backend gets a user of its own, ``process`` when they share the host's."""
    return "uid" if running_as_root() else "process"


def set_not_dumpable() -> bool:
    """Mark this process not dumpable on Linux, as the launcher does each backend; False elsewhere or on failure."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        return ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _ready(directory: Path, stamp: str) -> bool:
    try:
        return (directory / READY).read_text(encoding="utf-8") == stamp
    except OSError:
        return False


def _replace(directory: Path, filled: Path, stamp: str) -> None:
    (filled / READY).write_text(stamp, encoding="utf-8")
    if directory.exists():
        shutil.rmtree(directory)
    filled.rename(directory)


def extract(data: bytes, code: Path) -> str:
    """Write the files of bundle ``data`` into ``code``, read-only, unless it holds them already; the bundle's
    SHA-256. PlacementError for a bundle that is not one."""
    sha256 = hashlib.sha256(data).hexdigest()
    if _ready(code, sha256):
        return sha256
    try:
        contents = read_bundle(data)
    except ValueError as exc:
        raise PlacementError(f"the bundle does not read: {exc}") from None
    code.parent.mkdir(parents=True, exist_ok=True)
    filled = Path(tempfile.mkdtemp(prefix=".extract-", dir=code.parent))
    for entry in contents.files:
        target = filled / entry.path
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        target.write_bytes(entry.data)
        target.chmod(0o555 if entry.executable else 0o444)
    filled.chmod(0o755)
    _replace(code, filled, sha256)
    return sha256


def _run(command: list[str], doing: str) -> None:
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=INSTALL_TIMEOUT, env=_install_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlacementError(f"cannot {doing}: {exc}") from None
    if done.returncode != 0:
        tail = " | ".join((done.stderr or done.stdout).strip().splitlines()[-3:])
        raise PlacementError(f"cannot {doing} (exit {done.returncode}): {tail[:600]}")


def _install_env() -> dict[str, str]:
    """What pip and venv run with: the host's PATH and locale, never its credentials."""
    kept = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT") if name in os.environ}
    return {**kept, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def build_env(where: Placement, requirements: str | None, python: str = sys.executable) -> None:
    """Make the plugin's virtual environment, its requirements installed from wheels, unless it is made already."""
    wanted = (where.code / requirements) if requirements else None
    stamp = hashlib.sha256(wanted.read_bytes()).hexdigest() if wanted is not None and wanted.is_file() else "-"
    if wanted is not None and stamp == "-":
        raise PlacementError(f"the manifest names requirements {requirements}, which the bundle does not hold")
    if _ready(where.env, stamp):
        return
    where.env.parent.mkdir(parents=True, exist_ok=True)
    filled = Path(tempfile.mkdtemp(prefix=".venv-", dir=where.env.parent))
    shutil.rmtree(filled)
    _run([python, "-m", "venv", "--without-pip", str(filled)], f"make the environment of plugin {where.name}")
    if wanted is not None:
        target = filled / where.python.relative_to(where.env)
        pip = [python, "-m", "pip", "--python", str(target), "install", "--only-binary=:all:", "-r", str(wanted)]
        _run(pip, f"install the requirements of plugin {where.name}")
    _replace(where.env, filled, stamp)


def prepare_data(where: Placement, owner: Identity | None) -> None:
    """The plugin's data directory and its tmp, mode 0700, owned by ``owner`` when it has a user of its own."""
    tmp = where.data / "tmp"
    tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in (where.data, tmp):
        if owner is not None:
            os.chown(directory, owner.uid, owner.gid)
        directory.chmod(0o700)


def backend_env(where: Placement) -> dict[str, str]:
    """The whole environment of a backend, built from nothing."""
    return {
        "PATH": f"{where.python.parent}{os.pathsep}{SYSTEM_PATH}",
        "HOME": str(where.data),
        "TMPDIR": str(where.data / "tmp"),
        "LANG": "C.UTF-8",
        "VIRTUAL_ENV": str(where.env),
        "EVO_PLUGIN": where.name,
        "EVO_PLUGIN_VERSION": where.version,
        "EVO_PLUGIN_PROTOCOL": PROTOCOL,
    }


def backend_command(where: Placement, entry: str) -> list[str]:
    """The command of a backend: the environment's Python, isolated from PYTHON* variables and the user's site, the
    launcher, then the entry."""
    return [str(where.python), "-E", "-s", "-B", "-u", "-X", "utf8", "-c", LAUNCHER, str(where.code / entry)]

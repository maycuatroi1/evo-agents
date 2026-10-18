"""Where the backend of an isolated plugin lives on the plugin host's machine, and how it starts (docs/plugins.md).

Under the host's data directory ``root``, for plugin ``name`` at ``version``:

- ``code/<name>/<version>/``: the bundle's files, written once by the host and read-only to the backend;
- ``envs/<name>/<version>/``: its own virtual environment, ``python -m venv --without-pip`` from the host's Python,
  then the wheels its requirements file names, installed from the bundle alone (``evo_agents.plugins.requirements``);
- ``data/<name>/``: the one directory the backend writes, its HOME and, under ``tmp``, its TMPDIR, kept across
  versions and restarts.

When the host runs as root (the plugins service of the hub's compose file, with only the capabilities SETUID,
SETGID, CHOWN and KILL), each backend runs under a user and group of its own, ``UID_BASE`` plus the plugin's id on
the hub, without supplementary groups (``identity``). Its code and environment are root's, read-only to it, in a
top directory of mode 0750 whose group is its own, so no other backend can even list them; its data directory is its
own with mode 0700. The host has neither CAP_FOWNER nor CAP_DAC_OVERRIDE, so it changes the mode of a directory
only while it owns it, and takes a directory back (chown to root) before it touches what is inside. pip runs under
the backend's user too, never as root, in an empty directory of root's: the environment is the backend's while pip
fills it and root's again after (``build_env``), and the requirements are a file the host wrote from the plugin's,
refused unless every line names a hashed wheel of the bundle. Taking the environment back removes every link and
set-id file of the backend's user in it and resets every mode (``_take_back``), and the marker of a directory written
whole is created, never opened through a link (``_write_ready``): pip writes neither, so one there was planted by
another process of that user, a link aimed at a file root may write.

Isolation ``process`` is what a host that is not root can give: every backend under the host's own user. Their
environments and arguments stay apart, but not their files, their /proc entries in the moment after exec, nor the
host's: a backend can read the host's environment and any process of that user. The host refuses to start a backend
that way unless it is told to (``Host(allow_shared_user=True)``, ``evo-agents hub plugin-host --allow-shared-user``),
for a developer's machine; ``evo-agents plugin test`` runs the author's own plugin that way.

The backend starts through a trampoline and a launcher. The trampoline is the host's own Python, isolated
(``-I -S``: no site, no current directory). Started by the host, it first closes every file descriptor but 0, 1 and
2, still as root (a loop may ignore ``close_fds``: uvloop does), then takes the backend's user and group, dropping
every supplementary group, and exits NO_USER_EXIT unless it holds exactly them as its real, effective and saved IDs,
with no capability left; it does so itself, so the start needs no argument of the event loop's subprocess_exec that
some loops lack (``backend_command``). A backend that keeps the host's user (isolation ``process``) never runs as
root, unless ``evo-agents plugin test`` says so for the author's own plugin (``KEEP_ROOT``). Then it
changes into the backend's data directory, which root may not enter, and sets the resource limits
(``backend_limits``: no core file, open files, address space, and in isolation ``uid`` the processes of the user), so
they hold before any code of the plugin runs, then execs the environment's Python, isolated too (``-I``), with the
launcher. The launcher marks the process not dumpable on Linux (``prctl(PR_SET_DUMPABLE, 0)``) and exits 70 when that
fails, then runs the entry as ``__main__``. The environment of the backend is built from nothing (``backend_env``):
PATH, HOME, TMPDIR, LANG, VIRTUAL_ENV and the plugin's name, version and protocol. Its secrets never travel in an
environment variable or an argument: they come on its stdin, in ``initialize``. The host marks itself not dumpable
too (``set_not_dumpable``).

Standard library only.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from evo_agents.plugins.bundle import read_bundle
from evo_agents.plugins.protocol import PROTOCOL
from evo_agents.plugins.requirements import WHEELS, RequirementsError, normalized, parse_requirements

UID_BASE = 200_000  # the user and group of the backend of plugin id N are UID_BASE + N, when the host runs as root
PR_SET_DUMPABLE = 4
NOT_DUMPABLE_EXIT = 70  # the launcher's exit code when it cannot mark the process not dumpable
READY = ".evo-ready"  # the marker of a directory written whole: the bundle's SHA-256, or the requirements'
REQUIREMENTS_COPY = ".evo-requirements.txt"  # the file pip reads, written by the host from the plugin's
INSTALL_TIMEOUT = 600  # seconds for one venv and its requirements
SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"
LIMITS = {"core": 0, "nofile": 1024, "as": 4 * 1024**3}  # per process: no core file (it would hold the secrets)
NPROC = 256  # processes and threads of a backend's user, in isolation uid (RLIMIT_NPROC counts by real user)
SAME_USER = "-"  # the trampoline's user argument when the backend keeps the host's user, which must not be root
KEEP_ROOT = "root"  # the same, root included: `evo-agents plugin test` only, which runs the author's own plugin
NO_USER_EXIT = 71  # the exit code of the trampoline or the killer when it cannot take the user it is given
KILLER_UID_BASE = 100_000  # the real user of the process that kills plugin id N's processes is KILLER_UID_BASE + N
CLOSE_FDS = (  # every descriptor but 0, 1 and 2: uvloop's subprocess_exec ignores close_fds
    "def close_fds():\n"
    "    try:\n"
    "        fds = [int(n) for n in os.listdir('/proc/self/fd' if os.path.isdir('/proc/self/fd') else '/dev/fd')]\n"
    "    except (OSError, ValueError):\n"
    "        fds = range(3, 65536)\n"
    "    for fd in fds:\n"
    "        if fd > 2:\n"
    "            try:\n"
    "                os.close(fd)\n"
    "            except OSError:\n"
    "                pass\n"
)
TAKE_USER = (  # take a user and group, no other group and no capability, or exit NO_USER_EXIT; never user 0
    "def refuse(why):\n"
    "    sys.stderr.write('evo-agents plugin: ' + why + '\\n')\n"
    f"    sys.exit({NO_USER_EXIT})\n"
    "def take_user(ruid, euid, suid, gid):\n"
    "    if min(ruid, euid, suid, gid) <= 0:\n"
    "        refuse('will not take user or group 0')\n"
    "    linux = sys.platform.startswith('linux')\n"
    "    try:\n"
    "        os.setgroups([])\n"
    "        os.setresgid(gid, gid, gid)\n"
    "        os.setresuid(ruid, euid, suid)\n"
    "        held = (os.getresuid(), os.getresgid(), os.getgroups())\n"
    "        caps = []\n"
    "        if linux:\n"
    "            with open('/proc/self/status') as status:\n"
    "                caps = [int(line.split()[1], 16) for line in status\n"
    "                        if line.startswith(('CapPrm:', 'CapEff:', 'CapAmb:'))]\n"
    "    except (OSError, ValueError, AttributeError) as exc:\n"
    "        refuse('cannot take user %d: %s' % (euid, exc))\n"
    "    if held != ((ruid, euid, suid), (gid, gid, gid), []) or any(caps) or (linux and len(caps) < 2):\n"
    "        refuse('does not hold exactly user %d and group %d with no capability' % (euid, gid))\n"
)
TRAMPOLINE = (
    "import os, sys\n" + CLOSE_FDS + TAKE_USER + "close_fds()\n"
    "if sys.argv[1] == '-':\n"
    "    if os.getuid() == 0 or os.geteuid() == 0:\n"
    "        refuse('will not run a backend under the host user root')\n"
    "elif sys.argv[1] != 'root':\n"
    "    try:\n"
    "        uid, gid = (int(part) for part in sys.argv[1].split(':'))\n"
    "    except ValueError:\n"
    "        refuse('no user:group in %r' % sys.argv[1])\n"
    "    take_user(uid, uid, uid, gid)\n"
    "os.chdir(os.environ['HOME'])\n"
    "if sys.platform.startswith('linux'):\n"
    "    import resource\n"
    "    for item in filter(None, sys.argv[2].split(',')):\n"
    "        name, _, value = item.partition('=')\n"
    "        which = getattr(resource, 'RLIMIT_' + name.upper())\n"
    "        hard = resource.getrlimit(which)[1]\n"
    "        limit = int(value) if hard == resource.RLIM_INFINITY else min(int(value), hard)\n"
    "        resource.setrlimit(which, (limit, limit))\n"
    "os.execv(sys.argv[3], sys.argv[3:])\n"
)
LAUNCHER = (
    "import ctypes, os, runpy, sys\n"
    "if sys.platform.startswith('linux'):\n"
    "    libc = ctypes.CDLL(None, use_errno=True)\n"
    "    if libc.prctl(4, 0, 0, 0, 0) != 0:\n"
    "        sys.stderr.write('evo-agents plugin launcher: cannot mark the process not dumpable\\n')\n"
    "        sys.exit(70)\n"
    "entry = os.path.abspath(sys.argv[1])\n"
    "sys.argv = sys.argv[1:]\n"
    "sys.path.insert(0, os.path.dirname(entry))\n"
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


def killer_uid(owner: Identity) -> int | None:
    """The real user of the process that kills ``owner``'s processes (``process.kill_user``): one no process of the
    plugin has, so none of them may signal or trace it, while its effective user, the plugin's, lets it signal them
    all. None when ``owner`` is no user ``identity`` gives (a plugin id of KILLER_UID_BASE or more would meet the
    users of the backends)."""
    index = owner.uid - UID_BASE
    return KILLER_UID_BASE + index if 0 <= index < UID_BASE - KILLER_UID_BASE else None


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


def _write_ready(filled: Path, stamp: str) -> None:
    """Create the marker of ``filled`` and write ``stamp`` in it; PlacementError when something is there already. Never
    through a link: O_EXCL with O_CREAT refuses any name that exists, a link included, and O_NOFOLLOW says it again."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(filled / READY, flags, 0o444)
    except FileExistsError:
        raise PlacementError(f"{filled.name} holds a {READY} the host did not write; it is not used") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as marker:
        marker.write(stamp)


def _replace(directory: Path, filled: Path, stamp: str) -> None:
    _write_ready(filled, stamp)
    if directory.exists():
        shutil.rmtree(directory)
    filled.rename(directory)


def _mkdirs(directory: Path) -> None:
    """``directory`` and its missing parents, each mode 0755, so a backend's user can pass through them."""
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir(exist_ok=True)
        path.chmod(0o755)


def _seal(directory: Path, owner: Identity | None) -> None:
    """The top directory of a plugin's code or environment: root's and its group's, mode 0750, when it has a user of
    its own; mode 0755 otherwise. The host owns it, so it may change its mode without CAP_FOWNER."""
    if owner is None:
        directory.chmod(0o755)
        return
    directory.chmod(0o750)
    os.chown(directory, -1, owner.gid, follow_symlinks=False)


def extract(data: bytes, code: Path, owner: Identity | None = None) -> str:
    """Write the files of bundle ``data`` into ``code``, read-only, unless it holds them already; the bundle's
    SHA-256. PlacementError for a bundle that is not one."""
    sha256 = hashlib.sha256(data).hexdigest()
    if not _ready(code, sha256):
        try:
            contents = read_bundle(data)
        except ValueError as exc:
            raise PlacementError(f"the bundle does not read: {exc}") from None
        _mkdirs(code.parent)
        filled = Path(tempfile.mkdtemp(prefix=".extract-", dir=code.parent))
        for entry in contents.files:
            target = filled / entry.path
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.write_bytes(entry.data)
            target.chmod(0o555 if entry.executable else 0o444)
        _replace(code, filled, sha256)
    _seal(code, owner)
    return sha256


def _install_env(where: Placement) -> dict[str, str]:
    """What venv and pip run with: the host's PATH and locale, the plugin's data directory as HOME and TMPDIR, never
    the host's credentials."""
    kept = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT") if name in os.environ}
    home = {"HOME": str(where.data), "TMPDIR": str(where.data / "tmp")}
    return {**kept, **home, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PYTHONDONTWRITEBYTECODE": "1"}


def _run(command: list[str], doing: str, where: Placement, cwd: Path, owner: Identity | None = None) -> None:
    """Run one step of building an environment, in ``cwd``, under ``owner`` when given; PlacementError with the last
    lines of its output when it fails. ``cwd`` is a directory root may enter: subprocess changes into it before it
    takes ``owner``'s user, and root here cannot enter a directory of the backend's user."""
    user = {} if owner is None else {"user": owner.uid, "group": owner.gid, "extra_groups": []}
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            env=_install_env(where),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            check=False,
            **user,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PlacementError(f"cannot {doing}: {exc}") from None
    if done.returncode != 0:
        tail = " | ".join((done.stderr or done.stdout).strip().splitlines()[-3:])
        raise PlacementError(f"cannot {doing} (exit {done.returncode}): {tail[:600]}")


def _entries(tree: Path):
    """Every path under ``tree``, ``tree`` first, never following a symbolic link."""
    yield tree
    for top, directories, files in os.walk(tree, followlinks=False):
        for name in (*directories, *files):
            yield Path(top) / name


def _give(tree: Path, owner: Identity) -> None:
    """Hand ``tree``, which the host made, to the backend's user, for pip to fill under that user. Every path is listed
    first, while root owns them all: once a directory is the backend's (mode 0700), root may no longer list it. The
    environment's links (``bin/python``, ``lib64``) stay root's, so any link of that user later was not the host's."""
    for path in reversed(list(_entries(tree))):
        if not path.is_symlink():
            os.chown(path, owner.uid, owner.gid, follow_symlinks=False)


def _take_back(tree: Path, owner: Identity) -> None:
    """Make root own again what the backend's user owns under ``tree``: pip's files, read-only to the backend from
    now on. A link of that user is removed, never followed nor made root's: pip writes none, so it was planted, and
    root writing through it later would write a file outside the tree. So is a file with a set-id bit, which pip
    never writes either, and which root without CAP_FOWNER may not chown (the kernel clears the bits as it does,
    which only the owner may). A directory gets mode 0755 and a file loses any write bit but its owner's, so root
    may walk on and the backend's user, or another, can write nothing in the tree. The walk takes a directory back
    before it lists it, so once root owns a directory, no process of that user can change what is in it while the
    walk goes on."""
    for path in _entries(tree):
        info = os.lstat(path)
        if info.st_uid != owner.uid:
            continue
        directory = stat.S_ISDIR(info.st_mode)
        if stat.S_ISLNK(info.st_mode) or (not directory and info.st_mode & (stat.S_ISUID | stat.S_ISGID)):
            path.unlink()
            continue
        os.chown(path, 0, 0, follow_symlinks=False)
        os.chmod(path, 0o755 if directory else stat.S_IMODE(info.st_mode) & 0o755)


def read_requirements(where: Placement, requirements: str) -> tuple[str, Path]:
    """The file pip may read, written from the plugin's ``requirements``, and the bundle's wheels directory next to
    it; PlacementError for a file the host refuses (``evo_agents.plugins.requirements``)."""
    wanted = where.code / requirements
    try:
        text = wanted.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise PlacementError(
            f"the manifest names requirements {requirements}, which the bundle does not hold"
        ) from None
    try:
        parsed = parse_requirements(text)
    except RequirementsError as exc:
        raise PlacementError(f"{requirements}: {exc}") from None
    wheels = wanted.parent / WHEELS
    if parsed and not wheels.is_dir():
        raise PlacementError(f"{requirements} names wheels, but the bundle has no {WHEELS}/ next to it")
    return normalized(parsed), wheels


def build_env(where: Placement, requirements: str | None, owner: Identity | None = None, python: str = sys.executable):
    """Make the plugin's virtual environment, its requirements installed from the bundle's wheels under ``owner``,
    unless it is made already. ``prepare_data`` runs first: pip's HOME and TMPDIR are the plugin's."""
    lines, wheels = read_requirements(where, requirements) if requirements else ("", None)
    stamp = hashlib.sha256(lines.encode()).hexdigest() if requirements else "-"
    if _ready(where.env, stamp):
        _seal(where.env, owner)
        return
    _mkdirs(where.env.parent)
    filled = Path(tempfile.mkdtemp(prefix=".venv-", dir=where.env.parent))
    empty = _empty_dir(where.env.parent)
    try:
        venv = [python, "-I", "-m", "venv", "--without-pip", "--clear", str(filled)]
        _run(venv, f"make the environment of plugin {where.name}", where, empty)
        if lines:
            _install(where, lines, wheels, filled, empty, owner, python)
    finally:
        empty.rmdir()
    _seal(filled, owner)
    _replace(where.env, filled, stamp)


def _empty_dir(parent: Path) -> Path:
    """A new directory under ``parent`` that holds nothing and that no one but root may write, for venv and pip to run
    in: pip reads a requirement that looks like an archive's name as a file of its current directory."""
    empty = Path(tempfile.mkdtemp(prefix=".cwd-", dir=parent))
    empty.chmod(0o555)
    return empty


def _install(where: Placement, lines: str, wheels, filled: Path, cwd: Path, owner: Identity | None, python: str):
    """pip installs ``lines``, from ``wheels`` alone, into the environment ``filled`` under ``owner``, in ``cwd``;
    root owns the environment again afterwards, whatever pip did."""
    (filled / REQUIREMENTS_COPY).write_text(lines, encoding="utf-8")
    target = filled / where.python.relative_to(where.env)
    pip = [python, "-I", "-m", "pip", "--isolated", "--no-cache-dir", "--disable-pip-version-check", "--no-input"]
    pip += ["--python", str(target), "install", "--no-index", "--find-links", str(wheels), "--only-binary=:all:"]
    pip += ["--require-hashes", "-r", str(filled / REQUIREMENTS_COPY)]
    if owner is not None:
        _give(filled, owner)
    try:
        _run(pip, f"install the requirements of plugin {where.name}", where, cwd, owner)
    finally:
        if owner is not None:
            _take_back(filled, owner)


def _own_dir(directory: Path, owner: Identity) -> None:
    """``directory`` root's again (CAP_CHOWN), so the host may fix its mode and what is in it without CAP_FOWNER or
    CAP_DAC_OVERRIDE, mode 0700."""
    os.chown(directory, 0, 0, follow_symlinks=False)
    directory.chmod(0o700)


def prepare_data(where: Placement, owner: Identity | None) -> None:
    """The plugin's data directory and its tmp, mode 0700, owned by ``owner`` when it has a user of its own.

    As root without CAP_FOWNER, chmod works only on what root owns, and without CAP_DAC_OVERRIDE root cannot enter a
    directory of the backend's user: so the host takes the data directory back first, makes ``tmp`` a real directory
    (a link the backend left there is removed, never followed), sets the modes, then hands both to the backend."""
    data, tmp = where.data, where.data / "tmp"
    _mkdirs(data.parent)
    if owner is None:
        tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        data.chmod(0o700)
        tmp.chmod(0o700)
        return
    data.mkdir(mode=0o700, exist_ok=True)
    _own_dir(data, owner)
    if tmp.is_symlink() or (tmp.exists() and not tmp.is_dir()):
        tmp.unlink()
    tmp.mkdir(mode=0o700, exist_ok=True)
    _own_dir(tmp, owner)
    for directory in (tmp, data):
        os.chown(directory, owner.uid, owner.gid, follow_symlinks=False)


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


def backend_limits(owner: Identity | None) -> str:
    """The resource limits the trampoline sets, as ``name=value`` pairs; the processes of the user only when the
    backend has a user of its own (otherwise the limit would count the host's processes too)."""
    limits = {**LIMITS, **({"nproc": NPROC} if owner is not None else {})}
    return ",".join(f"{name}={value}" for name, value in limits.items())


def backend_cwd(where: Placement, owner: Identity | None) -> Path:
    """Where the backend's process starts: it starts as root, and root without CAP_DAC_READ_SEARCH cannot enter the
    data directory of the backend's user, so a backend with a user of its own starts in / and the trampoline, once it
    has taken that user, changes into its HOME, the data directory."""
    return Path("/") if owner is not None and os.name == "posix" else where.data


def user_argument(owner: Identity | None, keep_root: bool = False) -> str:
    """The user the trampoline takes, ``uid:gid``; for a backend that keeps the host's user, SAME_USER, which the
    trampoline refuses when that user is root, or KEEP_ROOT when ``keep_root`` says root may run it."""
    if owner is not None:
        return f"{owner.uid}:{owner.gid}"
    return KEEP_ROOT if keep_root else SAME_USER


def backend_command(where: Placement, entry: str, owner: Identity | None = None, keep_root: bool = False) -> list[str]:
    """The command of a backend: on POSIX the trampoline (the host's Python, isolated, closing every descriptor but
    0, 1 and 2, taking the backend's user, changing into HOME and setting the limits), then the environment's Python,
    isolated from PYTHON* variables, the user's site and the current directory, the launcher, then the entry.

    The trampoline takes the user itself (setgroups, setresgid, setresuid, then a check of the real, effective and
    saved IDs, the groups and the capabilities), rather than the host passing ``user``, ``group`` and ``extra_groups``
    to the event loop's subprocess_exec: uvloop, which uvicorn runs the host on when it is installed, refuses those
    arguments, and ignores ``close_fds``, so the trampoline closes the descriptors too, as root, before the drop.
    Without ``owner`` it refuses to go on as root, unless ``keep_root`` (``evo-agents plugin test`` only)."""
    backend = [str(where.python), "-I", "-B", "-u", "-X", "utf8", "-c", LAUNCHER, str(where.code / entry)]
    if os.name != "posix":
        return backend
    user = user_argument(owner, keep_root)
    return [sys.executable, "-I", "-S", "-c", TRAMPOLINE, user, backend_limits(owner), *backend]

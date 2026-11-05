"""How the daemon hands a run's leases over (``evo_agents.worker.credentials``, docs/credentials.md): the git
configuration and environment the run's git and agent get, the masking of the values a run holds in logs and events
(``remember``, ``forget``, ``scrub``), and the run's unix socket, both its path and binding for the daemon and ``ask``
for the processes that talk to it. Standard library only, like ``credentials``, so git's helper starts without the
worker extra."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import re
import shlex
import socket
import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

from evo_agents.hub.client import HubError, check_url
from evo_agents.hub.credential_requests import NOTED
from evo_agents.hub.credentials import (
    HUB_KIND,
    HUB_TOKEN_ENV,
    HUB_URL_ENV,
    KG_RUN,
    RUN_ACCESS,
    Lease,
    is_ssh_origin,
    matches,
    normalize_origin,
)
from evo_agents.hub.log import MASK, MIN_SECRET_LENGTH
from evo_agents.worker import logs
from evo_agents.worker.home import WorkerHome

SOCKET_NAME = "cred.sock"
SOCKET_MODE = 0o600
SOCKET_TIMEOUT = 10.0  # seconds a client of the socket has to ask, and the daemon to answer
MAX_ANSWER_BYTES = 1024 * 1024
SOCKET_PATH_BYTES = 100  # a longer socket path is bound and reached from its directory: sun_path holds 104 on macOS
CONFIG_COUNT = "GIT_CONFIG_COUNT"
_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")

_held: dict[str, set[int]] = {}  # each lease value a run of this process holds -> those runs, for ``scrub``


# What git and the agent get


def self_command() -> str:
    """This evo-agents as the start of a shell command: its console script by absolute path, or this interpreter with
    ``-m evo_agents`` behind PYTHONSAFEPATH=1, so that a worktree holding a package of that name, where git runs the
    helper, is not what gets imported."""
    from evo_agents.kg.schedule import executable

    program = executable()
    words = " ".join(shlex.quote(part) for part in program)
    return f"PYTHONSAFEPATH=1 {words}" if program[1:2] == ["-m"] else words


def helper_command(run_id: int) -> str:
    """The credential helper of run ``run_id``, as git's configuration names a shell command."""
    return f"!{self_command()} worker git-credential --run {int(run_id)}"


def env_command(run_id: int) -> str:
    """The command that prints the variables run ``run_id``'s leases add to its agent's environment."""
    return f"{self_command()} worker env --run {int(run_id)}"


def https_url(origin: str) -> str:
    """The URL the run's git reaches ``origin`` at: an https origin as it is, without a user; an http origin as the
    same URL over https, since a leased credential never goes over plain http; an SSH origin (``git@host:path``,
    ``ssh://git@host[:port]/path``) as ``https://host/path``, its path as written."""
    text = (origin or "").strip()
    scp = _SCP.match(text) if "://" not in text else None
    if scp:
        return f"https://{scp.group(1).lower()}/{scp.group(2).lstrip('/')}"
    parts = urlsplit(text)
    if parts.scheme in ("ssh", "git+ssh") and parts.hostname:
        return f"https://{parts.hostname.lower()}/{parts.path.lstrip('/')}"
    if parts.scheme in ("https", "http") and parts.hostname:
        port = f":{parts.port}" if parts.port else ""
        return f"https://{parts.hostname.lower()}{port}{parts.path}"
    return text


def _rewritten(origin: str) -> bool:
    """Whether the run's git reaches ``origin`` at another URL than its own: an SSH or a plain http origin."""
    return is_ssh_origin(origin) or urlsplit((origin or "").strip()).scheme == "http"


def covering(leases: Iterable[Lease], url: str) -> Lease | None:
    """The git lease whose url_prefix covers ``url``, the longest one; None when none does."""
    found = [lease for lease in leases if lease.kind == "git" and lease.url_prefix and matches(lease.url_prefix, url)]
    return max(found, key=lambda lease: len(normalize_origin(lease.url_prefix or "")), default=None)


def git_config(origins: Iterable[str], leases: Sequence[Lease], helper: str) -> list[tuple[str, str]]:
    """The configuration entries that hand each of ``origins`` a git lease covers to ``helper`` (see the module's
    docstring), in the order git reads them."""
    entries: list[tuple[str, str]] = []
    keyed: set[str] = set()
    for origin in dict.fromkeys(origins):
        if covering(leases, origin) is None:
            continue
        url = https_url(origin)
        if url not in keyed:
            keyed.add(url)
            entries += [
                (f"credential.{url}.helper", ""),  # empties the helpers the machine's configuration named before
                (f"credential.{url}.helper", helper),
                (f"credential.{url}.useHttpPath", "true"),
            ]
        if _rewritten(origin):
            entries.append((f"url.{url}.insteadOf", origin))
    return entries


def config_env(base: Mapping[str, str], entries: Sequence[tuple[str, str]]) -> dict[str, str]:
    """GIT_CONFIG_COUNT with GIT_CONFIG_KEY_n and GIT_CONFIG_VALUE_n for ``entries``, after the ones ``base`` sets
    already; empty without entries."""
    if not entries:
        return {}
    try:
        start = max(0, int(base.get(CONFIG_COUNT) or 0))
    except ValueError:
        start = 0
    env: dict[str, str] = {}
    for offset, (key, value) in enumerate(entries):
        env[f"GIT_CONFIG_KEY_{start + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    env[CONFIG_COUNT] = str(start + len(entries))
    return env


def run_access_env(leases: Iterable[Lease], hub_url: str | None, *, guarded: bool) -> tuple[dict[str, str], str | None]:
    """What the lease of kind HUB_KIND, the run's own token of the hub, adds to its agent's environment: the token as
    HUB_TOKEN_ENV and as HUB_URL_ENV the URL this daemon reaches the hub at (``hub_url``), else the lease's own; with
    why it adds nothing to a run that holds such a lease. ``({}, None)`` for a run that holds none. A ``guarded`` run
    takes only the token of a KG run (named ``kg-run:<project>``), which reaches its project's KG routes alone, never
    one that runs as its owner."""
    found = [lease for lease in leases if lease.kind == HUB_KIND]
    if not found:
        return {}, None
    lease = found[-1]
    if lease.provider != RUN_ACCESS or lease.env_var != HUB_TOKEN_ENV or not lease.value:
        return {}, f"the hub sent a lease of kind {HUB_KIND} that is not a run's token of {HUB_TOKEN_ENV}"
    if guarded and not lease.name.startswith(f"{KG_RUN}:"):
        return {}, "a run of the Curator or a release run never runs as its owner"
    try:
        url = check_url(hub_url or lease.base_url or "", HUB_URL_ENV)
    except HubError as exc:
        return {}, str(exc)
    return {HUB_TOKEN_ENV: lease.value, HUB_URL_ENV: url}, None


def credential_asks(home: WorkerHome, run_id: int) -> set[int]:
    """The decisions of run ``run_id`` that are credential requests, as ``evo-agents worker ask`` noted them in the
    run's file of decisions (``WorkerHome.decisions_path``)."""
    found: set[int] = set()
    with contextlib.suppress(OSError):
        for line in home.decisions_path(run_id).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                noted = json.loads(line)
                if isinstance(noted.get("id"), int) and noted.get(NOTED) is True:
                    found.add(noted["id"])
    return found


# Values out of logs and events


def remember(value: str, run_id: int) -> None:
    """Mask ``value`` in worker.log and in the events of every run of this process while run ``run_id``, or another
    run of it, holds the value."""
    if not value or len(value) < MIN_SECRET_LENGTH:
        return
    holders = _held.setdefault(value, set())
    if not holders:
        logs.mask(value)
    holders.add(int(run_id))


def forget(run_id: int) -> None:
    """Stop masking the values run ``run_id`` held, once no other run of this process holds them: the daemon's
    redaction keeps the leases of the runs in progress, not every value it was ever handed."""
    for value in [value for value, holders in _held.items() if int(run_id) in holders]:
        holders = _held[value]
        holders.discard(int(run_id))
        if not holders:
            del _held[value]
            logs.unmask(value)


def run_values(run_id: int) -> list[str]:
    """The lease values run ``run_id`` holds now, as ``remember`` keeps them."""
    return [value for value, holders in _held.items() if int(run_id) in holders]


def held_values() -> list[str]:
    """Every lease value a run of this process holds now: what an environment of code the worker does not trust must
    not hold (``untrusted.scrubbed_env``)."""
    return list(_held)


def scrub(value):
    """``value`` with every lease value a run of this process holds replaced by ``***``, in each string at any
    depth."""
    if not _held:
        return value
    return _scrub(value, sorted(_held, key=len, reverse=True))


def _scrub(value, secrets: list[str]):
    if isinstance(value, str):
        for secret in secrets:
            if secret in value:
                value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {key: _scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_scrub(item, secrets) for item in value]
    return value


# The socket


def socket_path(home: WorkerHome, run_id: int) -> Path:
    return home.run_dir(run_id) / SOCKET_NAME


def _at(path: Path, call: Callable[[str], None]) -> None:
    """``call(path)``; or, when the path is too long for a socket's address, ``call(name)`` from its directory."""
    if len(os.fsencode(str(path))) < SOCKET_PATH_BYTES:
        call(str(path))
        return
    here = os.open(".", os.O_RDONLY)
    try:
        os.chdir(path.parent)
        call(path.name)
    finally:
        os.fchdir(here)
        os.close(here)


_libc_handle = None


def _libc():
    global _libc_handle
    if _libc_handle is None:
        _libc_handle = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        uid_pointer = ctypes.POINTER(ctypes.c_uint32)
        _libc_handle.getpeereid.argtypes = (ctypes.c_int, uid_pointer, uid_pointer)
        _libc_handle.getpeereid.restype = ctypes.c_int
    return _libc_handle


def peer_uid(sock) -> int | None:
    """The uid of the process at the other end of a connected unix socket; None when the system does not say."""
    try:
        if hasattr(socket, "SO_PEERCRED"):  # Linux: pid, uid, gid
            data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            return struct.unpack("3i", data)[1]
        uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
        if _libc().getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            return None
        return int(uid.value)
    except (OSError, AttributeError, ValueError, TypeError):
        return None


def bind_socket(path: Path) -> socket.socket:
    """A listening unix socket at ``path``, mode 0600, replacing a file a daemon that died left there."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        _at(path, sock.bind)
        os.chmod(path, SOCKET_MODE)
        sock.listen(16)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


def ask(home: WorkerHome, run_id: int, request: dict, timeout: float = SOCKET_TIMEOUT) -> dict | None:
    """The answer of run ``run_id``'s socket to ``request``; None when the run holds no socket here or it does not
    answer."""
    path = socket_path(home, run_id)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    data = b""
    try:
        _at(path, sock.connect)
        sock.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        while len(data) < MAX_ANSWER_BYTES and not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        sock.close()
    try:
        answer = json.loads(data)
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None

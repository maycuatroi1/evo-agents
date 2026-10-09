"""The leases of a run on this worker: kept in the daemon's memory, handed to git and the agent, and given back
(docs/credentials.md, "How the daemon hands leases over").

- ``RunCredentials.take`` asks the hub for the run's leases (``POST /v1/worker/runs/{id}/credentials``) right after
  the claim, before the run fetches. The leases stay in the daemon's memory, never in a file, a log line or an event:
  each value is masked in worker.log (``logs.mask``) and in the run's events before they reach the spool (``scrub``)
  while a run holds it. ``release`` forgets the run's values (``forget``), each one masked no more once no other run
  of the daemon holds it, so the daemon does not keep every value it was handed for its whole life.
  A hub that does not answer is asked again with the backoff, TAKE_TRIES times in all; one that refuses, an older hub
  without the route among them, leaves the run to the machine's own credentials, as before 0.5.0.
- Each repo whose origin no lease covers gets a ``system`` event, "no leased credential for {origin}: {reason}; git
  uses this machine's own", so a laptop whose owner set no secret runs as it always did. A repo the hub names in
  ``missing`` is one of them even when a lease's url_prefix covers its origin: ``github-app:<owner>`` is for
  ``https://github.com/<owner>``, but GitHub made its token for the repos the hub leased it for alone, and git handed
  that token for another repo of the owner gets 403.
- ``git_config`` is what the run's git (``gitops.git``, ``fetch``, ``push``) and its agent get as ``GIT_CONFIG_*``
  entries, for each origin of the run's checkouts that a git lease covers, those of the repos in ``missing`` left out,
  keyed by its https URL (``https_url``):
  ``credential.<url>.helper`` empty, which empties the list of helpers for that URL and so drops the machine's own
  (osxkeychain, ``gh auth git-credential``, a store), then this run's helper (``!evo-agents worker git-credential --run
  N``, by absolute path), ``credential.<url>.useHttpPath`` true, so the helper learns which repo git asks for, and for
  an SSH or a plain http origin ``url.<url>.insteadOf`` the origin, so the run reaches it over https. The hub compares
  origins in their https form, so a lease covers an http origin too; the run's git never sends it over plain http.
  Other origins keep the machine's helpers, and the machine's git configuration is not touched. Leases of kind ``env``
  go into the agent's environment.
- The run's socket ``runs/<run>/cred.sock`` (mode 0600, in the run's directory, mode 0700) answers processes of the
  daemon's own uid alone (``peer_uid``: SO_PEERCRED on Linux, getpeereid elsewhere), one JSON request a connection.
  ``{"op": "git", "protocol", "host", "path"}`` gets the username and value of the git lease whose url_prefix covers
  that URL, the longest one, for protocol https alone, a GitHub token with less than GITHUB_TOKEN_REFRESH_SECONDS left
  asked for again first, so git never gets a token about to end; ``{"op": "env"}`` gets what the run's leases add to
  its agent's environment, for ``evo-agents worker env``, which an interactive pane evaluates rather than holding the
  values in its script. A run of the Curator answers ``op env`` only with a ticket of the pane (below).
- ``with_renewal`` runs a push of the daemon; when it fails to authenticate (``gitops.GitAuthError``) on an origin a
  lease of the run covers, ``renew`` gives the run's leases back and takes them again, which makes new GitHub tokens
  even when the ones held had time left, and the push runs once more. A second failure is the run's failure. A push
  of a repo in ``missing`` that fails to authenticate with the machine's own credentials takes the leases again the
  same way, since its project may list it by now, and runs once more only when a lease covers the repo then;
  otherwise the failure names the hub's reason ("the hub leased no credential for {repo}: {reason}").
- ``release`` gives the leases back (``DELETE`` on the same route) when the run ends, is parked or the daemon stops,
  closes the socket and forgets the values, in the redaction too.

``git_credential`` and ``print_env`` are the sides of ``evo-agents worker git-credential --run N get|store|erase``
(git's credential protocol; store and erase do nothing) and ``evo-agents worker env --run N``. This module is standard
library only, so git's helper starts without the worker extra; the daemon's side imports ``hubapi`` when it runs.

A run of the Curator (``RunCredentials.guarded``) never hands its agent a push credential, on GitHub or GitLab alike:
the agent's environment gets the env leases and git configuration that empties the list of credential helpers
(``GUARDED_AGENT_CONFIG``), never this run's helper; and the socket answers ``op git`` only for a ticket the daemon
issues for one git command of its own (``RunCredentials.ticketed``: TICKET_VARIABLE in that command's environment,
valid while the command runs, with git's hooks and fsmonitor off and https the one protocol, DAEMON_GIT_CONFIG). Its
socket answers ``op env`` likewise only for a ticket of the pane the daemon opens for a takeover (``pane_command``:
ENV_TICKET_VARIABLE on that pane's one ``evo-agents worker env``, good for one answer within PANE_TICKET_SECONDS), so
code the run does not trust, such as a verify command or a hidden check of a judge run, cannot read the env leases (a
subscription token among them) from the socket. A
Builder's agent that reports a step done asks the daemon to push instead (``{"op": "push", "repo", "title"}``,
``ask_push``): the daemon pushes that repo's own branch, curator/..., from its worktree, with the run's credential, and
answers what it pushed.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import os
import re
import secrets
import shlex
import socket
import struct
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

from evo_agents.hub.credentials import (
    DEFAULT_GIT_USERNAME,
    Lease,
    env_name_refusal,
    is_ssh_origin,
    matches,
    normalize_origin,
)
from evo_agents.hub.log import MASK, MIN_SECRET_LENGTH
from evo_agents.worker import logs
from evo_agents.worker.home import HOME_VARIABLE, WorkerHome

log = logging.getLogger("evo_agents.worker")

SOCKET_NAME = "cred.sock"
SOCKET_MODE = 0o600
DIR_MODE = 0o700
SOCKET_TIMEOUT = 10.0  # seconds a client of the socket has to ask, and the daemon to answer
MAX_REQUEST_BYTES = 16 * 1024
MAX_ANSWER_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 64 * 1024  # what git writes to its helper
SOCKET_PATH_BYTES = 100  # a longer socket path is bound and reached from its directory: sun_path holds 104 on macOS
TAKE_TRIES = 5  # asks for a run's leases while the hub does not answer, the backoff between them
RELEASE_TRIES = 5
REFRESH_TIMEOUT = 30.0  # seconds a GitHub token near its end may take to come, before git gets the one it replaces
CONFIG_COUNT = "GIT_CONFIG_COUNT"
TICKET_VARIABLE = "EVO_GIT_TICKET"  # a ticket of one git command of the daemon, which the helper hands the socket
ENV_TICKET_VARIABLE = "EVO_ENV_TICKET"  # a ticket of the pane of a guarded run, which `worker env` hands the socket
PANE_TICKET_SECONDS = 120.0  # how long a pane's ticket waits for its one `worker env`
PUSH_TIMEOUT = 900.0  # seconds a push the daemon makes for a Builder's agent may take, its renewal included
# What a guarded run's agent gets of git's configuration: no credential helper at all, for any URL, so neither the
# run's lease nor the machine's own helpers answer it.
GUARDED_AGENT_CONFIG = (("credential.helper", ""),)
# What the daemon's own git commands of a guarded run add: no hook or fsmonitor of the checkout runs in them, and they
# reach a remote over https alone, so a remote, an insteadOf or a receivepack the agent wrote into the checkout's
# configuration cannot run a command under them.
DAEMON_GIT_CONFIG = (
    ("core.hooksPath", os.devnull),
    ("core.fsmonitor", "false"),
    ("protocol.allow", "never"),
    ("protocol.https.allow", "always"),
)
MISSING_NOTE = "no leased credential for {origin}: {reason}; git uses this machine's own"
NOT_COVERED = "no lease of the run covers the origin of this worker's checkout"
GIT_ACTIONS = ("get", "store", "erase")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SCP = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")

_held: dict[str, set[int]] = {}  # each lease value a run of this process holds -> those runs, for ``scrub``
T = TypeVar("T")


def _now() -> datetime:
    return datetime.now(UTC)


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


def _bind(path: Path) -> socket.socket:
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


# The run's side, in the daemon


class RunCredentials:
    """The leases of one run in the daemon's memory, its socket, and what its git and agent get of them.

    ``note`` writes a ``system`` event of the run. ``agent_vars`` is what the leases add to the agent's environment
    (``env`` leases and the git configuration); ``git_vars`` what they add to the environment of the run's git."""

    def __init__(
        self,
        run_id: int,
        home: WorkerHome,
        base_env: Mapping[str, str],
        note: Callable[..., None],
        *,
        guarded: bool = False,
    ):
        self.run_id = int(run_id)
        # A run of the Curator: no push credential reaches its agent (see the module's docstring).
        self.guarded = guarded
        # The daemon's push of a repo for a Builder's agent, which asks for it on the socket.
        self.pusher: Callable[[str, str | None], Awaitable[dict]] | None = None
        self._tickets: set[str] = set()  # those of the daemon's git commands that run now
        self._pane_tickets: dict[str, float] = {}  # a guarded run's pane: ticket -> when it ends (time.monotonic)
        self.home = home
        self.path = socket_path(home, run_id)
        self.base_env = base_env
        self.note = note
        self.hub = None
        self.leases: list[Lease] = []
        self.missing: list[dict] = []
        self.agent_vars: dict[str, str] = {}
        self.git_vars: dict[str, str] = {}
        self.origins: dict[str, list[str]] = {}  # repo -> the URLs of its checkout's origin, as take was given them
        self.asked = False  # the hub may hold leases of the run for this worker: give them back at the end
        self._server: asyncio.AbstractServer | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:  # the values stay out of tracebacks
        return f"RunCredentials(run={self.run_id}, leases={len(self.leases)})"

    @property
    def withheld(self) -> frozenset[str]:
        """The variables of the agent's environment that come from the leases: a pane's script never holds them."""
        return frozenset(self.agent_vars)

    def pane_command(self) -> str | None:
        """What a pane's script evaluates for the variables in ``withheld``; None when the run holds none. For a
        guarded run the command carries a ticket the socket takes for one ``op env`` within PANE_TICKET_SECONDS, so
        each pane gets a command of its own."""
        if not self.agent_vars:
            return None
        command = env_command(self.run_id)
        if not self.guarded:
            return command
        now = time.monotonic()
        self._pane_tickets = {ticket: ends for ticket, ends in self._pane_tickets.items() if ends > now}
        ticket = secrets.token_urlsafe(32)
        self._pane_tickets[ticket] = now + PANE_TICKET_SECONDS
        return f"{ENV_TICKET_VARIABLE}={ticket} {command}"

    # Taking

    async def take(self, hub, origins: Mapping[str, Sequence[str]], *, stop: asyncio.Event | None = None) -> None:
        """Ask the hub for the run's leases and hand them to the run's git and agent: ``origins`` are the URLs of the
        remote origin of each repo's checkout here, as their configuration writes them."""
        from evo_agents.worker.hubapi import Backoff, Refused, Unreachable

        self.hub = hub
        self.origins = {repo: list(urls) for repo, urls in origins.items()}
        if self.pusher is not None:  # a Builder's agent pushes through the socket, leases or not
            await self._open_for_push()
        backoff = Backoff()
        answer = None
        for attempt in range(1, TAKE_TRIES + 1):
            self.asked = True
            try:
                answer = await hub.credentials(self.run_id)
                break
            except Unreachable as exc:
                if attempt == TAKE_TRIES or (stop is not None and stop.is_set()):
                    self.note(f"The hub did not answer for the run's credentials ({exc}): git uses this machine's own.")
                    return
                delay = exc.retry_after if exc.retry_after is not None else backoff.next()
                log.warning(
                    "credentials not taken; asking again",
                    extra={"run_id": self.run_id, "retry_in_s": round(delay, 1), "error": str(exc)},
                )
                await _wait_or(stop, delay)
            except Refused as exc:  # nothing was leased; an older hub without the route answers 404
                self.asked = False
                self.note(f"The hub leased this run no credentials ({exc}): git uses this machine's own.")
                return
        self._keep(answer)
        await self._serve_leases()
        self._note_missing(origins)
        log.info(
            "credentials taken",
            extra={
                "run_id": self.run_id,
                "leases": len(self.leases),
                "names": sorted(lease.name for lease in self.leases),
                "missing": len(self.missing),
            },
        )

    def _keep(self, answer) -> None:
        """Hold the leases of the hub's answer, every value masked from now on."""
        leases = []
        items = answer.get("leases") if isinstance(answer, dict) else None
        for item in items if isinstance(items, list) else []:
            try:
                lease = Lease.from_json(item)
            except (KeyError, TypeError, ValueError):
                log.warning("a lease the hub sent was not read", extra={"run_id": self.run_id})
                continue
            remember(lease.value, self.run_id)
            leases.append(lease)
        self.leases = leases
        missing = answer.get("missing") if isinstance(answer, dict) else None
        self.missing = [item for item in missing or [] if isinstance(item, dict)]

    @property
    def unleased(self) -> dict[str, str]:
        """The repos the hub leased nothing for, each with its reason. A lease whose url_prefix covers one of them
        does not answer for it: a GitHub token is ``github-app:<owner>`` for the whole owner, but GitHub made it for
        the repos the hub leased it for alone."""
        found: dict[str, str] = {}
        for item in self.missing:
            repo = str(item.get("repo") or "")
            if repo:
                found.setdefault(repo, str(item.get("reason") or "the hub gave no reason"))
        return found

    async def _serve_leases(self) -> None:
        """Hand the leases held to the run's git and agent (``_hand_over``) and answer for them on the run's socket,
        opened when it is not yet; a socket that does not open leaves git to the machine's own credentials."""
        self._hand_over(self.origins)
        if not self.leases or self._server is not None:
            return
        try:
            await self._open()
        except OSError as exc:
            log.error("the run's credential socket did not open", extra={"run_id": self.run_id, "error": str(exc)})
            self.note(
                f"The run's credential socket did not open ({type(exc).__name__}: {exc}): git uses this machine's "
                "own credentials."
            )
            self.agent_vars = {
                k: v for k, v in self.agent_vars.items() if not k.startswith("GIT_CONFIG_") or self.guarded
            }
            self.git_vars = {}

    def _hand_over(self, origins: Mapping[str, Sequence[str]]) -> None:
        """What the run's agent and git get: the env leases, and the git configuration of the leased origins, those of
        the repos the hub leased nothing for left out (``unleased``)."""
        env: dict[str, str] = {}
        for lease in self.leases:
            if lease.kind != "env" or not lease.env_var:
                continue
            refusal = env_name_refusal(lease.env_var)
            if refusal is not None:
                self.note(f"The lease {lease.name} is not put into the agent's environment: {refusal}.")
                continue
            if lease.env_var not in env:
                env[lease.env_var] = lease.value
        unleased = self.unleased
        urls = [url for repo, found in origins.items() if repo not in unleased for url in found]
        entries = git_config(urls, self.leases, helper_command(self.run_id))
        config = config_env(self.base_env, entries)
        if self.guarded:
            self.agent_vars = {**env, **config_env(self.base_env, GUARDED_AGENT_CONFIG)}
            daemon = config_env(self.base_env, [*entries, *DAEMON_GIT_CONFIG]) if entries else {}
            self.git_vars = {**daemon, HOME_VARIABLE: str(self.home.root)} if entries else {}
            return
        self.agent_vars = {**env, **config}
        self.git_vars = {**config, HOME_VARIABLE: str(self.home.root)} if config else {}

    @contextlib.contextmanager
    def ticketed(self, base: Mapping[str, str]):
        """The environment of one git command of the daemon, ``base`` with the run's git configuration; for a guarded
        run, with a ticket the socket takes for ``op git`` while the block runs and never after."""
        env = {**base, **self.git_vars}
        if not self.guarded or not self.git_vars:
            yield env
            return
        ticket = secrets.token_urlsafe(32)
        self._tickets.add(ticket)
        try:
            yield {**env, TICKET_VARIABLE: ticket}
        finally:
            self._tickets.discard(ticket)

    def _note_missing(self, origins: Mapping[str, Sequence[str]]) -> None:
        """A ``system`` event for each origin of the run git reaches with the machine's credentials."""
        said: set[str] = set()
        for item in self.missing:
            repo = str(item.get("repo") or "")
            local = list(origins.get(repo) or ())
            origin = item.get("origin") or (local[0] if local else repo)
            said.add(repo)
            reason = str(item.get("reason") or "the hub gave no reason")
            self.note(MISSING_NOTE.format(origin=origin, reason=reason), repo=repo, origin=item.get("origin"))
        for repo, urls in origins.items():
            for url in urls:
                if repo not in said and covering(self.leases, url) is None:
                    said.add(repo)
                    self.note(MISSING_NOTE.format(origin=url, reason=NOT_COVERED), repo=repo, origin=url)

    # The socket

    async def _open_for_push(self) -> None:
        if self._server is not None:
            return
        try:
            await self._open()
        except OSError as exc:
            log.error("the run's socket did not open", extra={"run_id": self.run_id, "error": str(exc)})
            self.note(
                f"The run's socket did not open ({type(exc).__name__}: {exc}): its agent cannot have a step pushed."
            )

    async def _open(self) -> None:
        self.path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.path.parent, DIR_MODE)
        sock = _bind(self.path)
        try:
            self._server = await asyncio.start_unix_server(self._serve, sock=sock, limit=MAX_REQUEST_BYTES)
        except BaseException:
            sock.close()
            raise

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = peer_uid(writer.get_extra_info("socket"))
            if uid != os.getuid():
                log.warning(
                    "the run's credential socket refused a process of another user",
                    extra={"run_id": self.run_id, "uid": uid},
                )
                return
            line = await asyncio.wait_for(reader.readline(), SOCKET_TIMEOUT)
            request = json.loads(line or b"null")
            answer = await self.answer(request if isinstance(request, dict) else {})
            writer.write(json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
            await asyncio.wait_for(writer.drain(), SOCKET_TIMEOUT)
        except (TimeoutError, ValueError, OSError) as exc:
            log.warning(
                "a request to the run's credential socket failed",
                extra={"run_id": self.run_id, "error": type(exc).__name__},
            )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def answer(self, request: dict) -> dict:
        """The socket's answer to one request (see the module's docstring)."""
        op = request.get("op")
        if op == "git":
            if self.guarded and not self._ticket_ok(request.get("ticket")):
                log.warning(
                    "the credential socket of a run of the Curator refused an ask without a ticket",
                    extra={"run_id": self.run_id},
                )
                return {}
            lease = await self.git_lease(request)
            if lease is None:
                return {}
            return {"username": lease.username or DEFAULT_GIT_USERNAME, "password": lease.value}
        if op == "env":
            if self.guarded and not self._pane_ticket_ok(request.get("ticket")):
                log.warning(
                    "the credential socket of a run of the Curator refused op env without a ticket of its pane",
                    extra={"run_id": self.run_id},
                )
                return {}
            return {"env": dict(self.agent_vars)}
        if op == "push":
            return await self._push(request)
        return {"error": "the socket answers op git, op env and op push"}

    def _ticket_ok(self, ticket) -> bool:
        """Whether ``ticket`` is one of a git command of the daemon that runs now."""
        return isinstance(ticket, str) and any(secrets.compare_digest(ticket, held) for held in self._tickets)

    def _pane_ticket_ok(self, ticket) -> bool:
        """Whether ``ticket`` is one of a pane of the run, not used yet and not past its time; it is used up."""
        if not isinstance(ticket, str):
            return False
        now = time.monotonic()
        held = next((item for item in self._pane_tickets if secrets.compare_digest(ticket, item)), None)
        if held is None:
            return False
        ends = self._pane_tickets.pop(held)
        return ends > now

    async def _push(self, request: dict) -> dict:
        """A Builder's agent asks the daemon to push the run's branch of a repo (``pusher``)."""
        repo, title = request.get("repo"), request.get("title")
        if self.pusher is None:
            return {
                "error": "this run pushes nothing for its agent: only a Builder of the Curator asks the daemon to push"
            }
        if not isinstance(repo, str) or not repo:
            return {"error": "name the repo to push"}
        try:
            return await asyncio.wait_for(self.pusher(repo, title if isinstance(title, str) else None), PUSH_TIMEOUT)
        except TimeoutError:
            return {"error": f"the push of {repo} took more than {PUSH_TIMEOUT:g}s"}

    async def git_lease(self, request: Mapping) -> Lease | None:
        """The git lease for the URL of a request of git's helper, asked for again first when it is a GitHub token
        near its end."""
        protocol, host = str(request.get("protocol") or ""), str(request.get("host") or "")
        path = str(request.get("path") or "")
        if protocol != "https" or not host:  # a lease never goes over plain http, whatever the hub's prefix covers
            return None
        lease = self._choose(protocol, host, path)
        if lease is not None and lease.needs_refresh(_now()):
            await self.refresh()
            lease = self._choose(protocol, host, path)
        return lease

    def _choose(self, protocol: str, host: str, path: str) -> Lease | None:
        lease = covering(self.leases, f"{protocol}://{host}/{path.lstrip('/')}")
        if lease is not None or path:
            return lease
        # Without the path (a git that did not send it) only a lease alone on the host can be meant.
        same = [item for item in self.leases if item.kind == "git" and _host_of(item.url_prefix) == host.lower()]
        return same[0] if len(same) == 1 else None

    async def refresh(self) -> None:
        """Ask again for the leases when a GitHub token among them nears its end; the old ones stay when the hub does
        not answer, since they work until they end."""
        async with self._lock:
            if self.hub is None or not any(lease.needs_refresh(_now()) for lease in self.leases):
                return
            try:
                answer = await asyncio.wait_for(self.hub.credentials(self.run_id), REFRESH_TIMEOUT)
            except Exception as exc:  # HubProblem or a timeout: git gets the token it has
                log.warning(
                    "a GitHub token near its end was not asked for again",
                    extra={"run_id": self.run_id, "error": f"{type(exc).__name__}: {exc}"},
                )
                return
            self._keep(answer)
            log.info("credentials asked for again", extra={"run_id": self.run_id, "leases": len(self.leases)})

    def covers(self, repo: str) -> bool:
        """Whether a git lease of the run covers the origin of ``repo``'s checkout, a repo the hub leased nothing for
        never (``unleased``)."""
        if repo in self.unleased:
            return False
        return any(covering(self.leases, url) is not None for url in self.origins.get(repo) or ())

    async def renew(self) -> bool:
        """Give the run's leases back and take them again: the hub revokes the GitHub tokens held, so the ask makes
        new ones, and a secret comes with its value as it is now. Whether the hub answered both."""
        from evo_agents.worker.hubapi import HubProblem

        async with self._lock:
            if self.hub is None:
                return False
            try:
                await self.hub.release_credentials(self.run_id)
                answer = await self.hub.credentials(self.run_id)
            except HubProblem as exc:
                log.warning("the run's leases were not taken again", extra={"run_id": self.run_id, "error": str(exc)})
                return False
            self._keep(answer)
            await self._serve_leases()
            log.info("credentials taken again", extra={"run_id": self.run_id, "leases": len(self.leases)})
            return True

    async def with_renewal(self, repo: str, push: Callable[[], Awaitable[T]]) -> T:
        """``push()``, a push of the daemon to ``repo``'s origin; when the origin refuses its credential and a lease of
        the run covers it, the leases are taken again once (``renew``) and ``push()`` runs once more. A repo the hub
        leased nothing for (``unleased``) is asked for again the same way, since its project may list it by now, and
        pushed once more only when a lease covers it then; otherwise its failure names the hub's reason. The second
        failure, like any other, goes to the caller."""
        from evo_agents.worker.gitops import GitAuthError

        try:
            return await push()
        except GitAuthError as exc:
            leased = self.covers(repo)
            reason = self.unleased.get(repo)
            if not leased and reason is None:
                raise
            if leased:
                self.note(
                    f"The push of {repo} failed to authenticate ({exc}): the run gives its leases back, takes them "
                    "again and pushes once more.",
                    repo=repo,
                )
            else:
                self.note(
                    f"The push of {repo} failed to authenticate with this machine's credentials ({exc}), and the hub "
                    f"leased none for it ({reason}): the run asks the hub again and pushes once more if a lease "
                    "covers it now.",
                    repo=repo,
                )
            renewed = await self.renew()
            if not leased and not (renewed and self.covers(repo)):
                reason = self.unleased.get(repo, reason)
                raise GitAuthError(f"{exc}; the hub leased no credential for {repo}: {reason}") from None
            if not renewed:
                raise
        return await push()

    # Giving back

    async def close(self) -> None:
        """Stop answering on the socket and remove it."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), SOCKET_TIMEOUT)
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)

    async def release(self, *, stop: asyncio.Event | None = None, tries: int = RELEASE_TRIES) -> None:
        """Close the socket, give the leases back to the hub and forget them, values included (``forget``); once."""
        await self.close()
        self.leases, self.agent_vars, self.git_vars = [], {}, {}
        self._pane_tickets.clear()
        forget(self.run_id)
        if not self.asked or self.hub is None:
            return
        self.asked = False
        from evo_agents.worker.hubapi import Backoff, HubProblem, Unreachable

        backoff = Backoff()
        for attempt in range(1, max(1, tries) + 1):
            try:
                answer = await self.hub.release_credentials(self.run_id)
            except Unreachable as exc:
                if attempt >= tries or (stop is not None and stop.is_set() and attempt >= 2):
                    log.warning(
                        "leases not given back: the hub takes them back itself once the run ended",
                        extra={"run_id": self.run_id, "error": str(exc)},
                    )
                    return
                await _wait_or(stop, exc.retry_after if exc.retry_after is not None else backoff.next())
                continue
            except HubProblem as exc:
                log.warning("leases not given back", extra={"run_id": self.run_id, "error": str(exc)})
                return
            log.info("leases given back", extra={"run_id": self.run_id, "revoked": answer.get("revoked")})
            return


def _host_of(url: str | None) -> str | None:
    parts = urlsplit(normalize_origin(url or ""))
    if not parts.hostname:
        return None
    return f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname


async def _wait_or(event: asyncio.Event | None, seconds: float) -> None:
    if event is None:
        await asyncio.sleep(max(0.0, seconds))
        return
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(event.wait(), max(0.0, seconds))


# The commands


def read_attributes(stream) -> dict[str, str]:
    """git's ``key=value`` lines up to an empty line or the end, ``url=`` taken apart into protocol, host and path."""
    found: dict[str, str] = {}
    size = 0
    for line in stream:
        size += len(line)
        line = line.rstrip("\r\n")
        if not line or size > MAX_INPUT_BYTES:
            break
        key, sep, value = line.partition("=")
        if sep:
            found[key] = value
    url = found.get("url")
    if url and "host" not in found:
        parts = urlsplit(url)
        found.setdefault("protocol", parts.scheme)
        if parts.hostname:
            found["host"] = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
        found.setdefault("path", parts.path.lstrip("/"))
    return found


def _line_safe(value) -> bool:
    return isinstance(value, str) and bool(value) and not any(char in value for char in "\n\r\0")


def git_credential(run_id: int, action: str, stdin, stdout, stderr, home: WorkerHome | None = None) -> int:
    """``evo-agents worker git-credential --run N ACTION``: for ``get``, the username and password of run N's lease
    for what git asks, from the run's socket; ``store``, ``erase`` and any other action read git's lines and do
    nothing. Nothing is printed when the run holds no lease for it: git then fails to authenticate."""
    attributes = read_attributes(stdin)
    if action != "get":
        return 0
    request = {"op": "git", **{key: attributes.get(key, "") for key in ("protocol", "host", "path")}}
    ticket = os.environ.get(TICKET_VARIABLE)
    if ticket:
        request["ticket"] = ticket
    answer = ask(home or WorkerHome(), run_id, request)
    if answer is None:
        stderr.write(f"evo-agents: run {run_id} holds no credentials on this worker\n")
        return 0
    username, password = answer.get("username"), answer.get("password")
    if _line_safe(username) and _line_safe(password):
        stdout.write(f"username={username}\npassword={password}\n")
    return 0


def ask_push(home: WorkerHome, run_id: int, repo: str, title: str | None) -> dict:
    """``evo-agents worker step`` of a Builder of the Curator: ask the daemon to push the run's branch of ``repo``;
    its answer (``branch``, ``head``, ``changed``, ``default``, ``commits``, or ``error``)."""
    answer = ask(home, run_id, {"op": "push", "repo": repo, "title": title}, timeout=PUSH_TIMEOUT + SOCKET_TIMEOUT)
    return answer if answer is not None else {"error": f"run {run_id} holds no socket on this worker to push through"}


def print_env(
    run_id: int, stdout, stderr, home: WorkerHome | None = None, environ: Mapping[str, str] | None = None
) -> int:
    """``evo-agents worker env --run N``: ``export NAME=value`` lines of what run N's leases add to its agent's
    environment, for a shell to evaluate; 1 when the run holds no socket here, or answers nothing (a run of the
    Curator asked without its pane's ticket, ENV_TICKET_VARIABLE)."""
    request: dict = {"op": "env"}
    ticket = (os.environ if environ is None else environ).get(ENV_TICKET_VARIABLE)
    if ticket:
        request["ticket"] = ticket
    answer = ask(home or WorkerHome(), run_id, request)
    env = answer.get("env") if isinstance(answer, dict) else None
    if not isinstance(env, dict):
        stderr.write(f"evo-agents: run {run_id} holds no credentials on this worker\n")
        return 1
    for key, value in sorted(env.items()):
        if _NAME.match(str(key)) and isinstance(value, str) and "\0" not in value:
            stdout.write(f"export {key}={shlex.quote(value)}\n")
    return 0

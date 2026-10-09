"""Credentials as the hub models them: the secrets a member keeps on the hub, the leases a run gets of them, and the
rules both ends check. Standard library only, so the api and the worker daemon share it; ``docs/credentials.md``
describes the design built on it.

A secret is a value its owner writes once into the hub and never reads back: kind ``env`` puts it into the agent's
environment as ``env_var``; kind ``git`` answers git's credential requests for origins under ``url_prefix`` with
``username`` and the value as password. A secret is bound to projects (and, optionally, to workers); a run of one of
those projects, on one of those workers, gets a lease of it. The hub also leases tokens it makes itself: provider
``github-app`` is an installation token of the hub's GitHub App, for the run's repos on github.com only, that lives an
hour and is revoked when the run ends; a review run's and a judge run's read only (``github_permissions``). A run of
the Curator (a review run, a judge run, or a plan run of a plan the Curator made, ``evo_agents.hub.judge``) gets its
token from a second App, the Curator's own (EVO_HUB_CURATOR_APP_ID and EVO_HUB_CURATOR_APP_PRIVATE_KEY), which the
rulesets of the repos keep off their default branches, and never from the first: without the Curator's App it gets
none. On other forges it gets the one git secret the charter names (``git_secret``) and the env secrets the charter
lists (``env_secrets``), none of the owner's others.

Origins are compared in one form: ``normalize_origin`` turns ``git@host:path``, ``ssh://git@host/path`` and
``https://host/path.git`` into ``https://host/path``, so a secret whose url_prefix is https also covers a repo whose
origin is SSH, which the daemon rewrites to https for the run's git only.

A worker's ``dispatch_from`` says who may hand it runs: ``any`` credential of its owner, or a ``web`` session only, so
a machine token that leaked cannot put work onto it. A run's ``dispatched_via`` is the credential it was dispatched
with (``dispatch_credential``), or ``schedule`` for a run the night shift queued; a worker set to ``web`` claims only
runs whose dispatched_via is ``web``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

SECRET_KINDS = ("env", "git")  # a variable of the agent's environment, or a credential git asks for
PROVIDERS = ("secret", "github-app")  # a member's secret, or an installation token the hub's GitHub App makes

ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
# Variables a secret may not set: they steer the shell, the daemon, git or the interpreter rather than the agent.
DENIED_ENV = frozenset({"PATH", "HOME", "SHELL", "USER", "TMPDIR", "SSH_AUTH_SOCK"})
DENIED_ENV_PREFIXES = ("EVO_", "GIT_", "LD_", "DYLD_", "PYTHON")

GITHUB_HOST = "github.com"
GITHUB_PERMISSIONS = {"contents": "write", "metadata": "read"}  # all an installation token of a run may do
GITHUB_READ_PERMISSIONS = {"contents": "read", "metadata": "read"}  # all the token of a review run may do: read
READ_ONLY_KINDS = ("review", "judge")  # the kinds of run whose GitHub token reads only (runs.RUN_KINDS)


def github_permissions(run_kind: str) -> dict[str, str]:
    """What the GitHub token of a run of ``run_kind`` may do: read only for a review run and a judge run, which push
    nothing."""
    return dict(GITHUB_READ_PERMISSIONS if run_kind in READ_ONLY_KINDS else GITHUB_PERMISSIONS)


GITHUB_TOKEN_REFRESH_SECONDS = 600  # the daemon asks again for a GitHub token with less than this left

MAX_SECRET_BYTES = 16384  # one secret's value, as UTF-8
MAX_SECRETS_PER_OWNER = 200  # secrets one member keeps, deleted ones not counted
MAX_SECRET_NAME_CHARS = 64
SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DEFAULT_GIT_USERNAME = "oauth2"  # what GitLab takes with a project or personal access token as password

_SCP_ORIGIN = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]+):(?!//)(.+)$")

# Who may hand a worker its runs (workers.dispatch_from): runs dispatched with any credential of its owner, or only
# those dispatched from a web session. And the credential a run was dispatched with (runs.dispatched_via): a web
# session, a token (a machine token, or the worker token of a run's agent on /mcp), or none, the night shift of a
# project's charter acting for its owner (schema 0012, evo_agents.hub.curator).
DISPATCH_FROM = ("any", "web")
DISPATCHED_VIA = ("machine", "web", "schedule")


def dispatch_credential(credential_kind: str) -> str:
    """The ``dispatched_via`` of a dispatch made with a credential of ``credential_kind``, one of the kinds of
    ``evo_agents.hub.server.security``: ``web`` for a web session, ``machine`` for any token."""
    return "web" if credential_kind == "web" else "machine"


def env_name_refusal(name: str) -> str | None:
    """Why ``name`` cannot be a secret's env_var, or None when it can."""
    if not ENV_NAME.fullmatch(name or ""):
        return f"{name!r} is not a variable name: upper case letters, digits and _, not starting with a digit"
    if name in DENIED_ENV or name.startswith(DENIED_ENV_PREFIXES):
        return f"{name} steers the shell, git or the worker and cannot be set from a secret"
    return None


def normalize_origin(origin: str) -> str:
    """``origin`` as ``https://host/path``: no user, no port of SSH, no ``.git``, no trailing slash, host in lower case.

    ``git@host:path``, ``ssh://git@host[:port]/path`` and ``https://[user@]host/path(.git)`` all map onto the same
    form. A string that is none of these comes back stripped, so it matches nothing but itself."""
    text = (origin or "").strip()
    scp = _SCP_ORIGIN.match(text) if "://" not in text else None
    if scp:
        host, path = scp.group(1), scp.group(2)
        port = ""
    else:
        parts = urlsplit(text)
        if parts.scheme not in ("https", "http", "ssh", "git+ssh") or not parts.hostname:
            return text.rstrip("/")
        host, path = parts.hostname, parts.path
        # The port of an https origin is part of where it is; the port of SSH says nothing about the https one.
        port = f":{parts.port}" if parts.port and parts.scheme in ("https", "http") else ""
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"https://{host.lower()}{port}/{path}".rstrip("/")


def is_ssh_origin(origin: str) -> bool:
    text = (origin or "").strip()
    return text.startswith(("ssh://", "git+ssh://")) or ("://" not in text and bool(_SCP_ORIGIN.match(text)))


def matches(url_prefix: str, origin: str) -> bool:
    """Whether a git secret for ``url_prefix`` covers ``origin``: the normalized prefix is the normalized origin, or
    a whole leading part of its path (``https://h/group`` covers ``https://h/group/repo``, not ``https://h/groupie``)."""
    prefix = normalize_origin(url_prefix)
    target = normalize_origin(origin)
    if not prefix.startswith("https://") or not target.startswith("https://"):
        return False
    return target == prefix or target.startswith(prefix + "/")


def github_repo(origin: str) -> tuple[str, str] | None:
    """``(owner, repo)`` of an origin on github.com, or None for any other host."""
    url = urlsplit(normalize_origin(origin))
    if url.hostname != GITHUB_HOST:
        return None
    parts = url.path.strip("/").split("/")
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


@dataclass(frozen=True)
class Lease:
    """One credential a run holds: an env var for its agent, or what git answers for origins under url_prefix.

    ``value`` is the secret itself; ``repr`` leaves it out, so a lease that reaches a log or a traceback does not
    carry it."""

    id: int
    kind: str  # one of SECRET_KINDS
    provider: str  # one of PROVIDERS
    name: str = ""  # the secret's name, or ``github-app:owner``
    env_var: str | None = None
    url_prefix: str | None = None
    username: str | None = None
    value: str = field(default="", repr=False)
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        target = self.env_var if self.kind == "env" else self.url_prefix
        return (
            f"Lease(id={self.id}, kind={self.kind!r}, provider={self.provider!r}, name={self.name!r}, "
            f"target={target!r}, expires_at={self.expires_at.isoformat() if self.expires_at else None})"
        )

    __str__ = __repr__

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "provider": self.provider,
            "name": self.name,
            "env_var": self.env_var,
            "url_prefix": self.url_prefix,
            "username": self.username,
            "value": self.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

    @classmethod
    def from_json(cls, data: dict) -> Lease:
        expires = data.get("expires_at")
        return cls(
            id=int(data["id"]),
            kind=str(data["kind"]),
            provider=str(data.get("provider") or "secret"),
            name=str(data.get("name") or ""),
            env_var=data.get("env_var"),
            url_prefix=data.get("url_prefix"),
            username=data.get("username"),
            value=str(data.get("value") or ""),
            expires_at=datetime.fromisoformat(expires) if expires else None,
        )

    def seconds_left(self, now: datetime) -> float | None:
        return None if self.expires_at is None else (self.expires_at - now).total_seconds()

    def needs_refresh(self, now: datetime) -> bool:
        """Whether the daemon should ask for this lease again: a GitHub token close to its end."""
        left = self.seconds_left(now)
        return self.provider == "github-app" and left is not None and left < GITHUB_TOKEN_REFRESH_SECONDS

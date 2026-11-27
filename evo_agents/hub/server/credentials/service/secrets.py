"""The owner's secrets: PUT, GET and DELETE under /v1/secrets, where a member writes a value the hub keeps sealed for
the runs of their projects and never hands back (``docs/credentials.md``).

PUT /v1/secrets/{name} creates the caller's secret of that name or replaces it whole, value included. Kind ``env``
names the variable of the agent's environment it sets (``env_var``, never one of
``evo_agents.hub.credentials.DENIED_ENV`` or its prefixes); kind ``git`` names the https prefix of the origins it
answers for (``url_prefix``, kept in the form of ``credentials.normalize_origin``) and the user git sends with it
(``username``, DEFAULT_GIT_USERNAME when left out); kind ``forge`` names the https prefix of the GitLab projects whose
merge requests the hub drives with it (``url_prefix``, as for git, and no username: GitLab takes the token in a
header), and is never leased to a run (``evo_agents.hub.server.forge.service.secrets``). ``projects`` binds it to projects the caller
holds the writer role on, the admin role for kind forge; left out or empty, the secret is personal: kept for its owner
alone and leased to no run until it is bound to a project, by a PUT naming its projects or when a run of its owner asks
for its variable (``evo_agents.hub.server.credentials.service.resolver``). A forge secret needs a project, and a personal secret
names no workers or pools. ``workers`` narrows it to some of the caller's own workers that are not revoked, by
name, and left out means any worker of the caller; a forge secret names none, as no worker ever gets it. ``expires_at`` is the end its owner gives it, which kind forge needs. The value is 1 to
MAX_SECRET_BYTES bytes of UTF-8, sealed with ``Sealer`` under the associated data of its owner, name and kind before
it reaches the database; without EVO_HUB_SECRETS_KEY the route answers 503 and keeps nothing. A member keeps at most
MAX_SECRETS_PER_OWNER secrets.

PATCH /v1/secrets/{name} replaces the binding of the caller's secret ``name`` alone, without its value: ``workers``
or ``pools``, exactly one of them, checked as PUT checks them, an empty list binding it to any worker of its owner
again. Its value, kind, target, projects, end, OAuth tokens and shares stay, and so does ``updated_at``, the time its
value was written; the leases already out stay too, as after a PUT. A secret the caller does not own is 404, and the
name of a provider account 409.

GET /v1/secrets lists the caller's own secrets that are not deleted: name, kind, target, bindings and dates, never the
value, sealed or not. A hub admin sees only their own too; /v1/admin/stats counts the rows of every table and shows
none; each comes with its shares not revoked (``evo_agents.hub.server.share_views``). DELETE /v1/secrets/{name} deletes
softly: the row stays, for the leases and the audit lines that name it, while its sealed value and its bindings go,
and the leases and shares of it that are not given back or revoked yet are marked revoked. The name
is free again at once.

The key of a provider account is a secret of kind provider (``evo_agents.hub.server.provider_accounts``), which these
routes neither list, write nor delete: a PUT of its name answers 409, a DELETE 404.

An env secret for CLAUDE_CODE_OAUTH_TOKEN may carry ``oauth``, the refresh token and other parts of an account's
sign-in with OAuth (``evo_agents.hub.server.account_oauth``): the hub seals them with the value in the account's row,
and a write without ``oauth`` drops what was there.

Each write adds one audit row, secret.put, secret.rebind or secret.delete, whose target is the secret's id
(``secret:<id>``), never its name: the audit is for hub admins, and a secret's name is for its owner alone, as GET
/v1/secrets and a run's credentials are; secret.rebind names the new binding after the id (``binding_text``). No
audit row, log line or error holds a value. A write made with a web session needs X-Evo-CSRF, as every write does
(``evo_agents.hub.server.security``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import psycopg
from fastapi import HTTPException, Request
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.credentials import (
    DEFAULT_GIT_USERNAME,
    FORGE_KIND,
    GITHUB_HOST,
    MAX_SECRET_BYTES,
    MAX_SECRETS_PER_OWNER,
    PROVIDER_KIND,
    env_name_refusal,
    normalize_origin,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.models.oauth import OAUTH_ENV_VAR, OAuthTokens
from evo_agents.hub.server.accounts.repository.choice import clear_relogin
from evo_agents.hub.server.accounts.repository.oauth import drop_tokens, seal_tokens, store_tokens
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.credentials.models.sealing import Sealed, Sealer, secret_aad
from evo_agents.hub.server.credentials.models.secrets import (
    MAX_URL_PREFIX_CHARS,
    NO_KEY,
    NO_SECRET,
    PROVIDER_HOLDS,
    REBIND_BOTH,
    REBIND_NEITHER,
    URL_PREFIX,
    Secret,
    SecretRebind,
    SecretWrite,
    SecretWritten,
)
from evo_agents.hub.server.credentials.repository import secrets as repository
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds
from evo_agents.hub.server.share_views import shares_of

log = logging.getLogger(__name__)


# Reading secrets


async def owned_secrets(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list[Secret]:
    """The live secrets of ``owner_id`` as their owner sees them (``name`` alone when given), each with its shares."""
    rows = await repository.owned_secret_rows(conn, owner_id, name)
    shares = await shares_of(conn, [row.id for row in rows])
    return [Secret(**row._mapping, shares=shares.get(row.id, [])) for row in rows]


# Writing secrets


def sealer_of(request: Request) -> Sealer:
    sealer: Sealer | None = request.app.state.sealer
    if sealer is None:
        missing = ", ".join(request.app.state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, NO_KEY.format(missing=missing))
    return sealer


def _url_prefix(text: str) -> str:
    """``text`` in the form the secrets table keeps, ``https://host[:port][/path]``; 422 for anything else. No message
    repeats the URL, which may hold a credential."""
    try:
        parts = urlsplit(text.strip())
        port = parts.port
    except ValueError:
        parts, port = None, None
    if parts is None or parts.scheme.lower() != "https" or not parts.hostname:
        raise Unprocessable("url_prefix must be an https URL, such as https://gitlab.example.org/group")
    if parts.username is not None or parts.password is not None:
        raise Unprocessable(
            "url_prefix may not hold a user or a password: send the user as username and the token as value"
        )
    if parts.query or parts.fragment or (port is not None and not 0 < port < 65536):
        raise Unprocessable("url_prefix is a host and a path, without a query, a fragment or a port out of range")
    prefix = normalize_origin(text)
    if len(prefix) > MAX_URL_PREFIX_CHARS or not URL_PREFIX.fullmatch(prefix):
        raise Unprocessable(
            "url_prefix must be https://host[:port][/path], whose host has letters, digits, '.', '-' and '_' only"
        )
    return prefix


def audit_target(secret_id: int) -> str:
    """How an audit row names a secret: by its id, never its name, which is its owner's to see; the audit is read by
    hub admins."""
    return f"secret:{secret_id}"


def _target(body: SecretWrite) -> tuple[str | None, str | None, str | None]:
    """(env_var, url_prefix, username) as the secrets table keeps them; 422 for fields of the other kind."""
    if body.kind == "env":
        if body.url_prefix is not None or body.username is not None:
            raise Unprocessable("url_prefix and username belong to a secret of kind git, not env")
        if body.env_var is None:
            raise Unprocessable("a secret of kind env needs env_var, the variable it sets")
        refusal = env_name_refusal(body.env_var)
        if refusal is not None:
            raise Unprocessable(refusal)
        return body.env_var, None, None
    for field_name in ("env_var", "environment"):
        if getattr(body, field_name) is not None:
            raise Unprocessable(f"{field_name} belongs to a secret of kind env, not {body.kind}")
    if body.url_prefix is None:
        raise Unprocessable(
            f"a secret of kind {body.kind} needs url_prefix, the https prefix of the origins it answers for"
        )
    if body.kind == FORGE_KIND:
        if body.username is not None:
            raise Unprocessable("a secret of kind forge takes no username: GitLab reads the token from a header")
        prefix = _url_prefix(body.url_prefix)
        if urlsplit(prefix).hostname == GITHUB_HOST:
            raise Unprocessable(
                "a secret of kind forge is for GitLab: on github.com the hub merges by the Curator's tiers"
            )
        return None, prefix, None
    return None, _url_prefix(body.url_prefix), body.username or DEFAULT_GIT_USERNAME


def _forge_refusal(body: SecretWrite) -> str | None:
    """Why ``body`` cannot be a forge secret, or None: it needs an end; its binding is ``binding_refusal``'s."""
    if body.kind == FORGE_KIND and body.expires_at is None:
        return "a secret of kind forge needs expires_at: set the end of the GitLab token it holds"
    return None


def _oauth(body: SecretWrite) -> OAuthTokens | None:
    """The OAuth tokens of ``body``, which only an env secret for CLAUDE_CODE_OAUTH_TOKEN may carry; 422 otherwise."""
    oauth = body.oauth
    if oauth is None:
        return None
    if body.kind != "env" or body.env_var != OAUTH_ENV_VAR:
        raise Unprocessable(f"oauth belongs to a secret of kind env for {OAUTH_ENV_VAR}")
    if body.expires_at is not None and body.expires_at != oauth.access_expires_at:
        raise Unprocessable("expires_at of an account of OAuth is the end of its access token: leave it out")
    return oauth


def _value(body: SecretWrite) -> str:
    value = body.value.get_secret_value()
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise Unprocessable(f"a secret's value is at most {MAX_SECRET_BYTES} bytes of UTF-8")
    if "\x00" in value:
        raise Unprocessable("a secret's value may not hold a NUL character")
    if body.kind == "git" and ("\n" in value or "\r" in value):
        raise Unprocessable("the value of a secret of kind git is one line: git's credential protocol has no other")
    if body.kind == FORGE_KIND and ("\n" in value or "\r" in value):
        raise Unprocessable("the value of a secret of kind forge is one line: an HTTP header holds it")
    return value


def unique_names(values: list[str]) -> list[str]:
    seen, kept = set(), []
    for value in values:
        if value.lower() not in seen:
            seen.add(value.lower())
            kept.append(value)
    return kept


async def writable_projects(conn: AsyncConnection, user: Principal, names: list[str], kind: str) -> list[int]:
    """The ids of the projects ``names``; 404 for one ``user`` cannot see, 403 for one without the writer role, or
    without the admin role for a secret of kind forge, which merges into the project's repos."""
    needed = "admin" if kind == FORGE_KIND else "writer"
    ids = []
    for name in names:
        access = await project_access(conn, user, name)
        if not holds(access.role, needed):
            role = access.role or "no role"
            raise Forbidden(
                f"a secret of kind {kind} for project {name} needs the {needed} role on it; you hold {role}"
            )
        ids.append(access.project_id)
    return ids


async def own_workers(conn: AsyncConnection, user: Principal, names: list[str]) -> list[int]:
    """The ids of the caller's workers ``names`` that are not revoked; 403 for any other name, whoever's it is."""
    if not names:
        return []
    found = await repository.worker_ids(conn, user.user_id, names)
    for name in names:
        if name.lower() not in found:
            raise Forbidden(
                f"{name} is not a worker of yours: a secret binds only to your own workers that are not revoked "
                "(GET /v1/workers)",
            )
    return [found[name.lower()] for name in names]


async def own_pools(conn: AsyncConnection, user: Principal, names: list[str]) -> list[int]:
    """The ids of the caller's worker pools ``names``; 403 for any other name, whoever's it is."""
    if not names:
        return []
    found = await repository.pool_ids(conn, user.user_id, names)
    for name in names:
        if name.lower() not in found:
            raise Forbidden(
                f"{name} is not a worker pool of yours: a secret binds only to your own pools (GET /v1/worker-pools)",
            )
    return [found[name.lower()] for name in names]


def both_refusal(workers: list[str], pools: list[str]) -> str | None:
    """Why a binding cannot name ``workers`` and ``pools`` both, or None."""
    if workers and pools:
        return "bind it to workers or to pools, not both: a pool covers each of its nodes already"
    return None


def personal_refusal(kind: str, projects: list[str], workers: list[str], pools: list[str]) -> str | None:
    """Why a secret bound to no project cannot be of ``kind`` or name ``workers`` or ``pools``, or None: a personal
    secret is leased to no run, and a forge secret is for the merge requests of its projects."""
    if projects:
        return None
    if kind == FORGE_KIND:
        return "a secret of kind forge needs a project: the hub drives the merge requests of its projects with it"
    if workers or pools:
        return "a secret bound to no project takes no workers or pools: no run gets it until it is bound to a project"
    return None


def binding_refusal(kind: str, workers: list[str], pools: list[str]) -> str | None:
    """Why a secret of ``kind`` cannot be bound to ``workers`` and ``pools``, or None: a forge secret binds to none, as
    no worker ever gets it, and no secret binds to workers and pools both."""
    if kind == FORGE_KIND and (workers or pools):
        return "a secret of kind forge binds to no worker or pool: only the hub uses it, and no run is ever leased it"
    return both_refusal(workers, pools)


@dataclass(frozen=True)
class Checked:
    """A write of a secret as ``checked_write`` read it, its value sealed: what the transaction writes."""

    target: tuple[str | None, str | None, str | None]  # env_var, url_prefix, username
    value: str
    oauth: OAuthTokens | None
    expires_at: datetime | None
    projects: list[str]
    workers: list[str]
    pools: list[str]
    sealed: Sealed

    def __repr__(self) -> str:  # the value stays out of tracebacks and logs
        return f"Checked(target={self.target!r}, value=***)"


def checked_write(sealer: Sealer, user: Principal, name: str, body: SecretWrite) -> Checked:
    """``body``, a write of the caller's secret ``name``, read and its value sealed before any transaction; 422 for
    what it cannot be."""
    target = _target(body)
    value = _value(body)
    oauth = _oauth(body)
    expires_at = oauth.access_expires_at if oauth is not None else body.expires_at
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise Unprocessable("expires_at is past already: a secret's end is in the future")
    refusal = (
        _forge_refusal(body)
        or binding_refusal(body.kind, body.workers, body.pools)
        or personal_refusal(body.kind, body.projects, body.workers, body.pools)
    )
    if refusal is not None:
        raise Unprocessable(refusal)
    projects, workers, pools = unique_names(body.projects), unique_names(body.workers), unique_names(body.pools)
    sealed = sealer.seal(value, secret_aad(user.user_id, name, body.kind))
    return Checked(target, value, oauth, expires_at, projects, workers, pools, sealed)


async def put(
    conn: AsyncConnection, sealer: Sealer, user: Principal, name: str, body: SecretWrite, write: Checked
) -> SecretWritten:
    """Create the caller's secret ``name`` or replace it whole, in the caller's transaction, as ``write`` says; one
    audit row secret.put."""
    env_var, url_prefix, username = write.target
    sealed = write.sealed
    project_ids = await writable_projects(conn, user, write.projects, body.kind)
    worker_ids = await own_workers(conn, user, write.workers)
    pool_ids = await own_pools(conn, user, write.pools)
    await repository.lock_owner(conn, user.user_id)
    if await repository.provider_holds(conn, user.user_id, name):
        raise Conflict(PROVIDER_HOLDS.format(name=name))
    live = await repository.live_secrets(conn, user.user_id, name)
    if live >= MAX_SECRETS_PER_OWNER:
        raise Conflict(f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another")
    try:
        secret_id, created = await repository.upsert(
            conn,
            user.user_id,
            name,
            kind=body.kind,
            env_var=env_var,
            url_prefix=url_prefix,
            username=username,
            environment=body.environment,
            sealed=sealed.ciphertext,
            nonce=sealed.nonce,
            key_id=sealed.key_id,
            expires_at=write.expires_at,
        )
    except sa_exc.IntegrityError as exc:  # what the checks above let through and the table does not take
        if not isinstance(exc.orig, psycopg.errors.CheckViolation):
            raise
        raise Unprocessable("the secrets table refused this secret's kind, target or username") from None
    await repository.bind_secret(conn, secret_id, project_ids, worker_ids, pool_ids)
    if write.oauth is not None:
        await store_tokens(conn, secret_id, seal_tokens(sealer, secret_id, write.value, write.oauth))
        await clear_relogin(conn, secret_id)  # a new sign-in ends a cooldown for a dead refresh token
    else:  # written whole: a set of OAuth tokens the secret had before does not outlive its value
        await drop_tokens(conn, secret_id)
    target = audit_target(secret_id)
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_PUT, target=target)
    (secret,) = await owned_secrets(conn, user.user_id, name)
    extra = {"secret": name, "kind": body.kind, "login": user.login, "replaced": not created, "key_id": sealed.key_id}
    log.info("secret written", extra=extra)
    return SecretWritten(**secret.model_dump(), created=created)


# Rebinding a secret


def rebinding(body: SecretRebind) -> tuple[list[str], list[str]]:
    """(workers, pools) of the binding ``body`` gives, each name once; 422 for a body that names neither or both."""
    if body.workers is None and body.pools is None:
        raise Unprocessable(REBIND_NEITHER)
    if body.workers is not None and body.pools is not None:
        raise Unprocessable(REBIND_BOTH)
    return unique_names(body.workers or []), unique_names(body.pools or [])


def binding_text(secret: Secret) -> str:
    """The binding of ``secret`` as its audit row names it: ``workers=<names>``, ``pools=<names>`` or ``anywhere``, by
    the names the hub keeps."""
    if secret.workers:
        return "workers=" + ",".join(secret.workers)
    if secret.pools:
        return "pools=" + ",".join(secret.pools)
    return "anywhere"


async def rebind(conn: AsyncConnection, user: Principal, name: str, workers: list[str], pools: list[str]) -> Secret:
    """Bind the caller's secret ``name`` to ``workers`` or ``pools``, or to any worker of theirs, in the caller's
    transaction; one audit row secret.rebind."""
    row = await repository.live_secret(conn, user.user_id, name)
    if row is None:
        raise NotFound(NO_SECRET.format(name=name))
    if row.kind == PROVIDER_KIND:
        raise Conflict(PROVIDER_HOLDS.format(name=name))
    refusal = binding_refusal(row.kind, workers, pools)
    if refusal is not None:
        raise Unprocessable(refusal)
    worker_ids = await own_workers(conn, user, workers)
    pool_ids = await own_pools(conn, user, pools)
    project_ids = await repository.bound_projects(conn, row.id)
    if not project_ids:  # personal, or its projects went: nothing to bind them on
        raise Conflict(f"secret {name} is bound to no project: write it whole with PUT, naming them")
    await repository.bind_secret(conn, row.id, project_ids, worker_ids, pool_ids)
    (secret,) = await owned_secrets(conn, user.user_id, name)
    target = f"{audit_target(row.id)} {binding_text(secret)}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_REBIND, target=target)
    extra = {"secret": name, "login": user.login, "workers": len(worker_ids), "pools": len(pool_ids)}
    log.info("secret rebound", extra=extra)
    return secret


# Deleting a secret


async def delete(conn: AsyncConnection, user: Principal, name: str) -> None:
    """Delete the caller's secret ``name`` softly, in the caller's transaction (``repository.retire_secret``); one
    audit row secret.delete."""
    secret_id = await repository.deletable_secret(conn, user.user_id, name)
    if secret_id is None:
        raise NotFound(NO_SECRET.format(name=name))
    revoked = await repository.retire_secret(conn, secret_id, user.user_id)
    target = audit_target(secret_id)
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_DELETE, target=target)
    log.info("secret deleted", extra={"secret": name, "login": user.login, "leases_revoked": revoked})

"""The owner's secrets: PUT, GET and DELETE under /v1/secrets, where a member writes a value the hub keeps sealed for
the runs of their projects and never hands back (``docs/credentials.md``).

PUT /v1/secrets/{name} creates the caller's secret of that name or replaces it whole, value included. Kind ``env``
names the variable of the agent's environment it sets (``env_var``, never one of
``evo_agents.hub.credentials.DENIED_ENV`` or its prefixes); kind ``git`` names the https prefix of the origins it
answers for (``url_prefix``, kept in the form of ``credentials.normalize_origin``) and the user git sends with it
(``username``, DEFAULT_GIT_USERNAME when left out); kind ``forge`` names the https prefix of the GitLab projects whose
merge requests the hub drives with it (``url_prefix``, as for git, and no username: GitLab takes the token in a
header), and is never leased to a run (``evo_agents.hub.server.forge``). ``projects`` binds it to one or more projects
the caller holds the writer role on, the admin role for kind forge; ``workers`` narrows it to some of the caller's own
workers that are not revoked, by name, and left out means any worker of the caller; a forge secret names none, as no
worker ever gets it. ``expires_at`` is the end its owner gives it, which kind forge needs. The value is 1 to
MAX_SECRET_BYTES bytes of UTF-8, sealed with ``Sealer`` under the associated data of its owner, name and kind before
it reaches the database; without EVO_HUB_SECRETS_KEY the route answers 503 and keeps nothing. A member keeps at most
MAX_SECRETS_PER_OWNER secrets.

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

Each write adds one audit row, secret.put or secret.delete, whose target is the secret's id (``secret:<id>``), never
its name: the audit is for hub admins, and a secret's name is for its owner alone, as GET /v1/secrets and a run's
credentials are. No audit row, log line or error holds a value. A write made with a web session needs X-Evo-CSRF, as
every write does (``evo_agents.hub.server.security``).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, Field, SecretStr
from sqlalchemy import Text, column, delete, func, insert, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import (
    DEFAULT_GIT_USERNAME,
    FORGE_KIND,
    GITHUB_HOST,
    MAX_SECRET_BYTES,
    MAX_SECRETS_PER_OWNER,
    PROVIDER_KIND,
    SECRET_ENVIRONMENTS,
    SECRET_KINDS,
    SECRET_NAME,
    env_name_refusal,
    normalize_origin,
)
from evo_agents.hub.server import audit, route_autonomy
from evo_agents.hub.server.account_choice import clear_relogin
from evo_agents.hub.server.account_oauth import (
    OAUTH_ENV_VAR,
    OAuthTokens,
    drop_tokens,
    seal_tokens,
    store_tokens,
)
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.models.placement import PoolName
from evo_agents.hub.server.sealing import Sealer, secret_aad
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_views import CredentialShare, shares_of
from evo_agents.hub.server.worker_models import LINE, WORKER_NAME

log = logging.getLogger(__name__)

MAX_ENV_VAR_CHARS = 200  # as the secrets table accepts them
MAX_URL_PREFIX_CHARS = 2000
MAX_USERNAME_CHARS = 200
MAX_BINDINGS = 100  # projects, and workers, one secret names
# https://host[:port][/path], host in lower case, no user, no trailing slash: what normalize_origin makes of an https
# URL, and what the secrets table accepts
URL_PREFIX = re.compile(r"https://[a-z0-9._-]+(:[0-9]{1,5})?(/[^\s]*[^/\s])?")
NO_KEY = (
    "this hub keeps no secrets: it needs {missing}, the key that seals them; ask its operator to set it "
    "(docs/credentials.md)"
)
NO_SECRET = "you have no secret named {name} on this hub: see GET /v1/secrets"
PROVIDER_HOLDS = "{name} is one of your provider accounts: change it with /v1/provider-accounts/{name}"

router = APIRouter(
    prefix="/v1/secrets", tags=["secrets"], dependencies=[route_autonomy.K8], responses={401: {"model": ErrorBody}}
)

SecretName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the caller's secrets")]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


# Models


class SecretWrite(BaseModel):
    kind: Literal[SECRET_KINDS] = Field(
        description="env: a variable of the agent's environment; git: what git's credential helper answers; forge: "
        "a GitLab token the hub alone uses to drive merge requests, never leased to a run"
    )
    env_var: str | None = Field(
        None,
        max_length=MAX_ENV_VAR_CHARS,
        description="kind env only: the variable it sets, upper case, never one that steers the shell, git or the "
        "worker (PATH, HOME, EVO_*, GIT_* ...)",
    )
    url_prefix: str | None = Field(
        None,
        max_length=MAX_URL_PREFIX_CHARS,
        description="kinds git and forge: the https prefix of the origins it answers for, such as "
        "https://gitlab.example.org/group; kept as https://host/path, without .git or a trailing slash",
    )
    username: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_USERNAME_CHARS,
        pattern=LINE,
        description=f"kind git only: the user git sends with the value; {DEFAULT_GIT_USERNAME} when left out",
    )
    environment: Literal[SECRET_ENVIRONMENTS] | None = Field(
        None,
        description="kind env only: the environment it is for; a run whose principal's K7 is auto:test_secrets is "
        "leased the env secrets labelled test alone (docs/autonomy.md)",
    )
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1,
        max_length=MAX_BINDINGS,
        description="the projects whose runs get it; the writer role on each, the admin role for kind forge",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="names of the caller's own workers that are not revoked; left out or empty for any of them; "
        "none for kind forge",
    )
    pools: list[PoolName] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="names of the caller's own worker pools: each node of each gets it, the nodes that join later "
        "too; never with workers, and none for kind forge",
    )
    expires_at: AwareDatetime | None = Field(
        None,
        description="after this no run gets it, and the hub no longer uses one of kind forge; in the future; "
        "required for kind forge",
    )
    oauth: OAuthTokens | None = Field(
        None,
        description=f"kind env, env_var {OAUTH_ENV_VAR} only: the account signed in with OAuth. The value is its "
        "access token; this holds the refresh token and when the access token ends, and the hub keeps all of them "
        "sealed. expires_at, when given, is that end",
    )
    value: SecretStr = Field(
        min_length=1,
        max_length=MAX_SECRET_BYTES,
        description=f"the secret itself, 1 to {MAX_SECRET_BYTES} bytes of UTF-8; written once, never shown again",
    )


class Secret(BaseModel):
    """A secret as its owner sees it: everything but the value."""

    name: str
    kind: Literal[SECRET_KINDS]
    env_var: str | None = Field(description="kind env: the variable it sets")
    url_prefix: str | None = Field(description="kinds git and forge: the https prefix of the origins it answers for")
    username: str | None = Field(description="kind git: the user git sends with it")
    environment: str | None = Field(None, description="kind env: the environment it is labelled for (test)")
    projects: list[str] = Field(description="the projects whose runs get it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    pools: list[str] = Field([], description="the worker pools it is bound to, each node of each")
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime = Field(description="when its value was last written")
    shares: list[CredentialShare] = Field(description="its shares not revoked, newest first")


class SecretWritten(Secret):
    created: bool = Field(description="false when a secret of this name was replaced")


# Reading secrets


def bound_names(target, key):
    """The names of the rows of ``target`` (projects or workers) the secret's bindings name by ``key``, each once and
    sorted, as an array; correlated to the secret of the enclosing query."""
    b = tables.secret_bindings
    names = (
        select(target.c.name)
        .distinct()
        .join_from(b, target, target.c.id == key)
        .where(b.c.secret_id == tables.secrets.c.id)
        .order_by(target.c.name)
    )
    return func.array(names.scalar_subquery(), type_=ARRAY(Text))


async def _secrets(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list[Secret]:
    s, b = tables.secrets, tables.secret_bindings
    listed = select(
        s.c.id,
        s.c.name,
        s.c.kind,
        s.c.env_var,
        s.c.url_prefix,
        s.c.username,
        s.c.environment,
        bound_names(tables.projects, b.c.project_id).label("projects"),
        bound_names(tables.workers, b.c.worker_id).label("workers"),
        bound_names(tables.worker_pools, b.c.pool_id).label("pools"),
        s.c.expires_at,
        s.c.created_at,
        s.c.updated_at,
    ).where(s.c.owner_id == owner_id, s.c.kind != PROVIDER_KIND, s.c.deleted_at.is_(None))
    if name is not None:
        listed = listed.where(s.c.name == name)
    rows = (await conn.execute(listed.order_by(s.c.name))).all()
    shares = await shares_of(conn, [row.id for row in rows])
    return [Secret(**row._mapping, shares=shares.get(row.id, [])) for row in rows]


@router.get("", response_model=list[Secret])
async def list_secrets(request: Request, user: CurrentUser, response: Response) -> list[Secret]:
    """The caller's own secrets, by name, without their values; a hub admin's own too."""
    async with request.app.state.engine.begin() as conn:
        found = await _secrets(conn, user.user_id)
    response.headers.update(NO_STORE)
    return found


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
        raise HTTPException(422, "url_prefix must be an https URL, such as https://gitlab.example.org/group")
    if parts.username is not None or parts.password is not None:
        raise HTTPException(
            422, "url_prefix may not hold a user or a password: send the user as username and the token as value"
        )
    if parts.query or parts.fragment or (port is not None and not 0 < port < 65536):
        raise HTTPException(422, "url_prefix is a host and a path, without a query, a fragment or a port out of range")
    prefix = normalize_origin(text)
    if len(prefix) > MAX_URL_PREFIX_CHARS or not URL_PREFIX.fullmatch(prefix):
        raise HTTPException(
            422, "url_prefix must be https://host[:port][/path], whose host has letters, digits, '.', '-' and '_' only"
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
            raise HTTPException(422, "url_prefix and username belong to a secret of kind git, not env")
        if body.env_var is None:
            raise HTTPException(422, "a secret of kind env needs env_var, the variable it sets")
        refusal = env_name_refusal(body.env_var)
        if refusal is not None:
            raise HTTPException(422, refusal)
        return body.env_var, None, None
    for field_name in ("env_var", "environment"):
        if getattr(body, field_name) is not None:
            raise HTTPException(422, f"{field_name} belongs to a secret of kind env, not {body.kind}")
    if body.url_prefix is None:
        raise HTTPException(
            422, f"a secret of kind {body.kind} needs url_prefix, the https prefix of the origins it answers for"
        )
    if body.kind == FORGE_KIND:
        if body.username is not None:
            raise HTTPException(422, "a secret of kind forge takes no username: GitLab reads the token from a header")
        prefix = _url_prefix(body.url_prefix)
        if urlsplit(prefix).hostname == GITHUB_HOST:
            raise HTTPException(
                422, "a secret of kind forge is for GitLab: on github.com the hub merges by the Curator's tiers"
            )
        return None, prefix, None
    return None, _url_prefix(body.url_prefix), body.username or DEFAULT_GIT_USERNAME


def _forge_refusal(body: SecretWrite) -> str | None:
    """Why ``body`` cannot be a forge secret, or None: it needs an end, and binds to no worker, as none ever gets it."""
    if body.kind != FORGE_KIND:
        return None
    if body.expires_at is None:
        return "a secret of kind forge needs expires_at: set the end of the GitLab token it holds"
    if body.workers or body.pools:
        return "a secret of kind forge binds to no worker or pool: only the hub uses it, and no run is ever leased it"
    return None


def _oauth(body: SecretWrite) -> OAuthTokens | None:
    """The OAuth tokens of ``body``, which only an env secret for CLAUDE_CODE_OAUTH_TOKEN may carry; 422 otherwise."""
    oauth = body.oauth
    if oauth is None:
        return None
    if body.kind != "env" or body.env_var != OAUTH_ENV_VAR:
        raise HTTPException(422, f"oauth belongs to a secret of kind env for {OAUTH_ENV_VAR}")
    if body.expires_at is not None and body.expires_at != oauth.access_expires_at:
        raise HTTPException(422, "expires_at of an account of OAuth is the end of its access token: leave it out")
    return oauth


def _value(body: SecretWrite) -> str:
    value = body.value.get_secret_value()
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise HTTPException(422, f"a secret's value is at most {MAX_SECRET_BYTES} bytes of UTF-8")
    if "\x00" in value:
        raise HTTPException(422, "a secret's value may not hold a NUL character")
    if body.kind == "git" and ("\n" in value or "\r" in value):
        raise HTTPException(
            422, "the value of a secret of kind git is one line: git's credential protocol has no other"
        )
    if body.kind == FORGE_KIND and ("\n" in value or "\r" in value):
        raise HTTPException(422, "the value of a secret of kind forge is one line: an HTTP header holds it")
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
        if not has_role(access.role, needed):
            role = access.role or "no role"
            raise HTTPException(
                403, f"a secret of kind {kind} for project {name} needs the {needed} role on it; you hold {role}"
            )
        ids.append(access.project_id)
    return ids


async def own_workers(conn: AsyncConnection, user: Principal, names: list[str]) -> list[int]:
    """The ids of the caller's workers ``names`` that are not revoked; 403 for any other name, whoever's it is."""
    if not names:
        return []
    w = tables.workers
    lower = func.lower(w.c.name)
    own = select(lower.label("name"), w.c.id).where(
        w.c.owner_id == user.user_id, w.c.revoked_at.is_(None), lower.in_([name.lower() for name in names])
    )
    found = {row.name: row.id for row in await conn.execute(own)}
    for name in names:
        if name.lower() not in found:
            raise HTTPException(
                403,
                f"{name} is not a worker of yours: a secret binds only to your own workers that are not revoked "
                "(GET /v1/workers)",
            )
    return [found[name.lower()] for name in names]


async def own_pools(conn: AsyncConnection, user: Principal, names: list[str]) -> list[int]:
    """The ids of the caller's worker pools ``names``; 403 for any other name, whoever's it is."""
    if not names:
        return []
    p = tables.worker_pools
    lower = func.lower(p.c.name)
    own = select(lower.label("name"), p.c.id).where(
        p.c.owner_id == user.user_id, lower.in_([name.lower() for name in names])
    )
    found = {row.name: row.id for row in await conn.execute(own)}
    for name in names:
        if name.lower() not in found:
            raise HTTPException(
                403,
                f"{name} is not a worker pool of yours: a secret binds only to your own pools (GET /v1/worker-pools)",
            )
    return [found[name.lower()] for name in names]


def both_refusal(workers: list[str], pools: list[str]) -> str | None:
    """Why a binding cannot name ``workers`` and ``pools`` both, or None."""
    if workers and pools:
        return "bind it to workers or to pools, not both: a pool covers each of its nodes already"
    return None


async def live_secrets(conn: AsyncConnection, owner_id: int, other_than: str) -> int:
    s = tables.secrets
    live = (
        select(func.count())
        .select_from(s)
        .where(s.c.owner_id == owner_id, s.c.deleted_at.is_(None), s.c.name != other_than)
    )
    return (await conn.execute(live)).scalar_one()


async def provider_holds(conn: AsyncConnection, owner_id: int, name: str) -> bool:
    """Whether the live secret ``name`` of ``owner_id`` is a provider account's key, which /v1/secrets never writes."""
    s = tables.secrets
    held = select(s.c.id).where(
        s.c.owner_id == owner_id, s.c.name == name, s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None)
    )
    return (await conn.execute(held)).first() is not None


def _upsert(owner_id: int, name: str, **values):
    """Insert the live secret ``name`` of ``owner_id``, or replace the one there is, whole; RETURNING its id and
    whether it was inserted: xmax is 0 on a row this statement inserted, and the updating transaction's id on a row it
    replaced."""
    s = tables.secrets
    statement = pg_insert(s).values(owner_id=owner_id, name=name, **values)
    replaced = {key: statement.excluded[key] for key in values} | {"updated_at": func.now()}
    return statement.on_conflict_do_update(
        index_elements=[s.c.owner_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
    ).returning(s.c.id, (column("xmax") == 0).label("created"))


async def bind_secret(
    conn: AsyncConnection, secret_id: int, project_ids: list[int], worker_ids: list[int], pool_ids: list[int] = ()
) -> None:
    """The secret's bindings, replaced: each project on each of the workers, or on each of the pools, or on any worker
    of its owner."""
    b = tables.secret_bindings
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    targets = [{"worker_id": worker_id} for worker_id in worker_ids] or [{"pool_id": pool_id} for pool_id in pool_ids]
    rows = [
        {"secret_id": secret_id, "project_id": project_id, "worker_id": None, "pool_id": None, **target}
        for project_id in project_ids
        for target in targets or [{}]
    ]
    if rows:
        await conn.execute(insert(b), rows)


@router.put(
    "/{name}",
    response_model=SecretWritten,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def put_secret(
    request: Request, name: SecretName, body: SecretWrite, user: CurrentUser, response: Response
) -> SecretWritten:
    """Create the caller's secret ``name``, or replace it whole; the value is sealed and never shown again."""
    sealer = sealer_of(request)
    env_var, url_prefix, username = _target(body)
    value = _value(body)
    oauth = _oauth(body)
    expires_at = oauth.access_expires_at if oauth is not None else body.expires_at
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise HTTPException(422, "expires_at is past already: a secret's end is in the future")
    refusal = _forge_refusal(body)
    if refusal is not None:
        raise HTTPException(422, refusal)
    refusal = both_refusal(body.workers, body.pools)
    if refusal is not None:
        raise HTTPException(422, refusal)
    projects, workers, pools = unique_names(body.projects), unique_names(body.workers), unique_names(body.pools)
    sealed = sealer.seal(value, secret_aad(user.user_id, name, body.kind))
    async with request.app.state.engine.begin() as conn:
        project_ids = await writable_projects(conn, user, projects, body.kind)
        worker_ids = await own_workers(conn, user, workers)
        pool_ids = await own_pools(conn, user, pools)
        # One write of a member's secrets at a time, so two of them cannot both pass the count.
        users = tables.users
        await conn.execute(select(users.c.id).where(users.c.id == user.user_id).with_for_update())
        if await provider_holds(conn, user.user_id, name):
            raise HTTPException(409, PROVIDER_HOLDS.format(name=name))
        live = await live_secrets(conn, user.user_id, name)
        if live >= MAX_SECRETS_PER_OWNER:
            raise HTTPException(
                409, f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another"
            )
        statement = _upsert(
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
            expires_at=expires_at,
        )
        try:
            secret_id, created = (await conn.execute(statement)).one()
        except sa_exc.IntegrityError as exc:  # what the checks above let through and the table does not take
            if not isinstance(exc.orig, psycopg.errors.CheckViolation):
                raise
            raise HTTPException(422, "the secrets table refused this secret's kind, target or username") from None
        await bind_secret(conn, secret_id, project_ids, worker_ids, pool_ids)
        if oauth is not None:
            await store_tokens(conn, secret_id, seal_tokens(sealer, secret_id, value, oauth))
            await clear_relogin(conn, secret_id)  # a new sign-in ends a cooldown for a dead refresh token
        else:  # written whole: a set of OAuth tokens the secret had before does not outlive its value
            await drop_tokens(conn, secret_id)
        target = audit_target(secret_id)
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_PUT, target=target)
        (secret,) = await _secrets(conn, user.user_id, name)
    extra = {"secret": name, "kind": body.kind, "login": user.login, "replaced": not created, "key_id": sealed.key_id}
    log.info("secret written", extra=extra)
    response.headers.update(NO_STORE)
    return SecretWritten(**secret.model_dump(), created=created)


async def retire_secret(conn: AsyncConnection, secret_id: int, actor_id: int) -> int:
    """Delete the secret ``secret_id`` softly: its sealed value, tokens and bindings go, its leases and shares are
    revoked by ``actor_id``; the number of leases revoked."""
    s, b, cl, cs = tables.secrets, tables.secret_bindings, tables.credential_leases, tables.credential_shares
    gone = update(s).values(deleted_at=func.now(), sealed=None, nonce=None, key_id=None)
    await conn.execute(gone.where(s.c.id == secret_id))
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    await drop_tokens(conn, secret_id)
    ended = await conn.execute(
        update(cl).values(revoked_at=func.now()).where(cl.c.secret_id == secret_id, cl.c.revoked_at.is_(None))
    )
    unshared = update(cs).values(revoked_at=func.now(), revoked_by=actor_id)
    await conn.execute(unshared.where(cs.c.secret_id == secret_id, cs.c.revoked_at.is_(None)))
    return ended.rowcount


@router.delete("/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_secret(request: Request, name: SecretName, user: CurrentUser) -> Response:
    """Delete the caller's secret ``name``: its value and bindings go, and its leases are revoked."""
    s = tables.secrets
    found = (
        select(s.c.id)
        .where(s.c.owner_id == user.user_id, s.c.name == name, s.c.kind != PROVIDER_KIND, s.c.deleted_at.is_(None))
        .with_for_update()
    )
    async with request.app.state.engine.begin() as conn:
        secret_id = (await conn.execute(found)).scalar_one_or_none()
        if secret_id is None:
            raise HTTPException(404, NO_SECRET.format(name=name))
        revoked = await retire_secret(conn, secret_id, user.user_id)
        target = audit_target(secret_id)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_DELETE, target=target
        )
    log.info("secret deleted", extra={"secret": name, "login": user.login, "leases_revoked": revoked})
    return Response(status_code=204)

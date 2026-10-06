"""The owner's secrets: PUT, GET and DELETE under /v1/secrets, where a member writes a value the hub keeps sealed for
the runs of their projects and never hands back (``docs/credentials.md``).

PUT /v1/secrets/{name} creates the caller's secret of that name or replaces it whole, value included. Kind ``env``
names the variable of the agent's environment it sets (``env_var``, never one of
``evo_agents.hub.credentials.DENIED_ENV`` or its prefixes); kind ``git`` names the https prefix of the origins it
answers for (``url_prefix``, kept in the form of ``credentials.normalize_origin``) and the user git sends with it
(``username``, DEFAULT_GIT_USERNAME when left out). ``projects`` binds it to one or more projects the caller holds the
writer role on; ``workers`` narrows it to some of the caller's own workers that are not revoked, by name, and left
out means any worker of the caller. ``expires_at`` is the end its owner gives it. The value is 1 to
MAX_SECRET_BYTES bytes of UTF-8, sealed with ``Sealer`` under the associated data of its owner, name and kind before
it reaches the database; without EVO_HUB_SECRETS_KEY the route answers 503 and keeps nothing. A member keeps at most
MAX_SECRETS_PER_OWNER secrets.

GET /v1/secrets lists the caller's own secrets that are not deleted: name, kind, target, bindings and dates, never the
value, sealed or not. A hub admin sees only their own too; /v1/admin/stats counts the rows of every table and shows
none. DELETE /v1/secrets/{name} deletes softly: the row stays, for the leases and the audit lines that name it, while
its sealed value and its bindings go, and the leases of it that are not given back yet are marked revoked. The name
is free again at once.

Each write adds one audit row, secret.put or secret.delete, whose target is the secret's id (``secret:<id>``), never
its name: the audit is for hub admins, and a secret's name is for its owner alone, as GET /v1/secrets and a run's
credentials are. No audit row, log line or error holds a value. A write made with a web session needs X-Evo-CSRF, as
every write does (``evo_agents.hub.server.security``).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlsplit

import psycopg
from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, Field, SecretStr

from evo_agents.hub.access import has_role
from evo_agents.hub.credentials import (
    DEFAULT_GIT_USERNAME,
    MAX_SECRET_BYTES,
    MAX_SECRETS_PER_OWNER,
    SECRET_KINDS,
    SECRET_NAME,
    env_name_refusal,
    normalize_origin,
)
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.sealing import Sealer, secret_aad
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.workers import LINE, WORKER_NAME

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

router = APIRouter(prefix="/v1/secrets", tags=["secrets"], responses={401: {"model": ErrorBody}})

SecretName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the caller's secrets")]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


# Models


class SecretWrite(BaseModel):
    kind: Literal[SECRET_KINDS] = Field(
        description="env: a variable of the agent's environment; git: what git's credential helper answers"
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
        description="kind git only: the https prefix of the origins it answers for, such as "
        "https://gitlab.example.org/group; kept as https://host/path, without .git or a trailing slash",
    )
    username: str | None = Field(
        None,
        min_length=1,
        max_length=MAX_USERNAME_CHARS,
        pattern=LINE,
        description=f"kind git only: the user git sends with the value; {DEFAULT_GIT_USERNAME} when left out",
    )
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1, max_length=MAX_BINDINGS, description="the projects whose runs get it; the writer role on each"
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        default_factory=list,
        max_length=MAX_BINDINGS,
        description="names of the caller's own workers that are not revoked; left out or empty for any of them",
    )
    expires_at: AwareDatetime | None = Field(None, description="after this no run gets it; in the future")
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
    url_prefix: str | None = Field(description="kind git: the https prefix of the origins it answers for")
    username: str | None = Field(description="kind git: the user git sends with it")
    projects: list[str] = Field(description="the projects whose runs get it")
    workers: list[str] = Field(description="the workers it is bound to; empty for any worker of its owner")
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime = Field(description="when its value was last written")


class SecretWritten(Secret):
    created: bool = Field(description="false when a secret of this name was replaced")


# Reading secrets

SECRETS = """
SELECT s.name, s.kind, s.env_var, s.url_prefix, s.username,
       ARRAY(SELECT DISTINCT p.name FROM secret_bindings b JOIN projects p ON p.id = b.project_id
              WHERE b.secret_id = s.id ORDER BY p.name),
       ARRAY(SELECT DISTINCT w.name FROM secret_bindings b JOIN workers w ON w.id = b.worker_id
              WHERE b.secret_id = s.id ORDER BY w.name),
       s.expires_at, s.created_at, s.updated_at
  FROM secrets s
 WHERE s.owner_id = %(owner)s AND s.deleted_at IS NULL AND (%(name)s::text IS NULL OR s.name = %(name)s)
 ORDER BY s.name
"""
SECRET_FIELDS = (
    "name",
    "kind",
    "env_var",
    "url_prefix",
    "username",
    "projects",
    "workers",
    "expires_at",
    "created_at",
    "updated_at",
)


async def _secrets(conn, owner_id: int, name: str | None = None) -> list[Secret]:
    cursor = await conn.execute(SECRETS, {"owner": owner_id, "name": name})
    return [Secret(**dict(zip(SECRET_FIELDS, row, strict=True))) for row in await cursor.fetchall()]


@router.get("", response_model=list[Secret])
async def list_secrets(request: Request, user: CurrentUser, response: Response) -> list[Secret]:
    """The caller's own secrets, by name, without their values; a hub admin's own too."""
    async with request.app.state.pool.connection() as conn:
        found = await _secrets(conn, user.user_id)
    response.headers.update(NO_STORE)
    return found


# Writing secrets


def _sealer(request: Request) -> Sealer:
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
    if body.env_var is not None:
        raise HTTPException(422, "env_var belongs to a secret of kind env, not git")
    if body.url_prefix is None:
        raise HTTPException(
            422, "a secret of kind git needs url_prefix, the https prefix of the origins it answers for"
        )
    return None, _url_prefix(body.url_prefix), body.username or DEFAULT_GIT_USERNAME


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
    return value


def _unique(values: list[str]) -> list[str]:
    seen, kept = set(), []
    for value in values:
        if value.lower() not in seen:
            seen.add(value.lower())
            kept.append(value)
    return kept


async def _writable(conn, user: Principal, names: list[str]) -> list[int]:
    """The ids of the projects ``names``; 404 for one ``user`` cannot see, 403 for one without the writer role."""
    ids = []
    for name in names:
        access = await project_access(conn, user, name)
        if not has_role(access.role, "writer"):
            role = access.role or "no role"
            raise HTTPException(403, f"a secret for project {name} needs the writer role on it; you hold {role}")
        ids.append(access.project_id)
    return ids


async def _own_workers(conn, user: Principal, names: list[str]) -> list[int]:
    """The ids of the caller's workers ``names`` that are not revoked; 403 for any other name, whoever's it is."""
    if not names:
        return []
    cursor = await conn.execute(
        "SELECT lower(name), id FROM workers WHERE owner_id = %s AND revoked_at IS NULL AND lower(name) = ANY(%s)",
        (user.user_id, [name.lower() for name in names]),
    )
    found = dict(await cursor.fetchall())
    for name in names:
        if name.lower() not in found:
            raise HTTPException(
                403,
                f"{name} is not a worker of yours: a secret binds only to your own workers that are not revoked "
                "(GET /v1/workers)",
            )
    return [found[name.lower()] for name in names]


LIVE_SECRETS = "SELECT count(*) FROM secrets WHERE owner_id = %s AND deleted_at IS NULL AND name <> %s"
UPSERT_SECRET = """
INSERT INTO secrets (owner_id, name, kind, env_var, url_prefix, username, sealed, nonce, key_id, expires_at)
VALUES (%(owner)s, %(name)s, %(kind)s, %(env_var)s, %(url_prefix)s, %(username)s, %(sealed)s, %(nonce)s,
        %(key_id)s, %(expires_at)s)
ON CONFLICT (owner_id, name) WHERE deleted_at IS NULL DO UPDATE
   SET kind = EXCLUDED.kind, env_var = EXCLUDED.env_var, url_prefix = EXCLUDED.url_prefix,
       username = EXCLUDED.username, sealed = EXCLUDED.sealed, nonce = EXCLUDED.nonce, key_id = EXCLUDED.key_id,
       expires_at = EXCLUDED.expires_at, updated_at = now()
RETURNING id, xmax = 0
"""


async def _bind(conn, secret_id: int, project_ids: list[int], worker_ids: list[int]) -> None:
    """The secret's bindings, replaced: each project on each of the workers, or on any worker of its owner."""
    await conn.execute("DELETE FROM secret_bindings WHERE secret_id = %s", (secret_id,))
    if worker_ids:
        await conn.execute(
            "INSERT INTO secret_bindings (secret_id, project_id, worker_id) "
            "SELECT %s, p, w FROM unnest(%s::bigint[]) AS p, unnest(%s::bigint[]) AS w",
            (secret_id, project_ids, worker_ids),
        )
    else:
        await conn.execute(
            "INSERT INTO secret_bindings (secret_id, project_id) SELECT %s, unnest(%s::bigint[])",
            (secret_id, project_ids),
        )


@router.put(
    "/{name}",
    response_model=SecretWritten,
    responses={**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def put_secret(
    request: Request, name: SecretName, body: SecretWrite, user: CurrentUser, response: Response
) -> SecretWritten:
    """Create the caller's secret ``name``, or replace it whole; the value is sealed and never shown again."""
    sealer = _sealer(request)
    env_var, url_prefix, username = _target(body)
    value = _value(body)
    if body.expires_at is not None and body.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(422, "expires_at is past already: a secret's end is in the future")
    projects, workers = _unique(body.projects), _unique(body.workers)
    sealed = sealer.seal(value, secret_aad(user.user_id, name, body.kind))
    async with request.app.state.pool.connection() as conn:
        project_ids = await _writable(conn, user, projects)
        worker_ids = await _own_workers(conn, user, workers)
        # One write of a member's secrets at a time, so two of them cannot both pass the count.
        await conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user.user_id,))
        live = (await (await conn.execute(LIVE_SECRETS, (user.user_id, name))).fetchone())[0]
        if live >= MAX_SECRETS_PER_OWNER:
            raise HTTPException(
                409, f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another"
            )
        params = {
            "owner": user.user_id,
            "name": name,
            "kind": body.kind,
            "env_var": env_var,
            "url_prefix": url_prefix,
            "username": username,
            "sealed": sealed.ciphertext,
            "nonce": sealed.nonce,
            "key_id": sealed.key_id,
            "expires_at": body.expires_at,
        }
        try:
            secret_id, created = await (await conn.execute(UPSERT_SECRET, params)).fetchone()
        except psycopg.errors.CheckViolation:  # what the checks above let through and the table does not take
            raise HTTPException(422, "the secrets table refused this secret's kind, target or username") from None
        await _bind(conn, secret_id, project_ids, worker_ids)
        target = audit_target(secret_id)
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_PUT, target=target)
        (secret,) = await _secrets(conn, user.user_id, name)
    extra = {"secret": name, "kind": body.kind, "login": user.login, "replaced": not created, "key_id": sealed.key_id}
    log.info("secret written", extra=extra)
    response.headers.update(NO_STORE)
    return SecretWritten(**secret.model_dump(), created=created)


DELETE_SECRET = """
UPDATE secrets SET deleted_at = now(), sealed = NULL, nonce = NULL, key_id = NULL
 WHERE owner_id = %s AND name = %s AND deleted_at IS NULL
RETURNING id
"""


@router.delete("/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_secret(request: Request, name: SecretName, user: CurrentUser) -> Response:
    """Delete the caller's secret ``name``: its value and bindings go, and its leases are revoked."""
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(DELETE_SECRET, (user.user_id, name))).fetchone()
        if row is None:
            raise HTTPException(404, NO_SECRET.format(name=name))
        secret_id = row[0]
        await conn.execute("DELETE FROM secret_bindings WHERE secret_id = %s", (secret_id,))
        cursor = await conn.execute(
            "UPDATE credential_leases SET revoked_at = now() WHERE secret_id = %s AND revoked_at IS NULL", (secret_id,)
        )
        revoked = cursor.rowcount
        target = audit_target(secret_id)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SECRET_DELETE, target=target
        )
    log.info("secret deleted", extra={"secret": name, "login": user.login, "leases_revoked": revoked})
    return Response(status_code=204)

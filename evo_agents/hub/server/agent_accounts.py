"""The accounts of an agent: PUT, PATCH and DELETE /v1/projects/{p}/agents/{handle}/accounts/{name}, where an admin of the
project gives an agent profile env secrets of its own, which the hub keeps sealed and never hands back
(``docs/credentials.md``, section "Accounts of an agent").

An account of an agent is a row of ``secrets`` that the profile owns (``secrets.profile_id``, no member owner, migration
0101), always kind env, unique by name among the profile's live accounts. It is bound to the profile's project and to
at least one worker the admin names: a worker of the admin's own that is not revoked, or a shared worker of another
member, and in both cases one that serves the project. Weight, priority and pause are those of any account
(``evo_agents.hub.accounts``), kept in the ``accounts`` row of the secret.

PUT creates the account, or replaces the one of that name whole, value included: the secret keeps its id, so its weight,
priority, rotation and the runs that hold it go on. PATCH changes what it is given: ``value`` renews it (the same
account, a new value), ``weight``, ``priority``, ``workers``, ``expires_at`` (null drops the end) and ``paused``. DELETE
deletes it softly: the row stays for the leases and audit lines that name it, while its sealed value and bindings go and
the leases of it not given back are marked revoked. Deleting the profile does the same to all its accounts
(``retire_profile_accounts``). The default profile @<project> and a deleted profile take none (422); a writer or a
reader who writes gets 403, and a hub admin without a grant on the project too.

The value is sealed under the associated data of its profile and name (``sealing.agent_secret_aad``), which no member's
secret has, so a sealed value copied between the two kinds of row does not open. Without EVO_HUB_SECRETS_KEY a write
answers 503 and keeps nothing. Each write adds one audit row in the project, agent-account.put, .set or .delete, whose
target names the agent and the account by its id (``<project>/@<handle> secret:<id>``), never a value or the account's
name. No route, log line, audit row or error holds a value.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import column, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import profiles, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.accounts import (
    ACCOUNT_STATES,
    MAX_PRIORITY,
    MAX_WEIGHT,
    account_state,
)
from evo_agents.hub.credentials import MAX_SECRET_BYTES, SECRET_NAME, env_name_refusal
from evo_agents.hub.server import audit
from evo_agents.hub.server.account_oauth import drop_tokens
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.sealing import Sealer, agent_secret_aad
from evo_agents.hub.server.secrets import NO_KEY, bound_names
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

MAX_ENV_VAR_CHARS = 200
MAX_WORKERS = 100
MAX_ACCOUNTS_PER_AGENT = 50
Handle = Annotated[str, Path(pattern=profiles.HANDLE_PATTERN, description="the agent's handle, @ left out")]
AccountName = Annotated[
    str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the agent's live accounts")
]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

router = APIRouter(prefix="/v1/projects", tags=["agents"], responses={401: {"model": ErrorBody}})


# Models


class AgentAccountWrite(BaseModel):
    env_var: str = Field(
        max_length=MAX_ENV_VAR_CHARS,
        description="the variable of the agent's environment it sets, upper case, never one that steers the shell, git "
        "or the worker (PATH, HOME, EVO_*, GIT_* ...)",
    )
    value: SecretStr = Field(
        min_length=1,
        max_length=MAX_SECRET_BYTES,
        description=f"the secret itself, 1 to {MAX_SECRET_BYTES} bytes of UTF-8; written once, never shown again",
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] = Field(
        min_length=1,
        max_length=MAX_WORKERS,
        description="names of the workers it is bound to: yours that are not revoked, or a member's shared worker; each "
        "serves the agent's project",
    )
    expires_at: AwareDatetime | None = Field(None, description="after this no run gets it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 1 when new")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner; 0 when new")


class AgentAccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: SecretStr | None = Field(
        None, min_length=1, max_length=MAX_SECRET_BYTES, description="renew: a new value for the same account"
    )
    workers: list[Annotated[str, Field(pattern=WORKER_NAME)]] | None = Field(
        None, min_length=1, max_length=MAX_WORKERS, description="replaces the workers it is bound to"
    )
    expires_at: AwareDatetime | None = Field(None, description="its new end; null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, so no new run takes it; false resumes it")


class AgentAccount(BaseModel):
    """An account of an agent as an admin sees it: everything but the value."""

    id: int = Field(description="the id of its secret, which audit lines name it by")
    name: str
    env_var: str = Field(description="the variable it sets")
    workers: list[str] = Field(description="the workers it is bound to")
    weight: int
    priority: int
    paused_at: datetime | None
    cooldown_until: datetime | None
    cooldown_reason: str | None
    last_leased_at: datetime | None
    expires_at: datetime | None
    state: Literal[ACCOUNT_STATES]
    created_at: datetime
    updated_at: datetime = Field(description="when its value was last written")


class AgentAccountWritten(AgentAccount):
    created: bool = Field(description="false when an account of this name was replaced")


# Reading


def _accounts(profile_id: int, name: str | None = None):
    s, a, b = tables.secrets, tables.accounts, tables.secret_bindings
    statement = (
        select(
            s.c.id,
            s.c.name,
            s.c.env_var,
            bound_names(tables.workers, b.c.worker_id).label("workers"),
            func.coalesce(a.c.weight, 1).label("weight"),
            func.coalesce(a.c.priority, 0).label("priority"),
            a.c.paused_at,
            a.c.cooldown_until,
            a.c.cooldown_reason,
            a.c.last_leased_at,
            s.c.expires_at,
            s.c.created_at,
            s.c.updated_at,
            func.now().label("now"),
        )
        .outerjoin_from(s, a, a.c.secret_id == s.c.id)
        .where(s.c.profile_id == profile_id, s.c.deleted_at.is_(None))
        .order_by(s.c.env_var, s.c.name)
    )
    return statement if name is None else statement.where(s.c.name == name)


def view_of(row) -> dict:
    values = dict(row._mapping)
    now = values.pop("now")
    state = account_state(row.paused_at, row.cooldown_until, row.expires_at, now)
    return {**values, "state": state}


async def _view(conn: AsyncConnection, profile_id: int, name: str) -> dict:
    return view_of((await conn.execute(_accounts(profile_id, name))).one())


# Checks


def _sealer(request: Request) -> Sealer:
    sealer: Sealer | None = request.app.state.sealer
    if sealer is None:
        missing = ", ".join(request.app.state.config.credentials_missing()) or "EVO_HUB_SECRETS_KEY"
        raise HTTPException(503, NO_KEY.format(missing=missing))
    return sealer


def _value(secret: SecretStr) -> str:
    value = secret.get_secret_value()
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise HTTPException(422, f"an account's value is at most {MAX_SECRET_BYTES} bytes of UTF-8")
    if "\x00" in value:
        raise HTTPException(422, "an account's value may not hold a NUL character")
    return value


def _end(expires_at: datetime | None) -> None:
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise HTTPException(422, "expires_at is past already: an account's end is in the future")


def _unique(values: list[str]) -> list[str]:
    seen, kept = set(), []
    for value in values:
        if value.lower() not in seen:
            seen.add(value.lower())
            kept.append(value)
    return kept


async def _agent(conn: AsyncConnection, user: Principal, project: str, handle: str) -> tuple[ProjectAccess, int]:
    """The access to ``project`` and the id of its live, non-default profile ``handle``, its row locked; 404 for a
    project or an agent the caller cannot see, 403 without the admin role, 422 for the default or a deleted profile."""
    access = await project_access(conn, user, project)
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise HTTPException(
            403, f"changing the accounts of an agent of project {project} needs the admin role on it; you hold {held}"
        )
    ap = tables.agent_profiles
    found = (
        await conn.execute(
            select(ap.c.id, ap.c.is_default, ap.c.deleted_at)
            .where(ap.c.project_id == access.project_id, ap.c.handle == handle)
            .with_for_update(of=ap)
        )
    ).one_or_none()
    if found is None:
        raise HTTPException(
            404, f"project {project} has no agent @{handle}: see `evo-agents hub agent list --project {project}`"
        )
    if found.is_default:
        raise HTTPException(
            422, f"@{handle} is the default agent of project {project}, which takes no account of its own"
        )
    if found.deleted_at is not None:
        raise HTTPException(422, f"@{handle} is deleted: a deleted agent takes no account")
    return access, found.id


async def _workers(conn: AsyncConnection, user: Principal, access: ProjectAccess, names: list[str]) -> list[int]:
    """The ids of the workers ``names``, each the caller's own and not revoked or a shared one of another member, and
    serving the project; the caller's own first when two share a name. 422 for any other, naming it."""
    w, wp = tables.workers, tables.worker_projects
    lower = func.lower(w.c.name)
    usable = select(lower.label("name"), w.c.id, w.c.owner_id).where(
        w.c.revoked_at.is_(None),
        lower.in_([name.lower() for name in names]),
        or_(w.c.owner_id == user.user_id, w.c.shared.is_(True)),
        select(wp.c.worker_id).where(wp.c.worker_id == w.c.id, wp.c.project_id == access.project_id).exists(),
    )
    found: dict[str, list] = {}
    for row in await conn.execute(usable):
        found.setdefault(row.name, []).append(row)
    ids = []
    for name in names:
        rows = found.get(name.lower(), [])
        own = [row for row in rows if row.owner_id == user.user_id]
        if len(own or rows) != 1:
            why = (
                "is shared by more than one member" if rows else "is neither your worker, not revoked, nor a shared one"
            )
            raise HTTPException(
                422,
                f"worker {name} {why} that serves project {access.name}: an account of an agent binds to your own "
                "workers and shared ones that serve its project (GET /v1/workers)",
            )
        ids.append((own or rows)[0].id)
    return ids


async def _bind(conn: AsyncConnection, secret_id: int, project_id: int, worker_ids: list[int]) -> None:
    """The account's bindings, replaced: the profile's project on each of the workers."""
    b = tables.secret_bindings
    await conn.execute(delete(b).where(b.c.secret_id == secret_id))
    await conn.execute(
        insert(b), [{"secret_id": secret_id, "project_id": project_id, "worker_id": worker} for worker in worker_ids]
    )


async def _settings(conn: AsyncConnection, secret_id: int, weight: int | None, priority: int | None, paused=None):
    """Weight, priority and pause of the account, written to its ``accounts`` row (made when there is none)."""
    a = tables.accounts
    values = {key: given for key, given in (("weight", weight), ("priority", priority)) if given is not None}
    paused_at = {} if paused is None else {"paused_at": func.now() if paused else None}
    if not values and not paused_at:
        return
    made = pg_insert(a).values(secret_id=secret_id, **values, **paused_at)
    kept = {"paused_at": func.coalesce(a.c.paused_at, func.now()) if paused else None} if paused_at else {}
    changed = {**values, **kept, "updated_at": func.now()}
    await conn.execute(made.on_conflict_do_update(index_elements=[a.c.secret_id], set_=changed))


async def _record(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


def _target(project: str, handle: str, secret_id: int, fields: list[str] | None = None) -> str:
    named = f" fields={','.join(fields)}" if fields else ""
    return f"{project}/@{handle} secret:{secret_id}{named}"


# Retiring


async def retire_accounts(conn: AsyncConnection, secret_ids: list[int]) -> int:
    """Delete the accounts ``secret_ids`` in the transaction of ``conn``: the sealed values and the bindings go, the
    rows are marked deleted, and the leases of them not given back are marked revoked. The number of leases revoked."""
    if not secret_ids:
        return 0
    s, b, cl, a = tables.secrets, tables.secret_bindings, tables.credential_leases, tables.accounts
    await conn.execute(
        update(s).values(deleted_at=func.now(), sealed=None, nonce=None, key_id=None).where(s.c.id.in_(secret_ids))
    )
    await conn.execute(delete(b).where(b.c.secret_id.in_(secret_ids)))
    await conn.execute(
        update(a).values(oauth_sealed=None, oauth_nonce=None, oauth_key_id=None).where(a.c.secret_id.in_(secret_ids))
    )
    ended = await conn.execute(
        update(cl).values(revoked_at=func.now()).where(cl.c.secret_id.in_(secret_ids), cl.c.revoked_at.is_(None))
    )
    return ended.rowcount


async def retire_profile_accounts(conn: AsyncConnection, profile_id: int) -> int:
    """Delete every live account of the profile, as deleting the profile does; how many there were."""
    s = tables.secrets
    ids = (await conn.execute(select(s.c.id).where(s.c.profile_id == profile_id, s.c.deleted_at.is_(None)))).scalars()
    ids = list(ids)
    await retire_accounts(conn, ids)
    return len(ids)


# Writing


async def _live(conn: AsyncConnection, profile_id: int, other_than: str) -> int:
    s = tables.secrets
    live = (
        select(func.count())
        .select_from(s)
        .where(s.c.profile_id == profile_id, s.c.deleted_at.is_(None), s.c.name != other_than)
    )
    return (await conn.execute(live)).scalar_one()


@router.put("/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccountWritten, responses=REFUSALS)
async def put_agent_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentAccountWrite,
    user: CurrentUser,
    response: Response,
) -> AgentAccountWritten:
    """Give the agent the account ``name``, or replace it whole; the value is sealed and never shown again."""
    if (refusal := env_name_refusal(body.env_var)) is not None:
        raise HTTPException(422, refusal)
    value = _value(body.value)
    _end(body.expires_at)
    s = tables.secrets
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await _agent(conn, user, project, handle)
        sealer = _sealer(request)
        worker_ids = await _workers(conn, user, access, _unique(body.workers))
        if await _live(conn, profile_id, name) >= MAX_ACCOUNTS_PER_AGENT:
            raise HTTPException(409, f"@{handle} keeps {MAX_ACCOUNTS_PER_AGENT} accounts already: delete one first")
        sealed = sealer.seal(value, agent_secret_aad(profile_id, name))
        fields = {
            "env_var": body.env_var,
            "sealed": sealed.ciphertext,
            "nonce": sealed.nonce,
            "key_id": sealed.key_id,
            "expires_at": body.expires_at,
        }
        statement = pg_insert(s).values(profile_id=profile_id, name=name, kind="env", **fields)
        replaced = {key: statement.excluded[key] for key in fields} | {"updated_at": func.now()}
        statement = statement.on_conflict_do_update(
            index_elements=[s.c.profile_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
        ).returning(s.c.id, (column("xmax") == 0).label("created"))
        secret_id, created = (await conn.execute(statement)).one()
        await _bind(conn, secret_id, access.project_id, worker_ids)
        await drop_tokens(conn, secret_id)
        await _settings(conn, secret_id, body.weight, body.priority)
        await _record(conn, user, access, audit.AGENT_ACCOUNT_PUT, _target(project, handle, secret_id))
        view = await _view(conn, profile_id, name)
    log.info(
        "agent account written",
        extra={"project": project, "handle": handle, "login": user.login, "was_created": created},
    )
    response.headers.update(NO_STORE)
    return AgentAccountWritten(**view, created=created)


async def _change(conn: AsyncConnection, sealer, profile_id: int, name: str, body: AgentAccountChange):
    """The changes of ``body`` to the live account ``name`` and the names of the fields changed."""
    s = tables.secrets
    secret = (
        await conn.execute(
            select(s.c.id)
            .where(s.c.profile_id == profile_id, s.c.name == name, s.c.deleted_at.is_(None))
            .with_for_update()
        )
    ).one_or_none()
    if secret is None:
        raise HTTPException(404, f"the agent has no account named {name}")
    values: dict = {}
    if body.value is not None:
        sealed = sealer.seal(_value(body.value), agent_secret_aad(profile_id, name))
        values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    if "expires_at" in body.model_fields_set:
        values["expires_at"] = body.expires_at
    if values:
        await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == secret.id))
    return secret.id


@router.patch("/{project}/agents/{handle}/accounts/{name}", response_model=AgentAccount, responses=REFUSALS)
async def change_agent_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentAccountChange,
    user: CurrentUser,
    response: Response,
) -> AgentAccount:
    """Renew the account (``value``), or change its weight, priority, workers or end, or pause or resume it."""
    named = sorted(body.model_fields_set)
    if not named:
        raise HTTPException(422, "give something to change: value, workers, expires_at, weight, priority or paused")
    if body.value is not None:
        _value(body.value)
    if "expires_at" in body.model_fields_set:
        _end(body.expires_at)
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await _agent(conn, user, project, handle)
        sealer = _sealer(request) if body.value is not None else None
        worker_ids = None if body.workers is None else await _workers(conn, user, access, _unique(body.workers))
        secret_id = await _change(conn, sealer, profile_id, name, body)
        if worker_ids is not None:
            await _bind(conn, secret_id, access.project_id, worker_ids)
        await _settings(conn, secret_id, body.weight, body.priority, body.paused)
        await _record(conn, user, access, audit.AGENT_ACCOUNT_SET, _target(project, handle, secret_id, named))
        view = await _view(conn, profile_id, name)
    log.info(
        "agent account changed", extra={"project": project, "handle": handle, "login": user.login, "fields": named}
    )
    response.headers.update(NO_STORE)
    return AgentAccount(**view)


@router.delete(
    "/{project}/agents/{handle}/accounts/{name}", status_code=204, response_class=Response, responses=REFUSALS
)
async def delete_agent_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser
) -> Response:
    """Delete the account: its value and bindings go, and its leases not given back are revoked."""
    s = tables.secrets
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await _agent(conn, user, project, handle)
        found = select(s.c.id).where(s.c.profile_id == profile_id, s.c.name == name, s.c.deleted_at.is_(None))
        secret_id = (await conn.execute(found.with_for_update())).scalar_one_or_none()
        if secret_id is None:
            raise HTTPException(404, f"the agent has no account named {name}")
        revoked = await retire_accounts(conn, [secret_id])
        await _record(conn, user, access, audit.AGENT_ACCOUNT_DELETE, _target(project, handle, secret_id))
    log.info(
        "agent account deleted",
        extra={"project": project, "handle": handle, "login": user.login, "leases_revoked": revoked},
    )
    return Response(status_code=204)

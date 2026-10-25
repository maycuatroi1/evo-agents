"""The provider accounts of an agent: an API key of a model provider that the agent's runs reach through the gateway,
under /v1/projects/{project}/agents/{handle}/provider-accounts (``docs/credentials.md``, section "Provider accounts").

They are a member's provider accounts (``provider_accounts``) owned by an agent profile instead: a secret of kind
provider with ``profile_id`` set, sealed under ``agent_provider_aad`` of the profile and the name, and a row of
``provider_accounts``. An admin of the project writes them, as the agent's other accounts (``agent_accounts``): PUT
creates or replaces one whole, PATCH changes what it is given and keeps the id, so weight, priority, rotation, runs
and shares go on, DELETE deletes it softly. The key is given by ``value`` only: an agent has no env secret of a
member to copy. ``workers`` are those of an agent's account: one or more, the caller's own or shared ones serving the
project. Every member of the project reads them, without the key.

The name space is that of the agent's accounts: a name an env account of the agent holds answers 409, as a PUT of
an env account answers 409 for a provider account's name. The agent counts both against MAX_ACCOUNTS_PER_AGENT. Each
write adds one audit row, provider-account.put, .set or .delete, whose target names the project, the agent, the
secret's id, the provider and the host of the base URL; no route, log line, audit row or error holds the key.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import column, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.credentials import MAX_SECRET_BYTES, PROVIDER_KIND
from evo_agents.hub.provider_accounts import API_FORMATS, MAX_MODELS, PROVIDER_ID
from evo_agents.hub.server import audit, gateway_sync, route_autonomy
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.agent_accounts import (
    MAX_ACCOUNTS_PER_AGENT,
    MAX_WORKERS,
    AccountName,
    Handle,
    account_settings,
    admin_agent,
    agent_workers,
    bind_agent_account,
    live_agent_accounts,
    record_agent,
    retire_accounts,
)
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.provider_store import (
    ModelsFrom,
    ProviderAccount,
    ProviderAccountWritten,
    audit_text,
    checked_url,
    endpoint,
    key_value,
    listed_models,
    no_past_end,
    provider_rows,
    store_endpoint,
    view_of,
)
from evo_agents.hub.server.sealing import Sealed, agent_provider_aad
from evo_agents.hub.server.secrets import sealer_of, unique_names
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_views import shares_of
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

PATH = "/{project}/agents/{handle}/provider-accounts"
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}
Workers = list[Annotated[str, Field(pattern=WORKER_NAME)]]
Models = list[Annotated[str, Field(min_length=1, max_length=200)]]

router = APIRouter(
    prefix="/v1/projects",
    tags=["agents"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}},
)


class AgentProviderAccountWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")
    value: SecretStr = Field(min_length=1, max_length=MAX_SECRET_BYTES, description="the API key; never shown again")
    models: Models = Field(default_factory=list, max_length=MAX_MODELS, description="model names, as typed")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    workers: Workers = Field(min_length=1, max_length=MAX_WORKERS, description="workers serving the agent's project")
    expires_at: AwareDatetime | None = Field(None, description="after this no run takes it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = None


class AgentProviderAccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = None
    api_format: Literal[API_FORMATS] | None = None
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="a new API key")
    models: Models | None = Field(None, max_length=MAX_MODELS)
    models_from: ModelsFrom | None = None
    workers: Workers | None = Field(None, min_length=1, max_length=MAX_WORKERS)
    expires_at: AwareDatetime | None = Field(None, description="null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, false resumes it")


# Reading


async def _accounts(conn: AsyncConnection, profile_id: int, name: str | None = None) -> list[ProviderAccount]:
    rows = (await conn.execute(provider_rows(tables.secrets.c.profile_id == profile_id, name))).all()
    shares = await shares_of(conn, [row.id for row in rows])
    return [ProviderAccount(**view_of(row, shares.get(row.id))) for row in rows]


async def _readable(conn: AsyncConnection, user: Principal, project: str, handle: str) -> int:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the agents of project {access.name} needs a grant on it")
    ap = tables.agent_profiles
    found = select(ap.c.id).where(ap.c.project_id == access.project_id, ap.c.handle == handle)
    profile_id = (await conn.execute(found)).scalar_one_or_none()
    if profile_id is None:
        raise HTTPException(404, f"project {project} has no agent @{handle}")
    return profile_id


async def _own(conn: AsyncConnection, profile_id: int, name: str, handle: str):
    s = tables.secrets
    statement = provider_rows(s.c.profile_id == profile_id, name).add_columns(s.c.sealed, s.c.nonce, s.c.key_id)
    row = (await conn.execute(statement.with_for_update(of=s))).one_or_none()
    if row is None:
        raise HTTPException(404, f"@{handle} has no provider account named {name}")
    return row


@router.get(PATH, response_model=list[ProviderAccount], responses=REFUSALS)
async def list_agent_provider_accounts(
    request: Request, project: ProjectName, handle: Handle, user: CurrentUser, response: Response
) -> list[ProviderAccount]:
    """The agent's provider accounts, by name, without their keys; any member of the project reads them."""
    async with request.app.state.engine.begin() as conn:
        found = await _accounts(conn, await _readable(conn, user, project, handle))
    response.headers.update(NO_STORE)
    return found


# Writing


def _target(project: str, handle: str, secret_id: int, provider: str, url: str, fields=None) -> str:
    return f"{project}/@{handle} {audit_text(secret_id, provider, url, fields)}"


async def _name_free(conn: AsyncConnection, profile_id: int, name: str, handle: str) -> None:
    s = tables.secrets
    held = select(s.c.kind).where(s.c.profile_id == profile_id, s.c.name == name, s.c.deleted_at.is_(None))
    kind = (await conn.execute(held)).scalar_one_or_none()
    if kind is not None and kind != PROVIDER_KIND:
        raise HTTPException(409, f"{name} is an account of @{handle} of kind {kind}: give another name")
    if await live_agent_accounts(conn, profile_id, name) >= MAX_ACCOUNTS_PER_AGENT:
        raise HTTPException(409, f"@{handle} keeps {MAX_ACCOUNTS_PER_AGENT} accounts already: delete one first")


@router.put(PATH + "/{name}", response_model=ProviderAccountWritten, responses=REFUSALS)
async def put_agent_provider_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentProviderAccountWrite,
    user: CurrentUser,
    response: Response,
) -> ProviderAccountWritten:
    """Give the agent the provider account ``name``, or replace it whole; the key is sealed and never shown again."""
    sealer = sealer_of(request)
    key = key_value(body.value)
    url, api_format = endpoint(body.provider, body.base_url, body.api_format)
    url = await checked_url(request, url)
    no_past_end(body.expires_at)
    models = await listed_models(request, body.models, body.models_from, body.provider, (url, api_format, key))
    s = tables.secrets
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await admin_agent(conn, user, project, handle)
        worker_ids = await agent_workers(conn, user, access, unique_names(body.workers))
        await _name_free(conn, profile_id, name, handle)
        sealed = sealer.seal(key, agent_provider_aad(profile_id, name))
        fields = {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
        fields["expires_at"] = body.expires_at
        made = pg_insert(s).values(profile_id=profile_id, name=name, kind=PROVIDER_KIND, **fields)
        replaced = {key: made.excluded[key] for key in fields} | {"updated_at": func.now()}
        made = made.on_conflict_do_update(
            index_elements=[s.c.profile_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
        ).returning(s.c.id, (column("xmax") == 0).label("created"))
        secret_id, created = (await conn.execute(made)).one()
        await store_endpoint(
            conn, secret_id, provider=body.provider, base_url=url, api_format=api_format, models=models
        )
        await bind_agent_account(conn, secret_id, access.project_id, worker_ids)
        await account_settings(conn, secret_id, body.weight, body.priority, body.paused)
        target = _target(project, handle, secret_id, body.provider, url)
        await record_agent(conn, user, access, audit.PROVIDER_ACCOUNT_PUT, target)
        (found,) = await _accounts(conn, profile_id, name)
    log.info("agent provider account written", extra={"project": project, "handle": handle, "created": created})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return ProviderAccountWritten(**found.model_dump(), created=created)


async def _endpoint_after(request: Request, row, body: AgentProviderAccountChange) -> tuple[str, str]:
    if body.base_url is None and body.api_format is None:
        return row.base_url, row.api_format
    url, api_format = endpoint(row.provider, body.base_url or row.base_url, body.api_format or row.api_format)
    return (await checked_url(request, url) if body.base_url is not None else url), api_format


@router.patch(PATH + "/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def change_agent_provider_account(
    request: Request,
    project: ProjectName,
    handle: Handle,
    name: AccountName,
    body: AgentProviderAccountChange,
    user: CurrentUser,
    response: Response,
) -> ProviderAccount:
    """Change what ``body`` gives of the agent's provider account ``name``; the rest, the key included, stays."""
    named = sorted(body.model_fields_set)
    if not named:
        raise HTTPException(422, "give something to change: base_url, models, workers, weight, a key ...")
    if "expires_at" in body.model_fields_set:
        no_past_end(body.expires_at)
    needs_key = body.value is not None or body.models_from == "endpoint"
    sealer = sealer_of(request) if needs_key else None
    async with request.app.state.engine.begin() as conn:
        _, profile_id = await admin_agent(conn, user, project, handle)
        row = await _own(conn, profile_id, name, handle)
    key = key_value(body.value) if body.value is not None else None
    if key is None and sealer is not None:
        key = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), agent_provider_aad(profile_id, name))
    url, api_format = await _endpoint_after(request, row, body)
    models = row.models
    if body.models is not None or body.models_from is not None:
        typed = list(row.models) if body.models is None else body.models
        models = await listed_models(request, typed, body.models_from, row.provider, (url, api_format, key))
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await admin_agent(conn, user, project, handle)
        row = await _own(conn, profile_id, name, handle)
        await store_endpoint(conn, row.id, provider=row.provider, base_url=url, api_format=api_format, models=models)
        values: dict = {}
        if body.value is not None:
            sealed = sealer.seal(key, agent_provider_aad(profile_id, name))
            values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
        if "expires_at" in body.model_fields_set:
            values["expires_at"] = body.expires_at
        s = tables.secrets
        await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == row.id))
        if body.workers is not None:
            worker_ids = await agent_workers(conn, user, access, unique_names(body.workers))
            await bind_agent_account(conn, row.id, access.project_id, worker_ids)
        await account_settings(conn, row.id, body.weight, body.priority, body.paused)
        target = _target(project, handle, row.id, row.provider, url, named)
        await record_agent(conn, user, access, audit.PROVIDER_ACCOUNT_SET, target)
        (found,) = await _accounts(conn, profile_id, name)
    log.info("agent provider account changed", extra={"project": project, "handle": handle, "fields": named})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return found


@router.delete(PATH + "/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_agent_provider_account(
    request: Request, project: ProjectName, handle: Handle, name: AccountName, user: CurrentUser
) -> Response:
    """Delete the agent's provider account ``name``: its key and bindings go, its leases are revoked."""
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await admin_agent(conn, user, project, handle)
        row = await _own(conn, profile_id, name, handle)
        await retire_accounts(conn, [row.id])
        cs = tables.credential_shares
        unshared = update(cs).values(revoked_at=func.now(), revoked_by=user.user_id)
        await conn.execute(unshared.where(cs.c.secret_id == row.id, cs.c.revoked_at.is_(None)))
        target = _target(project, handle, row.id, row.provider, row.base_url)
        await record_agent(conn, user, access, audit.PROVIDER_ACCOUNT_DELETE, target)
    log.info("agent provider account deleted", extra={"project": project, "handle": handle})
    gateway_sync.poke(request.app)  # the gateway follows the write
    return Response(status_code=204)

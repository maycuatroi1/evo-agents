"""A member's provider accounts: an API key of a model provider with the endpoint it calls and the models it serves,
under /v1/provider-accounts (``docs/credentials.md``, section "Provider accounts").

PUT /v1/provider-accounts/{name} creates the caller's account of that name, or replaces it whole, key included. The
name follows the rules of a secret's and shares their name space: a name one of the caller's secrets holds answers
409. ``provider`` is the id of an entry of the catalog that the gateway serves; ``base_url`` and ``api_format`` are
the catalog's when left out (a custom entry has no base URL, so a write gives one); the hub keeps a base URL only when
every address its host resolves to is public (``public_endpoint``). The key is ``value``, or ``from_secret``: the name
of one of the caller's env secrets, such as zhipu-glm for ZHIPU_API_KEY, whose value the hub opens and seals again
under the new account, the env secret staying as it was and leased as before. ``models`` are the names typed;
``models_from`` adds the catalog's default model (catalog) or every model the endpoint lists (endpoint, a call with the
key). ``projects`` (the writer role on each), ``workers`` (the caller's own, not revoked; none for any of them),
``expires_at``, ``weight``, ``priority`` and ``paused`` are those of any account.

PATCH changes what it is given and keeps the rest: base_url, api_format, models (with models_from), projects, workers,
expires_at (null drops the end), weight, priority and paused need no key; ``value`` or ``from_secret`` changes the
key. The account keeps its id, so its weight, priority, rotation, the runs that took it and its shares go on. DELETE
deletes it softly, as DELETE /v1/secrets does a secret: its sealed key and its bindings go, the leases and shares of it
are revoked, the row stays for the audit lines that name it.

POST /v1/provider-accounts/probe tries an endpoint before it is saved, with the key of the body; POST
/v1/provider-accounts/{name}/probe tries a saved one with its key. Both are the Test connection and Fetch models of
the page: the hub calls the endpoint (``provider_probe``) and answers whether it took the key, the models it listed and
a sanitized error, with status 200 either way. GET lists the caller's accounts, GET of a name shows one; neither has
the key, sealed or not.

The key of a provider account is a secret of kind provider, sealed under the associated data of its owner, name and
kind, and never leased raw to a run: ``credentials.LEASED_KINDS`` leaves the kind out. Each write adds one audit row,
provider-account.put, .set or .delete, whose target names the account by its secret's id, its provider and the host of
its base URL (``provider_store.audit_text``). No route, log line, audit row or error holds the key.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import column, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.accounts import MAX_PRIORITY, MAX_WEIGHT
from evo_agents.hub.credentials import MAX_SECRET_BYTES, MAX_SECRETS_PER_OWNER, PROVIDER_KIND, SECRET_NAME
from evo_agents.hub.provider_accounts import API_FORMATS, MAX_MODELS, PROVIDER_ID
from evo_agents.hub.server import audit, gateway_sync, route_autonomy
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.provider_store import (
    NO_KEY_GIVEN,
    ModelsFrom,
    ProbeWrite,
    ProviderAccount,
    ProviderAccountWritten,
    ProviderProbe,
    StoredProbe,
    as_probe,
    audit_text,
    call,
    checked_url,
    endpoint,
    key_value,
    listed_models,
    no_past_end,
    provider_rows,
    store_endpoint,
    store_settings,
    view_of,
)
from evo_agents.hub.server.sealing import Sealed, Sealer, secret_aad
from evo_agents.hub.server.secrets import (
    bind_secret,
    live_secrets,
    own_workers,
    retire_secret,
    sealer_of,
    unique_names,
    writable_projects,
)
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_views import shares_of
from evo_agents.hub.server.worker_models import WORKER_NAME

log = logging.getLogger(__name__)

MAX_BINDINGS = 100
NO_ACCOUNT = "you have no provider account named {name} on this hub: see GET /v1/provider-accounts"
AccountName = Annotated[
    str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="unique among the caller's secrets and accounts")
]
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422, 503)}

router = APIRouter(
    prefix="/v1/provider-accounts",
    tags=["provider-accounts"],
    dependencies=[route_autonomy.K8],
    responses={401: {"model": ErrorBody}},
)

Projects = list[Annotated[str, Field(pattern=PROJECT_NAME)]]
Workers = list[Annotated[str, Field(pattern=WORKER_NAME)]]
Models = list[Annotated[str, Field(min_length=1, max_length=200)]]


class ProviderAccountWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(pattern=PROVIDER_ID.pattern, description="the id of an entry of the catalog")
    base_url: str | None = Field(None, description="the https root of its API; the catalog's when left out")
    api_format: Literal[API_FORMATS] | None = Field(None, description="the catalog's when left out")
    value: SecretStr | None = Field(
        None, min_length=1, max_length=MAX_SECRET_BYTES, description="the API key; written once, never shown again"
    )
    from_secret: str | None = Field(
        None, pattern=f"^{SECRET_NAME.pattern}$", description="or one of your env secrets whose value is the key"
    )
    models: Models = Field(default_factory=list, max_length=MAX_MODELS, description="model names, as typed")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    projects: Projects = Field(min_length=1, max_length=MAX_BINDINGS, description="the writer role on each")
    workers: Workers = Field(default_factory=list, max_length=MAX_BINDINGS, description="your own; empty for any")
    expires_at: AwareDatetime | None = Field(None, description="after this no run takes it; in the future")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT, description="its share in a weighted pool; 0 leaves it")
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY, description="the lower, the sooner in a fill-first pool")
    paused: bool | None = Field(None, description="true: no new run takes it")


class ProviderAccountChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(None, description="the https root of its API")
    api_format: Literal[API_FORMATS] | None = None
    value: SecretStr | None = Field(None, min_length=1, max_length=MAX_SECRET_BYTES, description="a new API key")
    from_secret: str | None = Field(None, pattern=f"^{SECRET_NAME.pattern}$", description="or an env secret's value")
    models: Models | None = Field(None, max_length=MAX_MODELS, description="the model names, replaced")
    models_from: ModelsFrom | None = Field(None, description="add the catalog's default model, or the endpoint's")
    projects: Projects | None = Field(None, min_length=1, max_length=MAX_BINDINGS)
    workers: Workers | None = Field(None, max_length=MAX_BINDINGS)
    expires_at: AwareDatetime | None = Field(None, description="null drops the end")
    weight: int | None = Field(None, ge=0, le=MAX_WEIGHT)
    priority: int | None = Field(None, ge=0, le=MAX_PRIORITY)
    paused: bool | None = Field(None, description="true pauses it, false resumes it")


# Reading


async def _accounts(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list[ProviderAccount]:
    rows = (await conn.execute(provider_rows(tables.secrets.c.owner_id == owner_id, name))).all()
    shares = await shares_of(conn, [row.id for row in rows])
    return [ProviderAccount(**view_of(row, shares.get(row.id))) for row in rows]


async def _own(conn: AsyncConnection, owner_id: int, name: str, *, lock: bool = False):
    """The row of the caller's live account ``name`` (``provider_rows``) with its sealed key; 404 when there is none."""
    s = tables.secrets
    statement = provider_rows(s.c.owner_id == owner_id, name).add_columns(s.c.sealed, s.c.nonce, s.c.key_id)
    row = (await conn.execute(statement.with_for_update(of=s) if lock else statement)).one_or_none()
    if row is None:
        raise HTTPException(404, NO_ACCOUNT.format(name=name))
    return row


@router.get("", response_model=list[ProviderAccount])
async def list_provider_accounts(request: Request, user: CurrentUser, response: Response) -> list[ProviderAccount]:
    """The caller's provider accounts, by name, without their keys; a hub admin's own too."""
    async with request.app.state.engine.begin() as conn:
        found = await _accounts(conn, user.user_id)
    response.headers.update(NO_STORE)
    return found


@router.get("/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def get_provider_account(request: Request, name: AccountName, user: CurrentUser, response: Response):
    """The caller's provider account ``name``, without its key."""
    async with request.app.state.engine.begin() as conn:
        found = await _accounts(conn, user.user_id, name)
    if not found:
        raise HTTPException(404, NO_ACCOUNT.format(name=name))
    response.headers.update(NO_STORE)
    return found[0]


# The key


async def _key(conn: AsyncConnection, sealer: Sealer, user: Principal, value: SecretStr | None, source: str | None):
    """The key a write gives: ``value``, or the value of the caller's env secret ``source``; 422 for both or none."""
    if (value is None) == (source is None):
        raise HTTPException(422, NO_KEY_GIVEN if value is None else "give value or from_secret, not both")
    if value is not None:
        return key_value(value)
    s = tables.secrets
    found = select(s.c.sealed, s.c.nonce, s.c.key_id).where(
        s.c.owner_id == user.user_id, s.c.name == source, s.c.kind == "env", s.c.deleted_at.is_(None)
    )
    row = (await conn.execute(found)).one_or_none()
    if row is None or row.sealed is None:
        raise HTTPException(404, f"you have no env secret named {source}: see GET /v1/secrets")
    opened = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), secret_aad(user.user_id, source, "env"))
    return key_value(SecretStr(opened))


def _stored_key(sealer: Sealer, owner_id: int, row) -> str:
    return sealer.open(Sealed(row.sealed, row.nonce, row.key_id), secret_aad(owner_id, row.name, PROVIDER_KIND))


# Writing


async def _name_free(conn: AsyncConnection, owner_id: int, name: str) -> None:
    """409 when a live secret of the caller holds ``name`` and is not a provider account; the count of live secrets
    checked, with the caller's row locked so two writes cannot both pass it."""
    s, users = tables.secrets, tables.users
    await conn.execute(select(users.c.id).where(users.c.id == owner_id).with_for_update())
    held = select(s.c.kind).where(s.c.owner_id == owner_id, s.c.name == name, s.c.deleted_at.is_(None))
    kind = (await conn.execute(held)).scalar_one_or_none()
    if kind is not None and kind != PROVIDER_KIND:
        raise HTTPException(409, f"your secret {name} is of kind {kind}: give the provider account another name")
    if await live_secrets(conn, owner_id, name) >= MAX_SECRETS_PER_OWNER:
        raise HTTPException(409, f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another")


def _upsert(owner_id: int, name: str, **values):
    s = tables.secrets
    statement = pg_insert(s).values(owner_id=owner_id, name=name, kind=PROVIDER_KIND, **values)
    replaced = {key: statement.excluded[key] for key in values} | {"updated_at": func.now()}
    return statement.on_conflict_do_update(
        index_elements=[s.c.owner_id, s.c.name], index_where=s.c.deleted_at.is_(None), set_=replaced
    ).returning(s.c.id, (column("xmax") == 0).label("created"))


@router.put("/{name}", response_model=ProviderAccountWritten, responses=REFUSALS)
async def put_provider_account(
    request: Request, name: AccountName, body: ProviderAccountWrite, user: CurrentUser, response: Response
) -> ProviderAccountWritten:
    """Create the caller's provider account ``name``, or replace it whole; the key is sealed and never shown again."""
    sealer = sealer_of(request)
    url, api_format = endpoint(body.provider, body.base_url, body.api_format)
    url = await checked_url(request, url)
    no_past_end(body.expires_at)
    async with request.app.state.engine.begin() as conn:
        key = await _key(conn, sealer, user, body.value, body.from_secret)
    models = await listed_models(request, body.models, body.models_from, body.provider, (url, api_format, key))
    sealed = sealer.seal(key, secret_aad(user.user_id, name, PROVIDER_KIND))
    async with request.app.state.engine.begin() as conn:
        project_ids = await writable_projects(conn, user, unique_names(body.projects), PROVIDER_KIND)
        worker_ids = await own_workers(conn, user, unique_names(body.workers))
        await _name_free(conn, user.user_id, name)
        written = _upsert(
            user.user_id,
            name,
            sealed=sealed.ciphertext,
            nonce=sealed.nonce,
            key_id=sealed.key_id,
            expires_at=body.expires_at,
        )
        secret_id, created = (await conn.execute(written)).one()
        await store_endpoint(
            conn, secret_id, provider=body.provider, base_url=url, api_format=api_format, models=models
        )
        await bind_secret(conn, secret_id, project_ids, worker_ids)
        await store_settings(conn, secret_id, body.weight, body.priority, body.paused)
        target = audit_text(secret_id, body.provider, url)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROVIDER_ACCOUNT_PUT, target=target
        )
        (found,) = await _accounts(conn, user.user_id, name)
    log.info(
        "provider account written", extra={"login": user.login, "provider": body.provider, "replaced": not created}
    )
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return ProviderAccountWritten(**found.model_dump(), created=created)


async def _changed_endpoint(request: Request, row, body: ProviderAccountChange) -> tuple[str, str]:
    """(base URL, API format) after ``body``, the base URL checked again when it changed."""
    if body.base_url is None and body.api_format is None:
        return row.base_url, row.api_format
    url, api_format = endpoint(row.provider, body.base_url or row.base_url, body.api_format or row.api_format)
    return (await checked_url(request, url) if body.base_url is not None else url), api_format


async def _changed_models(request: Request, row, body: ProviderAccountChange, url, api_format, key) -> list | None:
    if body.models is None and body.models_from is None:
        return None
    typed = list(row.models) if body.models is None else body.models
    return await listed_models(request, typed, body.models_from, row.provider, (url, api_format, key))


@router.patch("/{name}", response_model=ProviderAccount, responses=REFUSALS)
async def change_provider_account(
    request: Request, name: AccountName, body: ProviderAccountChange, user: CurrentUser, response: Response
) -> ProviderAccount:
    """Change what ``body`` gives of the caller's account ``name``; the rest, the key included, stays."""
    named = sorted(body.model_fields_set)
    if not named:
        raise HTTPException(422, "give something to change: base_url, models, projects, workers, weight, a key ...")
    if "expires_at" in body.model_fields_set:
        no_past_end(body.expires_at)
    new_key = body.value is not None or body.from_secret is not None
    needs_key = new_key or body.models_from == "endpoint"
    sealer = sealer_of(request) if needs_key else None
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name)
        key = await _key(conn, sealer, user, body.value, body.from_secret) if new_key else None
    if key is None and sealer is not None:
        key = _stored_key(sealer, user.user_id, row)
    url, api_format = await _changed_endpoint(request, row, body)
    models = await _changed_models(request, row, body, url, api_format, key)
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name, lock=True)
        await _write_change(conn, user, row, body, (url, api_format, models), key if new_key else None, sealer)
        target = audit_text(row.id, row.provider, url, named)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROVIDER_ACCOUNT_SET, target=target
        )
        (found,) = await _accounts(conn, user.user_id, name)
    log.info("provider account changed", extra={"login": user.login, "fields": [n for n in named if n != "value"]})
    response.headers.update(NO_STORE)
    gateway_sync.poke(request.app)  # the gateway follows the write
    return found


async def _write_change(conn: AsyncConnection, user: Principal, row, body, made, key: str | None, sealer) -> None:
    """The writes of a PATCH: the endpoint row, the key and end, the bindings and the settings."""
    url, api_format, models = made
    await store_endpoint(
        conn,
        row.id,
        provider=row.provider,
        base_url=url,
        api_format=api_format,
        models=row.models if models is None else models,
    )
    s = tables.secrets
    values: dict = {}
    if key is not None:
        sealed = sealer.seal(key, secret_aad(user.user_id, row.name, PROVIDER_KIND))
        values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    if "expires_at" in body.model_fields_set:
        values["expires_at"] = body.expires_at
    await conn.execute(update(s).values(**values, updated_at=func.now()).where(s.c.id == row.id))
    if body.projects is not None or body.workers is not None:
        projects = unique_names(body.projects) if body.projects is not None else list(row.projects)
        workers = unique_names(body.workers) if body.workers is not None else list(row.workers)
        project_ids = await writable_projects(conn, user, projects, PROVIDER_KIND)
        await bind_secret(conn, row.id, project_ids, await own_workers(conn, user, workers))
    await store_settings(conn, row.id, body.weight, body.priority, body.paused)


@router.delete("/{name}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_provider_account(request: Request, name: AccountName, user: CurrentUser) -> Response:
    """Delete the caller's account ``name``: its key and bindings go, its leases and shares are revoked."""
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name, lock=True)
        await retire_secret(conn, row.id, user.user_id)
        target = audit_text(row.id, row.provider, row.base_url)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROVIDER_ACCOUNT_DELETE, target=target
        )
    log.info("provider account deleted", extra={"login": user.login, "provider": row.provider})
    gateway_sync.poke(request.app)  # the gateway follows the write
    return Response(status_code=204)


# Test connection and Fetch models


@router.post("/probe", response_model=ProviderProbe, responses=REFUSALS)
async def probe_endpoint(request: Request, body: ProbeWrite, user: CurrentUser, response: Response) -> ProviderProbe:
    """Call an endpoint not saved yet with the key of ``body``: whether it took it, and the models it lists."""
    url, api_format = endpoint(body.provider, body.base_url, body.api_format)
    sealer = sealer_of(request) if body.from_secret is not None else None
    async with request.app.state.engine.begin() as conn:
        key = await _key(conn, sealer, user, body.value, body.from_secret)
    response.headers.update(NO_STORE)
    return as_probe(await call(request, url, api_format, key, body.model))


@router.post("/{name}/probe", response_model=ProviderProbe, responses=REFUSALS)
async def probe_provider_account(
    request: Request, name: AccountName, body: StoredProbe, user: CurrentUser, response: Response
) -> ProviderProbe:
    """Call the endpoint of the caller's account ``name`` with its key: whether it took it, and the models it lists."""
    sealer = sealer_of(request)
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name)
    key = _stored_key(sealer, user.user_id, row)
    model = body.model or (row.models[0] if row.models else None)
    response.headers.update(NO_STORE)
    return as_probe(await call(request, row.base_url, row.api_format, key, model))

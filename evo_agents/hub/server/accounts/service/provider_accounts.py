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

POST /v1/provider-accounts/{name}/quota reads the quota a Z.ai account has left from Z.ai and keeps it as the
account's last usage (``quota``): the windows read, or an error without the key, with status 200 either way.

POST /v1/provider-accounts/probe tries an endpoint before it is saved, with the key of the body; POST
/v1/provider-accounts/{name}/probe tries a saved one with its key. Both are the Test connection and Fetch models of
the page: the hub calls the endpoint (``probe``) and answers whether it took the key, the models it listed and a
sanitized error, with status 200 either way. GET lists the caller's accounts, GET of a name shows one; neither has
the key, sealed or not.

The key of a provider account is a secret of kind provider, sealed under the associated data of its owner, name and
kind, and never leased raw to a run: ``credentials.LEASED_KINDS`` leaves the kind out. Each write adds one audit row,
provider-account.put, .set or .delete, whose target names the account by its secret's id, its provider and the host of
its base URL (``providers.audit_text``). No route, log line, audit row or error holds the key.
"""

from __future__ import annotations

from fastapi import Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.credentials import MAX_SECRETS_PER_OWNER, PROVIDER_KIND
from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.models.probe import Probe
from evo_agents.hub.server.accounts.models.provider_accounts import (
    NO_ACCOUNT,
    NO_KEY_GIVEN,
    ProbeWrite,
    ProviderAccount,
    ProviderAccountWritten,
    StoredProbe,
)
from evo_agents.hub.server.accounts.models.quota import ProviderQuota
from evo_agents.hub.server.accounts.repository import provider_accounts as repository
from evo_agents.hub.server.accounts.service.providers import (
    audit_text,
    call,
    checked_url,
    endpoint,
    key_value,
    listed_models,
    no_past_end,
    view_of,
)
from evo_agents.hub.server.accounts.service.quota import refresh_for
from evo_agents.hub.server.accounts.service.shapes import ProviderAccountChange, ProviderAccountWrite
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
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
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.share_views import shares_of

# Reading


async def _accounts(conn: AsyncConnection, owner_id: int, name: str | None = None) -> list[ProviderAccount]:
    rows = await repository.owned(conn, owner_id, name)
    shares = await shares_of(conn, [row.id for row in rows])
    return [ProviderAccount(**view_of(row, shares.get(row.id))) for row in rows]


async def _own(conn: AsyncConnection, owner_id: int, name: str, *, lock: bool = False):
    """The row of the caller's live account ``name`` (``provider_rows``) with its sealed key; 404 when there is none."""
    row = await repository.own(conn, owner_id, name, lock=lock)
    if row is None:
        raise NotFound(NO_ACCOUNT.format(name=name))
    return row


async def list_accounts(request: Request, user: Principal) -> list[ProviderAccount]:
    """The caller's provider accounts, by name, without their keys."""
    async with request.app.state.engine.begin() as conn:
        return await _accounts(conn, user.user_id)


async def read(request: Request, user: Principal, name: str) -> ProviderAccount:
    """The caller's provider account ``name``, without its key; 404 when there is none."""
    async with request.app.state.engine.begin() as conn:
        found = await _accounts(conn, user.user_id, name)
    if not found:
        raise NotFound(NO_ACCOUNT.format(name=name))
    return found[0]


# The key


async def _key(conn: AsyncConnection, sealer, user: Principal, value: SecretStr | None, source: str | None) -> str:
    """The key a write gives: ``value``, or the value of the caller's env secret ``source``; 422 for both or none."""
    if (value is None) == (source is None):
        raise Unprocessable(NO_KEY_GIVEN if value is None else "give value or from_secret, not both")
    if value is not None:
        return key_value(value)
    assert source is not None
    row = await repository.env_secret(conn, user.user_id, source)
    if row is None or row.sealed is None:
        raise NotFound(f"you have no env secret named {source}: see GET /v1/secrets")
    opened = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), secret_aad(user.user_id, source, "env"))
    return key_value(SecretStr(opened))


def _stored_key(sealer: Sealer, owner_id: int, row) -> str:
    return sealer.open(Sealed(row.sealed, row.nonce, row.key_id), secret_aad(owner_id, row.name, PROVIDER_KIND))


# Writing


async def _name_free(conn: AsyncConnection, owner_id: int, name: str) -> None:
    """409 when a live secret of the caller holds ``name`` and is not a provider account; the count of live secrets
    checked, with the caller's row locked so two writes cannot both pass it."""
    await repository.lock_owner(conn, owner_id)
    kind = await repository.kind_named(conn, owner_id, name)
    if kind is not None and kind != PROVIDER_KIND:
        raise Conflict(f"your secret {name} is of kind {kind}: give the provider account another name")
    if await live_secrets(conn, owner_id, name) >= MAX_SECRETS_PER_OWNER:
        raise Conflict(f"you keep {MAX_SECRETS_PER_OWNER} secrets already: delete one before adding another")


async def put(request: Request, user: Principal, name: str, body: ProviderAccountWrite) -> ProviderAccountWritten:
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
        secret_id, created = await repository.upsert(
            conn,
            user.user_id,
            name,
            sealed=sealed.ciphertext,
            nonce=sealed.nonce,
            key_id=sealed.key_id,
            expires_at=body.expires_at,
        )
        await repository.store_endpoint(
            conn, secret_id, provider=body.provider, base_url=url, api_format=api_format, models=models
        )
        await bind_secret(conn, secret_id, project_ids, worker_ids)
        await repository.store_settings(conn, secret_id, body.weight, body.priority, body.paused)
        target = audit_text(secret_id, body.provider, url)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROVIDER_ACCOUNT_PUT, target=target
        )
        (found,) = await _accounts(conn, user.user_id, name)
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


async def change(request: Request, user: Principal, name: str, body: ProviderAccountChange) -> ProviderAccount:
    """Change what ``body`` gives of the caller's account ``name``; the rest, the key included, stays."""
    named = sorted(body.model_fields_set)
    if not named:
        raise Unprocessable("give something to change: base_url, models, projects, workers, weight, a key ...")
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
    return found


async def _write_change(conn: AsyncConnection, user: Principal, row, body, made, key: str | None, sealer) -> None:
    """The writes of a PATCH: the endpoint row, the key and end, the bindings and the settings."""
    url, api_format, models = made
    await repository.store_endpoint(
        conn,
        row.id,
        provider=row.provider,
        base_url=url,
        api_format=api_format,
        models=row.models if models is None else models,
    )
    values: dict = {}
    if key is not None:
        sealed = sealer.seal(key, secret_aad(user.user_id, row.name, PROVIDER_KIND))
        values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    if "expires_at" in body.model_fields_set:
        values["expires_at"] = body.expires_at
    await repository.update_secret(conn, row.id, values)
    if body.projects is not None or body.workers is not None:
        projects = unique_names(body.projects) if body.projects is not None else list(row.projects)
        workers = unique_names(body.workers) if body.workers is not None else list(row.workers)
        project_ids = await writable_projects(conn, user, projects, PROVIDER_KIND)
        await bind_secret(conn, row.id, project_ids, await own_workers(conn, user, workers))
    await repository.store_settings(conn, row.id, body.weight, body.priority, body.paused)


async def delete(request: Request, user: Principal, name: str) -> str:
    """Delete the caller's account ``name``: its key and bindings go, its leases and shares are revoked; its
    provider."""
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name, lock=True)
        await retire_secret(conn, row.id, user.user_id)
        target = audit_text(row.id, row.provider, row.base_url)
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROVIDER_ACCOUNT_DELETE, target=target
        )
    return row.provider


# Test connection and Fetch models


async def probe_new(request: Request, user: Principal, body: ProbeWrite) -> Probe:
    """Call an endpoint not saved yet with the key of ``body``."""
    url, api_format = endpoint(body.provider, body.base_url, body.api_format)
    sealer = sealer_of(request) if body.from_secret is not None else None
    async with request.app.state.engine.begin() as conn:
        key = await _key(conn, sealer, user, body.value, body.from_secret)
    return await call(request, url, api_format, key, body.model)


async def probe_stored(request: Request, user: Principal, name: str, body: StoredProbe) -> Probe:
    """Call the endpoint of the caller's account ``name`` with its key."""
    sealer = sealer_of(request)
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name)
    key = _stored_key(sealer, user.user_id, row)
    model = body.model or (row.models[0] if row.models else None)
    return await call(request, row.base_url, row.api_format, key, model)


async def quota(request: Request, user: Principal, name: str) -> ProviderQuota:
    """Read the quota the caller's Z.ai account ``name`` has left and keep it as the account's last usage."""
    sealer_of(request)  # 503 without EVO_HUB_SECRETS_KEY
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, user.user_id, name)
    return await refresh_for(request, row.id)

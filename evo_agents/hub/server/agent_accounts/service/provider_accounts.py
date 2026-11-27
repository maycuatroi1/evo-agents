"""The provider accounts of an agent: an API key of a model provider that the agent's runs reach through the gateway,
under /v1/projects/{project}/agents/{handle}/provider-accounts (``docs/credentials.md``, section "Provider accounts").

They are a member's provider accounts (``provider_accounts``) owned by an agent profile instead: a secret of kind
provider with ``profile_id`` set, sealed under ``agent_provider_aad`` of the profile and the name, and a row of
``provider_accounts``. An admin of the project writes them, as the agent's other accounts (``agent_accounts.service.accounts``): PUT
creates or replaces one whole, PATCH changes what it is given and keeps the id, so weight, priority, rotation, runs
and shares go on, DELETE deletes it softly. The key is given by ``value`` only: an agent has no env secret of a
member to copy. ``workers`` are those of an agent's account: one or more, the caller's own or shared ones serving the
project. Every member of the project reads them, without the key, and may have the hub read the quota a Z.ai one has
left (POST .../{name}/quota, ``accounts.service.quota``), which it keeps as the account's last usage.

The name space is that of the agent's accounts: a name an env account of the agent holds answers 409, as a PUT of
an env account answers 409 for a provider account's name. The agent counts both against MAX_ACCOUNTS_PER_AGENT. Each
write adds one audit row, provider-account.put, .set or .delete, whose target names the project, the agent, the
secret's id, the provider and the host of the base URL; no route, log line, audit row or error holds the key.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.models.provider_accounts import ProviderAccount, ProviderAccountWritten
from evo_agents.hub.server.accounts.models.quota import ProviderQuota
from evo_agents.hub.server.accounts.repository.provider_accounts import provider_rows, store_endpoint
from evo_agents.hub.server.accounts.service.providers import (
    audit_text,
    checked_url,
    endpoint,
    key_value,
    listed_models,
    no_past_end,
    view_of,
)
from evo_agents.hub.server.accounts.service.quota import refresh_for
from evo_agents.hub.server.agent_accounts.models.accounts import MAX_ACCOUNTS_PER_AGENT
from evo_agents.hub.server.agent_accounts.repository import accounts as account_repository
from evo_agents.hub.server.agent_accounts.repository import provider_accounts as repository
from evo_agents.hub.server.agent_accounts.service.accounts import admin_agent, agent_workers
from evo_agents.hub.server.agent_accounts.service.shapes import AgentProviderAccountChange, AgentProviderAccountWrite
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.credentials.models.sealing import Sealed, agent_provider_aad
from evo_agents.hub.server.credentials.service.secrets import sealer_of, unique_names
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.share_views import shares_of

# Reading


async def _accounts(conn: AsyncConnection, profile_id: int, name: str | None = None) -> list[ProviderAccount]:
    rows = await repository.provider_accounts_of(conn, provider_rows, profile_id, name)
    shares = await shares_of(conn, [row.id for row in rows])
    return [ProviderAccount(**view_of(row, shares.get(row.id))) for row in rows]


async def _readable(conn: AsyncConnection, user: Principal, project: str, handle: str) -> int:
    access = await project_access(conn, user, project)
    if access.role is None:
        raise Forbidden(f"reading the agents of project {access.name} needs a grant on it")
    profile_id = await account_repository.profile_id_of(conn, access.project_id, handle)
    if profile_id is None:
        raise NotFound(f"project {project} has no agent @{handle}")
    return profile_id


async def _own(conn: AsyncConnection, profile_id: int, name: str, handle: str):
    row = await repository.locked_provider_account(conn, provider_rows, profile_id, name)
    if row is None:
        raise NotFound(f"@{handle} has no provider account named {name}")
    return row


async def list_accounts(request: Request, user: Principal, project: str, handle: str) -> list[ProviderAccount]:
    """The agent's provider accounts, by name, without their keys."""
    async with request.app.state.engine.begin() as conn:
        return await _accounts(conn, await _readable(conn, user, project, handle))


# Writing


def _target(project: str, handle: str, secret_id: int, provider: str, url: str, fields=None) -> str:
    return f"{project}/@{handle} {audit_text(secret_id, provider, url, fields)}"


async def _name_free(conn: AsyncConnection, profile_id: int, name: str, handle: str) -> None:
    kind = await account_repository.held_kind(conn, profile_id, name)
    if kind is not None and kind != PROVIDER_KIND:
        raise Conflict(f"{name} is an account of @{handle} of kind {kind}: give another name")
    if await account_repository.live_agent_accounts(conn, profile_id, name) >= MAX_ACCOUNTS_PER_AGENT:
        raise Conflict(f"@{handle} keeps {MAX_ACCOUNTS_PER_AGENT} accounts already: delete one first")


async def put(
    request: Request, user: Principal, project: str, handle: str, name: str, body: AgentProviderAccountWrite
) -> ProviderAccountWritten:
    """Give the agent the provider account ``name``, or replace it whole."""
    sealer = sealer_of(request)
    key = key_value(body.value)
    url, api_format = endpoint(body.provider, body.base_url, body.api_format)
    url = await checked_url(request, url)
    no_past_end(body.expires_at)
    models = await listed_models(request, body.models, body.models_from, body.provider, (url, api_format, key))
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await admin_agent(conn, user, project, handle)
        worker_ids = await agent_workers(conn, user, access, unique_names(body.workers))
        await _name_free(conn, profile_id, name, handle)
        sealed = sealer.seal(key, agent_provider_aad(profile_id, name))
        fields = {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
        fields["expires_at"] = body.expires_at
        secret_id, created = await account_repository.put_secret(conn, profile_id, name, PROVIDER_KIND, fields)
        await store_endpoint(
            conn, secret_id, provider=body.provider, base_url=url, api_format=api_format, models=models
        )
        await account_repository.bind_agent_account(conn, secret_id, access.project_id, worker_ids)
        await account_repository.account_settings(conn, secret_id, body.weight, body.priority, body.paused)
        target = _target(project, handle, secret_id, body.provider, url)
        await Auditor.of(conn, user, access).record(audit.PROVIDER_ACCOUNT_PUT, target)
        (found,) = await _accounts(conn, profile_id, name)
    return ProviderAccountWritten(**found.model_dump(), created=created)


async def _endpoint_after(request: Request, row, body: AgentProviderAccountChange) -> tuple[str, str]:
    if body.base_url is None and body.api_format is None:
        return row.base_url, row.api_format
    url, api_format = endpoint(row.provider, body.base_url or row.base_url, body.api_format or row.api_format)
    return (await checked_url(request, url) if body.base_url is not None else url), api_format


async def change(
    request: Request, user: Principal, project: str, handle: str, name: str, body: AgentProviderAccountChange
) -> tuple[ProviderAccount, list[str]]:
    """The provider account after the changes of ``body``, and the names of the fields given."""
    named = sorted(body.model_fields_set)
    if not named:
        raise Unprocessable("give something to change: base_url, models, workers, weight, a key ...")
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
        if body.value is not None and sealer is not None and key is not None:
            sealed = sealer.seal(key, agent_provider_aad(profile_id, name))
            values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
        if "expires_at" in body.model_fields_set:
            values["expires_at"] = body.expires_at
        await account_repository.change_secret(conn, row.id, values)
        if body.workers is not None:
            worker_ids = await agent_workers(conn, user, access, unique_names(body.workers))
            await account_repository.bind_agent_account(conn, row.id, access.project_id, worker_ids)
        await account_repository.account_settings(conn, row.id, body.weight, body.priority, body.paused)
        target = _target(project, handle, row.id, row.provider, url, named)
        await Auditor.of(conn, user, access).record(audit.PROVIDER_ACCOUNT_SET, target)
        (found,) = await _accounts(conn, profile_id, name)
    return found, named


async def delete(request: Request, user: Principal, project: str, handle: str, name: str) -> None:
    """Delete the agent's provider account ``name``: its key and bindings go, its leases and shares are revoked."""
    async with request.app.state.engine.begin() as conn:
        access, profile_id = await admin_agent(conn, user, project, handle)
        row = await _own(conn, profile_id, name, handle)
        await account_repository.retire_accounts(conn, [row.id])
        await repository.unshare(conn, row.id, user.user_id)
        target = _target(project, handle, row.id, row.provider, row.base_url)
        await Auditor.of(conn, user, access).record(audit.PROVIDER_ACCOUNT_DELETE, target)


async def quota(request: Request, user: Principal, project: str, handle: str, name: str) -> ProviderQuota:
    """Read the quota the agent's Z.ai account ``name`` has left and keep it as the account's last usage."""
    sealer_of(request)  # 503 without EVO_HUB_SECRETS_KEY
    async with request.app.state.engine.begin() as conn:
        row = await _own(conn, await _readable(conn, user, project, handle), name, handle)
    return await refresh_for(request, row.id)

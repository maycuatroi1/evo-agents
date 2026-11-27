"""The accounts of an agent: PUT, PATCH and DELETE /v1/projects/{p}/agents/{handle}/accounts/{name}, where an admin of the
project gives an agent profile env secrets of its own, which the hub keeps sealed and never hands back
(``docs/credentials.md``, section "Accounts of an agent").

An account of an agent is a row of ``secrets`` that the profile owns (``secrets.profile_id``, no member owner, migration
0104), kind env here, unique by name among the profile's live accounts; a provider account of the agent, kind provider,
shares that name space and has its routes (``evo_agents.hub.server.agent_accounts.service.provider_accounts``), so these neither list
nor change one, and a PUT of its name answers 409. It is bound to the profile's project and to
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

from datetime import UTC, datetime

from fastapi import Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.account_providers import category_of, provider_of
from evo_agents.hub.accounts import account_state
from evo_agents.hub.credentials import MAX_SECRET_BYTES, env_name_refusal
from evo_agents.hub.server import audit
from evo_agents.hub.server.account_choice import end_cooldown
from evo_agents.hub.server.account_oauth import drop_tokens
from evo_agents.hub.server.account_views import Figures, agent_account_figures, figured
from evo_agents.hub.server.agent_accounts.models.accounts import MAX_ACCOUNTS_PER_AGENT
from evo_agents.hub.server.agent_accounts.repository import accounts as repository
from evo_agents.hub.server.agent_accounts.service.shapes import (
    AgentAccount,
    AgentAccountChange,
    AgentAccountWrite,
    AgentAccountWritten,
)
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.sealing import agent_secret_aad
from evo_agents.hub.server.secrets import bound_names, sealer_of
from evo_agents.hub.server.security import Principal

# Reading


def view_of(row, figures: Figures | None) -> dict:
    """The fields of ``AgentAccount`` from a row of ``account_rows`` and the account's figures."""
    values = dict(row._mapping)
    now = values.pop("now")
    del values["profile_id"]
    state = account_state(row.paused_at, row.cooldown_until, row.expires_at, now)
    provider = provider_of(row.env_var)
    category = category_of(provider)
    return {**values, "state": state, "provider": provider, "category": category, **figured(figures, category)}


async def view(conn: AsyncConnection, profile_id: int, name: str) -> dict:
    """The fields of ``AgentAccount`` of the live account ``name`` of the profile, which exists."""
    row = await repository.account_of(conn, bound_names, profile_id, name)
    figures = await agent_account_figures(conn, [profile_id], [row.id])
    return view_of(row, figures.get(row.id))


# Checks


def value_of(secret: SecretStr) -> str:
    value = secret.get_secret_value()
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise Unprocessable(f"an account's value is at most {MAX_SECRET_BYTES} bytes of UTF-8")
    if "\x00" in value:
        raise Unprocessable("an account's value may not hold a NUL character")
    return value


def check_end(expires_at: datetime | None) -> None:
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise Unprocessable("expires_at is past already: an account's end is in the future")


def unique(values: list[str]) -> list[str]:
    seen, kept = set(), []
    for value in values:
        if value.lower() not in seen:
            seen.add(value.lower())
            kept.append(value)
    return kept


async def admin_agent(conn: AsyncConnection, user: Principal, project: str, handle: str) -> tuple[ProjectAccess, int]:
    """The access to ``project`` and the id of its live, non-default profile ``handle``, its row locked; 404 for a
    project or an agent the caller cannot see, 403 without the admin role, 422 for the default or a deleted profile."""
    access = await project_access(conn, user, project)
    if not has_role(access.role, "admin"):
        held = access.role or "no grant"
        raise Forbidden(
            f"changing the accounts of an agent of project {project} needs the admin role on it; you hold {held}"
        )
    found = await repository.locked_profile(conn, access.project_id, handle)
    if found is None:
        raise NotFound(f"project {project} has no agent @{handle}: see `evo-agents hub agent list --project {project}`")
    if found.is_default:
        raise Unprocessable(f"@{handle} is the default agent of project {project}, which takes no account of its own")
    if found.deleted_at is not None:
        raise Unprocessable(f"@{handle} is deleted: a deleted agent takes no account")
    return access, found.id


async def agent_workers(conn: AsyncConnection, user: Principal, access: ProjectAccess, names: list[str]) -> list[int]:
    """The ids of the workers ``names``, each the caller's own and not revoked or a shared one of another member, and
    serving the project; the caller's own first when two share a name. 422 for any other, naming it."""
    found: dict[str, list] = {}
    for row in await repository.usable_workers(conn, user.user_id, access.project_id, names):
        found.setdefault(row.name, []).append(row)
    ids = []
    for name in names:
        rows = found.get(name.lower(), [])
        own = [row for row in rows if row.owner_id == user.user_id]
        if len(own or rows) != 1:
            why = (
                "is shared by more than one member" if rows else "is neither your worker, not revoked, nor a shared one"
            )
            raise Unprocessable(
                f"worker {name} {why} that serves project {access.name}: an account of an agent binds to your own "
                "workers and shared ones that serve its project (GET /v1/workers)"
            )
        ids.append((own or rows)[0].id)
    return ids


def agent_target(project: str, handle: str, secret_id: int, fields: list[str] | None = None) -> str:
    named = f" fields={','.join(fields)}" if fields else ""
    return f"{project}/@{handle} secret:{secret_id}{named}"


# Writing


async def put(
    conn: AsyncConnection,
    request: Request,
    user: Principal,
    project: str,
    handle: str,
    name: str,
    body: AgentAccountWrite,
) -> AgentAccountWritten:
    """Give the agent the account ``name``, or replace it whole, in the transaction of ``conn``."""
    if (refusal := env_name_refusal(body.env_var)) is not None:
        raise Unprocessable(refusal)
    value = value_of(body.value)
    check_end(body.expires_at)
    access, profile_id = await admin_agent(conn, user, project, handle)
    sealer = sealer_of(request)
    worker_ids = await agent_workers(conn, user, access, unique(body.workers))
    if await repository.agent_provider_holds(conn, profile_id, name):
        raise Conflict(f"{name} is a provider account of @{handle}: change it under provider-accounts")
    if await repository.live_agent_accounts(conn, profile_id, name) >= MAX_ACCOUNTS_PER_AGENT:
        raise Conflict(f"@{handle} keeps {MAX_ACCOUNTS_PER_AGENT} accounts already: delete one first")
    sealed = sealer.seal(value, agent_secret_aad(profile_id, name))
    fields = {
        "env_var": body.env_var,
        "sealed": sealed.ciphertext,
        "nonce": sealed.nonce,
        "key_id": sealed.key_id,
        "expires_at": body.expires_at,
    }
    secret_id, created = await repository.put_secret(conn, profile_id, name, "env", fields)
    await repository.bind_agent_account(conn, secret_id, access.project_id, worker_ids)
    await drop_tokens(conn, secret_id)
    await repository.account_settings(conn, secret_id, body.weight, body.priority)
    await Auditor.of(conn, user, access).record(audit.AGENT_ACCOUNT_PUT, agent_target(project, handle, secret_id))
    return AgentAccountWritten(**await view(conn, profile_id, name), created=created)


async def _change(conn: AsyncConnection, sealer, profile_id: int, name: str, body: AgentAccountChange) -> int:
    """The changes of ``body`` to the live account ``name``; its id."""
    secret_id = await repository.locked_env_account(conn, profile_id, name)
    if secret_id is None:
        raise NotFound(f"the agent has no account named {name}")
    values: dict = {}
    if body.value is not None:
        sealed = sealer.seal(value_of(body.value), agent_secret_aad(profile_id, name))
        values |= {"sealed": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    if "expires_at" in body.model_fields_set:
        values["expires_at"] = body.expires_at
    if values:
        await repository.change_secret(conn, secret_id, values)
    return secret_id


async def change(
    conn: AsyncConnection,
    request: Request,
    user: Principal,
    project: str,
    handle: str,
    name: str,
    body: AgentAccountChange,
) -> tuple[AgentAccount, list[str]]:
    """The account after the changes of ``body``, and the names of what changed."""
    named = sorted(body.model_fields_set)
    if not named:
        raise Unprocessable("give something to change: value, workers, expires_at, weight, priority or paused")
    if body.value is not None:
        value_of(body.value)
    if "expires_at" in body.model_fields_set:
        check_end(body.expires_at)
    access, profile_id = await admin_agent(conn, user, project, handle)
    sealer = sealer_of(request) if body.value is not None else None
    worker_ids = None if body.workers is None else await agent_workers(conn, user, access, unique(body.workers))
    secret_id = await _change(conn, sealer, profile_id, name, body)
    if worker_ids is not None:
        await repository.bind_agent_account(conn, secret_id, access.project_id, worker_ids)
    await repository.account_settings(conn, secret_id, body.weight, body.priority, body.paused)
    if body.paused is False and await end_cooldown(conn, secret_id):
        named.append("cooldown")
    await Auditor.of(conn, user, access).record(
        audit.AGENT_ACCOUNT_SET, agent_target(project, handle, secret_id, named)
    )
    return AgentAccount(**await view(conn, profile_id, name)), named


async def delete(conn: AsyncConnection, user: Principal, project: str, handle: str, name: str) -> int:
    """Delete the account ``name`` of the agent; the number of its leases revoked."""
    access, profile_id = await admin_agent(conn, user, project, handle)
    secret_id = await repository.locked_env_account(conn, profile_id, name)
    if secret_id is None:
        raise NotFound(f"the agent has no account named {name}")
    revoked = await repository.retire_accounts(conn, [secret_id])
    await Auditor.of(conn, user, access).record(audit.AGENT_ACCOUNT_DELETE, agent_target(project, handle, secret_id))
    return revoked


async def retire_profile_accounts(conn: AsyncConnection, profile_id: int) -> int:
    """Delete every live account of the profile, as deleting the profile does; how many there were."""
    return await repository.retire_profile_accounts(conn, profile_id)

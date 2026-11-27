"""The quota a Z.ai provider account has left, read from Z.ai and kept as the account's last usage (``docs/credentials.md``,
section "Provider accounts"; the windows: ``evo_agents.hub.quota``).

``fetch_zai`` calls GET ``<origin>/api/monitor/usage/quota/limit`` with the account's key as a Bearer token, the origin
being the scheme, host and port of its base URL. It checks the URL again and calls the address it checked, with the
host name in the Host header and in TLS, follows no redirect, waits at most TIMEOUT_S seconds and reads at most
MAX_BODY_BYTES, as ``probe`` does. ``repository.quota.keep`` stores what it read in ``accounts.last_usage`` and
``last_usage_at``, replacing the last usage whole: Z.ai's answer is all of it. A window used up (100%) is a limit:
the account cools down until that window resets (reason ``ZAI_REASON``), so no run takes it before; a later read with
room again ends that cooldown, and no other.

``refresh`` reads and keeps the quota of one account whose key it opens (a member's or an agent's); POST
/v1/provider-accounts/{name}/quota and the route of an agent's provider account call it, ``GatewaySync`` every
``gateway.models.sync.QUOTA_INTERVAL`` seconds and after a sweep took back the keys of runs that ended (``refresh_all``). An error is made
for a member to read: no answer, the status and what the endpoint said (``probe.sanitized``), never the key.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.log import register_secret
from evo_agents.hub.quota import ZAI_QUOTA_PATH, zai_usage
from evo_agents.hub.server.accounts.models.probe import MAX_BODY_BYTES, TIMEOUT_S
from evo_agents.hub.server.accounts.models.quota import ProviderQuota, Quota, QuotaUsage
from evo_agents.hub.server.accounts.repository import quota as repository
from evo_agents.hub.server.accounts.service.probe import call_pinned, pinned_url, sanitized
from evo_agents.hub.server.public_endpoint import EndpointRefused, Resolver, base_url, public_addresses
from evo_agents.hub.server.sealing import Sealed, Sealer, agent_provider_aad, secret_aad

log = logging.getLogger(__name__)


def as_quota(found: Quota) -> ProviderQuota:
    usage = QuotaUsage.model_validate(found.usage) if found.usage is not None else None
    return ProviderQuota(ok=found.ok, usage=usage, status=found.status, error=found.error)


async def refresh_for(request: Request, secret_id: int) -> ProviderQuota:
    """``refresh`` with the app's sealer, transport and resolver, as a route answers it."""
    state = request.app.state
    hooks = (getattr(state, "provider_transport", None), getattr(state, "provider_resolver", None))
    return as_quota(await refresh(state.engine, state.sealer, secret_id, hooks))


def quota_url(url: str) -> str:
    """The quota API of the endpoint at ``url``: its origin and ZAI_QUOTA_PATH."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, ZAI_QUOTA_PATH, "", ""))


def _headers(key: str, host: str) -> dict[str, str]:
    return {
        "Host": host,
        "Accept": "application/json",
        "User-Agent": "evo-agents-hub",
        "Authorization": f"Bearer {key}",
    }


async def fetch_zai(
    url: str, key: str, *, transport: httpx.AsyncBaseTransport | None = None, resolver: Resolver | None = None
) -> Quota:
    """Read the quota of the Z.ai account at base URL ``url`` with ``key``, as the module says; nothing is kept."""
    try:
        url = quota_url(base_url(url))
        address = (await public_addresses(url, resolver))[0]
    except EndpointRefused as exc:
        return Quota(False, error=sanitized(str(exc), key))
    parts = urlsplit(url)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(TIMEOUT_S)) as client:
            status, body = await call_pinned(
                client, "GET", pinned_url(url, address), _headers(key, parts.netloc), None, parts.hostname or ""
            )
    except httpx.TimeoutException:
        return Quota(False, error=f"Z.ai did not answer within {TIMEOUT_S:g} seconds")
    except httpx.HTTPError as exc:
        return Quota(False, error=f"Z.ai could not be reached: {sanitized(type(exc).__name__, key)}")
    if not 200 <= status < 300:
        said = sanitized(body[:MAX_BODY_BYTES].decode("utf-8", "replace"), key)
        return Quota(False, status=status, error=f"HTTP {status}" + (f": {said}" if said else ""))
    try:
        usage = zai_usage(json.loads(body))
    except ValueError:
        return Quota(False, status=status, error="Z.ai's answer is not JSON")
    if usage is None:
        return Quota(False, status=status, error="Z.ai's answer lists no five-hour or weekly limit")
    return Quota(True, usage, status)


def _key(sealer: Sealer, row) -> str:
    aad = (
        agent_provider_aad(row.profile_id, row.name)
        if row.profile_id
        else secret_aad(row.owner_id, row.name, PROVIDER_KIND)
    )
    key = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), aad)
    register_secret(key)
    return key


async def refresh(engine: AsyncEngine, sealer: Sealer, secret_id: int, hooks: tuple = (None, None)) -> Quota:
    """Read and keep the quota of the live Z.ai account ``secret_id``; ``hooks`` are (transport, resolver), the
    tests' or none."""
    async with engine.connect() as conn:
        row = await repository.zai_account(conn, secret_id)
    if row is None:
        return Quota(False, error="the account is not one of Z.ai: its provider has no quota API the hub reads")
    try:
        key = _key(sealer, row)
    except Exception as exc:  # a key sealed under another EVO_HUB_SECRETS_KEY
        return Quota(False, error=f"the account's key does not open: {type(exc).__name__}")
    transport, resolver = hooks
    found = await fetch_zai(row.base_url, key, transport=transport, resolver=resolver)
    if found.ok and found.usage is not None:
        async with engine.begin() as conn:
            await repository.keep(conn, secret_id, found.usage)
    return found


async def refresh_all(engine: AsyncEngine, sealer: Sealer, hooks: tuple = (None, None)) -> int:
    """Read and keep the quota of every live Z.ai account; how many were kept. A failure is logged by the account's
    secret id and the error, never its key."""
    async with engine.connect() as conn:
        ids = await repository.zai_ids(conn)
    kept = 0
    for secret_id in ids:
        found = await refresh(engine, sealer, secret_id, hooks)
        kept += found.ok
        if not found.ok:
            log.warning("quota of a Z.ai account not read", extra={"secret_id": secret_id, "error": found.error})
    return kept

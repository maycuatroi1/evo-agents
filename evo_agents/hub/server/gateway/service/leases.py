"""The lease of a run on a gateway model (``evo_agents.hub.gateway_runs``): a key of the gateway made for the run, which
the hub alone holds, and a token of the run, which its worker gets (``docs/credentials.md``, section "Provider
accounts").

When the worker asks for the run's credentials (``credentials.lease_credentials``), ``prepare`` finds the provider
accounts the run may use on that worker (``service.accounts.usable_accounts``, the account picked by hand alone when
one was, the one quota-pacing gives alone under that strategy: ``paced_accounts``) and, unless the run holds a key of
the gateway for that same set already, has the gateway make one whose allowedConnections are those accounts'
connections (``service.sync``), ending GRACE seconds after the run's timeout would, never logging a request. ``record``
writes it in the ask's transaction: a credential_leases row of provider gateway for the key (secret_id null,
external_id the key's id, the key sealed under the lease's id, token_hash the SHA-256 of the run's token), one row per
account it reaches (secret_id the account, share_id the share it came under), and the earlier key's rows of the run
marked revoked. The run's token is an HMAC of the lease's id under the key (``service.tokens``), so the hub derives it
again for each ask without storing it. The worker's lease (``Lease``, kind gateway) carries the hub's gateway route,
the token, the model and the API format of the run's runtime, never a key. A run that may use no account gets no
gateway lease, and an event of the run says why. ``undo`` revokes a key made for an ask that failed.

``sweep`` takes back the keys of runs that ended: for each key row that still holds its key and is revoked, past its
end, or of a run no longer held, it reads the key's usage at the gateway into the run's usage (``usage.gateway``,
and ``total_cost_usd``, the cost the run reports), revokes the key and drops it. ``GatewaySync`` runs it each round,
and a run's end pokes it. The gateway route refuses a token whose lease is revoked or whose run is not held
(``service.proxy``) at once, before the sweep.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.credentials import Lease
from evo_agents.hub.gateway_runs import GATEWAY, PICK_PREFIX, ROUTE, RUNTIME_FORMATS
from evo_agents.hub.model_router import routed_model
from evo_agents.hub.server.gateway.client.omniroute import GatewayClient, GatewayError, GatewayOff
from evo_agents.hub.server.gateway.models.accounts import Asker
from evo_agents.hub.server.gateway.models.leases import GRACE, SWEEP_BATCH, Prepared
from evo_agents.hub.server.gateway.models.sync import NAME
from evo_agents.hub.server.gateway.repository import leases as repo
from evo_agents.hub.server.gateway.service import accounts, sync
from evo_agents.hub.server.gateway.service.tokens import run_token, token_hash
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad

log = logging.getLogger(__name__)


def _asker(run) -> Asker:
    """What the account rules read of the run the credential survey found (``credentials._Run``)."""
    return Asker(
        run.id,
        run.owner_id,
        run.principal,
        run.project_id,
        run.profile_id,
        run.profile_project_id,
        agent_owned=run.agent_owned,
        permissions={"credential": run.credentials} if run.credentials else None,
    )


async def _connections(client: GatewayClient, accounts, reasons: list[str]) -> dict[int, str]:
    """The gateway's active connection of each account, by secret id; the reason of each account without one."""
    found = {}
    for connection in await client.connections():
        match = NAME.fullmatch(connection.name)
        if match and connection.active:
            found[int(match.group(1))] = connection.id
    for account in accounts:
        if account.secret_id not in found:
            reasons.append(f"account {account.secret_id} has no active connection at the model gateway yet")
    return found


async def prepare(app, engine: AsyncEngine, run, worker_id: int, base_url: str) -> Prepared | None:
    """What the run ``run`` (the credential survey's) on worker ``worker_id`` is leased of the gateway, the key made
    when it needs a new one; None for a run that does not go through the gateway."""
    async with engine.connect() as conn:
        row = await repo.gateway_run(conn, run.id)
        if row.gateway_provider is None:
            return None
        prefix = row.gateway_provider
        model = row.model or routed_model(row.routing)  # a run the Token strategy put on a gateway model
        found = Prepared(run.id, prefix, model, RUNTIME_FORMATS.get(row.runtime, ""), base_url + ROUTE)
        picked = await repo.picked(conn, run.id, prefix)
        usable = await accounts.usable_accounts(conn, _asker(run), worker_id, prefix, picked)
        if not usable.accounts:
            found.reasons = [usable.why(prefix)]
            return found
        paced = await accounts.paced_accounts(conn, _asker(run), usable.accounts)
        wanted = {account.secret_id for account in usable.accounts}
        found.held = await repo.held_key(conn, run.id, worker_id, wanted, within=len(paced) < len(usable.accounts))
    if found.held is not None:
        found.accounts = usable.accounts
        return found
    usable.accounts = paced
    try:
        client = sync.client_of(app)
        connections = await _connections(client, usable.accounts, found.reasons)
        found.accounts = [account for account in usable.accounts if account.secret_id in connections]
        if not found.accounts:
            return found
        found.expires_at = datetime.now(UTC) + timedelta(seconds=row.timeout_s + GRACE)
        reach = [connections[account.secret_id] for account in found.accounts]
        made = await client.create_key(f"evo-run-{run.id}", reach, found.expires_at)
    except (GatewayOff, GatewayError) as exc:
        found.accounts = []
        found.reasons.append(f"the model gateway made no key: {exc}")
        return found
    found.key_id, found.key = made.id, made.value
    return found


async def _opened(conn: AsyncConnection, sealer: Sealer, lease_id: int) -> str | None:
    row = await repo.sealed_key(conn, lease_id)
    try:
        return sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(lease_id))
    except Unsealable as exc:
        log.warning("a gateway key does not open", extra={"lease_id": lease_id, "why": str(exc)})
        return None


async def _record_key(conn: AsyncConnection, found: Prepared, run, worker_id: int, sealer: Sealer) -> int:
    """Write the key made now and the accounts it reaches; the run's earlier keys are marked revoked."""
    await repo.revoke_earlier(conn, run.id)
    common = {
        "run_id": run.id,
        "worker_id": worker_id,
        "provider": GATEWAY,
        "target": found.model,
        "external_id": found.key_id,
        "expires_at": found.expires_at,
        **run.lease_columns(),
    }
    lease_id = await repo.insert_lease(conn, {**common, "secret_id": None})
    key = found.key.get_secret_value()
    sealed = sealer.seal(key, lease_aad(lease_id))  # bound to the lease's id, so inserted first
    values = {"sealed_value": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    values["token_hash"] = token_hash(run_token(key, lease_id))
    await repo.store_key(conn, lease_id, values)
    for account in found.accounts:
        await repo.insert_lease(conn, {**common, "secret_id": account.secret_id, "share_id": account.share_id})
    await repo.stamp_leased(conn, [account.secret_id for account in found.accounts])
    return lease_id


async def record(conn: AsyncConnection, found: Prepared | None, run, worker_id: int, sealer: Sealer) -> list[Lease]:
    """The gateway lease of ``found``, written in the ask's transaction; none, with an event of the run saying why,
    when the run may use no account."""
    if found is None:
        return []
    if not found.accounts:
        from evo_agents.hub.server.run_state import write_event  # it imports the routes that import this module

        text = f"model {found.prefix}/... gets no lease of the model gateway: {'; '.join(dict.fromkeys(found.reasons))}"
        await write_event(conn, found.run_id, {"text": text})
        return []
    if found.held is not None:
        lease_id = found.held
        key = await _opened(conn, sealer, lease_id)
        if key is None:
            return []
        found.expires_at = await repo.expires_at(conn, lease_id)
    else:
        lease_id = await _record_key(conn, found, run, worker_id, sealer)
        key = found.key.get_secret_value()
    lease = Lease(
        id=lease_id,
        kind=GATEWAY,
        provider=GATEWAY,
        name=f"{PICK_PREFIX}{found.prefix}",
        value=run_token(key, lease_id),
        expires_at=found.expires_at,
        base_url=found.base_url,
        model=found.model,
        api_format=found.api_format,
    )
    return [lease]


async def undo(app, found: Prepared | None) -> None:
    """Revoke a key made for an ask that then failed; the gateway failing leaves it to its end."""
    if found is None or found.key_id is None:
        return
    try:
        await sync.client_of(app).revoke_key(found.key_id)
    except (GatewayOff, GatewayError) as exc:
        log.warning("a gateway key made for a failed ask is left to its end", extra={"why": str(exc)})


# Taking keys back


def used_with(usage, used: dict) -> dict:
    """runs.usage ``usage`` with what the gateway counted for one more key of the run added: under ``gateway``, and
    its cost as total_cost_usd, the figure the run's budget, its page and the account figures read
    (``curator.run_cost``), for the gateway priced what the run's runtime cannot."""
    usage = dict(usage) if isinstance(usage, dict) else {}
    before = usage.get("gateway") if isinstance(usage.get("gateway"), dict) else {}
    merged = {key: (before.get(key) or 0) + value for key, value in used.items()}  # pyright: ignore[reportOptionalMemberAccess]
    merged["cost_usd"] = round(merged["cost_usd"], 6)
    return {**usage, "gateway": merged, "total_cost_usd": merged["cost_usd"]}


async def _take(engine: AsyncEngine, row, used: dict) -> bool:
    """Drop the key of lease ``row``, end the leases of its accounts and add its usage to the run's; False when another
    sweep took it first."""
    async with engine.begin() as conn:
        if not await repo.drop_key(conn, row):
            return False
        usage = await repo.run_usage(conn, row.run_id)
        await repo.set_run_usage(conn, row.run_id, used_with(usage, used))
    return True


async def sweep(client: GatewayClient, engine: AsyncEngine, batch: int = SWEEP_BATCH) -> int:
    """Take back the keys of the runs that ended, as the module says; how many. A key the gateway fails on stays for
    the next sweep."""
    async with engine.connect() as conn:
        rows = await repo.ended_keys(conn, batch)
    done = 0
    for row in rows:
        try:
            used = await client.key_usage(row.external_id)
            await client.revoke_key(row.external_id)
        except GatewayError as exc:
            log.warning("gateway: a run's key is left for the next sweep", extra={"lease_id": row.id, "why": str(exc)})
            continue
        figures = {
            "requests": used.requests,
            "input_tokens": used.prompt_tokens,
            "output_tokens": used.completion_tokens,
            "cost_usd": used.cost_usd,
        }
        done += await _take(engine, row, figures)
    if done:
        log.info("gateway: keys of ended runs taken back", extra={"keys": done})
    return done

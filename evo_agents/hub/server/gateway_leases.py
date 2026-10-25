"""The lease of a run on a gateway model (``evo_agents.hub.gateway_runs``): a key of the gateway made for the run, which
the hub alone holds, and a token of the run, which its worker gets (``docs/credentials.md``, section "Provider
accounts").

When the worker asks for the run's credentials (``credentials.lease_credentials``), ``prepare`` finds the provider
accounts the run may use on that worker (``gateway_accounts.usable_accounts``, the account picked by hand alone when one
was) and, unless the run holds a key of the gateway for that same set already, has the gateway make one whose
allowedConnections are those accounts' connections (``gateway_sync``), ending GRACE seconds after the run's timeout
would, never logging a request. ``record`` writes it in the ask's transaction: a credential_leases row of provider
gateway for the key (secret_id null, external_id the key's id, the key sealed under the lease's id, token_hash the
SHA-256 of the run's token), one row per account it reaches (secret_id the account, share_id the share it came under),
and the earlier key's rows of the run marked revoked. The run's token is an HMAC of the lease's id under the key, so
the hub derives it again for each ask without storing it. The worker's lease (``Lease``, kind gateway) carries the
hub's gateway route, the token, the model and the API format of the run's runtime, never a key. A run that may use
no account gets no gateway lease, and an event of the run says why. ``undo`` revokes a key made for an ask that
failed.

``sweep`` takes back the keys of runs that ended: for each key row that still holds its key and is revoked, past its
end, or of a run no longer held, it reads the key's usage at the gateway into the run's usage (``usage.gateway``,
and ``total_cost_usd``, the cost the run reports), revokes the key and drops it. ``GatewaySync`` runs it each round,
and a run's end pokes it. The gateway route refuses a token whose lease is revoked or whose run is not held
(``gateway_proxy``) at once, before the sweep.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs, tables
from evo_agents.hub.credentials import Lease
from evo_agents.hub.gateway_runs import GATEWAY, PICK_PREFIX, ROUTE, RUNTIME_FORMATS
from evo_agents.hub.server import gateway_accounts, gateway_sync
from evo_agents.hub.server.gateway_client import GatewayClient, GatewayError, GatewayOff
from evo_agents.hub.server.sealing import Sealed, Sealer, Unsealable, lease_aad

log = logging.getLogger(__name__)

GRACE = 300  # seconds a run's key outlives its timeout: the heartbeat that ends it, and the sweep after
TOKEN_PREFIX = "evr_"  # a run's token to the hub's gateway route
SWEEP_BATCH = 100


def run_token(key: str, lease_id: int) -> str:
    """The token of the run whose key of the gateway is ``key``, leased as ``lease_id``."""
    mac = hmac.new(key.encode(), f"evo-gateway-run:{lease_id}".encode(), hashlib.sha256).digest()
    return TOKEN_PREFIX + base64.urlsafe_b64encode(mac).decode().rstrip("=")


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


@dataclass
class Prepared:
    """What ``prepare`` found for a run on a gateway model, for ``record`` to write."""

    run_id: int
    prefix: str
    model: str
    api_format: str
    base_url: str
    accounts: list[gateway_accounts.Account] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    held: int | None = None  # the key row the run holds already for these accounts
    key_id: str | None = None  # a key made now
    key: SecretStr = field(default=SecretStr(""), repr=False)
    expires_at: datetime | None = None

    def audit_text(self) -> str:
        """How the credential.lease audit row names the provider accounts the run's key reaches: by id."""
        ids = ",".join(str(account.secret_id) for account in sorted(self.accounts, key=lambda a: a.secret_id))
        return f" provider-accounts={ids}" if ids else ""


def _asker(run) -> gateway_accounts.Asker:
    """What the account rules read of the run the credential survey found (``credentials._Run``)."""
    return gateway_accounts.Asker(
        run.id,
        run.owner_id,
        run.principal,
        run.project_id,
        run.profile_id,
        run.profile_project_id,
        agent_owned=run.agent_owned,
        permissions={"credential": run.credentials} if run.credentials else None,
    )


async def _picked(conn: AsyncConnection, run_id: int, prefix: str) -> int | None:
    ra = tables.run_accounts
    query = select(ra.c.secret_id).where(ra.c.run_id == run_id, ra.c.env_var == PICK_PREFIX + prefix)
    return (await conn.execute(query)).scalar_one_or_none()


async def _held_key(conn: AsyncConnection, run_id: int, worker_id: int, wanted: set[int]) -> int | None:
    """The live key row the run holds on this worker that reaches exactly the accounts ``wanted``."""
    cl = tables.credential_leases
    live = and_(cl.c.run_id == run_id, cl.c.worker_id == worker_id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
    keys = select(cl.c.id, cl.c.external_id).where(live, cl.c.secret_id.is_(None), cl.c.sealed_value.is_not(None))
    key = (await conn.execute(keys.where(cl.c.expires_at > func.now()).order_by(cl.c.id.desc()).limit(1))).first()
    if key is None:
        return None
    reached = select(cl.c.secret_id).where(live, cl.c.secret_id.is_not(None), cl.c.external_id == key.external_id)
    return key.id if set((await conn.execute(reached)).scalars()) == wanted else None


async def _connections(client: GatewayClient, accounts, reasons: list[str]) -> dict[int, str]:
    """The gateway's active connection of each account, by secret id; the reason of each account without one."""
    found = {}
    for connection in await client.connections():
        match = gateway_sync.NAME.fullmatch(connection.name)
        if match and connection.active:
            found[int(match.group(1))] = connection.id
    for account in accounts:
        if account.secret_id not in found:
            reasons.append(f"account {account.secret_id} has no active connection at the model gateway yet")
    return found


async def prepare(app, engine: AsyncEngine, run, worker_id: int, base_url: str) -> Prepared | None:
    """What the run ``run`` (the credential survey's) on worker ``worker_id`` is leased of the gateway, the key made
    when it needs a new one; None for a run that does not go through the gateway."""
    r = tables.runs
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(r.c.gateway_provider, r.c.model, r.c.runtime, r.c.timeout_s).where(r.c.id == run.id)
            )
        ).one()
        if row.gateway_provider is None:
            return None
        prefix = row.gateway_provider
        found = Prepared(run.id, prefix, row.model or "", RUNTIME_FORMATS.get(row.runtime, ""), base_url + ROUTE)
        picked = await _picked(conn, run.id, prefix)
        usable = await gateway_accounts.usable_accounts(conn, _asker(run), worker_id, prefix, picked)
        if not usable.accounts:
            found.reasons = [usable.why(prefix)]
            return found
        found.held = await _held_key(conn, run.id, worker_id, {account.secret_id for account in usable.accounts})
    if found.held is not None:
        found.accounts = usable.accounts
        return found
    try:
        client = gateway_sync.client_of(app)
        connections = await _connections(client, usable.accounts, found.reasons)
        found.accounts = [account for account in usable.accounts if account.secret_id in connections]
        if not found.accounts:
            return found
        found.expires_at = datetime.now(UTC) + timedelta(seconds=row.timeout_s + GRACE)
        reach = [connections[account.secret_id] for account in found.accounts]
        made = await client.create_key(f"evo-run-{run.id}", reach, found.expires_at.isoformat())
    except (GatewayOff, GatewayError) as exc:
        found.accounts = []
        found.reasons.append(f"the model gateway made no key: {exc}")
        return found
    found.key_id, found.key = made.id, made.value
    return found


async def _insert(conn: AsyncConnection, values: dict) -> int:
    cl = tables.credential_leases
    return (await conn.execute(cl.insert().values(**values).returning(cl.c.id))).scalar_one()


async def _opened(conn: AsyncConnection, sealer: Sealer, lease_id: int) -> str | None:
    cl = tables.credential_leases
    row = (await conn.execute(select(cl.c.sealed_value, cl.c.nonce, cl.c.key_id).where(cl.c.id == lease_id))).one()
    try:
        return sealer.open(Sealed(row.sealed_value, row.nonce, row.key_id), lease_aad(lease_id))
    except Unsealable as exc:
        log.warning("a gateway key does not open", extra={"lease_id": lease_id, "why": str(exc)})
        return None


async def _record_key(conn: AsyncConnection, found: Prepared, run, worker_id: int, sealer: Sealer) -> int:
    """Write the key made now and the accounts it reaches; the run's earlier keys are marked revoked."""
    cl = tables.credential_leases
    earlier = and_(cl.c.run_id == run.id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
    await conn.execute(update(cl).values(revoked_at=func.greatest(func.now(), cl.c.issued_at)).where(earlier))
    common = {
        "run_id": run.id,
        "worker_id": worker_id,
        "provider": GATEWAY,
        "target": found.model,
        "external_id": found.key_id,
        "expires_at": found.expires_at,
        **run.lease_columns(),
    }
    lease_id = await _insert(conn, {**common, "secret_id": None})
    key = found.key.get_secret_value()
    sealed = sealer.seal(key, lease_aad(lease_id))  # bound to the lease's id, so inserted first
    values = {"sealed_value": sealed.ciphertext, "nonce": sealed.nonce, "key_id": sealed.key_id}
    values["token_hash"] = token_hash(run_token(key, lease_id))
    await conn.execute(update(cl).values(**values).where(cl.c.id == lease_id))
    for account in found.accounts:
        await _insert(conn, {**common, "secret_id": account.secret_id, "share_id": account.share_id})
    a = tables.accounts
    stamp = {"last_leased_at": func.now()}
    taken = pg_insert(a).values([{"secret_id": account.secret_id, **stamp} for account in found.accounts])
    await conn.execute(taken.on_conflict_do_update(index_elements=[a.c.secret_id], set_=stamp))
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
        cl = tables.credential_leases
        found.expires_at = (await conn.execute(select(cl.c.expires_at).where(cl.c.id == lease_id))).scalar_one()
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
        await gateway_sync.client_of(app).revoke_key(found.key_id)
    except (GatewayOff, GatewayError) as exc:
        log.warning("a gateway key made for a failed ask is left to its end", extra={"why": str(exc)})


# Taking keys back


def _ended_keys(batch: int):
    cl, r = tables.credential_leases, tables.runs
    over = or_(cl.c.revoked_at.is_not(None), cl.c.expires_at <= func.now(), r.c.state.not_in(runs.HELD_STATES))
    return (
        select(cl.c.id, cl.c.run_id, cl.c.external_id)
        .join_from(cl, r, r.c.id == cl.c.run_id)
        .where(cl.c.provider == GATEWAY, cl.c.secret_id.is_(None), cl.c.sealed_value.is_not(None), over)
        .order_by(cl.c.id)
        .limit(batch)
    )


def used_with(usage, used: dict) -> dict:
    """runs.usage ``usage`` with what the gateway counted for one more key of the run added: under ``gateway``, and
    its cost as total_cost_usd, the figure the run's budget, its page and the account figures read
    (``curator.run_cost``), for the gateway priced what the run's runtime cannot."""
    usage = dict(usage) if isinstance(usage, dict) else {}
    before = usage.get("gateway") if isinstance(usage.get("gateway"), dict) else {}
    merged = {key: (before.get(key) or 0) + value for key, value in used.items()}
    merged["cost_usd"] = round(merged["cost_usd"], 6)
    return {**usage, "gateway": merged, "total_cost_usd": merged["cost_usd"]}


async def _take(engine: AsyncEngine, row, used: dict) -> bool:
    """Drop the key of lease ``row``, end the leases of its accounts and add its usage to the run's; False when another
    sweep took it first."""
    cl, r = tables.credential_leases, tables.runs
    async with engine.begin() as conn:
        ended = func.greatest(func.now(), cl.c.issued_at)
        taken = update(cl).values(
            sealed_value=None, nonce=None, key_id=None, revoked_at=func.coalesce(cl.c.revoked_at, ended)
        )
        if not (await conn.execute(taken.where(cl.c.id == row.id, cl.c.sealed_value.is_not(None)))).rowcount:
            return False
        same = and_(cl.c.external_id == row.external_id, cl.c.provider == GATEWAY, cl.c.revoked_at.is_(None))
        await conn.execute(update(cl).values(revoked_at=ended).where(same))
        usage = (await conn.execute(select(r.c.usage).where(r.c.id == row.run_id).with_for_update())).scalar_one()
        await conn.execute(update(r).values(usage=used_with(usage, used)).where(r.c.id == row.run_id))
    return True


async def sweep(client: GatewayClient, engine: AsyncEngine, batch: int = SWEEP_BATCH) -> int:
    """Take back the keys of the runs that ended, as the module says; how many. A key the gateway fails on stays for
    the next sweep."""
    async with engine.connect() as conn:
        rows = (await conn.execute(_ended_keys(batch))).all()
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

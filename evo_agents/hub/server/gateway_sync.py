"""The hub is the source of truth of the model gateway: ``reconcile`` makes the gateway's connections what the provider
accounts in Postgres say, and ``GatewaySync`` runs it when the api starts, shortly after a write of a provider account
(``poke``) and every ``INTERVAL`` seconds, so a gateway that lost its volume is rebuilt on the next round.

Each live provider account whose catalog entry goes through the gateway is one connection named
``evo-<secret id>-<fingerprint>``: of the catalog's provider, or, for a custom endpoint (custom-openai,
custom-anthropic), of a provider node named and prefixed ``evo-<secret id>``. A catalog provider's connection takes the
URL of OmniRoute's registry when the account keeps the catalog's base URL, and the account's base URL as
providerSpecificData.baseUrl only when it names another: the catalog's base URL is the registry's endpoint without its
tail (/messages, /chat/completions), and some of OmniRoute's executors (zai, anthropic and others) call that override
as it is, so the catalog's own URL there answers 404. The fingerprint, an HMAC under a key derived from
EVO_HUB_GATEWAY_SECRET, covers the provider, that override, the API format and the key, so a change is seen although
the gateway never shows a key back; the connection is then updated in place and keeps its id, except one whose
override goes, which the gateway cannot unset: it is made again. A paused or
expired account's connection is inactive. A connection or node of a deleted account, or one the hub does not know, is
deleted. The key is opened only here, in the api's memory, and goes to the gateway alone: a log line names the account
by its secret id, and an error of the gateway by the call and the status (``gateway_client.GatewayError``).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
from dataclasses import dataclass

from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import model_catalog, tables
from evo_agents.hub.config import HubConfig
from evo_agents.hub.credentials import PROVIDER_KIND
from evo_agents.hub.log import register_secret
from evo_agents.hub.server.gateway_client import Connection, GatewayClient, GatewayError, GatewayOff, Node, derive
from evo_agents.hub.server.sealing import Sealed, Sealer, agent_provider_aad, secret_aad

log = logging.getLogger(__name__)

INTERVAL = 60.0  # seconds between two rounds when nothing pokes
DEBOUNCE = 0.2  # seconds a poke waits for the writes that follow it
NAME = re.compile(r"evo-(\d+)-([0-9a-f]{16})")  # a connection the hub made
NODE_KINDS = {"custom-openai": "openai-compatible", "custom-anthropic": "anthropic-compatible"}


@dataclass(frozen=True)
class Wanted:
    """What one provider account makes of the gateway."""

    secret_id: int
    provider: str  # the catalog's id
    base_url: str
    api_format: str
    key: str
    active: bool
    fingerprint: str
    override: str | None = None  # the base URL the connection is told, when not the registry's (module docstring)

    @property
    def name(self) -> str:
        return f"evo-{self.secret_id}-{self.fingerprint}"

    @property
    def node_name(self) -> str:
        return f"evo-{self.secret_id}"

    @property
    def node_kind(self) -> str | None:
        return NODE_KINDS.get(self.provider)

    @property
    def api_type(self) -> str:
        return "responses" if self.api_format == "openai-responses" else "chat"


def fingerprint(fingerprint_key: str, provider: str, base_url: str, api_format: str, key: str) -> str:
    message = "\n".join((provider, base_url, api_format, key)).encode()
    return hmac.new(fingerprint_key.encode(), message, "sha256").hexdigest()[:16]


async def wanted(engine: AsyncEngine, sealer: Sealer, fingerprint_key: str) -> dict[int, Wanted]:
    """The live provider accounts the gateway serves, by secret id, each with its key opened."""
    s, p, a = tables.secrets, tables.provider_accounts, tables.accounts
    statement = (
        select(s.c.id, s.c.owner_id, s.c.profile_id, s.c.name, s.c.sealed, s.c.nonce, s.c.key_id, s.c.expires_at)
        .add_columns(p.c.provider, p.c.base_url, p.c.api_format, a.c.paused_at, func.now().label("now"))
        .join_from(s, p, p.c.secret_id == s.c.id)
        .outerjoin(a, a.c.secret_id == s.c.id)
        .where(s.c.kind == PROVIDER_KIND, s.c.deleted_at.is_(None))
        .order_by(s.c.id)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(statement)).all()
    found = {}
    for row in rows:
        entry = model_catalog.find(row.provider)
        if entry is None or entry["via"] != "gateway":
            continue
        aad = (
            agent_provider_aad(row.profile_id, row.name)
            if row.profile_id
            else secret_aad(row.owner_id, row.name, PROVIDER_KIND)
        )
        try:
            key = sealer.open(Sealed(row.sealed, row.nonce, row.key_id), aad)
        except Exception as exc:  # a key sealed under another EVO_HUB_SECRETS_KEY: its connection goes
            log.warning(
                "gateway: provider account key does not open", extra={"secret_id": row.id, "error": type(exc).__name__}
            )
            continue
        register_secret(key)
        active = row.paused_at is None and (row.expires_at is None or row.expires_at > row.now)
        override = None if _catalogs(entry, row.base_url) else row.base_url
        mark = fingerprint(fingerprint_key, row.provider, override or "", row.api_format, key)
        found[row.id] = Wanted(row.id, row.provider, row.base_url, row.api_format, key, active, mark, override)
    return found


def _catalogs(entry: dict, base_url: str) -> bool:
    """Whether ``base_url`` is the catalog's own for ``entry``, whose connection then takes the registry's URL."""
    return bool(entry.get("base_url")) and base_url.rstrip("/") == entry["base_url"].rstrip("/")


async def _nodes(client: GatewayClient, wanting: dict[int, Wanted]) -> dict[int, str]:
    """The node id of each account of a custom endpoint, made when missing or different; other nodes are deleted."""
    by_name = {want.node_name: want for want in wanting.values() if want.node_kind}
    kept: dict[int, str] = {}
    for node in await client.nodes():
        want = by_name.get(node.name)
        if want is None or want.secret_id in kept or not _same_node(node, want):
            await client.delete_node(node.id)
            continue
        kept[want.secret_id] = node.id
    for want in by_name.values():
        if want.secret_id not in kept:
            made = await client.create_node(
                want.node_name, want.node_name, want.node_kind, want.api_type, want.base_url
            )
            kept[want.secret_id] = made.id
    return kept


def _same_node(node: Node, want: Wanted) -> bool:
    return (node.kind, node.prefix, node.api_type, node.base_url.rstrip("/")) == (
        want.node_kind,
        want.node_name,
        want.api_type,
        want.base_url.rstrip("/"),
    )


async def _connections(
    client: GatewayClient, wanting: dict[int, Wanted], nodes: dict[int, str]
) -> dict[int, Connection]:
    """The hub's connection of each account; every other connection is deleted."""
    kept: dict[int, Connection] = {}
    for found in await client.connections():
        match = NAME.fullmatch(found.name)
        want = wanting.get(int(match.group(1))) if match else None
        provider = (nodes.get(want.secret_id) or want.provider) if want else None
        if want is None or want.secret_id in kept or found.provider != provider:
            await client.delete_connection(found.id)
            continue
        kept[want.secret_id] = found
    return kept


async def _apply(client: GatewayClient, want: Wanted, found: Connection | None, node_id: str | None) -> str | None:
    """Make the account's connection what it says; the change made, or None."""
    base_url = None if node_id else want.override
    if found is not None and found.name != want.name and found.base_url and not base_url and not node_id:
        await client.delete_connection(found.id)  # PUT cannot unset providerSpecificData.baseUrl
        found = None
    if found is None:
        found = await client.create_connection(node_id or want.provider, want.name, want.key, base_url)
        if want.active:
            await client.update_connection(found.id, active=True)
        return "created"
    if found.name != want.name:
        await client.update_connection(found.id, name=want.name, key=want.key, base_url=base_url, active=want.active)
        return "updated"
    if found.active != want.active:
        await client.update_connection(found.id, active=want.active)
        return "activated" if want.active else "deactivated"
    return None


async def reconcile(
    client: GatewayClient, engine: AsyncEngine, sealer: Sealer, fingerprint_key: str, seen: dict | None = None
) -> dict:
    """One round: the gateway's nodes and connections made what the provider accounts say. What changed, counted;
    ``seen`` gets the connection the gateway had of each account, with what it last saw of it."""
    wanting = await wanted(engine, sealer, fingerprint_key)
    nodes = await _nodes(client, wanting)
    kept = await _connections(client, wanting, nodes)  # listed after the nodes: a deleted node took its connections
    if seen is not None:
        seen.clear()
        seen.update(kept)
    changes: dict[str, int] = {}
    for secret_id, want in wanting.items():
        change = await _apply(client, want, kept.get(secret_id), nodes.get(secret_id))
        if change:
            changes[change] = changes.get(change, 0) + 1
            log.info(f"gateway: connection {change}", extra={"secret_id": secret_id})
    changes["accounts"] = len(wanting)
    return changes


class GatewaySync:
    """Runs ``reconcile`` at start, after a ``poke`` and every ``interval`` seconds, one round at a time."""

    def __init__(self, client: GatewayClient, engine: AsyncEngine, sealer: Sealer, secret: str, interval=INTERVAL):
        self.client, self.engine, self.sealer, self.interval = client, engine, sealer, interval
        self._fingerprint_key = derive(secret, "fingerprint")
        self._wake = asyncio.Event()
        self._asked = 1  # the start asks for the first round
        self._done = 0
        self._changed = asyncio.Condition()
        self.last_error: str | None = None
        self.status: dict[int, Connection] = {}  # the connection of each account at the last round, by secret id

    def poke(self) -> None:
        self._asked += 1
        self._wake.set()

    async def settled(self) -> None:
        """Wait until a round started after the last poke has ended."""
        asked = self._asked
        async with self._changed:
            await self._changed.wait_for(lambda: self._done >= asked)

    async def run(self) -> None:
        while True:
            asked = self._asked
            self._wake.clear()
            await self.round()
            async with self._changed:
                self._done = asked
                self._changed.notify_all()
            if self._asked == asked:
                try:
                    await asyncio.wait_for(self._wake.wait(), self.interval)
                except TimeoutError:
                    self._asked += 1
                    continue
            await asyncio.sleep(DEBOUNCE)

    async def round(self) -> None:
        await self._sweep()
        try:
            changes = await reconcile(self.client, self.engine, self.sealer, self._fingerprint_key, self.status)
        except GatewayError as exc:
            self.last_error = str(exc)
            log.warning("gateway: reconcile failed", extra={"error": str(exc)})
            return
        except Exception as exc:  # the next round tries again; the loop must not die
            self.last_error = type(exc).__name__
            log.exception("gateway: reconcile failed", extra={"error": type(exc).__name__})
            return
        self.last_error = None
        log.debug("gateway: reconciled", extra={"changes": changes})

    async def _sweep(self) -> None:
        """Take back the keys of the runs that ended (``gateway_leases.sweep``); a failure waits for the next round."""
        from evo_agents.hub.server import gateway_leases  # it reads the gateway's connections through this module

        try:
            await gateway_leases.sweep(self.client, self.engine)
        except Exception as exc:  # the loop must not die
            log.exception("gateway: the sweep of run keys failed", extra={"error": type(exc).__name__})


def poke(app: FastAPI) -> None:
    """Ask for a round soon: a provider account was written. Nothing while the gateway is off."""
    sync = getattr(app.state, "gateway_sync", None)
    if sync is not None:
        sync.poke()


def client_of(app: FastAPI) -> GatewayClient:
    """The gateway's client; ``GatewayOff`` while the hub runs without one."""
    client = getattr(app.state, "gateway", None)
    if client is None:
        raise GatewayOff()
    return client


@dataclass(frozen=True)
class Running:
    """What the lifespan closes when the api stops: the sync's task and the gateway's client."""

    app: FastAPI

    async def aclose(self) -> None:
        await stop(self.app)


async def start(app: FastAPI, config: HubConfig) -> Running:
    """Open the gateway's client and start its sync, when EVO_HUB_GATEWAY_SECRET is set; the app's state may carry
    ``gateway_transport`` (an httpx transport, in tests) and ``gateway_interval``."""
    client = GatewayClient.from_config(config, getattr(app.state, "gateway_transport", None))
    app.state.gateway, app.state.gateway_sync, app.state.gateway_task = client, None, None
    if client is None:
        log.info("model gateway off", extra={"missing": config.gateway_missing()})
        return Running(app)
    if app.state.sealer is None:
        log.warning("model gateway: no provider account syncs without EVO_HUB_SECRETS_KEY")
        return Running(app)
    interval = getattr(app.state, "gateway_interval", INTERVAL)
    sync = GatewaySync(client, app.state.engine, app.state.sealer, config.gateway_secret, interval)
    app.state.gateway_sync = sync
    app.state.gateway_task = asyncio.create_task(sync.run(), name="gateway-sync")
    return Running(app)


async def stop(app: FastAPI) -> None:
    task = getattr(app.state, "gateway_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    client = getattr(app.state, "gateway", None)
    if client is not None:
        await client.aclose()

"""The plugin market on the hub (plan plugin-market, criterion a5): the markets a hub admin adds, and what every
signed-in member reads of them.

A market source is a GitHub repo holding ``market.json`` (``evo_agents.plugins.market``); the hub reads each through
``evo_agents.hub.market_refresh`` every six hours and on a hub admin's refresh, and keeps the last index that read.
The operator's market (EVO_HUB_MARKET_DEFAULT_SOURCE, maycuatroi1/evo-agents-market unless set) is there from the
first read on, and only the variable removes it.

- GET /v1/market (``q``, ``industry``, ``category``, ``source``): the plugins of every market, each with its
  publisher and signer, price, latest release and its status on this hub: not installed, installed, an upgrade
  waiting, in the hub's image (a core plugin), not in the image, a licence needed (a paid plugin), or unavailable
  (no release that reads); with the industries and categories to filter by and each market's state;
- GET /v1/market/{source}/{plugin}: one plugin, with the permissions its latest release asks for and whether the hub
  trusts its signer already;
- GET|POST /v1/market/sources, DELETE /v1/market/sources/{id}, POST /v1/market/sources/{id}/refresh: the markets,
  for hub admins; a private repo's token is sealed with the hub's key (``market-source:{id}``) and never shown.

Installing and asking for an install are ``evo_agents.hub.server.market.service.install``. The routes under
/v1/market belong to the feature ``market`` (``evo_agents.hub.features``): a hub admin who turns it off gets 404 for
them. Adding and removing a source are audited as market.source.add and market.source.remove, naming
``market:<repo>``. ``evo-agents hub market`` is the client.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.market_refresh import place_builtin, refresh
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, NotFound
from evo_agents.hub.server.credentials.models.sealing import market_source_aad
from evo_agents.hub.server.market.models.market import (
    NO_KEY,
    MarketBrief,
    MarketList,
    MarketPermission,
    MarketPlugin,
    MarketPluginDetail,
    MarketPrice,
    MarketPublisher,
    MarketRelease,
    MarketSource,
    MarketSourceIn,
    MarketSources,
    Status,
)
from evo_agents.hub.server.market.repository import market as repository
from evo_agents.hub.server.market.repository.market import installed_plugins, source_rows
from evo_agents.hub.server.plugins.service.store import trusted_publishers
from evo_agents.hub.server.security import Principal
from evo_agents.plugins.compat import plugin_version_key
from evo_agents.plugins.manifest import PERMISSION_TEXT
from evo_agents.plugins.market import signer_identity, trusts

# Reading


def _newer(latest: str | None, installed: str | None) -> bool:
    try:
        return bool(latest and installed) and plugin_version_key(latest) > plugin_version_key(installed)  # pyright: ignore[reportArgumentType]
    except ValueError:
        return False


def status_of(item: dict, latest: dict | None, held: dict | None) -> tuple[Status, str | None]:
    """The status of market entry ``item`` on this hub and the version installed, by what the store holds."""
    if item["tier"] == "core":
        return ("in_image" if held and held["image"] else "not_in_image"), (held or {}).get("installed_version")
    installed = held["installed_version"] if held and held["publisher"] == item["publisher"]["id"] else None
    if installed is not None:
        return ("upgrade" if _newer((latest or {}).get("version"), installed) else "installed"), installed
    if item["price"]["kind"] != "free":
        return "needs_license", None
    if not latest or latest.get("error") or not latest.get("version"):
        return "unavailable", None
    return "not_installed", None


def _release(latest: dict | None) -> MarketRelease | None:
    if latest is None:
        return None
    names = latest.get("permissions") or []
    permissions = [MarketPermission(name=name, text=PERMISSION_TEXT.get(name, name)) for name in names]  # pyright: ignore[reportArgumentType]
    return MarketRelease(
        tag=latest.get("tag"), version=latest.get("version"), permissions=permissions, error=latest.get("error")
    )


def plugin_model(row, item: dict, held: dict | None) -> dict:
    latest = (row.releases or {}).get(item["id"])
    status, installed = status_of(item, latest, held)
    publisher = item["publisher"]
    try:
        identity = signer_identity(publisher["signer"])
    except ValueError:
        identity = publisher["signer"]
    fields = ("id", "name", "description", "industries", "categories", "repo", "tier", "package", "icon")
    return {
        **{name: item.get(name) for name in fields},
        **{name: item.get(name) for name in ("homepage", "license", "requires")},
        "source": row.id,
        "market": row.index.get("title") or row.index["name"],
        "publisher": MarketPublisher(**publisher, identity=identity),
        "price": MarketPrice(**item["price"]),
        "status": status,
        "installed_version": installed,
        "latest": _release(latest),
    }


def _matches(item: dict, q: str | None, industry: str | None, category: str | None) -> bool:
    if industry and industry not in item["industries"]:
        return False
    if category and category not in item["categories"]:
        return False
    if q:
        words = q.lower().split()
        text = " ".join(
            [
                item["id"],
                item["name"],
                item["description"],
                item["publisher"].get("name") or "",
                item["publisher"]["id"],
            ]
        ).lower()
        return all(word in text for word in words)
    return True


def entry_of(row, plugin_id: str) -> dict:
    """Market entry ``plugin_id`` of source ``row``; 404 when its index does not list it."""
    item = next((item for item in (row.index or {}).get("plugins", ()) if item["id"] == plugin_id), None)
    if item is None:
        raise NotFound(f"market {row.repo} lists no plugin {plugin_id}: see `evo-agents hub market list`")
    return item


async def source_row(conn: AsyncConnection, source_id: int):
    rows = await source_rows(conn, source_id)
    if not rows:
        raise NotFound(f"no market source {source_id}: see `evo-agents hub market source list`")
    return rows[0]


def brief(row) -> dict:
    index = row.index or {}
    return {
        "id": row.id,
        "repo": row.repo,
        "name": index.get("name"),
        "title": index.get("title"),
        "fetched_at": row.fetched_at,
        "error": row.last_error,
    }


async def market_list(
    conn: AsyncConnection, q: str | None, industry: str | None, category: str | None, source: int | None
) -> MarketList:
    """The plugins of every market, filtered, with the industries and categories of them all and each market."""
    rows = await source_rows(conn)
    held = await installed_plugins(conn)
    found, industries, categories = [], set(), set()
    for row in rows:
        for item in (row.index or {}).get("plugins", ()):
            industries.update(item["industries"])
            categories.update(item["categories"])
            if (source is None or row.id == source) and _matches(item, q, industry, category):
                found.append(MarketPlugin(**plugin_model(row, item, held.get(item["id"]))))
    markets = [MarketBrief(**brief(row)) for row in rows]
    return MarketList(plugins=found, industries=sorted(industries), categories=sorted(categories), markets=markets)


async def market_plugin(conn: AsyncConnection, config, source: int, plugin: str) -> MarketPluginDetail:
    """Plugin ``plugin`` of market ``source``, with whether the hub trusts its signer and may trust it."""
    row = await source_row(conn, source)
    item = entry_of(row, plugin)
    held = await installed_plugins(conn)
    publishers = await trusted_publishers(conn, config)
    publisher = item["publisher"]
    try:
        trusted = trusts(publishers, publisher["id"], publisher["signer"])
    except ValueError:
        trusted = False
    may = not config.plugin_publishers_locked and publisher["id"] not in config.plugin_core_publishers
    return MarketPluginDetail(**plugin_model(row, item, held.get(plugin)), trusted=trusted, may_trust=may)


# The sources


def source_model(row) -> MarketSource:
    return MarketSource(
        **brief(row),
        ref=row.ref,
        path=row.path,
        builtin=row.builtin,
        private=row.token is not None,
        added_by=row.login,
        added_at=row.added_at,
        plugins=len((row.index or {}).get("plugins", ())),
    )


async def sources_model(request: Request) -> MarketSources:
    config = request.app.state.config
    await place_builtin(request.app.state.engine, config)
    async with request.app.state.engine.begin() as conn:
        rows = await source_rows(conn)
    return MarketSources(
        sources=[source_model(row) for row in rows],
        default_source=config.market_default_source,
        publishers_locked=config.plugin_publishers_locked,
    )


async def refreshed(request: Request, source_id: int) -> MarketSource:
    state = request.app.state
    await refresh(
        state.engine,
        state.config,
        state.sealer,
        source_id=source_id,
        transport=getattr(state, "market_transport", None),
    )
    async with state.engine.begin() as conn:
        return source_model(await source_row(conn, source_id))


async def add_source(request: Request, body: MarketSourceIn, user: Principal) -> MarketSource:
    """Add market ``body`` and read it now; 409 when the hub reads it already, 503 for a token without the key."""
    state = request.app.state
    if body.token is not None and state.sealer is None:
        raise HTTPException(503, NO_KEY)
    async with state.engine.begin() as conn:
        source_id = await repository.add_source(conn, body.repo, body.ref, body.path, user.user_id)
        if source_id is None:
            raise Conflict(f"this hub reads {body.path} of {body.repo} at {body.ref} already")
        if body.token is not None:
            await repository.set_token(
                conn, source_id, state.sealer.seal_stored(body.token, market_source_aad(source_id))
            )
        target = f"market:{body.repo}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.MARKET_SOURCE_ADD, target=target
        )
    return await refreshed(request, source_id)


async def remove_source(conn: AsyncConnection, source_id: int, user: Principal) -> None:
    """Stop reading market ``source_id``; 409 for the operator's market."""
    row = await source_row(conn, source_id)
    if row.builtin:
        raise Conflict(
            f"{row.repo} is this hub's default market: its operator removes it with EVO_HUB_MARKET_DEFAULT_SOURCE="
        )
    await repository.delete_source(conn, source_id)
    target = f"market:{row.repo}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.MARKET_SOURCE_REMOVE, target=target
    )

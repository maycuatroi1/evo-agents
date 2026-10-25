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

Installing and asking for an install are ``evo_agents.hub.server.market_install``. The routes under /v1/market
belong to the feature ``market`` (``evo_agents.hub.features``): a hub admin who turns it off gets 404 for them.
Adding and removing a source are audited as market.source.add and market.source.remove, naming ``market:<repo>``.
``evo-agents hub market`` is the client.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.market_refresh import place_builtin, refresh
from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_store import IMAGE, trusted_publishers
from evo_agents.hub.server.sealing import market_source_aad
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin
from evo_agents.plugins.compat import plugin_version_key
from evo_agents.plugins.manifest import PERMISSION_TEXT, PLUGIN_ID
from evo_agents.plugins.market import REPO, signer_identity, trusts

STATUSES = ("not_installed", "installed", "upgrade", "in_image", "not_in_image", "needs_license", "unavailable")
Status = Literal["not_installed", "installed", "upgrade", "in_image", "not_in_image", "needs_license", "unavailable"]
MAX_QUERY_CHARS = 200
NO_KEY = (
    "this hub keeps no secrets: a private market's token needs EVO_HUB_SECRETS_KEY, the key that seals it; ask its "
    "operator to set it"
)
PluginId = Annotated[str, Path(pattern=PLUGIN_ID, description="the plugin's id, as the market lists it")]
SourceId = Annotated[int, Path(ge=1, description="the market source's id, as GET /v1/market/sources gives it")]
REFUSALS = {404: {"model": ErrorBody}}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (404, 409, 422, 503)}
ANSWERS = {401: {"model": ErrorBody}, 403: {"model": ErrorBody}}

router = APIRouter(prefix="/v1/market", tags=["market"], responses=ANSWERS)
sources_router = APIRouter(
    prefix="/v1/market/sources", tags=["market"], dependencies=[Depends(admin)], responses=ANSWERS
)


# Models


class MarketPermission(BaseModel):
    name: str
    text: str = Field(description="what the permission lets the plugin do, as a hub admin grants it")


class MarketRelease(BaseModel):
    tag: str | None = None
    version: str | None = None
    permissions: list[MarketPermission] = Field(default_factory=list)
    error: str | None = Field(None, description="why the latest release does not read, when it does not")


class MarketPublisher(BaseModel):
    id: str
    name: str | None = None
    signer: str = Field(description="what the market says signs its releases: sigstore:<workflow> or ed25519:<key>")
    identity: str = Field(description="how the signer reads among a publisher's identities")


class MarketPrice(BaseModel):
    kind: Literal["free", "subscription"]
    usd_per_hub_month: float | None = None
    seller: str | None = None


class MarketPlugin(BaseModel):
    source: int = Field(description="the id of the market source that lists it")
    market: str = Field(description="the market's name")
    id: str
    name: str
    description: str
    industries: list[str]
    categories: list[str]
    repo: str
    publisher: MarketPublisher
    tier: Literal["isolated", "core"]
    package: str | None = None
    price: MarketPrice
    icon: str | None = None
    homepage: str | None = None
    license: str | None = None
    requires: str | None = None
    status: Status
    installed_version: str | None = None
    latest: MarketRelease | None = Field(None, description="its latest release, for a free isolated plugin")


class MarketPluginDetail(MarketPlugin):
    trusted: bool = Field(description="whether the hub trusts the signer for the publisher already")
    may_trust: bool = Field(description="whether a hub admin may trust it here (not locked, not a core publisher)")


class MarketBrief(BaseModel):
    id: int
    repo: str
    name: str | None = None
    title: str | None = None
    fetched_at: datetime | None = None
    error: str | None = None


class MarketList(BaseModel):
    plugins: list[MarketPlugin]
    industries: list[str] = Field(description="every industry the markets list, to filter by")
    categories: list[str] = Field(description="every category the markets list, to filter by")
    markets: list[MarketBrief]


class MarketSource(MarketBrief):
    ref: str
    path: str
    builtin: bool = Field(description="the operator's market (EVO_HUB_MARKET_DEFAULT_SOURCE), which stays")
    private: bool = Field(description="whether the hub reads it with a token")
    added_by: str | None = None
    added_at: datetime
    plugins: int = Field(description="how many plugins its index lists")


class MarketSources(BaseModel):
    sources: list[MarketSource]
    default_source: str | None = Field(description="EVO_HUB_MARKET_DEFAULT_SOURCE")
    publishers_locked: bool = Field(description="EVO_HUB_PLUGIN_PUBLISHERS_LOCKED: hub admins may trust no publisher")


class MarketSourceIn(BaseModel):
    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO")]
    ref: Annotated[str, Field(min_length=1, max_length=255)] = "main"
    path: Annotated[str, Field(min_length=1, max_length=500, pattern=r"^[^\s]+$")] = "market.json"
    token: Annotated[str | None, Field(min_length=1, max_length=500)] = Field(
        None, description="a GitHub token that reads a private repo; sealed, never shown again"
    )


# Reading


async def installed_plugins(conn: AsyncConnection) -> dict[str, dict]:
    """name -> {installed_version, publisher, image} of every plugin the hub's store holds."""
    p, v = tables.plugins, tables.plugin_versions
    image = func.bool_or(v.c.signature_kind == IMAGE)
    query = (
        select(p.c.name, p.c.installed_version, p.c.publisher, p.c.disabled_reason, image.label("image"))
        .join_from(p, v, v.c.plugin_id == p.c.id, isouter=True)
        .group_by(p.c.id)
    )
    return {row.name: row._asdict() for row in await conn.execute(query)}


def _newer(latest: str | None, installed: str | None) -> bool:
    try:
        return bool(latest and installed) and plugin_version_key(latest) > plugin_version_key(installed)
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
    permissions = [MarketPermission(name=name, text=PERMISSION_TEXT.get(name, name)) for name in names]
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


async def source_rows(conn: AsyncConnection, source_id: int | None = None) -> list:
    s, users = tables.market_sources, tables.users
    query = (
        select(s, users.c.login)
        .join_from(s, users, users.c.id == s.c.added_by, isouter=True)
        .order_by(s.c.builtin.desc(), s.c.id)
    )
    if source_id is not None:
        query = query.where(s.c.id == source_id)
    return (await conn.execute(query)).all()


def entry_of(row, plugin_id: str) -> dict:
    """Market entry ``plugin_id`` of source ``row``; 404 when its index does not list it."""
    item = next((item for item in (row.index or {}).get("plugins", ()) if item["id"] == plugin_id), None)
    if item is None:
        raise HTTPException(404, f"market {row.repo} lists no plugin {plugin_id}: see `evo-agents hub market list`")
    return item


async def source_row(conn: AsyncConnection, source_id: int):
    rows = await source_rows(conn, source_id)
    if not rows:
        raise HTTPException(404, f"no market source {source_id}: see `evo-agents hub market source list`")
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


@router.get("", response_model=MarketList)
async def market(
    request: Request,
    user: CurrentUser,
    q: Annotated[str | None, Query(max_length=MAX_QUERY_CHARS)] = None,
    industry: Annotated[str | None, Query(max_length=64)] = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    source: Annotated[int | None, Query(ge=1)] = None,
) -> MarketList:
    """The plugins of every market this hub reads, by market then as each lists them; filtered by the words of
    ``q`` (in the id, name, description and publisher), an industry, a category and a market."""
    async with request.app.state.engine.begin() as conn:
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


async def _all_sources(request: Request) -> list:
    async with request.app.state.engine.begin() as conn:
        return await source_rows(conn)


@router.get("/{source}/{plugin}", response_model=MarketPluginDetail, responses=REFUSALS)
async def market_plugin(request: Request, source: SourceId, plugin: PluginId, user: CurrentUser) -> MarketPluginDetail:
    """One plugin of one market, with the permissions its latest release asks for."""
    config = request.app.state.config
    async with request.app.state.engine.begin() as conn:
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
    rows = await _all_sources(request)
    return MarketSources(
        sources=[source_model(row) for row in rows],
        default_source=config.market_default_source,
        publishers_locked=config.plugin_publishers_locked,
    )


@sources_router.get("", response_model=MarketSources)
async def sources(request: Request) -> MarketSources:
    """The markets this hub reads, the operator's first."""
    return await sources_model(request)


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


@sources_router.post("", response_model=MarketSource, responses=WRITE_REFUSALS)
async def add_source(request: Request, body: MarketSourceIn, user: AdminUser) -> MarketSource:
    """Add a market and read it now; a market that does not read is kept with the error, to refresh once fixed."""
    state = request.app.state
    if body.token is not None and state.sealer is None:
        raise HTTPException(503, NO_KEY)
    s = tables.market_sources
    async with state.engine.begin() as conn:
        added = await conn.execute(
            pg_insert(s)
            .values(repo=body.repo, ref=body.ref, path=body.path, added_by=user.user_id)
            .on_conflict_do_nothing()
            .returning(s.c.id)
        )
        source_id = added.scalar()
        if source_id is None:
            raise HTTPException(409, f"this hub reads {body.path} of {body.repo} at {body.ref} already")
        if body.token is not None:
            sealed = state.sealer.seal_stored(body.token, market_source_aad(source_id))
            await conn.execute(s.update().where(s.c.id == source_id).values(token=sealed))
        target = f"market:{body.repo}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.MARKET_SOURCE_ADD, target=target
        )
    return await refreshed(request, source_id)


@sources_router.delete("/{source_id}", response_model=MarketSources, responses=WRITE_REFUSALS)
async def remove_source(request: Request, source_id: SourceId, user: AdminUser) -> MarketSources:
    """Stop reading a market; the plugins installed from it stay installed. The operator's market stays."""
    s = tables.market_sources
    async with request.app.state.engine.begin() as conn:
        row = await source_row(conn, source_id)
        if row.builtin:
            raise HTTPException(
                409,
                f"{row.repo} is this hub's default market: its operator removes it with EVO_HUB_MARKET_DEFAULT_SOURCE=",
            )
        await conn.execute(delete(s).where(s.c.id == source_id))
        target = f"market:{row.repo}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.MARKET_SOURCE_REMOVE, target=target
        )
    return await sources_model(request)


@sources_router.post("/{source_id}/refresh", response_model=MarketSource, responses=WRITE_REFUSALS)
async def refresh_source(request: Request, source_id: SourceId, user: AdminUser) -> MarketSource:
    """Read a market again now: its index, and the latest release of each of its free plugins."""
    async with request.app.state.engine.begin() as conn:
        await source_row(conn, source_id)
    return await refreshed(request, source_id)

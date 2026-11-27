"""The routes GET /v1/market, GET /v1/market/{source}/{plugin} and the market sources under /v1/market/sources, which
read and keep the markets through ``evo_agents.hub.server.market.service.market``; that module says what each does."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from evo_agents.hub.server.market.models.market import (
    ANSWERS,
    MAX_QUERY_CHARS,
    REFUSALS,
    WRITE_REFUSALS,
    MarketList,
    MarketPluginDetail,
    MarketSource,
    MarketSourceIn,
    MarketSources,
    PluginId,
    SourceId,
)
from evo_agents.hub.server.market.service import market as service
from evo_agents.hub.server.security import AdminUser, CurrentUser, admin

router = APIRouter(prefix="/v1/market", tags=["market"], responses=ANSWERS)
sources_router = APIRouter(
    prefix="/v1/market/sources", tags=["market"], dependencies=[Depends(admin)], responses=ANSWERS
)


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
        return await service.market_list(conn, q, industry, category, source)


@router.get("/{source}/{plugin}", response_model=MarketPluginDetail, responses=REFUSALS)
async def market_plugin(request: Request, source: SourceId, plugin: PluginId, user: CurrentUser) -> MarketPluginDetail:
    """One plugin of one market, with the permissions its latest release asks for."""
    async with request.app.state.engine.begin() as conn:
        return await service.market_plugin(conn, request.app.state.config, source, plugin)


@sources_router.get("", response_model=MarketSources)
async def sources(request: Request) -> MarketSources:
    """The markets this hub reads, the operator's first."""
    return await service.sources_model(request)


@sources_router.post("", response_model=MarketSource, responses=WRITE_REFUSALS)
async def add_source(request: Request, body: MarketSourceIn, user: AdminUser) -> MarketSource:
    """Add a market and read it now; a market that does not read is kept with the error, to refresh once fixed."""
    return await service.add_source(request, body, user)


@sources_router.delete("/{source_id}", response_model=MarketSources, responses=WRITE_REFUSALS)
async def remove_source(request: Request, source_id: SourceId, user: AdminUser) -> MarketSources:
    """Stop reading a market; the plugins installed from it stay installed. The operator's market stays."""
    async with request.app.state.engine.begin() as conn:
        await service.remove_source(conn, source_id, user)
    return await service.sources_model(request)


@sources_router.post("/{source_id}/refresh", response_model=MarketSource, responses=WRITE_REFUSALS)
async def refresh_source(request: Request, source_id: SourceId, user: AdminUser) -> MarketSource:
    """Read a market again now: its index, and the latest release of each of its free plugins."""
    async with request.app.state.engine.begin() as conn:
        await service.source_row(conn, source_id)
    return await service.refreshed(request, source_id)

"""Health endpoints, both without sign-in.

``/v1/health`` reports the package version and the schema revision read from Postgres, and the blob store (R2)
after a HeadBucket on its bucket. It answers 503 naming the component that failed when Postgres does not answer
within HEALTH_TIMEOUT or the bucket does not answer; both are checked at the same time. A hub without the
EVO_HUB_S3_* variables reports ``r2: unconfigured`` and stays healthy, as it was configured. ``gateway`` is the model
gateway (``gateway_client``): ``off`` without EVO_HUB_GATEWAY_SECRET, else ``ok`` when its health check answers and
``error``, which fails the check, when it does not. ``broker`` is the event bus (``evo_agents.hub.bus``): ``off``
without EVO_HUB_BROKER_URL, else ``ok`` when the broker takes a connection and ``error`` when it does not, which leaves
the status ``ok``: the hub still delivers through Postgres and its jobs. ``cache`` is the shared cache
(``evo_agents.hub.cache``): ``off`` without EVO_HUB_REDIS_URL, each process keeping its own, else ``ok`` when Redis
answers a PING and ``error`` when it does not, which leaves the status ``ok`` too: the hub reads Postgres instead.
``/v1/health/live``
only says the process serves requests: it is the container healthcheck, and a database or R2 outage must not
get the container restarted.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import psycopg
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import exc as sa_exc

from evo_agents import __version__
from evo_agents.hub import bus
from evo_agents.hub.db import schema_revision

log = logging.getLogger(__name__)

HEALTH_TIMEOUT = 3.0  # seconds for the database to hand out a connection and answer
NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(prefix="/v1", tags=["health"])


class Health(BaseModel):
    status: Literal["ok", "unavailable"]
    version: str = Field(description="evo-agents package version")
    schema_revision: str | None = Field(alias="schema", description="Alembic revision applied to the database")
    db: Literal["ok", "unavailable"]
    r2: Literal["ok", "unavailable", "unconfigured"] = Field(description="the blob store, by a HeadBucket")
    gateway: Literal["ok", "error", "off"] = Field(description="the model gateway, by its health check")
    broker: Literal["ok", "error", "off"] = Field(
        description="the event bus's broker, by a connection; its error leaves the status ok"
    )
    cache: Literal["ok", "error", "off"] = Field(
        description="the shared cache's Redis, by a PING; its error leaves the status ok"
    )
    failed: list[str] = Field(default_factory=list, description="components that did not answer")


class Live(BaseModel):
    status: Literal["ok"]


@router.get(
    "/health",
    response_model=Health,
    responses={503: {"model": Health, "description": "a component did not answer"}},
)
async def health(request: Request, response: Response):
    store = request.app.state.blobs
    r2 = asyncio.ensure_future(store.status()) if store is not None else None
    gateway = getattr(request.app.state, "gateway", None)
    gateway_up = asyncio.ensure_future(gateway.healthy()) if gateway is not None else None
    config = getattr(request.app.state, "config", None)
    broker_url = getattr(config, "broker_url", None)
    broker = asyncio.ensure_future(bus.check(broker_url)) if broker_url is not None else None
    shared = getattr(request.app.state, "cache", None)
    cached = asyncio.ensure_future(shared.check()) if shared is not None else None
    try:
        schema, db = await schema_revision(request.app.state.engine, HEALTH_TIMEOUT), "ok"
    except (psycopg.Error, sa_exc.DBAPIError, OSError, TimeoutError) as exc:  # PoolTimeout arrives as a DBAPIError
        log.warning("health check failed: database unavailable", extra={"error": type(exc).__name__})
        schema, db = None, "unavailable"
    r2_status = "unconfigured" if r2 is None else await r2
    if r2_status == "unavailable":
        log.warning("health check failed: blob store unavailable", extra={"bucket": store.bucket})
    gateway_status = "off" if gateway_up is None else "ok" if await gateway_up else "error"
    if gateway_status == "error":
        log.warning("health check failed: model gateway unavailable", extra={"url": gateway.url})
    broker_status = "off" if broker is None else await broker  # an error is logged, and fails nothing
    cache_status = "off" if cached is None else await cached  # ...and so is the cache's
    statuses = (("db", db), ("r2", r2_status), ("gateway", gateway_status))
    failed = [name for name, status in statuses if status in ("unavailable", "error")]
    body = Health(
        status="unavailable" if failed else "ok",
        version=__version__,
        schema=schema,
        db=db,
        r2=r2_status,
        gateway=gateway_status,
        broker=broker_status,
        cache=cache_status,
        failed=failed,
    )
    if failed:
        return JSONResponse(body.model_dump(by_alias=True), status_code=503, headers=NO_STORE)
    response.headers.update(NO_STORE)
    return body


@router.get("/health/live", response_model=Live)
async def live(response: Response) -> Live:
    response.headers.update(NO_STORE)
    return Live(status="ok")

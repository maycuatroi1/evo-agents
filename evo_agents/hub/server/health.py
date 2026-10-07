"""Health endpoints, both without sign-in.

``/v1/health`` reports the package version and the schema revision read from Postgres, and the blob store (R2)
after a HeadBucket on its bucket. It answers 503 naming the component that failed when Postgres does not answer
within HEALTH_TIMEOUT or the bucket does not answer; both are checked at the same time. A hub without the
EVO_HUB_S3_* variables reports ``r2: unconfigured`` and stays healthy, as it was configured. ``/v1/health/live``
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
    try:
        schema, db = await schema_revision(request.app.state.engine, HEALTH_TIMEOUT), "ok"
    except (psycopg.Error, sa_exc.DBAPIError, OSError, TimeoutError) as exc:  # PoolTimeout arrives as a DBAPIError
        log.warning("health check failed: database unavailable", extra={"error": type(exc).__name__})
        schema, db = None, "unavailable"
    r2_status = "unconfigured" if r2 is None else await r2
    if r2_status == "unavailable":
        log.warning("health check failed: blob store unavailable", extra={"bucket": store.bucket})
    failed = [name for name, status in (("db", db), ("r2", r2_status)) if status == "unavailable"]
    body = Health(
        status="unavailable" if failed else "ok",
        version=__version__,
        schema=schema,
        db=db,
        r2=r2_status,
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

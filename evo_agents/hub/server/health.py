"""Health endpoints, both without sign-in.

``/v1/health`` reports the package version and the schema revision read from Postgres, and answers 503
naming the component that failed when Postgres does not answer within HEALTH_TIMEOUT. ``/v1/health/live``
only says the process serves requests: it is the container healthcheck, and a database outage must not
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
    failed: list[str] = Field(default_factory=list, description="components that did not answer")


class Live(BaseModel):
    status: Literal["ok"]


@router.get(
    "/health",
    response_model=Health,
    responses={503: {"model": Health, "description": "a component did not answer"}},
)
async def health(request: Request, response: Response):
    try:
        schema = await schema_revision(request.app.state.pool, HEALTH_TIMEOUT)
    except (psycopg.Error, OSError, TimeoutError, asyncio.TimeoutError) as exc:  # PoolTimeout is a psycopg.Error
        log.warning("health check failed: database unavailable", extra={"error": type(exc).__name__})
        body = Health(status="unavailable", version=__version__, schema=None, db="unavailable", failed=["db"])
        return JSONResponse(body.model_dump(by_alias=True), status_code=503, headers=NO_STORE)
    response.headers.update(NO_STORE)
    return Health(status="ok", version=__version__, schema=schema, db="ok")


@router.get("/health/live", response_model=Live)
async def live(response: Response) -> Live:
    response.headers.update(NO_STORE)
    return Live(status="ok")

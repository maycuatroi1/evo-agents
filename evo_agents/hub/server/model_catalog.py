"""The catalog of model providers (``evo_agents.hub.model_catalog``).

- GET /v1/model-catalog, for anyone signed in: the version, commit and licence of OmniRoute the catalog was generated
  from, the groups in the order the page shows them, and every provider, optionally only those of one ``group``. The
  web loads it when the page that adds an account opens, not with its bundle: it is the same for every member, so the
  answer carries an ETag and may be kept by the browser for an hour.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, Field

from evo_agents.hub import model_catalog
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1", tags=["model-catalog"], responses={401: {"model": ErrorBody}})
CACHE_CONTROL = "private, max-age=3600"


class CatalogSource(BaseModel):
    name: str
    repository: str
    tag: str
    version: str = Field(description="the OmniRoute version, the one of the gateway image")
    commit: str
    license: str
    copyright: str


class CatalogProvider(BaseModel):
    id: str
    alias: str = Field(description="what the gateway reads before the slash of a model name")
    name: str
    group: str
    color: str
    text_icon: str
    website: str
    key_hint: str
    base_url: str = Field(description="the root of the API; empty for the custom entries and the Claude subscription")
    api_format: Literal["openai", "anthropic", "openai-responses", "gemini"]
    default_model: str
    key_required: bool
    free: bool
    via: Literal["gateway", "login"] = Field(description="login: the Claude subscription, which skips the gateway")


class ModelCatalog(BaseModel):
    source: CatalogSource
    groups: list[str]
    providers: list[CatalogProvider]


@router.get("/model-catalog", response_model=ModelCatalog, responses={304: {"description": "unchanged"}})
async def get_model_catalog(
    request: Request,
    response: Response,
    user: CurrentUser,
    group: str | None = Query(default=None, description="only the providers of this group"),
) -> ModelCatalog | Response:
    """The providers a member can add an account for, with where the catalog came from."""
    catalog = model_catalog.load()
    shown = [p for p in catalog["providers"] if group is None or p["group"] == group]
    etag = '"' + hashlib.sha256(f"{catalog['source']['commit']}:{group}".encode()).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return ModelCatalog(source=catalog["source"], groups=catalog["groups"], providers=shown)

"""The shape of the shared KG pool of the hub (``evo_agents.hub.server.kg_shared_pool_api``)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel


class KgSharedPool(BaseModel):
    """The shared KG pool of the hub."""

    pool: str | None = Field(description="the pool named the shared KG pool; null while none is")
    owner: str | None = Field(description="the login of the member who owns it; null while none is")
    projects: list[str] = Field(description="the projects it serves, by name")


class KgSharedPoolSet(StrictModel):
    """The member's pool to name the shared KG pool."""

    owner: Annotated[str, Field(pattern=LOGIN_NAME, description="the login of the member who owns the pool")]
    pool: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", description="the pool's name")]

"""The shapes of a login on the worker's own machine (``routes.local``): what the worker asks for, and the lease it
gets back."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub import browser_login, profiles
from evo_agents.hub.server.browser.models.identities import MAX_DOMAINS, OWNERS, Domain, IdentityName
from evo_agents.hub.server.browser.models.leases import BrowserProfileLease
from evo_agents.hub.server.runs.models import MAX_ID

LocalId = Annotated[int, Path(ge=1, le=MAX_ID, description="a browser identity this worker logged in")]


class LocalLoginAsk(BaseModel):
    project: str = Field(description="the project of the identity")
    name: IdentityName
    owner: Literal[OWNERS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        "member", description="whose identity: yours, the project's or an agent's"
    )
    agent: str | None = Field(None, pattern=profiles.HANDLE_PATTERN, description="the agent's handle, for owner agent")
    domains: list[Domain] = Field(
        [], max_length=MAX_DOMAINS, description="the sites a new identity keeps; needed to create one, else ignored"
    )
    login_url: str | None = Field(None, max_length=browser_login.MAX_URL_CHARS, description="of a new identity")


class LocalLogin(BrowserProfileLease):
    project: str
    created: bool = Field(description="whether this login created the identity")

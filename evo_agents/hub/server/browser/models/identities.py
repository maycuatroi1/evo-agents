"""The shapes of a browser identity of a project (``routes.identities``): the metadata an answer shows, with the
limits and the types of the body that creates one (``service.shapes``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub.credentials import SECRET_NAME
from evo_agents.hub.server.errors import ErrorBody

MAX_DOMAINS = 50
MAX_IDENTITIES = 200  # live identities of one project, its members' included
DOMAIN = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
OWNERS = ("member", "project", "agent")
STATUSES = ("active", "needs_login", "disabled")
IdentityName = Annotated[
    str,
    Field(pattern=f"^{SECRET_NAME.pattern}$", description="unique among its owner's live identities in the project"),
]
Domain = Annotated[
    str, Field(max_length=253, pattern=DOMAIN, description="a host name in lower case, such as github.com")
]
IdentityId = Annotated[int, Path(ge=1, description="the identity's id (`evo-agents hub browser identity list`)")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


class BrowserIdentity(BaseModel):
    id: int
    project: str
    name: str
    owner: Literal[OWNERS]  # pyright: ignore[reportInvalidTypeForm]
    owner_login: str | None = Field(description="the member who owns it; null for the project's and an agent's")
    agent: str | None = Field(
        description="the handle of the agent profile that owns it; null for a member's or the project's"
    )
    agent_default: bool = Field(description="whether its agent profile hands it to every run under it")
    domains: list[str]
    login_url: str | None = Field(description="the page its login session opens; null: its first domain's home page")
    worker: str
    worker_owner: str = Field(description="the login of the worker's owner")
    status: Literal[STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    version: int = Field(description="profiles saved so far; 0 until the first login is saved")
    size: int | None = Field(description="bytes of the current profile, sealed; null at version 0")
    chromium_version: str | None = Field(description="the Chromium that saved the current profile")
    held_by_run: int | None = Field(description="the run that holds it now")
    last_saved_at: datetime | None
    last_saved_by: str | None
    last_used_at: datetime | None
    created_at: datetime
    can_manage: bool = Field(description="whether you may delete it: its member, or an admin for the project's")

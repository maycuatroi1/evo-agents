"""The shapes of a self-managed project's routes (``evo_agents.hub.server.project_admin``): the view of its state and
grants, turning the state on or off, an install from a trusted owner's release, and an invite link to the project."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.base.models import LOGIN_NAME, StrictModel
from evo_agents.hub.server.identity.models.invites import DEFAULT_DAYS, MAX_DAYS
from evo_agents.hub.server.market.models.install import TAG
from evo_agents.hub.server.plugins.models.publishers import SIGNER
from evo_agents.plugins.market import REPO

OWNER = r"^[A-Za-z0-9][A-Za-z0-9-]{0,99}$"


class ProjectGrant(BaseModel):
    login: str
    role: Literal["reader", "writer", "admin"]
    max_level: str
    granted_by: str | None = Field(description="the login of whoever gave or last changed the grant")
    granted_at: datetime


class ProjectAdminView(BaseModel):
    name: str
    self_managed: bool = Field(description="whether the project's admins install plugins and give grants themselves")
    trusted_owners: list[str] = Field(description="the GitHub owners whose releases they install plugins from")
    levels: list[str] = Field(description="the project's label ladder, lowest first")
    grants: list[ProjectGrant] = Field(description="every grant of the project, by login")


class SelfManagedIn(BaseModel):
    enabled: bool
    trusted_owners: list[Annotated[str, Field(pattern=OWNER)]] = Field(
        default_factory=list,
        max_length=50,
        description="GitHub owners (users or organisations); at least one to turn on",
    )


class ProjectInstallIn(StrictModel):  # no trust_publisher: the hub keeps no trust of a project's install
    repo: Annotated[str, Field(pattern=f"^{REPO.pattern}$", description="OWNER/REPO of a trusted owner")]
    tag: Annotated[str | None, Field(pattern=TAG)] = Field(
        None, description="the release's tag; the latest if left out"
    )
    signer: Annotated[str | None, Field(pattern=SIGNER)] = Field(
        None, description="a workflow of the repo that signs its releases; its ci.yml when left out"
    )


class ProjectInviteIn(BaseModel):
    role: Literal["reader", "writer", "admin"]
    max_level: str = Field(min_length=1, max_length=100)
    for_login: Annotated[str | None, Field(pattern=LOGIN_NAME)] = None
    days: int = Field(DEFAULT_DAYS, ge=1, le=MAX_DAYS)

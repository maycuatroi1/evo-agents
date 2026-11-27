"""The shapes of the plugins of a project (``service.grants``): what an installed plugin asks for, and the grant."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.plugins.models.pages import PluginPage
from evo_agents.plugins.manifest import PERMISSIONS, TIERS

PROJECT_STATES = ("installed", "disabled")
Permission = Annotated[str, Field(min_length=1, max_length=100)]


class PluginPermission(BaseModel):
    name: str
    text: str = Field(description="what it lets the plugin do in the project")
    granted: bool


class ProjectPlugin(BaseModel):
    """An installed plugin as one project sees it: what it asks for, and whether the project enabled it."""

    name: str
    title: str = Field(description="the name its manifest gives it")
    description: str
    tier: Literal[TIERS]  # pyright: ignore[reportInvalidTypeForm]
    publisher: str
    version: str = Field(description="the version installed on the hub")
    state: Literal[PROJECT_STATES] = Field(description="disabled: the hub turned it off, and it does nothing anywhere")  # pyright: ignore[reportInvalidTypeForm]
    disabled_reason: str | None
    enabled: bool
    builtin: bool = Field(
        False,
        description="a feature of the hub (canvas, srs, tasks), not a plugin of the store: the project's features turn "
        "it on and off, and it may do what it asks for while on",
    )
    permissions: list[PluginPermission] = Field(description="what the installed version asks for, granted or not")
    granted: list[str] = Field(
        description="what it may do in the project: what an admin of the project granted it that the installed version "
        "still asks for, in force while state is installed; empty while not enabled"
    )
    pending: list[str] = Field(
        description="asked for and not granted, after an upgrade asked for more: enable it again to grant them"
    )
    records_level: str | None = Field(
        None,
        description="the highest level of the project's ladder its jobs and events, which have no caller, reach in its "
        "records (records:use); none: they reach none",
    )
    config_schema: dict | None = Field(description="the JSON Schema of its config, secret fields marked x-secret")
    pages: list[PluginPage] = Field(description="the pages it adds to the project's part of the web")
    enabled_by: str | None
    enabled_at: datetime | None
    granted_by: str | None
    granted_at: datetime | None


class PluginEnable(StrictModel):
    permissions: list[Permission] = Field(
        max_length=len(PERMISSIONS),
        description="the grant: exactly what the installed version asks for, as GET .../plugins/{name} shows it",
    )
    records_level: str | None = Field(
        None,
        description="a level of the project's ladder: the highest its jobs and events, which have no caller, reach in "
        "the plugin's records; left out, what is set stays (none at first)",
    )

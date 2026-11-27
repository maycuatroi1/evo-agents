"""The path parameters and refusals of the agent-type routes and the shapes of an agent type a project enables
(``evo_agents.hub.server.agents.service.agent_types`` says the rules)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub import profiles, task_runs
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.chats.models.members import LOGIN_PATTERN
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.plugins.manifest import PLUGIN_ID
from evo_agents.plugins.names import CONTRIBUTION

POINT = "agent_types"
NOTHING = "nothing changed"
MAX_HANDLE = 100  # the characters of a handle, as profiles.HANDLE_PATTERN allows

PluginName = Annotated[str, Path(pattern=PLUGIN_ID, description="the plugin's id")]
TypeName = Annotated[str, Path(pattern=f"^{CONTRIBUTION}$", description="the agent type, as the manifest names it")]
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


class AgentTypeIn(StrictModel):
    """How a project runs an agent type: under whose name, at what cost a day."""

    run_as: str = Field(
        pattern=LOGIN_PATTERN,
        description="the member whose name its runs go under: a writer or an admin of the project",
    )
    daily_usd: float = Field(
        gt=0,
        le=task_runs.MAX_DAILY_USD,
        description=f"what its task runs may cost in {task_runs.BUDGET_HOURS} hours; a task past it is refused",
    )
    min_run_usd: float | None = Field(
        default=None,
        gt=0,
        description=(
            f"the least cap of one task or question run, above 0 and at most daily_usd; a run is refused when less "
            f"than this is left of daily_usd. None: keep the one set, else the hub's default ${task_runs.DEFAULT_MIN_RUN_USD:.2f}"
        ),
    )
    handle: str | None = Field(
        default=None,
        pattern=profiles.HANDLE_PATTERN,
        description=(
            "the handle of the profile the first enable makes; none: the template's, or <template's>-<project> when "
            "another agent of the hub holds it. A handle never changes, so a later enable names the profile's or none"
        ),
    )


class AgentType(BaseModel):
    """An agent type a plugin the project enabled contributes, and how the project enabled it (null: never)."""

    plugin: str
    name: str
    title: str | None
    description: str | None
    handle: str = Field(description="its profile's handle: the one the hub made, or the one its template names")
    environment: str | None = Field(description="<plugin>/<name>, the environment its runs take")
    packs: list[str] = Field(description="the agent packs of the plugin it attaches, <plugin>/<name>")
    enabled: bool = Field(description="enabled in the project and not turned off: the plugin may hand it tasks")
    run_as: str | None
    daily_usd: float | None
    min_run_usd: float | None = Field(description="the least cap of one run as set; null: the hub's default")
    effective_min_run_usd: float | None = Field(
        description="the floor in effect: min_run_usd, else the hub's default, never above daily_usd; null: not enabled"
    )
    spent_usd: float = Field(description=f"what its task runs cost in the last {task_runs.BUDGET_HOURS} hours")
    enabled_by: str | None
    enabled_at: datetime | None
    disabled_at: datetime | None

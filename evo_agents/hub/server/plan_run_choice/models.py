"""The shapes of a member's choice in the Run plan dialog of a project, and the values a choice goes back to."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import ConfigDict, Field, model_validator

from evo_agents.hub import runs, system_spec
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models.limits import (
    PROFILE_HELP,
    RUNTIME_HELP,
    ModelName,
    ProfileHandle,
    RuntimeName,
)
from evo_agents.hub.tables_push import SYSTEM, WORKER

Target = Literal["worker", "system"]  # tables_push.TARGETS
PLAN_TIMEOUTS = get_args(runs.PlanTimeout)
DEFAULT_TIMEOUT_H = 4  # PlanRunDispatch.timeout_h's
FIELDS = ("target", "system", "agent", "runtime", "model", "mode", "timeout_h", "session_per_step")
REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class PlanRunChoiceIn(StrictModel):
    """What the Run plan dialog keeps of a member's choice; the defaults are a plan run's."""

    target: Target = Field(
        WORKER,
        description="worker: a plan run on a worker (POST .../plan-runs); system: the plan handed to the entry agent "
        "of the system ``system`` names (POST .../plans/{plan_id}/system-dispatch)",
    )
    system: str | None = Field(
        None,
        min_length=1,
        max_length=63,
        pattern=system_spec.SLUG_PATTERN,
        description="the slug of a system of the project when target is system; null when it is worker",
    )
    agent: ProfileHandle | None = Field(None, description=PROFILE_HELP)
    runtime: RuntimeName = Field("any", description=RUNTIME_HELP)
    model: ModelName | None = Field(None, description="the model, as its runtime names it; null: the runtime's own")
    mode: runs.Mode = "headless"
    timeout_h: runs.PlanTimeout = Field(DEFAULT_TIMEOUT_H, description="hours of agent time a plan run may take")
    session_per_step: bool = Field(False, description="true: a new agent session for each step of a plan run")

    @model_validator(mode="after")
    def _system_with_target(self) -> PlanRunChoiceIn:
        if (self.target == SYSTEM) != (self.system is not None):
            raise ValueError("name a system when, and only when, target is system")
        return self


class PlanRunChoice(PlanRunChoiceIn):
    """The caller's choice as the hub keeps it, checked against the project now."""

    model_config = ConfigDict(extra="ignore")

    saved: bool = Field(description="whether the caller saved a choice in the project; false: these are the defaults")
    defaulted: list[str] = Field(
        description="the fields of the saved choice that went back to their default, since what they named can no "
        "longer be used: system (deleted, or without an entry agent; target is then worker), agent (deleted, or not "
        "allowed in the project), runtime, model (not allowed, or not registered), mode, timeout_h"
    )
    updated_at: datetime | None = Field(description="when the caller saved it; null when they never did")

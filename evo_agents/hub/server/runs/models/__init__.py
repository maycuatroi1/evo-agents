"""The run API's shapes and limits: request and response models, the path and query types of its routes, and the
refusals and messages they share. The lowest layer of ``evo_agents.hub.server.runs``: it imports neither the routes
nor the service (lint-imports, contract hub-runs-layers), and the modules around the run API import their models and
limits from here, those of ``run_parts`` too. The models live in the modules of this package, by what they describe;
this module names the ones its importers use."""

from pydantic import BaseModel, Field, create_model

from evo_agents.hub import runs
from evo_agents.hub.server.runs.models.browser import BrowserInstallRequest
from evo_agents.hub.server.runs.models.dispatch import (
    AccessChange,
    ActiveRun,
    AuthorRunDispatch,
    Dispatch,
    OverlapWait,
    PlanProgress,
    PlanRunDispatch,
    PlanTrace,
    ReadySteps,
    Rerun,
    Run,
    SteadyWait,
    StepReadiness,
    SystemDispatch,
    SystemDispatched,
)
from evo_agents.hub.server.runs.models.heartbeat import (
    HealthReport,
    HeartbeatAnswer,
    HeartbeatRequest,
    KgDuty,
    KgSyncReport,
    NightlyRead,
    RunControl,
    SelfUpdateReport,
    UpdateNowRequest,
    WorkerSettings,
    available,
)
from evo_agents.hub.server.runs.models.limits import (
    LINE,
    MAX_ID,
    MAX_LIST,
    MAX_OFFSET,
    MAX_STATS_DAYS,
    MIN_STATS_DAYS,
    NOT_HELD,
    OBJECT_NAME,
    REFUSALS,
    STATS_DAYS,
    STEP_KEY_CHARS,
    AccountName,
    ModelName,
    RunId,
    StepKey,
)
from evo_agents.hub.server.runs.models.reports import (
    AuthoredPlan,
    RunDay,
    RunFigures,
    RunStats,
    StateReport,
    StepProof,
    StepReport,
    StepWritten,
    VerifyResult,
)
from evo_agents.hub.server.runs.models.run_parts import (
    ClaimedBudget,
    LostAttempt,
    PlanCopy,
    PluginRuntime,
    ReleaseSpec,
    RunBudget,
    RunEnvironment,
    RunOrigin,
    RunPack,
    RunProfile,
    RunRepo,
    RunSkill,
)
from evo_agents.hub.server.runs.models.spec import Claim, ClaimRequest, CuratorSpec, RunSpec

# Listing runs: its counts are a model made of the states a run may be shown in

StateCounts = create_model(
    "StateCounts",
    __doc__="Runs in each state.",
    **{state: (int, Field(0, ge=0)) for state in runs.SHOWN_STATES},
)


class RunList(BaseModel):
    runs: list[Run] = Field(description="newest first")
    total: int = Field(description="runs that match every filter")
    counts: StateCounts = Field(
        description="runs in each state that match the other filters: what the state filter would leave"
    )
    limit: int
    offset: int


__all__ = [
    "AccessChange",
    "AccountName",
    "ActiveRun",
    "AuthorRunDispatch",
    "AuthoredPlan",
    "BrowserInstallRequest",
    "Claim",
    "ClaimRequest",
    "ClaimedBudget",
    "CuratorSpec",
    "Dispatch",
    "HealthReport",
    "HeartbeatAnswer",
    "HeartbeatRequest",
    "KgDuty",
    "KgSyncReport",
    "LINE",
    "LostAttempt",
    "MAX_ID",
    "MAX_LIST",
    "MAX_OFFSET",
    "MAX_STATS_DAYS",
    "MIN_STATS_DAYS",
    "ModelName",
    "NOT_HELD",
    "NightlyRead",
    "OBJECT_NAME",
    "OverlapWait",
    "PlanCopy",
    "PlanProgress",
    "PlanRunDispatch",
    "PlanTrace",
    "PluginRuntime",
    "REFUSALS",
    "ReadySteps",
    "ReleaseSpec",
    "Rerun",
    "Run",
    "RunBudget",
    "RunControl",
    "RunDay",
    "RunEnvironment",
    "RunFigures",
    "RunId",
    "RunList",
    "RunOrigin",
    "RunPack",
    "RunProfile",
    "RunRepo",
    "RunSkill",
    "RunSpec",
    "RunStats",
    "STATS_DAYS",
    "STEP_KEY_CHARS",
    "SelfUpdateReport",
    "StateCounts",
    "StateReport",
    "SteadyWait",
    "StepKey",
    "StepProof",
    "StepReadiness",
    "StepReport",
    "StepWritten",
    "SystemDispatch",
    "SystemDispatched",
    "UpdateNowRequest",
    "VerifyResult",
    "WorkerSettings",
    "available",
]

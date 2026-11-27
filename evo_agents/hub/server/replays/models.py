"""The bodies of the replays' routes and of the MCP tool curator_replay: what a replay run's worker reports, a task
of the suite, a replay and a change's replays, with the audit action of a replay queued and the refusals."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Header
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import replay
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import OBJECT_NAME

REPLAY = "curator.replay"  # the night shift queued a replay run: "<project>/<plan> run:<id> change:<id> task:<id>"
MAX_TASKS_LISTED = 200
HARNESS_KINDS = ("skill",)  # the kinds of proposal replayed whatever the paths they name
AGENT_NO_CHANGE = (
    "the agent of a run reads no change's replays: they are of the holdout split, which changes are measured on; a "
    "member reads them with `evo-agents hub curator replay show CHANGE`"
)

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}

ReplayKey = Annotated[
    str | None,
    Header(
        alias=replay.REPLAY_KEY_HEADER,
        max_length=200,
        description="the replay run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]
Split = Literal[replay.SPLITS]  # pyright: ignore[reportInvalidTypeForm]


class VerifyRun(StrictModel):
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)


class ArmResult(StrictModel):
    """What one arm of a replay found: whether the step's verify passed on the agent's work, and what the agent took."""

    passed: bool = Field(description="every verify command exited 0")
    harness_sha: str = Field(pattern=OBJECT_NAME, description="the commit of the change's repo the arm's harness is at")
    harness_paths: list[Annotated[str, Field(min_length=1, max_length=1000)]] = Field(
        default_factory=list, max_length=replay.MAX_PATHS, description="the files of the change the arm ran with"
    )
    verify: list[VerifyRun] = Field(default_factory=list, max_length=10)
    cost_usd: float | None = Field(None, ge=0, description="the agent's session; null when its runtime names none")
    turns: int | None = Field(None, ge=0)
    tool_calls: int = Field(0, ge=0)
    tool_errors: int = Field(0, ge=0, description="the tool calls that failed")
    peak_context_tokens: int | None = Field(None, ge=0)
    duration_ms: int = Field(ge=0, description="the agent's time")
    agent_error: str | None = Field(None, max_length=replay.MAX_REASON_CHARS, description="why its turn did not end")
    session_id: str | None = Field(None, max_length=200)


class ReplayReport(StrictModel):
    """One arm's result with the task's base commit; or why the worker cannot replay the task at all."""

    arm: Literal[replay.ARMS] | None = None  # pyright: ignore[reportInvalidTypeForm]
    base_sha: str | None = Field(None, pattern=OBJECT_NAME, description="the task's base commit the arm started at")
    result: ArmResult | None = None
    refused: str | None = Field(None, min_length=1, max_length=replay.MAX_REASON_CHARS)
    task_unusable: bool = Field(False, description="with refused: no replay can run the task, whatever the change")

    @model_validator(mode="after")
    def _one(self):
        if self.refused is None and (self.arm is None or self.result is None or self.base_sha is None):
            raise ValueError("a replay report is an arm with its result and base_sha, or refused with why")
        if self.refused is not None and (self.arm is not None or self.result is not None):
            raise ValueError("a refused replay reports no arm")
        return self


class ReplayTask(BaseModel):
    id: int
    plan_id: str
    step_key: str
    repo: str
    title: str | None
    what: str
    verify: str
    commits: list[str]
    source: Literal[replay.SOURCES]  # pyright: ignore[reportInvalidTypeForm]
    split: Split  # pyright: ignore[reportInvalidTypeForm]
    base_sha: str | None = Field(description="the first parent of the oldest of its commits, once a replay read it")
    unusable: str | None = Field(description="why no replay can run it")
    created_at: datetime


# its OpenAPI name, shared with the ReplayTask of a run's spec, is by module: the one it had before the package
ReplayTask.__module__ = "evo_agents.hub.server.replays"
ReplayTask.model_rebuild(force=True)


class ReplayTaskList(BaseModel):
    project: str
    split: Split | None  # pyright: ignore[reportInvalidTypeForm]
    tasks: list[ReplayTask] = Field(description="in the order they came into the suite")
    total: int
    counts: dict[str, int] = Field(description="the tasks of each split the caller may read")


class Replay(BaseModel):
    id: int
    change_id: int
    task_id: int
    plan_id: str
    step_key: str
    repo: str
    title: str | None
    split: Split  # pyright: ignore[reportInvalidTypeForm]
    run_id: int | None
    state: Literal[replay.STATES]  # pyright: ignore[reportInvalidTypeForm]
    attempts: int
    base_sha: str | None
    a: dict | None = Field(description="arm a, the harness of the default branch: ArmResult")
    b: dict | None = Field(description="arm b, the harness of the change's branch: ArmResult")
    reason: str | None
    created_at: datetime
    updated_at: datetime


class ChangeReplay(BaseModel):
    project: str
    change_id: int
    tasks: int = Field(description="the tasks each change is replayed on, as the charter says now")
    replays: list[Replay]
    arms: dict = Field(description="replay.compare: per arm the replays, passed, cost, turns, tool errors, context")
    verdict: dict = Field(
        description="replay.verdict at the head of the change's pull request: whether its replays won, and why"
    )

"""The shapes of the routes of the hub's merge queue on GitLab (``routes.view``): the merge requests of the queue and
their ledger, the audit samples, the breakers, an admin's answers and the queue's figures."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import merge_breaker, merge_gate, post_merge
from evo_agents.hub.server.merge.models.passes import MAX_REASON_CHARS
from evo_agents.hub.server.merge.models.post_merge import MAX_NOTE_CHARS

MAX_EVENTS = 100  # of a merge request's ledger, the newest, on the route
MAX_SAMPLES = 50  # on the route, those waiting for an answer first


class MergeEvent(BaseModel):
    id: int
    kind: str
    gate: int | None = Field(description="the gate's number, 0 to 7, for kind gate")
    gate_name: str | None
    passed: bool | None
    reason: str | None
    sha: str | None
    run_id: int | None
    detail: dict[str, Any]
    created_at: datetime


class QueuedMergeRequest(BaseModel):
    """A merge request of the queue: where it comes from, where it stands and why, and its ledger."""

    id: int
    repo: str
    iid: int | None = Field(description="null while the hub could not open it")
    source: str = Field(description=f"one of {', '.join(merge_gate.SOURCES)}")
    target_branch: str
    source_branch: str
    run_id: int | None
    plan_id: str | None
    steps: list[str]
    change_id: int | None
    profile: str | None
    model: str | None
    title: str | None
    web_url: str | None
    state: str = Field(description=f"one of {', '.join(merge_gate.STATES)}")
    reason: str | None = Field(description="why it waits or is blocked, the gate first")
    head_sha: str | None
    fingerprint: str | None
    risk: dict[str, Any] | None
    verdict: dict[str, Any] | None
    judge_run_id: int | None
    queued_at: datetime | None
    merged_at: datetime | None
    created_at: datetime
    updated_at: datetime
    events: list[MergeEvent] = Field(description=f"its ledger, the newest {MAX_EVENTS} first")
    before_sha: str | None = Field(None, description="the head of the target branch its last turn rebased it onto")
    rebases: int = Field(0, description="its rebases again in a row since its gates last passed")
    after_sha: str | None = Field(None, description="its commit on the target branch, once merged")
    reverts_id: int | None = Field(None, description="a revert's: the merge request of the queue it undoes")
    uat_result: str | None = Field(None, description=f"its own UAT check: one of {', '.join(post_merge.RESULTS)}")
    prod_result: str | None = Field(None, description=f"its own PROD check: one of {', '.join(post_merge.RESULTS)}")


class MergeSample(BaseModel):
    """A merge request drawn for an audit sample, and an admin's answer."""

    id: int
    queue_id: int
    repo: str
    iid: int | None
    title: str | None
    web_url: str | None
    result: str | None = Field(
        description=f"null until an admin answers: one of {', '.join(post_merge.SAMPLE_RESULTS)}"
    )
    note: str | None
    answered_by: str | None
    answered_at: datetime | None
    created_at: datetime


class MergeBreaker(BaseModel):
    """The breaker of a repo of the queue: armed, or tripped until an admin resets it."""

    repo: str
    state: str = Field(description=f"one of {', '.join(merge_breaker.STATES)}")
    cause: str | None = Field(description=f"what tripped it last: one of {', '.join(merge_breaker.CAUSES)}")
    reason: str | None = Field(description="why it tripped last, each thing that tripped it")
    tripped_at: datetime | None
    trips: int = Field(description="how often it tripped")
    resumed_by: str | None = Field(description="the admin who reset it last")
    resumed_at: datetime | None = Field(description="what trips it counts from then")
    red_since: datetime | None = Field(description="the hub saw its target branch turn red then, none passing since")


class BreakerReset(MergeBreaker):
    """A breaker an admin reset, and the merge requests waiting for their turn again."""

    resumed: list[int] = Field(description="the iids of the merge requests it had paused, ready again")


class MergeQueueView(BaseModel):
    project: str
    requests: list[QueuedMergeRequest] = Field(description="newest first")
    samples: list[MergeSample] = Field([], description=f"the audit samples, those waiting first, at most {MAX_SAMPLES}")
    breakers: list[MergeBreaker] = Field([], description="the breaker of each repo the queue passed over, by repo")


class SampleAnswer(BaseModel):
    result: Literal[post_merge.SAMPLE_RESULTS] = Field(description="faulty counts as a pass of the Judge found wrong")  # pyright: ignore[reportInvalidTypeForm]
    note: str | None = Field(None, max_length=MAX_NOTE_CHARS)


class HoldPass(BaseModel):
    reason: str = Field(
        min_length=1,
        max_length=MAX_REASON_CHARS,
        description="why the hold may go on: the ledger and the audit keep it with the admin's login",
    )


class MergeFigure(BaseModel):
    """The merges of one repo, agent and model in the window."""

    repo: str
    agent: str | None = Field(description="the agent profile of the run that made them, or person for a person's")
    model: str | None
    merged: int
    reverted: int = Field(description="merged, then found the culprit of a failed check")
    rolled_back: int = Field(description="merged, then PROD went back for their deploy")
    judge_misses: int = Field(description="passed by the Judge, then reverted, rolled back or sampled faulty")
    wait_seconds: float | None = Field(description="the mean time from ready to merged")
    wait_seconds_max: float | None
    uat_seconds: float | None = Field(description="the mean time from merged to the UAT check passed")
    prod_seconds: float | None = Field(description="the mean time from merged to the PROD check passed")


class MergeFigures(BaseModel):
    project: str
    days: int
    figures: list[MergeFigure]

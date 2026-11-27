"""The bodies of the ops actions of an Orchestrator in an ops thread and what they answer (``ops.service.actions`` is
the design)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.hub import review, runs
from evo_agents.hub.server.orchestrator.models import Reason

CURATOR_RESUME = "curator.resume"  # curator.RESUME, the action of `evo-agents hub curator resume`
Id = Annotated[int, Field(ge=1, le=2**63 - 1)]


class OpsRunIn(BaseModel):
    run_id: Id = Field(description="a chat or task run of the batch that failed or was lost")
    reason: Reason


class OpsWaitIn(BaseModel):
    decision_id: Id = Field(description="a stuck wait of the batch: a decision of category wait")
    text: str = Field(min_length=1, max_length=runs.MAX_ANSWER_BYTES, description="the answer that wakes its run")
    reason: Reason


class OpsReasonIn(BaseModel):
    reason: Reason


class OpsReadIn(BaseModel):
    notification_id: Id = Field(description="a notice of the project")
    reason: Reason


class OpsFindingIn(BaseModel):
    lens: review.Lens
    severity: review.Severity
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=8000, description="what is wrong with the system, and the fix")
    reason: Reason


class OpsPriorityIn(BaseModel):
    run_id: Id = Field(description="a queued run of the project")
    level: runs.Priority = Field(description="its new priority: urgent, high, medium or low")
    reason: Reason


class OpsPlanIn(BaseModel):
    plan_id: str = Field(min_length=1, max_length=200, description="a stalled plan of the batch (a plan.stalled item)")
    reason: Reason


class OpsActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the Orchestrator's name")
    run_id: int | None = Field(None, description="the run the work went to, or the run the answer woke")
    finding_id: int | None = Field(None, description="the finding it wrote")
    handled: int = Field(0, description="items of the batch it marked handled")

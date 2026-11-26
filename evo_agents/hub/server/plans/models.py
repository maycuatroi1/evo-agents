"""The request and answer bodies of the plans of a project, a plan as the hub holds it (``Held``), the refusal of a
write that says more than a message (``PlanError``), and the names and limits the routes share.

``Conflict`` and ``Written`` are in the package itself (``evo_agents.hub.server.plans``): the OpenAPI document names
them by their module, so they keep the name they have always had.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Path
from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import runs
from evo_agents.hub.plans import AREAS, PLAN_ID, SECTIONS, UPDATABLE

PlanId = Annotated[str, Path(pattern=PLAN_ID)]
SINK_HEADER = "X-Evo-Sink"
REVISION_CONFLICT = "revision_conflict"
PLAN_CREATE = "plan.create"
PLAN_PUT = "plan.put"
PLAN_PATCH = "plan.patch"
PLAN_COMPLETE = "plan.complete"
PLAN_ABANDON = "plan.abandon"
PLAN_REOPEN = "plan.reopen"
SHOWN_PROBLEMS = 5
PROBLEMS_PATH = "problems"  # where in a plan its problems are, as a warning or a refusal names it
NO_PROBLEMS = (
    "the plan names no problem of the project it solves: add problems: [P-<number>], as `evo-agents hub problem list` "
    "lists them"
)


class PlanError(Exception):
    """A refusal with more than a message: problems as ``detail``, or the held plan with a revision conflict."""

    def __init__(self, status: int, message: str, *, code=None, detail=None, current=None):
        super().__init__(message)
        self.status, self.message, self.code, self.detail, self.current = status, message, code, detail, current


class Problem(BaseModel):
    path: str
    message: str


class PlanTrace(BaseModel):
    """A trace of a system that runs the plan (``plan_systems``) and has a run active: its runs hold the plan's steps
    until they end."""

    id: int = Field(description="the trace, as GET /v1/projects/{p}/systems/{slug}/traces/{id} names it")
    system: str = Field(description="the slug of the system it runs under")
    run_id: int = Field(description="its active run, the latest when it has several")
    state: runs.ActiveState


class PlanStepState(BaseModel):
    """A step of a plan as the list shows it: how the plan names it (``plans.step_key``) and its status."""

    key: str
    status: str | None = Field(description="done, in_progress, pending, blocked or descoped; null when it has none")


class PlanCurrentStep(PlanStepState):
    title: str | None = Field(description="the step's title, else the start of its what")


class PlanSummary(BaseModel):
    plan_id: str
    area: Literal[AREAS]  # pyright: ignore[reportInvalidTypeForm]
    revision: int
    digest: str
    title: str | None
    steps_total: int
    steps_done: int
    updated_at: datetime
    updated_by: str
    goal: str | None = Field(default=None, description="the plan's goal, cut to its first characters")
    steps: list[PlanStepState] = Field(default_factory=list, description="each step in the plan's order")
    current_step: PlanCurrentStep | None = Field(
        default=None, description="the first step neither done nor descoped; null when there is none"
    )
    repos: list[str] = Field(default_factory=list, description="the names of the repos the plan works in")
    problems: list[str] = Field(
        default_factory=list, description="the codes (P-<number>) of the problems of the project its body names"
    )
    trace: PlanTrace | None = Field(
        default=None,
        description="the trace of a system the plan was handed to that has a run active (system_traces.plan_traces): "
        "its runs are chat runs, which name no plan, so the project's runs do not show it",
    )

    @field_validator("problems", mode="before")
    @classmethod
    def _named(cls, value) -> list:
        """The codes a body's problems hold; none when the body has no such list."""
        return [code for code in value if isinstance(code, str)] if isinstance(value, list) else []


class Plan(BaseModel):
    project: str
    plan_id: str
    area: Literal[AREAS]  # pyright: ignore[reportInvalidTypeForm]
    revision: int
    digest: str = Field(description="sha256 of the canonical JSON of the body, as evo_agents.harness.plan_digest")
    label: dict
    body: dict
    created_at: datetime
    updated_at: datetime
    updated_by: str


@dataclass(frozen=True)
class Held:
    """A plan as the hub holds it: its row of ``plans`` with the login of its last writer."""

    plan_id: str
    area: str
    label: dict
    body: dict
    revision: int
    digest: str
    created_at: datetime
    updated_at: datetime
    updated_by: str

    def view(self, project: str) -> dict:
        return Plan(project=project, **vars(self)).model_dump(mode="json")


class RevisionFields(BaseModel):
    revision: int
    area: Literal[AREAS]  # pyright: ignore[reportInvalidTypeForm]
    digest: str
    summary: str
    actor: str
    created_at: datetime


class Revision(RevisionFields):
    run_id: int | None = Field(
        None,
        description="the author run that wrote the revision, as its dispatcher (actor), or the local run whose session "
        "wrote it; null for any other write",
    )


# One revision with its body keeps the keys of 0.8.0, which consumers of `hub plan show --revision` pin exactly (seam
# hub-cli-v1); the author run that wrote a revision is in the history (Revision.run_id).
class RevisionBody(RevisionFields):
    label: dict
    body: dict


class PlanDiffLine(BaseModel):
    kind: Literal["context", "added", "removed"]
    old: int | None = Field(description="the line's number in the from revision's text; null for an added line")
    new: int | None = Field(description="the line's number in the to revision's text; null for a removed line")
    text: str


class PlanDiffHunk(BaseModel):
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: list[PlanDiffLine]


class PlanDiff(BaseModel):
    plan_id: str
    from_revision: Revision
    to_revision: Revision
    context: int
    added: int = Field(description="lines added in all hunks")
    removed: int = Field(description="lines removed in all hunks")
    hunks: list[PlanDiffHunk] = Field(description="empty when the two revisions read the same")


class PlanHandoff(BaseModel):
    project: str
    plan_id: str
    revision: int
    step: str | None = Field(description="the step the handoff is for, whose dependencies' evidence comes first")
    budget_tokens: int
    tokens: int = Field(description="the text's size as the hub estimates it: UTF-8 bytes / 3, rounded up")
    cut: bool = Field(description="whether anything was cut or left out to stay within the budget")
    text: str = Field(description="the handoff, as evo_agents.hub.handoff builds it; markdown")
    difficulty: Literal["easy", "medium", "hard"] | None = Field(
        None, description="the step's difficulty: its own, else as the hub's rules rate it; null without a step"
    )
    recommended_model: str | None = Field(
        None, description="the model the project's Token strategy gives that difficulty now (routing.*)"
    )
    routing_reason: str | None = Field(None, description="why that difficulty and that model")


class PlanPut(BaseModel):
    body: dict[str, Any] = Field(description="the plan as its YAML file reads; a hub key in it is ignored")
    area: Literal[AREAS] | None = Field(None, description="where a new plan goes (default active); else its area")  # pyright: ignore[reportInvalidTypeForm]
    if_revision: int | None = Field(
        None, ge=0, description="the revision being replaced; leave it out, or 0, to create the plan"
    )
    label: dict | None = Field(None, description="{level, location, integrity}; default: the project's, or the held")
    run_id: int | None = Field(
        None,
        ge=1,
        le=2**63 - 1,
        description="the caller's own local run (kind local) whose session writes the plan: the revision records it "
        "(Revision.run_id) and the run takes the plan; 404 for any other run",
    )


class PlanPatch(BaseModel):
    section: Literal[SECTIONS]  # pyright: ignore[reportInvalidTypeForm]
    index: int | None = Field(None, ge=0, description="the item's position in the section")
    step: int | str | None = Field(None, description="for steps, the step's id (or order) instead of an index")
    updates: dict[str, str] = Field(min_length=1, max_length=len(UPDATABLE), description=", ".join(UPDATABLE))
    if_revision: int = Field(ge=1, description="the revision the update was made against")
    run_id: int | None = Field(
        None,
        ge=1,
        le=2**63 - 1,
        description="the caller's own local run (kind local) whose session writes the progress: the revision records "
        "it (Revision.run_id) and the run takes the plan and the step; 404 for any other run",
    )


class PlanComplete(BaseModel):
    if_revision: int | None = Field(None, ge=1)
    mark_steps_done: bool = Field(
        False, description="mark every step that is neither done nor descoped done first, instead of refusing the plan"
    )


class PlanAbandon(BaseModel):
    reason: str = Field(min_length=1, max_length=4096, description="why the plan is dropped")
    superseded_by: str | None = Field(
        None, pattern=PLAN_ID, description="the plan that replaces it; the status is then superseded"
    )
    if_revision: int | None = Field(None, ge=1)


class PlanReopen(BaseModel):
    if_revision: int | None = Field(None, ge=1)

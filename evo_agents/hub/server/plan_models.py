"""The request and answer bodies of the plan routes (``evo_agents.hub.server.plans``), which imports them back.

Conflict and Written stay in ``plans``: the OpenAPI document names them by module there, so moving them would rename
their schemas.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub.plans import AREAS, PLAN_ID, SECTIONS, UPDATABLE


class Problem(BaseModel):
    path: str
    message: str


class PlanSummary(BaseModel):
    plan_id: str
    area: Literal[AREAS]
    revision: int
    digest: str
    title: str | None
    steps_total: int
    steps_done: int
    updated_at: datetime
    updated_by: str
    problems: list[str] = Field(
        default_factory=list, description="the codes (P-<number>) of the problems of the project its body names"
    )

    @field_validator("problems", mode="before")
    @classmethod
    def _named(cls, value) -> list:
        """The codes a body's problems hold; none when the body has no such list."""
        return [code for code in value if isinstance(code, str)] if isinstance(value, list) else []


class Plan(BaseModel):
    project: str
    plan_id: str
    area: Literal[AREAS]
    revision: int
    digest: str = Field(description="sha256 of the canonical JSON of the body, as evo_agents.harness.plan_digest")
    label: dict
    body: dict
    created_at: datetime
    updated_at: datetime
    updated_by: str


class RevisionFields(BaseModel):
    revision: int
    area: Literal[AREAS]
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


class PlanPut(BaseModel):
    body: dict[str, Any] = Field(description="the plan as its YAML file reads; a hub key in it is ignored")
    area: Literal[AREAS] | None = Field(None, description="where a new plan goes (default active); else its area")
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
    section: Literal[SECTIONS]
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


class PlanAbandon(BaseModel):
    reason: str = Field(min_length=1, max_length=4096, description="why the plan is dropped")
    superseded_by: str | None = Field(
        None, pattern=PLAN_ID, description="the plan that replaces it; the status is then superseded"
    )
    if_revision: int | None = Field(None, ge=1)


class PlanReopen(BaseModel):
    if_revision: int | None = Field(None, ge=1)

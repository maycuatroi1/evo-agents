"""The models of the decisions on a project's knowledge graph: a decision with its history, and what a writer, an
agent or a loop of the review queue writes. The list a member reads (``service.KgDecisionList``) names the build of
the graph it was read at (``kg_web.GraphRef``), whose module reaches the service: it lives there."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import Path as PathParam
from pydantic import BaseModel, Field, field_validator, model_validator

from evo_agents.hub.kg_decisions import MAX_REASON_CHARS, MAX_REF_CHARS

Kind = Literal["deprecate", "merge", "link"]  # kg_decisions.KINDS
State = Literal["proposed", "active", "rejected", "undone", "orphan"]  # kg_decisions.STATES and ORPHAN
Step = Literal["accept", "reject", "undo"]  # the keys of kg_decisions.TRANSITIONS
Ref = Annotated[str, Field(min_length=1, max_length=MAX_REF_CHARS)]
MAX_REV = 2**31 - 1


class KgDecisionEvent(BaseModel):
    rev: int = Field(description="the project's decision revision this step made")
    action: Literal["propose", "decide", "accept", "reject", "undo"]
    by: str = Field(
        description="the login of the member, or of the agent's owner, who took the step; plugin:<id> for a core "
        "plugin that took it as itself"
    )
    at: datetime
    reason: str
    build_id: int | None = Field(description="the build the graph was at; null once that build is gone")
    origin: str | None = Field(
        None, description="the loop that took the step, as kg-optimize; null for one taken by hand"
    )
    run_id: int | None = Field(None, description="that loop's run, when it named one; null once the run is gone")


class KgDecision(BaseModel):
    number: int = Field(description="the decision's number in the project, from 1")
    kind: Kind
    state: State
    src: str = Field(description="the node deprecated, the first node merged, or the edge's source")
    rel: str | None = Field(description="the edge's type, for an edge deprecated or linked")
    dst: str | None = Field(description="the edge's target, or the node merged into src's id")
    history: list[KgDecisionEvent] = Field(description="oldest first; the first step says why it was taken")


class KgDecisionStep(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)
    base_rev: int = Field(ge=0, le=MAX_REV, description="the decision revision you read")

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a reason must say something")
        return value


class NewKgDecision(KgDecisionStep):
    kind: Kind
    src: Ref
    rel: Ref | None = None
    dst: Ref | None = None

    @model_validator(mode="after")
    def _shape(self) -> NewKgDecision:
        """What each kind names, as migration 0142 checks."""
        if self.kind == "merge" and (self.rel is not None or self.dst is None or self.dst == self.src):
            raise ValueError("a merge names two different nodes, src and dst, and no rel")
        if self.kind == "link" and (self.rel is None or self.dst is None):
            raise ValueError("a link names src, rel and dst")
        if self.kind == "deprecate" and (self.rel is None) != (self.dst is None):
            raise ValueError("a deprecation names a node (src) or an edge (src, rel and dst)")
        return self


Number = Annotated[int, PathParam(ge=1, le=MAX_REV, description="the decision's number in the project")]


class Fix(BaseModel):
    """A decision a loop takes as itself from a fix of the review queue (``evo_agents.kg.review``)."""

    kind: Kind
    src: str
    rel: str | None
    dst: str | None
    reason: str
    fingerprint: str = Field(description="the review item the fix is of")

"""The shapes and limits of a project's review queue (``evo_agents.hub.server.kg_review``): the items and their
dismissals, and what a call to apply their fixes asks and answers."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub.server.kg.models.web import GraphRef

MAX_REASON_CHARS = 1000  # as migration 0141 checks
MAX_DECISIONS, MAX_DECISIONS_CAP = 25, 200  # the decisions a call to apply takes, by default and at most
LOOP = "kg-optimize"
NO_BUILD = "no build of this project succeeded yet"

Rule = Literal[  # RULES of evo_agents.kg.review, in order
    "dangling_mention",
    "unmentioned_document",
    "cluster_without_knowledge",
    "possible_duplicate",
    "ambiguous_match",
    "bulk_noise",
    "conflicting_claim",
    "wrong_endpoint",
    "cardinality",
    "cycle",
    "duplicate_key",
    "missing_prop",
]


class ReviewNode(BaseModel):
    id: str
    kind: str
    name: str | None


class Dismissal(BaseModel):
    reason: str
    by: str = Field(description="the login of the writer who dismissed it; plugin:<id> for a core plugin")
    at: datetime
    build_id: int | None = Field(description="the build they saw the item in; null once that build is gone")


class ReviewItem(BaseModel):
    fingerprint: str = Field(description="the item's id, the same in every build that finds it")
    rule: Rule
    action: str = Field(
        description="what the rule suggests: fix_mention, link_or_deprecate, add_knowledge, merge, disambiguate, "
        "exclude or check_claims"
    )
    cluster: str | None = Field(description="the level-1 cluster of its first node you can see, if any")
    cluster_label: str | None = Field(description="that cluster's label, as your map shows it")
    nodes: list[ReviewNode] = Field(description="the nodes the item is about that you can see")
    detail: dict = Field(
        description="what the rule found: count, and per rule mentions, samples, path, kind, where, rel, claims"
    )
    weight: int = Field(description="the most edges you can see any of its nodes has; the queue's order")
    dismissal: Dismissal | None = Field(description="null while the item is open")


class RuleCount(BaseModel):
    rule: Rule
    open: int
    dismissed: int


class ReviewQueue(BaseModel):
    graph: GraphRef | None = Field(description="null until a build of the project succeeded")
    computed: bool = Field(description="false when the build has no review queue; reason says why")
    reason: str | None
    can_dismiss: bool = Field(description="true for a writer of the project")
    counts: list[RuleCount] = Field(description="every rule, with the items you can see open and dismissed")
    items: list[ReviewItem] = Field(description="by rule, then most connected first")


class DismissBody(BaseModel):
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a reason must say something")
        return value


class ApplyBody(BaseModel):
    origin: str = Field(LOOP, pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$", description="the loop that applies the fixes")
    run_id: int | None = Field(None, ge=1, description="the run of the loop, a run of this project")
    max_decisions: int = Field(MAX_DECISIONS, ge=1, le=MAX_DECISIONS_CAP, description="the cap of this call")
    rules: list[Rule] | None = Field(None, description="only the items of these rules; every rule when null")
    dry_run: bool = Field(False, description="say what would be decided and decide nothing")


class AppliedDecision(BaseModel):
    number: int | None = Field(description="the decision's number; null on a dry run")
    kind: str
    src: str
    rel: str | None
    dst: str | None
    reason: str


class AppliedItem(BaseModel):
    fingerprint: str
    rule: Rule
    whole: bool = Field(description="true when every decision of the item's fix is taken or was said already")
    decisions: list[AppliedDecision]


class ReportItem(BaseModel):
    fingerprint: str
    rule: Rule
    cluster: str | None
    cluster_label: str | None
    nodes: list[ReviewNode]


class ApplyResult(BaseModel):
    graph: GraphRef
    rev: int = Field(description="the project's decision revision after the call")
    dry_run: bool
    max_decisions: int
    decided: int = Field(description="the decisions taken (or, on a dry run, that would be)")
    skipped: int = Field(description="the decisions of fixes a proposed or active decision already says")
    items: list[AppliedItem] = Field(description="the items whose fix took a decision, in the queue's order")
    report: list[ReportItem] = Field(description="the open items no decision fixes, for a person to read")
    left: dict[str, int] = Field(description="per rule, the open items with a fix not wholly taken")


class Undismissed(BaseModel):
    fingerprint: str
    was_by: str = Field(description="who had dismissed it: a login, or plugin:<id> for a core plugin")

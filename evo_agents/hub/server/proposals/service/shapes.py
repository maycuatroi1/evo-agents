"""The shapes of a proposal that carry a probe or a system change: what a review run proposes (``ProposalIn``, and
``SessionProposalIn`` for a member's session) and the proposal as members read it (``Proposal``). They hold the models
of ``evo_agents.hub.server.probes`` and ``evo_agents.hub.server.system_curation``, which reach this service through
the rest of the hub, so they sit in the service layer, not in ``evo_agents.hub.server.proposals.models``."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import review, tiers
from evo_agents.hub.server import probes as stored_probes
from evo_agents.hub.server import system_curation
from evo_agents.hub.server.changes.models.views import Change
from evo_agents.hub.server.memories.models import PromotionIn
from evo_agents.hub.server.proposals.models import EvidenceIn, PathIn, ProposalSummary, RepoPath, Title
from evo_agents.hub.server.runs.models import MAX_ID


class ProposalIn(BaseModel):
    lens: review.Lens
    kind: tiers.ChangeKind = Field(description="the kind of change, which gives its first tier")
    title: Title
    summary: str | None = Field(None, min_length=1, max_length=review.MAX_BODY_BYTES, description="markdown, 16 KiB")
    paths: list[PathIn] = Field(default_factory=list, max_length=tiers.MAX_PATHS, description="what it would edit")
    finding_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list, max_length=review.MAX_FINDING_IDS, description="findings of the project it rests on"
    )
    evidence: list[EvidenceIn] = Field(default_factory=list, max_length=review.MAX_EVIDENCE)
    plan: dict | None = Field(None, description="the draft plan: plan.schema.json, in outcome steps; none to promote")
    promotion: PromotionIn | None = Field(None, description="kind memory: in place of a plan")
    system_change: system_curation.SystemChangeIn | None = Field(
        None, description="kind agent_config, lens agent_system: in place of a plan"
    )
    probe: stored_probes.ProbeIn | None = Field(
        None,
        description="a task and a verify that say whether an agent repeats the mistake; required when the evidence "
        "cites a correction and the paths include a file a probe arm can place",
    )
    fix_of: int | None = Field(
        None,
        ge=1,
        le=MAX_ID,
        description="the merged change whose probe recurred two weekly reports in a row that the prompt asked this run "
        "to fix; at least tier 1, and the hub adds the probe results as evidence",
    )

    @model_validator(mode="after")
    def _supported(self):
        if not self.finding_ids and not self.evidence and self.fix_of is None:
            raise ValueError(
                "a proposal rests on findings, evidence of its own, or both: give at least one (a fix proposal, "
                "fix_of, rests on the probe results the hub adds)"
            )
        carried = [self.plan, self.promotion, self.system_change]
        if sum(item is not None for item in carried) != 1 or (self.promotion and self.kind != "memory"):
            raise ValueError(
                "a proposal carries a draft plan, or in its place a promotion when its kind is memory, or a system "
                "change when its kind is agent_config"
            )
        if (self.system_change is not None) != (self.kind == system_curation.KIND) or (
            self.system_change is not None and self.lens != system_curation.LENS
        ):
            raise ValueError("a system change rides on a proposal of the lens agent_system and the kind agent_config")
        if self.summary is not None and len(self.summary.encode()) > review.MAX_BODY_BYTES:
            raise ValueError(f"a proposal's summary is at most {review.MAX_BODY_BYTES} bytes of UTF-8")
        self.finding_ids = list(dict.fromkeys(self.finding_ids))
        return self


class Proposal(ProposalSummary):
    summary: str | None
    paths: list[RepoPath]
    impacted: list[RepoPath] | None = Field(description="what the paths reach in the knowledge graph; null without one")
    tier_reasons: list[str] = Field(description="why it has its tier, rule by rule")
    finding_ids: list[int]
    evidence: list[dict] = Field(description="its own evidence, as the hub resolved it")
    plan: dict = Field(description="the draft plan; empty for a promotion")
    promotion: dict | None = Field(None, description="kind memory: what it promotes, and the shared memory it wrote")
    system_change: dict | None = Field(
        None, description="kind agent_config: what it changes, and what accepting it made (applied)"
    )
    note: str | None = Field(description="the owner's note with the answer")
    change: Change | None = Field(
        None,
        description="what accepting it made, tier 0 to 2: the Curator's plan, its branch, its pull request and why it "
        "waits; null before, for tier 3, and when the caller may not read its plan",
    )
    probe: stored_probes.Probe | None = Field(
        None, description="its active probe, if any; its verify is null unless the caller is an admin of the project"
    )


class SessionProposalIn(ProposalIn):
    run_id: int = Field(ge=1, le=MAX_ID, description="the local run of your session that records it")

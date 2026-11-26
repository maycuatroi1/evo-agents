"""The shapes of what the weekly probe report asks of a review run (``evo_agents.hub.server.probes.service.fixes``
decides it): an ``Assignment`` to fix a change's rule or to replace a probe."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from evo_agents.hub import review

WEEK = timedelta(days=7)
OPEN = ("open", "deferred")  # the states of a fix proposal still to answer
RESOLVED = "probe_report"  # how the hub found the probe evidence it adds


@dataclass
class Assignment:
    """What a review run is asked to do of one change (``fix``) or one probe (``replace``)."""

    kind: str  # fix | replace
    change_id: int
    title: str  # the title of the change's proposal
    repo: str  # the probe's repo
    probe_id: int | None = None  # replace: the probe to replace
    task: str | None = None  # replace: its task, clipped
    results: list[dict] = field(default_factory=list)  # the probe evidence of both weeks, the older first
    stale: list[str] = field(default_factory=list)  # replace: why it was stale, each week and model, the older first

    def prompt(self) -> dict:
        """The assignment as ``review.build_review_prompt`` reads it."""
        shown = sorted({(item["probe_id"], item["runtime"], item.get("model") or "") for item in self.results})
        return {
            "kind": self.kind,
            "change_id": self.change_id,
            "title": self.title,
            "repo": self.repo,
            "probe_id": self.probe_id,
            "task": self.task,
            "weeks": sorted({item["week"] for item in self.results}),
            "probes": [f"probe #{probe} on {runtime}{f' {model}' if model else ''}" for probe, runtime, model in shown],
            "evidence": [review.evidence_text(item) for item in self.results],
            "statuses": sorted({item["status"] for item in self.results}),
            "stale": self.stale,
        }

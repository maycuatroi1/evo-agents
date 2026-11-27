"""What the hub read of the CI of one release tag (``service.watch``)."""

from __future__ import annotations

from dataclasses import dataclass, field

from evo_agents.hub import release_ci
from evo_agents.hub.server.releases.models.actions import WorkflowRun


@dataclass(frozen=True)
class Read:
    """What the hub read of the CI of one tag: the run of the workflow, its jobs once it completed, and the failures
    of a red one; or what kept it from reading (``blocked``: the installation grants no actions)."""

    run: WorkflowRun | None = None
    jobs: list[dict] = field(default_factory=list)
    failures: dict | None = None
    problem: str | None = None
    blocked: bool = False

    @property
    def passed(self) -> bool:
        return (
            self.run is not None
            and self.run.completed
            and release_ci.published({"conclusion": self.run.conclusion}, self.jobs)
        )

    @property
    def red(self) -> bool:
        """Completed, not published, with a job that failed."""
        return self.failures is not None and bool(self.failures.get("jobs"))

"""What the merge queue's gate reads of a project once a pass, the merge requests it opens for a run's work, and
its spans (``service.gate``, ``service.gate_pass``)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from evo_agents.hub import autonomy

OPEN_RETRY = timedelta(hours=1)  # a merge request GitLab would not open is asked for again after this
MAX_REVISIONS = 500  # of a policy, read for when a repo went to mode auto


@dataclass(frozen=True)
class Project:
    """What a pass reads of a project once."""

    id: int
    name: str
    policy: dict  # autonomy.effective
    repos: dict[str, autonomy.RepoFacts]
    revisions: list[tuple[datetime, dict | None]]  # the newest first
    owner_id: int | None  # who wrote the newest revision: the judge runs and the graph's reads are theirs
    charter: dict


@dataclass
class Candidate:
    """A merge request the hub opens for a run's work."""

    source: str  # plan or task
    branch: str
    title: str
    description: str
    run_id: int | None
    plan_id: str | None
    steps: list[str]
    row_id: int | None  # its row, when an earlier pass could not open it

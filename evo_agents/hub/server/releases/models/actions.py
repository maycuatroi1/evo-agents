"""What the hub keeps of GitHub Actions for a release tag (``service.actions``): the run of a tag's workflow, the
refusal of an installation that grants no actions, and the page sizes it reads."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub import release_ci
from evo_agents.hub.server.forge.models.github import GitHubRefused

RUNS_PAGE = 20  # runs of the workflow for one commit the hub lists
JOBS_PAGE = 100
MAX_JOB_PAGES = 3
INSTALLATIONS_PAGE = "https://github.com/settings/installations"


class NoActions(GitHubRefused):
    """The App's installation does not grant actions at the level a call needs."""


@dataclass(frozen=True)
class WorkflowRun:
    """The run of a tag's workflow, as the hub keeps it."""

    id: int
    status: str | None
    conclusion: str | None
    url: str | None
    attempt: int | None
    started_at: str | None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


def actions_problem(owner: str, repo: str, level: str) -> str:
    return (
        f"{release_ci.ACTIONS_PERMISSION}: the App's installation on {owner}/{repo} does not grant actions {level}, "
        f"so the hub cannot read the CI of a release tag or run its failed jobs again: a member of {owner} accepts "
        f"the App's new permissions on the installation's page ({INSTALLATIONS_PAGE})"
    )

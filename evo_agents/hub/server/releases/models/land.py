"""A release run waiting to land, and why the hub refuses to land one (``service.land``)."""

from __future__ import annotations

from dataclasses import dataclass

from evo_agents.hub import releases

TAG = "release.tag"  # the hub tagged a release: "<project> cycle:<id> run:<id> tag=<tag> commit=<sha>"


class Refusal(Exception):
    """Why the hub does not land a run, in words safe to show."""

    cause = "refused"


class Hacking(Refusal):
    """The run's change shows signs of test hacking."""

    cause = "hacking"


@dataclass(frozen=True)
class Landing:
    """A release run waiting to land, with its cycle, schedule, project and repo."""

    run_id: int
    cycle_id: int
    project_id: int
    project: str
    owner_id: int
    owner: str
    repo: str
    origin: str | None
    tag_pattern: str
    workflow: str
    target_ref: str
    target_commit: str | None
    base_tag: str | None
    version: str
    branch: str
    head_commit: str
    decision: str | None
    pull_number: int | None
    merge_commit: str | None
    cycle_state: str

    @property
    def tag(self) -> str:
        return releases.tag_prefix(self.tag_pattern) + self.version

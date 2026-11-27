"""The errors and the findings of the hub's own calls to GitHub (``service.pulls``)."""

from __future__ import annotations

from dataclasses import dataclass, field

from evo_agents.hub.server.github import GitHubRefused


class Refused(GitHubRefused):
    """GitHub answered a call with a refusal: its ``status`` and the items of its ``errors``, each {field, code,
    message} as far as GitHub gave them."""

    def __init__(self, text: str, status: int | None = None, errors: list[dict] | None = None):
        super().__init__(text)
        self.status = status
        self.errors = errors or []


class Truncated(GitHubRefused):
    """GitHub lists more than the hub reads: the list is not read in part."""


@dataclass(frozen=True)
class RulesetCheck:
    """What ``check_ruleset`` found of a repo: whether its default branch keeps the Curator's App off, and why; and the
    checks its active rulesets require before a merge."""

    protected: bool
    default_branch: str | None
    reason: str
    rulesets: list[dict] = field(default_factory=list)  # each {id, enforcement, can_bypass}
    required_checks: list[dict] = field(default_factory=list)  # each {context, integration_id}

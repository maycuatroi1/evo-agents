"""The routes of the hub ``evo-agents hub curator`` asks, one gateway each (``evo_agents.hub.cli.base.ProjectResource``),
and the keys of their answers that the commands declare for ``--json`` (the contract, ``evo-agents hub contract print``).

- :class:`NightShift`: GET /v1/projects/{p}/curator, and POST .../curator/pause and .../curator/resume.
- :class:`Charters`: GET and PUT .../curator/charter, and GET .../curator/charter/revisions.
- :class:`Proposals`: GET .../curator/proposals and .../{id}, and POST .../{id}/answer and .../{id}/plan.
- :class:`Findings` and :class:`Figures`: GET .../curator/findings and .../curator/figures.
- :class:`Changes`: GET .../curator/changes and POST .../changes/{id}/close.
- :class:`Protection`: GET .../curator/protection and POST .../protection/{repo}/check.

Standard library only, like the rest of the client.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from evo_agents.hub.cli.base import ProjectResource
from evo_agents.hub.contract import returns_object
from evo_agents.hub.curator import CHARTER_META

CHARTER_BODY_KEYS = (
    "goals",
    "window",
    "pacing",
    "night_budget_usd",
    "run_budget_usd",
    "run_max_turns",
    "run_minutes",
    "max_runs_per_night",
    "night_plans",
    "max_decisions_per_day",
    "brief_at",
    "planned_tiers",
    "auto_merge",
    "merge_without_ci",
    "answer_by",
    "protected_paths",
    "circuit_breaker",
    "outcome_days",
    "review",
    "replay",
    "history",
    "probes",
    "evolve",
    "reviewer",
    "builder",
    "judge",
    "git_secret",
    "env_secrets",
    "read_git_secrets",
)
CHARTER_KEYS = CHARTER_BODY_KEYS + CHARTER_META
REVISION_KEYS = ("revision", "updated_by", "updated_at")
STATUS_KEYS = (
    "project",
    "charter",
    "paused",
    "schedules",
    "night",
    "last_review_run",
    "state",
    "open_proposals",
    "last_brief",
)
PROPOSAL_SUMMARY_KEYS = (
    "id",
    "project",
    "run_id",
    "lens",
    "kind",
    "title",
    "tier",
    "state",
    "evidence_count",
    "duplicate_of",
    "answered_by",
    "answered_at",
    "deferred_until",
    "inbox_at",
    "created_at",
    "revert_of",
    "fix_of",
)
PROPOSAL_KEYS = PROPOSAL_SUMMARY_KEYS + (
    "summary",
    "paths",
    "impacted",
    "tier_reasons",
    "finding_ids",
    "evidence",
    "plan",
    "promotion",
    "system_change",
    "note",
    "change",
    "probe",
)
FINDING_KEYS = ("id", "project", "run_id", "lens", "severity", "title", "body", "evidence", "created_at", "fix_of")
CHANGE_KEYS = (
    "id",
    "project",
    "proposal_id",
    "plan_id",
    "repo",
    "branch",
    "forge",
    "tier",
    "state",
    "reason",
    "builder_run_id",
    "judge_run_id",
    "pr_number",
    "pr_url",
    "base_branch",
    "head_sha",
    "passed",
    "verdict",
    "merged_at",
    "merge_sha",
    "created_at",
    "updated_at",
    "repos",
)
REPO_CHECK_KEYS = (
    "repo",
    "origin",
    "forge",
    "github_repo",
    "protected",
    "default_branch",
    "reason",
    "rulesets",
    "checked_at",
    "checked_by",
)
PROPOSAL = returns_object(*PROPOSAL_KEYS, schema="Proposal")
STATUS = returns_object(*STATUS_KEYS, schema="CuratorStatus")
CHARTER = returns_object(*CHARTER_KEYS, schema="Charter")
CHANGE = returns_object(*CHANGE_KEYS, schema="Change")


class NightShift(ProjectResource):
    """Where the night shift of a project stands, and its switch."""

    collection = "curator"

    def status(self) -> Any:
        return self.call("GET")

    def switch(self, action: str) -> Any:
        """``action`` is pause or resume; the hub answers the status after it."""
        return self.call("POST", action)


class Charters(ProjectResource):
    """The charter of a project and its revisions."""

    collection = "curator/charter"

    def get(self, revision: int | None = None) -> Any:
        return self.call("GET", revision=revision)

    def put(self, body: dict) -> Any:
        return self.call("PUT", body=body)

    def revisions(self) -> Any:
        return self.call("GET", "revisions")


class Proposals(ProjectResource):
    """The proposals of a project's review runs."""

    collection = "curator/proposals"

    def list(self, **query: Any) -> Any:
        """One page of the proposals: a list value repeats its key, a value of None is left out, in the order given."""
        given = urlencode({key: value for key, value in query.items() if value is not None}, doseq=True)
        return self.hub.call("GET", self.path() + (f"?{given}" if given else ""))

    def get(self, proposal_id: int) -> Any:
        return self.call("GET", proposal_id)

    def answer(self, proposal_id: int, body: dict) -> Any:
        return self.call("POST", proposal_id, "answer", body=body)

    def plan(self, proposal_id: int) -> Any:
        return self.call("POST", proposal_id, "plan")


class Findings(ProjectResource):
    """What the review runs found."""

    collection = "curator/findings"

    def list(self, run: int | None, lens: str | None, limit: int | None, offset: int | None) -> Any:
        return self.call("GET", run_id=run, lens=lens, limit=limit, offset=offset)


class Figures(ProjectResource):
    """The figures curator.collect counted for a night."""

    collection = "curator/figures"

    def get(self, night: str | None = None) -> Any:
        return self.call("GET", night=night)


class Changes(ProjectResource):
    """What the accepted proposals became."""

    collection = "curator/changes"

    def list(self) -> Any:
        return self.call("GET")

    def close(self, change_id: int, reason: str) -> Any:
        return self.call("POST", change_id, "close", body={"reason": reason})


class Protection(ProjectResource):
    """The rulesets that keep the Curator's App off the project's repos."""

    collection = "curator/protection"

    def list(self) -> Any:
        return self.call("GET")

    def check(self, repo: str) -> Any:
        return self.call("POST", repo, "check")

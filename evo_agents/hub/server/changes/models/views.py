"""The models of the Curator's changes (``evo_agents.hub.server.changes.service.job`` describes the design): the API's
shapes of a change and of a judge run's inputs and verdict, the judge run's key header, and the policy of a run of the
Curator (``CuratorPolicy``)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Header
from pydantic import BaseModel, Field

from evo_agents.hub import curator, diff_signs, findings, judge, security_pass, tiers
from evo_agents.hub.server.runs.models import OBJECT_NAME, VerifyResult

JudgeKey = Annotated[
    str | None,
    Header(
        alias=judge.JUDGE_KEY_HEADER,
        max_length=200,
        description="the judge run's own key, which its claim handed the daemon; the worker token alone is refused",
    ),
]


# Models


class ChangeRepo(BaseModel):
    """One repo of a change that works in several (``evo_agents.hub.server.change_repos``), in plan order."""

    repo: str
    position: int = Field(description="its order in the plan, from 1: an owner before the repos that consume it")
    depends_on: list[str] = Field(description="the repos merged before it")
    state: str
    reason: str | None
    pr_number: int | None
    pr_url: str | None
    passed: bool | None = Field(description="its Judge's verdict; null before it")
    merged_at: datetime | None


class Change(BaseModel):
    """What an accepted proposal of tier 0 to 2 became, and where it stands."""

    id: int
    project: str
    proposal_id: int
    plan_id: str | None = Field(description="the Curator's plan on the hub; null when the draft could not become one")
    repo: str | None
    branch: str | None = Field(description="curator/..., the one branch its runs push")
    forge: judge.Forge | None
    tier: int = Field(ge=0, le=3, description="its proposal's tier; 3 once a sign of score hacking showed")
    state: judge.ChangeState
    reason: str | None = Field(description="why it stays as it is, such as why its pull request stays open")
    builder_run_id: int | None
    judge_run_id: int | None
    pr_number: int | None
    pr_url: str | None
    base_branch: str | None
    head_sha: str | None = Field(description="the commit the Judge judges, and the hub merges")
    passed: bool | None = Field(description="the Judge's verdict; null before it")
    verdict: dict | None = Field(description="the verdict: the Judge's reasons, the checks run, the signs found")
    merged_at: datetime | None
    merge_sha: str | None
    created_at: datetime
    updated_at: datetime
    repos: list[ChangeRepo] = Field(
        default_factory=list, description="a change in several repos: each, in plan order; empty for one in one repo"
    )


class ChangeList(BaseModel):
    project: str
    changes: list[Change] = Field(description="newest first")


class ChangeClose(BaseModel):
    reason: str = Field(min_length=1, max_length=2000, description="why it is closed: its reason and ledger say it")


MAX_DROP_NOTE = 8000  # characters of what a Builder that drops its change measured, kept in the ledger line


class ChangeDrop(BaseModel):
    reason: str = Field(
        min_length=1, max_length=2000, description="why its Builder drops the change, in one line: its reason says it"
    )
    note: str | None = Field(
        default=None,
        max_length=MAX_DROP_NOTE,
        description="what the Builder measured or found, kept whole in the ledger line (details.note) of the drop",
    )


class JudgeInputs(BaseModel):
    """What a judge run reads: the change, the proposal, the plan's verify commands, the charter's protected paths,
    and the project's hidden checks, which no other run reads."""

    change_id: int
    repo: str
    branch: str
    base_branch: str | None
    head_sha: str | None = Field(description="the commit to judge; null on GitLab: the branch's tip")
    proposal: dict = Field(description="id, title, kind, tier, summary, paths")
    verify: list[str] = Field(description="the verify of each step of the plan, as the Builder had them")
    protected_paths: list[str]
    evolvable_paths: list[str] = Field(
        default_factory=list,
        description="the charter's evolve.evolvable_paths: a harness file of these is no protected_path sign",
    )
    hidden_checks: list[str] = Field(description="commands run in the worktree; never logged, never shown")
    knowledge: dict | None = Field(
        None,
        description="a change of kind kg_source whose pull request changed knowledge.yaml: its files, the sources it "
        "edits and the checks the worker runs for it besides the verify (harness validate, a trial sync of each "
        "source), whose results the verdict's verify carries under those names; null for any other change",
    )


class HiddenResult(BaseModel):
    index: int = Field(ge=1, le=curator.MAX_HIDDEN_CHECKS, description="the check's place in the charter, from 1")
    exit_code: int
    duration_ms: int | None = Field(None, ge=0)
    base_exit_code: int | None = Field(
        None, description="a merge check's failed check: its exit code on the base branch alone, where it ran again"
    )


class Sign(BaseModel):
    kind: diff_signs.SignKind
    path: str = Field(min_length=1, max_length=1000)
    line: int | None = Field(None, ge=0)
    text: str = Field("", max_length=diff_signs.MAX_SIGN_TEXT)


class SecurityFinding(BaseModel):
    """A vulnerability the security pass kept after its filter."""

    path: str | None = Field(None, max_length=findings.MAX_PATH_CHARS)
    line: int | None = Field(None, ge=1)
    text: str = Field(min_length=1, max_length=findings.MAX_TEXT_CHARS)


class SecurityPass(BaseModel):
    """What the security pass of a Judge run (``evo_agents.hub.security_pass``) said of the change, and what it cost:
    the dimension ``security``, its findings with path and line, and the turns the pass took."""

    verdict: judge.Verdict
    reason: str = Field("", max_length=2000)
    findings: list[SecurityFinding] = Field(default_factory=list, max_length=findings.MAX_NEW)
    candidates: int = Field(0, ge=0, description="the findings the first turn raised, before the filter")
    turns: int = Field(0, ge=0, description="agent turns the pass took: one that finds, one for each candidate")
    cost_usd: float | None = Field(None, ge=0, description="what those turns cost, when the runtime reports it")


def security_dimension(security: SecurityPass | None) -> dict:
    """The dimension ``security`` of a verdict from the pass; a verdict without the pass fails it."""
    if security is None:
        return {"verdict": "fail", "reason": "the security pass gave no result"}
    return {"verdict": security.verdict, "reason": security.reason}


def security_findings(security: SecurityPass | None) -> list[dict]:
    """The findings of the pass as ``findings.read_new`` reads them."""
    found = [] if security is None else [item.model_dump() for item in security.findings]
    return findings.read_new([{**item, "dimension": security_pass.DIMENSION} for item in found])


class VerdictIn(BaseModel):
    verdict: judge.Verdict | None = Field(description="the Judge agent's; null when it gave none")
    security: SecurityPass | None = Field(
        None, description="the security pass of the run: a verdict without it fails the dimension security"
    )
    reasons: str | None = Field(None, max_length=judge.MAX_REASONS_CHARS)
    head_sha: str = Field(pattern=OBJECT_NAME, description="the commit the worktree was at")
    base_sha: str | None = Field(None, pattern=OBJECT_NAME)
    verify: list[VerifyResult] = Field(default_factory=list, max_length=50)
    hidden: list[HiddenResult] = Field(default_factory=list, max_length=curator.MAX_HIDDEN_CHECKS)
    signs: list[Sign] = Field(default_factory=list, max_length=diff_signs.MAX_SIGNS)
    paths: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] = Field(
        default_factory=list,
        max_length=judge.MAX_PATHS,
        description="every path the diff judged touches; they give the proposal its tier again, which only raises it",
    )


# What a run of the Curator gets

READING_ROLES = ("reviewer", "judge")  # the Curator's runs that read their repos and push nothing


@dataclass(frozen=True)
class CuratorPolicy:
    """A run of the Curator, and what its charter lets it have: its role, the owner's git secret it uses on other
    forges than GitHub, the more git secrets a run that only reads may read with (``read_git_secrets``), the env
    secrets it gets, and the charter's protected paths (a Builder's without those its accepted proposal lists,
    ``service.policy``)."""

    role: str  # reviewer, builder, judge or replay
    git_secret: str | None
    env_secrets: tuple[str, ...]
    protected: tuple[str, ...]
    change_id: int | None = None
    read_git_secrets: tuple[str, ...] = ()

    @property
    def git_secrets(self) -> tuple[str, ...]:
        """The names of the owner's git secrets this run may be leased: the charter's git_secret, and for a run that
        only reads (READING_ROLES) its read_git_secrets too."""
        named = (self.git_secret,) if self.git_secret else ()
        return named + (self.read_git_secrets if self.role in READING_ROLES else ())


def key_digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()

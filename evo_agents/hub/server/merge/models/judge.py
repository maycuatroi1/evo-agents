"""The shapes of what a judge run of the merge queue, or of a merge check, reads and posts (``service.judge``)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import diff_signs, findings, judge, tiers
from evo_agents.hub.server.changes.models.views import HiddenResult, SecurityPass, Sign
from evo_agents.hub.server.runs.models import OBJECT_NAME, VerifyResult

MAX_DIMENSIONS = 20


class MergeJudgeInputs(BaseModel):
    """What a judge run of the queue reads: the merge request, the verify of its plan's steps in the repo, the
    charter's protected paths, and the project's hidden checks, which no other run reads."""

    queue_id: int
    repo: str
    iid: int
    branch: str
    base_branch: str
    head_sha: str | None
    second: bool = Field(description="the second Judge, of a merge request of high risk")
    verify: list[str]
    protected_paths: list[str]
    hidden_checks: list[str] = Field(description="commands run in the worktree; never logged, never shown")


class Dimension(BaseModel):
    verdict: Literal[judge.VERDICTS]  # pyright: ignore[reportInvalidTypeForm]
    reason: str = Field("", max_length=2000)


class JudgeFinding(BaseModel):
    """A thing the Judge found wrong, on one of the dimensions it judges."""

    dimension: str = Field(min_length=1, max_length=findings.MAX_DIMENSION_CHARS)
    path: str | None = Field(None, max_length=findings.MAX_PATH_CHARS)
    line: int | None = Field(None, ge=1)
    text: str = Field(min_length=1, max_length=findings.MAX_TEXT_CHARS)


class FindingMark(BaseModel):
    """What the Judge says of a finding of an earlier round."""

    status: Literal[findings.MARKS]  # pyright: ignore[reportInvalidTypeForm]
    where: str | None = Field(None, max_length=findings.MAX_WHERE_CHARS, description="the file:line that shows it")
    reason: str = Field("", max_length=findings.MAX_TEXT_CHARS)


class MergeVerdictIn(BaseModel):
    verdict: Literal[judge.VERDICTS] | None = Field(description="the Judge agent's; null when it gave none")  # pyright: ignore[reportInvalidTypeForm]
    reasons: str | None = Field(None, max_length=judge.MAX_REASONS_CHARS)
    head_sha: str = Field(pattern=OBJECT_NAME, description="the commit the worktree was at")
    base_sha: str | None = Field(None, pattern=OBJECT_NAME)
    verify: list[VerifyResult] = Field(default_factory=list, max_length=50)
    hidden: list[HiddenResult] = Field(default_factory=list, max_length=50)
    signs: list[Sign] = Field(default_factory=list, max_length=diff_signs.MAX_SIGNS)
    paths: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] = Field(
        default_factory=list, max_length=judge.MAX_PATHS
    )
    dimensions: dict[str, Dimension] | None = Field(
        None, description="each dimension the Judge judged apart, with its verdict and reason"
    )
    findings: list[JudgeFinding] = Field(
        default_factory=list, max_length=findings.MAX_NEW, description="what the Judge found wrong in this round"
    )
    prior: dict[str, FindingMark] | None = Field(
        None, description="the Judge's mark of each finding of an earlier round its prompt listed, by id"
    )
    security: SecurityPass | None = Field(
        None, description="the security pass of the run, which judges the dimension security apart from the agent"
    )
    conflicts: list[Annotated[str, Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)]] | None = Field(
        None,
        max_length=judge.MAX_PATHS,
        description="a merge check's: the paths where merging the base into the head conflicts; null when it merged",
    )
    signs_weighed: bool = Field(
        False,
        description="the Judge weighed the signs of score hacking against the plan (judge.WEIGH_SIGNS) instead of the "
        "signs failing the change unjudged: a merge check's Judge, or the queue's when its claim said signs_passed",
    )


class MergeVerdict(BaseModel):
    queue_id: int
    passed: bool = Field(description="this Judge's verdict, its checks and the worker's reading of the diff")
    state: str = Field(description="where the merge request stands now")
    reason: str | None

"""The models of what a review run writes and the owner's answers (``evo_agents.hub.server.proposals.service.record``):
findings, proposals, their evidence, the answer, and the night's figures as members read them.
``evo_agents.hub.review`` and ``evo_agents.hub.tiers`` hold the rules they check."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from fastapi import Path
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub import review, tiers
from evo_agents.hub.server.runs.models import LINE, MAX_ID, OBJECT_NAME

Title = Annotated[str, Field(min_length=1, max_length=review.MAX_TITLE_CHARS, pattern=LINE)]
RepoName = Annotated[str, Field(pattern=review.REPO_NAME)]
ItemId = Annotated[int, Path(ge=1, le=MAX_ID)]


class EvidenceIn(BaseModel):
    """One piece of evidence, as ``review.parse_evidence`` reads it from the command line."""

    kind: review.EvidenceKind
    session_id: str | None = Field(None, pattern=review.SESSION_ID, description="session, transcript: the session")
    file: str | None = Field(None, pattern=review.TRANSCRIPT_FILE, max_length=512, description="transcript: its file")
    field: review.DigestField | None = Field(None, description="session: the list of the digest")
    index: int | None = Field(None, ge=0, le=10_000, description="session: the entry of that list, from 0")
    run_id: int | None = Field(None, ge=1, le=MAX_ID, description="run: the run")
    seq: int | None = Field(None, ge=1, le=2**31 - 1, description="run: the seq of its event")
    repo: RepoName | None = Field(None, description="code: a repo of the project")
    path: str | None = Field(None, min_length=1, max_length=tiers.MAX_PATH_CHARS, description="code: within the repo")
    line: int | None = Field(None, ge=1, le=review.MAX_LINE, description="code, transcript: the line")
    commit: str | None = Field(None, pattern=OBJECT_NAME, description="code: the commit the worktree was at")
    memory_id: int | None = Field(None, ge=1, le=MAX_ID, description="memory: a memory of the project")
    revision: int | None = Field(None, ge=1, le=review.MAX_REVISION, description="memory: one of its revisions")
    span_id: int | None = Field(None, ge=1, le=MAX_ID, description="span: a span of an agent system's trace")

    @model_validator(mode="after")
    def _complete(self):
        needed = {"session": ("session_id",), "transcript": ("session_id", "line"), "run": ("run_id", "seq")}
        needed = {**needed, "code": ("repo", "path"), "memory": ("memory_id", "revision"), "span": ("span_id",)}
        needed = needed[self.kind]
        if self.kind == "transcript":
            self.file = self.file or review.MAIN_FILE
        missing = [name for name in needed if getattr(self, name) is None]
        if missing:
            raise ValueError(f"{self.kind} evidence needs {', '.join(missing)}")
        if (self.field is None) != (self.index is None):
            raise ValueError("a digest's entry is named by field and index together")
        if self.path is not None:
            found = tiers.normalize_path(self.path)
            if found is None:
                raise ValueError(f"{self.path!r} is not a path within a repo")
            self.path = found
        return self

    def stored(self) -> dict:
        return {key: value for key, value in self.model_dump().items() if value is not None}


class FindingIn(BaseModel):
    lens: review.Lens
    severity: review.Severity = "medium"
    title: Title
    body: str | None = Field(None, min_length=1, max_length=review.MAX_BODY_BYTES, description="markdown, 16 KiB")
    evidence: list[EvidenceIn] = Field(min_length=1, max_length=review.MAX_EVIDENCE)
    fix_of: int | None = Field(
        None,
        ge=1,
        le=MAX_ID,
        description="the merged change whose probe recurred two weekly reports in a row that the prompt asked this run "
        "to fix: why this run proposes no fix of its rule; the hub adds the probe results as evidence",
    )

    @model_validator(mode="after")
    def _bounded(self):
        if self.body is not None and len(self.body.encode()) > review.MAX_BODY_BYTES:
            raise ValueError(f"a finding's body is at most {review.MAX_BODY_BYTES} bytes of UTF-8")
        return self


class Finding(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the review run that recorded it")
    lens: str
    severity: review.Severity
    title: str
    body: str | None
    evidence: list[dict] = Field(description="each piece as the hub resolved it, with how (resolved)")
    created_at: datetime
    fix_of: int | None = Field(None, description="the change it says why this run proposed no fix of")


class FindingList(BaseModel):
    findings: list[Finding] = Field(description="newest first")
    total: int
    limit: int
    offset: int


class PathIn(BaseModel):
    repo: RepoName
    path: str = Field(min_length=1, max_length=tiers.MAX_PATH_CHARS)

    @model_validator(mode="after")
    def _normal(self):
        found = tiers.normalize_path(self.path)
        if found is None:
            raise ValueError(f"{self.path!r} is not a path within a repo")
        self.path = found
        return self


class RepoPath(BaseModel):
    repo: str
    path: str


class ProposalSummary(BaseModel):
    id: int
    project: str
    run_id: int = Field(description="the review run that proposed it")
    lens: str
    kind: str
    title: str
    tier: int = Field(ge=0, le=3, description="as the hub's tier rules computed it")
    state: review.ProposalState
    evidence_count: int = Field(description="the pieces of evidence it rests on, its findings' included")
    duplicate_of: int | None = Field(description="the rejected proposal a dropped one repeats")
    answered_by: str | None
    answered_at: datetime | None
    deferred_until: datetime | None
    inbox_at: datetime | None = Field(
        description="when it became an Inbox item of the owner; tier 2 only, and a revert the hub proposes"
    )
    created_at: datetime
    revert_of: int | None = Field(
        None, description="the proposal whose merged change it reverts: the hub proposed it once its figures got worse"
    )
    fix_of: int | None = Field(
        None, description="the merged change whose rule it fixes: its probe recurred two weekly reports in a row"
    )


class ProposalCounts(BaseModel):
    """How many proposals each value of a filter would list, the other filters applied: every state, tier and lens
    named, with 0 where none is."""

    state: dict[str, int]
    tier: dict[str, int] = Field(description='by tier, "0" to "3"')
    lens: dict[str, int]


class ProposalList(BaseModel):
    proposals: list[ProposalSummary] = Field(description="newest first")
    total: int
    limit: int
    offset: int
    counts: ProposalCounts | None = Field(None, description="for the list's filters: what each of their values holds")


class ProposalAnswer(BaseModel):
    action: review.Answer
    note: str | None = Field(None, min_length=1, max_length=review.MAX_NOTE_CHARS, pattern=LINE)
    defer_days: int | None = Field(
        None, ge=review.DEFER_DAYS[0], le=review.DEFER_DAYS[1], description="with defer; 7 by default"
    )

    @model_validator(mode="after")
    def _days(self):
        if self.defer_days is not None and self.action != "defer":
            raise ValueError("defer_days goes with the action defer")
        return self


class NightFiguresView(BaseModel):
    project: str
    night: date
    since: datetime
    until: datetime
    run_id: int | None = Field(description="the review run queued on them, if any")
    figures: dict = Field(description="the figures, as curator.collect counted them")
    created_at: datetime


class SessionFindingIn(FindingIn):
    run_id: int = Field(ge=1, le=MAX_ID, description="the local run of your session that records it")

"""The requests and answers of the history of a project: the batches of the night's history run and what its worker
reports (``HistoryBatch``, ``HistoryReport``), where the run stands and the clusters (``HistoryStatus``,
``FacetClusterList``), the sessions and runs, the hits of a search and the lines of a transcript (``HistorySessions``,
``HistorySearch``, ``TranscriptLines``), with the limits they take."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from evo_agents.hub import history
from evo_agents.hub.server.base.models import StrictModel

# The night's history run
CANDIDATES = history.BATCH_ITEMS * 4  # waiting items one batch looks at, the latest first
MAX_RUNS_LISTED = 10
MAX_REPORTED = 50
LONG_WAIT = timedelta(days=7)  # an item the model gave no facet MAX_ATTEMPTS times waits this long

# Reading the whole history
SOURCES = ("transcript", "run", "chat")
AGENT_SOURCES = ("transcript", "run")  # what the agent of a run other than the Reviewer searches (see the package)
KINDS = ("session", "run")
SCAN_ROWS = 5000  # facets one list examines
SCAN_HITS = 200  # hits each source answers before they are merged
MAX_LIMIT = 100
MAX_SEARCH = 50
MAX_AROUND = 100  # lines before and after the one asked for
QUERY_CHARS = 500

Item = Annotated[str, Field(pattern=history.ITEM.pattern, description="session:ID or run:ID")]
GroupKey = Annotated[str, Field(pattern=history.KEY.pattern, description="<task_type>/<failure mode or none>")]


# The night's history run


class HistoryNext(StrictModel):
    clusters: bool = Field(False, description="group the facets again and add the groups that have no name yet")


class HistoryItem(BaseModel):
    item: str = Field(description="session:ID or run:ID")
    kind: str
    source: str = Field(description="what it is, in words")
    fingerprint: str = Field(description="the content it was read at; the report names it back")
    lines: int = Field(description="the entries of its transcript")
    anchors: int = Field(description="its errors and corrections")
    cut: bool = Field(description="whether the excerpt leaves part of it out")
    excerpt: str


class GroupSample(BaseModel):
    summary: str
    friction: list[str]


class HistoryGroup(BaseModel):
    key: str
    label: str = Field(description="its kind of task and failure mode, in words")
    sessions: int
    samples: list[GroupSample]


class NamedGroup(BaseModel):
    key: str
    name: str
    description: str | None


class HistoryBatch(BaseModel):
    items: list[HistoryItem] = Field(description="the next batch, the latest first; none once nothing waits")
    waiting: int = Field(description="the items without a facet of the model, this batch's included")
    groups: list[HistoryGroup] = Field(description="with clusters: the groups of facets that have no name yet")
    named: list[NamedGroup] = Field(description="with clusters: the groups named before")


class FacetReport(StrictModel):
    item: Item
    fingerprint: str = Field(min_length=1, max_length=200)
    facet: dict | None = Field(None, description="the facet the model wrote (history.clean_facet)")
    error: str | None = Field(None, min_length=1, max_length=history.MAX_ERROR_CHARS, description="why there is none")

    @model_validator(mode="after")
    def _one(self):
        if (self.facet is None) == (self.error is None):
            raise ValueError("a reported item has a facet or an error, not both")
        return self


class NameReport(StrictModel):
    key: GroupKey
    name: str = Field(min_length=1, max_length=history.MAX_NAME_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")
    description: str | None = Field(None, min_length=1, max_length=history.MAX_DESCRIPTION_CHARS)
    same_as: GroupKey | None = Field(None, description="the group it is the same as, which it counts in")


class HistoryReport(StrictModel):
    facets: list[FacetReport] = Field(default_factory=list, max_length=MAX_REPORTED)
    names: list[NameReport] = Field(default_factory=list, max_length=history.MAX_NAMED)


class HistoryReported(BaseModel):
    stored: int = Field(description="facets kept")
    stale: int = Field(description="facets of content that changed since, or of an item gone, left")
    failed: int = Field(description="items the model gave none, which wait")
    named: int = Field(description="groups that got their name")
    waiting: int = Field(description="the items without a facet of the model now")


# Where the history run stands, and the clusters


class LlmCoverage(BaseModel):
    version: int
    items: int = Field(description="sessions and runs the model reads, as the reader may read them")
    analyzed: int = Field(description="those with a facet of the model of their content now")
    waiting: int
    failed: int = Field(description="those whose last attempt failed")


class HistoryRunView(BaseModel):
    id: int
    night: date | None
    state: str
    model: str | None
    cost_usd: float | None
    analyzed: int
    failed: int
    stale: int
    named: int
    queued_at: datetime
    finished_at: datetime | None


class HistoryStatus(BaseModel):
    project: str
    budget_usd: float | None = Field(description="the night's cap of the history run; null without a charter")
    runtime: str | None = Field(description="the reviewer's runtime it runs on")
    model: str | None = Field(description="the charter's history.model; null: the cheapest the worker reports")
    llm: LlmCoverage
    runs: list[HistoryRunView] = Field(description="the latest history runs, newest first")
    clusters: int = Field(description="clusters in the night's figures")
    hidden: int = Field(description="smaller clusters, which stay out of them")


class FacetCluster(BaseModel):
    key: str
    name: str
    named: bool = Field(description="whether the model named it; else its name is its key in words")
    description: str | None
    task_type: str
    failure_mode: str
    sessions: int = Field(description="its sessions, a run counting as one")
    runs: int
    members: int
    representative: str | None = Field(description="its latest session, as evidence names it")
    examples: list[str]
    keys: list[str] = Field(description="the groups it holds: its own and those the model joined to it")


class FacetClusterList(BaseModel):
    project: str
    min_sessions: int
    clusters: list[FacetCluster]
    hidden: int = Field(description="clusters of fewer sessions, left out")
    counted_at: datetime | None


# The sessions and runs, a search, the lines of a transcript


class HistorySessionItem(BaseModel):
    kind: Literal[KINDS]  # pyright: ignore[reportInvalidTypeForm]
    session_id: str | None = Field(description="a session's id; for a run, the session its agent ran in, if known")
    run_id: int | None
    run_kind: str | None = Field(description="the kind of a run")
    state: str | None = Field(description="the state of a run")
    plan_id: str | None
    login: str | None = Field(description="the member whose session or run it is")
    runtime: str | None
    at: datetime | None = Field(description="when it ended, or its last message")
    source: str | None = Field(description="what its facet was made from: transcript, digest, run_log or events")
    numbers: dict = Field(description="the numbers of its facet: messages, tool errors, corrections, tokens, cost")
    summary: str | None = Field(description="what the cheap model wrote of it")
    outcome: str | None = Field(description="the outcome the model wrote")
    failure_modes: list[str]
    cluster: str | None = Field(description="the key of the cluster it counts in")
    evidence: str = Field(description="how a finding cites it")
    transcript: bool = Field(description="the hub holds its transcript: transcript_lines reads it")


class HistorySessions(BaseModel):
    project: str
    items: list[HistorySessionItem] = Field(description="the latest first")
    total: int = Field(description="those that match, of the facets examined")
    limit: int
    offset: int


class HistoryHit(BaseModel):
    source: Literal[SOURCES]  # pyright: ignore[reportInvalidTypeForm]
    at: datetime | None
    role: str = Field(
        description="who: user, agent, call TOOL, TOOL result or failed, an error; for a chat, the author"
    )
    text: str = Field(description="the words around the match, the matched ones between « and »")
    rank: float
    evidence: str | None = Field(description="how a finding cites it; null for a chat message")
    session_id: str | None
    file: str | None
    line: int | None = Field(description="the line of the transcript, or the seq of the run's event")
    run_id: int | None
    run_kind: str | None
    chat_id: int | None
    chat: str | None = Field(description="the chat's title")
    login: str | None = Field(description="the member whose session or run it is, or the chat message's author")
    runtime: str | None
    _gate: tuple | None = PrivateAttr(default=None)  # what tells whether a sink may read it (``gated``); not served

    def gated(self, gate: tuple) -> HistoryHit:
        """The hit with what tells whether a sink may read it: ("label", a label as stored), ("chat", the label of a
        chat message) or ("run", kind, plan id, dispatcher)."""
        self._gate = gate
        return self

    @property
    def gate(self) -> tuple | None:
        return self._gate


class HistorySearch(BaseModel):
    project: str
    query: str
    sources: list[str]
    hits: list[HistoryHit] = Field(description="best first")
    total: int = Field(description="hits found, at most SCAN_HITS of each source")
    limit: int
    offset: int
    reranked: bool = Field(
        default=False,
        description="a ranker of a plugin the project enabled put the hits of this page in a new order; each hit's "
        "rank is still the ts_rank of its words",
    )


class TranscriptLine(BaseModel):
    line: int
    role: str
    text: str


class TranscriptLines(BaseModel):
    project: str
    session_id: str
    file: str
    runtime: str
    login: str
    lines: int = Field(description="lines of the file the hub holds")
    asked: int = Field(description="the line asked for")
    first: int = Field(description="the first line read")
    last: int = Field(description="the last line read")
    entries: list[TranscriptLine] = Field(description="each entry of the lines read, in order")
    evidence: str = Field(description="how a finding cites the line asked for")

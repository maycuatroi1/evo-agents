"""The shapes of the traces of an agent system: the bodies of the routes of ``routes.traces``, the bounds of their
queries and the attributes a reader without a grant on a span's project still sees."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import Path
from pydantic import BaseModel, Field

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID
from evo_agents.hub.server.work_status_models import WorkStatus, WorkWaiting

MAX_TRACES = 200  # traces a list or the metrics read at most
MAX_WINDOW_TRACES = 2000  # traces the spread of a window reads at most, the newest first
MAX_DAYS = 365
MAX_HOURS = 24 * 30
RESUME = "evo.resume_of_run_id"  # the attribute of an invoke_agent span of a run that goes on in a session
SHOWN_ATTRIBUTES = ("gen_ai.",)  # the attributes a reader without a grant on a span's project still sees

TraceId = Annotated[int, Path(ge=1, le=MAX_ID)]
READ_REFUSALS: dict[int | str, dict[str, Any]] = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


class SpanOut(BaseModel):
    id: int
    parent_id: int | None = Field(description="the span it runs under; null for the first span of the trace")
    name: str = Field(
        description="invoke_agent (a turn of an agent) or execute_tool (an ask, a mention, a forward, a kg query)"
    )
    tool: str | None = Field(
        description="the tool of an execute_tool span: agent_ask, chat_mention, agent_forward, kg_*"
    )
    agent: str = Field(description="the handle of the agent")
    node: str | None = Field(description="its node in the spec; @handle for an external agent the spec lacks")
    from_node: str | None = Field(description="the node it came from: the edge it crossed")
    to_node: str | None = Field(description="the node an ask, a mention or a forward went to")
    run_id: int | None
    project: str | None = Field(description="the project of its run")
    runtime: str | None
    status: str = Field(description="running, ok, error or refused")
    off_design: bool = Field(description="the edge it crossed is not an interacts edge of the spec")
    reason: str | None = Field(description="why: the reason an off-edge ask gave, or why the hub refused")
    message_id: int | None = Field(description="the chat message it rests on, which the chat's routes show")
    attributes: dict = Field(description="OpenTelemetry GenAI attributes (gen_ai.*) and the hub's (evo.*)")
    started_at: datetime
    ended_at: datetime | None
    seconds: float | None = Field(description="how long it ran; null while it runs")
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float = Field(0, description="what its run cost, reported else estimated; 0 for a tool span")
    model: str | None = None
    run_state: str | None = Field(
        None, description="the state of its run now (running, waiting, parked, done, failed...); null for a tool span"
    )
    content_withheld: bool = Field(
        False,
        description="its run is of a project the reader holds no grant on: reason, message and evo.* are left out",
    )


class TraceSummary(BaseModel):
    id: int
    system: str = Field(description="the slug of the system")
    revision: int = Field(description="the revision of its spec the trace ran under")
    chat_id: int | None
    message_id: int | None = Field(description="the person's message that opened it")
    opened_by: str | None = Field(description="the login of who wrote that message")
    started_at: datetime
    last_at: datetime
    runs: int = Field(description="its invoke_agent spans")
    off_design: int = Field(description="its spans that crossed an edge the spec lacks")
    errors: int
    usd: float
    work_chat_id: int | None = Field(None, description="the work chat its first hand-off opened; null before one")
    title: str | None = Field(
        None, description="what the entry agent set (work set), else the first line of its first message"
    )
    goal: str | None = Field(None, description="what the entry agent set; null when not set")
    done_when: str | None = Field(None, description="what the entry agent set; null when not set")
    status: WorkStatus | None = Field(None, description="the status of its work, derived from its runs and decisions")
    agent: str | None = Field(None, description="the handle of the agent at work now; null when none is")
    waiting: WorkWaiting | None = Field(None, description="what its work waits for; null for nothing")


class Metrics(BaseModel):
    nodes: list[dict] = Field(description="per node: count, errors, off_design, p50_seconds, last_seconds, tokens, usd")
    edges: list[dict] = Field(description="per edge (from_node, to_node): the same numbers")


class Spread(BaseModel):
    p50: float | None
    p95: float | None
    max: float | None


class EdgeRatio(BaseModel):
    from_node: str | None
    to_node: str | None
    count: int = Field(description="attempts to cross it: turns handed over it, and asks and mentions refused on it")
    off_design: int
    off_design_ratio: float = Field(description="off_design / count")


class TraceNumbers(BaseModel):
    id: int
    revision: int
    started_at: datetime
    last_at: datetime
    open: bool = Field(description="a turn of it is still running")
    usd: float = Field(
        description="the final cost of its runs and the runs their tree spawned, as the budget counts it"
    )
    runs: int = Field(description="its runs and the runs their tree spawned, as the budget counts them")
    max_hop: int | None = Field(description="the deepest hop of its runs; null when none has one")
    trace_usd: float = Field(description="limits.trace_usd of the revision it ran under")
    trace_runs: int = Field(description="limits.trace_runs of the revision it ran under; 0 for no limit")
    over_usd: float | None = Field(description="usd minus trace_usd when positive, else null")
    budget_refusals: int = Field(description="asks and mentions the budget refused")
    loop_threads: int
    idle_threads: int
    rounds_threads: int
    edges: list[EdgeRatio]


class TraceSpread(BaseModel):
    traces: int
    usd: Spread
    runs: Spread
    max_hop: Spread
    over_budget: int = Field(description="traces whose usd passed trace_usd")
    max_over_usd: float | None
    budget_refusals: int
    loop_threads: int
    idle_threads: int
    rounds_threads: int
    edges: list[EdgeRatio] = Field(description="per edge over every trace: attempts, off design and their ratio")


class TraceDistribution(BaseModel):
    system: str
    days: int
    since: datetime
    withheld: int = Field(description="traces of the window the caller may not read, left out")
    truncated: bool = Field(
        description=f"the window held more than {MAX_WINDOW_TRACES} traces: the oldest are left out"
    )
    summary: TraceSpread
    traces: list[TraceNumbers] = Field(description="the newest first")

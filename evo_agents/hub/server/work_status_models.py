"""The shapes of the status of a trace's work and of its work card (``work_status`` derives them): the statuses,
outcomes and waits, and the API's models of a brief and a card. A leaf: the chat's models carry a brief and a card
without importing what reads them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, get_args

from pydantic import BaseModel, Field

WorkStatus = Literal["running", "needs_person", "failed", "done"]
STATUSES: tuple[str, ...] = get_args(WorkStatus)
Outcome = Literal["met", "not_met"]
OUTCOMES: tuple[str, ...] = get_args(Outcome)
WaitKind = Literal["run", "person", "decision"]
WAITS: tuple[str, ...] = get_args(WaitKind)
AskKind = Literal["ask", "escalate", "reassign"]


class WorkSystem(BaseModel):
    id: int | None = Field(description="null once the system is deleted")
    slug: str
    name: str | None = Field(description="null once the system is deleted")
    project: str | None = Field(default=None, description="the name of its project")


class WorkWaiting(BaseModel):
    kind: WaitKind = Field(description="run: an agent's run; person: a person drives or reviews a run; decision")
    run_id: int | None = Field(default=None, description="the run awaited, or the run a person drives")
    agent: str | None = Field(default=None, description="the handle of the agent of that run")
    decision_id: int | None = Field(default=None, description="the open decision that asks a person")
    login: str | None = Field(default=None, description="the person the decision asks")
    question: str | None = None


class WorkBrief(BaseModel):
    """What a list shows of the work of a trace."""

    trace_id: int
    system: WorkSystem
    title: str = Field(description="what the entry agent set, else the first line of the trace's first message")
    status: WorkStatus
    agent: str | None = Field(description="the handle of the agent at work now; null when none is")
    waiting: WorkWaiting | None = Field(description="what the work waits for; null for nothing")
    coordinator: str | None = Field(
        default=None, description="the handle of the system's entry agent, which coordinates the work"
    )
    outcome: Outcome | None = Field(
        default=None,
        description="what the entry agent recorded of the work against its done-when (work set --outcome); null "
        "when it recorded none, and for a not_met a member marked handled",
    )
    left: str | None = Field(
        default=None, description="what the entry agent named as still to do (work set --left); null for nothing"
    )


class WorkThread(BaseModel):
    chat_id: int
    root_id: int


class WorkAsk(BaseModel):
    kind: AskKind = Field(description="ask: an agent's ask; escalate: to the Orchestrator; reassign: its reassignment")
    from_agent: str = Field(description="the handle of the agent that handed the work")
    agent: str | None = Field(description="the handle of the agent that took it; null when refused")
    brief: str | None = Field(description="the goal of its brief")
    status: str = Field(description="the state of the run it made; else its span's status (refused, error)")
    run_id: int | None
    usd: float
    thread: WorkThread | None = Field(description="the thread of its question")


class WorkDecision(BaseModel):
    kind: Literal["orchestrator", "person"] = Field(
        description="orchestrator: a thread of the Orchestrator; person: a decision that asks a person"
    )
    id: int = Field(description="the thread's id, or the decision's")
    rule: str | None = Field(description="the rule that opened the thread (failed, escalate, ...)")
    outcome: str | None = Field(
        description="answer, reassign or ask_owner; null while the thread is open, and on a failed or stale thread "
        "the hub closed once a later run of its agent ended done"
    )
    question: str | None
    state: str = Field(description="open or closed for a thread; the decision's state")
    answer: str | None = Field(description="the option and words of the answer")
    asked_of: str | None
    run_id: int | None = Field(description="the Orchestrator's run, or the run that asked")
    at: datetime


class WorkCard(WorkBrief):
    """The work card of a trace: its root-thread message carries it, GET /v1/chats/{id}/work answers it."""

    chat_id: int | None = Field(description="the chat of the message that opened the trace")
    message_id: int | None
    work_chat_id: int | None
    goal: str | None
    done_when: str | None
    asks: list[WorkAsk] = Field(description="the sub-tasks, oldest first")
    decisions: list[WorkDecision] = Field(description="oldest first")
    runs: int
    usd: float
    hop: int | None = Field(description="the deepest hop of its runs")
    max_hops: int | None = Field(None, description="the most hops of a trace; null: hops have no limit")
    trace_runs: int = Field(description="the runs a trace of its revision may make; 0 for no limit")
    trace_usd: float = Field(description="the USD a trace of its revision may spend")
    started_at: datetime
    ended_at: datetime | None = Field(description="its last span, once no run of it is active; null while running")
    seconds: float = Field(description="from its start to its end, or to now")
    handled_by: str | None = Field(
        default=None, description="the login of the member who marked the work handled; null until one did"
    )
    handled_at: datetime | None = Field(default=None, description="when the work was marked handled")

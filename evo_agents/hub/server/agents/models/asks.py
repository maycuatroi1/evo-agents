"""The limits of the answer of an agent asked, and the shapes of an ask, its answer and a forward
(``evo_agents.hub.server.agents.service.asks`` says the rules)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import chats, runs, system_context
from evo_agents.hub.server.chats.models.agents import Handle

ANSWER_IN_THREAD = 2048  # bytes of the answer the asking run's thread shows; the agent gets it whole
THREAD_CUT_MARK = "\n[cut by the hub: {omitted} of {total} bytes left out; message #{message} has the whole answer]"
LAST_REPLY_SCAN = 200  # the last messages of a run last_reply reads to step over the lines of its pushes, one a repo
CHAIN_MOST = 50  # the runs of a session last_reply goes back through


class AgentAskIn(BaseModel):
    handle: Handle = Field(description="the agent asked, of another project, @ left out")
    body: str = Field(min_length=1, description=f"what it is asked, at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    goal: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what the agent asked should reach"
    )
    output: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what its answer should hold"
    )
    bounds: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what it should not do or touch"
    )
    done_when: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: when its work is finished"
    )
    refs: list[str] = Field(
        default_factory=list,
        max_length=system_context.MAX_REFS,
        description="the brief: refs it starts from (repo:path, a run, an output, a URL)",
    )
    reason: str | None = Field(
        None,
        max_length=system_context.REASON_CHARS,
        description="why an agent of a system asks an agent off its interacts edges; without it such an ask is refused",
    )
    priority: runs.Priority = Field(
        runs.DEFAULT_PRIORITY,
        description="the priority of the run it asks for: a worker claims its queued runs by it, urgent first",
    )

    def brief(self) -> dict:
        return system_context.clean_brief(self.model_dump(include={*system_context.BRIEF_FIELDS, "refs"}))

    @field_validator("body")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a question has some text")
        if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a question is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return value


class AgentAsked(BaseModel):
    run_id: int | None = Field(description="the run of the agent asked; null when the hub refused")
    decision_id: int | None = Field(description="the wait of the asking run its answer wakes; null when refused")
    refusal: str | None = Field(description="why the hub handed no work, as the thread says too")


@dataclass(frozen=True)
class Answer:
    """What the asking run gets (``agent``), the message that wakes its agent, and what its thread shows
    (``thread``), with the message of the agent asked that it repeats (``repeats``), its answer; None for none."""

    agent: str
    thread: str
    repeats: int | None = None


class AgentForwardIn(BaseModel):
    run_id: int = Field(gt=0, description="the run whose last answer is forwarded: a run of the same trace")


class AgentForwarded(BaseModel):
    message_id: int | None = Field(description="the message forwarded into the run's thread; null when refused")
    refusal: str | None = Field(description="why the hub forwarded nothing, as the thread says too")

"""The bodies of the Orchestrator's routes: the three actions of an Orchestrator in its thread (``service.actions``)
and the escalation of an agent (``service.threads``)."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import chats, profiles, runs, system_context
from evo_agents.hub.server.decisions.models import OptionIn

REASON_CHARS = 600
Reason = Annotated[str, Field(min_length=1, max_length=REASON_CHARS, description="why, for the Decision: line")]
Text = Annotated[str, Field(min_length=1, max_length=chats.MAX_MESSAGE_BYTES)]


class OrchestratorAnswerIn(BaseModel):
    text: Text = Field(description="the answer: it wakes the asking run, or goes to the agent of the thread")
    reason: Reason


class OrchestratorReassignIn(BaseModel):
    handle: str = Field(
        min_length=1, max_length=100, pattern=profiles.HANDLE_PATTERN, description="an agent node of the system"
    )
    note: Text = Field(description="the work as that agent should read it: it sees nothing of the thread")
    reason: Reason


class OrchestratorAskOwnerIn(BaseModel):
    question: str = Field(min_length=1, max_length=runs.MAX_QUESTION_CHARS)
    context: str | None = Field(None, min_length=1, max_length=runs.MAX_DECISION_CONTEXT_BYTES)
    options: list[OptionIn] = Field(min_length=runs.DECISION_OPTIONS[0], max_length=runs.DECISION_OPTIONS[1])
    reason: Reason


class OrchestratorActed(BaseModel):
    message_id: int = Field(description="the Decision: line the hub posted in the thread, in the Orchestrator's name")
    run_id: int | None = Field(description="the run the answer woke or the work went to; null for ask-owner")
    decision_id: int | None = Field(description="the decision asked of the owner; null for answer and reassign")


class EscalateIn(BaseModel):
    question: str = Field(
        min_length=1, description=f"what the agent asks its Orchestrator, at most {chats.MAX_MESSAGE_BYTES} bytes"
    )
    goal: str | None = Field(None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: the goal")
    output: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what the answer should hold"
    )
    bounds: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: what not to do or touch"
    )
    done_when: str | None = Field(
        None, max_length=system_context.BRIEF_FIELD_CHARS, description="the brief: when the work is finished"
    )
    refs: list[str] = Field(
        default_factory=list, max_length=system_context.MAX_REFS, description="the brief: refs it starts from"
    )

    def brief(self) -> dict:
        return system_context.clean_brief(self.model_dump(include={*system_context.BRIEF_FIELDS, "refs"}))

    @field_validator("question")
    @classmethod
    def _bounded(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a question has some text")
        if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
            raise ValueError(f"a question is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
        return value


class Escalated(BaseModel):
    run_id: int | None = Field(description="the Orchestrator's run; null when the hub refused")
    decision_id: int | None = Field(description="the wait of the asking run its answer wakes; null when refused")
    thread_id: int | None = Field(description="the thread in the ops chat of the system; null when refused")
    refusal: str | None = Field(description="why the hub handed no work, as the thread says too")

"""The shapes of the answer jobs of a project (``evo_agents.hub.server.kg.service.answers``) and their limits."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import kg_answers as model
from evo_agents.hub.server.errors import ErrorBody

MAX_LISTED = 50
MAX_LINES = 1000  # lines of one page of GET .../lines
MAX_REPLIES = 1000  # lines of one reply

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


class KgAnswerAsk(BaseModel):
    questions: list[dict] = Field(
        min_length=1,
        max_length=model.MAX_QUESTIONS,
        description="the questions in the file's order: each {id, question}, the id a string or an integer",
    )
    batch: int = Field(model.DEFAULT_BATCH, ge=1, le=model.MAX_BATCH, description="questions asked of one agent run")


class KgAnswerJob(BaseModel):
    id: int
    project: str
    state: Literal["running", "done", "failed"]
    questions: int
    answered: int = Field(description="questions with an answer")
    errors: int = Field(description="questions with an error: from the agent, or from a run that ended without one")
    pending: int = Field(description="questions no run asked yet")
    asking: int = Field(description="questions of the run active now, not yet answered")
    batch: int = Field(description="questions asked of one agent run")
    runs: int = Field(description="agent runs queued for the job")
    run_id: int | None = Field(description="the run asking the current batch")
    note: str | None = Field(description="why the next batch waits: no node of the pool fits, another review run")
    error: str | None = Field(description="why the job failed")
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    again: bool = Field(default=False, description="POST only: the same file was sent before, and this is its job")


class KgAnswerJobs(BaseModel):
    jobs: list[KgAnswerJob]


class KgAnswerLine(BaseModel):
    id: str
    question: str
    answer: str | None
    evidence: list[str] = Field(description="the node ids or repo:path names the agent read the answer from")
    model: str | None = Field(description="the model of the run that asked the question")
    input_tokens: int = Field(description="its share of the run's input tokens, cache reads and writes included")
    output_tokens: int = Field(description="its share of the run's output tokens")
    error: str | None = Field(description="why there is no answer: from the agent, or the run that ended without one")


class KgAnswerLines(BaseModel):
    lines: list[KgAnswerLine]
    total: int = Field(description="questions of the job")
    offset: int


class KgAnswerReply(BaseModel):
    answers: list[dict] = Field(
        min_length=1,
        max_length=MAX_REPLIES,
        description='each {"id", "answer", "evidence"} or {"id", "error"}',
    )


class KgAnswerReplied(BaseModel):
    kept: int = Field(description="answers and errors kept")
    problems: list[str] = Field(description="the lines not kept and why, by their position in the reply")

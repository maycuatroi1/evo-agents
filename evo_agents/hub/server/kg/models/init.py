"""The shapes and limits of a project's kg_init runs (``evo_agents.hub.server.kg_init`` says what they do), and the
callables the job is handed for what it reads of the graph."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

MAX_TUNE = 300  # starter questions asked
QUEUE_WAIT = timedelta(hours=24)  # a run that no node could take in this long fails
MAX_LISTED = 10
KG_INIT = "kg.init"  # the audit action of an asked-for run
WITHHELD = "(text withheld)"

Measure = Callable[[AsyncEngine, object, object, "Path | None"], Awaitable[dict]]
Phase = Callable[..., Awaitable[tuple]]  # kg_questions.phase: (whether the run waits for its questions run, its body)


class InitFailed(Exception):
    pass


class InitRun(BaseModel):
    id: int
    project: str
    via: Literal["first_build", "profile", "manual"]
    state: Literal["queued", "running", "done", "failed"]
    why: list[str] = Field(description="what asked for the run")
    build_id: int | None
    agent_run_id: int | None = Field(description="the review run (lens kg_init) of the agent that decides")
    questions: dict | None = Field(
        default=None,
        description="the run that wrote the starter questions first (lens kg_questions): state, why, agent_run_id, "
        "what the agents' sink reads, the target, the eval set version it made; null before the run looked",
    )
    summary: str
    decisions: dict = Field(description="what the Curator decided and what was applied; empty until it decided")
    notes: list[str]
    report: str | None = Field(description="the report, markdown; null until the run ends")
    error: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None


class InitRuns(BaseModel):
    active: InitRun | None
    runs: list[InitRun]


class InitAll(BaseModel):
    started: list[str] = Field(description="the projects a run was queued for")
    skipped: dict[str, str] = Field(description="the projects left out, with why")


class DecisionBody(BaseModel):
    decision: dict = Field(description="the decision: strategy, settings, sources, code_changes and reasons")


class DecisionOut(BaseModel):
    run: int
    applied: dict = Field(description="what each part of the decision did")

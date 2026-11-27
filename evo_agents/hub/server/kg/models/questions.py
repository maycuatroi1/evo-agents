"""The shapes of the starter questions a kg_init run's questions run sends (``evo_agents.hub.server.kg.service.questions``)
and the callables its phase is handed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncEngine

MAX_QUESTIONS = 2000

Count = Callable[[AsyncEngine, object, object, "Path | None"], Awaitable[dict]]
Queue = Callable[..., Awaitable["int | None"]]


class QuestionsBody(BaseModel):
    questions: list[dict] = Field(
        min_length=1,
        max_length=MAX_QUESTIONS,
        description="the questions: each {question, files and/or nodes}, an id optional",
    )


class QuestionsOut(BaseModel):
    run: int = Field(description="the kg_init run")
    set: str
    version: int = Field(description="the version of the project's eval set the questions are")
    questions: int
    holdout: int = Field(description="the questions in the holdout part, which the kg_init run does not see")
    classes: dict[str, int] = Field(description="questions by the class of data their answers are of")

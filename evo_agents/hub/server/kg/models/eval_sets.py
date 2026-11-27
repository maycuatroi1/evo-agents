"""The shapes and limits of a project's retrieval question set (``evo_agents.hub.server.kg_eval_sets``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

MAX_QUESTIONS = 5000
MAX_NAME_CHARS = 100


class EvalSetBody(BaseModel):
    set: str = Field(min_length=1, max_length=MAX_NAME_CHARS, description="the set's name, e.g. evo-agents")
    questions: list[dict] = Field(
        min_length=1,
        max_length=MAX_QUESTIONS,
        description="the questions, as the lines of a set file: id, question, type, project, repo, files, nodes",
    )


class EvalSetPut(BaseModel):
    set: str
    version: int
    digest: str = Field(description="SHA-256 of what the questions ask and answer")
    questions: int
    holdout: int = Field(description="the questions in the holdout part")
    created: bool = Field(description="false when the set was the latest version already")


class EvalSetView(BaseModel):
    set: str
    version: int
    digest: str
    created_at: datetime
    split: Literal["all", "tune", "holdout"]
    answers: bool = Field(description="whether the tune questions carry their answers; holdout ones never do")
    questions: list[dict] = Field(description="the questions of the part asked, in the set's order, each with split")

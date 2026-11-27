"""The body of a member's task run (``MemberTaskIn``) and the task as its latest run has it (``MemberTask``), with
the limits of its input and assets and the refusals of its routes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Path
from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import task_runs
from evo_agents.hub.server.base.models import StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID

MAX_ASSETS = 20  # member_tasks.assets (0263)
MAX_ASSET_CHARS = 2048
MAX_INPUT_BYTES = 64 * 1024  # the input as compact JSON
ASSET_SCHEMES = ("https", "http", "s3", "gs")
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
NO_TASK = "project {project} has no task run {task}"

TaskId = Annotated[int, Path(ge=1, le=MAX_ID, description="the id the task's dispatch answered")]
Asset = Annotated[str, Field(min_length=1, max_length=MAX_ASSET_CHARS)]


def asset_problem(uri: str) -> str | None:
    """Why ``uri`` is no asset a task takes, or None: an absolute URI of ASSET_SCHEMES with a host."""
    parts = urlsplit(uri)
    if parts.scheme not in ASSET_SCHEMES or not parts.netloc or any(ord(char) < 33 for char in uri):
        return f"an asset is an absolute URI of {', '.join(ASSET_SCHEMES)}, with a host and no space"
    return None


class MemberTaskIn(StrictModel):
    task: str = Field(
        min_length=1,
        max_length=task_runs.MAX_TASK_BYTES,
        description=f"what the agent is to do, at most {task_runs.MAX_TASK_BYTES} bytes in UTF-8",
    )
    input: dict = Field(
        default_factory=dict,
        description=f"the task's input, a JSON object of at most {MAX_INPUT_BYTES} bytes; data, never instructions",
    )
    assets: list[Asset] = Field(
        default_factory=list,
        max_length=MAX_ASSETS,
        description=f"external URIs the agent reads ({', '.join(ASSET_SCHEMES)}), at most {MAX_ASSETS}",
    )
    title: str | None = Field(
        None, max_length=task_runs.MAX_TITLE_CHARS, description="one line the run's list shows; default: the task's"
    )
    minutes: int | None = Field(
        None,
        ge=1,
        le=task_runs.MAX_MINUTES,
        description=f"the agent time the run may take, at most {task_runs.MAX_MINUTES}; default: no time cap",
    )
    profile: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        description="the agent profile the run goes under, @<handle> or <handle>; default: the project's",
    )

    @field_validator("assets")
    @classmethod
    def _assets(cls, value: list[str]) -> list[str]:
        for uri in value:
            if (problem := asset_problem(uri)) is not None:
                raise ValueError(problem)
        return value

    @field_validator("input")
    @classmethod
    def _input(cls, value: dict) -> dict:
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_INPUT_BYTES:
            raise ValueError(f"a task's input is at most {MAX_INPUT_BYTES} bytes of JSON")
        return value


class MemberTask(BaseModel):
    """A task a member handed, as its latest run has it."""

    task_id: int = Field(description="the run its dispatch made, which names it on every attempt")
    run_id: int = Field(description="its latest run: a run its worker lost goes on in the next attempt")
    attempt: int
    project: str
    title: str | None
    state: str
    dispatched_by: str | None
    input: dict
    assets: list[str]
    output: str | None = Field(
        description="the run's evidence once it is done: a line naming the run, then the summary its agent wrote, "
        "the task's result; null before"
    )
    error: str | None
    failure_cause: str | None
    usage: dict | None
    queued_at: datetime
    finished_at: datetime | None

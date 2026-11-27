"""The shapes of what a plugin reads of the task runs of its own agent types (``routes.runs``)."""

from __future__ import annotations

from urllib.parse import quote

from pydantic import BaseModel, Field

from evo_agents.hub.server.outputs.models import RunOutput
from evo_agents.hub.server.task_dispatch import Task


class TaskRunSummary(Task):
    """A task run of an agent type of the plugin, with what the hub kept of its end."""

    summary: str | None = Field(
        description="the evidence of the run once it ended done or in review: the worker, the commits and their "
        "diffstat, each verify command with its exit code, then the agent's summary; null before"
    )
    error: str | None = Field(description="why it failed, as its worker reported it; null otherwise")


class TaskRunOutputs(BaseModel):
    run_id: int
    outputs: list[RunOutput] = Field(
        description="oldest first, those the filters keep; url is where the plugin reads them"
    )
    count: int = Field(description="every output the run holds")
    bytes: int = Field(description="their bytes")


def content_url(project: str, run_id: int, output_id: int) -> str:
    """Where the plugin API answers the bytes of output ``output_id`` of run ``run_id``."""
    return f"/v1/plugin-api/projects/{quote(project, safe='')}/runs/{run_id}/outputs/{output_id}/content"


__all__ = ["TaskRunOutputs", "TaskRunSummary", "content_url"]

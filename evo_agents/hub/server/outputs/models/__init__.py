"""The shapes of the outputs of runs (``evo_agents.hub.outputs``): the ``RunOutput`` every answer carries and the URL
its reader fetches the bytes from, what a machine sends, the settings and limits a project and a run answer, and the
parameters and answers of the routes of the bytes. The notification settings of a member (the notices they turned
off, tiers, quiet hours) are in ``notices``, apart: they take the kinds of ``push_tiers``, which reaches the rest of
the server, and what imports ``RunOutput`` needs none of that.

An output's ``url`` is a path of this hub that answers its bytes to whoever may read what listed it: the run's
readers for the outputs of a run (GET /v1/projects/{p}/runs/{id}/outputs/{oid}/content), the decision's readers for
the outputs a question carries (GET /v1/projects/{p}/decisions/{id}/outputs/{oid}/content), and the chat's members
who read the message for those a chat message carries (GET /v1/chats/{id}/outputs/{oid}/content). ``?download=true``
asks for it as an attachment (``service.content``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import Header, Path, Query
from pydantic import UUID4, BaseModel, Field, field_validator, model_validator

from evo_agents.hub import outputs
from evo_agents.hub.server.base.models import Sha256, StrictModel
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plans import models as plan_models
from evo_agents.hub.server.runs.models import MAX_ID

# The parameters and answers of the routes

OutputId = Annotated[int, Path(ge=1, le=MAX_ID)]
DecisionId = Annotated[int, Path(ge=1, le=MAX_ID)]
Sink = Annotated[str | None, Header(alias=plan_models.SINK_HEADER)]
Download = Annotated[bool, Query(description="answer it as an attachment, which a browser saves")]
RangeHeader = Annotated[str | None, Header(alias="Range", description="one byte range, bytes=first-last")]
IfNoneMatch = Annotated[str | None, Header(description="the ETag of the bytes the browser holds")]
CONTENT: dict[int | str, dict[str, Any]] = {
    200: {"content": {"application/octet-stream": {}}, "description": "the output's bytes, in its own media type"},
    206: {"content": {"application/octet-stream": {}}, "description": "the byte range asked for"},
    304: {"description": "the bytes the browser holds, named by If-None-Match, are the output's"},
    416: {"description": "the range asked for lies outside the output"},
    **{code: {"model": ErrorBody} for code in (403, 404, 503)},
}
LINK_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 503)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}
SEND_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}


# An output as it reads


class RunOutput(BaseModel):
    id: int
    run_id: int
    source: Literal[outputs.SOURCES] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="sent: a file the agent sent; tool: an image taken out of a tool's result; outputs: a file of "
        "the run's .evo-run/outputs/"
    )
    name: str = Field(description="the file's name, or its path below .evo-run/outputs/")
    media_type: str = Field(description="type/subtype, as the machine that sent it named it")
    viewer: Literal[outputs.VIEWERS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="what the web opens it with; none offers a download only"
    )
    size: int = Field(description="bytes")
    sha256: str
    caption: str | None
    tool_call_id: str | None = Field(description="the tool call of the run's trace it came from, when one")
    created_at: datetime
    interactive: bool = Field(
        False,
        description="an interactive answer its agent marked: HTML the web shows inside the chat message that carries it",
    )
    url: str = Field(
        description="where this hub answers its bytes to the reader of what listed it; ?download=true for a download"
    )


def run_url(project: str, run_id: int, output_id: int) -> str:
    return f"/v1/projects/{quote(project, safe='')}/runs/{run_id}/outputs/{output_id}/content"


def decision_url(project: str, decision_id: int, output_id: int) -> str:
    return f"/v1/projects/{quote(project, safe='')}/decisions/{decision_id}/outputs/{output_id}/content"


def chat_url(chat_id: int, output_id: int) -> str:
    return f"/v1/chats/{chat_id}/outputs/{output_id}/content"


def view(row, url: str) -> RunOutput:
    """The ``RunOutput`` of ``row`` (``repository.views.columns``), its bytes at ``url``."""
    fields = {name: getattr(row, name) for name in RunOutput.model_fields if name not in ("url", "viewer")}
    return RunOutput(**fields, viewer=outputs.viewer(row.media_type), url=url)


class OutputLink(BaseModel):
    sha256: str
    size: int
    name: str = Field(description="the output's own name, which a download keeps")
    url: str = Field(description="a presigned GET of the bytes, working until expires_at")
    expires_at: datetime


# What a machine sends


class OutputUploadItem(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description=f"bytes, at most {outputs.MAX_FILE_BYTES}")
    source: Literal[outputs.SOURCES]  # pyright: ignore[reportInvalidTypeForm]


class OutputUploadRequest(BaseModel):
    items: list[OutputUploadItem] = Field(min_length=1, max_length=outputs.MAX_BATCH)


class OutputIn(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description=f"bytes, at most {outputs.MAX_FILE_BYTES}")
    source: Literal[outputs.SOURCES]  # pyright: ignore[reportInvalidTypeForm]
    name: str = Field(min_length=1, max_length=outputs.MAX_NAME_CHARS, description="the file's name, or its path")
    media_type: str = Field(min_length=3, max_length=255, description="type/subtype, no parameters")
    caption: str | None = Field(None, max_length=outputs.MAX_CAPTION_CHARS)
    tool_call_id: str | None = Field(
        None, max_length=outputs.MAX_TOOL_CALL_CHARS, description="the tool call of the run's trace it came from"
    )
    upload_id: UUID4 | None = Field(
        None, description="the upload of its bytes (POST .../output-uploads); null when the project holds them already"
    )
    interactive: bool = Field(
        False,
        description=f"an interactive answer: an HTML file of at most {outputs.MAX_INTERACTIVE_BYTES} bytes, which the "
        "web shows inside the chat message that carries it; a file sent again marked keeps the mark",
    )

    _name = field_validator("name")(outputs.check_name)
    _media_type = field_validator("media_type")(outputs.check_media_type)
    _caption = field_validator("caption")(outputs.check_caption)

    @model_validator(mode="after")
    def _interactive(self):
        if self.interactive:
            outputs.check_interactive(self.media_type, self.size)
        return self

    @field_validator("tool_call_id")
    @classmethod
    def _tool_call(cls, value: str | None) -> str | None:
        return outputs.check_line(value, outputs.MAX_TOOL_CALL_CHARS, "a tool call id")


class OutputBatch(BaseModel):
    outputs: list[OutputIn] = Field(min_length=1, max_length=outputs.MAX_BATCH)


class RecordedOutputs(BaseModel):
    outputs: list[RunOutput] = Field(description="in the order of the request; a file the run held is the one it is")
    added: int = Field(description="outputs new to the run")


@dataclass(frozen=True)
class Target:
    """The run outputs are recorded on, its row locked, as the worker that claimed it or the member whose local run it
    is reaches it."""

    run_id: int
    project: str
    project_id: int
    kind: str
    state: str


# What readers list, and the settings


class RunOutputs(BaseModel):
    run_id: int
    outputs: list[RunOutput] = Field(description="oldest first, those the filters keep")
    count: int = Field(description="every output the run holds")
    bytes: int = Field(description="their bytes")
    tool_images: bool = Field(description="the project takes images out of tools' results")


class OutputSettings(BaseModel):
    project: str
    tool_images: bool = Field(description="the project takes images out of tools' results (outputs of source tool)")
    max_file_bytes: int = Field(description="the most bytes one output has")
    max_outputs: int = Field(description="the most outputs a run holds")
    max_run_bytes: int = Field(description="the most bytes of outputs a run holds, a SHA-256 counted once")


class RunOutputSettings(OutputSettings):
    run_id: int
    outputs: int = Field(description="outputs the run holds")
    bytes: int = Field(description="their bytes")


class OutputSettingsIn(StrictModel):
    tool_images: bool = Field(description="take images out of tools' results: false refuses outputs of source tool")


def settings(project: str, on: bool) -> OutputSettings:
    """The settings of ``project``, its switch of tool images ``on``, and the limits."""
    return OutputSettings(
        project=project,
        tool_images=on,
        max_file_bytes=outputs.MAX_FILE_BYTES,
        max_outputs=outputs.MAX_RUN_OUTPUTS,
        max_run_bytes=outputs.MAX_RUN_BYTES,
    )

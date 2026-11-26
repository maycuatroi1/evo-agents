"""The parameters and refusals of the routes of attachments; an upload is ``Attachment`` of the run API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Path, Query

from evo_agents.hub import attachments
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID

AttachmentId = Annotated[int, Path(ge=1, le=MAX_ID)]
FileName = Annotated[str, Query(min_length=1, max_length=attachments.MAX_NAME_CHARS, description="the file's name")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 413, 422, 503)}
DROP_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409)}
BODY = {
    "requestBody": {
        "required": True,
        "description": f"the file's bytes, at most {attachments.MAX_FILE_BYTES}; Content-Type is its media type",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}

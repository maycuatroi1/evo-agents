"""The bodies of the blob routes, the holder of a blob, the refusals and the limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from pydantic import UUID4, BaseModel, Field

from evo_agents.hub.blobs import UPLOAD_KINDS
from evo_agents.hub.server.base.models import PROJECT_NAME, Sha256

MAX_ITEMS = 1000
UNAVAILABLE = "the blob store did not answer; nothing was committed and the uploads are kept, try again shortly"
GLOBAL = "(global)"  # the hub as the holder of blobs in logs and audit rows; no project name has parentheses
GLOBAL_KINDS = frozenset({"skill-bundle", "plugin-bundle"})  # what the hub holds outside any project: skills, plugins

ProjectField = Annotated[str, Field(pattern=PROJECT_NAME)]


class UploadItem(BaseModel):
    sha256: Sha256
    size: int = Field(ge=0, description="bytes, at most the limit of the kind")
    kind: Literal[UPLOAD_KINDS]  # pyright: ignore[reportInvalidTypeForm]


class UploadRequest(BaseModel):
    project: ProjectField | None = Field(
        None, description="left out for blobs the hub holds itself (bundles of global skills; a hub admin only)"
    )
    items: list[UploadItem] = Field(min_length=1, max_length=MAX_ITEMS)


class UploadTicket(BaseModel):
    sha256: str
    upload_id: str
    url: str = Field(description="presigned PUT: send exactly `size` bytes as application/octet-stream")


class Uploads(BaseModel):
    uploads: list[UploadTicket]
    present: list[str] = Field(description="blobs the project holds already, with nothing to upload")
    expires_at: datetime = Field(description="when the URLs stop working")


class CommitRequest(BaseModel):
    project: ProjectField | None = Field(None, description="left out for blobs the hub holds itself")
    upload_ids: list[UUID4] = Field(min_length=1, max_length=MAX_ITEMS)


class Blob(BaseModel):
    sha256: str
    size: int
    kind: str


class Committed(BaseModel):
    blobs: list[Blob]
    added: int = Field(description="blobs new to the project; it held the others already")


@dataclass(frozen=True)
class Holder:
    """What holds a blob: a project, or the hub itself (``project_id`` None) for the bundles of global skills."""

    project_id: int | None
    name: str  # the project's, or GLOBAL


@dataclass(frozen=True)
class OverLimit:
    """The 413 of an upload request with an item over the size limit of its kind: its message and detail."""

    message: str
    detail: list[dict]


class Mismatch(Exception):
    """Uploads whose bytes are not what was declared; their objects and rows are gone already."""

    def __init__(self, problems: list[dict]):
        super().__init__(f"{len(problems)} upload(s) do not match what was declared")
        self.problems = problems

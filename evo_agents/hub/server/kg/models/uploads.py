"""The shapes and limits of the archives a writer uploads as sources of a project's graph
(``evo_agents.hub.server.kg_uploads``): what starting an upload asks and answers, the presigned parts, and an upload
as ``hub kg uploads`` lists it."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar")  # the longer first: a name ending in .tar.gz is not a .gz
MAX_PART_URLS = 100  # presigned URLs one call hands out
MAX_LISTED = 200
SOURCE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", description="hex SHA-256 of the whole archive")]
SourceName = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f/]+$")]


class KgUploadAsk(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"^[^\x00-\x1f\x7f/\\]+$", description="the file's name")
    size: int = Field(ge=1, description="bytes")
    sha256: Sha256
    source: SourceName | None = Field(None, description="the source the archive becomes; from its name without one")
    label: str | None = Field(
        None, min_length=1, max_length=100, description="a level of the project; its default label without one"
    )
    new_project: bool = Field(
        False,
        description="create the project, with no harness, before the upload (``kg_upload_project``); 409 if it exists",
    )


class KgUploadStarted(BaseModel):
    id: int
    source: str
    state: str
    size: int
    part_size: int
    parts: int = Field(description="how many parts the archive is sent in")
    done: list[int] = Field(description="the parts the store already holds, which the client does not send again")
    resumed: bool = Field(description="an upload of the same archive was in flight and continues")
    max_size: int = Field(description="the largest archive this hub takes")


class KgPartsAsk(BaseModel):
    numbers: list[int] = Field(min_length=1, max_length=MAX_PART_URLS)


class KgPartUrl(BaseModel):
    number: int
    size: int
    url: str


class KgPartUrls(BaseModel):
    parts: list[KgPartUrl]


class KgUploadPath(BaseModel):
    path: str
    reason: str


class KgUpload(BaseModel):
    id: int
    source: str
    name: str
    state: str
    size: int
    label: dict
    sender: str | None
    created_at: datetime
    uploaded_at: datetime | None
    ended_at: datetime | None
    files: int | None = Field(description="files read from the archive; None until it is read")
    skipped: dict[str, int] = Field(description="files left out, counted by reason")
    assets: dict[str, int] = Field(default_factory=dict, description="files kept by path and size only, by reason")
    paths: list[KgUploadPath] = Field(
        default_factory=list,
        description="the first files skipped or kept as assets; only for a grant that sees the label",
    )
    replaced_by: int | None = Field(None, description="the upload of the same source that replaced this one")
    build_id: int | None
    build_status: str | None
    error: str | None


class KgUploadList(BaseModel):
    uploads: list[KgUpload]


class KgPartSent(BaseModel):
    number: int
    size: int

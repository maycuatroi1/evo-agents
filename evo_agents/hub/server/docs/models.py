"""The models layer of the feature docs: its error, the rows the repository reads and writes, and the API as Pydantic
models, what its routes take and answer. Every Pydantic model is named ``Docs...`` in the hub's OpenAPI document."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evo_agents.hub.server.projects import LabelIn
from evo_agents.hub.tables_docs import (
    IMAGE_NAME,
    IMAGE_TYPES,
    MAX_BODY_CHARS,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_NAME_CHARS,
    MAX_IMAGES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
)

SHA256 = r"^[0-9a-f]{64}$"
Format = Literal["markdown", "html"]  # tables_docs.FORMATS
SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
RACE = "another save of the document took the same version number at the same moment: nothing was written; try again"
IMAGE_BODY = {
    "requestBody": {
        "required": True,
        "description": f"the image's bytes, a PNG, JPEG, WebP or GIF image of at most {MAX_IMAGE_BYTES} bytes",
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
    }
}


class DocsError(Exception):
    """Why the hub refuses a request about a document, with the HTTP status a route answers; nothing was written."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DocumentRow:
    """A document with its latest version's number, author and time."""

    id: int
    slug: str
    title: str
    label: dict
    latest_version: int
    created_by: str
    created_at: datetime
    updated_by: str
    updated_at: datetime
    format: str = "markdown"  # its latest version's


@dataclass(frozen=True)
class ImageRef:
    """An image a version shows: the name its Markdown gives it, and the image."""

    name: str
    sha256: str
    media_type: str
    size: int


@dataclass(frozen=True)
class VersionRow:
    """A version of a document; ``body`` and ``images`` only when it was read whole (``version``)."""

    id: int
    number: int
    title: str
    summary: str
    author: str
    created_at: datetime
    format: str = "markdown"
    body: str | None = None
    images: list[ImageRef] = field(default_factory=list)


@dataclass(frozen=True)
class KgDocument:
    """A document as the project's knowledge graph reads it: its latest version whole, with its author."""

    slug: str
    title: str
    label: dict
    version: int
    author: str
    body: str
    updated_at: datetime
    format: str = "markdown"


@dataclass(frozen=True)
class Draft:
    """The next version of a document a save writes: ``content`` ({title, body, format, summary}) showing ``images``
    ({name: sha256}), by ``author_id``; a new document's stored ``label``, and the latest version whole of one that
    exists (``latest``), which the save compares it with."""

    content: dict[str, Any]
    images: dict[str, str]
    author_id: int
    label: dict | None = None
    latest: VersionRow | None = None

    @property
    def same_as_latest(self) -> bool:
        """Whether the title, body, format and images are those of the latest version."""
        if self.latest is None:
            return False
        latest, content = self.latest, self.content
        shown = {image.name: image.sha256 for image in latest.images}
        same = (latest.title, latest.body, latest.format) == (content["title"], content["body"], content["format"])
        return same and shown == self.images


@dataclass(frozen=True)
class Saved:
    document: DocumentRow
    version: int
    created: bool
    changed: bool


class DocsDocumentSummary(BaseModel):
    """A document of a project, with its latest version."""

    slug: str
    title: str = Field(description="the title of its latest version")
    latest_version: int
    label: dict = Field(description="the label the document and every version and image of it carry")
    created_by: str
    created_at: datetime
    updated_by: str = Field(description="the author of its latest version")
    updated_at: datetime
    format: Format = Field("markdown", description="the format of its latest version")


class DocsVersion(BaseModel):
    number: int
    title: str
    summary: str = Field(description="what the version changed; may be empty")
    author: str
    created_at: datetime
    format: Format = Field("markdown", description="markdown, or html for a page of its own")


class DocsImage(BaseModel):
    name: str = Field(description="the name the Markdown gives the image: ![alt](name)")
    sha256: str
    media_type: str
    size: int
    url: str = Field(description="GET of the image on this hub, for the document's readers")


class DocsVersionDetail(DocsVersion):
    body: str = Field(description="the version's Markdown, or its HTML page when the format is html")
    images: list[DocsImage] = Field(description="the images the version shows, by name; none for an HTML page")
    page_url: str | None = Field(
        None,
        description="for an HTML version, GET of the page on this hub, served under a sandbox for a frame of the web",
    )


class DocsDocument(DocsDocumentSummary):
    versions: list[DocsVersion] = Field(description="every version, the newest first")
    shown: DocsVersionDetail = Field(description="the version asked for, the latest by default")


class DocsImageRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_IMAGE_NAME_CHARS, pattern=IMAGE_NAME)
    sha256: str = Field(pattern=SHA256, description="an image of the project, uploaded before")


class DocsSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(max_length=MAX_BODY_CHARS, description="the Markdown, or the HTML page when format is html")
    format: Format = Field("markdown", description="html saves the body as a page the hub serves in a sandbox")
    summary: str = Field("", max_length=MAX_SUMMARY_CHARS, description="what this version changes")
    images: list[DocsImageRef] = Field(default_factory=list, max_length=MAX_IMAGES)
    base_version: int | None = Field(
        None,
        ge=0,
        description="409 unless the document is at this version now, 0 unless it does not exist yet; left out, the "
        "save goes on top",
    )
    label: LabelIn | None = Field(None, description="a new document's label, an admin's choice; the project's if not")

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a title is not blank")
        return value

    @field_validator("images")
    @classmethod
    def _names(cls, value: list[DocsImageRef]) -> list[DocsImageRef]:
        names = [image.name for image in value]
        twice = sorted({name for name in names if names.count(name) > 1})
        if twice:
            raise ValueError(f"each image is named once: {', '.join(twice)} is named twice")
        return value


class DocsSaved(BaseModel):
    document: DocsDocumentSummary
    version: int = Field(description="the latest version after the save")
    created: bool = Field(description="the save created the document")
    changed: bool = Field(description="false when the save changed nothing and no version was written")


class DocsUploadedImage(BaseModel):
    sha256: str
    media_type: str = Field(description=f"one of {', '.join(IMAGE_TYPES)}, by the image's first bytes")
    size: int
    held: bool = Field(description="the project held this image already")


def summary_model(row: DocumentRow) -> DocsDocumentSummary:
    return DocsDocumentSummary(
        slug=row.slug,
        title=row.title,
        latest_version=row.latest_version,
        label=row.label,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_by=row.updated_by,
        updated_at=row.updated_at,
        format=cast(Format, row.format),
    )


def version_model(row: VersionRow) -> DocsVersion:
    return DocsVersion(
        number=row.number,
        title=row.title,
        summary=row.summary,
        author=row.author,
        created_at=row.created_at,
        format=cast(Format, row.format),
    )


def image_url(project: str, slug: str, sha256: str) -> str:
    return f"/v1/x/docs/{quote(project, safe='')}/documents/{quote(slug, safe='')}/images/{sha256}"


def page_url(project: str, slug: str, number: int) -> str:
    """GET of an HTML version's page, pinned to that version."""
    return f"/v1/x/docs/{quote(project, safe='')}/documents/{quote(slug, safe='')}/page?version={number}"


def _image(project: str, slug: str, image: ImageRef) -> DocsImage:
    return DocsImage(
        name=image.name,
        sha256=image.sha256,
        media_type=image.media_type,
        size=image.size,
        url=image_url(project, slug, image.sha256),
    )


def detail_model(project: str, slug: str, row: VersionRow) -> DocsVersionDetail:
    images = [_image(project, slug, image) for image in row.images]
    page = page_url(project, slug, row.number) if row.format == "html" else None
    return DocsVersionDetail(**version_model(row).model_dump(), body=row.body or "", images=images, page_url=page)

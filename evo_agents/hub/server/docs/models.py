"""The API of the feature docs as Pydantic models: what its routes take and answer. Every model is named ``Docs...`` in
the hub's OpenAPI document."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evo_agents.hub.server.docs.store import DocumentRow, ImageRef, VersionRow
from evo_agents.hub.server.projects import LabelIn
from evo_agents.hub.tables_docs import (
    IMAGE_NAME,
    IMAGE_TYPES,
    MAX_BODY_CHARS,
    MAX_IMAGE_NAME_CHARS,
    MAX_IMAGES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
)

SHA256 = r"^[0-9a-f]{64}$"


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


class DocsVersion(BaseModel):
    number: int
    title: str
    summary: str = Field(description="what the version changed; may be empty")
    author: str
    created_at: datetime


class DocsImage(BaseModel):
    name: str = Field(description="the name the Markdown gives the image: ![alt](name)")
    sha256: str
    media_type: str
    size: int
    url: str = Field(description="GET of the image on this hub, for the document's readers")


class DocsVersionDetail(DocsVersion):
    body: str = Field(description="the version's Markdown")
    images: list[DocsImage] = Field(description="the images the version shows, by name")


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
    body: str = Field(max_length=MAX_BODY_CHARS, description="the Markdown")
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
    )


def version_model(row: VersionRow) -> DocsVersion:
    return DocsVersion(
        number=row.number, title=row.title, summary=row.summary, author=row.author, created_at=row.created_at
    )


def image_url(project: str, slug: str, sha256: str) -> str:
    return f"/v1/x/docs/{quote(project, safe='')}/documents/{quote(slug, safe='')}/images/{sha256}"


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
    return DocsVersionDetail(**version_model(row).model_dump(), body=row.body or "", images=images)

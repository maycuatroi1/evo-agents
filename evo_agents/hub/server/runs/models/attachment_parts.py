"""The shape of a file a person attached to a message (``evo_agents.hub.attachments``), as the run API, the chats and
the worker's inbox, claim and chat read show it."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from evo_agents.hub import attachments

MAX_ATTACHMENT_ID = 2**63 - 1  # bigint, as runs.models.MAX_ID
AttachmentIds = Annotated[
    list[Annotated[int, Field(ge=1, le=MAX_ATTACHMENT_ID)]],
    Field(
        max_length=attachments.MAX_PER_MESSAGE,
        description=f"files uploaded for this message (POST .../attachments), at most {attachments.MAX_PER_MESSAGE}; "
        "with one or more, the text may be empty",
    ),
]


class SentFile(BaseModel):
    """A file as a ``user_message`` event names it (``as_event``)."""

    id: int
    name: str = Field(description="the file's own name, as its uploader gave it")
    media_type: str
    size: int = Field(description="bytes")


class Attachment(SentFile):
    sha256: str


class LinkedAttachment(Attachment):
    url: str = Field(description="the hub's URL of its bytes, for those who may read the message")


def linked(files: list[Attachment], base: str) -> list[LinkedAttachment]:
    """``files``, each with the URL of its bytes under ``base``: ``/v1/chats/{id}``, or
    ``/v1/projects/{p}/runs/{id}``."""
    return [LinkedAttachment(**f.model_dump(), url=f"{base}/attachments/{f.id}/content") for f in files]


def as_event(files: list[Attachment]) -> list[dict]:
    """``files`` as a ``user_message`` event carries them: id, name, media type and size."""
    return [{"id": f.id, "name": f.name, "media_type": f.media_type, "size": f.size} for f in files]

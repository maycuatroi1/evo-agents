"""The versions of a document as the routes answer them (``DocVersion``, ``DocVersions``), the key a document's
versions share (``doc_key``) and the limits and refusals of the routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Query
from pydantic import BaseModel, Field

from evo_agents.hub import outputs
from evo_agents.hub.server.chats.models.views import ChatAuthor
from evo_agents.hub.server.errors import ErrorBody

ATTACHED = "attached"
MAX_QUERY_CHARS = 1024  # an output's name may carry its path below .evo-run/outputs/
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 422)}
DocName = Annotated[
    str,
    Query(
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description="the document's name; only its last part after a slash counts, without case",
    ),
]


class DocVersion(BaseModel):
    source: Literal[(ATTACHED, *outputs.SOURCES)] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="attached: a file a person attached to a message; sent, tool or outputs: an output of a run"
    )
    id: int = Field(description="the attachment's id, or the output's: the two are counted apart")
    name: str = Field(description="the file's name; an output's may carry its path below .evo-run/outputs/")
    media_type: str
    viewer: Literal[outputs.VIEWERS] = Field(  # pyright: ignore[reportInvalidTypeForm]
        description="what the web opens it with; none offers a download only"
    )
    size: int = Field(description="bytes")
    sha256: str
    caption: str | None = Field(description="an output's caption; null for an attachment")
    created_at: datetime = Field(description="when its message was sent, or when the run sent the output")
    url: str = Field(description="where this hub answers its bytes to the reader of this list")
    message_id: int | None = Field(description="the chat message that carries it; null for none")
    seq: int | None = Field(description="the chat event of that message; null outside a chat")
    thread_root_id: int | None = Field(description="the first message of that message's thread; null for none")
    run_id: int | None = Field(description="the run that wrote it, for an output; null for an attachment")
    author: ChatAuthor | None = Field(
        description="who sent it: the person who attached it, or the agent of the chat message that carries an "
        "output; null for an output in a run's list, which the run's agent wrote"
    )


class DocVersions(BaseModel):
    key: str = Field(description="the name the versions share: the last part after a slash, lower case")
    versions: list[DocVersion] = Field(description="oldest first; those the reader may not read left out")


def doc_key(name: str) -> str:
    """The key of the document ``name`` is a version of, as the web's ``docKey`` has it."""
    name = name.strip()
    return (name[name.rfind("/") + 1 :] or name).lower()

"""The shapes of a plugin's records over HTTP (``routes.records``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from evo_agents.hub import plugin_records
from evo_agents.hub.server.base.models import StrictModel


class RecordIn(StrictModel):
    body: dict[str, Any] = Field(description=f"a JSON object, at most {plugin_records.MAX_BODY_BYTES // 1024} KiB")
    level: str | None = Field(None, description="a level of the project's ladder; default: its default label's")
    audience: Literal["writer", "everyone"] | None = Field(
        None, description="writer: only the person who writes it sees it; default: everyone the level reaches"
    )
    revision: int | None = Field(None, ge=0, description="0: a new record; n: the record at revision n; none: either")


class RecordOut(BaseModel):
    collection: str
    key: str
    revision: int
    level: str
    audience: Literal["writer", "everyone"]
    body: dict[str, Any]
    created_by: str | None = Field(description="the login of the writer; none for a job or an event")
    created_at: datetime
    updated_by: str | None
    updated_at: datetime


class RecordPage(BaseModel):
    records: list[RecordOut]
    next_after: str | None = Field(description="the key to pass as `after` for the next page; none at the end")


__all__ = ["RecordIn", "RecordOut", "RecordPage"]

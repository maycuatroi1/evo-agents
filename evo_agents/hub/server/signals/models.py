"""The request and response models of the signals of a project, of its registry of keys and of its ingest keys, and
their limits."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Path
from pydantic import AwareDatetime, BaseModel, Field

from evo_agents.hub import signals
from evo_agents.hub.tables_signals import (
    INGEST_NAME,
    KEY_NAME,
    MAX_DESCRIPTION_CHARS,
    MAX_EXTERNAL_ID_CHARS,
    MAX_KEY_NAME_CHARS,
    MAX_LEVEL_CHARS,
    MAX_SOURCE_CHARS,
    MAX_TENANT_CHARS,
    MAX_TEXT_BYTES,
    MAX_UNIT_CHARS,
)

SINK_HEADER = "X-Evo-Sink"  # evo_agents.hub.server.plans.SINK_HEADER
PRINTABLE = r"^[^\x00-\x1f\x7f]+$"
PRINTABLE_OR_EMPTY = r"^[^\x00-\x1f\x7f]*$"  # a unit or a source of a key, which may be empty
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
TOO_LARGE = f"a batch of signals is at most {signals.MAX_BATCH_BYTES // 1024} KiB of request"
KeyName = Annotated[
    str, Path(max_length=MAX_KEY_NAME_CHARS, pattern=KEY_NAME, description="the key, as kb.search.hits")
]
IngestId = Annotated[int, Path(ge=1, le=2**63 - 1)]
Printable = Annotated[str, Field(pattern=PRINTABLE)]


# Signals


class SignalIn(BaseModel):
    key: str = Field(min_length=1, max_length=MAX_KEY_NAME_CHARS, description="a key of the project's registry")
    value: float | None = Field(default=None, allow_inf_nan=False, description="the number, for a key of kind number")
    text: str | None = Field(
        default=None,
        max_length=MAX_TEXT_BYTES,
        description=f"the feedback, for a key of kind feedback: at most {MAX_TEXT_BYTES // 1024} KiB in UTF-8",
    )
    source: Printable | None = Field(
        default=None, max_length=MAX_SOURCE_CHARS, description="who measured it; the key's source when left out"
    )
    measured_at: AwareDatetime | None = Field(default=None, description="when it was measured; now when left out")
    tenant: Printable | None = Field(
        default=None, max_length=MAX_TENANT_CHARS, description="who it is about, such as a customer; free text"
    )
    synthetic: bool = Field(default=False, description="it comes from simulated people, not real ones")
    external_id: Printable | None = Field(
        default=None,
        max_length=MAX_EXTERNAL_ID_CHARS,
        description="the sender's id of it: sent again with the same key, it is stored once",
    )
    level: Printable | None = Field(
        default=None,
        max_length=MAX_LEVEL_CHARS,
        description="its sensitivity, a level of the project's ladder; the key's, else the project's default",
    )


class SignalBatchIn(BaseModel):
    items: list[SignalIn] = Field(min_length=1, max_length=signals.MAX_ITEMS)


class SignalRejection(BaseModel):
    index: int = Field(description="the item's place in the batch, 0 first")
    key: str
    external_id: str | None
    reason: str


class SignalBatchResult(BaseModel):
    batch_id: UUID
    accepted: int = Field(description="signals stored")
    duplicates: int = Field(description="items whose external_id this key or member had sent before")
    rejected: list[SignalRejection]


class Signal(BaseModel):
    id: int
    key: str
    kind: Literal["number", "feedback"]
    value: float | None
    text: str | None
    source: str
    measured_at: datetime
    tenant: str | None
    synthetic: bool
    external_id: str | None
    label: dict
    via: Literal["ingest", "member", "run"] = Field(description="who pushed it: an ingest key, a member, a run")
    ingest_key: str | None = Field(description="the ingest key's name, when one pushed it")
    login: str | None = Field(description="the member who pushed it, or for whom a run did")
    run_id: int | None
    batch_id: UUID
    received_at: datetime


# The registry of keys


class SignalKeyIn(BaseModel):
    name: str = Field(max_length=MAX_KEY_NAME_CHARS, pattern=KEY_NAME, description="two to six dotted parts")
    kind: Literal["number", "feedback"]
    unit: str = Field(default="", max_length=MAX_UNIT_CHARS, pattern=PRINTABLE_OR_EMPTY)
    description: str = Field(default="", max_length=MAX_DESCRIPTION_CHARS)
    source: str = Field(default="", max_length=MAX_SOURCE_CHARS, pattern=PRINTABLE_OR_EMPTY)
    level: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_LEVEL_CHARS,
        description="the level its signals get when they name none; the project's default level when left out",
    )


class SignalKey(BaseModel):
    name: str
    kind: Literal["number", "feedback"]
    unit: str
    description: str
    source: str
    level: str | None
    created_by: str
    created_at: datetime
    archived_at: datetime | None


# Ingest keys


class SignalIngestKeyIn(BaseModel):
    name: str = Field(max_length=100, pattern=INGEST_NAME, description="the service it is for, unique among live keys")
    max_level: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_LEVEL_CHARS,
        description="the highest level it writes; the project's default level when left out",
    )


class SignalIngestKey(BaseModel):
    id: int
    name: str
    max_level: str
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None


class SignalIngestKeyIssued(SignalIngestKey):
    token: str = Field(description="evi_...: shown this once, the hub keeps only its SHA-256")

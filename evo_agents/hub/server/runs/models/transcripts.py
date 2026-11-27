"""The shapes and limits of session transcripts on the hub (``evo_agents.hub.server.transcripts``): a part as the
client sends it, what became of it, how far the hub holds a transcript, and the answers of the reads, the usage and
the deletions."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import UUID4, AwareDatetime, BaseModel, Field, model_validator

from evo_agents.hub import digest
from evo_agents.hub import transcripts as local
from evo_agents.hub.blobs import KIND_LIMITS

MAX_PARTS = 100  # parts in one request
MAX_LOOKUP = 1000  # session ids one held request names
DEFAULT_LIMIT = 50
MAX_LIMIT = 500
SCAN_ROWS = 20_000
TRANSCRIPT_DELETE = "transcript.delete"  # an admin deleted transcripts: "<project> session=<id> transcripts=<n>"
STATUSES = ("stored", "held", "conflict", "missing", "refused", "deleted")

FileKey = Annotated[
    str,
    Field(
        pattern=rf"^({local.FILE_KEY.pattern})$",
        max_length=local.MAX_FILE_KEY,
        description="main for the session's own transcript, subagents/... for a subagent's",
    ),
]
Machine = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,40}$", description="the machine, as its hooks name it")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SessionField = Annotated[str, Field(pattern=rf"^{digest.SESSION_ID.pattern}$")]
Runtime = Literal[local.RUNTIMES]


class PartIn(BaseModel):
    """One part of a transcript, as ``evo_agents.hub.transcripts.Part.wire`` sends it."""

    session_id: SessionField
    file: FileKey
    runtime: Runtime  # pyright: ignore[reportInvalidTypeForm]
    machine: Machine
    cwd: str | None = Field(None, max_length=digest.MAX_CWD, description="the session's directory")
    start: int = Field(ge=0, description="the byte of the file on the machine the part starts at")
    end: int = Field(gt=0, description="the byte after its last")
    first_line: int = Field(ge=1, description="the number of its first line in the file")
    lines: int = Field(ge=1, le=10**8)
    sha256: Sha256 = Field(description="the blob of kind transcript that holds its lines, gzipped")
    size: int = Field(ge=1, le=KIND_LIMITS[local.KIND], description="bytes of that blob")
    raw_size: int = Field(ge=0, le=10**10, description="bytes of its redacted lines before gzip")
    started_at: AwareDatetime | None = Field(None, description="the earliest timestamp of its lines")
    ended_at: AwareDatetime | None = Field(None, description="the latest")

    @model_validator(mode="after")
    def _ordered(self):
        if self.end <= self.start:
            raise ValueError("a part ends after it starts")
        return self


class PartsRequest(BaseModel):
    upload_ids: list[UUID4] = Field(
        default_factory=list, max_length=MAX_PARTS, description="uploads of kind transcript to commit first"
    )
    parts: list[PartIn] = Field(min_length=1, max_length=MAX_PARTS)


class PartOutcome(BaseModel):
    session_id: str
    file: str
    start: int
    end: int
    status: Literal[STATUSES]  # pyright: ignore[reportInvalidTypeForm]
    source_bytes: int = Field(description="bytes of the file on the machine the hub holds now")
    lines: int = Field(description="lines of it the hub holds now")


class PartsAnswer(BaseModel):
    parts: list[PartOutcome] = Field(description="in the order of the files and their bytes")
    stored: int = Field(description="parts this request recorded")
    bytes: int = Field(description="bytes of the blobs of those parts")


class HeldRequest(BaseModel):
    session_ids: list[SessionField] = Field(min_length=1, max_length=MAX_LOOKUP)


class HeldTranscript(BaseModel):
    session_id: str
    file: str
    runtime: str
    source_bytes: int
    lines: int
    parts: int
    deleted: bool = Field(description="its content was deleted; the hub takes no more of it")


class HeldAnswer(BaseModel):
    transcripts: list[HeldTranscript] = Field(description="the caller's transcripts of those sessions")


class TranscriptSummary(BaseModel):
    session_id: str
    file: str
    runtime: str
    login: str = Field(description="the member who uploaded it")
    machine: str
    cwd: str | None
    label: dict
    source_bytes: int = Field(description="bytes of the file on the machine the hub holds")
    lines: int
    parts: int
    size: int = Field(description="bytes of its parts in the blob store, gzipped")
    raw_size: int = Field(description="bytes of its redacted lines")
    sha256: str | None = Field(description="the chain of its parts' hashes (evo_agents.hub.transcripts.chain)")
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime = Field(description="its first part")
    updated_at: datetime = Field(description="its latest part, or its deletion")
    deleted_at: datetime | None = Field(description="when its content was deleted; null while the hub holds it")


class TranscriptList(BaseModel):
    transcripts: list[TranscriptSummary] = Field(description="the latest updated first")
    total: int = Field(description="transcripts the caller may read that match the filters")
    limit: int
    offset: int


class TranscriptPart(BaseModel):
    seq: int
    source_start: int
    source_end: int
    first_line: int
    lines: int
    sha256: str
    size: int
    raw_size: int
    created_at: datetime


class TranscriptFile(TranscriptSummary):
    part_list: list[TranscriptPart] = Field(description="its parts in order")


class SessionTranscripts(BaseModel):
    project: str
    session_id: str
    transcripts: list[TranscriptFile] = Field(description="the session's own (main) first, then its subagents'")


class MemberUsage(BaseModel):
    login: str
    sessions: int
    transcripts: int
    bytes: int


class TranscriptUsage(BaseModel):
    project: str
    sessions: int = Field(description="sessions whose transcripts the hub holds")
    transcripts: int = Field(description="their transcript files, subagents' included")
    parts: int
    bytes: int = Field(description="bytes of their parts in the blob store, gzipped")
    raw_bytes: int = Field(description="bytes of their redacted lines")
    source_bytes: int = Field(description="bytes of the files on the machines they came from")
    blob_bytes: int = Field(description="bytes of the distinct blobs of kind transcript the project holds")
    deleted: int = Field(description="transcripts whose content was deleted; their rows stay")
    first_at: datetime | None = Field(description="the earliest first part")
    last_at: datetime | None = Field(description="the latest part")
    retention_days: int = Field(description="days a transcript stays after its latest part (EVO_HUB_TRANSCRIPT_DAYS)")
    members: list[MemberUsage] = Field(description="per member who uploaded, the most bytes first")


class TranscriptsDeleted(BaseModel):
    project: str
    session_id: str | None = Field(description="the session; null when every transcript of the project went")
    transcripts: int = Field(description="transcripts whose content was deleted")
    parts: int
    bytes: int

"""The request and response models of the memories, their limits and the names the other layers share: the
namespaces a read reaches, the audit actions, the bounds of a request and of a page."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.memory import MAX_AUDIENCE
from evo_agents.hub.memory_files import MAX_BODY, SHARED_TYPES, body_problem, name_problem
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.kg.protocol import PRINCIPAL

PUT = "memory.put"  # audit actions; the target is memory:<id>
DELETE = "memory.delete"
AUDIENCE = "memory.audience"  # an admin lowered the audience
RESTORE = "memory.restore"  # a writer restored an archived memory
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SEARCH_LIMIT = 50
PAGE_BYTES = 4 * 1024 * 1024  # bodies in one page, past which it ends early
SCAN_ROWS = 5000
BATCH = 200  # rows read at a time while filtering by the read rule
MAX_REVISION = 2**31 - 1
MAX_ID = 2**63 - 1
LOCK_CLASS = 0x6D656D  # pg_advisory_xact_lock namespace serializing the writes of one memory key
DEFAULT_REVISIONS = 50
MAX_REVISIONS = 200
MAX_REQUEST = 6 * MAX_BODY + 64 * 1024  # a body escaped as JSON at worst (\u0001 for every byte), and the rest
TOO_LARGE = f"a request is at most {MAX_REQUEST // 1024} KiB, a memory at most {MAX_BODY // 1024} KiB"

SHARED = ("project", "reference")  # SHARED_TYPES, in the order the queries name them
OWNED = ("user", "feedback")

MAX_RUN_WRITES = 5  # memories one run writes
NONE, OWN, BOTH = "none", "own", "both"  # the namespaces a read reaches: no agent's, one agent's, or both
LIVE = "live"  # what a run's agent reaches naming its own namespace: its memories less those archived or promoted
AGENT_TYPES = ("project", "reference", "feedback")  # what an agent's memory may be

PROMPT_ROWS = 40  # agents' memories in a review prompt, newest first
SHARED_ROWS = 100  # shared memories in a review prompt
BODY_BYTES = 1536  # each agent's memory's body in the prompt, at most
PROMPT_SCAN_ROWS = 400  # memories read for a prompt before the read rule filters them

INDEX_BYTES = 4 * 1024  # the index of an agent's memories in a prompt, at most
INDEX_ROWS = 200  # memories read for an index, newest first

PrincipalName = Annotated[str, Field(max_length=200, pattern=PRINCIPAL)]

REFUSALS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    413: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


def printable(location: str) -> bool:
    """Whether ``location`` may be the location of a memory of a project."""
    return all(ch.isprintable() for ch in location) and "/" not in location and "\\" not in location


class MemoryState(BaseModel):
    read_at: datetime | None = Field(None, description="an agent's memory: when the agent of a run last opened it")
    archived_at: datetime | None = Field(
        None,
        description="an agent's memory: when the hub archived it, nobody having opened it for 30 days; out of the "
        "index and the searches of runs until a writer restores it",
    )
    stale_at: datetime | None = Field(
        None, description="an agent's memory: since when a path it cites (code:REPO:PATH) is gone from the graph"
    )
    promoted_at: datetime | None = Field(
        None,
        description="an agent's memory: when an accepted proposal of the Curator promoted it into a shared memory; out "
        "of the index and the searches of runs from then on, until a new revision",
    )
    promoted_into: int | None = Field(None, description="the shared memory it was promoted into, while it exists")


class Memory(MemoryState):
    id: int
    scope: str
    project: str | None
    location: str
    name: str
    type: str
    owner: str = Field(description="the login of the person who created it")
    agent: str | None = Field(None, description="the handle of the agent profile whose memory it is; else null")
    label: dict
    body: str = Field(description="the whole file; empty for a tombstone")
    revision: int
    deleted: bool
    created_at: datetime
    updated_at: datetime
    updated_by: str


class Written(Memory):
    created: bool
    changed: bool = Field(description="false when the hub already held exactly this")


class Page(BaseModel):
    items: list[Memory]
    next_cursor: str | None = Field(description="the cursor of the next page; null after the last one")


class Found(Memory):
    rank: float


class Results(BaseModel):
    items: list[Found]


class Conflict(ErrorBody):
    current: Memory | None = Field(description="the version the hub holds, when the caller may see it")


@dataclass(frozen=True)
class Row:
    memory: Memory
    owner_id: int
    project_id: int | None
    profile_id: int | None = None  # the agent's namespace, None for none


class AudienceIn(BaseModel):
    audience: list[PrincipalName] = Field(
        max_length=MAX_AUDIENCE, description="what the memory's audience becomes: a part of it, [] for none"
    )
    if_revision: int = Field(ge=1, le=MAX_REVISION, description="the revision last seen")


class Provenance(BaseModel):
    """Where a revision came from, when the agent of a run wrote it or an accepted proposal promoted agents' memories
    into it."""

    run: int | None = Field(description="the run whose agent wrote it; null for a promotion")
    proposal: int | None = Field(None, description="the Curator's proposal whose promotion wrote it")
    agent: str | None = Field(description="the handle of the profile the run ran under")
    agent_version: int | None = Field(description="the version of that profile")
    origin: str | None = Field(description="the login of the person at the run's origin, who dispatched it")
    hop: int | None = Field(description="the run's hop in its chat: 1 for a person's mention, more agent to agent")
    chat: int | None = Field(description="the chat of the run")
    chat_message: int | None = Field(description="the chat message that started the run")


class RevisionSummary(BaseModel):
    revision: int
    type: str
    label: dict
    deleted: bool = Field(description="a tombstone: the memory was deleted at this revision")
    size: int = Field(description="bytes of the body in UTF-8")
    actor: str = Field(description="the login of who wrote this revision")
    created_at: datetime
    provenance: Provenance | None = Field(None, description="the run or the promotion that wrote it; null for neither")


class Revisions(BaseModel):
    memory_id: int
    items: list[RevisionSummary] = Field(description="the latest first")
    next_before: int | None = Field(description="pass as before for older revisions; null after the oldest")


class MemoryRevision(RevisionSummary):
    memory_id: int
    body: str = Field(description="the whole file as it was; empty for a tombstone")


class PromotionTarget(BaseModel):
    """The shared memory a promotion writes: one of the project by id and current revision, or a new one."""

    memory_id: int | None = Field(None, ge=1, le=MAX_ID, description="a shared memory of the project to merge into")
    revision: int | None = Field(None, ge=1, le=MAX_REVISION, description="its current revision")
    location: str | None = Field(None, min_length=1, max_length=255, description="a new one: harness or a repo")
    name: str | None = Field(None, min_length=4, max_length=255, description="a new one: its file name, ending in .md")
    type: Literal[tuple(sorted(SHARED_TYPES))] | None = Field(  # pyright: ignore[reportInvalidTypeForm]
        None, description="a new one: project or reference"
    )

    @model_validator(mode="after")
    def _one(self):
        merges = self.memory_id is not None or self.revision is not None
        new = any(value is not None for value in (self.location, self.name, self.type))
        if merges == new:
            raise ValueError("a target is {memory_id, revision} of a shared memory, or {location, name, type}")
        if merges and (self.memory_id is None or self.revision is None):
            raise ValueError("a shared memory to merge into is named by memory_id and revision together")
        if new and None in (self.location, self.name, self.type):
            raise ValueError("a new shared memory is named by location, name and type together")
        if new and not printable(self.location or ""):
            raise ValueError("a location is printable text without a slash or a backslash")
        problem = name_problem(self.name or "") if new else None
        if problem:
            raise ValueError(problem)
        return self


class PromotionIn(BaseModel):
    """What a proposal of kind memory promotes: its sources are its evidence naming agents' memories."""

    target: PromotionTarget
    body: str = Field(min_length=1, max_length=MAX_BODY, description="the whole memory after the merge")

    @model_validator(mode="after")
    def _body(self):
        problem = body_problem(self.body)
        if problem:
            raise ValueError(problem)
        return self

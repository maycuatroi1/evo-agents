"""Memories: the files of Claude Code's auto-memory, so every machine of a person, and the members of a project, see
the same ones.

A memory is one file of a memory directory, stored whole (frontmatter and all) with its type, a label and an integer
revision. Its scope is ``project`` when the directory is the harness root (location ``harness``) or a repo (location:
the repo's name) of a hub project, else ``personal``, where the location is what is left of the directory's slug
(``evo_agents.hub.memory`` maps directories and places both ways).

Who sees a memory: in a project, project and reference memories go to the members the read rule lets through the
sink the caller names, and user and feedback memories to their owner alone, under the same rule; personal memories go
to their owner alone. A memory one cannot see answers exactly as one that does not exist. The sink a read goes through
is the one the caller names; left out, a machine token reads through the Claude Code session's (claude-code@anthropic),
since a pull writes files that sessions read, and a web session through none: the web shows members their own view,
so the label must pass ``ProjectRules.visible_by_grant`` (the grant's max level alone). The agent of a run, on the
hub's /mcp with its worker's token (``Principal.scope``), reads and writes the memories of the run's project alone,
under its owner's grant as the scope caps it (``projects.project_access``): never a personal memory of its owner,
nor one of another project.

PUT /v1/memories writes a memory by its key: scope, project, location and name, plus the owner for the types only the
owner sees. ``if_revision`` is the revision the writer last saw: none to create (a tombstone counts as nothing), the
current one to change it; anything else is a 409 that carries the current version when the caller may see it. The
same content again changes nothing and writes no audit row, whatever ``if_revision`` says. DELETE /v1/memories/{id}
leaves a tombstone: deleted, no body, the next revision. Writes into a project follow the write rule (the writer role,
and a label the project's hub sink clears; the current label, else the project's default, when the request names
none); personal memories need a signed-in caller. Every change appends one row to memory_revisions and one audit row
``memory:<id>`` in the same transaction. Neither the audit trail nor the logs carry a name or any content.

GET /v1/memories lists what the caller sees in (updated_at, id) order with an opaque cursor, tombstones included on
request, which is what a sync needs. GET /v1/memories/search ranks the full-text matches of name and body
(configuration simple, websearch syntax). GET /v1/memories/{id} is one memory. GET /v1/memories/{id}/revisions lists
its revisions newest first, and GET .../revisions/{revision} is one with its body; a revision whose type and label the
caller could not read is left out, as the memory itself would be.

Sizes are bounded: a body is at most MAX_BODY bytes of UTF-8, a request at most MAX_REQUEST bytes (read before
parsing), a page at most MAX_LIMIT memories and about PAGE_BYTES of bodies, and one request examines at most
SCAN_ROWS rows the caller cannot see.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from evo_agents.hub.access import has_role
from evo_agents.hub.db import legacy
from evo_agents.hub.memory import (
    AGENT_SINK,
    HARNESS,
    MAX_BODY,
    PERSONAL_LOCATION,
    SHARED_TYPES,
    TYPES,
    body_problem,
    name_problem,
)
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.audit import record
from evo_agents.hub.server.errors import CODES, ErrorBody
from evo_agents.hub.server.projects import PRINTABLE, LabelIn, ProjectAccess, project_access
from evo_agents.hub.server.security import WEB, CurrentUser, Principal

log = logging.getLogger(__name__)

PUT = "memory.put"  # audit actions; the target is memory:<id>
DELETE = "memory.delete"
MAX_REQUEST = 6 * MAX_BODY + 64 * 1024  # a body escaped as JSON at worst (\u0001 for every byte), and the rest
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
SEARCH_LIMIT = 50
PAGE_BYTES = 4 * 1024 * 1024  # bodies in one page, past which it ends early
SCAN_ROWS = 5000
BATCH = 200  # rows read at a time while filtering by the read rule
MAX_REVISION = 2**31 - 1
MAX_ID = 2**63 - 1
LOCK_CLASS = 0x6D656D  # pg_advisory_xact_lock namespace serializing the writes of one memory key
TOO_LARGE = f"a request is at most {MAX_REQUEST // 1024} KiB, a memory at most {MAX_BODY // 1024} KiB"

Sink = Annotated[
    str,
    Query(min_length=1, max_length=100, pattern=PRINTABLE, description="the sink reading the memories (read rule)"),
]
ReadSink = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=100,
        pattern=PRINTABLE,
        description=(
            "the sink reading the memories (read rule); left out, claude-code@anthropic for a machine token and none "
            "for a web session, which reads by its grant alone"
        ),
    ),
]
REFUSALS = {
    403: {"model": ErrorBody},
    404: {"model": ErrorBody},
    413: {"model": ErrorBody},
    422: {"model": ErrorBody},
}


class BoundedRoute(APIRoute):
    """Reads at most MAX_REQUEST bytes of a request before FastAPI parses it: a larger one is a 413, whether it says
    its length or not, and is never held in memory whole."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


router = APIRouter(
    prefix="/v1/memories", tags=["memories"], route_class=BoundedRoute, responses={401: {"model": ErrorBody}}
)


def _printable(location: str) -> bool:
    return all(ch.isprintable() for ch in location) and "/" not in location and "\\" not in location


class MemoryIn(BaseModel):
    scope: Literal["project", "personal"]
    project: str | None = Field(None, pattern=PROJECT_NAME, description="the hub project; scope project only")
    location: str = Field(
        min_length=1,
        max_length=255,
        description="harness or a repo of the project; for scope personal, what is left of the directory's slug",
    )
    name: str = Field(min_length=4, max_length=255, description="the file name, ending in .md")
    type: Literal[TYPES]
    body: str = Field(max_length=MAX_BODY, description="the whole file, frontmatter included")
    label: LabelIn | None = Field(
        None, description="scope project only; the current label, else the project's default, when left out"
    )
    if_revision: int | None = Field(
        None, ge=1, le=MAX_REVISION, description="the revision last seen; left out to create the memory"
    )

    @model_validator(mode="after")
    def _check(self):
        problems = [p for p in (name_problem(self.name), body_problem(self.body)) if p]
        if self.scope == "project":
            if self.project is None:
                problems.append("a project memory names its project")
            if not _printable(self.location):
                problems.append("a project location is printable text without a slash or a backslash")
        else:
            if self.project is not None or self.label is not None:
                problems.append("a personal memory has no project and no label")
            if not PERSONAL_LOCATION.fullmatch(self.location):
                problems.append("a personal location is [~A-Za-z0-9-] then [A-Za-z0-9-] characters")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class Memory(BaseModel):
    id: int
    scope: str
    project: str | None
    location: str
    name: str
    type: str
    owner: str = Field(description="the login of the person who created it")
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


COLUMNS = """
m.id, m.scope, p.name, m.location, m.name, m.type, o.login, m.label, m.body, m.revision, m.deleted, m.created_at,
m.updated_at, u.login, m.owner_id, m.project_id
"""
FROM = """
  FROM memories m LEFT JOIN projects p ON p.id = m.project_id
  JOIN users o ON o.id = m.owner_id JOIN users u ON u.id = m.updated_by
"""
# What may be visible before labels count: one's personal memories (not for the agent of a run) and, in the projects
# one holds a grant on, the shared memories and one's own.
CANDIDATE = """
(%(personal)s AND m.scope = 'personal' AND m.owner_id = %(user)s
 OR m.scope = 'project' AND m.project_id = ANY(%(projects)s)
    AND (m.type IN ('project', 'reference') OR m.owner_id = %(user)s))
"""
FILTERS = """
(%(scope)s::text IS NULL OR m.scope = %(scope)s)
AND (%(project_id)s::bigint IS NULL OR m.project_id = %(project_id)s)
AND (%(location)s::text IS NULL OR m.location = %(location)s)
"""
LIST = f"""
SELECT {COLUMNS} {FROM}
 WHERE {CANDIDATE} AND {FILTERS} AND (%(deleted)s OR NOT m.deleted)
   AND (m.updated_at, m.id) > (%(after_at)s::timestamptz, %(after_id)s::bigint)
 ORDER BY m.updated_at, m.id
 LIMIT %(batch)s
"""
SEARCH = f"""
SELECT {COLUMNS}, ts_rank(m.search, q) AS rank
  {FROM}, websearch_to_tsquery('simple', %(q)s) q
 WHERE m.search @@ q AND NOT m.deleted AND {CANDIDATE} AND {FILTERS}
 ORDER BY rank DESC, m.updated_at DESC, m.id DESC
 LIMIT %(batch)s OFFSET %(offset)s
"""
ONE = f"SELECT {COLUMNS} {FROM} WHERE m.id = %s"
KEY = f"""
SELECT {COLUMNS} {FROM}
 WHERE m.scope = %(scope)s AND m.project_id IS NOT DISTINCT FROM %(project_id)s AND m.location = %(location)s
   AND m.name = %(name)s AND (%(shared)s AND m.type IN ('project', 'reference')
                              OR NOT %(shared)s AND m.type IN ('user', 'feedback') AND m.owner_id = %(user)s
                              OR m.scope = 'personal' AND m.owner_id = %(user)s)
   FOR UPDATE OF m
"""
INSERT = """
INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
"""
UPDATE = """
UPDATE memories SET type = %s, label = %s, body = %s, deleted = %s, revision = revision + 1, updated_at = now(),
       updated_by = %s
 WHERE id = %s
"""
REVISION = """
INSERT INTO memory_revisions (memory_id, revision, type, label, body, deleted, actor_id)
SELECT id, revision, type, label, body, deleted, %s FROM memories WHERE id = %s
"""
GRANTED = """
SELECT p.name FROM grants g JOIN projects p ON p.id = g.project_id WHERE g.user_id = %s ORDER BY p.name
"""


FIELDS = tuple(Memory.model_fields)  # the order of COLUMNS, before owner_id and project_id


def _row(values) -> Row:
    memory = Memory(**dict(zip(FIELDS, values[: len(FIELDS)], strict=True)))
    return Row(memory, values[len(FIELDS)], values[len(FIELDS) + 1])


def _through(user: Principal, sink: str | None) -> str | None:
    """The sink a read goes through: the one named; else none for a web session (the member reads by their grant,
    as on the hub itself) and Claude Code's for a machine token (a pull writes files that sessions read)."""
    if sink is not None:
        return sink
    return None if user.kind == WEB else AGENT_SINK


def _visible(row: Row, user: Principal, accesses: dict[str, ProjectAccess], sink: str | None) -> bool:
    """The read rule of memories: personal ones and user and feedback ones are their owner's, and a personal one is
    never the agent of a run's; in a project the label must pass the rule of ``evo_agents.hub.access`` for the
    caller's grant and ``sink``, or for the grant alone when ``sink`` is None."""
    memory = row.memory
    if memory.scope == "personal":
        return row.owner_id == user.user_id and user.scope is None
    if memory.type not in SHARED_TYPES and row.owner_id != user.user_id:
        return False
    access = accesses.get(memory.project)
    if access is None:
        return False
    if sink is None:
        return access.rules.visible_by_grant(memory.label, access.max_level)
    return access.visible(memory.label, sink)


async def _access(conn, user: Principal, project: str) -> ProjectAccess | None:
    """``user``'s access to ``project``, None without one, or outside the scope of a run's agent."""
    try:
        return await project_access(conn, user, project)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


async def _accesses(conn, user: Principal, project: str | None) -> dict[str, ProjectAccess]:
    """The projects whose memories ``user`` may read, by name: ``project`` alone when given (404 when the caller
    cannot see it), else every project the caller holds a grant on, the run's alone for the agent of a run. A hub
    admin without a grant reads nothing."""
    if project is not None:
        access = await project_access(conn, user, project)
        return {project: access} if access.role else {}
    names = [row[0] for row in await (await legacy(conn, GRANTED, (user.user_id,))).fetchall()]
    found = {name: await _access(conn, user, name) for name in names if user.reaches(name)}
    return {name: access for name, access in found.items() if access is not None and access.role}


def _params(user: Principal, accesses: dict[str, ProjectAccess], scope, project, location) -> dict:
    project_id = accesses[project].project_id if project is not None and project in accesses else None
    return {
        "personal": user.scope is None,  # the agent of a run reads no personal memory
        "user": user.user_id,
        "projects": [access.project_id for access in accesses.values()],
        "scope": scope,
        "project_id": project_id,
        "location": location,
    }


# Cursors: the (updated_at, id) of the last row a page examined, base64url JSON. They only position a listing in
# rows the caller's filters select, so they need no signature.


def _cursor(updated_at: datetime, memory_id: int) -> str:
    return base64.urlsafe_b64encode(json.dumps([updated_at.isoformat(), memory_id]).encode()).decode().rstrip("=")


def _after(cursor: str | None) -> tuple[str, int]:
    if cursor is None:
        return "-infinity", 0
    try:
        moment, memory_id = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(moment, str) or type(memory_id) is not int or not 0 <= memory_id <= MAX_ID:
            raise ValueError
        datetime.fromisoformat(moment)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(422, "cursor is not one this hub handed out: start the listing again without it") from None
    return moment, memory_id


def _size(memory: Memory) -> int:
    return len(memory.body)  # characters: a cheap bound of the page, which needs no exactness


@router.get("", response_model=Page)
async def list_memories(
    request: Request,
    user: CurrentUser,
    scope: Literal["project", "personal"] | None = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME)] = None,
    location: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    deleted: Annotated[bool, Query(description="include tombstones, as a sync needs")] = False,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    sink: ReadSink = None,
) -> Page:
    """The memories the caller sees, oldest change first."""
    if project is not None and scope == "personal":
        raise HTTPException(422, "a personal memory has no project: leave out project or scope")
    after_at, after_id = _after(cursor)
    sink = _through(user, sink)
    async with request.app.state.engine.begin() as conn:
        accesses = await _accesses(conn, user, project)
        if project is not None and not accesses:
            return Page(items=[], next_cursor=None)  # a hub admin without a grant
        params = {**_params(user, accesses, scope, project, location), "deleted": deleted, "batch": BATCH}
        items: list[Memory] = []
        budget, scanned, following = PAGE_BYTES, 0, None
        while True:
            rows = await (await legacy(conn, LIST, {**params, "after_at": after_at, "after_id": after_id})).fetchall()
            for values in rows:
                row = _row(values)
                scanned += 1
                after_at, after_id = row.memory.updated_at, row.memory.id
                if _visible(row, user, accesses, sink):
                    items.append(row.memory)
                    budget -= _size(row.memory)
                if len(items) >= limit or budget <= 0 or scanned >= SCAN_ROWS:
                    following = _cursor(after_at, after_id)
                    break
            if following or len(rows) < BATCH:
                break
    return Page(items=items, next_cursor=following)


@router.get("/search", response_model=Results, responses={422: {"model": ErrorBody}})
async def search_memories(
    request: Request,
    user: CurrentUser,
    q: Annotated[str, Query(min_length=1, max_length=500, description='words, "a phrase", or -excluded')],
    scope: Literal["project", "personal"] | None = None,
    project: Annotated[str | None, Query(pattern=PROJECT_NAME)] = None,
    location: Annotated[str | None, Query(min_length=1, max_length=255)] = None,
    limit: Annotated[int, Query(ge=1, le=SEARCH_LIMIT)] = 10,
    sink: ReadSink = None,
) -> Results:
    """The memories the caller sees whose name or text matches ``q`` (full text, configuration simple), best
    first."""
    if "\x00" in q:
        raise HTTPException(422, "q holds a NUL character")
    sink = _through(user, sink)
    async with request.app.state.engine.begin() as conn:
        accesses = await _accesses(conn, user, project)
        if project is not None and not accesses:
            return Results(items=[])  # a hub admin without a grant
        params = {**_params(user, accesses, scope, project, location), "q": q, "batch": BATCH}
        found: list[Found] = []
        budget = PAGE_BYTES
        for offset in range(0, SCAN_ROWS, BATCH):
            rows = await (await legacy(conn, SEARCH, {**params, "offset": offset})).fetchall()
            for values in rows:
                row = _row(values)
                if _visible(row, user, accesses, sink):
                    found.append(Found(**row.memory.model_dump(), rank=values[len(FIELDS) + 2]))
                    budget -= _size(row.memory)
                if len(found) >= limit or budget <= 0:
                    return Results(items=found)
            if len(rows) < BATCH:
                break
    return Results(items=found)


def _not_found(memory_id: int) -> str:
    return f"no memory {memory_id} that you can see on this hub"


async def _load(conn, memory_id: int, *, lock: bool = False) -> Row | None:
    found = await (await legacy(conn, ONE + (" FOR UPDATE OF m" if lock else ""), (memory_id,))).fetchone()
    return _row(found) if found else None


async def _readable(conn, user: Principal, memory_id: int, sink: str | None, *, lock: bool = False):
    """The memory and the caller's access to its project; 404 when it does not exist or the caller cannot see it."""
    row = await _load(conn, memory_id, lock=lock)
    access = None
    if row is not None and row.memory.project is not None:
        access = await _access(conn, user, row.memory.project)
    accesses = {row.memory.project: access} if row is not None and access is not None else {}
    if row is None or not _visible(row, user, accesses, sink):
        raise HTTPException(404, _not_found(memory_id))
    return row, access


MemoryId = Annotated[int, Path(ge=1, le=MAX_ID)]


@router.get("/{memory_id}", response_model=Memory, responses={404: {"model": ErrorBody}})
async def show(request: Request, memory_id: MemoryId, user: CurrentUser, sink: ReadSink = None) -> Memory:
    async with request.app.state.engine.begin() as conn:
        row, _ = await _readable(conn, user, memory_id, _through(user, sink))
    return row.memory


def _conflict(request: Request, message: str, current: Memory | None) -> JSONResponse:
    body = Conflict(
        error=CODES[409], message=message, request_id=getattr(request.state, "request_id", None), current=current
    )
    return JSONResponse(jsonable_encoder(body.model_dump(exclude={"detail"})), status_code=409)


def _lock_key(body: MemoryIn, user: Principal, project_id: int | None) -> int:
    owner = user.user_id if body.scope == "personal" or body.type not in SHARED_TYPES else 0
    key = json.dumps([body.scope, project_id, owner, body.location, body.name]).encode()
    return int.from_bytes(hashlib.sha256(key).digest()[:4], "big", signed=True)


async def _written(conn, user: Principal, memory_id: int, action: str) -> Memory:
    """After a change: its revision row, its audit row, and the memory as it now is."""
    await legacy(conn, REVISION, (user.user_id, memory_id))
    await record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=f"memory:{memory_id}")
    return (await _load(conn, memory_id)).memory


def _logged(memory: Memory, user: Principal, outcome: str) -> None:
    extra = {"memory_id": memory.id, "scope": memory.scope, "project": memory.project, "outcome": outcome}
    log.info("memory write", extra={**extra, "revision": memory.revision, "login": user.login})


async def _has_repo(conn, project_id: int, name: str) -> bool:
    found = await legacy(conn, "SELECT 1 FROM project_repos WHERE project_id = %s AND name = %s", (project_id, name))
    return await found.fetchone() is not None


@router.put("", response_model=Written, responses={**REFUSALS, 409: {"model": Conflict}})
async def put(request: Request, body: MemoryIn, user: CurrentUser, sink: Sink = AGENT_SINK) -> Written | JSONResponse:
    """Create or change the memory at the key ``body`` names."""
    if body.scope == "personal" and user.scope is not None:
        raise HTTPException(
            403,
            f"the agent of run {user.scope.run_id} writes memories of project {user.scope.project} only; a personal "
            "memory is its owner's",
        )
    async with request.app.state.engine.begin() as conn:
        access = None
        if body.scope == "project":
            access = await project_access(conn, user, body.project)
            if not has_role(access.role, "writer"):
                access.push_label(None)  # raises the write rule's 403, which checks the role first
            if body.location != HARNESS and not await _has_repo(conn, access.project_id, body.location):
                raise HTTPException(
                    422,
                    f"location must be {HARNESS} or a repo of project {body.project}: see `evo-agents hub project "
                    "list --json`",
                )
        project_id = access.project_id if access else None
        await legacy(conn, "SELECT pg_advisory_xact_lock(%s, %s)", (LOCK_CLASS, _lock_key(body, user, project_id)))
        key = {
            "scope": body.scope,
            "project_id": project_id,
            "location": body.location,
            "name": body.name,
            "shared": body.scope == "project" and body.type in SHARED_TYPES,
            "user": user.user_id,
        }
        found = await (await legacy(conn, KEY, key)).fetchone()
        current = _row(found) if found else None
        accesses = {body.project: access} if access else {}
        if current is not None and not _visible(current, user, accesses, sink):
            return _conflict(
                request,
                f"project {body.project} holds a memory {body.name} at {body.location} that you cannot read; "
                "nothing was written. Use another name",
                None,
            )
        if access is None:
            label = {}
        elif body.label is not None:
            label = access.push_label(body.label.model_dump(exclude_none=True))
        else:
            label = access.push_label(current.memory.label if current else None)
        held = current.memory if current else None
        if held and not held.deleted and (held.body, held.type, held.label) == (body.body, body.type, label):
            return Written(**held.model_dump(), created=False, changed=False)
        if body.if_revision is None and held and not held.deleted:
            return _conflict(
                request,
                f"{body.name} exists on the hub at revision {held.revision}: send the revision you last saw as "
                "if_revision to change it",
                held,
            )
        if body.if_revision is not None and held is None:
            return _conflict(
                request, f"the hub holds no {body.name} here: send it without if_revision to create it", None
            )
        if body.if_revision is not None and body.if_revision != held.revision:
            return _conflict(
                request,
                f"{body.name} changed on the hub: it is at revision {held.revision}, not {body.if_revision}",
                held,
            )
        try:
            if held is None:
                values = (body.scope, project_id, body.location, body.name, body.type, user.user_id)
                inserted = await legacy(conn, INSERT, (*values, Jsonb(label), body.body, user.user_id))
                memory_id = (await inserted.fetchone())[0]
            else:
                memory_id = held.id
                update = (body.type, Jsonb(label), body.body, False, user.user_id, memory_id)
                await legacy(conn, UPDATE, update)
            memory = await _written(conn, user, memory_id, PUT)
        except psycopg.errors.IntegrityError as exc:  # the checks above match the schema's; this is a backstop
            log.warning("memory write refused by the schema", extra={"constraint": exc.diag.constraint_name})
            raise HTTPException(422, "the memory breaks a rule of the hub's schema; nothing was written") from None
    outcome = "created" if held is None else "restored" if held.deleted else "updated"
    _logged(memory, user, outcome)
    return Written(**memory.model_dump(), created=held is None, changed=True)


@router.delete("/{memory_id}", response_model=Written, responses={**REFUSALS, 409: {"model": Conflict}})
async def delete(
    request: Request,
    memory_id: MemoryId,
    user: CurrentUser,
    if_revision: Annotated[int, Query(ge=1, le=MAX_REVISION, description="the revision last seen")],
    sink: Sink = AGENT_SINK,
) -> Written | JSONResponse:
    """Leave a tombstone of memory ``memory_id``: deleted, no body, the next revision. Deleting a tombstone changes
    nothing."""
    async with request.app.state.engine.begin() as conn:
        row, access = await _readable(conn, user, memory_id, sink, lock=True)
        held = row.memory
        if access is not None:
            access.push_label(held.label)  # the write rule
        if held.deleted:
            return Written(**held.model_dump(), created=False, changed=False)
        if if_revision != held.revision:
            return _conflict(
                request, f"{held.name} changed on the hub: it is at revision {held.revision}, not {if_revision}", held
            )
        await legacy(conn, UPDATE, (held.type, Jsonb(held.label), "", True, user.user_id, memory_id))
        memory = await _written(conn, user, memory_id, DELETE)
    _logged(memory, user, "deleted")
    return Written(**memory.model_dump(), created=False, changed=True)


# History: the revisions of one memory, each shown only when the caller could read the memory as it was then.

DEFAULT_REVISIONS = 50
MAX_REVISIONS = 200
REVISIONS = """
SELECT r.revision, r.type, r.label, r.deleted, octet_length(r.body), u.login, r.created_at
  FROM memory_revisions r JOIN users u ON u.id = r.actor_id
 WHERE r.memory_id = %(memory_id)s AND (%(before)s::integer IS NULL OR r.revision < %(before)s)
 ORDER BY r.revision DESC
 LIMIT %(batch)s
"""
ONE_REVISION = """
SELECT r.revision, r.type, r.label, r.deleted, octet_length(r.body), u.login, r.created_at, r.body
  FROM memory_revisions r JOIN users u ON u.id = r.actor_id
 WHERE r.memory_id = %s AND r.revision = %s
"""


class RevisionSummary(BaseModel):
    revision: int
    type: str
    label: dict
    deleted: bool = Field(description="a tombstone: the memory was deleted at this revision")
    size: int = Field(description="bytes of the body in UTF-8")
    actor: str = Field(description="the login of who wrote this revision")
    created_at: datetime


class Revisions(BaseModel):
    memory_id: int
    items: list[RevisionSummary] = Field(description="the latest first")
    next_before: int | None = Field(description="pass as before for older revisions; null after the oldest")


class MemoryRevision(RevisionSummary):
    memory_id: int
    body: str = Field(description="the whole file as it was; empty for a tombstone")


REVISION_FIELDS = tuple(RevisionSummary.model_fields)


def _as_of(row: Row, kind: str, label: dict) -> Row:
    """``row`` as it was at a revision of type ``kind`` labelled ``label``: what the read rule judges a revision by."""
    return Row(row.memory.model_copy(update={"type": kind, "label": label}), row.owner_id, row.project_id)


def _revision_visible(
    row: Row, values, user: Principal, access: ProjectAccess | None, sink: str | None
) -> RevisionSummary | None:
    summary = RevisionSummary(**dict(zip(REVISION_FIELDS, values[: len(REVISION_FIELDS)], strict=True)))
    accesses = {row.memory.project: access} if access is not None else {}
    return summary if _visible(_as_of(row, summary.type, summary.label), user, accesses, sink) else None


RevisionNumber = Annotated[int, Path(ge=1, le=MAX_REVISION)]


@router.get("/{memory_id}/revisions", response_model=Revisions, responses={404: {"model": ErrorBody}})
async def revisions(
    request: Request,
    memory_id: MemoryId,
    user: CurrentUser,
    before: Annotated[int | None, Query(ge=2, le=MAX_REVISION, description="only revisions older than this")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_REVISIONS)] = DEFAULT_REVISIONS,
    sink: ReadSink = None,
) -> Revisions:
    """The revisions of a memory the caller sees, the latest first. A revision whose label (or type) the caller
    could not read is left out, so a label raised in the past never shows through its history."""
    sink = _through(user, sink)
    items: list[RevisionSummary] = []
    following = None
    async with request.app.state.engine.begin() as conn:
        row, access = await _readable(conn, user, memory_id, sink)
        params = {"memory_id": memory_id, "before": before, "batch": BATCH}
        oldest_seen = False
        for _ in range(0, SCAN_ROWS, BATCH):
            found = await (await legacy(conn, REVISIONS, params)).fetchall()
            for values in found:
                params["before"] = values[0]
                summary = _revision_visible(row, values, user, access, sink)
                if summary is not None:
                    items.append(summary)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
            if len(found) < BATCH:
                oldest_seen = True
                break
        if not oldest_seen and params["before"] is not None and params["before"] > 1:
            following = params["before"]  # the limit or the scan bound ended this page
    return Revisions(memory_id=memory_id, items=items, next_before=following)


@router.get("/{memory_id}/revisions/{revision}", response_model=MemoryRevision, responses={404: {"model": ErrorBody}})
async def show_revision(
    request: Request,
    memory_id: MemoryId,
    revision: RevisionNumber,
    user: CurrentUser,
    sink: ReadSink = None,
) -> MemoryRevision:
    """One revision of a memory with its body; 404 when it does not exist or the caller could not read it."""
    sink = _through(user, sink)
    async with request.app.state.engine.begin() as conn:
        row, access = await _readable(conn, user, memory_id, sink)
        values = await (await legacy(conn, ONE_REVISION, (memory_id, revision))).fetchone()
    summary = None if values is None else _revision_visible(row, values, user, access, sink)
    if summary is None:
        raise HTTPException(404, f"no revision {revision} of memory {memory_id} that you can see on this hub")
    return MemoryRevision(**summary.model_dump(), memory_id=memory_id, body=values[len(REVISION_FIELDS)])

"""What the routes of the memories do: the read rule, the write rule and the fences of a run, the paging of a listing
and the history of a memory, each request in one transaction (``UnitOfWork``) that reads and writes through the
repository.

A refusal is a domain error with the message the API has always given. A write whose ``if_revision`` is not the
revision the hub holds raises ``Stale``, a 409 carrying the current version when the caller may see it, which the
route answers with that version; nothing is written before it, so the transaction rolls back.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from datetime import datetime

import psycopg
from fastapi import HTTPException
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.memory_files import SHARED_TYPES
from evo_agents.hub.memory_places import HARNESS
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Forbidden, NotFound, Unprocessable
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.memories import repository
from evo_agents.hub.server.memories.models import (
    AUDIENCE,
    BATCH,
    DELETE,
    MAX_ID,
    PAGE_BYTES,
    PUT,
    SCAN_ROWS,
    AudienceIn,
    Found,
    Memory,
    MemoryRevision,
    Page,
    Results,
    Revisions,
    RevisionSummary,
    Row,
    Written,
)
from evo_agents.hub.server.memories.service import agents
from evo_agents.hub.server.memories.service.audience import Reader, label_audience, raised, with_audience
from evo_agents.hub.server.memories.service.inputs import MemoryIn
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import WEB, Principal, holds
from evo_agents.kg.policy import audience_below

log = logging.getLogger(__name__)


class Stale(Conflict):
    """A write that cannot be made at the revision it names: the 409 with the version the hub holds, when the caller
    may see it."""

    def __init__(self, message: str, current: Memory | None):
        super().__init__(message)
        self.current = current


# The read rule


def through(user: Principal, sink: str | None) -> str | None:
    """The sink a read goes through: the one named; else none for a web session (the member reads by their grant,
    as on the hub itself) and Claude Code's for a machine token (a pull writes files that sessions read)."""
    if sink is not None:
        return sink
    return None if user.kind == WEB else AGENT_SINK


def visible(
    row: Row, user: Principal, accesses: dict[str, ProjectAccess], sink: str | None, audience: frozenset[str]
) -> bool:
    """The read rule of memories: the audience the label names, if any, is within ``audience``, the caller's
    (``Reader``); personal ones and user and feedback ones are their owner's, and a personal one is never the agent of
    a run's; in a project the label must pass the rule of ``evo_agents.hub.access`` for the caller's grant and
    ``sink``, or for the grant alone when ``sink`` is None."""
    memory = row.memory
    if not audience_below(label_audience(memory.label), audience):
        return False
    if memory.scope == "personal":
        return row.owner_id == user.user_id and user.scope is None
    if not agents.readable(row.profile_id, memory.type, user):
        return False
    if memory.type not in SHARED_TYPES and row.owner_id != user.user_id and row.profile_id is None:
        return False
    access = accesses.get(memory.project)  # pyright: ignore[reportArgumentType]
    if access is None:
        return False
    if sink is None:
        return access.rules.visible_by_grant(memory.label, access.max_level)
    return access.visible(memory.label, sink)


async def _access(conn: AsyncConnection, user: Principal, project: str) -> ProjectAccess | None:
    """``user``'s access to ``project``, None without one, or outside the scope of a run's agent."""
    try:
        return await project_access(conn, user, project)
    except HTTPException as exc:
        if exc.status_code == 404:
            return None
        raise


async def _accesses(conn: AsyncConnection, user: Principal, project: str | None) -> dict[str, ProjectAccess]:
    """The projects whose memories ``user`` may read, by name: ``project`` alone when given (404 when the caller
    cannot see it), else every project the caller holds a grant on, the run's alone for the agent of a run. A hub
    admin without a grant reads nothing."""
    if project is not None:
        access = await project_access(conn, user, project)
        return {project: access} if access.role else {}
    names = await repository.granted_projects(conn, user.user_id)
    found = {name: await _access(conn, user, name) for name in names if user.reaches(name)}
    return {name: access for name, access in found.items() if access is not None and access.role}


def _reached(user: Principal, accesses: dict[str, ProjectAccess], scope, project, location, reach) -> tuple[dict, dict]:
    """The shape of ``repository.reach`` for the caller, the filters given and the namespaces ``reach`` (namespace,
    id), and its parameters."""
    narrowed = accesses[project].project_id if project is not None and project in accesses else None
    shape = {
        "personal": user.scope is None,  # the agent of a run reads no personal memory
        "scope": scope is not None,
        "project": narrowed is not None,
        "location": location is not None,
        "namespace": reach[0],
    }
    bound = {
        "namespace_id": reach[1],
        "user_id": user.user_id,
        "reach_projects": [access.project_id for access in accesses.values()],
        "scope": scope,
        "project_id": narrowed,
        "location": location,
    }
    return shape, bound


def _size(memory: Memory) -> int:
    return len(memory.body)  # characters: a cheap bound of the page, which needs no exactness


def _not_found(memory_id: int) -> str:
    return f"no memory {memory_id} that you can see on this hub"


async def readable(
    conn: AsyncConnection,
    user: Principal,
    memory_id: int,
    sink: str | None,
    *,
    lock: bool = False,
    reader: Reader | None = None,
) -> tuple[Row, ProjectAccess | None]:
    """The memory and the caller's access to its project; 404 when it does not exist or the caller cannot see it."""
    row = await repository.load(conn, memory_id, lock=lock)
    access = None
    if row is not None and row.memory.project is not None:
        access = await _access(conn, user, row.memory.project)
    accesses = {row.memory.project: access} if row is not None and access is not None else {}
    audience = await (reader or Reader(conn, user)).audience(row.memory.label) if row is not None else frozenset()
    if row is None or not visible(row, user, accesses, sink, audience):  # pyright: ignore[reportArgumentType]
        raise NotFound(_not_found(memory_id))
    return row, access


# Paging


def cursor(updated_at: datetime, memory_id: int) -> str:
    """The cursor of a listing after the row (``updated_at``, ``memory_id``)."""
    return base64.urlsafe_b64encode(json.dumps([updated_at.isoformat(), memory_id]).encode()).decode().rstrip("=")


def after(handed: str | None) -> tuple[str, int]:
    """The (updated_at, id) a cursor names, the start of a listing for None; 422 for one this hub did not hand out."""
    if handed is None:
        return "-infinity", 0
    try:
        moment, memory_id = json.loads(base64.urlsafe_b64decode(handed + "=" * (-len(handed) % 4)))
        if not isinstance(moment, str) or type(memory_id) is not int or not 0 <= memory_id <= MAX_ID:
            raise ValueError
        datetime.fromisoformat(moment)
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise Unprocessable("cursor is not one this hub handed out: start the listing again without it") from None
    return moment, memory_id


# Reads


async def listed(
    engine: AsyncEngine,
    user: Principal,
    *,
    scope: str | None,
    project: str | None,
    location: str | None,
    deleted: bool,
    handed: str | None,
    limit: int,
    sink: str | None,
    agent: str | None,
) -> Page:
    """The memories the caller sees, oldest change first, from cursor ``handed``."""
    if project is not None and scope == "personal":
        raise Unprocessable("a personal memory has no project: leave out project or scope")
    position = after(handed)
    sink = through(user, sink)
    async with UnitOfWork(engine) as conn:
        *reach, project = await agents.reach(conn, user, agent, project)
        accesses = await _accesses(conn, user, project)
        if project is not None and not accesses:
            return Page(items=[], next_cursor=None)  # a hub admin without a grant
        shape, bound = _reached(user, accesses, scope, project, location, reach)
        items: list[Memory] = []
        budget, scanned, following, reader = PAGE_BYTES, 0, None, Reader(conn, user)
        while True:
            rows = await repository.listed(conn, deleted, shape, bound, position)
            for row in rows:
                scanned += 1
                position = (row.memory.updated_at, row.memory.id)
                if visible(row, user, accesses, sink, await reader.audience(row.memory.label)):
                    items.append(row.memory)
                    budget -= _size(row.memory)
                if len(items) >= limit or budget <= 0 or scanned >= SCAN_ROWS:
                    following = cursor(row.memory.updated_at, row.memory.id)
                    break
            if following or len(rows) < BATCH:
                break
        await reader.gave(memory.label for memory in items)
    return Page(items=items, next_cursor=following)


async def search(
    engine: AsyncEngine,
    user: Principal,
    *,
    q: str,
    scope: str | None,
    project: str | None,
    location: str | None,
    limit: int,
    sink: str | None,
    agent: str | None,
) -> Results:
    """The memories the caller sees whose name or text matches ``q`` (full text, configuration simple), best
    first."""
    if "\x00" in q:
        raise Unprocessable("q holds a NUL character")
    sink = through(user, sink)
    async with UnitOfWork(engine) as conn:
        *reach, project = await agents.reach(conn, user, agent, project)
        accesses = await _accesses(conn, user, project)
        if project is not None and not accesses:
            return Results(items=[])  # a hub admin without a grant
        shape, bound = _reached(user, accesses, scope, project, location, reach)
        found: list[Found] = []
        budget, reader = PAGE_BYTES, Reader(conn, user)
        for offset in range(0, SCAN_ROWS, BATCH):
            rows = await repository.searched(conn, shape, bound, q, offset)
            for row, rank in rows:
                if visible(row, user, accesses, sink, await reader.audience(row.memory.label)):
                    found.append(Found(**row.memory.model_dump(), rank=rank))
                    budget -= _size(row.memory)
                if len(found) >= limit or budget <= 0:
                    break
            if len(found) >= limit or budget <= 0 or len(rows) < BATCH:
                break
        await reader.gave(memory.label for memory in found)
    return Results(items=found)


async def opened(conn: AsyncConnection, user: Principal, profile_id: int | None, memory_id: int) -> None:
    """The read of memory ``memory_id``, of namespace ``profile_id``, that ``user`` opened: recorded when the agent of
    a run opened an agent's memory."""
    if user.scope is not None and profile_id is not None:
        await repository.mark_opened(conn, memory_id)


async def shown(engine: AsyncEngine, user: Principal, memory_id: int, sink: str | None) -> Memory:
    """Memory ``memory_id`` as the caller sees it, through ``sink``."""
    async with UnitOfWork(engine) as conn:
        reader = Reader(conn, user)
        row, _ = await readable(conn, user, memory_id, through(user, sink), reader=reader)
        await reader.gave([row.memory.label])
        await opened(conn, user, row.profile_id, memory_id)
    return row.memory


# Writes


async def _written(conn: AsyncConnection, user: Principal, row_project: int | None, memory_id: int, action: str):
    """After a change: its revision row, its audit row, and the memory as it now is."""
    await repository.add_revision(conn, memory_id, user.user_id, agents.provenance(user))
    await Auditor(conn, row_project, user).record(action, f"memory:{memory_id}")
    written = await repository.load(conn, memory_id)
    assert written is not None
    return written.memory


def _logged(memory: Memory, user: Principal, outcome: str) -> None:
    extra = {"memory_id": memory.id, "scope": memory.scope, "project": memory.project, "outcome": outcome}
    log.info("memory write", extra={**extra, "revision": memory.revision, "login": user.login})


async def _target(conn: AsyncConnection, body: MemoryIn, user: Principal) -> tuple[ProjectAccess | None, int | None]:
    """The writer's access to the project of ``body`` (the write rule's role, a location of the project) and the
    namespace it writes into (``agents.namespace``); (None, None) for a personal memory."""
    if body.scope != "project" or body.project is None:
        return None, None
    access = await project_access(conn, user, body.project)
    if not holds(access.role, "writer"):
        access.push_label(None)  # raises the write rule's 403, which checks the role first
    if body.location != HARNESS and not await repository.has_repo(conn, access.project_id, body.location):
        raise Unprocessable(
            f"location must be {HARNESS} or a repo of project {body.project}: see `evo-agents hub project list --json`"
        )
    return access, await agents.namespace(conn, user, access.project_id, body.project, body.agent)


def _key_kind(body: MemoryIn, profile_id: int | None) -> str:
    if profile_id is not None:
        return "agent"
    return "shared" if body.scope == "project" and body.type in SHARED_TYPES else "owned"


async def _fenced(conn: AsyncConnection, body: MemoryIn, user: Principal, key: dict, current: Row | None) -> None:
    """The fences of a run (``agents.check_write``), and for the agent of a profile that is not its project's
    default, no new memory at the name of a live shared one, which it would seem to change."""
    profile_id = key["profile_id"]
    project = body.project or ""
    await agents.check_write(conn, user, body.type, current and current.memory.id, profile_id, project)
    if current is None and profile_id is not None and user.scope is not None:
        shared = await repository.at_key(conn, "shared", {**key, "profile_id": None})
        if shared is not None and not shared.memory.deleted:
            raise Forbidden(
                f"{agents.promotion(user.scope, project)}. Project {body.project} holds a shared memory "
                f"{body.name} at {body.location}: write yours under another name; nothing was written"
            )


def _revision_conflict(body: MemoryIn, held: Memory | None) -> None:
    """The 409 of a write whose if_revision is not the revision the hub holds."""
    if body.if_revision is None and held and not held.deleted:
        message = f"{body.name} exists on the hub at revision {held.revision}: send the revision you last saw as "
        raise Stale(message + "if_revision to change it", held)
    if body.if_revision is not None and held is None:
        raise Stale(f"the hub holds no {body.name} here: send it without if_revision to create it", None)
    if held is not None and body.if_revision not in (None, held.revision):
        message = f"{body.name} changed on the hub: it is at revision {held.revision}, not {body.if_revision}"
        raise Stale(message, held)


async def _label(conn: AsyncConnection, user: Principal, body: MemoryIn, access, held: Memory | None) -> dict:
    """The label the write takes: the one named, else the current one, else the project's default, as the write rule
    stores it, holding the audience the write raises it to."""
    if access is None:
        label = {}
    elif body.label is not None:
        label = access.push_label(body.label.model_dump(exclude_none=True))
    else:
        label = access.push_label(held.label if held else None)
    return with_audience(label, await raised(conn, user, held.label if held else None, body.audience))


async def _store(conn: AsyncConnection, user: Principal, body: MemoryIn, project_id, profile_id, held, label) -> int:
    """Write the memory ``body`` names over ``held`` (its next revision), else a new one; its id."""
    if held is not None:
        await repository.change(conn, held.id, body.type, label, body.body, False, user.user_id)
        return held.id
    values = {
        "scope": body.scope,
        "project_id": project_id,
        "profile_id": profile_id,
        "location": body.location,
        "name": body.name,
        "type": body.type,
        "owner_id": user.user_id,
        "label": label,
        "body": body.body,
        "updated_by": user.user_id,
    }
    return await repository.insert_memory(conn, values)


async def put(engine: AsyncEngine, user: Principal, body: MemoryIn, sink: str) -> Written:
    """Create or change the memory at the key ``body`` names, in the namespace of ``body.agent`` or the run's."""
    if body.scope == "personal" and user.scope is not None:
        raise Forbidden(
            f"the agent of run {user.scope.run_id} writes memories of project {user.scope.project} only; a personal "
            "memory is its owner's"
        )
    async with UnitOfWork(engine) as conn:
        access, profile_id = await _target(conn, body, user)
        project_id = access.project_id if access else None
        await repository.lock_key(
            conn, body.scope, body.type, body.location, body.name, user.user_id, project_id, profile_id
        )
        key = repository.key(body.scope, project_id, body.location, body.name, user.user_id, profile_id)
        current = await repository.at_key(conn, _key_kind(body, profile_id), key)
        accesses = {body.project: access} if access and body.project else {}
        audience = await Reader(conn, user).audience(current.memory.label) if current else frozenset()
        if current is not None and not visible(current, user, accesses, sink, audience):
            raise Stale(
                f"project {body.project} holds a memory {body.name} at {body.location} that you cannot read; "
                "nothing was written. Use another name",
                None,
            )
        await _fenced(conn, body, user, key, current)
        held = current.memory if current else None
        label = await _label(conn, user, body, access, held)
        if held and not held.deleted and (held.body, held.type, held.label) == (body.body, body.type, label):
            return Written(**held.model_dump(), created=False, changed=False)
        _revision_conflict(body, held)
        try:
            memory_id = await _store(conn, user, body, project_id, profile_id, held, label)
            memory = await _written(conn, user, project_id, memory_id, PUT)
        except sa_exc.IntegrityError as exc:  # the checks above match the schema's; this is a backstop
            if not isinstance(exc.orig, psycopg.errors.IntegrityError):
                raise
            log.warning("memory write refused by the schema", extra={"constraint": exc.orig.diag.constraint_name})
            raise Unprocessable("the memory breaks a rule of the hub's schema; nothing was written") from None
    outcome = "created" if held is None else "restored" if held.deleted else "updated"
    _logged(memory, user, outcome)
    return Written(**memory.model_dump(), created=held is None, changed=True)


async def delete(engine: AsyncEngine, user: Principal, memory_id: int, if_revision: int, sink: str) -> Written:
    """Leave a tombstone of memory ``memory_id``: deleted, no body, the next revision. Deleting a tombstone changes
    nothing."""
    async with UnitOfWork(engine) as conn:
        row, access = await readable(conn, user, memory_id, sink, lock=True)
        held = row.memory
        if access is not None:
            access.push_label(held.label)  # the write rule
        await agents.check_write(conn, user, held.type, held.id, row.profile_id, held.project or "")
        if held.deleted:
            return Written(**held.model_dump(), created=False, changed=False)
        if if_revision != held.revision:
            raise Stale(f"{held.name} changed on the hub: it is at revision {held.revision}, not {if_revision}", held)
        await repository.change(conn, memory_id, held.type, held.label, "", True, user.user_id)
        memory = await _written(conn, user, row.project_id, memory_id, DELETE)
    _logged(memory, user, "deleted")
    return Written(**memory.model_dump(), created=False, changed=True)


async def lower_audience(engine: AsyncEngine, user: Principal, memory_id: int, body: AudienceIn, sink: str) -> Written:
    """Lower the audience of memory ``memory_id`` by hand, to a part of it: an admin of its project who reads it
    alone, with the audit row ``memory.audience``. Any other write keeps or raises it (``put``)."""
    async with UnitOfWork(engine) as conn:
        row, access = await readable(conn, user, memory_id, sink, lock=True)
        held = row.memory
        if access is None or not holds(access.role, "admin"):
            where = f"project {held.project}" if access else "a project, and a personal memory is in none"
            raise Forbidden(f"lowering the audience of a memory needs the admin role on {where}")
        access.push_label(held.label)  # the write rule
        current, wanted = label_audience(held.label), frozenset(body.audience)
        if not wanted <= current:
            raise Unprocessable("the audience may only lose principals here: a write of the memory adds them")
        if body.if_revision != held.revision:
            message = f"{held.name} changed on the hub: it is at revision {held.revision}, not {body.if_revision}"
            raise Stale(message, held)
        if wanted == current:
            return Written(**held.model_dump(), created=False, changed=False)
        label = with_audience(held.label, wanted)
        await repository.change(conn, memory_id, held.type, label, held.body, held.deleted, user.user_id)
        memory = await _written(conn, user, row.project_id, memory_id, AUDIENCE)
    _logged(memory, user, "audience lowered")
    return Written(**memory.model_dump(), created=False, changed=True)


# History: the revisions of one memory, each shown only when the caller could read the memory as it was then.


def _as_of(row: Row, kind: str, label: dict) -> Row:
    """``row`` as it was at a revision of type ``kind`` labelled ``label``: what the read rule judges a revision by."""
    return Row(
        row.memory.model_copy(update={"type": kind, "label": label}), row.owner_id, row.project_id, row.profile_id
    )


async def _revision_visible(
    row: Row, values, reader: Reader, access: ProjectAccess | None, sink: str | None
) -> RevisionSummary | None:
    fields = {name: values._mapping[name] for name in ("revision", "type", "label", "deleted", "size", "actor")}
    summary = RevisionSummary(**fields, created_at=values.created_at, provenance=agents.provenance_of(values))
    accesses = {row.memory.project: access} if access is not None and row.memory.project is not None else {}
    audience = await reader.audience(summary.label)
    return summary if visible(_as_of(row, summary.type, summary.label), reader.user, accesses, sink, audience) else None


async def revisions(
    engine: AsyncEngine, user: Principal, memory_id: int, before: int | None, limit: int, sink: str | None
) -> Revisions:
    """The revisions of a memory the caller sees, the latest first. A revision whose label (or type) the caller
    could not read is left out, so a label raised in the past never shows through its history."""
    sink = through(user, sink)
    items: list[RevisionSummary] = []
    following = None
    async with UnitOfWork(engine) as conn:
        reader = Reader(conn, user)
        row, access = await readable(conn, user, memory_id, sink, reader=reader)
        oldest_seen = False
        for _ in range(0, SCAN_ROWS, BATCH):
            found = await repository.history(conn, memory_id, before)
            for values in found:
                before = values.revision
                summary = await _revision_visible(row, values, reader, access, sink)
                if summary is not None:
                    items.append(summary)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
            if len(found) < BATCH:
                oldest_seen = True
                break
        if not oldest_seen and before is not None and before > 1:
            following = before  # the limit or the scan bound ended this page
    return Revisions(memory_id=memory_id, items=items, next_before=following)


async def show_revision(
    engine: AsyncEngine, user: Principal, memory_id: int, revision: int, sink: str | None
) -> MemoryRevision:
    """One revision of a memory with its body; 404 when it does not exist or the caller could not read it."""
    sink = through(user, sink)
    async with UnitOfWork(engine) as conn:
        reader = Reader(conn, user)
        row, access = await readable(conn, user, memory_id, sink, reader=reader)
        values = await repository.revision(conn, memory_id, revision)
        summary = None if values is None else await _revision_visible(row, values, reader, access, sink)
    if summary is None or values is None:
        raise NotFound(f"no revision {revision} of memory {memory_id} that you can see on this hub")
    return MemoryRevision(**summary.model_dump(), memory_id=memory_id, body=values.body)

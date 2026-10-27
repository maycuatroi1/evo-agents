"""The records an isolated plugin keeps on the hub (``docs/plugins.md``, section Records), in Postgres, by project,
plugin and collection (``tables.plugin_records``, schema 0169).

A record is a JSON object under a key of a collection, with a revision that counts its writes, the level of the project's
ladder it carries, whether its writer alone is its audience, who wrote it last and when. The hub filters every read by
``Reach``, what one call may reach: the person the call is for with their grant's highest level, the sink the call reads
through (the agent of a run reads through its sink's clearance, a member on the web by their grant alone), and for the
record the audience it names. A record the call may not see is a record that does not exist: ``get`` and ``delete`` find
none, ``listing`` leaves it out, and ``put`` over it cannot tell its key from one that is taken. A call for no person
(a job, an event) reaches the levels up to the one the admin of the project set when enabling the plugin, the audience
of a record not counted.

The rules are ``evo_agents.hub.access.ProjectRules`` over the label ``{level, projects: [project]}`` of each record, as for
every other object of a project; nothing here chooses who a call is for, which ``evo_agents.hub.server.plugin_records_api``
and ``plugin_delegation`` decide from the request. No module of the server is imported: the job worker could use it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import ProjectRules

COLLECTION = r"^[a-z][a-z0-9_-]{0,62}$"
KEY = r"^[A-Za-z0-9][A-Za-z0-9._~:@=+-]{0,199}$"
MAX_BODY_BYTES = 64 * 1024  # a record's body as compact JSON
MAX_PAGE = 200
WRITER = "writer"  # the audience that is the record's writer alone
EVERYONE = "everyone"  # the audience that is every person the level reaches
AUDIENCES = (WRITER, EVERYONE)
_COLLECTION = re.compile(COLLECTION)
_KEY = re.compile(KEY)


class RecordError(Exception):
    """A call the records refuse; ``status`` is the HTTP status the hub answers with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Reach:
    """What one call of plugin ``plugin_id`` may reach of the records of project ``project_id``."""

    project_id: int
    plugin_id: int
    rules: ProjectRules
    max_level: str | None  # the highest level the call reaches; None reaches none
    user_id: int | None = None  # the person the call is for; None for a call with no caller
    sink: str | None = None  # the sink the call reads through; None reads by the grant alone
    people: frozenset[int] = field(default_factory=frozenset)  # who the read must be cleared for (the chat run's two)

    def sees_level(self, level: str) -> bool:
        """Whether a record at ``level`` is within this call's reach: the read rule of the project's ladder."""
        label = {"level": level, "projects": [self.rules.name]}
        if self.sink is None:
            return self.rules.visible_by_grant(label, self.max_level)
        return self.rules.visible(label, self.max_level, self.sink)

    def levels(self) -> list[str]:
        return [level for level in self.rules.levels if self.sees_level(level)]

    def sees(self, row) -> bool:
        """Whether this call sees a stored row: its level, and for a call for a person the audience it names."""
        if not self.sees_level(row.level):
            return False
        return self.user_id is None or self._audience(row.audience_user_id)

    def _audience(self, named: int | None) -> bool:
        return named is None or (self.people or frozenset({self.user_id})) == frozenset({named})


def _record_columns():
    r = tables.plugin_records.c
    created, updated = tables.users.alias("created_users"), tables.users.alias("updated_users")
    columns = (
        r.collection,
        r.key,
        r.revision,
        r.level,
        r.audience_user_id,
        r.body,
        r.created_at,
        r.updated_at,
        created.c.login.label("created_by"),
        updated.c.login.label("updated_by"),
    )
    source = tables.plugin_records.outerjoin(created, created.c.id == r.created_by).outerjoin(
        updated, updated.c.id == r.updated_by
    )
    return columns, source


def _where(reach: Reach, collection: str):
    r = tables.plugin_records.c
    return and_(r.project_id == reach.project_id, r.plugin_id == reach.plugin_id, r.collection == collection)


def view(row) -> dict:
    """The record a stored row is, as the API gives it."""
    return {
        "collection": row.collection,
        "key": row.key,
        "revision": row.revision,
        "level": row.level,
        "audience": WRITER if row.audience_user_id is not None else EVERYONE,
        "body": row.body,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_by": row.updated_by,
        "updated_at": row.updated_at,
    }


def check_name(collection: str, key: str | None = None) -> None:
    if not _COLLECTION.fullmatch(collection):
        raise RecordError(422, "a collection is named by lowercase letters, digits, - and _, starting with a letter")
    if key is not None and not _KEY.fullmatch(key):
        raise RecordError(422, "a key is 1 to 200 characters of letters, digits and . _ ~ : @ = + -")


def _body_size(body: dict) -> int:
    return len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())


async def get(conn: AsyncConnection, reach: Reach, collection: str, key: str) -> dict:
    """Record ``key`` of ``collection``; 404 when there is none or this call may not see it."""
    check_name(collection, key)
    columns, source = _record_columns()
    r = tables.plugin_records.c
    row = (
        await conn.execute(select(*columns).select_from(source).where(_where(reach, collection), r.key == key))
    ).first()
    if row is None or not reach.sees(row):
        raise RecordError(404, f"no record {key} in collection {collection}")
    return view(row)


def _visible_sql(reach: Reach):
    r = tables.plugin_records.c
    seen = r.level.in_(reach.levels())
    if reach.user_id is None:
        return seen
    people = reach.people or frozenset({reach.user_id})
    only = next(iter(people)) if len(people) == 1 else None
    audience = (
        r.audience_user_id.is_(None) if only is None else or_(r.audience_user_id.is_(None), r.audience_user_id == only)
    )
    return and_(seen, audience)


async def listing(
    conn: AsyncConnection, reach: Reach, collection: str, *, after: str | None = None, limit: int = 50
) -> tuple[list[dict], str | None]:
    """The records of ``collection`` this call sees, by key, at most ``limit`` after key ``after``, and the key to pass
    as ``after`` for the next page (None at the end)."""
    check_name(collection)
    limit = max(1, min(limit, MAX_PAGE))
    columns, source = _record_columns()
    r = tables.plugin_records.c
    query = select(*columns).select_from(source).where(_where(reach, collection), _visible_sql(reach))
    if after is not None:
        query = query.where(r.key > after)
    rows = list(await conn.execute(query.order_by(r.key).limit(limit + 1)))
    page = rows[:limit]
    return [view(row) for row in page], (page[-1].key if len(rows) > limit else None)


async def _held(conn: AsyncConnection, reach: Reach, collection: str, key: str):
    r = tables.plugin_records
    query = select(r).where(_where(reach, collection), r.c.key == key).with_for_update()
    return (await conn.execute(query)).first()


def _reachable(reach: Reach, level: str) -> None:
    if level not in reach.rules.levels:
        raise RecordError(
            422, f"{level!r} is not a level of project {reach.rules.name} ({', '.join(reach.rules.levels)})"
        )
    if not reach.sees_level(level):
        raise RecordError(403, f"a record at {level} is above what this call reaches, which is up to {reach.max_level}")


async def put(
    conn: AsyncConnection,
    reach: Reach,
    collection: str,
    key: str,
    body: dict,
    *,
    level: str | None = None,
    audience: str | None = None,
    revision: int | None = None,
) -> tuple[dict, bool]:
    """Write record ``key`` of ``collection`` as this call: (the record, whether it is new).

    ``revision`` is the revision the writer read: 0 asks for a new record, a number for the very revision (409 when the
    record has moved on), and None writes over what is there. A new record takes ``level`` or the level of the
    project's default label, and the audience ``audience`` names (the writer alone, or everyone the level reaches); an
    existing one keeps what it has unless they are given. A level above this call's reach is 403. A key held by a record
    this call may not see is a taken key (409) for a write that asks for a new record or none in particular, else
    404, as for a key nobody holds."""
    check_name(collection, key)
    if audience is not None and audience not in AUDIENCES:
        raise RecordError(422, f"audience is {WRITER} or {EVERYONE}")
    if audience == WRITER and reach.user_id is None:
        raise RecordError(422, "a call with no caller has no writer to name as the audience")
    if _body_size(body) > MAX_BODY_BYTES:
        raise RecordError(413, f"a record's body is at most {MAX_BODY_BYTES // 1024} KiB as JSON")
    held = await _held(conn, reach, collection, key)
    if held is None:
        if revision not in (None, 0):
            raise RecordError(404, f"no record {key} in collection {collection}")
        return await _create(conn, reach, collection, key, body, level, audience)
    if not reach.sees(held):
        if revision in (None, 0):
            raise RecordError(409, f"key {key} of collection {collection} is taken")
        raise RecordError(404, f"no record {key} in collection {collection}")
    if revision == 0 or (revision is not None and revision != held.revision):
        raise RecordError(409, f"record {key} is at revision {held.revision}, not {revision}")
    return await _replace(conn, reach, held, body, level, audience), False


async def _create(conn, reach: Reach, collection: str, key: str, body: dict, level: str | None, audience: str | None):
    level = level or reach.rules.default_label["level"]
    _reachable(reach, level)
    r = tables.plugin_records
    values = {
        "project_id": reach.project_id,
        "plugin_id": reach.plugin_id,
        "collection": collection,
        "key": key,
        "level": level,
        "audience_user_id": reach.user_id if audience == WRITER else None,
        "body": body,
        "created_by": reach.user_id,
        "updated_by": reach.user_id,
    }
    made = await conn.execute(pg_insert(r).values(values).on_conflict_do_nothing().returning(r.c.key))
    if made.first() is None:
        raise RecordError(409, f"key {key} of collection {collection} is taken")
    return await get(conn, reach, collection, key), True


async def _replace(conn, reach: Reach, held, body: dict, level: str | None, audience: str | None) -> dict:
    r = tables.plugin_records
    values: dict = {"body": body, "revision": held.revision + 1, "updated_by": reach.user_id, "updated_at": func.now()}
    if level is not None:
        _reachable(reach, level)
        values["level"] = level
    if audience is not None:
        values["audience_user_id"] = reach.user_id if audience == WRITER else None
    await conn.execute(update(r).where(_where(reach, held.collection), r.c.key == held.key).values(values))
    return await get(conn, reach, held.collection, held.key)


async def remove(
    conn: AsyncConnection, reach: Reach, collection: str, key: str, *, revision: int | None = None
) -> None:
    """Delete record ``key``; 404 when there is none or this call may not see it, 409 when ``revision`` is not its."""
    check_name(collection, key)
    held = await _held(conn, reach, collection, key)
    if held is None or not reach.sees(held):
        raise RecordError(404, f"no record {key} in collection {collection}")
    if revision is not None and revision != held.revision:
        raise RecordError(409, f"record {key} is at revision {held.revision}, not {revision}")
    r = tables.plugin_records
    await conn.execute(delete(r).where(_where(reach, collection), r.c.key == key))


__all__ = [
    "AUDIENCES",
    "COLLECTION",
    "KEY",
    "MAX_BODY_BYTES",
    "MAX_PAGE",
    "Reach",
    "RecordError",
    "get",
    "listing",
    "put",
    "remove",
]

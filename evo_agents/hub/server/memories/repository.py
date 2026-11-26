"""The memories in the database: tables memories and memory_revisions, the agent profiles whose namespaces they are
in, and what the Curator's promotion and the daily upkeep read and write of them.

The statements are built once, or once per shape, with bind parameters: the reads and writes of memories run on every
pull and push of every session, and building a statement on each call cost more than its round trip (docs/hub.md,
Data access). A shape is the set of filters a request gives (``reach``).
"""

from __future__ import annotations

import functools
import hashlib
import json
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    DateTime,
    Integer,
    Select,
    SmallInteger,
    String,
    and_,
    bindparam,
    func,
    insert,
    or_,
    select,
    true,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, tables_memory_agents
from evo_agents.hub.db import one_of
from evo_agents.hub.memory_files import SHARED_TYPES
from evo_agents.hub.server.memories.models import (
    BATCH,
    BOTH,
    INDEX_ROWS,
    LIVE,
    LOCK_CLASS,
    MAX_RUN_WRITES,
    NONE,
    OWN,
    PROMPT_SCAN_ROWS,
    SHARED,
    Memory,
    Row,
)

# What a memory shows of its upkeep


def state_columns() -> tuple:
    """The columns of memories a MemoryState shows."""
    m = tables.memories
    return m.c.read_at, m.c.archived_at, m.c.stale_at, m.c.promoted_at, m.c.promoted_into


FRESH = {"archived_at": None, "stale_at": None, "promoted_at": None, "promoted_into": None}  # what a new revision sets

_OPENED = (
    update(tables.memories)
    .values(read_at=func.now())
    .where(tables.memories.c.id == bindparam("opened_id", type_=BigInteger))
)


async def mark_opened(conn: AsyncConnection, memory_id: int) -> None:
    await conn.execute(_OPENED, {"opened_id": memory_id})


# The namespaces of agents and the provenance of a revision


def namespace_condition(namespace: str):
    """The condition on memories.profile_id of a listing or a search reaching ``namespace``, :namespace_id bound. The
    agent of a run (BOTH, LIVE) reaches none of its memories the daily upkeep archived or the Curator promoted."""
    m = tables.memories
    named = m.c.profile_id == bindparam("namespace_id", type_=BigInteger)
    live = and_(named, m.c.archived_at.is_(None), m.c.promoted_at.is_(None))
    none = m.c.profile_id.is_(None)
    return {NONE: none, OWN: named, LIVE: live, BOTH: or_(none, live)}[namespace]


def key_condition(kind: str):
    """The namespace of the memory at a key, by ``kind`` of key: an agent's (:profile_id), else none."""
    m = tables.memories
    if kind == "agent":
        return m.c.profile_id == bindparam("profile_id", type_=BigInteger)
    return m.c.profile_id.is_(None)


def key_kind(kind: str) -> ColumnElement[bool]:
    """What else the key of ``kind`` asks of the memory there: shared, the caller's (:user_id) own, or anything in the
    agent's namespace."""
    m = tables.memories
    if kind == "agent":
        return true()
    if kind == "shared":
        return one_of(m.c.type, tables_memory_agents.SHARED)
    return and_(one_of(m.c.type, tables_memory_agents.OWNED), m.c.owner_id == bindparam("user_id", type_=BigInteger))


async def profile(conn: AsyncConnection, handle: str) -> tuple[int, str, bool] | None:
    """The id, the project's name and whether it is the default of agent profile ``handle``; None for none."""
    ap, p = tables.agent_profiles, tables.projects
    found = select(ap.c.id, p.c.name, ap.c.is_default).join_from(ap, p, p.c.id == ap.c.project_id)
    row = (await conn.execute(found.where(ap.c.handle == handle))).first()
    return None if row is None else (row[0], row[1], row[2])


_WRITTEN = (
    select(tables.memory_revisions.c.memory_id)
    .distinct()
    .where(tables.memory_revisions.c.run_id == bindparam("run_id", type_=BigInteger))
    .limit(MAX_RUN_WRITES + 1)
)  # the memories run :run_id wrote


async def written_by_run(conn: AsyncConnection, run_id: int) -> set[int]:
    """The memories run ``run_id`` wrote, up to one more than it may."""
    return set((await conn.execute(_WRITTEN, {"run_id": run_id})).scalars())


# memory_revisions column: (parameter of the revision row, its type)
COLUMNS = {
    "run_id": ("prov_run", BigInteger),
    "profile_id": ("prov_profile", BigInteger),
    "profile_version": ("prov_version", Integer),
    "origin_id": ("prov_origin", BigInteger),
    "hop": ("prov_hop", SmallInteger),
    "chat_id": ("prov_chat", BigInteger),
    "chat_message_id": ("prov_message", BigInteger),
    "proposal_id": ("prov_proposal", BigInteger),
}
PARAMETERS = tuple(parameter for parameter, _ in COLUMNS.values())


def provenance_parameters() -> list:
    """The bind parameters of the provenance columns, in the order of COLUMNS."""
    return [bindparam(parameter, type_=kind) for parameter, kind in COLUMNS.values()]


def provenance_columns():
    """The columns of memory_revisions a revision's provenance shows, labelled ``prov_*``, and the joins they need
    from memory_revisions: (columns, join)."""
    r, ap = tables.memory_revisions, tables.agent_profiles
    origin = tables.users.alias("origin")
    columns = (
        r.c.run_id.label("prov_run"),
        ap.c.handle.label("prov_agent"),
        r.c.profile_version.label("prov_version"),
        origin.c.login.label("prov_origin"),
        r.c.hop.label("prov_hop"),
        r.c.chat_id.label("prov_chat"),
        r.c.chat_message_id.label("prov_message"),
        r.c.proposal_id.label("prov_proposal"),
    )

    def join(source):
        return source.outerjoin(ap, ap.c.id == r.c.profile_id).outerjoin(origin, origin.c.id == r.c.origin_id)

    return columns, join


# Reading memories


def memories(*extra) -> Select:
    """The memories with what a Memory shows, column by field name, then owner_id, project_id and ``extra``: the
    project's name, the logins of the owner and of the last writer and the handle of the agent joined in."""
    m, p, ap = tables.memories, tables.projects, tables.agent_profiles
    owner, writer = tables.users.alias("o"), tables.users.alias("u")
    return select(
        m.c.id,
        m.c.scope,
        p.c.name.label("project"),
        m.c.location,
        m.c.name,
        m.c.type,
        owner.c.login.label("owner"),
        ap.c.handle.label("agent"),
        m.c.label,
        m.c.body,
        m.c.revision,
        m.c.deleted,
        m.c.created_at,
        m.c.updated_at,
        writer.c.login.label("updated_by"),
        m.c.owner_id,
        m.c.project_id,
        m.c.profile_id,
        *state_columns(),
        *extra,
    ).select_from(
        m.outerjoin(p, p.c.id == m.c.project_id)
        .outerjoin(ap, ap.c.id == m.c.profile_id)
        .join(owner, owner.c.id == m.c.owner_id)
        .join(writer, writer.c.id == m.c.updated_by)
    )


def row_of(found) -> Row:
    """A row of ``memories()`` as the memory, its owner's, project's and agent's ids, by column name."""
    return Row(Memory(**found._mapping), found.owner_id, found.project_id, found.profile_id)


def reach(*, personal: bool, scope: bool, project: bool, location: bool, namespace: str) -> list:
    """The conditions on the memories that may be visible before labels count: one's (:user_id) personal memories
    when ``personal`` (never for the agent of a run) and, in the projects :reach_projects one holds a grant on, the
    shared memories, one's own and the agents' of ``namespace`` (``namespace_condition``); then the :scope,
    :project_id and :location the caller narrowed to, those the other flags name."""
    m = tables.memories
    user_id = bindparam("user_id", type_=BigInteger)
    in_projects = and_(
        m.c.scope == "project",
        one_of(m.c.project_id, name="reach_projects"),
        or_(one_of(m.c.type, SHARED), m.c.owner_id == user_id, m.c.profile_id.is_not(None)),
        namespace_condition(namespace),
    )
    candidate = or_(and_(m.c.scope == "personal", m.c.owner_id == user_id), in_projects) if personal else in_projects
    conditions = [candidate]
    if scope:
        conditions.append(m.c.scope == bindparam("scope", type_=String))
    if project:
        conditions.append(m.c.project_id == bindparam("project_id", type_=BigInteger))
    if location:
        conditions.append(m.c.location == bindparam("location", type_=String))
    return conditions


@functools.cache
def _listed(deleted: bool, **shape) -> Select:
    """A batch of the memories ``reach(**shape)`` selects after (:after_at, :after_id), in (updated_at, id)
    order; tombstones too when ``deleted``."""
    m = tables.memories
    query = memories().where(*reach(**shape))
    if not deleted:
        query = query.where(~m.c.deleted)
    after = tuple_(bindparam("after_at", type_=DateTime(timezone=True)), bindparam("after_id", type_=BigInteger))
    return query.where(tuple_(m.c.updated_at, m.c.id) > after).order_by(m.c.updated_at, m.c.id).limit(BATCH)


@functools.cache
def _searched(**shape) -> Select:
    """A batch of the live memories ``reach(**shape)`` selects whose name or body matches :q, best first, from
    row :offset."""
    m = tables.memories
    query = func.websearch_to_tsquery("simple", bindparam("q", type_=String)).column_valued("q")
    rank = func.ts_rank(m.c.search, query).label("rank")
    return (
        memories(rank)
        .where(m.c.search.bool_op("@@")(query), ~m.c.deleted, *reach(**shape))
        .order_by(rank.desc(), m.c.updated_at.desc(), m.c.id.desc())
        .limit(BATCH)
        .offset(bindparam("offset", type_=Integer))
    )


async def listed(conn: AsyncConnection, deleted: bool, shape: dict, bound: dict, after: tuple) -> list[Row]:
    """A batch of BATCH memories of ``shape`` with the parameters ``bound``, after (updated_at, id) ``after``."""
    query = _listed(deleted, **shape)
    rows = (await conn.execute(query, {**bound, "after_at": after[0], "after_id": after[1]})).all()
    return [row_of(values) for values in rows]


async def searched(conn: AsyncConnection, shape: dict, bound: dict, q: str, offset: int) -> list[tuple[Row, float]]:
    """A batch of BATCH memories of ``shape`` matching ``q``, best first, from row ``offset``, with their rank."""
    rows = (await conn.execute(_searched(**shape), {**bound, "q": q, "offset": offset})).all()
    return [(row_of(values), values.rank) for values in rows]


_GRANTED = (
    select(tables.projects.c.name)
    .join_from(tables.grants, tables.projects, tables.projects.c.id == tables.grants.c.project_id)
    .where(tables.grants.c.user_id == bindparam("user_id", type_=BigInteger))
    .order_by(tables.projects.c.name)
)  # the names of the projects :user_id holds a grant on


async def granted_projects(conn: AsyncConnection, user_id: int) -> list[str]:
    """The names of the projects ``user_id`` holds a grant on, by name."""
    return list((await conn.execute(_GRANTED, {"user_id": user_id})).scalars().all())


_LOADED = memories().where(tables.memories.c.id == bindparam("memory_id", type_=BigInteger))
_LOCKED = _LOADED.with_for_update(of=tables.memories)


async def load(conn: AsyncConnection, memory_id: int, *, lock: bool = False) -> Row | None:
    """Memory ``memory_id``, locked for the transaction when ``lock``; None for none."""
    found = (await conn.execute(_LOCKED if lock else _LOADED, {"memory_id": memory_id})).one_or_none()
    return row_of(found) if found else None


async def has_repo(conn: AsyncConnection, project_id: int, name: str) -> bool:
    repos = tables.project_repos
    found = select(repos.c.name).where(repos.c.project_id == project_id, repos.c.name == name)
    return (await conn.execute(found)).first() is not None


# The memory at a key


@functools.cache
def _at_key(kind: str) -> Select:
    """The memory at the key :scope, :project_id, :location and :name, locked: the shared one of the project when
    ``kind`` is shared, the caller's (:user_id) own when owned, the agent's (:profile_id) when agent."""
    m = tables.memories
    user_id = bindparam("user_id", type_=BigInteger)
    return (
        memories()
        .where(
            m.c.scope == bindparam("scope", type_=String),
            m.c.project_id.is_not_distinct_from(bindparam("project_id", type_=BigInteger)),
            m.c.location == bindparam("location", type_=String),
            m.c.name == bindparam("name", type_=String),
            key_condition(kind),
            or_(key_kind(kind), and_(m.c.scope == "personal", m.c.owner_id == user_id)),
        )
        .with_for_update(of=m)
    )


def key(
    scope: str, project_id: int | None, location: str, name: str, user_id: int, profile_id: int | None = None
) -> dict:
    """The parameters of the memory at a key, in namespace ``profile_id``."""
    return {
        "profile_id": profile_id,
        "scope": scope,
        "project_id": project_id,
        "location": location,
        "name": name,
        "user_id": user_id,
    }


async def at_key(conn: AsyncConnection, kind: str, params: dict) -> Row | None:
    """The memory at the key ``params`` (``key``) names for a key of ``kind``, locked; None for none."""
    found = (await conn.execute(_at_key(kind), params)).one_or_none()
    return row_of(found) if found else None


_LOCK = select(func.pg_advisory_xact_lock(LOCK_CLASS, bindparam("lock_key", type_=Integer)))


async def lock_key(
    conn: AsyncConnection,
    scope: str,
    kind: str,
    location: str,
    name: str,
    user_id: int,
    project_id: int | None,
    profile_id: int | None,
) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the memory at a key."""
    owner = user_id if scope == "personal" or kind not in SHARED_TYPES else 0
    place = [scope, project_id, owner, location, name] + ([profile_id] if profile_id else [])
    lock = int.from_bytes(hashlib.sha256(json.dumps(place).encode()).digest()[:4], "big", signed=True)
    await conn.execute(_LOCK, {"lock_key": lock})


# Writing memories


async def insert_memory(conn: AsyncConnection, values: dict) -> int:
    """Write a new memory of ``values`` at its first revision; its id."""
    m = tables.memories
    return (await conn.execute(insert(m).values(**values).returning(m.c.id))).scalar_one()


# The next revision of memory :memory_id, written by :writer_id. The parameters name no column of memories: an
# UPDATE would take a parameter named after a column as a value to set.
CHANGED = (
    update(tables.memories)
    .values(
        type=bindparam("new_type", type_=String),
        label=bindparam("new_label", type_=JSONB),
        body=bindparam("new_body", type_=String),
        deleted=bindparam("new_deleted", type_=Boolean),
        revision=tables.memories.c.revision + 1,
        updated_at=func.now(),
        updated_by=bindparam("writer_id", type_=BigInteger),
        **FRESH,
    )
    .where(tables.memories.c.id == bindparam("memory_id", type_=BigInteger))
)


async def change(
    conn: AsyncConnection, memory_id: int, kind: str, label: dict, body: str, deleted: bool, user_id: int
) -> None:
    """Write the next revision of memory ``memory_id`` (CHANGED)."""
    params = {
        "memory_id": memory_id,
        "new_type": kind,
        "new_label": label,
        "new_body": body,
        "new_deleted": deleted,
        "writer_id": user_id,
    }
    await conn.execute(CHANGED, params)


# The revision row of memory :written_id as it now is, by :actor, with its provenance; the parameters name no column
# of memory_revisions.
REVISION = insert(tables.memory_revisions).from_select(
    ["memory_id", "revision", "type", "label", "body", "deleted", "actor_id", *COLUMNS],
    select(
        tables.memories.c.id,
        tables.memories.c.revision,
        tables.memories.c.type,
        tables.memories.c.label,
        tables.memories.c.body,
        tables.memories.c.deleted,
        bindparam("actor", type_=BigInteger),
        *provenance_parameters(),
    ).where(tables.memories.c.id == bindparam("written_id", type_=BigInteger)),
)


async def add_revision(conn: AsyncConnection, memory_id: int, actor: int, provenance: dict) -> None:
    """The revision row of memory ``memory_id`` as it now is, by ``actor``, with ``provenance`` (``prov_*``)."""
    await conn.execute(REVISION, {"written_id": memory_id, "actor": actor, **provenance})


# The revisions of a memory


def _revisions(*extra) -> Select:
    """The revisions of memories with what a RevisionSummary shows, column by field name, its provenance
    (``provenance_columns``), then ``extra``."""
    r, u = tables.memory_revisions, tables.users
    provenance, joined = provenance_columns()
    return select(
        r.c.revision,
        r.c.type,
        r.c.label,
        r.c.deleted,
        func.octet_length(r.c.body).label("size"),
        u.c.login.label("actor"),
        r.c.created_at,
        *provenance,
        *extra,
    ).select_from(joined(r.join(u, u.c.id == r.c.actor_id)))


async def history(conn: AsyncConnection, memory_id: int, before: int | None) -> list:
    """A batch of the revisions of memory ``memory_id`` older than ``before``, the latest first."""
    r = tables.memory_revisions
    query = _revisions().where(r.c.memory_id == memory_id)
    if before is not None:
        query = query.where(r.c.revision < before)
    return list((await conn.execute(query.order_by(r.c.revision.desc()).limit(BATCH))).all())


async def revision(conn: AsyncConnection, memory_id: int, number: int):
    """Revision ``number`` of memory ``memory_id`` with its body; None for none."""
    r = tables.memory_revisions
    query = _revisions(r.c.body).where(r.c.memory_id == memory_id, r.c.revision == number)
    return (await conn.execute(query)).one_or_none()


# What the Curator's promotion reads and writes


def _changed_memories():
    m, r = tables.memories, tables.memory_revisions
    namespace = tables.agent_profiles.alias("namespace")
    provenance, joined = provenance_columns()
    source = joined(
        m.join(r, and_(r.c.memory_id == m.c.id, r.c.revision == m.c.revision)).join(
            namespace, namespace.c.id == m.c.profile_id
        )
    )
    since = bindparam("since", type_=DateTime(timezone=True))
    return (
        select(m.c.id, m.c.revision, m.c.location, m.c.name, m.c.type, m.c.label, m.c.body, namespace.c.handle)
        .add_columns(*provenance)
        .select_from(source)
        .where(
            m.c.project_id == bindparam("project_id", type_=BigInteger),
            m.c.profile_id.is_not(None),
            ~m.c.deleted,
            m.c.archived_at.is_(None),
            m.c.promoted_at.is_(None),
            func.coalesce(m.c.updated_at > since, True),
        )
        .order_by(m.c.updated_at.desc(), m.c.id.desc())
        .limit(PROMPT_SCAN_ROWS)
    )


async def changed_agent_memories(conn: AsyncConnection, project_id: int, since: datetime | None) -> list:
    """The live agents' memories of the project written since ``since`` (all for None), newest first, each at its
    revision with its provenance (``prov_*``) and the agent's handle."""
    return list((await conn.execute(_changed_memories(), {"project_id": project_id, "since": since})).all())


def _shared_memories():
    m = tables.memories
    return (
        select(m.c.id, m.c.revision, m.c.location, m.c.name, m.c.type, m.c.label, m.c.body)
        .where(
            m.c.project_id == bindparam("project_id", type_=BigInteger),
            m.c.scope == "project",
            m.c.profile_id.is_(None),
            m.c.type.in_(SHARED),
            ~m.c.deleted,
        )
        .order_by(m.c.location, m.c.name)
        .limit(PROMPT_SCAN_ROWS)
    )


async def shared_memories(conn: AsyncConnection, project_id: int) -> list:
    """The project's live shared memories (of no agent, type project or reference), by place."""
    return list((await conn.execute(_shared_memories(), {"project_id": project_id})).all())


async def last_review(conn: AsyncConnection, run_id: int, project_id: int) -> datetime | None:
    """When the project's last review run before ``run_id`` that ended done was claimed, which built its prompt; None
    before the first."""
    r = tables.runs
    query = select(func.max(r.c.leased_at)).where(
        r.c.project_id == project_id, r.c.kind == "review", r.c.state == "done", r.c.id < run_id
    )
    return (await conn.execute(query)).scalar_one_or_none()


async def auto_merge(conn: AsyncConnection, project_id: int) -> list:
    """The tiers the auto_merge of the project's newest charter names."""
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    body = (await conn.execute(query)).scalar_one_or_none() or {}
    return list(body.get("auto_merge") or [])


async def update_proposal(conn: AsyncConnection, proposal_id: int, values: dict) -> None:
    p = tables.proposals
    await conn.execute(update(p).values(**values).where(p.c.id == proposal_id))


async def mark_promoted(conn: AsyncConnection, memory_ids: list[int], into: int) -> None:
    """Mark the memories ``memory_ids`` promoted into memory ``into`` now."""
    m = tables.memories
    await conn.execute(update(m).values(promoted_at=func.now(), promoted_into=into).where(m.c.id.in_(memory_ids)))


async def revision_of(conn: AsyncConnection, memory_id: int) -> int:
    m = tables.memories
    return (await conn.execute(select(m.c.revision).where(m.c.id == memory_id))).scalar_one()


# The index of an agent's memories and their upkeep

_INDEX_RUN = (
    select(
        tables.agent_profiles.c.id.label("profile_id"),
        tables.agent_profiles.c.handle,
        tables.runs.c.kind,
        tables.projects.c.name.label("project"),
        tables.users.c.id.label("owner_id"),
        tables.users.c.login,
    )
    .join_from(tables.runs, tables.agent_profiles, tables.agent_profiles.c.id == tables.runs.c.profile_id)
    .join(tables.projects, tables.projects.c.id == tables.runs.c.project_id)
    .join(tables.users, tables.users.c.id == tables.runs.c.dispatched_by)
    .where(tables.runs.c.id == bindparam("run_id", type_=BigInteger), ~tables.agent_profiles.c.is_default)
)  # the profile of run :run_id, when not its project's default, its project and the person who dispatched it

INDEXED = (
    select(
        tables.memories.c.id,
        tables.memories.c.name,
        tables.memories.c.type,
        tables.memories.c.label,
        tables.memories.c.body,
        tables.memories.c.stale_at,
    )
    .where(
        tables.memories.c.profile_id == bindparam("profile_id", type_=BigInteger),
        ~tables.memories.c.deleted,
        tables.memories.c.archived_at.is_(None),
        tables.memories.c.promoted_at.is_(None),
    )
    .order_by(tables.memories.c.updated_at.desc(), tables.memories.c.id.desc())
    .limit(INDEX_ROWS)
)  # the memories of profile :profile_id an index may list, newest first


async def index_run(conn: AsyncConnection, run_id: int):
    """The profile of run ``run_id`` when it is not its project's default, its kind, project and owner; None else."""
    return (await conn.execute(_INDEX_RUN, {"run_id": run_id})).one_or_none()


async def indexed(conn: AsyncConnection, profile_id: int) -> list:
    """The memories of profile ``profile_id`` an index may list, newest first (INDEXED)."""
    return list((await conn.execute(INDEXED, {"profile_id": profile_id})).all())


_LIVE = (
    tables.memories.c.profile_id.is_not(None),
    ~tables.memories.c.deleted,
    tables.memories.c.promoted_at.is_(None),
)  # the memories of agents, neither deleted nor promoted

_ARCHIVED = (
    update(tables.memories)
    .values(archived_at=bindparam("now", type_=DateTime(timezone=True)))
    .where(
        *_LIVE,
        tables.memories.c.archived_at.is_(None),
        func.coalesce(tables.memories.c.read_at, tables.memories.c.created_at)
        < bindparam("cutoff", type_=DateTime(timezone=True)),
        tables.memories.c.updated_at < bindparam("cutoff", type_=DateTime(timezone=True)),
    )
)  # the memories of agents nobody opened or wrote since :cutoff, archived at :now

_CITING = select(
    tables.memories.c.id,
    tables.memories.c.project_id,
    tables.memories.c.owner_id,
    tables.memories.c.body,
    tables.memories.c.stale_at,
).where(*_LIVE)

_MARKED = (
    update(tables.memories)
    .values(stale_at=bindparam("stale", type_=DateTime(timezone=True)))
    .where(tables.memories.c.id == bindparam("marked_id", type_=BigInteger))
)

_RESTORED = (
    update(tables.memories)
    .values(archived_at=None, read_at=func.now())
    .where(tables.memories.c.id == bindparam("restored_id", type_=BigInteger))
)


async def archive(conn: AsyncConnection, now: datetime, cutoff: datetime) -> int:
    """Archive, at ``now``, the memories of agents nobody opened or wrote since ``cutoff``; how many."""
    return (await conn.execute(_ARCHIVED, {"now": now, "cutoff": cutoff})).rowcount


async def citing(conn: AsyncConnection) -> list:
    """The live memories of agents with their project, owner, body and stale mark."""
    return list((await conn.execute(_CITING)).all())


async def mark_stale(conn: AsyncConnection, marks: list[dict]) -> None:
    """Set the stale mark of each memory of ``marks`` (``marked_id``, ``stale``: a time or None)."""
    await conn.execute(_MARKED, marks)


async def restore(conn: AsyncConnection, memory_id: int) -> None:
    """Bring back archived memory ``memory_id``, as if just opened."""
    await conn.execute(_RESTORED, {"restored_id": memory_id})

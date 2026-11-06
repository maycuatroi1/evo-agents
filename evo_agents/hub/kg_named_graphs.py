"""The named graphs of a hub project (``docs/kg.md``, Named graphs): rows of kg_named_graphs, each a name and a set of
the project's sources. The kg tools read one by narrowing the project's one build to those sources at query time
(``evo_agents.kg.graph_scope``); ``default``, every source, has no row. ``sources_of`` is what a kg tool call with
``graph`` resolves; the routes are ``evo_agents.hub.server.kg_graphs``. No SQL is written by hand: every statement is
built with SQLAlchemy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_kg import GRAPH_NAME_PATTERN
from evo_agents.kg.graph_scope import DEFAULT_GRAPH

MAX_GRAPHS = 50  # named graphs a project holds
MAX_SOURCES = 200  # sources a named graph holds
MAX_DESCRIPTION_CHARS = 500
MAX_SOURCE_CHARS = 200
SOURCE_PATTERN = r"^[^\x00-\x1f\x7f/]+$"  # a source id, as `hub kg upload` takes it
_NAME = re.compile(GRAPH_NAME_PATTERN)
_SOURCE = re.compile(SOURCE_PATTERN)


class GraphError(ValueError):
    """A named graph that cannot be written as asked: its message says why (a 422 or a 409 on the api)."""


class GraphExists(GraphError):
    pass


class NoSuchGraph(LookupError):
    pass


@dataclass(frozen=True)
class NamedGraph:
    name: str
    description: str
    sources: list[str]
    created_by: int | None
    updated_by: int | None
    created_at: datetime
    updated_at: datetime


def check_name(name: str) -> str:
    """``name`` when it can name a graph; GraphError otherwise (``default`` is the implicit graph of every source)."""
    if name == DEFAULT_GRAPH:
        raise GraphError(f"'{DEFAULT_GRAPH}' is the graph of every source of the project; pick another name")
    if not _NAME.match(name):
        raise GraphError(
            f"a graph name is 1 to 63 of a-z, 0-9, '.', '_' and '-', starting with a letter or a digit: {name!r}"
        )
    return name


def check_sources(sources: list[str]) -> list[str]:
    """``sources`` without repeats, in their order, when each can be a source id; GraphError otherwise."""
    found: list[str] = []
    for source in sources:
        if not isinstance(source, str) or not 0 < len(source) <= MAX_SOURCE_CHARS or not _SOURCE.match(source):
            raise GraphError(f"not a source id: {source!r}")
        if source not in found:
            found.append(source)
    if len(found) > MAX_SOURCES:
        raise GraphError(f"a named graph holds at most {MAX_SOURCES} sources, not {len(found)}")
    return found


def _row(row) -> NamedGraph:
    return NamedGraph(
        name=row.name,
        description=row.description,
        sources=list(row.sources or []),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _columns():
    t = tables.kg_named_graphs
    return (t.c.name, t.c.description, t.c.sources, t.c.created_by, t.c.updated_by, t.c.created_at, t.c.updated_at)


async def listed(conn: AsyncConnection, project_id: int) -> list[NamedGraph]:
    """The named graphs of the project, by name."""
    t = tables.kg_named_graphs
    rows = await conn.execute(select(*_columns()).where(t.c.project_id == project_id).order_by(t.c.name))
    return [_row(row) for row in rows]


async def one(conn: AsyncConnection, project_id: int, name: str) -> NamedGraph:
    """The named graph ``name`` of the project; NoSuchGraph when it has none."""
    t = tables.kg_named_graphs
    row = (await conn.execute(select(*_columns()).where(t.c.project_id == project_id, t.c.name == name))).first()
    if row is None:
        raise NoSuchGraph(name)
    return _row(row)


async def sources_of(conn: AsyncConnection, project_id: int, name: str) -> frozenset[str] | None:
    """The sources of the named graph ``name`` of the project, or None when it has no graph of that name."""
    t = tables.kg_named_graphs
    found = (
        await conn.execute(select(t.c.sources).where(t.c.project_id == project_id, t.c.name == name))
    ).scalar_one_or_none()
    return None if found is None else frozenset(found)


async def built_sources(conn: AsyncConnection, project_id: int) -> list[str]:
    """The sources the project's newest successful build read (its coverage), by id."""
    builds = tables.kg_builds
    coverage = (
        await conn.execute(
            select(builds.c.coverage)
            .where(builds.c.project_id == project_id, builds.c.status == "succeeded")
            .order_by(builds.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    entries = (coverage or {}).get("sources") or [] if isinstance(coverage, dict) else []
    return sorted({e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)})


async def create(
    conn: AsyncConnection, project_id: int, name: str, sources: list[str], description: str, user_id: int | None
) -> NamedGraph:
    """Add the named graph ``name``; GraphExists when the project has one of that name, GraphError when it holds
    ``MAX_GRAPHS`` already or a field is not valid."""
    check_name(name)
    sources = check_sources(sources)
    t = tables.kg_named_graphs
    held = (await conn.execute(select(func.count()).where(t.c.project_id == project_id))).scalar_one()
    if held >= MAX_GRAPHS:
        raise GraphError(f"a project holds at most {MAX_GRAPHS} named graphs")
    made = await conn.execute(
        pg_insert(t)
        .values(
            project_id=project_id,
            name=name,
            description=description,
            sources=sources,
            created_by=user_id,
            updated_by=user_id,
        )
        .on_conflict_do_nothing(constraint="kg_named_graphs_project_id_name_key")
        .returning(t.c.id)
    )
    if made.first() is None:
        raise GraphExists(f"the project has a graph named {name} already")
    return await one(conn, project_id, name)


async def change(
    conn: AsyncConnection,
    project_id: int,
    name: str,
    *,
    rename: str | None = None,
    description: str | None = None,
    sources: list[str] | None = None,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    user_id: int | None,
) -> NamedGraph:
    """Rename the graph ``name``, set its description, set its sources (``sources``) or give it ``add`` and strip it of
    ``remove``, in that order. NoSuchGraph, GraphExists for a new name taken, GraphError for a field not valid."""
    t = tables.kg_named_graphs
    row = (
        await conn.execute(
            select(t.c.id, t.c.sources).where(t.c.project_id == project_id, t.c.name == name).with_for_update()
        )
    ).first()
    if row is None:
        raise NoSuchGraph(name)
    held = list(row.sources or []) if sources is None else check_sources(sources)
    held = check_sources(held + [s for s in check_sources(add or []) if s not in held])
    gone = set(check_sources(remove or []))
    values: dict = {"sources": [s for s in held if s not in gone], "updated_by": user_id, "updated_at": func.now()}
    if description is not None:
        values["description"] = description
    if rename is not None and rename != name:
        check_name(rename)
        taken = await conn.execute(select(t.c.id).where(t.c.project_id == project_id, t.c.name == rename))
        if taken.first() is not None:
            raise GraphExists(f"the project has a graph named {rename} already")
        values["name"] = rename
    await conn.execute(update(t).where(t.c.id == row.id).values(**values))
    return await one(conn, project_id, values.get("name", name))


async def remove(conn: AsyncConnection, project_id: int, name: str) -> None:
    """Delete the named graph ``name``; NoSuchGraph when the project has none. The build and the sources stay."""
    t = tables.kg_named_graphs
    gone = await conn.execute(delete(t).where(t.c.project_id == project_id, t.c.name == name).returning(t.c.id))
    if gone.first() is None:
        raise NoSuchGraph(name)

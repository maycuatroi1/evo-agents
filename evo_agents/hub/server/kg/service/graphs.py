"""A project's named graphs (``evo_agents.hub.server.kg_graphs``): who may change them, and the transaction of each
read and write with its audit row. ``evo_agents.hub.kg_named_graphs`` keeps their rows."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_named_graphs as store
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.graphs import NamedGraphCreate, NamedGraphOut, NamedGraphsOut, NamedGraphUpdate
from evo_agents.hub.server.kg.repository.settings import logins
from evo_agents.hub.server.kg.service import settings
from evo_agents.hub.server.kg.service.web import require_reader
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg.graph_scope import DEFAULT_GRAPH


async def may_change(conn: AsyncConnection, user, access) -> None:
    """The writer role, and for a run's token a run of this project, as for the KG settings."""
    await settings.may_change(conn, user, access, "the named graphs")


def _out(graph: store.NamedGraph, built: list[str], names: dict[int, str]) -> NamedGraphOut:
    held = set(built)
    return NamedGraphOut(
        name=graph.name,
        description=graph.description,
        sources=graph.sources,
        default=False,
        missing=[s for s in graph.sources if s not in held] if built else [],
        created_by=names.get(graph.created_by) if graph.created_by is not None else None,
        updated_by=names.get(graph.updated_by) if graph.updated_by is not None else None,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


async def _one_out(conn: AsyncConnection, project_id: int, graph: store.NamedGraph) -> NamedGraphOut:
    built = await store.built_sources(conn, project_id)
    names = await logins(conn, {i for i in (graph.created_by, graph.updated_by) if i is not None})
    return _out(graph, built, names)


def _refused(e: Exception, project: str, name: str) -> HTTPException:
    if isinstance(e, store.NoSuchGraph):
        return HTTPException(404, f"project {project} has no graph named {name}: `evo-agents hub kg graph list`")
    if isinstance(e, store.GraphExists):
        return HTTPException(409, str(e))
    return HTTPException(422, str(e))


async def _audit(conn: AsyncConnection, user, access, project: str, name: str, kind: str, sources: int) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_GRAPH,
        target=f"{project} graph={name} kind={kind} sources={sources}",
        project_id=access.project_id,
    )


async def list_graphs(engine: AsyncEngine, user, project: str) -> NamedGraphsOut:
    """``default`` and the named graphs of the project, with the sources its newest build read, for a reader."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        found = await store.listed(conn, access.project_id)
        built = await store.built_sources(conn, access.project_id)
        names = await logins(conn, {i for g in found for i in (g.created_by, g.updated_by) if i is not None})
    default = NamedGraphOut(name=DEFAULT_GRAPH, description="every source of the project", sources=[], default=True)
    return NamedGraphsOut(
        graphs=[default, *(_out(g, built, names) for g in found)],
        sources=built,
        can_change=holds(access.role, "writer"),
    )


async def create_graph(engine: AsyncEngine, user, project: str, body: NamedGraphCreate) -> NamedGraphOut:
    """Add the named graph of ``body``; 409 when the name is taken, 422 for a bad name or source."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            made = await store.create(
                conn, access.project_id, body.name, list(body.sources), body.description, user.user_id
            )
        except (store.GraphError, store.NoSuchGraph) as e:
            raise _refused(e, project, body.name) from e
        await _audit(conn, user, access, project, made.name, "create", len(made.sources))
        return await _one_out(conn, access.project_id, made)


async def update_graph(engine: AsyncEngine, user, project: str, name: str, body: NamedGraphUpdate) -> NamedGraphOut:
    """Change the named graph ``name`` as ``body`` says; 404 without it."""
    if name == DEFAULT_GRAPH:
        raise HTTPException(422, f"'{DEFAULT_GRAPH}' is every source of the project and cannot change")
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            made = await store.change(
                conn,
                access.project_id,
                name,
                rename=body.rename,
                description=body.description,
                sources=None if body.sources is None else list(body.sources),
                add=list(body.add),
                remove=list(body.remove),
                user_id=user.user_id,
            )
        except (store.GraphError, store.NoSuchGraph) as e:
            raise _refused(e, project, name) from e
        kind = "update" if made.name == name else f"update renamed={name}"
        await _audit(conn, user, access, project, made.name, kind, len(made.sources))
        return await _one_out(conn, access.project_id, made)


async def delete_graph(engine: AsyncEngine, user, project: str, name: str) -> None:
    """Take the named graph ``name`` away; 404 without it."""
    if name == DEFAULT_GRAPH:
        raise HTTPException(422, f"'{DEFAULT_GRAPH}' is every source of the project and cannot be deleted")
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            await store.remove(conn, access.project_id, name)
        except store.NoSuchGraph as e:
            raise _refused(e, project, name) from e
        await _audit(conn, user, access, project, name, "delete", 0)

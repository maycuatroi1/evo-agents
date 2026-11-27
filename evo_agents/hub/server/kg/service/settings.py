"""A project's KG settings (``evo_agents.hub.server.kg_settings``): who may change them, the transaction of a change or
an undo with its audit row, and what a measure of a candidate reads before it asks the graph."""

from __future__ import annotations

from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_settings as store
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.graph import GRAPHS_TRIED
from evo_agents.hub.server.kg.models.settings import (
    MAX_FIGURES_CHARS,
    ChangeOut,
    SetBody,
    SettingsOut,
)
from evo_agents.hub.server.kg.repository import settings as repository
from evo_agents.hub.server.kg.repository.graph import built_graphs
from evo_agents.hub.server.kg.service.eval_sets import latest_set
from evo_agents.hub.server.kg.service.graph import member
from evo_agents.hub.server.kg.service.web import require_reader
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg import settings as kg_settings


async def may_change(conn: AsyncConnection, user, access, what: str = "the KG settings") -> None:
    """The writer role, and for a run's token a run of this project; ``what`` names what is changed."""
    require_reader(access)
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"changing {what} of project {access.name} needs the writer role on it")
    run = user.acts_for_run
    if run is not None and await repository.run_project(conn, run) != access.project_id:
        raise HTTPException(403, f"run {run} is not a run of project {access.name}")


async def _out(conn: AsyncConnection, project_id: int, role, *, needs_rebuild: list[str]) -> SettingsOut:
    tuning = await store.read(conn, project_id)
    found = await store.changes(conn, project_id)
    logins = await repository.logins(conn, {c.changed_by for c in found if c.changed_by is not None})
    return SettingsOut(
        values=tuning.to_json(),
        overrides=tuning.overrides(),
        defaults=kg_settings.DEFAULT.to_json(),
        needs_rebuild=needs_rebuild,
        changes=[
            ChangeOut(
                id=c.id,
                kind=c.kind,
                changed_by=logins.get(c.changed_by) if c.changed_by is not None else None,
                run_id=c.run_id,
                reason=c.reason,
                figures=c.figures,
                before=c.before,
                after=c.after,
                undoes=c.undoes,
                changed_at=c.changed_at,  # pyright: ignore[reportArgumentType]
            )
            for c in found
        ],
        can_change=holds(role, "writer"),
    )


async def _audit(conn: AsyncConnection, user, access, project: str, made: int, kind: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=audit.KG_SETTINGS,
        target=f"{project} change={made} kind={kind}",
        project_id=access.project_id,
    )


async def read_settings(engine: AsyncEngine, user, project: str) -> SettingsOut:
    """What the project is tuned with and the newest changes, for a reader."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        require_reader(access)
        return await _out(conn, access.project_id, access.role, needs_rebuild=[])


async def set_settings(engine: AsyncEngine, user, project: str, body: SetBody) -> SettingsOut:
    """Set ``body.values`` on top of what the project has, with who, why and the figures cited; 422 for a bad key."""
    if body.figures is not None and len(repr(body.figures)) > MAX_FIGURES_CHARS:
        raise HTTPException(422, f"figures are over {MAX_FIGURES_CHARS} characters")
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            before, after, made = await store.change(
                conn,
                access.project_id,
                body.values,
                user_id=user.user_id,
                run_id=user.acts_for_run,
                reason=body.reason,
                figures=body.figures,
            )
        except kg_settings.SettingsError as e:
            raise HTTPException(422, str(e)) from e
        await _audit(conn, user, access, project, made, "set")
        return await _out(conn, access.project_id, access.role, needs_rebuild=kg_settings.rebuild_keys(before, after))


async def undo_settings(engine: AsyncEngine, user, project: str, reason: str | None) -> SettingsOut:
    """Put back what the newest change that is not undone replaced; 404 when there is none."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        await may_change(conn, user, access)
        try:
            before, after, made = await store.undo(
                conn, access.project_id, user_id=user.user_id, run_id=user.acts_for_run, reason=reason
            )
        except store.NothingToUndo as e:
            raise HTTPException(404, f"project {access.name} has no change of its KG settings to undo") from e
        await _audit(conn, user, access, project, made, "undo")
        return await _out(conn, access.project_id, access.role, needs_rebuild=kg_settings.rebuild_keys(before, after))


class Measuring(NamedTuple):
    """What a measure asks the graph with: the caller's access, the project's kept graphs, its question set and the
    caller's audience (both only with a graph), the project's settings and the candidate's query keys on top."""

    access: ProjectAccess
    graphs: list
    found_set: tuple | None
    audience: frozenset
    current: kg_settings.Tuning
    candidate: kg_settings.Tuning
    needs_rebuild: list[str]


async def measuring(engine: AsyncEngine, user, project: str, values: dict) -> Measuring:
    """Read what a measure of ``values`` needs, for a member; 422 for a bad key. Nothing is stored."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        member(access)
        current = await store.read(conn, access.project_id)
        try:
            wanted = kg_settings.of(values, current)
        except kg_settings.SettingsError as e:
            raise HTTPException(422, str(e)) from e
        graphs = await built_graphs(conn, access.project_id, GRAPHS_TRIED)
        found_set = await latest_set(conn, access.project_id) if graphs else None
        audience = await reader_audience(conn, user) if graphs else frozenset()
    needs = kg_settings.rebuild_keys(current, wanted)
    # what a graph already built can be asked with: the query keys of the candidate; its build keys stay as they are
    kept = {k: getattr(wanted, k) for k in kg_settings.KEYS if k not in kg_settings.BUILD_KEYS}
    if "semantic_mode" in needs:
        kept["semantic_mode"] = current.semantic_mode
    candidate = kg_settings.of(kept, current)
    return Measuring(access, graphs, found_set, audience, current, candidate, needs)

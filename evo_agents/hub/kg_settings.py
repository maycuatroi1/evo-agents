"""The KG settings of a hub project (``evo_agents.kg.settings``): its row of kg_settings holds the overrides, its row of
kg_semantic holds the semantic mode and model, and every change is a row of kg_settings_changes with who made it (a
member, or the run of an agent), when, why, the figures it cites and the values before and after, so one command puts
the last change back. A project with no row is tuned by the defaults. ``read`` is what a build and ``kg_search`` use.
No SQL is written by hand: every statement is built with SQLAlchemy."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_semantic, tables
from evo_agents.kg import semantic
from evo_agents.kg import settings as kg_settings

MAX_FIGURES_CHARS = 4000
HISTORY_LIMIT = 20


class NothingToUndo(Exception):
    pass


@dataclass(frozen=True)
class Change:
    id: int
    kind: str
    changed_by: int | None
    run_id: int | None
    reason: str | None
    figures: dict | None
    before: dict
    after: dict
    undoes: int | None
    changed_at: object


async def read(conn: AsyncConnection, project_id: int) -> kg_settings.Tuning:
    """The project's tuning: the defaults, its kg_semantic row, its kg_settings row."""
    row = await conn.execute(
        select(tables.kg_settings.c.overrides).where(tables.kg_settings.c.project_id == project_id)
    )
    held = row.scalar() or {}
    chosen = await kg_semantic.read(conn, project_id)
    base = kg_settings.of({"semantic_mode": chosen.mode, "semantic_model": chosen.model})
    try:
        return kg_settings.of(held, base)
    except kg_settings.SettingsError:  # a row written by a later version: its known keys, the rest default
        known = {k: v for k, v in held.items() if k in kg_settings.KEYS}
        return kg_settings.of(known, base)


async def changes(conn: AsyncConnection, project_id: int, limit: int = HISTORY_LIMIT) -> list[Change]:
    t = tables.kg_settings_changes
    rows = await conn.execute(select(t).where(t.c.project_id == project_id).order_by(t.c.id.desc()).limit(limit))
    return [
        Change(r.id, r.kind, r.changed_by, r.run_id, r.reason, r.figures, r.before, r.after, r.undoes, r.changed_at)
        for r in rows
    ]


async def _store(
    conn: AsyncConnection,
    project_id: int,
    before: kg_settings.Tuning,
    after: kg_settings.Tuning,
    *,
    kind: str,
    user_id: int | None,
    run_id: int | None,
    reason: str | None,
    figures: dict | None,
    undoes: int | None,
) -> int:
    rest = {k: v for k, v in after.overrides().items() if k not in kg_settings.SEMANTIC_KEYS}
    s = tables.kg_settings
    values = {"overrides": rest, "updated_by": user_id, "updated_run_id": run_id}
    from sqlalchemy import func

    await conn.execute(
        pg_insert(s)
        .values(project_id=project_id, **values)
        .on_conflict_do_update(index_elements=[s.c.project_id], set_={**values, "updated_at": func.now()})
    )
    if (before.semantic_mode, before.semantic_model) != (after.semantic_mode, after.semantic_model):
        await kg_semantic.write(conn, project_id, semantic.Settings(after.semantic_mode, after.semantic_model), user_id)
    c = tables.kg_settings_changes
    made = await conn.execute(
        c.insert()
        .values(
            project_id=project_id,
            kind=kind,
            changed_by=user_id,
            run_id=run_id,
            reason=reason,
            figures=figures,
            before=before.to_json(),
            after=after.to_json(),
            undoes=undoes,
        )
        .returning(c.c.id)
    )
    return made.scalar_one()


async def change(
    conn: AsyncConnection,
    project_id: int,
    updates: dict,
    *,
    user_id: int | None,
    run_id: int | None = None,
    reason: str | None = None,
    figures: dict | None = None,
) -> tuple[kg_settings.Tuning, kg_settings.Tuning, int]:
    """Set ``updates`` (keys of ``kg.settings``) on top of what the project has: (before, after, the change's id).
    Raises ``kg_settings.SettingsError`` for an unknown key or a value out of range, before anything is written."""
    before = await read(conn, project_id)
    after = kg_settings.of(updates, before)
    made = await _store(
        conn,
        project_id,
        before,
        after,
        kind="set",
        user_id=user_id,
        run_id=run_id,
        reason=reason,
        figures=figures,
        undoes=None,
    )
    return before, after, made


async def undo(
    conn: AsyncConnection, project_id: int, *, user_id: int | None, run_id: int | None = None, reason: str | None = None
) -> tuple[kg_settings.Tuning, kg_settings.Tuning, int]:
    """Put back what the newest change that is not undone replaced, as a change of its own that names it; raises
    NothingToUndo when there is none."""
    found = await changes(conn, project_id, limit=1000)
    undone = {c.undoes for c in found if c.undoes is not None}
    target = next((c for c in found if c.kind == "set" and c.id not in undone), None)
    if target is None:
        raise NothingToUndo
    before = await read(conn, project_id)
    after = kg_settings.of(target.before)
    made = await _store(
        conn,
        project_id,
        before,
        after,
        kind="undo",
        user_id=user_id,
        run_id=run_id,
        reason=reason or f"undo of change {target.id}",
        figures=None,
        undoes=target.id,
    )
    return before, after, made

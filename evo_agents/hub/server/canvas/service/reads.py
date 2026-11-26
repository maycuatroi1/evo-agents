"""Who may do what with the canvases of a project (``reader``, ``writer``, ``admin``), and what a member reads of them:
the canvases they see (``canvases``), one canvas (``canvas``) and one claim of it (``claim``).

A canvas carries a label (the project's default, or one an admin gave it when it was made), and every row of it
carries the same; a canvas is read under that label through the sink X-Evo-Sink names, else the project's hub sink, as
plans are read (``ProjectAccess.visible``). A canvas the member cannot see answers the 404 of one that does not exist.
A piece of evidence is read under its own label, so one above what the member may read is counted, never shown.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.access import has_role
from evo_agents.hub.server.canvas.models.content import CanvasError, code
from evo_agents.hub.server.canvas.models.rows import CanvasRow, ClaimRow
from evo_agents.hub.server.canvas.repository import store
from evo_agents.hub.server.projects import ProjectAccess

# Who may do what


def reader(access: ProjectAccess) -> None:
    """Reading needs a grant on the project (a hub admin without one gets this far only)."""
    if access.role is None:
        raise CanvasError(403, f"reading the canvases of project {access.name} needs a grant on it")


def writer(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "writer"):
        raise CanvasError(403, f"{what} in project {access.name} needs the writer role; you are {access.role}")


def admin(access: ProjectAccess, what: str) -> None:
    reader(access)
    if not has_role(access.role, "admin"):
        raise CanvasError(403, f"{what} in project {access.name} needs the admin role; you are {access.role}")


def sink_of(access: ProjectAccess, sink: str | None) -> str | None:
    """The sink a read goes through: the one named, else the project's hub sink."""
    if sink:
        return sink
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


def visible(access: ProjectAccess, label, sink: str | None) -> bool:
    through = sink_of(access, sink)
    return through is not None and access.visible(label, through)


def missing(access: ProjectAccess, slug: str) -> CanvasError:
    return CanvasError(
        404, f"no canvas {slug} in project {access.name} that you can see: see `evo-agents hub canvas list`"
    )


def writable(access: ProjectAccess, canvas: CanvasRow) -> CanvasRow:
    """``canvas`` when it takes writes; 409 when it is archived."""
    if canvas.archived_at is not None:
        raise CanvasError(
            409,
            f"canvas {canvas.slug} of project {access.name} is archived and takes no change: an admin unarchives it",
        )
    return canvas


# What a member reads


async def canvases(
    conn: AsyncConnection, access: ProjectAccess, sink: str | None, *, with_archived: bool = False
) -> list[CanvasRow]:
    """The canvases of the project the member sees, by slug; the archived ones too with ``with_archived``."""
    reader(access)
    rows = await store.canvases_of(conn, access.project_id, with_archived=with_archived)
    return [row for row in rows if visible(access, row.label, sink)]


async def canvas(
    conn: AsyncConnection, access: ProjectAccess, slug: str, sink: str | None, *, lock: bool = False
) -> CanvasRow:
    """Canvas ``slug`` as the member sees it; 404 when there is none or they cannot see it. ``lock`` takes its row
    lock first, which every write of the canvas and its claims takes, so they go one after the other."""
    reader(access)
    row = await store.canvas_of(conn, access.project_id, slug, lock=lock)
    if row is None or not visible(access, row.label, sink):
        raise missing(access, slug)
    return row


async def claim(
    conn: AsyncConnection, access: ProjectAccess, canvas: CanvasRow, number: int, *, lock: bool = False
) -> ClaimRow:
    """Claim ``number`` of ``canvas``; 404 when the canvas has none. ``lock`` takes its row lock."""
    row = await store.claim_of(conn, access.project_id, number, lock=lock)
    if row is None or row.canvas_id != canvas.id:
        raise CanvasError(404, f"no claim {code(number)} in canvas {canvas.slug} of project {access.name}")
    return row

"""The rows behind the tasks of an approval (``service.auto_tasks``): the hub's task tables (``tables.tasks``), which
srs writes as the plugin tasks reads its items, and srs's own claims of an approval (srs_approval_tasks) and of a
release (srs_use_case_releases.tasks)."""

from __future__ import annotations

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.tables_srs import FRAME_TASK_PREFIX
from evo_agents.hub.tables_tasks import MAX_CAUSE_CHARS, MAX_VALUE_CHARS


async def history(conn: AsyncConnection, task_id: int, name: str, old: str | None, new: str | None, cause: str):
    """A row of the hub (no actor) in the history of task ``task_id``."""
    values = {
        "task_id": task_id,
        "actor_id": None,
        "field": name,
        "from_value": old[:MAX_VALUE_CHARS] if old else old,
        "to_value": new[:MAX_VALUE_CHARS] if new else new,
        "cause": cause[:MAX_CAUSE_CHARS],
    }
    await conn.execute(insert(tables.tasks.events).values(**values))


async def task_by_key(conn: AsyncConnection, project_id: int, key: str) -> int | None:
    """The id of the task of the project whose external key is ``key``, or None."""
    t = tables.tasks.items
    return await conn.scalar(select(t.c.id).where(t.c.project_id == project_id, t.c.external_key == key))


async def next_number(conn: AsyncConnection, project_id: int) -> int:
    """The number the project's next task takes."""
    t = tables.tasks.items
    return (await conn.scalar(select(func.max(t.c.number)).where(t.c.project_id == project_id)) or 0) + 1


async def insert_task(conn: AsyncConnection, values: dict) -> int:
    t = tables.tasks.items
    return (await conn.execute(insert(t).values(**values).returning(t.c.id))).scalar_one()


async def insert_link(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.tasks.links).values(**values))


async def insert_comment(conn: AsyncConnection, values: dict) -> None:
    await conn.execute(insert(tables.tasks.comments).values(**values))


async def set_status(conn: AsyncConnection, task_id: int, status: str) -> None:
    t = tables.tasks.items
    await conn.execute(update(t).where(t.c.id == task_id).values(status=status, updated_at=func.now()))


def _linked(project_id: int, item_id: int, finished: tuple[str, ...], *columns):
    t, k = tables.tasks.items, tables.tasks.links
    return (
        select(*columns)
        .join_from(k, t, t.c.id == k.c.task_id)
        .where(k.c.kind == "srs_item", k.c.srs_item_id == item_id, t.c.project_id == project_id)
        .where(t.c.status.not_in(finished))
    )


async def open_tasks(conn: AsyncConnection, project_id: int, item_id: int, finished: tuple[str, ...]) -> list:
    """The open tasks (id, number, status; none in ``finished``) of the project linked to SRS item ``item_id``, by
    number."""
    t = tables.tasks.items
    query = _linked(project_id, item_id, finished, t.c.id, t.c.number, t.c.status).distinct().order_by(t.c.number)
    return list(await conn.execute(query))


async def framed(conn: AsyncConnection, project_id: int, item_id: int, finished: tuple[str, ...], slug: str) -> bool:
    """Whether SRS item ``item_id`` has an open frame task of document ``slug``."""
    t = tables.tasks.items
    query = _linked(project_id, item_id, finished, t.c.id)
    query = query.where(t.c.external_key.startswith(f"{FRAME_TASK_PREFIX}{slug}:", autoescape=True)).limit(1)
    return await conn.scalar(query) is not None


async def frames(
    conn: AsyncConnection, project_id: int, slug: str, finished: tuple[str, ...], only: list[int] | None = None
) -> list:
    """(number, title, code of an SRS item it links) of the open frame tasks of document ``slug`` with no child task,
    by number; those numbered ``only`` alone when given."""
    t, k, i = tables.tasks.items, tables.tasks.links, tables.srs.items
    child = t.alias("child")
    prefix = f"{FRAME_TASK_PREFIX}{slug}:"
    query = (
        select(t.c.number, t.c.title, i.c.code)
        .select_from(
            t.outerjoin(k, (k.c.task_id == t.c.id) & (k.c.kind == "srs_item")).outerjoin(i, i.c.id == k.c.srs_item_id)
        )
        .where(t.c.project_id == project_id, t.c.external_key.startswith(prefix, autoescape=True))
        .where(t.c.status.not_in(finished), ~exists().where(child.c.parent_id == t.c.id))
        .order_by(t.c.number, k.c.id)
    )
    if only is not None:
        query = query.where(t.c.number.in_(only or [0]))
    return list(await conn.execute(query))


async def names(conn: AsyncConnection, project_id: int, user_id: int) -> tuple[str, str]:
    """(the project's name, the member's login)."""
    p, u = tables.projects, tables.users
    project = (await conn.execute(select(p.c.name).where(p.c.id == project_id))).scalar_one()
    return project, (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


# The claim of an approval


async def approval_done(conn: AsyncConnection, version_id: int):
    """The split run and outcome the approval of version ``version_id`` kept."""
    a = tables.srs.approval_tasks
    return (await conn.execute(select(a.c.split_run_id, a.c.outcome).where(a.c.version_id == version_id))).one()


async def claim_approval(conn: AsyncConnection, version_id: int, previous: int | None, approver_id: int) -> int | None:
    """Claim the tasks of the approval of version ``version_id``: the claim's id, or None when a call claimed it
    before."""
    a = tables.srs.approval_tasks
    claim = pg_insert(a).values(version_id=version_id, previous_version=previous, approved_by=approver_id)
    return await conn.scalar(claim.on_conflict_do_nothing(index_elements=[a.c.version_id]).returning(a.c.id))


async def keep_approval(conn: AsyncConnection, claim_id: int, values: dict) -> None:
    a = tables.srs.approval_tasks
    await conn.execute(update(a).where(a.c.id == claim_id).values(**values))


# The claim of a release


async def claim_release(conn: AsyncConnection, release_id: int):
    """Claim release ``release_id`` of a use case (what it released, by whom and when), or None when a call claimed it
    before or it is not there."""
    e = tables.srs.use_case_releases
    claim = (
        update(e)
        .where(e.c.id == release_id, e.c.tasks.is_(None))
        .values(tasks={})
        .returning(e.c.document_id, e.c.item_id, e.c.rev, e.c.version_id, e.c.change, e.c.released_by, e.c.released_at)
    )
    return (await conn.execute(claim)).first()


async def keep_release(conn: AsyncConnection, release_id: int, kept: dict) -> None:
    e = tables.srs.use_case_releases
    await conn.execute(update(e).where(e.c.id == release_id).values(tasks=kept))


async def add_to_release(conn: AsyncConnection, release_id: int, values: dict) -> None:
    """Add ``values`` to what release ``release_id`` keeps."""
    e = tables.srs.use_case_releases
    await conn.execute(update(e).where(e.c.id == release_id).values(tasks=e.c.tasks.op("||")(values)))


async def release_tasks(conn: AsyncConnection, batch: list[int]) -> list:
    """(id, tasks) of the releases ``batch``."""
    e = tables.srs.use_case_releases
    return list(await conn.execute(select(e.c.id, e.c.tasks).where(e.c.id.in_(batch or [0]))))


async def version_number(conn: AsyncConnection, version_id: int) -> int | None:
    v = tables.srs.versions
    return await conn.scalar(select(v.c.number).where(v.c.id == version_id))


async def item_code(conn: AsyncConnection, item_id: int) -> str:
    """The code of item ``item_id``, else its key."""
    i = tables.srs.items
    return str(await conn.scalar(select(func.coalesce(i.c.code, i.c.key)).where(i.c.id == item_id)))


async def released_before(conn: AsyncConnection, item_id: int, rev: int) -> int | None:
    """The number of the version the newest release of item ``item_id`` before revision ``rev`` was approved on."""
    e, v = tables.srs.use_case_releases, tables.srs.versions
    return await conn.scalar(
        select(v.c.number)
        .join_from(e, v, v.c.id == e.c.version_id)
        .where(e.c.item_id == item_id, e.c.rev < rev)
        .order_by(e.c.rev.desc())
        .limit(1)
    )


__all__ = [
    "add_to_release",
    "approval_done",
    "claim_approval",
    "claim_release",
    "framed",
    "frames",
    "history",
    "insert_comment",
    "insert_link",
    "insert_task",
    "item_code",
    "keep_approval",
    "keep_release",
    "names",
    "next_number",
    "open_tasks",
    "release_tasks",
    "released_before",
    "set_status",
    "task_by_key",
    "version_number",
]

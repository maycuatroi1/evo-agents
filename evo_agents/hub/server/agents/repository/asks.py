"""The SQL of an agent's ask of another agent: the marks of the run asked and of the wait it answers, the thread and
the chain of runs an answer is read from, the refs of the work, and the waits the end of a run answers (``waits``)."""

from __future__ import annotations

from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables


async def mark_asked(conn: AsyncConnection, child: int, run_id: int, wait_id: int) -> None:
    """Mark run ``child`` asked by run ``run_id``, its end answering wait ``wait_id`` (unless it answers an open wait
    already), and the wait awaiting it."""
    r, d = tables.runs, tables.decisions
    held = exists().where(d.c.id == r.c.spawned_wait_id, d.c.state == "open")  # a wait it answers already stays
    await conn.execute(
        update(r).values(spawned_by_run_id=run_id, spawned_wait_id=wait_id).where(r.c.id == child, ~held)
    )
    await conn.execute(update(d).values(awaits_run_id=child).where(d.c.id == wait_id))


async def started_in(conn: AsyncConnection, run_id: int):
    """(profile_id, root) of run ``run_id``: its agent and the thread of the message that started it; None for a run
    no message started."""
    r, m = tables.runs, tables.chat_messages
    query = (
        select(r.c.profile_id, func.coalesce(m.c.thread_root_id, m.c.id).label("root"))
        .join_from(r, m, m.c.id == r.c.chat_message_id)
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def written_in(conn: AsyncConnection, root: int, profile_id: int, run_ids: list[int], most: int) -> list:
    """(id, body) of the last ``most`` messages agent ``profile_id`` wrote in thread ``root`` as runs ``run_ids``,
    newest first."""
    m = tables.chat_messages
    in_thread = func.coalesce(m.c.thread_root_id, m.c.id) == root
    mine = (m.c.author_profile_id == profile_id, m.c.run_id.in_(run_ids))
    query = select(m.c.id, m.c.body).where(in_thread, *mine).order_by(m.c.id.desc())
    return list((await conn.execute(query.limit(most))).all())


async def went_on_from(conn: AsyncConnection, run_id: int):
    """(resume_of_run_id, parent_run_id) of run ``run_id``; None when the hub has no such run."""
    r = tables.runs
    query = select(r.c.resume_of_run_id, r.c.parent_run_id).where(r.c.id == run_id)
    return (await conn.execute(query)).one_or_none()


async def wait_outputs(conn: AsyncConnection, wait_id: int) -> list:
    """(id, name) of the outputs of the runs that answer wait ``wait_id``, in order."""
    r, o = tables.runs, tables.run_outputs
    chain = select(r.c.id).where(r.c.spawned_wait_id == wait_id)
    query = select(o.c.id, o.c.name).where(o.c.run_id.in_(chain)).order_by(o.c.id)
    return list((await conn.execute(query)).all())


async def run_work(conn: AsyncConnection, run_id: int):
    """(branch, repos, commit_sha) of run ``run_id``, which exists."""
    r = tables.runs
    return (await conn.execute(select(r.c.branch, r.c.repos, r.c.commit_sha).where(r.c.id == run_id))).one()


async def wait_project(conn: AsyncConnection, wait_id: int) -> int:
    """The project of wait ``wait_id``, which exists."""
    d = tables.decisions
    return (await conn.execute(select(d.c.project_id).where(d.c.id == wait_id))).scalar_one()


async def project_names(conn: AsyncConnection, run_id: int, project_id: int) -> tuple[str, str]:
    """(the name of the project of run ``run_id``, the name of project ``project_id``)."""
    r, p = tables.runs, tables.projects
    asked = select(p.c.name).join_from(r, p, p.c.id == r.c.project_id).where(r.c.id == run_id)
    asking = select(p.c.name).where(p.c.id == project_id)
    return (await conn.execute(asked)).scalar_one(), (await conn.execute(asking)).scalar_one()


async def run_end(conn: AsyncConnection, run_id: int):
    """(error, failure_cause) of run ``run_id``, which exists."""
    r = tables.runs
    return (await conn.execute(select(r.c.error, r.c.failure_cause).where(r.c.id == run_id))).one()


def waits(run_id: int):
    """The waits the end of run ``run_id`` answers: the one it carries (``spawned_wait_id``) and each open wait of an
    ask that run, or a run it goes on from in its session or retries after a loss, took in its inbox
    (``decisions.awaits_run_id``)."""
    r, d = tables.runs, tables.decisions
    columns = (r.c.id, r.c.resume_of_run_id, r.c.parent_run_id)
    chain = select(*columns).where(r.c.id == run_id).cte("answering", recursive=True)
    earlier = r.alias("earlier")
    went_on = or_(earlier.c.id == chain.c.resume_of_run_id, earlier.c.id == chain.c.parent_run_id)
    chain = chain.union(
        select(earlier.c.id, earlier.c.resume_of_run_id, earlier.c.parent_run_id).join_from(earlier, chain, went_on)
    )
    carried = select(r.c.spawned_wait_id).where(r.c.id == run_id).scalar_subquery()
    awaited = d.c.awaits_run_id.in_(select(chain.c.id))
    query = select(d.c.id, d.c.project_id, d.c.plan_id).where(d.c.state == "open", (d.c.id == carried) | awaited)
    return query.order_by(d.c.id)


async def open_waits(conn: AsyncConnection, run_id: int) -> list:
    """The rows of ``waits``."""
    return list((await conn.execute(waits(run_id))).all())


async def run_column(conn: AsyncConnection, run_id: int, column: str):
    """Column ``column`` (state, project_id or profile_id) of run ``run_id``, which exists."""
    r = tables.runs
    return (await conn.execute(select(r.c[column]).where(r.c.id == run_id))).scalar_one()


async def carried_outputs(conn: AsyncConnection, message_id: int) -> list[int]:
    """The outputs message ``message_id`` carries, in order."""
    k = tables.run_output_links
    carried = select(k.c.output_id).where(k.c.chat_message_id == message_id).order_by(k.c.output_id)
    return list((await conn.execute(carried)).scalars())

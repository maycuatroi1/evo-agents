"""The SQL of the answer jobs of a project (``evo_agents.hub.server.kg.service.answers``): the jobs and their counts,
their questions and answers, the runs that asked them and the batches the hub queues."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_answers as model
from evo_agents.hub import tables

# Reading


def _counts():
    a = tables.kg_answers
    waiting = and_(a.c.answer.is_(None), a.c.error.is_(None))
    return (
        func.count().filter(a.c.answer.is_not(None)).label("answered"),
        func.count().filter(a.c.error.is_not(None)).label("errors"),
        func.count().filter(waiting, a.c.run_id.is_(None)).label("pending"),
    )


def _jobs():
    j, u = tables.kg_answer_jobs, tables.users
    return select(j, u.c.login.label("by")).join_from(j, u, u.c.id == j.c.created_by, isouter=True)


async def job_counts(conn: AsyncConnection, job_ids: list[int]) -> dict[int, Any]:
    """{job id: its answered, errors, pending and asking counts} of the jobs ``job_ids`` with a question."""
    a = tables.kg_answers
    current = and_(a.c.run_id == func.coalesce(tables.kg_answer_jobs.c.run_id, 0), a.c.replied_at.is_(None))
    return {
        row.job_id: row
        for row in await conn.execute(
            select(a.c.job_id, *_counts(), func.count().filter(current, a.c.error.is_(None)).label("asking"))
            .join_from(a, tables.kg_answer_jobs, tables.kg_answer_jobs.c.id == a.c.job_id)
            .where(a.c.job_id.in_(job_ids))
            .group_by(a.c.job_id)
        )
    }


async def jobs(conn: AsyncConnection, project_id: int, limit: int) -> Sequence[Any]:
    """The project's newest ``limit`` jobs, each with the login of its sender."""
    j = tables.kg_answer_jobs
    return (await conn.execute(_jobs().where(j.c.project_id == project_id).order_by(j.c.id.desc()).limit(limit))).all()


async def job(conn: AsyncConnection, project_id: int, job_id: int, *, lock: bool = False):
    """Job ``job_id`` of the project with the login of its sender, locked with ``lock``; None without it."""
    j = tables.kg_answer_jobs
    query = _jobs().where(j.c.id == job_id, j.c.project_id == project_id)
    return (await conn.execute(query.with_for_update(of=j) if lock else query)).first()


async def answers(conn: AsyncConnection, job_id: int, offset: int, limit: int) -> Sequence[Any]:
    """A page of the job's questions and answers, in the file's order."""
    a = tables.kg_answers
    return (
        await conn.execute(select(a).where(a.c.job_id == job_id).order_by(a.c.position).offset(offset).limit(limit))
    ).all()


async def run_figures(conn: AsyncConnection, job_id: int) -> dict[int, tuple[str | None, int, int, int]]:
    """{run id: (model, input tokens, output tokens, questions of the job it asked)}."""
    a, r = tables.kg_answers, tables.runs
    query = (
        select(r.c.id, r.c.model_used, r.c.model, r.c.usage, func.count().label("n"))
        .join_from(a, r, r.c.id == a.c.run_id)
        .where(a.c.job_id == job_id)
        .group_by(r.c.id)
    )
    found = {}
    for row in await conn.execute(query):
        tokens_in, tokens_out = model.usage_tokens(row.usage)
        found[row.id] = (row.model_used or row.model, tokens_in, tokens_out, int(row.n))
    return found


async def ranks(conn: AsyncConnection, job_id: int, positions: list[int]) -> dict[int, int]:
    """{position: its rank among the job's questions of the same run}: which share of the run's tokens it takes."""
    a = tables.kg_answers
    rank = func.row_number().over(partition_by=a.c.run_id, order_by=a.c.position) - 1
    inner = select(a.c.position, rank.label("rank")).where(a.c.job_id == job_id, a.c.run_id.is_not(None)).subquery()
    query = select(inner.c.position, inner.c.rank).where(inner.c.position.in_(positions))
    return {row.position: int(row.rank) for row in await conn.execute(query)}


async def run_state(conn: AsyncConnection, run_id: int | None, *, lock: bool = False) -> str | None:
    r = tables.runs
    query = select(r.c.state).where(r.c.id == run_id)
    return await conn.scalar(query.with_for_update() if lock else query)


# Asking


async def lock_project(conn: AsyncConnection, project_id: int) -> None:
    await conn.execute(select(tables.projects.c.id).where(tables.projects.c.id == project_id).with_for_update())


async def job_of_file(conn: AsyncConnection, project_id: int, digest: str) -> int | None:
    """The newest job of the project whose file has ``digest``."""
    j = tables.kg_answer_jobs
    return await conn.scalar(
        select(j.c.id).where(j.c.project_id == project_id, j.c.digest == digest).order_by(j.c.id.desc()).limit(1)
    )


async def ask_again(conn: AsyncConnection, job_id: int, batch: int) -> None:
    """Ask again the questions of job ``job_id`` without an agent's reply."""
    a, j = tables.kg_answers, tables.kg_answer_jobs
    reset = await conn.execute(
        update(a)
        .where(a.c.job_id == job_id, a.c.replied_at.is_(None), a.c.error.is_not(None))
        .values(run_id=None, error=None, asked_at=None, evidence=[])
    )
    values = {"batch_size": batch, "updated_at": func.now()}
    if reset.rowcount:
        values |= {"state": model.RUNNING, "error": None, "note": None, "waiting_since": None, "ended_at": None}
    await conn.execute(update(j).where(j.c.id == job_id).values(**values))


async def add_job(
    conn: AsyncConnection, project_id: int, user_id: int | None, asked: list[tuple[str, str]], digest: str, batch: int
) -> int:
    j, a = tables.kg_answer_jobs, tables.kg_answers
    made = await conn.execute(
        j.insert()
        .values(
            project_id=project_id,
            created_by=user_id,
            digest=digest,
            questions=len(asked),
            batch_size=batch,
            state=model.RUNNING,
        )
        .returning(j.c.id)
    )
    job_id: int = made.scalar_one()
    await conn.execute(
        a.insert(),
        [{"job_id": job_id, "position": n, "qid": qid, "question": text} for n, (qid, text) in enumerate(asked)],
    )
    return job_id


# Replies


async def batch_rows(conn: AsyncConnection, job_id: int, run_id: int) -> Sequence[Any]:
    """(qid, position) of the questions run ``run_id`` asks in job ``job_id``."""
    a = tables.kg_answers
    return (await conn.execute(select(a.c.qid, a.c.position).where(a.c.job_id == job_id, a.c.run_id == run_id))).all()


async def keep_reply(conn: AsyncConnection, job_id: int, position: int, values: dict) -> None:
    a = tables.kg_answers
    await conn.execute(
        update(a).where(a.c.job_id == job_id, a.c.position == position).values(**values, replied_at=func.now())
    )


# Ending runs and jobs


async def run_ended(conn: AsyncConnection, job_id: int, run_id: int, error: str) -> None:
    """The questions of run ``run_id`` without a reply get ``error``, and the job asks with no run."""
    a, j = tables.kg_answers, tables.kg_answer_jobs
    await conn.execute(
        update(a)
        .where(a.c.job_id == job_id, a.c.run_id == run_id, a.c.answer.is_(None), a.c.error.is_(None))
        .values(error=error)
    )
    await conn.execute(update(j).where(j.c.id == job_id).values(run_id=None, updated_at=func.now()))


async def end(conn: AsyncConnection, job_id: int, state: str, error: str | None = None) -> None:
    j, a = tables.kg_answer_jobs, tables.kg_answers
    if error is not None:
        await conn.execute(
            update(a)
            .where(a.c.job_id == job_id, a.c.run_id.is_(None), a.c.answer.is_(None), a.c.error.is_(None))
            .values(error=error)
        )
    await conn.execute(
        update(j)
        .where(j.c.id == job_id)
        .values(state=state, error=error, note=None, waiting_since=None, ended_at=func.now(), updated_at=func.now())
    )


async def ask_cancel(conn: AsyncConnection, run_id: int) -> None:
    r = tables.runs
    await conn.execute(
        update(r).where(r.c.id == run_id, r.c.cancel_requested_at.is_(None)).values(cancel_requested_at=func.now())
    )


async def stopped(conn: AsyncConnection, job_id: int, why: str, error: str) -> None:
    """Job ``job_id`` ends failed with ``why``; its questions without an answer get ``error``."""
    a, j = tables.kg_answers, tables.kg_answer_jobs
    await conn.execute(
        update(a).where(a.c.job_id == job_id, a.c.answer.is_(None), a.c.error.is_(None)).values(error=error)
    )
    await conn.execute(
        update(j)
        .where(j.c.id == job_id)
        .values(
            state=model.FAILED,
            error=why,
            run_id=None,
            note=None,
            waiting_since=None,
            ended_at=func.now(),
            updated_at=func.now(),
        )
    )


# Moving the jobs


async def running_ids(conn: AsyncConnection) -> Sequence[int]:
    j = tables.kg_answer_jobs
    return (await conn.execute(select(j.c.id).where(j.c.state == model.RUNNING).order_by(j.c.id))).scalars().all()


async def running_job(conn: AsyncConnection, job_id: int):
    """Job ``job_id`` with its project's name while it runs, locked, or None when it ended or another moves it."""
    j, p = tables.kg_answer_jobs, tables.projects
    return (
        await conn.execute(
            select(j, p.c.name.label("project"))
            .join_from(j, p, p.c.id == j.c.project_id)
            .where(j.c.id == job_id, j.c.state == model.RUNNING)
            .with_for_update(of=j, skip_locked=True)
        )
    ).first()


async def now(conn: AsyncConnection) -> datetime:
    return (await conn.execute(select(func.now()))).scalar_one()


async def waits(conn: AsyncConnection, job_id: int, since: datetime, note: str) -> None:
    j = tables.kg_answer_jobs
    await conn.execute(update(j).where(j.c.id == job_id).values(waiting_since=since, note=note, updated_at=func.now()))


async def pending(conn: AsyncConnection, job_id: int, limit: int) -> Sequence[Any]:
    """(position, qid, question) of the job's first ``limit`` questions no run asked yet."""
    a = tables.kg_answers
    return (
        await conn.execute(
            select(a.c.position, a.c.qid, a.c.question)
            .where(a.c.job_id == job_id, a.c.run_id.is_(None), a.c.answer.is_(None), a.c.error.is_(None))
            .order_by(a.c.position)
            .limit(limit)
        )
    ).all()


async def asked(conn: AsyncConnection, job_id: int, positions: list[int], run_id: int) -> None:
    """Run ``run_id`` asks the questions at ``positions`` of the job."""
    a, j = tables.kg_answers, tables.kg_answer_jobs
    await conn.execute(
        update(a).where(a.c.job_id == job_id, a.c.position.in_(positions)).values(run_id=run_id, asked_at=func.now())
    )
    await conn.execute(
        update(j)
        .where(j.c.id == job_id)
        .values(run_id=run_id, runs=j.c.runs + 1, waiting_since=None, note=None, updated_at=func.now())
    )


async def run_questions(conn: AsyncConnection, run_id: int) -> Sequence[Any]:
    """(job_id, qid, question, name of the project) of the questions run ``run_id`` asks, in the file's order."""
    a, j, p = tables.kg_answers, tables.kg_answer_jobs, tables.projects
    return (
        await conn.execute(
            select(a.c.job_id, a.c.qid, a.c.question, p.c.name)
            .join_from(a, j, j.c.id == a.c.job_id)
            .join(p, p.c.id == j.c.project_id)
            .where(a.c.run_id == run_id)
            .order_by(a.c.position)
        )
    ).all()


def answer_runs():
    """The ids of the runs that asked a batch of an answer job: never in the Curator's night figures."""
    a = tables.kg_answers
    return select(a.c.run_id).where(a.c.run_id.is_not(None))

"""The answer jobs of a project (``evo_agents.hub.server.kg_answers`` has the routes and the plan): who may read, ask,
reply and stop, the counts of a job, and the pass that moves the running jobs; each route's work in one transaction.
``evo_agents.hub.kg_answers`` holds the rules without the database."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_answers as model
from evo_agents.hub import runs
from evo_agents.hub.server import audit, run_access_rules
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.answers import (
    KgAnswerAsk,
    KgAnswerJob,
    KgAnswerJobs,
    KgAnswerLine,
    KgAnswerLines,
    KgAnswerReplied,
    KgAnswerReply,
)
from evo_agents.hub.server.kg.repository import answers as repository
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal, holds

log = logging.getLogger("evo_agents.hub.server.kg_answers")  # the name its log lines had before the layers

AskerOf = Callable[[AsyncConnection, Any], Awaitable["tuple[int, str] | None"]]  # kg_init.asker_of
QueueRepoless = Callable[..., Awaitable["tuple[int, dict] | None"]]  # kg_init.queue_repoless


# Reading


async def _shown(conn: AsyncConnection, project: str, rows: list) -> list[KgAnswerJob]:
    if not rows:
        return []
    found = await repository.job_counts(conn, [row.id for row in rows])
    out = []
    for row in rows:
        n = found.get(row.id)
        out.append(
            KgAnswerJob(
                id=row.id,
                project=project,
                state=row.state,
                questions=row.questions,
                answered=n.answered if n else 0,
                errors=n.errors if n else 0,
                pending=n.pending if n else 0,
                asking=n.asking if n else 0,
                batch=row.batch_size,
                runs=row.runs,
                run_id=row.run_id,
                note=row.note,
                error=row.error,
                created_by=row.by,
                created_at=row.created_at,
                updated_at=row.updated_at,
                ended_at=row.ended_at,
            )
        )
    return out


async def _reads(conn: AsyncConnection, user: Principal, project: str):
    access = await project_access(conn, user, project)
    if access.role is None:
        raise HTTPException(403, f"reading the answer jobs of project {access.name} needs a grant on it")
    return access


async def _writes(conn: AsyncConnection, user: Principal, project: str, what: str):
    access = await project_access(conn, user, project)
    if not holds(access.role, "writer"):
        raise HTTPException(403, f"{what.format(project=access.name)} needs the writer role on it")
    return access


async def _job(conn: AsyncConnection, access, job_id: int, *, lock: bool = False):
    row = await repository.job(conn, access.project_id, job_id, lock=lock)
    if row is None:
        raise HTTPException(404, f"project {access.name} has no answer job {job_id}")
    return row


async def list_jobs(engine: AsyncEngine, user: Principal, project: str, limit: int) -> KgAnswerJobs:
    """The answer jobs of the project, newest first, with their counts."""
    async with UnitOfWork(engine) as conn:
        access = await _reads(conn, user, project)
        rows = list(await repository.jobs(conn, access.project_id, limit))
        return KgAnswerJobs(jobs=await _shown(conn, access.name, rows))


async def get_job(engine: AsyncEngine, user: Principal, project: str, job_id: int) -> KgAnswerJob:
    """One answer job with its counts."""
    async with UnitOfWork(engine) as conn:
        access = await _reads(conn, user, project)
        row = await _job(conn, access, job_id)
        return (await _shown(conn, access.name, [row]))[0]


async def job_lines(
    engine: AsyncEngine, user: Principal, project: str, job_id: int, offset: int, limit: int
) -> KgAnswerLines:
    """The job's answers in the file's order, each with the model of its run and its share of the run's tokens."""
    async with UnitOfWork(engine) as conn:
        access = await _reads(conn, user, project)
        job = await _job(conn, access, job_id)
        rows = await repository.answers(conn, job.id, offset, limit)
        figures = await repository.run_figures(conn, job.id)
        ranks = await repository.ranks(conn, job.id, [row.position for row in rows])
    lines = []
    for row in rows:
        used, tokens_in, tokens_out, n = figures.get(row.run_id, (None, 0, 0, 0))
        rank = ranks.get(row.position, 0)
        lines.append(
            KgAnswerLine(
                id=row.qid,
                question=row.question,
                answer=row.answer,
                evidence=list(row.evidence or []),
                model=used,
                input_tokens=model.shares(tokens_in, n)[rank] if n else 0,
                output_tokens=model.shares(tokens_out, n)[rank] if n else 0,
                error=row.error,
            )
        )
    return KgAnswerLines(lines=lines, total=job.questions, offset=offset)


# Asking


async def ask(engine: AsyncEngine, user: Principal, project: str, body: KgAnswerAsk) -> KgAnswerJob:
    """A new job of the file's questions, or the job of the same file sent before, which asks again only its questions
    without an agent's reply."""
    try:
        asked = model.questions(body.questions)
    except model.Refused as exc:
        raise HTTPException(422, f"the questions were not kept: {exc}") from None
    digest = model.digest(asked)
    async with UnitOfWork(engine) as conn:
        access = await _writes(conn, user, project, "asking questions of project {project}'s graph")
        if user.acts_for_run is not None and run_access_rules.ANSWERS not in user.run_reach:
            raise HTTPException(
                403,
                "the agent of a run asks no answer job, as it would hand agents work, unless it runs as its owner and "
                "the owner's run reach holds answers",
            )
        await repository.lock_project(conn, access.project_id)
        found = await repository.job_of_file(conn, access.project_id, digest)
        if found is not None:
            await repository.ask_again(conn, (await _job(conn, access, found, lock=True)).id, body.batch)
            job_id: int = found
        else:
            job_id = await repository.add_job(conn, access.project_id, user.user_id, asked, digest, body.batch)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=model.AUDIT,
            target=f"{access.name} job={job_id} questions={len(asked)} batch={body.batch}"
            + (" again" if found else ""),
            project_id=access.project_id,
        )
        out = (await _shown(conn, access.name, [await _job(conn, access, job_id)]))[0]
    log.info(
        "kg answer job asked",
        extra={"project": access.name, "job_id": job_id, "questions": len(asked), "again": found is not None},
    )
    return out.model_copy(update={"again": found is not None})


# Replies


async def _batch_rows(conn: AsyncConnection, user: Principal, access, job_id: int) -> dict[str, int]:
    """{id: position} of the questions the run of ``user`` asks in job ``job_id``; refuses anyone else."""
    run_id = user.acts_for_run
    if run_id is None:
        raise HTTPException(403, "only the agent of the run asking a batch sends its answers")
    await _job(conn, access, job_id, lock=True)
    rows = await repository.batch_rows(conn, job_id, run_id)
    if not rows:
        raise HTTPException(403, f"run {run_id} asks no question of answer job {job_id}")
    state = await repository.run_state(conn, run_id)
    if state not in runs.ACTIVE_STATES:
        raise HTTPException(409, f"run {run_id} ended {state}: its answers are no longer taken")
    return {row.qid: row.position for row in rows}


async def reply(
    engine: AsyncEngine, user: Principal, project: str, job_id: int, body: KgAnswerReply
) -> KgAnswerReplied:
    """Keep the answers, or errors, the agent of the run asking a batch sends for questions of its batch; the lines
    not kept come back with why, by position."""
    async with UnitOfWork(engine) as conn:
        access = await _writes(conn, user, project, "sending answers to project {project}")
        mine = await _batch_rows(conn, user, access, job_id)
        problems: list[str] = []
        kept = 0
        for n, entry in enumerate(body.answers, start=1):
            qid, values, why = model.check_reply(entry)
            if why is None and qid not in mine:
                why = "names a question this run was not asked"
            if why is not None or qid is None or values is None:
                problems.append(f"line {n} {why}")
                continue
            await repository.keep_reply(conn, job_id, mine[qid], values)
            kept += 1
    log.info(
        "kg answers replied",
        extra={"project": access.name, "job_id": job_id, "run_id": user.acts_for_run, "kept": kept},
    )
    return KgAnswerReplied(kept=kept, problems=problems[: model.MAX_PROBLEMS])


# Stopping


async def stop(engine: AsyncEngine, user: Principal, project: str, job_id: int) -> KgAnswerJob:
    """Stop a running job: its run is cancelled (a queued one at once, a held one by its worker), its questions without
    an answer get the error that it was stopped, and it ends failed."""
    from evo_agents.hub.server.run_state import move_run

    async with UnitOfWork(engine) as conn:
        access = await _writes(conn, user, project, "stopping an answer job of project {project}")
        job = await _job(conn, access, job_id, lock=True)
        if job.state != model.RUNNING:
            raise HTTPException(409, f"answer job {job_id} is {job.state}: only a running job stops")
        why = f"{user.login} stopped answer job {job_id}"
        if job.run_id is not None:
            state = await repository.run_state(conn, job.run_id, lock=True)
            if state == "queued":
                await move_run(conn, job.run_id, state, "cancelled", "owner", reason=why, token_id=user.token_id)
            elif state in runs.HELD_STATES:
                await repository.ask_cancel(conn, job.run_id)
        await repository.stopped(conn, job_id, why, f"{why} before this question had an answer")
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=model.AUDIT,
            target=f"{access.name} job={job_id} stopped",
            project_id=access.project_id,
        )
        out = (await _shown(conn, access.name, [await _job(conn, access, job_id)]))[0]
    log.info("kg answer job stopped", extra={"project": access.name, "job_id": job_id})
    return out


# The job that moves them


async def _ended(conn: AsyncConnection, job) -> bool:
    """Whether the run of ``job``'s batch ended: its questions without a reply then get an error."""
    state = await repository.run_state(conn, job.run_id)
    if state in runs.ACTIVE_STATES:
        return False
    await repository.run_ended(conn, job.id, job.run_id, model.ended_error(job.run_id, state))
    return True


async def _waits(conn: AsyncConnection, job, note: str, now: datetime) -> str:
    """No run could be queued for ``job``'s next batch: it waits, or fails after QUEUE_WAIT."""
    since = job.waiting_since or now
    if since + model.QUEUE_WAIT < now:
        await repository.end(conn, job.id, model.FAILED, f"no agent run could be queued in {model.QUEUE_WAIT}: {note}")
        return "failed"
    await repository.waits(conn, job.id, since, note)
    return "waiting"


async def _next(conn: AsyncConnection, job, now: datetime, asker_of: AskerOf, queue_repoless: QueueRepoless) -> str:
    """Queue the run of ``job``'s next batch, or end the job when no question is left."""
    from evo_agents.hub.server.run_state import notify_queued, write_event

    pending = await repository.pending(conn, job.id, job.batch_size)
    if not pending:
        await repository.end(conn, job.id, model.DONE)
        return "done"
    who = await asker_of(conn, SimpleNamespace(requested_by=job.created_by, project_id=job.project_id))
    if who is None:
        return await _waits(conn, job, "no review run: the project has no admin to dispatch it as", now)
    notes: list[str] = []
    queued = await queue_repoless(
        conn, job.project_id, job.project, who[0], model.TITLE.format(project=job.project), notes, repos=False
    )
    if queued is None:
        return await _waits(conn, job, notes[-1] if notes else "no review run", now)
    run_id, caps = queued
    taken = pending[: len(model.batch([(row.qid, row.question) for row in pending], job.batch_size))]
    await repository.asked(conn, job.id, [row.position for row in taken], run_id)
    text = (
        f"Queued for answer job #{job.id} of project {job.project}: {len(taken)} questions; cost cap "
        f"{caps['max_usd']} USD, {caps['max_turns']} turns, {caps['max_seconds'] // 60} minutes of agent time."
    )
    await write_event(conn, run_id, {"text": text, "answer_job_id": job.id, "questions": len(taken), **caps})
    await notify_queued(conn, run_id)
    await audit.record(
        conn,
        actor_id=who[0],
        token_id=None,
        action=model.AUDIT,
        target=f"{job.project} run:{run_id} job={job.id} questions={len(taken)}",
        project_id=job.project_id,
    )
    return "queued"


async def _move(
    engine: AsyncEngine, job_id: int, now: datetime | None, asker_of: AskerOf, queue_repoless: QueueRepoless
) -> str | None:
    async with UnitOfWork(engine) as conn:
        job = await repository.running_job(conn, job_id)
        if job is None:
            return None
        if job.run_id is not None and not await _ended(conn, job):
            return "asking"
        return await _next(conn, job, now or await repository.now(conn), asker_of, queue_repoless)


async def advance(
    engine: AsyncEngine, *, now: datetime | None = None, asker_of: AskerOf, queue_repoless: QueueRepoless
) -> dict:
    """One pass over the running answer jobs, each in a transaction of its own: how many ended in each outcome
    (asking, queued, waiting, done, failed). ``asker_of`` and ``queue_repoless`` are kg_init's, who dispatches the run
    of a batch and how it is queued."""
    async with UnitOfWork(engine) as conn:
        ids = await repository.running_ids(conn)
    found: dict[str, int] = {}
    for job_id in ids:
        try:
            word = await _move(engine, job_id, now, asker_of, queue_repoless)
        except Exception:  # one job failing leaves the others their turn
            log.exception("moving an answer job failed", extra={"job_id": job_id})
            word = "error"
        if word:
            found[word] = found.get(word, 0) + 1
            if word in ("queued", "done", "failed"):
                log.info("kg answer job moved", extra={"job_id": job_id, "outcome": word})
    return found


async def prompt_of(conn: AsyncConnection, run_id: int) -> str | None:
    """The prompt of review run ``run_id`` when it asks a batch of an answer job; None for any other run."""
    rows = await repository.run_questions(conn, run_id)
    if not rows:
        return None
    return model.prompt(project=rows[0].name, job_id=rows[0].job_id, asked=[(row.qid, row.question) for row in rows])

"""The answer jobs of a project (plan kg-zip-to-answers, criterion a6): a file of questions asked of its graph by Claude
Code agents in batches. ``evo_agents.hub.kg_answers`` holds the rules without the database.

- POST /v1/kg/{project}/answers (``evo-agents hub kg answer --project P --questions FILE``, a writer): keep the file as
  a job, or find the job of the same file (``kg_answers.digest``) and ask again its questions without an agent's reply.
  The agent of a run cannot: it would hand agents work.
- GET /v1/kg/{project}/answers (``--status``): the project's jobs, newest first, with their counts. GET .../{job_id}:
  one job. GET .../{job_id}/lines: its answers in the file's order, as answers.jsonl holds them: the model of the run
  that asked each and its share of the run's tokens. A grant on the project reads them.
- POST .../{job_id}/replies (``hub kg answer --job J --reply FILE``): the agent of the run asking a batch sends its
  answers, or an error for a question, while the run is active; only for the questions of its batch.
- ``advance``, called each minute by ``curator.collect``: for each running job, the questions of a run that ended
  without a reply get an error; the next batch is queued as a review run with no repo (``kg_init.queue_repoless``),
  dispatched by the sender of the file, on the project's KG pool, under the review cost cap. One review run of a
  project is active at a time, so a job waits for the others; a job no run could be queued for in
  ``kg_answers.QUEUE_WAIT`` fails, its questions not asked with an error. A job with no question left is done.
- ``prompt_of``: the prompt of the run of a batch (``collect.review_prompt``).

Logs and audit rows name the project, the job and the runs, and count; never a question or an answer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from types import SimpleNamespace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi import Path as PathParam
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import kg_answers as model
from evo_agents.hub import runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser, Principal

log = logging.getLogger(__name__)

MAX_LISTED = 50
MAX_LINES = 1000  # lines of one page of GET .../lines
MAX_REPLIES = 1000  # lines of one reply

router = APIRouter(prefix="/v1/kg", tags=["kg"], responses={401: {"model": ErrorBody}})

JobId = Annotated[int, PathParam(ge=1, description="the answer job, as `hub kg answer --status` lists it")]
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}


class KgAnswerAsk(BaseModel):
    questions: list[dict] = Field(
        min_length=1,
        max_length=model.MAX_QUESTIONS,
        description="the questions in the file's order: each {id, question}, the id a string or an integer",
    )
    batch: int = Field(model.DEFAULT_BATCH, ge=1, le=model.MAX_BATCH, description="questions asked of one agent run")


class KgAnswerJob(BaseModel):
    id: int
    project: str
    state: Literal["running", "done", "failed"]
    questions: int
    answered: int = Field(description="questions with an answer")
    errors: int = Field(description="questions with an error: from the agent, or from a run that ended without one")
    pending: int = Field(description="questions no run asked yet")
    asking: int = Field(description="questions of the run active now, not yet answered")
    batch: int = Field(description="questions asked of one agent run")
    runs: int = Field(description="agent runs queued for the job")
    run_id: int | None = Field(description="the run asking the current batch")
    note: str | None = Field(description="why the next batch waits: no node of the pool fits, another review run")
    error: str | None = Field(description="why the job failed")
    created_by: str | None
    created_at: datetime
    updated_at: datetime
    ended_at: datetime | None
    again: bool = Field(default=False, description="POST only: the same file was sent before, and this is its job")


class KgAnswerJobs(BaseModel):
    jobs: list[KgAnswerJob]


class KgAnswerLine(BaseModel):
    id: str
    question: str
    answer: str | None
    evidence: list[str] = Field(description="the node ids or repo:path names the agent read the answer from")
    model: str | None = Field(description="the model of the run that asked the question")
    input_tokens: int = Field(description="its share of the run's input tokens, cache reads and writes included")
    output_tokens: int = Field(description="its share of the run's output tokens")
    error: str | None = Field(description="why there is no answer: from the agent, or the run that ended without one")


class KgAnswerLines(BaseModel):
    lines: list[KgAnswerLine]
    total: int = Field(description="questions of the job")
    offset: int


class KgAnswerReply(BaseModel):
    answers: list[dict] = Field(
        min_length=1,
        max_length=MAX_REPLIES,
        description='each {"id", "answer", "evidence"} or {"id", "error"}',
    )


class KgAnswerReplied(BaseModel):
    kept: int = Field(description="answers and errors kept")
    problems: list[str] = Field(description="the lines not kept and why, by their position in the reply")


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


async def _shown(conn: AsyncConnection, project: str, rows: list) -> list[KgAnswerJob]:
    if not rows:
        return []
    a = tables.kg_answers
    current = and_(a.c.run_id == func.coalesce(tables.kg_answer_jobs.c.run_id, 0), a.c.replied_at.is_(None))
    found = {
        row.job_id: row
        for row in await conn.execute(
            select(a.c.job_id, *_counts(), func.count().filter(current, a.c.error.is_(None)).label("asking"))
            .join_from(a, tables.kg_answer_jobs, tables.kg_answer_jobs.c.id == a.c.job_id)
            .where(a.c.job_id.in_([row.id for row in rows]))
            .group_by(a.c.job_id)
        )
    }
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


async def _job(conn: AsyncConnection, access, job_id: int, *, lock: bool = False):
    j = tables.kg_answer_jobs
    query = _jobs().where(j.c.id == job_id, j.c.project_id == access.project_id)
    row = (await conn.execute(query.with_for_update(of=j) if lock else query)).first()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no answer job {job_id}")
    return row


@router.get("/{project}/answers", response_model=KgAnswerJobs, responses=REFUSALS)
async def list_jobs(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=MAX_LISTED)] = 20,
) -> KgAnswerJobs:
    """The answer jobs of the project, newest first, with their counts."""
    j = tables.kg_answer_jobs
    async with request.app.state.engine.begin() as conn:
        access = await _reads(conn, user, project)
        rows = (
            await conn.execute(_jobs().where(j.c.project_id == access.project_id).order_by(j.c.id.desc()).limit(limit))
        ).all()
        return KgAnswerJobs(jobs=await _shown(conn, access.name, rows))


@router.get("/{project}/answers/{job_id}", response_model=KgAnswerJob, responses=REFUSALS)
async def get_job(request: Request, project: ProjectName, job_id: JobId, user: CurrentUser) -> KgAnswerJob:
    """One answer job with its counts."""
    async with request.app.state.engine.begin() as conn:
        access = await _reads(conn, user, project)
        row = await _job(conn, access, job_id)
        return (await _shown(conn, access.name, [row]))[0]


async def _run_figures(conn: AsyncConnection, job_id: int) -> dict[int, tuple[str | None, int, int, int]]:
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


async def _ranks(conn: AsyncConnection, job_id: int, positions: list[int]) -> dict[int, int]:
    """{position: its rank among the job's questions of the same run}: which share of the run's tokens it takes."""
    a = tables.kg_answers
    rank = func.row_number().over(partition_by=a.c.run_id, order_by=a.c.position) - 1
    inner = select(a.c.position, rank.label("rank")).where(a.c.job_id == job_id, a.c.run_id.is_not(None)).subquery()
    query = select(inner.c.position, inner.c.rank).where(inner.c.position.in_(positions))
    return {row.position: int(row.rank) for row in await conn.execute(query)}


@router.get("/{project}/answers/{job_id}/lines", response_model=KgAnswerLines, responses=REFUSALS)
async def job_lines(
    request: Request,
    project: ProjectName,
    job_id: JobId,
    user: CurrentUser,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_LINES)] = MAX_LINES,
) -> KgAnswerLines:
    """The job's answers in the file's order, as answers.jsonl holds them: a question not answered has an error, or
    neither while it waits for a run."""
    a = tables.kg_answers
    async with request.app.state.engine.begin() as conn:
        access = await _reads(conn, user, project)
        job = await _job(conn, access, job_id)
        rows = (
            await conn.execute(select(a).where(a.c.job_id == job.id).order_by(a.c.position).offset(offset).limit(limit))
        ).all()
        figures = await _run_figures(conn, job.id)
        ranks = await _ranks(conn, job.id, [row.position for row in rows])
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


async def _again(conn: AsyncConnection, row, batch: int) -> None:
    """Ask again the questions of job ``row`` without an agent's reply."""
    a, j = tables.kg_answers, tables.kg_answer_jobs
    reset = await conn.execute(
        update(a)
        .where(a.c.job_id == row.id, a.c.replied_at.is_(None), a.c.error.is_not(None))
        .values(run_id=None, error=None, asked_at=None, evidence=[])
    )
    values = {"batch_size": batch, "updated_at": func.now()}
    if reset.rowcount:
        values |= {"state": model.RUNNING, "error": None, "note": None, "waiting_since": None, "ended_at": None}
    await conn.execute(update(j).where(j.c.id == row.id).values(**values))


async def _keep(
    conn: AsyncConnection, user: Principal, access, asked: list[tuple[str, str]], digest: str, batch: int
) -> int:
    j, a = tables.kg_answer_jobs, tables.kg_answers
    made = await conn.execute(
        j.insert()
        .values(
            project_id=access.project_id,
            created_by=user.user_id,
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


@router.post("/{project}/answers", response_model=KgAnswerJob, status_code=201, responses=REFUSALS)
async def ask(request: Request, project: ProjectName, body: KgAnswerAsk, user: CurrentUser) -> KgAnswerJob:
    """Ask the questions of a file of the project's graph: a new job, or the job of the same file sent before, which
    asks again only its questions without an agent's reply. Agents answer them in batches as the hub moves the job."""
    try:
        asked = model.questions(body.questions)
    except model.Refused as exc:
        raise HTTPException(422, f"the questions were not kept: {exc}") from None
    digest = model.digest(asked)
    j = tables.kg_answer_jobs
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"asking questions of project {access.name}'s graph needs the writer role on it")
        if user.acts_for_run is not None:
            raise HTTPException(403, "the agent of a run asks no answer job: it would hand agents work")
        await conn.execute(
            select(tables.projects.c.id).where(tables.projects.c.id == access.project_id).with_for_update()
        )
        found = await conn.scalar(
            select(j.c.id)
            .where(j.c.project_id == access.project_id, j.c.digest == digest)
            .order_by(j.c.id.desc())
            .limit(1)
        )
        if found is not None:
            await _again(conn, await _job(conn, access, found, lock=True), body.batch)
            job_id: int = found
        else:
            job_id = await _keep(conn, user, access, asked, digest, body.batch)
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
    a, r = tables.kg_answers, tables.runs
    rows = (await conn.execute(select(a.c.qid, a.c.position).where(a.c.job_id == job_id, a.c.run_id == run_id))).all()
    if not rows:
        raise HTTPException(403, f"run {run_id} asks no question of answer job {job_id}")
    state = await conn.scalar(select(r.c.state).where(r.c.id == run_id))
    if state not in runs.ACTIVE_STATES:
        raise HTTPException(409, f"run {run_id} ended {state}: its answers are no longer taken")
    return {row.qid: row.position for row in rows}


@router.post("/{project}/answers/{job_id}/replies", response_model=KgAnswerReplied, responses=REFUSALS)
async def reply(
    request: Request, project: ProjectName, job_id: JobId, body: KgAnswerReply, user: CurrentUser
) -> KgAnswerReplied:
    """The agent of the run asking a batch sends answers, or errors, for questions of its batch; an id sent again
    replaces its answer. The lines that are not kept come back with why, by position."""
    a = tables.kg_answers
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"sending answers to project {access.name} needs the writer role on it")
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
            await conn.execute(
                update(a).where(a.c.job_id == job_id, a.c.position == mine[qid]).values(**values, replied_at=func.now())
            )
            kept += 1
    log.info(
        "kg answers replied",
        extra={"project": access.name, "job_id": job_id, "run_id": user.acts_for_run, "kept": kept},
    )
    return KgAnswerReplied(kept=kept, problems=problems[: model.MAX_PROBLEMS])


# The job that moves them


async def _ended(conn: AsyncConnection, job) -> bool:
    """Whether the run of ``job``'s batch ended: its questions without a reply then get an error."""
    r, a, j = tables.runs, tables.kg_answers, tables.kg_answer_jobs
    state = await conn.scalar(select(r.c.state).where(r.c.id == job.run_id))
    if state in runs.ACTIVE_STATES:
        return False
    await conn.execute(
        update(a)
        .where(a.c.job_id == job.id, a.c.run_id == job.run_id, a.c.answer.is_(None), a.c.error.is_(None))
        .values(error=model.ended_error(job.run_id, state))
    )
    await conn.execute(update(j).where(j.c.id == job.id).values(run_id=None, updated_at=func.now()))
    return True


async def _end(conn: AsyncConnection, job, state: str, error: str | None = None) -> None:
    j, a = tables.kg_answer_jobs, tables.kg_answers
    if error is not None:
        await conn.execute(
            update(a)
            .where(a.c.job_id == job.id, a.c.run_id.is_(None), a.c.answer.is_(None), a.c.error.is_(None))
            .values(error=error)
        )
    await conn.execute(
        update(j)
        .where(j.c.id == job.id)
        .values(state=state, error=error, note=None, waiting_since=None, ended_at=func.now(), updated_at=func.now())
    )


async def _waits(conn: AsyncConnection, job, note: str, now: datetime) -> str:
    """No run could be queued for ``job``'s next batch: it waits, or fails after QUEUE_WAIT."""
    j = tables.kg_answer_jobs
    since = job.waiting_since or now
    if since + model.QUEUE_WAIT < now:
        await _end(conn, job, model.FAILED, f"no agent run could be queued in {model.QUEUE_WAIT}: {note}")
        return "failed"
    await conn.execute(update(j).where(j.c.id == job.id).values(waiting_since=since, note=note, updated_at=func.now()))
    return "waiting"


async def _asked_by(conn: AsyncConnection, job) -> tuple[int, str] | None:
    from evo_agents.hub.server.kg_init import asker_of

    return await asker_of(conn, SimpleNamespace(requested_by=job.created_by, project_id=job.project_id))


async def _next(conn: AsyncConnection, job, now: datetime) -> str:
    """Queue the run of ``job``'s next batch, or end the job when no question is left."""
    from evo_agents.hub.server.kg_init import queue_repoless
    from evo_agents.hub.server.run_state import notify_queued, write_event

    a, j = tables.kg_answers, tables.kg_answer_jobs
    pending = (
        await conn.execute(
            select(a.c.position, a.c.qid, a.c.question)
            .where(a.c.job_id == job.id, a.c.run_id.is_(None), a.c.answer.is_(None), a.c.error.is_(None))
            .order_by(a.c.position)
            .limit(job.batch_size)
        )
    ).all()
    if not pending:
        await _end(conn, job, model.DONE)
        return "done"
    who = await _asked_by(conn, job)
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
    await conn.execute(
        update(a)
        .where(a.c.job_id == job.id, a.c.position.in_([row.position for row in taken]))
        .values(run_id=run_id, asked_at=func.now())
    )
    await conn.execute(
        update(j)
        .where(j.c.id == job.id)
        .values(run_id=run_id, runs=j.c.runs + 1, waiting_since=None, note=None, updated_at=func.now())
    )
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


async def _move(engine: AsyncEngine, job_id: int, now: datetime | None) -> str | None:
    j, p = tables.kg_answer_jobs, tables.projects
    async with engine.begin() as conn:
        job = (
            await conn.execute(
                select(j, p.c.name.label("project"))
                .join_from(j, p, p.c.id == j.c.project_id)
                .where(j.c.id == job_id, j.c.state == model.RUNNING)
                .with_for_update(of=j, skip_locked=True)
            )
        ).first()
        if job is None:
            return None
        if job.run_id is not None and not await _ended(conn, job):
            return "asking"
        return await _next(conn, job, now or (await conn.execute(select(func.now()))).scalar_one())


async def advance(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass over the running answer jobs, each in a transaction of its own: how many ended in each outcome
    (asking, queued, waiting, done, failed)."""
    j = tables.kg_answer_jobs
    async with engine.begin() as conn:
        ids = (await conn.execute(select(j.c.id).where(j.c.state == model.RUNNING).order_by(j.c.id))).scalars().all()
    found: dict[str, int] = {}
    for job_id in ids:
        try:
            word = await _move(engine, job_id, now)
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
    a, j, p = tables.kg_answers, tables.kg_answer_jobs, tables.projects
    rows = (
        await conn.execute(
            select(a.c.job_id, a.c.qid, a.c.question, p.c.name)
            .join_from(a, j, j.c.id == a.c.job_id)
            .join(p, p.c.id == j.c.project_id)
            .where(a.c.run_id == run_id)
            .order_by(a.c.position)
        )
    ).all()
    if not rows:
        return None
    return model.prompt(project=rows[0].name, job_id=rows[0].job_id, asked=[(row.qid, row.question) for row in rows])


def answer_runs():
    """The ids of the runs that asked a batch of an answer job: never in the Curator's night figures."""
    a = tables.kg_answers
    return select(a.c.run_id).where(a.c.run_id.is_not(None))

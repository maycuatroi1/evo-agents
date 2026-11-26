"""Queueing the builds of a project's knowledge graph (job ``hub.kg_build``), and the recovery pass that fails or
queues again the builds whose worker stopped (``recover_stalled``), out of ``evo_agents.hub.kg_build``, which runs
them and imports back the names its callers use.

A project gets at most one build waiting (``queue_build``); a change of a core plugin's data queues one only when
the project has a knowledge config (``queue_if_configured``), and none after FAILED_IN_A_ROW builds failed alike or
DIED_IN_A_ROW builds ended with their worker gone, when the project's admins are told (HALTED_NOTICE).
"""

from __future__ import annotations

import logging

from sqlalchemy import column, delete, exists, func, insert, or_, select, table, update
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import kg_graph_rows, tables
from evo_agents.hub.jobs import KG_BUILD, JobQueue, kg_lock

log = logging.getLogger("evo_agents.hub.kg_build")  # the logger of the builds, as before the split


QUEUE_ATTEMPTS = 5
STALLED_AFTER = 120.0  # seconds without a heartbeat from its worker before a running build counts as abandoned
STOPPED = "the worker running this build stopped before it finished; a new build was queued"
ORPHANED = "the job of this build ended without recording how it went; the project's next build takes its place"
STALLED = (
    "the worker running this build stopped sending heartbeats before it finished (killed, out of memory, or its machine "
    "lost); a new build was queued"
)
REPLACED = (STOPPED, STALLED, ORPHANED)  # the errors of a build whose place the project's next build takes
# After this many builds of a project in a row ended with their worker gone (DIED), the recovery pass queues no other:
# a build that kills its worker, as one over the memory limit of worker-kg does, would otherwise die and be queued again
# every few minutes for ever. A build that succeeds, one a member queues by hand, or one its worker stopped cleanly
# (STOPPED, as a deploy does: ``_stopped``) starts a new row.
DIED_IN_A_ROW = 3
HALTED = (
    f"the worker running this build stopped before it finished, as it did for at least {DIED_IN_A_ROW - 1} builds of "
    "the project before it; no new build was queued and the project's admins were told: queue one by hand once the "
    "cause is fixed"
)
DIED = (STALLED, ORPHANED, HALTED)  # the errors of a build that ended with its worker gone
DIED_READ = 100  # the newest builds of a project the count of DIED_IN_A_ROW reads at most
HALTED_NOTICE = "kg_build_halted"  # runs.NOTICE_KINDS: the hub stopped queueing a project's builds
# After this many builds of a project in a row failed alike (the same first line of error, config digest and runs), a
# change of a core plugin's data (``queue_if_configured``) queues no other: a build that fails on its input fails the
# same way again, and chats and tasks change every few minutes, so it was queued and failed some ten times an hour.
# A build that succeeds, a push of new runs, a new knowledge config or a build a member queues by hand goes on as before.
FAILED_IN_A_ROW = 3
ERROR_HEAD_CHARS = 200  # what of a build's error a notice and a reader with no graph are told: its first line, cut
IN_FLIGHT = ("queued", "running")
JOB_ENDED = ("succeeded", "failed", "cancelled", "aborted")  # a procrastinate job in these will not run again

# procrastinate's job table, which the hub reads and locks but never writes: the columns the kg queries use. The
# status is procrastinate's enum, so a value compared with it is sent untyped and Postgres reads it as the enum.
JOB_STATUS = ENUM(
    "todo",
    "doing",
    "succeeded",
    "failed",
    "cancelled",
    "aborting",
    "aborted",
    name="procrastinate_job_status",
    create_type=False,
)
procrastinate_jobs = table(
    "procrastinate_jobs", column("id"), column("status", JOB_STATUS), column("lock"), column("queueing_lock")
)


class QueueBusy(Exception):
    """A build could not be queued: the waiting one kept starting in between."""


def project_id_query(name: str):
    """The query of the id of the project named ``name``."""
    projects = tables.projects
    return select(projects.c.id).where(projects.c.name == name)


async def queue_build(
    job_queue: JobQueue, conn: AsyncConnection, project_id: int, project: str, requested_by: int | None
) -> tuple[int | None, bool]:
    """Queue a build of ``project`` in ``conn``'s transaction: (its build id, True), or (the waiting build's id,
    False) when one waits already, or (None, False) when the project is archived (``server.project_archive``), whose
    graph is not built again. The waiting job's row stays locked until the transaction ends, and the worker takes jobs
    with SKIP LOCKED, so it cannot start that build before the caller's rows are visible to it."""
    p = tables.projects
    if await conn.scalar(select(p.c.archived_at).where(p.c.id == project_id)) is not None:
        log.info("kg build not queued: the project is archived", extra={"project": project})
        return None, False
    key = kg_lock(project)
    builds, jobs = tables.kg_builds, procrastinate_jobs
    for _ in range(QUEUE_ATTEMPTS):
        build_id = (
            await conn.execute(
                insert(builds)
                .values(project_id=project_id, status="queued", requested_by=requested_by)
                .returning(builds.c.id)
            )
        ).scalar_one()
        job_id = await job_queue.defer_kg(KG_BUILD, project, connection=conn, project=project, build_id=build_id)
        if job_id is not None:
            await conn.execute(update(builds).values(job_id=job_id).where(builds.c.id == build_id))
            return build_id, True
        await conn.execute(delete(builds).where(builds.c.id == build_id))
        waiting = (
            await conn.execute(
                select(jobs.c.id.label("job_id"), builds.c.id.label("build_id"))
                .join_from(jobs, builds, builds.c.job_id == jobs.c.id, isouter=True)
                .where(jobs.c.queueing_lock == key, jobs.c.status == "todo")
                .with_for_update(of=jobs)
            )
        ).first()
        if waiting is not None:
            return waiting.build_id, False
        # The waiting job started in between: queue another, which will see this transaction's rows.
    raise QueueBusy(f"a build of project {project} could not be queued; try again shortly")


async def queue_if_configured(job_queue: JobQueue, conn: AsyncConnection, project_id: int, project: str) -> None:
    """Queue a build of ``project`` in ``conn``'s transaction, joining one that waits, when the project has a knowledge
    config on the hub: what a core plugin calls once the data its kg source gives changed
    (``evo_agents.hub.kg_sources``). A queue that stays busy is logged, never raised: a build follows the next push."""
    configs = tables.kg_configs
    found = await conn.execute(select(configs.c.project_id).where(configs.c.project_id == project_id))
    if found.first() is None:
        return
    if await _halted_by_failures(conn, project_id, project):
        return
    try:
        await queue_build(job_queue, conn, project_id, project, None)
    except QueueBusy:
        log.warning("kg build not queued after a plugin's change: the queue was busy", extra={"project": project})


def error_head(error: str | None) -> str:
    """The first line of a build's error, at most ERROR_HEAD_CHARS: a SQL error goes on with the statement and its
    parameters, which can hold content."""
    line = (error or "").strip().split("\n", 1)[0].strip()
    return line if len(line) <= ERROR_HEAD_CHARS else line[: ERROR_HEAD_CHARS - 3] + "..."


async def _failed_alike(conn: AsyncConnection, project_id: int):
    """The newest FAILED_IN_A_ROW ended builds of a project when they all failed alike (the same ``error_head``, config
    digest and runs) and their worker did not die or stop (REPLACED, DIED: ``_died_in_a_row`` counts those), with no
    build a member queued by hand after the oldest of them; else None."""
    builds = tables.kg_builds
    rows = (
        await conn.execute(
            select(
                builds.c.status,
                builds.c.error,
                builds.c.config_digest,
                builds.c.runs,
                builds.c.requested_by,
                builds.c.finished_at,
            )
            .where(builds.c.project_id == project_id, builds.c.status.not_in(IN_FLIGHT))
            .order_by(builds.c.id.desc())
            .limit(FAILED_IN_A_ROW)
        )
    ).all()
    if len(rows) < FAILED_IN_A_ROW:
        return None
    newest = rows[0]
    for i, row in enumerate(rows):
        if row.status != "failed" or row.error in (*REPLACED, *DIED):
            return None
        alike = (error_head(row.error), row.config_digest, row.runs)
        if alike != (error_head(newest.error), newest.config_digest, newest.runs):
            return None
        if row.requested_by is not None and i < len(rows) - 1:  # a member's build starts a new row
            return None
    return rows


async def _halted_by_failures(conn: AsyncConnection, project_id: int, project: str) -> bool:
    """Whether a change of a core plugin's data must queue no build of a project, its newest builds having failed
    alike (``_failed_alike``); the project's admins are told once a row (HALTED_NOTICE)."""
    rows = await _failed_alike(conn, project_id)
    if rows is None:
        return False
    notes = tables.notifications
    since = rows[-1].finished_at
    told = await conn.scalar(
        select(
            exists().where(
                notes.c.project_id == project_id,
                notes.c.notice_kind == HALTED_NOTICE,
                *([notes.c.created_at >= since] if since is not None else []),
            )
        )
    )
    head = error_head(rows[0].error)
    if not told:
        await _halted_notice(
            conn,
            project_id,
            project,
            len(rows),
            body=(
                f"The last {len(rows)} builds of {project} failed the same way: {head}. A change of a plugin's data "
                "(a chat, a task) queues no new build of it until one succeeds, a push brings new runs, its knowledge "
                f"config changes or a member queues one: `evo-agents hub kg build --project {project}` once the cause "
                "is fixed."
            ),
            error=head,
        )
    log.warning(
        "kg build not queued after a plugin's change: the newest builds failed alike",
        extra={"project": project, "in_a_row": len(rows)},
    )
    return True


def _orphaned():
    """The builds still queued or running whose job ended, or is gone (``hub.prune_jobs``), with their project and
    the status of their job: no worker records them any more, so they would say queued or running for ever."""
    builds, jobs, projects = tables.kg_builds, procrastinate_jobs, tables.projects
    return (
        select(builds.c.id, builds.c.project_id, projects.c.name, builds.c.job_id, jobs.c.status.label("job_status"))
        .join_from(builds, projects, projects.c.id == builds.c.project_id)
        .join(jobs, jobs.c.id == builds.c.job_id, isouter=True)
        .where(builds.c.status.in_(IN_FLIGHT), or_(jobs.c.id.is_(None), jobs.c.status.in_(JOB_ENDED)))
        .order_by(builds.c.id)
    )


async def _fail_orphaned(context, manager) -> tuple[int, list[str], list[str]]:
    """Fail the builds ``_orphaned`` finds, a project at a time, and queue a build of each project none of whose
    builds came after them (``_requeue``): the runs they would have built then wait for nothing. A project whose queue
    stays busy keeps its builds as they are until the next pass. The builds failed, and the projects queued again and
    those ``_requeue`` stopped."""
    builds = tables.kg_builds
    async with context.engine.connect() as conn:
        found = (await conn.execute(_orphaned())).all()
    by_project: dict[tuple[int, str], list] = {}
    for row in found:
        by_project.setdefault((row.project_id, row.name), []).append(row)
    failed, queued, stopped = 0, [], []
    for (project_id, project), rows in by_project.items():
        try:
            async with context.engine.begin() as conn:
                result = await conn.execute(
                    update(builds)
                    .values(
                        status="failed",
                        error=ORPHANED,
                        started_at=func.coalesce(builds.c.started_at, func.now()),
                        finished_at=func.now(),
                    )
                    .where(builds.c.id.in_([row.id for row in rows]), builds.c.status.in_(IN_FLIGHT))
                    .returning(builds.c.id)
                )
                ended = result.scalars().all()
                if not ended:
                    continue
                later = exists().where(builds.c.project_id == project_id, builds.c.id > max(ended))
                if not await conn.scalar(select(later)):
                    build_id, halted = await _requeue(conn, manager, project_id, project, ended)
                    if build_id is not None:
                        queued.append(project)
                    elif halted:
                        stopped.append(project)
        except QueueBusy:
            log.warning("orphaned kg builds left for the next pass: the queue was busy", extra={"project": project})
            continue
        failed += len(ended)
        log.warning(
            "kg builds whose job ended unrecorded failed",
            extra={
                "project": project,
                "builds": ended,
                "jobs": {row.job_id: row.job_status for row in rows if row.id in ended},
            },
        )
    return failed, queued, stopped


def _headless():
    """The projects, not archived, whose newest build succeeded while the hub's tables hold no graph of their main
    branch: that build came before the tables (schema 0248) and uploaded a graph file instead (``artifact_sha256``, or
    ``artifact_pruned_at`` once the retention deleted it), so the project serves no graph until a build writes its
    rows. A project whose newest build failed, is in flight or came after 0248 is not among them, so a build that
    keeps failing is not queued again on every pass."""
    builds, heads, projects = tables.kg_builds, tables.kg_graph_heads, tables.projects
    earlier = builds.alias("earlier")
    newest = (
        select(func.max(earlier.c.id))
        .where(earlier.c.project_id == projects.c.id)
        .correlate(projects)
        .scalar_subquery()
    )
    head = exists().where(
        heads.c.project_id == projects.c.id, heads.c.branch_id == kg_graph_rows.MAIN, heads.c.build_id.is_not(None)
    )
    return (
        select(projects.c.id, projects.c.name)
        .join_from(projects, builds, builds.c.id == newest)
        .where(
            projects.c.archived_at.is_(None),
            builds.c.status == "succeeded",
            or_(builds.c.artifact_sha256.is_not(None), builds.c.artifact_pruned_at.is_not(None)),
            ~head,
        )
        .order_by(projects.c.id)
    )


async def _fill_headless(context, manager) -> list[str]:
    """Queue a build of each project ``_headless`` finds, which writes its graph into the hub's tables: the first
    passes after the upgrade to 0248 fill the tables of every project that had a graph. A busy queue leaves the
    project for the next pass."""
    async with context.engine.connect() as conn:
        found = (await conn.execute(_headless())).all()
    queued = []
    for project_id, project in found:
        try:
            async with context.engine.begin() as conn:
                build_id, _ = await queue_build(JobQueue(manager), conn, project_id, project, None)
        except QueueBusy:
            log.warning("kg build to fill the graph tables left for the next pass", extra={"project": project})
            continue
        if build_id is not None:
            queued.append(project)
    if queued:
        log.info("kg builds queued to fill the graph tables", extra={"projects": queued})
    return queued


async def _died_in_a_row(conn: AsyncConnection, project_id: int) -> tuple[int, bool]:
    """How many of the newest ended builds of a project in a row ended with their worker gone (DIED), back to the
    newest a member queued by hand, which starts the row; and whether one of them was told already (HALTED). A build
    that succeeded, or that its worker stopped cleanly (STOPPED), ends the row."""
    builds = tables.kg_builds
    rows = await conn.execute(
        select(builds.c.status, builds.c.error, builds.c.requested_by)
        .where(builds.c.project_id == project_id, builds.c.status.not_in(IN_FLIGHT))
        .order_by(builds.c.id.desc())
        .limit(DIED_READ)
    )
    count, told = 0, False
    for row in rows:
        if row.status != "failed" or row.error not in DIED:
            break
        count, told = count + 1, told or row.error == HALTED
        if row.requested_by is not None:
            break
    return count, told


async def no_graph_reason(conn: AsyncConnection, project_id: int, project: str) -> str:
    """What a kg tool tells a reader of a project the hub holds no graph of: the newest build's state, and its error
    when it failed, since pushing runs again does not help a build that fails on its input; the push when the project
    was never built."""
    builds = tables.kg_builds
    newest = (
        await conn.execute(
            select(builds.c.id, builds.c.status, builds.c.error)
            .where(builds.c.project_id == project_id)
            .order_by(builds.c.id.desc())
            .limit(1)
        )
    ).first()
    head = f"project {project} has no graph on the hub yet"
    if newest is None:
        return f"{head}: push its runs with `evo-agents hub kg push` (a build follows)"
    listed = f"`evo-agents hub kg builds --project {project}` lists them"
    if newest.status in IN_FLIGHT:
        return f"{head}: build {newest.id} is {newest.status}, and the graph is served once a build succeeds"
    if newest.status == "failed":
        return (
            f"{head}: its newest build {newest.id} failed ({error_head(newest.error)}), and the graph is served once "
            f"a build succeeds; pushing runs again does not fix a build that fails; {listed}"
        )
    return f"{head}: its newest build {newest.id} ({newest.status}) holds no graph; {listed}"


async def _requeue(
    conn: AsyncConnection, manager, project_id: int, project: str, ended: list[int]
) -> tuple[int | None, bool]:
    """Queue a build of a project whose builds ``ended`` ended with their worker gone, in ``conn``'s transaction:
    (its build id, False). When they make DIED_IN_A_ROW builds in a row (``_died_in_a_row``), queue none, record
    HALTED on them and tell the project's admins, once a row: (None, True)."""
    count, told = await _died_in_a_row(conn, project_id)
    if count < DIED_IN_A_ROW:
        build_id, _ = await queue_build(JobQueue(manager), conn, project_id, project, None)
        return build_id, False
    builds = tables.kg_builds
    await conn.execute(update(builds).values(error=HALTED).where(builds.c.id.in_(ended)))
    if not told:
        await _halted_notice(conn, project_id, project, count)
    log.warning("kg builds whose worker died not queued again", extra={"project": project, "in_a_row": count})
    return None, True


async def recover_stalled(context, manager) -> dict:
    """Fail the kg builds whose worker stopped sending heartbeats (killed, or its machine lost), which would hold
    their project's lock for ever, and queue a new build of each such project, unless its builds keep dying so
    (``_requeue``); then fail the builds whose job ended without recording them (``_fail_orphaned``), which hold no
    lock but would say queued or running for ever; last, queue a build of each project whose graph the hub's tables
    lack (``_fill_headless``), which never picks a project whose newest build failed."""
    from procrastinate.jobs import Status

    stalled = await manager.get_stalled_jobs(task_name=KG_BUILD, seconds_since_heartbeat=STALLED_AFTER)
    ended: dict[str, list[int]] = {}
    for job in stalled:
        await manager.finish_job(job, Status.FAILED, delete_job=False)
        builds = tables.kg_builds
        async with context.engine.begin() as conn:
            failed = await conn.execute(
                update(builds)
                .values(
                    status="failed",
                    error=STALLED,
                    started_at=func.coalesce(builds.c.started_at, func.now()),
                    finished_at=func.now(),
                )
                .where(builds.c.job_id == job.id, builds.c.status.in_(IN_FLIGHT))
                .returning(builds.c.id)
            )
        ended.setdefault(job.task_kwargs.get("project"), []).extend(failed.scalars().all())
    queued, halted = [], []
    for project in sorted(p for p in ended if isinstance(p, str)):
        async with context.engine.begin() as conn:
            row = (await conn.execute(project_id_query(project))).one_or_none()
            if row is not None:
                build_id, stopped = await _requeue(conn, manager, row[0], project, ended[project])
                (halted if stopped else queued).append(project)
    if stalled:
        log.warning(
            "abandoned kg builds failed and queued again",
            extra={"jobs": len(stalled), "projects": queued, "halted": halted},
        )
    orphaned, requeued, stopped = await _fail_orphaned(context, manager)
    filled = await _fill_headless(context, manager)
    return {
        "failed": len(stalled),
        "orphaned": orphaned,
        "queued": sorted({*queued, *requeued}),
        "halted": sorted({*halted, *stopped}),
        "filled": filled,
    }


async def _halted_notice(conn: AsyncConnection, project_id: int, project: str, count: int, **say) -> None:
    """``kg_build.tell_halted``, which the import-linter exception for the notice covers where it lives."""
    from evo_agents.hub.kg_build import tell_halted  # kg_build imports this module

    await tell_halted(conn, project_id, project, count, **say)

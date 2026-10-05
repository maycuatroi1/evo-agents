"""The hub's background jobs on the procrastinate queue in Postgres: their names, the lock of a project's kg build,
and deferring them from the api.

The worker (``evo_agents.hub.worker``) runs the jobs. The api only defers them, through ``JobQueue`` over its own
connection pool, so it needs no procrastinate App of its own: procrastinate allows one App per process, and that one
is the worker's.

A kg job of project P is deferred with ``defer_kg``: lock and queueing lock both ``kg:P``. The lock makes the jobs
holding it run one after the other; the queueing lock keeps at most one of them waiting, and deferring another while
one waits is a no-op. procrastinate's queueing lock alone still lets several run at once, and its lock alone lets
any number wait. A job deferred on the caller's connection runs inside a savepoint, so a defer refused because one
waits already leaves the caller's transaction usable.
"""

from __future__ import annotations

from procrastinate import PsycopgConnector, exceptions
from procrastinate.manager import JobManager
from procrastinate.tasks import configure_task
from psycopg_pool import AsyncConnectionPool

PING = "hub.ping"  # does nothing for a moment: proves a worker takes jobs
CLEANUP_UPLOADS = "hub.cleanup_uploads"  # hourly: uploads never committed, after 24 hours
PRUNE_JOBS = "hub.prune_jobs"  # daily: finished jobs, after 14 days
KG_BUILD = "hub.kg_build"  # one build of a project's knowledge graph, under the project's kg lock
RECOVER_KG_BUILDS = "hub.recover_kg_builds"  # every 5 minutes: kg builds whose worker died, failed and queued again
PRUNE_KG_ARTIFACTS = "hub.prune_kg_artifacts"  # hourly: artifacts of graphs older than each project's newest few


def kg_lock(project: str) -> str:
    """The lock and queueing lock of project ``project``'s kg builds."""
    return f"kg:{project}"


class JobQueue:
    """Defers jobs over an open pool that someone else owns and closes."""

    def __init__(self, manager: JobManager):
        self._manager = manager

    @classmethod
    async def open(cls, pool: AsyncConnectionPool) -> JobQueue:
        connector = PsycopgConnector()
        await connector.open_async(pool=pool)  # an external pool: closing the connector leaves it open
        return cls(JobManager(connector))

    async def defer(
        self, task: str, *, lock: str | None = None, queueing_lock: str | None = None, connection=None, **kwargs
    ) -> int | None:
        """Queue a job of ``task`` with ``kwargs`` (JSON values); its id, or None when a job with the same
        ``queueing_lock`` waits already, which counts as queued. With ``connection``, the job is queued in that
        connection's transaction and exists only if it commits."""
        deferrer = configure_task(
            name=task, job_manager=self._manager, lock=lock, queueing_lock=queueing_lock, connection=connection
        )
        if connection is None or connection.autocommit:
            try:
                return await deferrer.defer_async(**kwargs)
            except exceptions.AlreadyEnqueued:
                return None
        # The refusal is an error in Postgres, which would abort the caller's whole transaction without the savepoint.
        await connection.execute("SAVEPOINT hub_defer")
        try:
            job_id = await deferrer.defer_async(**kwargs)
        except exceptions.AlreadyEnqueued:
            await connection.execute("ROLLBACK TO SAVEPOINT hub_defer")
            return None
        await connection.execute("RELEASE SAVEPOINT hub_defer")
        return job_id

    async def defer_kg(self, task: str, project: str, /, *, connection=None, **kwargs) -> int | None:
        """``defer`` with lock and queueing lock ``kg:<project>``: one kg job of the project runs at a time, and at
        most one waits. ``project`` is positional only, so the job's own arguments may hold a ``project`` too."""
        key = kg_lock(project)
        return await self.defer(task, lock=key, queueing_lock=key, connection=connection, **kwargs)

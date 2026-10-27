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

The caller's connection is an ``AsyncConnection`` of the hub's engine (``evo_agents.hub.db``): procrastinate, whose
only async connector is psycopg's, gets the psycopg connection under it through ``driver``, and the savepoint is the
engine's ``begin_nested()``. This is the one place the hub hands out that psycopg connection.
"""

from __future__ import annotations

from procrastinate import PsycopgConnector, exceptions
from procrastinate.manager import JobManager
from procrastinate.tasks import configure_task
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.db import driver

PING = "hub.ping"  # does nothing for a moment: proves a worker takes jobs
CLEANUP_UPLOADS = "hub.cleanup_uploads"  # hourly: uploads never committed, after 24 hours
PRUNE_JOBS = "hub.prune_jobs"  # daily: finished jobs, after 14 days
KG_BUILD = "hub.kg_build"  # one build of a project's knowledge graph, under the project's kg lock
RECOVER_KG_BUILDS = "hub.recover_kg_builds"  # every 5 minutes: kg builds whose worker died, failed and queued again
PRUNE_KG_ARTIFACTS = "hub.prune_kg_artifacts"  # hourly: artifacts of graphs older than each project's newest few
RECOVER_RUNS = "hub.recover_runs"  # every minute: runs whose lease ran out become lost and are queued again
PRUNE_RUN_EVENTS = "hub.prune_run_events"  # daily: events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago
PRUNE_AUDIT = "hub.prune_audit"  # daily: audit rows older than EVO_HUB_AUDIT_DAYS
DELIVER_NOTIFICATIONS = "hub.deliver_notifications"  # every minute: notifications due on their channels, retried
FIRE_SCHEDULES = "hub.fire_schedules"  # every minute: the night shift of each charter queues its next run
PRUNE_DIGESTS = "hub.prune_digests"  # daily: session digests not pushed again for 90 days
PRUNE_TRANSCRIPTS = "hub.prune_transcripts"  # daily: the content of transcripts past EVO_HUB_TRANSCRIPT_DAYS
PRUNE_SIGNALS = "hub.prune_signals"  # daily: signals measured more than EVO_HUB_SIGNAL_DAYS ago
ANALYZE_FACETS = "hub.analyze_facets"  # every 5 minutes: the facet of each new transcript, digest and ended run
INDEX_HISTORY = "hub.index_history"  # every 5 minutes: the words of each new part of a transcript and ended run
CURATOR_COLLECT = "curator.collect"  # every minute: each charter's night figures and its review run, in its window
CURATOR_BRIEF = "curator.brief"  # every minute: the morning brief of each charter, at its brief_at
CURATOR_CHANGES = "curator.changes"  # every minute: the Curator's pull requests opened, judged, merged or left open
MERGE_QUEUE = "hub.merge_queue"  # every minute: the merge requests of the merge queue on GitLab opened and gated
CURATOR_OUTCOMES = "curator.outcomes"  # every 10 minutes: the figures of merged changes counted again, outcome_days on
REPLAY_SUITE = "curator.replay_suite"  # hourly: each charter's suite of replay tasks, from the plans' steps done
PLUGIN_JOBS = "hub.plugin_jobs"  # every minute: the plugins' jobs whose cron takes this minute, each queued once
PLUGIN_JOB = "hub.plugin_job"  # one run of one plugin's job (evo_agents.hub.plugin_jobs)
DELIVER_PLUGIN_EVENTS = "hub.deliver_plugin_events"  # every minute: events and notifications due for plugins, retried
READ_NIGHTLY = "hub.read_nightly"  # every minute: where the tag nightly points, for every heartbeat answer
VALUE_CHECK = "hub.value_check"  # on a batch of a project's signals: its plugins' value_check jobs, for it alone
RELEASE_SCHEDULES = "hub.release_schedules"  # every minute: the daily check of each release schedule, at its time
LOOP_TICKS = "hub.loop_ticks"  # every 5 minutes: a tick of the Loop of each project that has it on
LOOP_TICK = "hub.loop_tick"  # one tick of a project's Loop, under its lock; also queued by each of its events
LOOP_NOTICES = "hub.loop_notices"  # every 5 minutes: the month's review of goals and the Loop's brief, when due
OPPORTUNITY_DEADLINES = "hub.opportunity_deadlines"  # hourly: owners told as the window of an open opportunity closes
TODAY_DIGEST = "hub.today_digest"  # every minute: the summary push of Today, at each member's time, once a day
MEMORY_UPKEEP = "hub.memory_upkeep"  # daily: agents' memories nobody opened for 30 days archived, stale ones marked
CONTEXT_ANSWER_REUSE = (
    "hub.context_answer_reuse"  # daily: the answers loaded the day before, as a signal, per project with them on
)
EXPIRE_SEATS = "hub.expire_seats"  # hourly: org seats past their end date ended, the leases of people at A0 back
MARKET_REFRESH = "hub.market_refresh"  # every 6 hours: each plugin market's index and its plugins' latest releases
MARKET_LICENSES = "hub.market_licenses"  # hourly: a buying hub's orders, licences, installs, reminders and lapses
MARKET_SIGNALS = "hub.market_signals"  # daily: a seller's active licences and the month's revenue, as signals
REAP_POOL_NODES = "hub.reap_pool_nodes"  # hourly: pool nodes offline past their pool's reap_after_days revoked


def kg_lock(project: str) -> str:
    """The lock and queueing lock of project ``project``'s kg builds."""
    return f"kg:{project}"


def value_check_lock(project_id: int) -> str:
    """The lock and queueing lock of project ``project_id``'s value checks: one runs at a time, and at most one
    waits, which the batches that come meanwhile join."""
    return f"value-check:{project_id}"


def loop_lock(project_id: int) -> str:
    """The lock and queueing lock of project ``project_id``'s Loop ticks: one runs at a time, and at most one waits,
    which the events that come meanwhile join."""
    return f"loop:{project_id}"


class JobQueue:
    """Defers jobs over an open pool that someone else owns and closes."""

    def __init__(self, manager: JobManager):
        self._manager = manager

    @classmethod
    async def open(cls, pool: AsyncConnectionPool) -> JobQueue:
        connector = PsycopgConnector()
        await connector.open_async(pool=pool)  # an external pool: closing the connector leaves it open
        return cls(JobManager(connector))

    @classmethod
    def in_transaction(cls) -> JobQueue:
        """A queue with no pool, which defers only on the caller's connection (``connection=``): for code handed a
        transaction and nothing else, such as a chat message that queues builds of the graphs holding it, wherever it
        is sent from. Deferring through it without a connection fails."""
        return cls(JobManager(PsycopgConnector()))

    async def defer(
        self,
        task: str,
        *,
        lock: str | None = None,
        queueing_lock: str | None = None,
        connection: AsyncConnection | None = None,
        **kwargs,
    ) -> int | None:
        """Queue a job of ``task`` with ``kwargs`` (JSON values); its id, or None when a job with the same
        ``queueing_lock`` waits already, which counts as queued. With ``connection``, the job is queued in that
        connection's transaction and exists only if it commits."""
        if connection is None:
            deferrer = configure_task(name=task, job_manager=self._manager, lock=lock, queueing_lock=queueing_lock)
            try:
                return await deferrer.defer_async(**kwargs)
            except exceptions.AlreadyEnqueued:
                return None
        deferrer = configure_task(
            name=task,
            job_manager=self._manager,
            lock=lock,
            queueing_lock=queueing_lock,
            connection=await driver(connection),
        )
        # The refusal is an error in Postgres, which would abort the caller's whole transaction without the
        # savepoint; leaving the block with it rolls back to the savepoint.
        try:
            async with connection.begin_nested():
                return await deferrer.defer_async(**kwargs)
        except exceptions.AlreadyEnqueued:
            return None

    async def defer_kg(
        self, task: str, project: str, /, *, connection: AsyncConnection | None = None, **kwargs
    ) -> int | None:
        """``defer`` with lock and queueing lock ``kg:<project>``: one kg job of the project runs at a time, and at
        most one waits. ``project`` is positional only, so the job's own arguments may hold a ``project`` too."""
        key = kg_lock(project)
        return await self.defer(task, lock=key, queueing_lock=key, connection=connection, **kwargs)

"""``evo-agents hub worker``: the process that runs the hub's background jobs, from the same image as the api.

The worker migrates the database like the api (under the advisory lock), then takes jobs from the procrastinate
queue in Postgres, ``concurrency`` at a time, until SIGTERM or SIGINT. On the first signal it takes no new job and
waits up to SHUTDOWN_GRACE for the running ones; procrastinate aborts what is still running after that, and a second
signal kills the process. Stopped by a signal, it exits 0; stopped by anything else, such as losing Postgres, it
exits 1, so the container is restarted and the failure shows. It needs the blob store: what it builds lives in R2,
and it removes the uploads nobody committed. ``--data-dir`` is its cache, which can be deleted at any time.

Jobs (names in ``evo_agents.hub.jobs``):

- ``hub.ping``: waits a few seconds and returns; deferring it shows that a worker takes jobs.
- ``hub.cleanup_uploads``, hourly: objects under ``uploads/`` and ``blob_uploads`` rows older than STALE_AFTER, and
  the attachments no message took within ``attachments.STALE_AFTER`` (``attachment_prune.remove_stale``).
- ``hub.expire_seats``, hourly: the org seats past their end date or that of a cohort holding them are ended, audited
  with the hub as actor, and the leases still out of the runs of every person whose primary seat ended are taken back
  and their GitHub tokens revoked (``evo_agents.hub.server.org_expiry``).
- ``hub.reap_pool_nodes``, hourly: each worker pool node offline longer than its pool's ``reap_after_days`` is
  revoked as its owner's revoke would, with the hub as actor (``evo_agents.hub.server.worker_reaper``); workers
  outside a pool are left alone.
- ``hub.prune_jobs``, daily: finished jobs older than JOB_RETENTION, failed ones included.
- ``hub.kg_build``: one build of a project's knowledge graph (``evo_agents.hub.kg_build``), deferred by the api
  under the project's kg lock.
- ``hub.recover_kg_builds``, every 5 minutes: kg builds whose worker stopped sending heartbeats are failed, which
  frees their project's lock, and their projects get a new build.
- ``hub.prune_kg_artifacts``, hourly: the artifacts of each project's graphs beyond its EVO_HUB_KG_KEEP_ARTIFACTS
  newest leave the bucket (``evo_agents.hub.kg_prune``).
- ``hub.recover_runs``, every minute: runs whose worker stopped extending the lease become lost, and the next attempt
  of their step is queued, or fail on their last attempt; runs past their timeout fail; a plan run that waited
  EVO_HUB_DECISION_WAIT_SECONDS (24 hours by default) for an answer is parked, and one parked for 7 days cancelled
  (``evo_agents.hub.server.run_state``). The runs that end give back their leases, and the GitHub tokens of every lease
  given back and not revoked at GitHub yet are revoked there (``evo_agents.hub.server.credentials``).
- ``hub.deliver_notifications``, every minute: the notification deliveries that are due, each handed to its
  channel's class, tried again with a backoff and failed after 5 tries (``evo_agents.hub.server.notifications``).
- ``hub.prune_run_events``, daily: the events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago, the outputs of
  source tool of those runs that no question or chat message carries (``evo_agents.hub.output_prune``), and the sealed
  values of the GitHub tokens leased to runs that are past their end.
- ``hub.prune_audit``, daily: the audit rows older than EVO_HUB_AUDIT_DAYS (365 by default).
- ``hub.fire_schedules``, every minute: each schedule of a project's charter that is not paused queues its next run
  inside its window and within its night's budget, the night's review run first, and cancels its queued runs outside
  it; its circuit breaker pauses a project's night shift once the charter's max_failed_in_a_row jobs of a night in a
  row failed or were reverted (``evo_agents.hub.server.curator``, ``evo_agents.hub.server.outcomes``).
- ``curator.collect``, every minute: the health of each charter's knowledge graph, measured every 6 hours against the
  default branches on GitHub (``evo_agents.hub.server.kg_health``); in each charter's window, the night's figures of
  the project, counted once without any model, and its review run; and the deferred proposals whose time has passed
  open again (``evo_agents.hub.server.collect``).
- ``hub.plugin_jobs``, every minute: a ``hub.plugin_job`` for each job of a plugin enabled in a project whose cron
  takes the minute, queued once at a time; ``hub.plugin_job`` runs it (``evo_agents.hub.plugin_jobs``).
- ``hub.value_check``: deferred with each batch of a project's signals that stored any (``evo_agents.hub.signals``),
  one at a time per project and at most one waiting: the plugins' jobs named value_check of that project, for it alone
  (``evo_agents.hub.plugin_jobs.run_on_signals``).
- ``hub.deliver_plugin_events``, every minute: the events and notifications due for plugins, at least once, tried
  again with a backoff (``evo_agents.hub.plugin_events``).
- ``hub.read_nightly``, every minute: the commit the tag nightly of evo-agents points to, once for the whole hub
  whatever its number of workers, kept in hub_nightly with the time of the read for the api to hand every daemon in
  its heartbeat answer; a read that fails keeps the last one and logs a line (``evo_agents.hub.nightly``).
- ``hub.release_schedules``, every minute, one pass at a time: each project's release schedule whose local time came
  within the hour gets the check of that day, once: without any model, the newest release tag and the commit of the
  target ref are compared on the packaging paths, and a change opens a cycle and queues its cut run, with the workers'
  App reading GitHub (``evo_agents.hub.server.release_check``, ``evo_agents.hub.releases``); then each release run
  that is done with its branch is landed: its pull request opened and merged, and its merge commit tagged, with the
  same App (``evo_agents.hub.server.release_land``). The pass is the server's: the ``hub worker`` command hands it
  down (``HubContext.release_pass``), since this module imports no server module beyond its job list
  (docs/structure.md, section Package boundaries).
- ``hub.loop_ticks``, every 5 minutes: a ``hub.loop_tick`` of each project whose Loop is on, queued under the
  project's Loop lock (``evo_agents.hub.loop_jobs``); ``hub.loop_tick`` runs one tick of a project's Loop, one at a
  time per project and at most one waiting, also queued by each event of the project. The tick is the server's
  (``evo_agents.hub.server.loop.tick``): the ``hub worker`` command hands it down (``HubContext.loop_tick``), as it
  does the release pass.
- ``hub.loop_notices``, every 5 minutes: for each project whose Loop is on, the notice goal_review on the first day of
  a month, and the Loop's part of the morning brief (loop_brief) at 07:00 for a project whose Curator sends none, in
  the Loop's zone, each once (``evo_agents.hub.server.loop.notices``, through ``evo_agents.hub.server.brief``).
- ``hub.opportunity_deadlines``, hourly: the owner of each open opportunity whose window ends gets one notice at 14
  days left, one at 3 and one the day after, each once per value of the date; the hub changes no status
  (``evo_agents.hub.server.opportunity_deadlines``).
- ``hub.today_digest``, every minute: the summary push of Today to each member with a push device who has been away
  from the hub for 8 hours, at the local time they chose, when something changed since they last looked, once a day
  (``evo_agents.hub.server.today_digest``, through ``evo_agents.hub.server.brief``).
- ``hub.prune_digests``, daily: the session digests not pushed again for 90 days
  (``evo_agents.hub.server.digests``).
- ``hub.prune_transcripts``, daily: the content of the session transcripts that got no part for
  EVO_HUB_TRANSCRIPT_DAYS days, and the blobs of kind transcript no part names (``evo_agents.hub.transcript_prune``).
- ``hub.memory_upkeep``, daily: the memories of agent profiles nobody opened for 30 days archived, and those citing
  code the project's knowledge graph no longer has marked stale (``evo_agents.hub.server.memory_upkeep``), handed
  down by the ``hub worker`` command (``HubContext.memory_upkeep``), as the release pass is.
- ``hub.prune_signals``, daily: the signals measured more than EVO_HUB_SIGNAL_DAYS days ago, 400 by default
  (``evo_agents.hub.signals``).
- ``hub.analyze_facets``, every 5 minutes, one pass at a time: the deterministic facet of each session whose transcripts
  or digest came, and of each run that ended, made once and again when its content or the facet's version changed
  (``evo_agents.hub.facet_job``).
- ``hub.index_history``, every 5 minutes, one pass at a time: the words of each part of a transcript and of each run
  that ended, for search, once (``evo_agents.hub.history_index``).
- ``curator.brief``, every minute: at each charter's brief_at, in its time zone, the morning brief of the night to the
  owner of the project's schedule, once a day (``evo_agents.hub.server.brief``); on Monday, for a charter that runs
  probes, the weekly report of their ISO week just ended too, once a week (``evo_agents.hub.server.probe_report``).
- ``curator.changes``, every minute: the Curator's changes move on: their pull requests opened, the Judge's check runs
  written, the tier 0 ones merged when everything allows it, the others left open for their owner and read again
  hourly for a merge or a close by hand; and the rulesets checked, the one a planned change waits for at the next
  pass and hourly while it does not pass, every other once a day (``evo_agents.hub.server.changes``, ``.protection``).
- ``hub.merge_queue``, every minute, one pass at a time: the merge queue on GitLab of each project whose autonomy
  policy has a repo in mode auto: the merge requests of its runs opened, those in scope taken, and each one gated
  until its Judges' verdicts let it wait for its turn, with the reason it waits or is blocked
  (``evo_agents.hub.server.merge_gate``, ``.merge_judge``).
- ``curator.outcomes``, every 10 minutes: the figures of each change of the Curator merged the charter's outcome_days
  ago counted again, its outcome (keep, revert or unclear) in its proposal's ledger, and a revert proposed when they
  got worse (``evo_agents.hub.server.outcomes``).
- ``curator.replay_suite``, hourly, one pass at a time: each project with a charter gets a replay task for each step
  of its plans that is done, has a verify of one line and names its commits, added once with its fixed split (keyed by
  the session secret, none without it) and never changed (``evo_agents.hub.replay_suite``).

procrastinate allows one App per process; ``queue`` is that App here. ``run`` gives it a connector of its own for
the time it runs, and the jobs reach the hub's tables, the blob store, the sealing key and the GitHub App through
``HubContext``: the connector's pool, and the SQLAlchemy engine on that pool (``evo_agents.hub.db``), made after
the pool opens and disposed of before it closes. The worker handles SIGTERM and SIGINT itself rather than through
procrastinate, which cannot tell a signal from a failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from procrastinate import App, JobContext, PsycopgConnector
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import delete, func

from evo_agents import __version__
from evo_agents.hub import jobs, tables
from evo_agents.hub.blobs import STALE_AFTER, BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import CONNECT_TIMEOUT, make_engine
from evo_agents.hub.log import redact_dsn
from evo_agents.hub.migrate import migrate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine

    from evo_agents.hub.server.github_app import GitHubApp
    from evo_agents.hub.server.sealing import Sealer

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub-worker"
SHUTDOWN_GRACE = 25.0  # seconds running jobs get after SIGTERM; the container needs a longer stop grace period
PING_MAX_SECONDS = 30.0
JOB_RETENTION = timedelta(days=14)
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
SIDE_CONNECTIONS = 3  # the pool serves the jobs plus procrastinate's fetching, heartbeat and periodic deferrer

queue = App(connector=PsycopgConnector())  # never opened as is: ``run`` swaps in a connector with the DSN


@dataclass(frozen=True)
class HubContext:
    """What a job reaches the hub through."""

    config: HubConfig
    pool: AsyncConnectionPool
    blobs: BlobStore
    data_dir: Path
    sealer: Sealer | None = None  # None without EVO_HUB_SECRETS_KEY
    github_app: GitHubApp | None = None  # None without EVO_HUB_GITHUB_APP_*: no GitHub token to revoke
    engine: AsyncEngine | None = None  # on ``pool``; made on it when not given
    curator_app: GitHubApp | None = None  # None without EVO_HUB_CURATOR_APP_*: no ruleset checked
    # the server's release pass (evo_agents.hub.server.release_check), which the hub worker command hands down;
    # None checks and lands no release
    release_pass: Callable[[AsyncEngine, GitHubApp | None], Awaitable[dict]] | None = None
    # the server's tick of a project's Loop (evo_agents.hub.server.loop.tick), handed down the same way; None ticks
    # no Loop
    loop_tick: Callable[[AsyncEngine, int], Awaitable[dict]] | None = None
    # the server's upkeep of agents' memories (evo_agents.hub.server.memory_upkeep), handed down the same way; None
    # keeps none
    memory_upkeep: Callable[[AsyncEngine, BlobStore, Path], Awaitable[dict]] | None = None

    def __post_init__(self):
        if self.engine is None:  # an engine keeps no connection of its own (NullPool): nothing to dispose of
            object.__setattr__(self, "engine", make_engine(self.pool))


def hub(context: JobContext) -> HubContext:
    return context.additional_context["hub"]


@queue.task(name=jobs.PING, pass_context=True)
async def ping(context: JobContext, seconds: float = 0.0, note: str = "") -> dict:
    """Wait ``seconds`` (at most PING_MAX_SECONDS) and return; for checking that a worker runs jobs."""
    await asyncio.sleep(min(max(float(seconds), 0.0), PING_MAX_SECONDS))
    log.info("ping", extra={"job_id": context.job.id, "note": str(note)[:200]})
    return {"worker": context.worker_name, "job_id": context.job.id}


async def remove_stale_uploads(store: BlobStore, engine: AsyncEngine, now: datetime | None = None) -> dict:
    """Delete the objects under ``uploads/`` and the ``blob_uploads`` rows older than STALE_AFTER: uploads nobody
    committed in time. The rows go first, so an upload whose row is gone can no longer be committed while its object
    is being deleted."""
    cutoff = (now or datetime.now(UTC)) - STALE_AFTER
    async with engine.begin() as conn:
        uploads = tables.blob_uploads
        rows = (await conn.execute(delete(uploads).where(uploads.c.created_at < cutoff))).rowcount
    objects = await asyncio.to_thread(store.remove_stale_uploads, cutoff)
    log.info("stale uploads removed", extra={"objects": objects, "rows": rows, "cutoff": cutoff.isoformat()})
    return {"objects": objects, "rows": rows}


async def prune_audit(engine: AsyncEngine, days: int) -> dict:
    """Delete the audit rows older than ``days`` days (EVO_HUB_AUDIT_DAYS), in one statement on audit_at_idx. Schema
    0001 rejects an update of an audit row, not its deletion; nothing references the rows."""
    trail = tables.audit
    async with engine.begin() as conn:
        deleted = (await conn.execute(delete(trail).where(trail.c.at < func.now() - timedelta(days=days)))).rowcount
    log.info("audit pruned", extra={"deleted": deleted, "days": days})
    return {"deleted": deleted, "days": days}


@queue.periodic(cron="17 * * * *")
@queue.task(name=jobs.CLEANUP_UPLOADS, pass_context=True, queueing_lock=jobs.CLEANUP_UPLOADS)
async def cleanup_uploads(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.attachment_prune import remove_stale

    found = hub(context)
    removed = await remove_stale_uploads(found.blobs, found.engine)
    return {**removed, **await remove_stale(found.engine, found.blobs)}


@queue.periodic(cron="5 * * * *")
@queue.task(name=jobs.EXPIRE_SEATS, pass_context=True, queueing_lock=jobs.EXPIRE_SEATS)
async def expire_seats(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.run_state import expire_seats as expire

    found = hub(context)
    return await expire(found.engine, found.sealer, found.github_app or getattr(found, "curator_app", None))


@queue.periodic(cron="41 */6 * * *")
@queue.task(name=jobs.MARKET_REFRESH, pass_context=True, queueing_lock=jobs.MARKET_REFRESH)
async def market_refresh(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.market_refresh import refresh

    found = hub(context)
    done = await refresh(found.engine, found.config, found.sealer)
    return {"sources": len(done), "failed": sum(1 for item in done if item["error"])}


@queue.periodic(cron="23 * * * *")
@queue.task(name=jobs.MARKET_LICENSES, pass_context=True, queueing_lock=jobs.MARKET_LICENSES)
async def market_licenses(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents import __version__
    from evo_agents.hub.server.market_install import Installing
    from evo_agents.hub.server.market_licenses import check_licenses
    from evo_agents.plugins.signing import default_sigstore

    found = hub(context)
    installing = Installing(found.engine, found.config, default_sigstore(), __version__, found.blobs)
    done = await check_licenses(installing, found.sealer)
    return {
        "checked": done.checked,
        "received": len(done.received),
        "installed": len(done.installed),
        "reminded": len(done.reminded),
        "lapsed": len(done.lapsed),
        "errors": len(done.errors),
    }


@queue.periodic(cron="17 3 * * *")
@queue.task(name=jobs.MARKET_SIGNALS, pass_context=True, queueing_lock=jobs.MARKET_SIGNALS)
async def market_signals(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.seller import push_signals

    found = hub(context)
    return await push_signals(found.engine, found.config)


@queue.periodic(cron="47 * * * *")
@queue.task(name=jobs.REAP_POOL_NODES, pass_context=True, queueing_lock=jobs.REAP_POOL_NODES)
async def reap_pool_nodes(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.run_state import reap_pool_nodes as reap

    found = hub(context)
    return await reap(found.engine, found.sealer, found.github_app or getattr(found, "curator_app", None))


@queue.task(name=jobs.KG_BUILD, pass_context=True)
async def kg_build(context: JobContext, project: str, build_id: int | None = None) -> dict:
    from evo_agents.hub.kg_build import run_build

    return await run_build(hub(context), project, build_id, context.job.id, context.app.job_manager)


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.RECOVER_KG_BUILDS, pass_context=True, queueing_lock=jobs.RECOVER_KG_BUILDS)
async def recover_kg_builds(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.kg_build import recover_stalled

    return await recover_stalled(hub(context), context.app.job_manager)


@queue.periodic(cron="31 * * * *")
@queue.task(name=jobs.PRUNE_KG_ARTIFACTS, pass_context=True, queueing_lock=jobs.PRUNE_KG_ARTIFACTS)
async def prune_kg_artifacts(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.kg_prune import prune

    found = hub(context)
    report = await prune(found.engine, found.blobs, found.config.kg_keep_artifacts)
    return report.summary()


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.RECOVER_RUNS, pass_context=True, queueing_lock=jobs.RECOVER_RUNS)
async def recover_runs(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.run_state import recover_runs as recover

    found = hub(context)
    return await recover(
        found.engine,
        decision_wait=timedelta(seconds=found.config.decision_wait_seconds),
        sealer=found.sealer,
        # a token is revoked with itself: either App's client does
        github_app=found.github_app or getattr(found, "curator_app", None),
    )


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.DELIVER_NOTIFICATIONS, pass_context=True, queueing_lock=jobs.DELIVER_NOTIFICATIONS)
async def deliver_notifications(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.notifications import deliver_notifications as deliver

    found = hub(context)
    return await deliver(found.engine, config=found.config, blobs=found.blobs)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.FIRE_SCHEDULES, pass_context=True, queueing_lock=jobs.FIRE_SCHEDULES)
async def fire_schedules(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.curator import fire_schedules as fire

    found = hub(context)
    return await fire(found.engine, curator_app=getattr(found, "curator_app", None) is not None)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.CURATOR_COLLECT, pass_context=True, queueing_lock=jobs.CURATOR_COLLECT)
async def curator_collect(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.collect import collect

    found = hub(context)
    app = found.github_app or found.curator_app
    return await collect(found.engine, github_app=app, blobs=found.blobs, data_dir=found.data_dir)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.CURATOR_BRIEF, pass_context=True, queueing_lock=jobs.CURATOR_BRIEF)
async def curator_brief(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.brief import send_briefs

    return await send_briefs(hub(context).engine)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.CURATOR_CHANGES, pass_context=True, queueing_lock=jobs.CURATOR_CHANGES)
async def curator_changes(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.changes import advance

    found = hub(context)
    curator_app = getattr(found, "curator_app", None)
    return await advance(found.engine, found.github_app, curator_app, blobs=found.blobs, data_dir=found.data_dir)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.MERGE_QUEUE, pass_context=True, lock=jobs.MERGE_QUEUE, queueing_lock=jobs.MERGE_QUEUE)
async def merge_queue(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.changes import merge_gate  # the queue's job, through the module of the Curator's changes

    found = hub(context)
    return await merge_gate.advance(found.engine, found.sealer, blobs=found.blobs, data_dir=found.data_dir)


@queue.periodic(cron="*/10 * * * *")
@queue.task(name=jobs.CURATOR_OUTCOMES, pass_context=True, queueing_lock=jobs.CURATOR_OUTCOMES)
async def curator_outcomes(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.outcomes import measure_outcomes

    return await measure_outcomes(hub(context).engine)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.PLUGIN_JOBS, pass_context=True, queueing_lock=jobs.PLUGIN_JOBS)
async def plugin_jobs(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.plugin_jobs import fire

    return await fire(hub(context).engine, context.app.job_manager, timestamp)


@queue.task(name=jobs.PLUGIN_JOB, pass_context=True)
async def plugin_job(context: JobContext, plugin: str, job: str) -> dict:
    from evo_agents.hub.plugin_jobs import run_job

    found = hub(context)
    return await run_job(found.engine, found.config, plugin, job)


@queue.task(name=jobs.VALUE_CHECK, pass_context=True)
async def value_check(context: JobContext, project_id: int) -> dict:
    from evo_agents.hub.plugin_jobs import run_on_signals

    found = hub(context)
    return await run_on_signals(found.engine, found.config, project_id)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.DELIVER_PLUGIN_EVENTS, pass_context=True, queueing_lock=jobs.DELIVER_PLUGIN_EVENTS)
async def deliver_plugin_events(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.plugin_events import deliver

    found = hub(context)
    return await deliver(found.engine, config=found.config)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.READ_NIGHTLY, pass_context=True, queueing_lock=jobs.READ_NIGHTLY)
async def read_nightly(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.nightly import refresh

    return await refresh(hub(context).engine)


@queue.periodic(cron="* * * * *")
@queue.task(
    name=jobs.RELEASE_SCHEDULES, pass_context=True, lock=jobs.RELEASE_SCHEDULES, queueing_lock=jobs.RELEASE_SCHEDULES
)
async def release_schedules(context: JobContext, timestamp: int | None = None) -> dict:
    found = hub(context)
    release_pass = getattr(found, "release_pass", None)
    if release_pass is None:
        log.warning("no release pass was handed to this worker: no release schedule is checked")
        return {"no_release_pass": 1}
    return await release_pass(found.engine, found.github_app)


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.LOOP_TICKS, pass_context=True, queueing_lock=jobs.LOOP_TICKS)
async def loop_ticks(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.loop_jobs import queue_ticks

    return await queue_ticks(hub(context).engine, context.app.job_manager)


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.LOOP_NOTICES, pass_context=True, queueing_lock=jobs.LOOP_NOTICES)
async def loop_notices(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.brief import send_loop_notices

    return await send_loop_notices(hub(context).engine)


@queue.periodic(cron="7 * * * *")
@queue.task(name=jobs.OPPORTUNITY_DEADLINES, pass_context=True, queueing_lock=jobs.OPPORTUNITY_DEADLINES)
async def opportunity_deadlines(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.opportunity_deadlines import remind

    return await remind(hub(context).engine)


@queue.periodic(cron="* * * * *")
@queue.task(name=jobs.TODAY_DIGEST, pass_context=True, queueing_lock=jobs.TODAY_DIGEST)
async def today_digest(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.brief import send_today_digests

    found = hub(context)
    return await send_today_digests(found.engine, config=found.config)


@queue.task(name=jobs.LOOP_TICK, pass_context=True)
async def loop_tick(context: JobContext, project_id: int) -> dict:
    found = hub(context)
    tick = getattr(found, "loop_tick", None)
    if tick is None:
        log.warning("no loop tick was handed to this worker: no Loop ticks", extra={"project_id": project_id})
        return {"no_loop_tick": 1}
    return await tick(found.engine, project_id)


@queue.periodic(cron="13 4 * * *")
@queue.task(name=jobs.PRUNE_RUN_EVENTS, pass_context=True, queueing_lock=jobs.PRUNE_RUN_EVENTS)
async def prune_run_events(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.run_state import prune_run_events as prune

    found = hub(context)
    return await prune(found.engine, found.config.run_log_days, found.blobs)


@queue.periodic(cron="33 4 * * *")
@queue.task(name=jobs.PRUNE_AUDIT, pass_context=True, queueing_lock=jobs.PRUNE_AUDIT)
async def prune_audit_rows(context: JobContext, timestamp: int | None = None) -> dict:
    found = hub(context)
    return await prune_audit(found.engine, found.config.audit_days)


@queue.periodic(cron="23 4 * * *")
@queue.task(name=jobs.PRUNE_DIGESTS, pass_context=True, queueing_lock=jobs.PRUNE_DIGESTS)
async def prune_digests(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.server.digests import prune_digests as prune

    return await prune(hub(context).engine)


@queue.periodic(cron="53 4 * * *")
@queue.task(name=jobs.PRUNE_TRANSCRIPTS, pass_context=True, queueing_lock=jobs.PRUNE_TRANSCRIPTS)
async def prune_transcripts(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.transcript_prune import prune_transcripts as prune

    found = hub(context)
    return await prune(found.engine, found.blobs, found.config.transcript_days)


@queue.periodic(cron="27 5 * * *")
@queue.task(name=jobs.MEMORY_UPKEEP, pass_context=True, lock=jobs.MEMORY_UPKEEP, queueing_lock=jobs.MEMORY_UPKEEP)
async def memory_upkeep(context: JobContext, timestamp: int | None = None) -> dict:
    found = hub(context)
    kept = getattr(found, "memory_upkeep", None)
    if kept is None:
        log.warning("no memory upkeep was handed to this worker: no agent's memory is archived or marked stale")
        return {"no_memory_upkeep": 1}
    return await kept(found.engine, found.blobs, found.data_dir)


@queue.periodic(cron="3 5 * * *")
@queue.task(name=jobs.PRUNE_SIGNALS, pass_context=True, queueing_lock=jobs.PRUNE_SIGNALS)
async def prune_signals(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.signals import prune_signals as prune

    found = hub(context)
    return await prune(found.engine, found.config.signal_days)


@queue.periodic(cron="41 * * * *")
@queue.task(name=jobs.REPLAY_SUITE, pass_context=True, lock=jobs.REPLAY_SUITE, queueing_lock=jobs.REPLAY_SUITE)
async def replay_suite(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.replay_suite import build_suites

    return await build_suites(hub(context).engine, hub(context).config.session_secret)


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.ANALYZE_FACETS, pass_context=True, lock=jobs.ANALYZE_FACETS, queueing_lock=jobs.ANALYZE_FACETS)
async def analyze_facets(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.facet_job import analyze_facets as analyze

    found = hub(context)
    return await analyze(found.engine, found.blobs, scratch=found.data_dir / "facets")


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.INDEX_HISTORY, pass_context=True, lock=jobs.INDEX_HISTORY, queueing_lock=jobs.INDEX_HISTORY)
async def index_history(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.history_index import index_history as index

    found = hub(context)
    return await index(found.engine, found.blobs, scratch=found.data_dir / "history")


@queue.periodic(cron="43 3 * * *")
@queue.task(name=jobs.PRUNE_JOBS, pass_context=True, queueing_lock=jobs.PRUNE_JOBS)
async def prune_jobs(context: JobContext, timestamp: int | None = None) -> None:
    await context.app.job_manager.delete_old_jobs(
        nb_hours=int(JOB_RETENTION.total_seconds() // 3600),
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )


def connector(config: HubConfig, concurrency: int) -> PsycopgConnector:
    """A connector whose pool procrastinate opens and closes, sized for ``concurrency`` jobs."""
    return PsycopgConnector(
        conninfo=config.dsn,
        min_size=1,
        max_size=max(config.pool_max_size, concurrency + SIDE_CONNECTIONS),
        timeout=config.pool_timeout,
        kwargs={"application_name": APPLICATION_NAME, "connect_timeout": CONNECT_TIMEOUT},
        name="evo-hub-worker",
        close_returns=True,  # a connection the engine closes goes back to the pool
    )


async def work(**options) -> bool:
    """Run jobs until SIGTERM or SIGINT (True) or until the worker stops by itself (False). procrastinate's own
    signal handlers are left out: they cannot tell the two apart."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(queue.run_worker_async(install_signal_handlers=False, **options))
    received: list[str] = []

    def stop(signum: int) -> None:
        received.append(signal.Signals(signum).name)
        for each in STOP_SIGNALS:  # a second signal kills the process
            loop.remove_signal_handler(each)
        log.info("stop requested", extra={"signal": received[0], "grace_s": SHUTDOWN_GRACE})
        task.cancel()  # procrastinate stops taking jobs and waits for the running ones

    for each in STOP_SIGNALS:
        loop.add_signal_handler(each, stop, each)
    try:
        await task
    except asyncio.CancelledError:
        if not received:
            raise
    finally:
        for each in STOP_SIGNALS:
            loop.remove_signal_handler(each)
    return bool(received)


async def run(config: HubConfig, concurrency: int = 1, *, release_pass=None, loop_tick=None, memory_upkeep=None) -> int:
    """Migrate, then run jobs until a signal stops the worker. The exit status: 0 when a signal stopped it, 1 when
    it could not start or stopped by itself. ``release_pass``, ``loop_tick`` and ``memory_upkeep`` are the server's
    (``HubContext.release_pass``, ``HubContext.loop_tick``, ``HubContext.memory_upkeep``)."""
    from evo_agents.hub.server.github_app import GitHubApp
    from evo_agents.hub.server.sealing import Sealer

    github_app = GitHubApp.from_config(config)  # a private key that does not open stops the start
    try:
        curator_app = GitHubApp.from_config(config, curator=True)
    except Exception:
        if github_app is not None:
            await github_app.aclose()
        raise
    store = BlobStore.from_config(config)
    if store is None:
        for opened in (github_app, curator_app):
            if opened is not None:
                await opened.aclose()
        raise ValueError("the worker needs the blob store: " + ", ".join(config.blob_store_missing()))
    name = f"{socket.gethostname()}-{os.getpid()}"
    target = redact_dsn(config.dsn)
    log.info(
        "worker starting",
        extra={
            "version": __version__,
            "db": target,
            "data_dir": str(config.data_dir),
            "concurrency": concurrency,
            "bucket": store.bucket,
            "worker": name,
        },
    )
    try:
        try:
            config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = await asyncio.to_thread(migrate, config.dsn)
        except Exception as exc:
            log.error("worker cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
            return 1
        with queue.replace_connector(connector(config, concurrency)):
            try:
                await queue.open_async()
            except Exception as exc:
                log.error("worker cannot start", extra={"db": target, "error": f"{type(exc).__name__}: {exc}"})
                return 1
            engine = make_engine(queue.connector.pool)
            try:
                context = HubContext(
                    config,
                    queue.connector.pool,
                    store,
                    config.data_dir,
                    Sealer.from_config(config),
                    github_app,
                    engine,
                    curator_app,
                    release_pass,
                    loop_tick,
                    memory_upkeep,
                )
                log.info("worker ready", extra={"schema": ",".join(result.after), "applied": list(result.applied)})
                signalled = await work(
                    name=name,
                    concurrency=concurrency,
                    shutdown_graceful_timeout=SHUTDOWN_GRACE,
                    listen_notify=True,
                    additional_context={"hub": context},
                )
            finally:
                await engine.dispose()
                await queue.close_async()
    finally:
        store.close()
        for opened in (github_app, curator_app):
            if opened is not None:
                await opened.aclose()
    if not signalled:
        log.error("worker stopped without being asked to; see the lines above", extra={"db": target})
        return 1
    log.info("worker stopped, connection pool closed")
    return 0


def main(config: HubConfig, concurrency: int = 1, *, release_pass=None, loop_tick=None, memory_upkeep=None) -> int:
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, not {concurrency}")
    passes = {"release_pass": release_pass, "loop_tick": loop_tick, "memory_upkeep": memory_upkeep}
    return asyncio.run(run(config, concurrency, **passes))

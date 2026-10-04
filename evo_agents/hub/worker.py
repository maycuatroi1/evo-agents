"""``evo-agents hub worker``: the process that runs the hub's background jobs, from the same image as the api.

The worker migrates the database like the api (under the advisory lock), then takes jobs from the procrastinate
queue in Postgres, ``concurrency`` at a time, until SIGTERM or SIGINT. On the first signal it takes no new job and
waits up to SHUTDOWN_GRACE for the running ones; procrastinate aborts what is still running after that, and a second
signal kills the process. Stopped by a signal, it exits 0; stopped by anything else, such as losing Postgres, it
exits 1, so the container is restarted and the failure shows. It needs the blob store: what it builds lives in R2,
and it removes the uploads nobody committed. ``--data-dir`` is its cache, which can be deleted at any time.

Jobs (names in ``evo_agents.hub.jobs``):

- ``hub.ping``: waits a few seconds and returns; deferring it shows that a worker takes jobs.
- ``hub.cleanup_uploads``, hourly: objects under ``uploads/`` and ``blob_uploads`` rows older than STALE_AFTER.
- ``hub.prune_jobs``, daily: finished jobs older than JOB_RETENTION, failed ones included.
- ``hub.kg_build``: one build of a project's knowledge graph (``evo_agents.hub.kg_build``), deferred by the api
  under the project's kg lock.
- ``hub.recover_kg_builds``, every 5 minutes: kg builds whose worker stopped sending heartbeats are failed, which
  frees their project's lock, and their projects get a new build.

procrastinate allows one App per process; ``queue`` is that App here. ``run`` gives it a connector of its own for
the time it runs, and the jobs reach the hub's tables and the blob store through ``HubContext``. The worker handles
SIGTERM and SIGINT itself rather than through procrastinate, which cannot tell a signal from a failure.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from procrastinate import App, JobContext, PsycopgConnector
from psycopg_pool import AsyncConnectionPool

from evo_agents import __version__
from evo_agents.hub import jobs
from evo_agents.hub.blobs import STALE_AFTER, BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import CONNECT_TIMEOUT
from evo_agents.hub.log import redact_dsn
from evo_agents.hub.migrate import migrate

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


def hub(context: JobContext) -> HubContext:
    return context.additional_context["hub"]


@queue.task(name=jobs.PING, pass_context=True)
async def ping(context: JobContext, seconds: float = 0.0, note: str = "") -> dict:
    """Wait ``seconds`` (at most PING_MAX_SECONDS) and return; for checking that a worker runs jobs."""
    await asyncio.sleep(min(max(float(seconds), 0.0), PING_MAX_SECONDS))
    log.info("ping", extra={"job_id": context.job.id, "note": str(note)[:200]})
    return {"worker": context.worker_name, "job_id": context.job.id}


async def remove_stale_uploads(store: BlobStore, pool: AsyncConnectionPool, now: datetime | None = None) -> dict:
    """Delete the objects under ``uploads/`` and the ``blob_uploads`` rows older than STALE_AFTER: uploads nobody
    committed in time. The rows go first, so an upload whose row is gone can no longer be committed while its object
    is being deleted."""
    cutoff = (now or datetime.now(timezone.utc)) - STALE_AFTER
    async with pool.connection() as conn:
        rows = (await conn.execute("DELETE FROM blob_uploads WHERE created_at < %s", (cutoff,))).rowcount
    objects = await asyncio.to_thread(store.remove_stale_uploads, cutoff)
    log.info("stale uploads removed", extra={"objects": objects, "rows": rows, "cutoff": cutoff.isoformat()})
    return {"objects": objects, "rows": rows}


@queue.periodic(cron="17 * * * *")
@queue.task(name=jobs.CLEANUP_UPLOADS, pass_context=True, queueing_lock=jobs.CLEANUP_UPLOADS)
async def cleanup_uploads(context: JobContext, timestamp: int | None = None) -> dict:
    found = hub(context)
    return await remove_stale_uploads(found.blobs, found.pool)


@queue.task(name=jobs.KG_BUILD, pass_context=True)
async def kg_build(context: JobContext, project: str, build_id: int | None = None) -> dict:
    from evo_agents.hub.kg_build import run_build

    return await run_build(hub(context), project, build_id, context.job.id, context.app.job_manager)


@queue.periodic(cron="*/5 * * * *")
@queue.task(name=jobs.RECOVER_KG_BUILDS, pass_context=True, queueing_lock=jobs.RECOVER_KG_BUILDS)
async def recover_kg_builds(context: JobContext, timestamp: int | None = None) -> dict:
    from evo_agents.hub.kg_build import recover_stalled

    return await recover_stalled(hub(context), context.app.job_manager)


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


async def run(config: HubConfig, concurrency: int = 1) -> int:
    """Migrate, then run jobs until a signal stops the worker. The exit status: 0 when a signal stopped it, 1 when
    it could not start or stopped by itself."""
    store = BlobStore.from_config(config)
    if store is None:
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
            try:
                context = HubContext(config, queue.connector.pool, store, config.data_dir)
                log.info("worker ready", extra={"schema": ",".join(result.after), "applied": list(result.applied)})
                signalled = await work(
                    name=name,
                    concurrency=concurrency,
                    shutdown_graceful_timeout=SHUTDOWN_GRACE,
                    listen_notify=True,
                    additional_context={"hub": context},
                )
            finally:
                await queue.close_async()
    finally:
        store.close()
    if not signalled:
        log.error("worker stopped without being asked to; see the lines above", extra={"db": target})
        return 1
    log.info("worker stopped, connection pool closed")
    return 0


def main(config: HubConfig, concurrency: int = 1) -> int:
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, not {concurrency}")
    return asyncio.run(run(config, concurrency))

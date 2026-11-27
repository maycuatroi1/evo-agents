"""The worker's jobs that keep the hub's storage small: stale uploads, old audit rows, run events, digests,
transcripts, signals and finished jobs; and ``hub.ping``."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from procrastinate import JobContext
from sqlalchemy import delete, func

from evo_agents.hub import jobs, tables
from evo_agents.hub.blobs import STALE_AFTER, BlobStore
from evo_agents.hub.worker.job import Job, hub, hub_engine

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

log = logging.getLogger("evo_agents.hub.worker")

PING_MAX_SECONDS = 30.0
JOB_RETENTION = timedelta(days=14)


class Ping(Job):
    name = jobs.PING

    async def run(self, context: JobContext, seconds: float = 0.0, note: str = "") -> dict:
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


class CleanupUploads(Job):
    name = jobs.CLEANUP_UPLOADS
    cron = "17 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub import kg_upload_prune
        from evo_agents.hub.attachment_prune import remove_stale

        found = hub(context)
        removed = await remove_stale_uploads(found.blobs, hub_engine(context))
        archives = await kg_upload_prune.remove_stale(found.blobs, hub_engine(context), datetime.now(UTC))
        return {**removed, **archives, **await remove_stale(hub_engine(context), found.blobs)}


class PruneSignals(Job):
    name = jobs.PRUNE_SIGNALS
    cron = "3 5 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.signals import prune_signals as prune

        found = hub(context)
        return await prune(hub_engine(context), found.config.signal_days)


class PruneRunEvents(Job):
    name = jobs.PRUNE_RUN_EVENTS
    cron = "13 4 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.run_reaper import prune_run_events as prune

        found = hub(context)
        return await prune(hub_engine(context), found.config.run_log_days, found.blobs)


class PruneAudit(Job):
    name = jobs.PRUNE_AUDIT
    cron = "33 4 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        found = hub(context)
        return await prune_audit(hub_engine(context), found.config.audit_days)


class PruneDigests(Job):
    name = jobs.PRUNE_DIGESTS
    cron = "23 4 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.digests.service.sessions import prune_digests as prune

        return await prune(hub_engine(context))


class PruneTranscripts(Job):
    name = jobs.PRUNE_TRANSCRIPTS
    cron = "53 4 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.transcript_prune import prune_transcripts as prune

        found = hub(context)
        return await prune(hub_engine(context), found.blobs, found.config.transcript_days)


class PruneJobs(Job):
    name = jobs.PRUNE_JOBS
    cron = "43 3 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> None:
        await context.app.job_manager.delete_old_jobs(
            nb_hours=int(JOB_RETENTION.total_seconds() // 3600),
            include_failed=True,
            include_cancelled=True,
            include_aborted=True,
        )

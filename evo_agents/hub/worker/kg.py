"""The worker's jobs of the knowledge graph: a project's build, the recovery of builds whose worker died, the prune
of old artifacts and the extraction of uploaded archives."""

from __future__ import annotations

import logging

from procrastinate import JobContext

from evo_agents.hub import jobs
from evo_agents.hub.worker.job import Job, hub, hub_engine

log = logging.getLogger("evo_agents.hub.worker")


class KgBuild(Job):
    name = jobs.KG_BUILD
    queue = jobs.KG_QUEUE

    async def run(self, context: JobContext, project: str, build_id: int | None = None) -> dict:
        from evo_agents.hub.kg_build import run_build

        return await run_build(hub(context), project, build_id, context.job.id, context.app.job_manager)


class RecoverKgBuilds(Job):
    name = jobs.RECOVER_KG_BUILDS
    cron = "*/5 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.kg_build import recover_stalled

        recovered = await recover_stalled(hub(context), context.app.job_manager)
        stalled = await jobs.fail_stalled(context.app.job_manager, but=frozenset({jobs.KG_BUILD}))
        if stalled:
            log.warning("jobs of stopped workers failed", extra={"jobs": stalled})
        return {**recovered, "stalled": stalled}


class PruneKgArtifacts(Job):
    name = jobs.PRUNE_KG_ARTIFACTS
    cron = "31 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.kg_prune import prune

        found = hub(context)
        report = await prune(hub_engine(context), found.blobs, found.config.kg_keep_artifacts)
        return report.summary()


class ExtractUploads(Job):
    name = jobs.EXTRACT_UPLOADS
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.kg_upload_extract import extract_pending

        return await extract_pending(hub(context), context.app.job_manager)

"""The worker's jobs of the Curator and of what reads the hub's sessions: shifts, collection, briefs, changes and the
merge queue, outcomes, digests, memory upkeep, reused answers, replay suites, facets, the history index and the
nightly read."""

from __future__ import annotations

import logging

from procrastinate import JobContext

from evo_agents.hub import jobs
from evo_agents.hub.worker.job import Job, hub, hub_engine

log = logging.getLogger("evo_agents.hub.worker")


class FireSchedules(Job):
    name = jobs.FIRE_SCHEDULES
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.curator.service.shift import fire_schedules as fire

        found = hub(context)
        return await fire(hub_engine(context), curator_app=getattr(found, "curator_app", None) is not None)


class CuratorCollect(Job):
    name = jobs.CURATOR_COLLECT
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.collect import collect

        found = hub(context)
        app = found.github_app or found.curator_app
        return await collect(hub_engine(context), github_app=app, blobs=found.blobs, data_dir=found.data_dir)


class CuratorBrief(Job):
    name = jobs.CURATOR_BRIEF
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.brief import send_briefs

        return await send_briefs(hub_engine(context))


class CuratorChanges(Job):
    name = jobs.CURATOR_CHANGES
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.changes.service.job import advance

        found = hub(context)
        curator_app = getattr(found, "curator_app", None)
        return await advance(
            hub_engine(context), found.github_app, curator_app, blobs=found.blobs, data_dir=found.data_dir
        )


class MergeQueue(Job):
    name = jobs.MERGE_QUEUE
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.changes.service.job import (
            merge_gate,  # the queue's job, through the module of the Curator's changes
        )
        from evo_agents.hub.server.merge.service import (  # a run's pull request on github.com, judged at its head
            ancestry as merge_ancestry,
        )
        from evo_agents.hub.server.merge.service import check as merge_check

        found = hub(context)
        outcomes = await merge_gate.advance(
            hub_engine(context), found.sealer, blobs=found.blobs, data_dir=found.data_dir
        )
        checks = await merge_check.advance(hub_engine(context), blobs=found.blobs, data_dir=found.data_dir)
        merged = await merge_ancestry.settle(
            hub_engine(context), getattr(found, "github_app", None), found.config.public_url
        )
        checks = {**checks, **{f"checks_{key}": value for key, value in merged.items()}}
        return {**outcomes, **checks} if checks else outcomes


class CuratorOutcomes(Job):
    name = jobs.CURATOR_OUTCOMES
    cron = "*/10 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.outcomes import measure_outcomes

        return await measure_outcomes(hub_engine(context))


class ReadNightly(Job):
    name = jobs.READ_NIGHTLY
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.nightly import refresh

        return await refresh(hub_engine(context))


class OpportunityDeadlines(Job):
    name = jobs.OPPORTUNITY_DEADLINES
    cron = "7 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.opportunity_deadlines import remind

        return await remind(hub_engine(context))


class TodayDigest(Job):
    name = jobs.TODAY_DIGEST
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.brief import send_today_digests

        found = hub(context)
        return await send_today_digests(hub_engine(context), config=found.config)


class MemoryUpkeep(Job):
    name = jobs.MEMORY_UPKEEP
    cron = "27 5 * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        found = hub(context)
        kept = getattr(found, "memory_upkeep", None)
        if kept is None:
            log.warning("no memory upkeep was handed to this worker: no agent's memory is archived or marked stale")
            return {"no_memory_upkeep": 1}
        return await kept(hub_engine(context), found.blobs, found.data_dir)


class ContextAnswerReuse(Job):
    name = jobs.CONTEXT_ANSWER_REUSE
    cron = "9 0 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.context_answer_reuse import push

        found = hub(context)
        return await push(hub_engine(context), found.config)


class ReplaySuite(Job):
    name = jobs.REPLAY_SUITE
    cron = "41 * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.replay_suite import build_suites

        return await build_suites(hub_engine(context), hub(context).config.session_secret)


class AnalyzeFacets(Job):
    name = jobs.ANALYZE_FACETS
    cron = "*/5 * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.facet_job import analyze_facets as analyze

        found = hub(context)
        return await analyze(hub_engine(context), found.blobs, scratch=found.data_dir / "facets")


class IndexHistory(Job):
    name = jobs.INDEX_HISTORY
    cron = "*/5 * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.history_index import index_history as index

        found = hub(context)
        return await index(hub_engine(context), found.blobs, scratch=found.data_dir / "history")

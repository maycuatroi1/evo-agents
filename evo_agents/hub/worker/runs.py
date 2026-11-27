"""The worker's jobs that keep runs moving: leases that ran out, seats and pool nodes, the turns of the hub runtime,
notifications, release cycles, cron jobs, the Loop and the spend of runs."""

from __future__ import annotations

import logging
from datetime import timedelta

from procrastinate import JobContext

from evo_agents.hub import jobs
from evo_agents.hub.worker.job import Job, hub, hub_engine

log = logging.getLogger("evo_agents.hub.worker")


class ExpireSeats(Job):
    name = jobs.EXPIRE_SEATS
    cron = "5 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.run_reaper import expire_seats as expire

        found = hub(context)
        return await expire(hub_engine(context), found.sealer, found.github_app or getattr(found, "curator_app", None))


class ReapPoolNodes(Job):
    name = jobs.REAP_POOL_NODES
    cron = "47 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.run_reaper import reap_pool_nodes as reap

        found = hub(context)
        return await reap(hub_engine(context), found.sealer, found.github_app or getattr(found, "curator_app", None))


class HubTurn(Job):
    name = jobs.HUB_TURN
    queue = jobs.HUB_QUEUE

    async def run(self, context: JobContext, run_id: int) -> dict:
        found = hub(context)
        if found.hub_turn is None:
            raise RuntimeError("the hub worker command handed down no turn of the hub runtime (HubContext.hub_turn)")
        return await found.hub_turn(hub_engine(context), found.config, found.blobs, found.sealer, run_id)


class RecoverRuns(Job):
    name = jobs.RECOVER_RUNS
    queue = jobs.LEASE
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.run_reaper import recover_runs as recover

        found = hub(context)
        return await recover(
            hub_engine(context),
            decision_wait=timedelta(seconds=found.config.decision_wait_seconds),
            sealer=found.sealer,
            # a token is revoked with itself: either App's client does
            github_app=found.github_app or getattr(found, "curator_app", None),
        )


class DeliverNotifications(Job):
    name = jobs.DELIVER_NOTIFICATIONS
    queue = jobs.LEASE
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.notifications.service.deliveries import deliver_notifications as deliver

        found = hub(context)
        return await deliver(hub_engine(context), config=found.config, blobs=found.blobs)


class ReleaseSchedules(Job):
    name = jobs.RELEASE_SCHEDULES
    queue = jobs.LEASE  # a long job on the default queue held the watch of the tag's CI back for half an hour
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        found = hub(context)
        release_pass = getattr(found, "release_pass", None)
        if release_pass is None:
            log.warning("no release pass was handed to this worker: no release schedule is checked")
            return {"no_release_pass": 1}
        return await release_pass(hub_engine(context), found.github_app)


class CronJobs(Job):
    name = jobs.CRON_JOBS
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        found = hub(context)
        cron_pass = getattr(found, "cron_pass", None)
        if cron_pass is None:
            log.warning("no cron pass was handed to this worker: no cron job fires")
            return {"no_cron_pass": 1}
        return await cron_pass(hub_engine(context))


class LoopTicks(Job):
    name = jobs.LOOP_TICKS
    cron = "*/5 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.loop_jobs import queue_ticks

        return await queue_ticks(hub_engine(context), context.app.job_manager)


class LoopNotices(Job):
    name = jobs.LOOP_NOTICES
    cron = "*/5 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.brief import send_loop_notices

        return await send_loop_notices(hub_engine(context))


class LoopTick(Job):
    name = jobs.LOOP_TICK

    async def run(self, context: JobContext, project_id: int) -> dict:
        found = hub(context)
        tick = getattr(found, "loop_tick", None)
        if tick is None:
            log.warning("no loop tick was handed to this worker: no Loop ticks", extra={"project_id": project_id})
            return {"no_loop_tick": 1}
        return await tick(
            hub_engine(context),
            project_id,
            github_app=getattr(found, "github_app", None),
            sealer=getattr(found, "sealer", None),
        )


class RecordSpend(Job):
    name = jobs.RECORD_SPEND
    cron = "* * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int) -> dict:
        from evo_agents.hub.spend_store import record_pending

        return await record_pending(hub_engine(context))


class LocalSpend(Job):
    name = jobs.LOCAL_SPEND
    cron = "*/5 * * * *"
    locked = True

    async def run(self, context: JobContext, timestamp: int) -> dict:
        from evo_agents.hub.local_spend import local_spend as read

        found = hub(context)
        return await read(hub_engine(context), found.blobs, scratch=found.data_dir / "facets")

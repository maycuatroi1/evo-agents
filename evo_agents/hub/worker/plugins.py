"""The worker's jobs of plugins and the market: plugin jobs and value checks, deliveries to plugins and webhooks, the
market's sources, licenses and signals."""

from __future__ import annotations

from procrastinate import JobContext

from evo_agents.hub import jobs
from evo_agents.hub.worker.job import Job, hub


class MarketRefresh(Job):
    name = jobs.MARKET_REFRESH
    cron = "41 */6 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.market_refresh import refresh

        found = hub(context)
        done = await refresh(found.engine, found.config, found.sealer)
        return {"sources": len(done), "failed": sum(1 for item in done if item["error"])}


class MarketLicenses(Job):
    name = jobs.MARKET_LICENSES
    cron = "23 * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents import __version__
        from evo_agents.hub.server.market.service.install import Installing
        from evo_agents.hub.server.market.service.licenses import check_licenses
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


class MarketSignals(Job):
    name = jobs.MARKET_SIGNALS
    cron = "17 3 * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.seller.service.signals import push_signals

        found = hub(context)
        return await push_signals(found.engine, found.config)


class PluginJobs(Job):
    name = jobs.PLUGIN_JOBS
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.plugin_jobs import fire

        return await fire(hub(context).engine, context.app.job_manager, timestamp)


class PluginJob(Job):
    name = jobs.PLUGIN_JOB

    async def run(self, context: JobContext, plugin: str, job: str) -> dict:
        from evo_agents.hub.plugin_jobs import run_job

        found = hub(context)
        return await run_job(found.engine, found.config, plugin, job)


class ValueCheck(Job):
    name = jobs.VALUE_CHECK

    async def run(self, context: JobContext, project_id: int) -> dict:
        from evo_agents.hub.plugin_jobs import run_on_signals

        found = hub(context)
        return await run_on_signals(found.engine, found.config, project_id)


class DeliverPluginEvents(Job):
    name = jobs.DELIVER_PLUGIN_EVENTS
    queue = jobs.DELIVERY_QUEUE
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub import bus
        from evo_agents.hub.plugin_events import BUS_GRACE_SECONDS, deliver

        found = hub(context)
        assert found.engine is not None  # the hub's worker opens its engine before its first job
        if not bus.on():
            return await deliver(found.engine, config=found.config)
        # The bus carries what falls due; the minute's pass takes what it left a while, and a pass the delivery process
        # queued while the broker could not be reached takes everything due, as without the bus.
        grace = BUS_GRACE_SECONDS if timestamp is not None else 0.0
        return await deliver(found.engine, config=found.config, schedule=False, grace=grace)


class DeliverWebhooks(Job):
    name = jobs.DELIVER_WEBHOOKS
    queue = jobs.DELIVERY_QUEUE
    cron = "* * * * *"

    async def run(self, context: JobContext, timestamp: int | None = None) -> dict:
        from evo_agents.hub.server.webhooks.service.delivery import deliver

        found = hub(context)
        assert found.engine is not None  # the hub's worker opens its engine before its first job
        return await deliver(found.engine, found.sealer)

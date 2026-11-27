"""Every job of the hub's worker, listed by hand: ``register`` puts them on the procrastinate App. A job missing here
never runs, so a new one is added to JOBS and to the names of ``evo_agents.hub.jobs``."""

from __future__ import annotations

from procrastinate import App

from evo_agents.hub.worker import curator, kg, plugins, runs, upkeep
from evo_agents.hub.worker.job import Job

JOBS: tuple[Job, ...] = (
    upkeep.Ping(),
    upkeep.CleanupUploads(),
    runs.ExpireSeats(),
    plugins.MarketRefresh(),
    plugins.MarketLicenses(),
    plugins.MarketSignals(),
    runs.ReapPoolNodes(),
    runs.HubTurn(),
    kg.KgBuild(),
    kg.RecoverKgBuilds(),
    kg.PruneKgArtifacts(),
    runs.RecoverRuns(),
    runs.DeliverNotifications(),
    curator.FireSchedules(),
    curator.CuratorCollect(),
    curator.CuratorBrief(),
    curator.CuratorChanges(),
    curator.MergeQueue(),
    kg.ExtractUploads(),
    curator.CuratorOutcomes(),
    plugins.PluginJobs(),
    plugins.PluginJob(),
    plugins.ValueCheck(),
    plugins.DeliverPluginEvents(),
    plugins.DeliverWebhooks(),
    curator.ReadNightly(),
    runs.ReleaseSchedules(),
    runs.CronJobs(),
    runs.LoopTicks(),
    runs.LoopNotices(),
    curator.OpportunityDeadlines(),
    curator.TodayDigest(),
    runs.LoopTick(),
    upkeep.PruneRunEvents(),
    upkeep.PruneAudit(),
    upkeep.PruneDigests(),
    upkeep.PruneTranscripts(),
    curator.MemoryUpkeep(),
    curator.ContextAnswerReuse(),
    upkeep.PruneSignals(),
    curator.ReplaySuite(),
    curator.AnalyzeFacets(),
    curator.IndexHistory(),
    runs.RecordSpend(),
    runs.LocalSpend(),
    upkeep.PruneJobs(),
)


def register(app: App, listed: tuple[Job, ...] = JOBS) -> None:
    """Add each job of ``listed`` to ``app``."""
    names = [job.name for job in listed]
    assert len(names) == len(set(names)), "two jobs of the hub's worker share a name"
    for job in listed:
        job.register(app)

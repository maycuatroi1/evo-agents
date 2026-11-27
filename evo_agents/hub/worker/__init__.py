"""``evo-agents hub worker``: the process that runs the hub's background jobs, from the same image as the api.

The worker migrates the database like the api (under the advisory lock), then takes jobs from the procrastinate
queue in Postgres, ``concurrency`` at a time, until SIGTERM or SIGINT. A second procrastinate worker in the same
process takes only the queue ``jobs.LEASE`` (recovering runs, delivering notifications, the release pass), one job
at a time, so those run every minute even while long jobs fill the ``concurrency`` slots; a third takes only
``jobs.HUB_QUEUE``, the turns of the hub runtime, HUB_CONCURRENCY at a time; a fourth only ``jobs.DELIVERY_QUEUE``,
the deliveries to plugins, DELIVERY_CONCURRENCY at a time, so what a commit queues for a plugin goes out within
seconds. On the first signal it takes no new job and waits up to SHUTDOWN_GRACE for the running ones (KG_SHUTDOWN_GRACE
on a worker of the queue kg alone, as short: a deploy starts the new api only once the old worker-kg stopped); procrastinate aborts what is still running
after that, and a second signal kills the process. Stopped by a signal, it exits 0;
stopped by anything else, such as losing Postgres, it exits 1, so the container is restarted and the failure shows. It
needs the blob store: what it builds lives in R2, and it removes the uploads nobody committed. ``--data-dir`` is its
cache, which can be deleted at any time.

Jobs (names in ``evo_agents.hub.jobs``), each a class of ``evo_agents.hub.worker.job.Job`` in the modules ``upkeep``,
``kg``, ``runs``, ``curator`` and ``plugins`` of this package, listed by hand in ``registry.JOBS``:

- ``hub.ping``: waits a few seconds and returns; deferring it shows that a worker takes jobs.
- ``hub.cleanup_uploads``, hourly: objects under ``uploads/`` and ``blob_uploads`` rows older than STALE_AFTER, the
  archive uploads (``hub kg upload``) untouched for as long, failed and their parts dropped
  (``evo_agents.hub.kg_upload_prune``), and the attachments no message took within
  ``attachments.STALE_AFTER`` (``attachment_prune.remove_stale``).
- ``hub.expire_seats``, hourly: the org seats past their end date or that of a cohort holding them are ended, audited
  with the hub as actor, and the leases still out of the runs of every person whose primary seat ended are taken back
  and their GitHub tokens revoked (``evo_agents.hub.server.org_expiry``).
- ``hub.reap_pool_nodes``, hourly: each worker pool node offline longer than its pool's ``reap_after_days`` is
  revoked as its owner's revoke would, with the hub as actor (``evo_agents.hub.server.workers.service.reaper``); workers
  outside a pool are left alone.
- ``hub.hub_turn``: one turn of a chat run of an agent that runs on the hub (runtime ``hub``), deferred by the api
  when the run queues: the model answers through the hub's gateway with the tools of the hub runtime, bounded in
  rounds and time (``evo_agents.hub.server.hub_runtime``).
- ``hub.prune_jobs``, daily: finished jobs older than JOB_RETENTION, failed ones included.
- ``hub.kg_build``: one build of a project's knowledge graph (``evo_agents.hub.kg_build``), deferred by the api
  under the project's kg lock.
- ``hub.recover_kg_builds``, every 5 minutes: kg builds whose worker stopped sending heartbeats are failed, which
  frees their project's lock, and their projects get a new build; so are builds whose job ended without recording
  them, whose project gets a new build when no later one took their place. A project whose last DIED_IN_A_ROW builds
  ended so gets none, and its admins are told (``kg_build_queue._requeue``). The other jobs of a worker that stopped
  sending heartbeats are failed too (``jobs.fail_stalled``), which frees their locks.
- ``hub.prune_kg_artifacts``, hourly: the artifacts of each project's graphs beyond its EVO_HUB_KG_KEEP_ARTIFACTS
  newest leave the bucket (``evo_agents.hub.kg_prune``).
- ``hub.recover_runs``, every minute: runs whose worker stopped extending the lease become lost, and the next attempt
  of their step is queued, or fail on their last attempt; runs past their timeout fail; a plan run that waited
  EVO_HUB_DECISION_WAIT_SECONDS (10 minutes by default) for an answer, or waits only for a time or an event of the hub,
  is parked, and one parked for 7 days cancelled
  (``evo_agents.hub.server.run_state``); a queued run dispatched with browser that no worker can take has its owner's
  nearest worker that lacks only Chromium asked to install it (``evo_agents.hub.server.browser.service.install``). The runs that end give back their leases, and the GitHub tokens of every lease
  given back and not revoked at GitHub yet are revoked there (``evo_agents.hub.server.credentials``).
- ``hub.deliver_notifications``, every minute, and queued by each transaction that stores a notification: the
  notification deliveries that are due, each handed to its channel's class, tried again with a backoff and failed
  after 5 tries (``evo_agents.hub.server.notifications.service.deliveries``).
- ``hub.prune_run_events``, daily: the events of runs that ended more than EVO_HUB_RUN_LOG_DAYS ago, the outputs of
  source tool of those runs that no question or chat message carries (``evo_agents.hub.output_prune``), and the sealed
  values of the GitHub tokens leased to runs that are past their end.
- ``hub.prune_audit``, daily: the audit rows older than EVO_HUB_AUDIT_DAYS (365 by default).
- ``hub.fire_schedules``, every minute: each schedule of a project's charter that is not paused queues its next run
  inside its window and within its night's budget, the night's review run first, and cancels its queued runs outside
  it; its circuit breaker pauses a project's night shift once the charter's max_failed_in_a_row jobs of a night in a
  row failed of the agent's cause or were reverted (``evo_agents.hub.server.curator``,
  ``evo_agents.hub.server.outcomes``).
- ``curator.collect``, every minute: the health of each charter's knowledge graph, measured every 6 hours against the
  default branches on GitHub (``evo_agents.hub.server.kg_health``); in each charter's window, the night's figures of
  the project, counted once without any model, and its review run; and the deferred proposals whose time has passed
  open again (``evo_agents.hub.server.collect``).
- ``hub.plugin_jobs``, every minute: a ``hub.plugin_job`` for each job of a plugin enabled in a project whose cron
  takes the minute, queued once at a time, the minutes a late pass missed in the last hour caught up; ``hub.plugin_job``
  runs it (``evo_agents.hub.plugin_jobs``).
- ``hub.value_check``: deferred with each batch of a project's signals that stored any (``evo_agents.hub.signals``),
  one at a time per project and at most one waiting: the plugins' jobs named value_check of that project, for it alone
  (``evo_agents.hub.plugin_jobs.run_on_signals``).
- ``hub.deliver_plugin_events``, every minute, on ``jobs.DELIVERY_QUEUE``: the events and notifications due for
  plugins, at least once, tried again with a backoff, or when a core plugin asked (``retry_after``); also queued by
  each transaction that stores what a bridge carries or a notification for a plugin, and by each pass for the soonest
  delivery it left pending, at its time (``evo_agents.hub.plugin_events``). With the event bus on
  (``evo_agents.hub.bus``) the delivery process carries them instead, and the minute's pass takes only what has been due
  for BUS_GRACE_SECONDS, unless the delivery process queued it because the broker could not be reached.
- ``hub.read_nightly``, every minute: the commit the tag nightly of evo-agents points to, once for the whole hub
  whatever its number of workers, kept in hub_nightly with the time of the read for the api to hand every daemon in
  its heartbeat answer; a read that fails keeps the last one and logs a line (``evo_agents.hub.nightly``).
- ``hub.release_schedules``, every minute, one pass at a time: each project's release schedule whose local time came
  within the hour gets the check of that day, once: without any model, the newest release tag and the commit of the
  target ref are compared on the packaging paths, and a change opens a cycle and queues its cut run, with the workers'
  App reading GitHub (``evo_agents.hub.server.releases.service.check``, ``evo_agents.hub.releases``); then each release run
  that is done with its branch is landed: its pull request opened and merged, and its merge commit tagged, with the
  same App (``evo_agents.hub.server.releases.service.land``). The pass is the server's: the ``hub worker`` command hands it
  down (``HubContext.release_pass``), since this module imports no server module beyond its job list
  (docs/structure.md, section Package boundaries).
- ``hub.cron_jobs``, every minute, one pass at a time: each cron job of a project not archived that is due fires once
  per scheduled minute as its owner: a message to an agent in a new thread, a plan run or a command run; a fire whose
  earlier run is still active is busy, minutes missed are recorded, and a job whose owner lost their role or seat, or
  whose runs failed three times in a row, pauses with a notice (``evo_agents.hub.server.cron.service.fire``). The pass is the
  server's, handed down by the ``hub worker`` command (``HubContext.cron_pass``).
- ``hub.loop_ticks``, every 5 minutes: a ``hub.loop_tick`` of each project whose Loop is on, queued under the
  project's Loop lock (``evo_agents.hub.loop_jobs``); ``hub.loop_tick`` runs one tick of a project's Loop, one at a
  time per project and at most one waiting, also queued by each event of the project. The tick is the server's
  (``evo_agents.hub.server.loop.tick``): the ``hub worker`` command hands it down (``HubContext.loop_tick``), as it
  does the release pass, and the job calls it with the workers' GitHub App, which reads the pull requests the plan
  runs of the Loop merged on GitHub, and with the hub's key of secrets, which opens the forge secret that reads their
  merge requests on GitLab.
- ``hub.loop_notices``, every 5 minutes: for each project whose Loop is on, the notice goal_review on the first day of
  a month, and the Loop's part of the morning brief (loop_brief) at 07:00 for a project whose Curator sends none, in
  the Loop's zone, each once (``evo_agents.hub.server.loop.notices``, through ``evo_agents.hub.server.brief``).
- ``hub.opportunity_deadlines``, hourly: the owner of each open opportunity whose window ends gets one notice at 14
  days left, one at 3 and one the day after, each once per value of the date; the hub changes no status
  (``evo_agents.hub.server.opportunity_deadlines``).
- ``hub.today_digest``, every minute: the summary push of Today to each member with a push device who has been away
  from the hub for 8 hours, at the local time they chose, when something changed since they last looked, once a day
  (``evo_agents.hub.server.digests.service.today``, through ``evo_agents.hub.server.brief``).
- ``hub.prune_digests``, daily: the session digests not pushed again for 90 days
  (``evo_agents.hub.server.digests.service.sessions``).
- ``hub.prune_transcripts``, daily: the content of the session transcripts that got no part for
  EVO_HUB_TRANSCRIPT_DAYS days, and the blobs of kind transcript no part names (``evo_agents.hub.transcript_prune``).
- ``hub.context_answer_reuse``, daily at 00:09: the answers loaded into prompts the day before, posted as the signal
  ``context.answer_reuse`` of each project whose policy sets ``context.answers`` (``server.context_answer_reuse``).
- ``hub.memory_upkeep``, daily: the memories of agent profiles nobody opened for 30 days archived, and those citing
  code the project's knowledge graph no longer has marked stale (``evo_agents.hub.server.memories.service.upkeep``), handed
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
  probes, the weekly report of their ISO week just ended too, once a week (``evo_agents.hub.server.probes.service.report``).
- ``curator.changes``, every minute, one pass at a time: the Curator's changes move on: their pull requests opened,
  the Judge's check runs written, the tier 0 ones merged when everything allows it, the others left open for their
  owner and read again hourly for a merge or a close by hand; and the rulesets checked, the one a planned change waits
  for at the next pass and hourly while it does not pass, every other once a day (``evo_agents.hub.server.changes``,
  ``.protection``).
- ``hub.merge_queue``, every minute, one pass at a time: the merge queue on GitLab of each project whose autonomy
  policy has a repo in mode auto: the merge requests of its runs opened, those in scope taken, and each one gated
  until its Judges' verdicts let it wait for its turn, with the reason it waits or is blocked
  (``evo_agents.hub.server.merge.service.gate``, ``evo_agents.hub.server.merge.service.judge``).
- ``hub.extract_uploads``, every minute, one pass at a time: each archive uploaded with ``hub kg upload`` read into the
  items of its source, the uploads of the source it replaces released, and a build queued
  (``evo_agents.hub.kg_upload_extract``).
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

from procrastinate import App, PsycopgConnector

from evo_agents import __version__
from evo_agents.hub import jobs
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import CONNECT_TIMEOUT, make_engine
from evo_agents.hub.log import redact_dsn
from evo_agents.hub.migrate import migrate
from evo_agents.hub.worker.job import HubContext as HubContext
from evo_agents.hub.worker.job import Job as Job
from evo_agents.hub.worker.job import hub as hub
from evo_agents.hub.worker.job import hub_engine as hub_engine
from evo_agents.hub.worker.registry import register
from evo_agents.hub.worker.upkeep import JOB_RETENTION as JOB_RETENTION
from evo_agents.hub.worker.upkeep import PING_MAX_SECONDS as PING_MAX_SECONDS
from evo_agents.hub.worker.upkeep import prune_audit as prune_audit
from evo_agents.hub.worker.upkeep import remove_stale_uploads as remove_stale_uploads

log = logging.getLogger(__name__)

APPLICATION_NAME = "evo-agents-hub-worker"
SHUTDOWN_GRACE = 25.0  # seconds running jobs get after SIGTERM; the container needs a longer stop grace period
# The grace of a worker of the queue kg alone (worker-kg). It was 540 s, above the median build, so a deploy would let
# the running build finish (#742); but Docker Compose starts none of the containers a deploy recreates until every old
# one has stopped, so the api stayed down while worker-kg finished its build: 9 minutes on 2026-11-19 18:49, at each
# merge to main. A build still running after the grace is aborted, recorded STOPPED and queued again, which costs
# worker-kg time and no one's access to the hub. worker-kg's stop_grace_period in deploy/hub/docker-compose.yml stays
# above it, or Docker kills the build first.
KG_SHUTDOWN_GRACE = 25.0
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
SIDE_CONNECTIONS = 3  # the pool serves the jobs plus procrastinate's fetching, heartbeat and periodic deferrer
LEASE_CONCURRENCY = 1  # jobs the worker on jobs.LEASE runs at a time, beside the ``concurrency`` of the others
HUB_CONCURRENCY = 4  # turns of the hub runtime the worker on jobs.HUB_QUEUE takes at a time: they wait on the gateway
DELIVERY_CONCURRENCY = 2  # passes of hub.deliver_plugin_events at a time: one a commit queued beside one scheduled
SIDE_WORKERS_CONCURRENCY = LEASE_CONCURRENCY + HUB_CONCURRENCY + DELIVERY_CONCURRENCY

queue = App(connector=PsycopgConnector())  # never opened as is: ``run`` swaps in a connector with the DSN
register(queue)


def connector(config: HubConfig, concurrency: int) -> PsycopgConnector:
    """A connector whose pool procrastinate opens and closes, sized for ``concurrency`` jobs, the lease worker's, the
    hub worker's and the delivery worker's."""
    return PsycopgConnector(
        conninfo=config.dsn,
        min_size=1,
        max_size=max(config.pool_max_size, concurrency + SIDE_WORKERS_CONCURRENCY + 4 * SIDE_CONNECTIONS),
        timeout=config.pool_timeout,
        kwargs={"application_name": APPLICATION_NAME, "connect_timeout": CONNECT_TIMEOUT},
        name="evo-hub-worker",
        close_returns=True,  # a connection the engine closes goes back to the pool
    )


def shutdown_grace(queues: list[str] | None) -> float:
    """Seconds the running jobs get after SIGTERM: KG_SHUTDOWN_GRACE when ``queues`` are the kg queue alone,
    SHUTDOWN_GRACE otherwise (every queue, or one with short jobs a deploy should not wait for)."""
    return KG_SHUTDOWN_GRACE if queues and set(queues) == {jobs.KG_QUEUE} else SHUTDOWN_GRACE


async def workers(**options) -> None:
    """Four procrastinate workers on the one App: one with ``options``, one on jobs.LEASE alone, so recovering runs
    and delivering notifications never wait behind a long job, one on jobs.HUB_QUEUE alone, HUB_CONCURRENCY turns
    of the hub runtime at a time, and one on jobs.DELIVERY_QUEUE alone, so a delivery to a plugin never waits behind a
    long job either. A worker whose ``queues`` leave out the default queue (``--queue kg``) runs the
    first alone. All stop when one does, the error of the one that stopped first raised; cancelled, they stop and
    wait for their running jobs."""
    queues = options.get("queues")
    if queues and jobs.DEFAULT_QUEUE not in queues:
        await queue.run_worker_async(install_signal_handlers=False, **options)
        return
    lease = {
        **options,
        "name": f"{options.get('name') or 'worker'}-{jobs.LEASE}",
        "queues": [jobs.LEASE],
        "concurrency": LEASE_CONCURRENCY,
    }
    turns = {
        **options,
        "name": f"{options.get('name') or 'worker'}-{jobs.HUB_QUEUE}",
        "queues": [jobs.HUB_QUEUE],
        "concurrency": HUB_CONCURRENCY,
    }
    deliveries = {
        **options,
        "name": f"{options.get('name') or 'worker'}-{jobs.DELIVERY_QUEUE}",
        "queues": [jobs.DELIVERY_QUEUE],
        "concurrency": DELIVERY_CONCURRENCY,
    }
    running = [
        asyncio.ensure_future(queue.run_worker_async(install_signal_handlers=False, **each))
        for each in (options, lease, turns, deliveries)
    ]
    try:
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for each in running:
            each.cancel()
        await asyncio.gather(*running, return_exceptions=True)
    for each in done:
        each.result()


async def work(**options) -> bool:
    """Run jobs until SIGTERM or SIGINT (True) or until the workers stop by themselves (False). procrastinate's own
    signal handlers are left out: they cannot tell the two apart."""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(workers(**options))
    received: list[str] = []

    def stop(signum: int) -> None:
        received.append(signal.Signals(signum).name)
        for each in STOP_SIGNALS:  # a second signal kills the process
            loop.remove_signal_handler(each)
        grace = options.get("shutdown_graceful_timeout", SHUTDOWN_GRACE)
        log.info("stop requested", extra={"signal": received[0], "grace_s": grace})
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


async def run(
    config: HubConfig,
    concurrency: int = 1,
    *,
    queues: list[str] | None = None,
    release_pass=None,
    loop_tick=None,
    memory_upkeep=None,
    entity_key=None,
    hub_turn=None,
    cron_pass=None,
) -> int:
    """Migrate, then run jobs of ``queues`` (None: every queue) until a signal stops the worker. The exit status: 0
    when a signal stopped it, 1 when it could not start or stopped by itself. ``release_pass``, ``loop_tick``, ``memory_upkeep``,
    ``entity_key`` and ``hub_turn`` are the server's (``HubContext.release_pass``, ``HubContext.loop_tick``,
    ``HubContext.memory_upkeep``, ``HubContext.entity_key``, ``HubContext.hub_turn``), and so is ``cron_pass``
    (``HubContext.cron_pass``)."""
    from evo_agents.hub.server.credentials.models.sealing import Sealer
    from evo_agents.hub.server.forge.client.github_app import GitHubApp

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
            "queues": queues or "all",
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
                    entity_key,
                    hub_turn,
                    cron_pass,
                )
                log.info("worker ready", extra={"schema": ",".join(result.after), "applied": list(result.applied)})
                signalled = await work(
                    name=name,
                    concurrency=concurrency,
                    queues=queues,
                    shutdown_graceful_timeout=shutdown_grace(queues),
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


def main(
    config: HubConfig,
    concurrency: int = 1,
    *,
    queues: list[str] | None = None,
    release_pass=None,
    loop_tick=None,
    memory_upkeep=None,
    entity_key=None,
    hub_turn=None,
    cron_pass=None,
) -> int:
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, not {concurrency}")
    passes = {"release_pass": release_pass, "loop_tick": loop_tick, "memory_upkeep": memory_upkeep}
    passes["entity_key"], passes["hub_turn"], passes["cron_pass"] = entity_key, hub_turn, cron_pass
    return asyncio.run(run(config, concurrency, queues=queues, **passes))

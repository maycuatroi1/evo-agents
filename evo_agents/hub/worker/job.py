"""A job of the hub's worker as a class: its name (in ``evo_agents.hub.jobs``), its queue, its cron when it runs on a
schedule, whether it holds a lock, and ``run``. ``Job.register`` puts it on the procrastinate App as the task the
decorators used to make, and ``evo_agents.hub.worker.registry`` lists every job by hand.

A periodic job (``cron`` set) takes the queueing lock of its own name, so at most one of it waits; ``locked`` adds the
lock of the same name, so two of it never run at once. ``run`` gets the job's context and the job's arguments, and a
periodic job's ``timestamp``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from procrastinate import App, JobContext
from psycopg_pool import AsyncConnectionPool

from evo_agents.hub import jobs
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.config import HubConfig
from evo_agents.hub.db import make_engine

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from procrastinate.tasks import Task
    from sqlalchemy.ext.asyncio import AsyncEngine

    from evo_agents.hub.server.credentials.models.sealing import Sealer
    from evo_agents.hub.server.forge.client.github_app import GitHubApp


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
    # the server's release pass (evo_agents.hub.server.releases.service.check), which the hub worker command hands down;
    # None checks and lands no release
    release_pass: Callable[[AsyncEngine, GitHubApp | None], Awaitable[dict]] | None = None
    # the server's tick of a project's Loop (evo_agents.hub.server.loop.tick), handed down the same way and called
    # with ``github_app``; None ticks no Loop
    loop_tick: Callable[..., Awaitable[dict]] | None = None
    # the server's upkeep of agents' memories (evo_agents.hub.server.memories.service.upkeep), handed down the same way; None
    # keeps none
    memory_upkeep: Callable[[AsyncEngine, BlobStore, Path], Awaitable[dict]] | None = None
    # the server's finder of the key of a build's entities step (evo_agents.hub.server.kg_entity_key.find), handed down
    # the same way; None finds no key, and a url model with a key_env stops before any call
    entity_key: Callable[..., Awaitable[tuple[dict, str | None]]] | None = None
    # the server's turn of a chat run on the hub runtime (evo_agents.hub.server.hub_runtime.take_turn), handed down the
    # same way; None takes no turn
    hub_turn: Callable[..., Awaitable[dict]] | None = None
    # the server's pass over the cron jobs of projects (evo_agents.hub.server.cron.service.fire.cron_pass), handed down the
    # same way; None fires no cron job
    cron_pass: Callable[[AsyncEngine], Awaitable[dict]] | None = None

    def __post_init__(self):
        if self.engine is None:  # an engine keeps no connection of its own (NullPool): nothing to dispose of
            object.__setattr__(self, "engine", make_engine(self.pool))


def hub(context: JobContext) -> HubContext:
    return context.additional_context["hub"]


def hub_engine(context: JobContext) -> AsyncEngine:
    """The engine a job reaches the hub's database through."""
    engine = hub(context).engine
    assert engine is not None  # HubContext.__post_init__ makes one on its pool
    return engine


class Job:
    """One task of the hub's queue. A subclass names it and writes ``run``."""

    name: ClassVar[str]
    queue: ClassVar[str] = jobs.DEFAULT_QUEUE
    cron: ClassVar[str | None] = None  # a periodic job's schedule; None for a job only deferred
    locked: ClassVar[bool] = False  # its jobs hold the lock of its name: one at a time

    async def run(self, context: JobContext, *args: Any, **kwargs: Any) -> dict | None:
        raise NotImplementedError

    def register(self, app: App) -> Task:
        """Add the job to ``app`` as a task, and its schedule when it has one."""
        task = app.task(
            name=self.name,
            pass_context=True,
            queue=self.queue,
            lock=self.name if self.locked else None,
            queueing_lock=self.name if self.cron else None,
        )(self.run)
        if self.cron is not None:
            app.periodic(cron=self.cron)(task)  # pyright: ignore[reportArgumentType]
        return task

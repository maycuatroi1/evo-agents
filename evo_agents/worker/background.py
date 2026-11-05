"""The background tasks of a worker's daemon (``daemon``): each one's error is logged as it ends."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("evo_agents.worker")


def task_failed(task: asyncio.Task) -> None:
    """Log what ended a background task of the daemon: a loop that raised stops for the daemon's life, and its error
    must not wait in the task until the daemon stops (or vanish, when nothing awaits it)."""
    if task.cancelled() or task.exception() is None:
        return
    exc = task.exception()
    log.error("a background task of the daemon failed", extra={"task": task.get_name()}, exc_info=exc)


def spawn(coro, name: str) -> asyncio.Task:
    """``coro`` as a task named ``name`` whose error is logged as it ends (``task_failed``)."""
    task = asyncio.create_task(coro, name=name)
    task.add_done_callback(task_failed)
    return task

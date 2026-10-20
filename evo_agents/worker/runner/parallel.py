"""The repos of a run, several at a time (``each``): the preflight's questions to each origin, the URLs of each origin
the leases are asked for, and the fetch and worktree of each repo a run prepares.

At most ``GIT_AT_ONCE`` repos of a run are worked on at once, each with its git commands one after the other, so a run
has at most that many git processes at a time. The repos start in the run's order, and the run's stop is checked
before each one starts (``Run.check``: a cancel, the timeout, the hub no longer holding the run, the daemon stopping
now). Once one raises, no other starts, those started finish (a git command is never cut off in a checkout whose locks
it may hold), and the error of the first repo in the run's order that raised is raised: the one a run that went
through its repos one by one would have raised, since every repo before it had started.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run

GIT_AT_ONCE = 8  # repos of a run worked on at once, and so git processes of the run at a time

Item = TypeVar("Item")
Result = TypeVar("Result")


async def each(
    run: Run,
    items: Sequence[Item],
    work: Callable[[Item], Awaitable[Result]],
    *,
    at_once: int = GIT_AT_ONCE,
) -> list[Result]:
    """``await work(item)`` for each of ``items``, at most ``at_once`` at a time, started in their order, each after
    ``run.check()``; the results in the order of ``items``. Once one raises, no other starts, those started finish,
    and the error of the first of ``items`` that raised is raised (``Stopped`` when it was the check)."""
    results: list = [None] * len(items)
    errors: dict[int, Exception] = {}
    queue = iter(enumerate(items))  # shared by the workers: each takes the next item in order

    async def worker() -> None:
        for index, item in queue:
            if errors:
                return
            try:
                run.check()
                results[index] = await work(item)
            except Exception as exc:  # raised again below, the first in order
                errors[index] = exc
                return

    await asyncio.gather(*(worker() for _ in range(min(max(at_once, 1), len(items)))))
    if errors:
        raise errors[min(errors)]
    return results

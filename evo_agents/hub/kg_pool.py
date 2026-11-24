"""The threads the hub answers kg_* reads on (plan kg-sound-fast-optimal, criterion a3): search, context, impact and
path are CPU work on a snapshot, and they run here, off the asyncio loop, on a pool of bounded size, so a burst of
reads waits its turn instead of taking every thread of the loop's default executor (which file and blob work
shares). Each call runs in a copy of the caller's context, as ``asyncio.to_thread`` runs it, so the pacing of a
read (``evo_agents.kg.pacing``) reaches the thread."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

WORKERS = max(2, min(8, (os.cpu_count() or 2)))
POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="hub-kg")


async def run(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """``func(*args, **kwargs)`` on the kg pool, in a copy of the current context."""
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(POOL, call)

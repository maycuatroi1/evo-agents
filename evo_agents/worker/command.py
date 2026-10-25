"""What every ``evo-agents worker`` command shares: its failures as an ``error:`` line and exit status, and the lazy
import of the worker extra (``evo_agents.worker.cli``)."""

from __future__ import annotations

import functools
import sys

from evo_agents.hub.client import HubError, exit_status
from evo_agents.worker.home import WorkerStateError

EXIT_FAILED = 1
EXIT_USAGE = 2
EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"


class MissingExtra(Exception):
    pass


def worker_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except MissingExtra as exc:
            print(
                f"error: evo-agents worker {args.worker_command} needs the worker extra ({exc}): {EXTRA_HINT}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        except (HubError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return exit_status(exc) if isinstance(exc, HubError) else EXIT_FAILED

    return run


def load_hubapi():
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
    return hubapi

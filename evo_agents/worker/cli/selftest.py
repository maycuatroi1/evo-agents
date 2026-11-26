"""``evo-agents worker selftest --runtime NAME``: the runtime's adapter, for real, on a tiny prompt in a scratch git
repository (``evo_agents.worker.selftest``). Its events are printed as they come; the exit status is 0 when the test
passed, 1 when it failed or the runtime is unavailable here, 2 for options that do not go together."""

from __future__ import annotations

import asyncio
import os

from evo_agents.clikit import Arg, Result, Stderr, Text
from evo_agents.hub import runs
from evo_agents.worker import selftest
from evo_agents.worker.adapter import load_adapters
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext


def refused(text: str, status: int) -> Result:
    return Result(None, Stderr(Text(f"error: {text}")), status)


class Selftest(WorkerCommand):
    name = "selftest"
    help = "run a runtime's adapter on a tiny prompt in a scratch repository and print its events (spends quota)"
    arguments = (
        Arg("--runtime", required=True, choices=runs.RUNTIMES, help="the runtime to check"),
        Arg("--model", help="the model; opencode takes provider/model"),
        Arg(
            "--effort",
            help="the reasoning effort (low for claude-code and codex unless given; an empty value: default)",
        ),
        Arg(
            "--timeout",
            type=float,
            default=selftest.DEFAULT_TIMEOUT,
            help=f"seconds before the agent is interrupted ({selftest.DEFAULT_TIMEOUT})",
        ),
        Arg("--keep", action="store_true", help="keep the scratch repository"),
        Arg(
            "--background",
            action="store_true",
            help=(
                "check that the session waits for a command run in the background: the agent starts "
                f"`{selftest.BACKGROUND_COMMAND}` that way, ends its turn, and writes the file once it ended "
                "(claude-code only)"
            ),
        ),
        Arg(
            "--gateway",
            metavar="URL",
            help="run through the gateway route URL (<hub>/v1, or a fake gateway's), its token in "
            f"{selftest.GATEWAY_TOKEN}",
        ),
    )

    def run(self, ctx: WorkerContext) -> Result:
        args = ctx.args
        cls = load_adapters().get(args.runtime)
        if cls is None:
            return refused(f"this worker has no adapter for {args.runtime}", 1)
        if args.background and args.runtime not in selftest.BACKGROUND_RUNTIMES:
            runtimes = ", ".join(selftest.BACKGROUND_RUNTIMES)
            return refused(f"--background checks the background commands of {runtimes}; {args.runtime} runs none", 2)
        if args.gateway and not (args.model and os.environ.get(selftest.GATEWAY_TOKEN)):
            return refused(f"--gateway needs --model and the route's token in {selftest.GATEWAY_TOKEN}", 2)
        detection = cls.detect()
        if not detection.available:
            return refused(f"{args.runtime} is unavailable here: {detection.reason}", 1)
        effort = args.effort if args.effort is not None else selftest.DEFAULT_EFFORT.get(args.runtime)
        status = asyncio.run(  # prints the events as they come, then the summary
            selftest.selftest(
                cls,
                args.runtime,
                model=args.model,
                effort=effort or None,
                timeout=args.timeout,
                keep=args.keep,
                background=args.background,
                route=args.gateway,
            )
        )
        return Result(None, status=status)

    def failed(self, exc: Exception) -> int | None:
        return None  # it runs an agent on this machine alone: no failure of the hub or the worker's state is its own


SELFTEST = Selftest()

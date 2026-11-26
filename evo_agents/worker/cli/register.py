"""``evo-agents worker register --name NAME --project P [--project P ...] --slots N [--label L ...]``: register this
machine directly, with the machine token of ``evo-agents hub login``, without a pairing code."""

from __future__ import annotations

import argparse

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub import runs
from evo_agents.hub.api import paths
from evo_agents.hub.client import Hub, load_credentials
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.membership import config_of, host_facts, joined, object_of, refuse_rejoin

MAX_SLOTS = runs.MAX_SLOTS  # 16, as the hub bounds workers.slots


def _slots(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not 1 <= number <= MAX_SLOTS:
        raise argparse.ArgumentTypeError(f"must be a whole number from 1 to {MAX_SLOTS}, not {value!r}")
    return number


class Register(WorkerCommand):
    name = "register"
    help = "register this machine with the machine token of `evo-agents hub login`, without a code"
    arguments = (
        Arg("--name", required=True, help="the worker's name, unique among your workers"),
        Arg("--project", required=True, action="append", help="a project it takes runs of; repeat it for each one"),
        Arg("--slots", type=_slots, default=1, help=f"runs it holds at once, 1 to {MAX_SLOTS} (default 1)"),
        Arg("--label", action="append", help="a label; repeat it for each one"),
        Arg("--allow-web-terminal", action="store_true", help="let the owner open a terminal on it from the web"),
        Arg("--replace", action="store_true", help="register even when this machine is a worker already"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        args = ctx.args
        refuse_rejoin(ctx.home, args.replace)
        credentials = load_credentials()
        hub = Hub(credentials.url, credentials.token)
        body = {
            "name": args.name,
            "projects": list(dict.fromkeys(args.project)),
            "slots": args.slots,
            "labels": list(dict.fromkeys(args.label or [])),
            "allow_web_terminal": args.allow_web_terminal,
            **host_facts(),
        }
        answer = object_of(hub, "POST", paths.WORKERS, body)
        config, token = config_of(hub.url, answer)
        return Result(None, Text(*joined(ctx.home, config, token, "Registered on")))


REGISTER = Register()

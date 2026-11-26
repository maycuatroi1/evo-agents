"""``evo-agents hub org worker list|share|unshare``: whom a shared worker is shared with, over /v1/org/workers
(``evo_agents.hub.server.workers.service.shares``; plan credential-sharing, criterion a5).

``list`` prints the caller's live workers (every live one for a hub admin): owner, name, whether it is shared, the
units it is narrowed to and what the newest report of its doctor found. ``share WORKER --unit SLUG`` narrows a worker
to a unit of the org chart, sharing it when it was not; ``unshare WORKER --unit SLUG`` takes the unit back, and with
the last one sharing ends. WORKER is a worker's name, or OWNER/NAME when a hub admin sees two of that name. A refusal
of the hub prints its reason and exits 1.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes) and ask the hub through
``evo_agents.hub.cli.org.resources.OrgChart``. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.org.resources import OrgChart
from evo_agents.hub.contract import returns_array, returns_object

EXIT_UNKNOWN = 1  # as a 404 of the hub exits
WORKER_SHARE_KEYS = ("id", "worker", "owner", "shared", "units", "doctor_at", "doctor_high", "doctor_medium")


class Unknown(Exception):
    """A worker name the caller's list does not hold, or holds twice."""


def find_worker(rows: list[dict], name: str) -> dict:
    """The row of ``name`` (a worker's name, or OWNER/NAME) among the workers the caller may share."""
    owner, _, bare = name.rpartition("/")
    found = [row for row in rows if row["worker"] == bare and owner in ("", row["owner"])]
    if not found:
        raise Unknown(f"you share no live worker named {name}: see `evo-agents hub org worker list`")
    if len(found) > 1:
        names = ", ".join(f"{row['owner']}/{row['worker']}" for row in found)
        raise Unknown(f"{len(found)} workers are named {bare}: name one as OWNER/NAME ({names})")
    return found[0]


def _doctor(row: dict) -> str:
    if row.get("doctor_at") is None:
        return "no doctor report"
    return f"doctor {row['doctor_at']}: {row['doctor_high']} high, {row['doctor_medium']} medium"


def worker_line(row: dict) -> str:
    if not row["shared"]:
        whom = "not shared"
    elif row["units"]:
        whom = "shared with " + ", ".join(row["units"])
    else:
        whom = "shared with every writer"
    return f"{row['owner']}/{row['worker']}  {whom}  ({_doctor(row)})"


class WorkerList(HubCommand):
    name = "list"
    help = "your live workers (every one for a hub admin) and whom each is shared with"
    output = returns_array(*WORKER_SHARE_KEYS, schema="WorkerShares")

    def run(self, ctx: HubContext) -> Result:
        rows = OrgChart(ctx.hub).workers()
        return Result(rows, Text("\n".join(worker_line(row) for row in rows) if rows else "You have no live worker."))


WORKER_UNIT = (
    Arg("worker", metavar="WORKER", help="the worker's name, or OWNER/NAME"),
    Arg("--unit", required=True, metavar="SLUG", help="the unit of the org chart"),
)


def changed(ctx: HubContext, change: Callable[[OrgChart, int, str], dict]) -> dict:
    """The row of the worker the command line names after ``change`` gave it the unit or took it back; Unknown when
    the caller's list does not hold that worker once."""
    chart = OrgChart(ctx.hub)
    found = find_worker(chart.workers(), ctx.args.worker)
    return change(chart, found["id"], ctx.args.unit)


def unknown(exc: Exception) -> int | None:
    """The exit status of a worker name that names no worker (its error printed); None for any other failure."""
    if not isinstance(exc, Unknown):
        return None
    print(f"error: {exc}", file=sys.stderr)
    return EXIT_UNKNOWN


class WorkerShare(HubCommand):
    name = "share"
    help = "narrow a worker to a unit and every unit below it, sharing it when it was not"
    arguments = WORKER_UNIT
    output = returns_object(*WORKER_SHARE_KEYS, schema="WorkerShares")

    def run(self, ctx: HubContext) -> Result:
        row = changed(ctx, OrgChart.share)
        return Result(row, Text(f"Worker {row['owner']}/{row['worker']} is shared with {', '.join(row['units'])}."))

    def failed(self, exc: Exception) -> int | None:
        return unknown(exc) or super().failed(exc)


class WorkerUnshare(HubCommand):
    name = "unshare"
    help = "take a unit back from a worker; with its last unit, sharing ends"
    arguments = WORKER_UNIT
    output = returns_object(*WORKER_SHARE_KEYS, schema="WorkerShares")

    def run(self, ctx: HubContext) -> Result:
        row = changed(ctx, OrgChart.unshare)
        unit = ctx.args.unit
        return Result(
            row, Text(f"Took unit {unit} back from worker {row['owner']}/{row['worker']}: {worker_line(row)}")
        )

    def failed(self, exc: Exception) -> int | None:
        return unknown(exc) or super().failed(exc)


WORKER = Group(
    "worker",
    "whom your shared workers are shared with: units of the org chart",
    [WorkerList(), WorkerShare(), WorkerUnshare()],
    dest="org_worker_command",
)

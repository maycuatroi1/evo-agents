"""``evo-agents hub kg optimize``: start a KG optimize run of a project now, or read its runs.

``optimize --project P`` asks the hub for a KG optimize run (POST /v1/kg/{project}/optimize, the writer role): at any
hour, under the review cost cap of the project's charter. The hub measures the graph, gives a review run to the worker
on duty, and measures again after the next build (``evo_agents.hub.server.kg_optimize``); the owner of the night shift
gets the report. One run of a project is in flight at a time: asked again, the hub answers with that run. ``--status``
lists the project's runs, newest first, with each one's summary (GET). ``--json`` prints the hub's answer. Standard
library only.

:data:`OPTIMIZE` is a class command of ``evo_agents.clikit``; :class:`KgOptimize` is the gateway to its route.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource

OPTIMIZE_RUN = (
    "id",
    "project",
    "via",
    "state",
    "night",
    "agent_run_id",
    "before_build_id",
    "after_build_id",
    "asked_weight",
    "summary",
    "report",
    "error",
    "created_at",
    "updated_at",
    "ended_at",
    "body",
)
OPTIMIZE_RUNS = ("active", "runs")


class KgOptimize(KgResource):
    """The KG optimize runs of a project: GET and POST /v1/kg/{p}/optimize."""

    collection = "optimize"

    def runs(self, limit: int) -> dict:
        return self.call("GET", limit=limit)

    def start(self) -> dict:
        return self.call("POST", body={})


def run_line(run: dict) -> str:
    review = f", review run {run['agent_run_id']}" if run.get("agent_run_id") else ""
    return f"#{run['id']} {run['via']} {run['state']}{review}: {run['summary']}"


class KgOptimizeCommand(HubCommand):
    name = "optimize"
    help = "start a KG optimize run of a project now (measure, fix, rebuild, measure again), or list its runs"
    arguments = (
        KG_PROJECT,
        Arg("--status", action="store_true", help="list the project's runs instead of starting one"),
        Arg("--limit", type=positive, default=10, help="with --status: the newest runs listed"),
    )
    output = returns_object(
        *OPTIMIZE_RUN,
        schema="OptimizeRun",
        variants=(("--status", returns_object(*OPTIMIZE_RUNS, schema="OptimizeRuns")),),
    )
    json_help = "machine-readable output: the run started or in flight, or with --status the project's runs"

    def run(self, ctx: HubContext) -> Result:
        optimize = KgOptimize.of(ctx)
        if ctx.args.status:
            found = optimize.runs(ctx.args.limit)
            lines = [run_line(run) for run in found["runs"]] or [f"project {ctx.project} has no KG optimize run yet"]
            return Result(found, Text(*lines))
        found = optimize.start()
        lines = [f"KG optimize run of project {ctx.project}: {run_line(found)}"]
        if found.get("report"):
            lines.append(found["report"])
        return Result(found, Text(*lines))


OPTIMIZE = KgOptimizeCommand()
NODES = (OPTIMIZE,)

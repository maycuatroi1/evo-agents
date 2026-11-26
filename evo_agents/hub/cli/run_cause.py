"""``evo-agents hub run-cause restore RUN --project P``: take back the cause a core plugin gave a failed run
(the writer role), over POST /v1/projects/{project}/runs/{run}/cause/restore. The run has the cause the hub had guessed
again, and its log says who restored it. ``--json`` prints the hub's CauseRestored: run_id, failure_cause, was.
A refusal (403, 404, 409) prints the hub's reason and exits 1. Standard library only.

The command is a class of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`RunCauses` is
the gateway to the route. Its ``--project`` is required and never read from a harness.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource

CAUSE_RESTORED = ("run_id", "failure_cause", "was")


class RunCauses(ProjectResource):
    """The failure causes of a project's runs: POST /v1/projects/{p}/runs/{run}/cause/restore."""

    collection = "runs"

    def restore(self, run: int) -> dict:
        return self.call("POST", run, "cause", "restore", body={})


class RunCause(HubCommand):
    name = "run-cause"
    help = "take back the cause a core plugin gave a failed run (the writer role)"
    arguments = (
        Arg("action", choices=["restore"], help="restore the cause the hub had guessed"),
        Arg("run", type=int, metavar="RUN", help="the run's id"),
        Arg("--project", required=True, help="project name on the hub"),
    )
    output = returns_object(*CAUSE_RESTORED, schema="CauseRestored")

    def run(self, ctx: HubContext) -> Result:
        found = RunCauses.of(ctx).restore(ctx.args.run)
        said = f"run {found['run_id']} has the cause {found['failure_cause']} again; a plugin had set {found['was']}"
        return Result(found, Text(said))


RUN_CAUSE = RunCause()

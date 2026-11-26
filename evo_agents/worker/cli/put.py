"""``evo-agents worker put FILE``: the agent of an author run puts the plan it wrote on the hub, as the member who
asked for it (PUT /v1/worker/runs/{id}/plan); replacing it needs ``--if-revision``, the revision it read.
"""

from __future__ import annotations

import json

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub.plan_writes import WRITTEN
from evo_agents.worker.agent_input import read_text
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError


def put_lines(answer: dict) -> list[str]:
    """What ``worker put`` prints of the hub's answer."""
    plan_text = f"plan {answer.get('plan_id')} of project {answer.get('project')}"
    if answer.get("created"):
        lines = [f"Created {plan_text} on the hub at revision {answer.get('revision')}."]
    elif answer.get("changed"):
        lines = [f"Revised {plan_text} on the hub: revision {answer.get('revision')}."]
    else:
        lines = [f"The hub already holds this as {plan_text}, revision {answer.get('revision')}; nothing changed."]
    lines += [
        f"warning: {warning.get('path') or '<root>'}: {warning.get('message')}"
        for warning in answer.get("warnings") or []
    ]
    return [*lines, f"Pass --if-revision {answer.get('revision')} to put it again."]


class Put(AgentCommand):
    name = "put"
    help = (
        "inside an author run: put the plan you wrote on the hub, as the member who asked for it; replacing it needs "
        "--if-revision"
    )
    kinds = ("author",)
    arguments = (
        Arg("file", metavar="FILE", help="the plan, YAML or JSON, as plan.schema.json has it (- for stdin)"),
        Arg(
            "--if-revision",
            type=int,
            metavar="REVISION",
            help="the revision on the hub this replaces, as `evo-agents worker plan --json` read it; leave it out to "
            "create the plan",
        ),
    )
    output = WRITTEN  # seam hub-cli-v1
    json_help = "the hub's answer as JSON"

    def run(self, ctx: AgentRunContext) -> Result:
        import yaml

        from evo_agents.hub.plans import MAX_PLAN_BYTES

        ctx.check()
        args = ctx.args
        text = read_text(args.file, "plan", 4 * MAX_PLAN_BYTES) or ""
        try:
            plan = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise WorkerStateError(f"{args.file} is neither YAML nor JSON: {exc}") from None
        if not isinstance(plan, dict):
            raise WorkerStateError(f"{args.file} holds no plan: a plan is a mapping with id, goal and steps")
        request: dict = {"body": json.loads(json.dumps(plan, default=str))}  # dates as text, as the hub keeps JSON
        if args.if_revision is not None:
            request["if_revision"] = args.if_revision

        async def work(hub) -> dict:
            return await hub.put_plan(ctx.run_id, request)

        answer = ctx.with_hub(work)
        return Result(answer, Text(*put_lines(answer)))


PUT = Put()

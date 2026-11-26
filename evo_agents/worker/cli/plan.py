"""``evo-agents worker plan``: the agent of a plan run or an author run prints the run's plan as the hub holds it now
(GET /v1/worker/runs/{id}/plan), as YAML under a line naming it, or the hub's answer with ``--json``.
"""

from __future__ import annotations

from evo_agents.clikit import Result, Text, Verbatim, Views, returns_object
from evo_agents.hub.plan_writes import PLAN_KEYS
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext


class Plan(AgentCommand):
    name = "plan"
    help = "inside a plan run or an author run: print the run's plan as the hub holds it now"
    kinds = ("plan", "author")
    output = returns_object(*PLAN_KEYS, schema="Plan")  # seam hub-cli-v1
    json_help = "the hub's answer as JSON"

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()

        async def work(hub) -> dict:
            return await hub.plan(ctx.run_id)

        view = ctx.with_hub(work)
        if ctx.args.json:
            return Result(view)
        import yaml

        header = (
            f"# The plan {view.get('plan_id')} of project {view.get('project')} at revision {view.get('revision')}, "
            "as the hub holds it now."
        )
        body = yaml.safe_dump(view.get("body") or {}, allow_unicode=True, sort_keys=False, width=120)
        return Result(view, Views(Text(header), Verbatim(body)))


PLAN = Plan()

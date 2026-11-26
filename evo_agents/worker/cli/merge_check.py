"""``evo-agents worker merge-check REPO [--pr N]``: the agent of a plan run has the hub judge the run's pull request on
github.com at its head before it merges it, in a repo whose policy has ``merge_check`` on
(``evo_agents.hub.server.merge_check``).

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker wait`` does, refuses outside a plan run of this
worker, and calls the hub with the worker's token (POST /v1/worker/runs/{id}/merge-check) with the branch the run
pushes in REPO. While the Judges work, the hub opens a wait of the run: the command notes it in the run's file of
decisions, as ``worker wait`` does, and says to end the turn, so the run parks and holds no worker slot; the verdict
comes back in this session as the message that answers the wait. When the check of that head has its verdict already,
it prints it. A verdict holds for the head it judged alone: after another push, ask again.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext
from evo_agents.worker.run_decisions import noted_decision


class MergeCheck(AgentCommand):
    name = "merge-check"
    help = (
        "inside a plan run: have the hub judge the run's pull request on github.com at its head before you merge it; "
        "ends in a wait for the verdict"
    )
    parser_options = {
        "description": "Have the hub judge the run's pull request on github.com at its head before you merge it, in "
        "a repo whose policy has merge_check on. The verdict answers a wait of the run: end your turn after it."
    }
    arguments = (
        Arg("repo", metavar="REPO", help="the run's repo of the pull request"),
        Arg("--pr", type=int, metavar="N", help="the pull request; without it, the open one of the branch"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        args = ctx.args
        workspace = ctx.workspaces().get(args.repo)
        body: dict = {"repo": args.repo, "number": args.pr}
        if workspace is not None:
            body["branch"] = workspace.branch

        async def work(hub) -> dict:
            return await hub.merge_check(ctx.run_id, body)

        answer = ctx.with_hub(work)
        what = f"pull request #{answer.get('number')} of {args.repo} at {str(answer.get('head_sha') or '')[:12]}"
        if answer.get("decision_id") is None:
            reason = f": {answer['reason']}" if answer.get("reason") else "."
            lines = [f"The merge check of {what} {answer.get('state')}{reason}"]
            if answer.get("state") == "passed":
                lines.append(
                    f"Merge only that head: gh pr merge {answer.get('number')} --match-head-commit "
                    f"{answer.get('head_sha')}"
                )
            return Result(answer, Text(*lines))
        decision_id = noted_decision(ctx.home, ctx.run_id, {"id": answer.get("decision_id")})
        return Result(
            answer,
            Text(
                f"The hub judges {what}: wait #{decision_id} is open until its verdict. End your turn once no work "
                "that does not depend on it is left: the run waits without the agent and holds no worker slot, and "
                f"this session goes on with a message naming decision #{decision_id} when the verdict comes. Do not "
                "merge it before."
            ),
        )


MERGE_CHECK = MergeCheck()

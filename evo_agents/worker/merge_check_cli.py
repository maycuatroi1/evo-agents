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

COMMAND = "merge-check"


def cmd_merge_check(args) -> int:
    from evo_agents.worker import cli as worker_cli  # it registers this module's parser

    return worker_cli.worker_command(_ask)(args)


def _ask(args) -> int:
    from evo_agents.worker import cli as worker_cli

    agent = worker_cli.agent_run(COMMAND)
    workspace = worker_cli._workspaces(agent).get(args.repo)
    body: dict = {"repo": args.repo, "number": args.pr}
    if workspace is not None:
        body["branch"] = workspace.branch

    async def work(hub) -> dict:
        return await hub.merge_check(agent.run_id, body)

    answer = worker_cli.with_hub(agent, work)
    what = f"pull request #{answer.get('number')} of {args.repo} at {str(answer.get('head_sha') or '')[:12]}"
    if answer.get("decision_id") is None:
        reason = f": {answer['reason']}" if answer.get("reason") else "."
        print(f"The merge check of {what} {answer.get('state')}{reason}")
        if answer.get("state") == "passed":
            print(
                f"Merge only that head: gh pr merge {answer.get('number')} --match-head-commit {answer.get('head_sha')}"
            )
        return 0
    decision_id = worker_cli._noted_decision(agent, {"id": answer.get("decision_id")})
    print(
        f"The hub judges {what}: wait #{decision_id} is open until its verdict. End your turn once no work that does "
        "not depend on it is left: the run waits without the agent and holds no worker slot, and this session goes on "
        f"with a message naming decision #{decision_id} when the verdict comes. Do not merge it before."
    )
    return 0


def register_merge_check(wsub) -> None:
    check = wsub.add_parser(
        COMMAND,
        help="inside a plan run: have the hub judge the run's pull request on github.com at its head before you merge "
        "it; ends in a wait for the verdict",
        description="Have the hub judge the run's pull request on github.com at its head before you merge it, in a "
        "repo whose policy has merge_check on. The verdict answers a wait of the run: end your turn after it.",
    )
    check.add_argument("repo", metavar="REPO", help="the run's repo of the pull request")
    check.add_argument("--pr", type=int, metavar="N", help="the pull request; without it, the open one of the branch")
    check.set_defaults(func=cmd_merge_check)

"""``evo-agents worker notify``: the agent of a plan run tells the run's owner of a push or a merge into a default
branch (POST /v1/worker/runs/{id}/notices); the hub sends plan_finished and run_failed itself.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext

AGENT_NOTICE_KINDS = ("push_default_branch", "merge_default_branch")  # the hub sends plan_finished and run_failed


class Notify(AgentCommand):
    name = "notify"
    help = "inside a plan run: tell the run's owner of a push or merge into a default branch"
    arguments = (
        Arg("--kind", required=True, choices=AGENT_NOTICE_KINDS),
        Arg("--title", required=True, help="one line"),
        Arg("--body", help="what happened, at most 16 KiB"),
        Arg("--repo", help="the run's repo"),
        Arg("--branch", help="the branch pushed or merged into"),
        Arg("--commit", action="append", metavar="SHA", help="a commit pushed or merged; repeat it"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        args = ctx.args
        body: dict = {"kind": args.kind, "title": args.title}
        for name in ("body", "repo", "branch"):
            if getattr(args, name) is not None:
                body[name] = getattr(args, name)
        if args.commit:
            body["commits"] = list(dict.fromkeys(args.commit))

        async def work(hub) -> dict:
            return await hub.notice(ctx.run_id, body)

        answer = ctx.with_hub(work)
        return Result(answer, Text(f"Notice #{answer.get('id')} sent to the owner of run #{ctx.run_id}: {args.title}"))


NOTIFY = Notify()

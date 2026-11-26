"""``evo-agents worker step KEY STATUS``: the agent of a plan run reports a step of the run's plan to the hub
(POST /v1/worker/runs/{id}/steps/{key}). ``done`` runs the step's verify commands again in the repo's worktree, commits
what is left and pushes the run's branch before it reports (``step_verify``); ``verify`` runs the verify alone, as a
job of the worker. What it checks first is in ``evo_agents.worker.step_report``.
"""

from __future__ import annotations

from evo_agents.clikit import NOTHING, Arg, Result, Text
from evo_agents.hub import proofs
from evo_agents.worker import output_cli, step_report, step_verify
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext

AGENT_STEP_STATUSES = ("in_progress", "done", "pending")  # what POST /v1/worker/runs/{id}/steps/{key} takes


class Step(AgentCommand):
    name = "step"
    help = "inside a plan run: report a step of the plan; done runs its verify commands again and pushes"
    arguments = (
        Arg("key", help="the step's id, or its order"),
        Arg(
            "status",
            choices=(*AGENT_STEP_STATUSES, "verify"),
            help="in_progress, done, pending to hand it back, or verify to run the verify as a job of the worker alone",
        ),
        Arg("--repo", help="the run's repo the step is in (default: the plan's repo of the step)"),
        Arg("--evidence", help="what was done and how it was checked; a line per decision taken"),
        Arg(
            proofs.FLAG,
            action="append",
            metavar="ID=VALUE",
            help="with done: the proof of a criterion of the plan's acceptance the step names (a4=path::test_name, "
            "a4=COMMAND or a4=n/a: why); repeat it, once for each; done is refused without one for each",
        ),
        Arg(
            "--verify",
            action="append",
            metavar="COMMAND",
            help="a command that checks the step, run in the repo's worktree; repeat it; done and verify need one",
        ),
        Arg(
            "--wait",
            type=float,
            metavar="SECONDS",
            help=f"how long done and verify wait on the verify (default {step_verify.verify_jobs.WAIT:g}; -1: until it "
            f"ends); it goes on after, and they exit {step_verify.verify_jobs.EXIT_RUNNING}",
        ),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        verify = step_report.verify_commands(args)
        output_cli.request_scan(ctx.home, ctx.run_id)  # the run's .evo-run/outputs/ goes to the hub with each report
        workspaces = step_report.step_workspaces(ctx, args.repo)
        step_report.refuse_done_after_conflict(ctx, args.status)

        async def work(hub) -> Result:
            body = (await hub.plan(ctx.run_id)).get("body") or {}
            step = step_report.plan_step(body, args.key)
            name = args.repo or step_report.step_repo(body, step, workspaces)
            wait = step_verify.verify_jobs.WAIT if args.wait is None else None if args.wait < 0 else args.wait
            if args.status == "verify":
                name = step_report.named_repo(name, args.key, workspaces)
                status = await step_verify.verify_step(ctx, args.key, name, workspaces[name].worktree, verify, wait)
                return Result(None, NOTHING, status)
            report: dict = {"status": args.status}
            if name is not None:
                report["repo"] = name
            if args.evidence is not None:
                report["evidence"] = args.evidence
            pushed = None
            if args.status == "done":
                name = step_report.named_repo(name, args.key, workspaces)
                given = step_report.step_proofs(args, body, step, workspaces[name].worktree)
                if given is not None:
                    report["proofs"] = given
                done = await step_verify.done_step(hub, ctx, args.key, step, body, name, workspaces[name], verify, wait)
                if isinstance(done, int):
                    return Result(None, NOTHING, done)
                pushed, report["verify"], line = done
                await step_report.notify_push(hub, ctx, name, pushed)
                report["commit_sha"] = pushed.head
                report["evidence"] = step_verify.with_line(args.evidence, line, step_report.MAX_EVIDENCE_CHARS)
            answer = await hub.step(ctx.run_id, args.key, report)
            where = f" ({name}@{pushed.head[:12]}, pushed to {pushed.branch})" if pushed is not None else ""
            kept = "" if answer.get("written", True) else ", as the plan had it already"
            return Result(
                answer, Text(f"Step {args.key} of plan {answer.get('plan_id')}: {answer.get('status')}{where}{kept}.")
            )

        return ctx.with_hub(work)


STEP = Step()

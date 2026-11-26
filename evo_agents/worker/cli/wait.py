"""``evo-agents worker wait``: the agent of a plan run waits for a time or an event of the hub rather than ask the
owner when it came (POST /v1/worker/runs/{id}/waits). The wait is noted in the run's file of decisions, as a decision
is, so the run parks without its agent until it wakes.
"""

from __future__ import annotations

from datetime import UTC, datetime

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub import runs
from evo_agents.worker.agent_input import payload_contains, wait_until
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError
from evo_agents.worker.run_decisions import noted_decision


def wait_body(args) -> dict:
    """The wait ``worker wait`` opens, once its options agree."""
    if args.until is None and args.on is None:
        raise WorkerStateError("a wait wakes at a time (--until), on an event of the hub (--on), or both")
    if args.contains and args.on is None:
        raise WorkerStateError("--contains filters the event of --on: name its kind with --on")
    body: dict = {"reason": args.reason}
    if args.until is not None:
        body["until"] = wait_until(args.until, datetime.now(UTC))
    if args.on is not None:
        body["on"] = args.on
    if args.contains:
        body["contains"] = payload_contains(args.contains)
    if args.step is not None:
        body["step_key"] = args.step
    return body


class Wait(AgentCommand):
    name = "wait"
    help = (
        "inside a plan run: wait for a time or an event of the hub rather than ask the owner when it came; prints "
        "the wait's id"
    )
    parser_options = {
        "description": "Wait for a time or an event of the hub rather than ask the owner when it came. A step of "
        "another plan done: --on plan.step --contains plan=northwind-launch --contains step=8 --contains status=done "
        "--until +12h. A GitHub Actions run (the CI of a tag, a deploy) that completes or has a job fail: --on "
        "github.workflow_run --contains repo=OWNER/NAME --contains run_id=ID --until +90m, ID the databaseId of `gh "
        "run list`; the hub reads that run every minute and wakes on its first failed job, or, with --contains "
        "status=completed, on its end alone."
    }
    arguments = (
        Arg("--reason", required=True, help="what the run waits for and why, for the owner to read"),
        Arg(
            "--until",
            metavar="TIME",
            help=f"wake at this time at the latest: ISO 8601 with a timezone (2026-10-30T09:00Z) or a span (+6h, "
            f"+90m, +5d), at most {runs.MAX_WAIT_DAYS} days ahead; without it, {runs.MAX_WAIT_DAYS} days",
        ),
        Arg(
            "--on",
            metavar="KIND",
            help="wake sooner on an event of the hub of this kind: plan.step (a step's status of a plan set or "
            "changed), github.workflow_run (a GitHub Actions run that completed or had a job fail), tasks.status, "
            "canvas.baseline, canvas.claim, signal.posted, srs.version.approved, or plugin.<plugin>.<name>",
        ),
        Arg(
            "--contains",
            action="append",
            metavar="KEY=VALUE",
            help="the event's payload has KEY equal to VALUE (read as JSON when it is), such as plan=SLUG, step=8 and "
            "status=done for plan.step, or repo=OWNER/NAME, run_id=ID and status=completed, failed=true or attempt=2 "
            "for github.workflow_run; repeat it",
        ),
        Arg("--step", help="the step of the run's plan it is about"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        args = ctx.args
        body = wait_body(args)

        async def work(hub) -> dict:
            return await hub.wait(ctx.run_id, body)

        answer = ctx.with_hub(work)
        decision_id = noted_decision(ctx.home, ctx.run_id, answer)
        wake_at = str(answer.get("wake_at") or "")[:16].replace("T", " ")
        sooner = f", or sooner on an event {args.on} of the project" if args.on is not None else ""
        return Result(
            answer,
            Text(
                f"Wait #{decision_id} is open: the run wakes at {wake_at} UTC at the latest{sooner}. End your turn "
                "once no work that does not depend on it is left: the run waits without the agent, the time does not "
                "count toward its timeout, and this session goes on with a message naming decision "
                f"#{decision_id} when it wakes."
            ),
        )


WAIT = Wait()

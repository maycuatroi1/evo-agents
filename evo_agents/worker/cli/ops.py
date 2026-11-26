"""``evo-agents worker ops rerun|redeliver|answer-wait|curator-resume|read|finding|priority|plan-dispatch``: what the Orchestrator of a
project's default system does in an ops thread the hub opened to it for a batch of the ops queue
(``evo_agents.hub.server.ops_actions``; ``evo_agents.hub.server.ops_threads`` opens the threads).

- ``rerun RUN --reason TEXT``: hand the work of a chat or task run of the batch that failed or was lost again, for a
  new session, to any worker that fits.
- ``redeliver RUN --reason TEXT``: the same, to a healthy worker of the project.
- ``answer-wait DECISION --text TEXT --reason TEXT``: answer a stuck wait of the batch, which wakes its run; a
  decision a person must make is never one.
- ``curator-resume --reason TEXT``: let the Curator's schedules its breaker paused run again.
- ``read NOTICE --reason TEXT``: mark a notice of the project read.
- ``finding --lens LENS --severity low|medium|high --title TEXT --body TEXT --reason TEXT``: write a finding for the
  Curator, with the batch as its evidence: a recurring cause is fixed in the system, not rerun.
- ``priority RUN LEVEL --reason TEXT``: give a queued run of the project another priority (urgent, high, medium, low),
  the order a worker claims its queue in: for a ``run.queued_long`` item.
- ``plan-dispatch PLAN --reason TEXT``: hand a stalled plan of the batch (a ``plan.stalled`` item) again to the entry
  agent of the default system, as ``evo-agents hub plan system-dispatch`` does for a person.

Each is a ``ChatCommand`` (``evo_agents.worker.cli.base``): it reads the run from EVO_RUN_ID like ``worker chat`` and
is for a chat run in an ops thread only. The
hub refuses what is outside the frame, with the reason, and the command prints it on stderr and exits 1. Each success
is one line, and the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No ``--json``: this
module may not import the hub's contract.
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.hub.runs import PRIORITIES
from evo_agents.worker.cli.base import AgentRunContext, ChatCommand

SEVERITIES = ("low", "medium", "high")
REASON = Arg("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
FAILED_RUN = Arg("run", type=int, metavar="RUN", help="the failed run's id")


def _text(value: str) -> str:
    text = sys.stdin.read() if value == "-" else value
    return text.strip() if value == "-" else text


def _said(answer: dict, done: str) -> Result:
    return Result(answer, Text(f"{done}; the Decision line is message #{answer['message_id']}."))


class OpsRerun(ChatCommand):
    name = "rerun"
    words = "chat ops rerun"
    help = "run the work of a failed run of the batch again, on any worker that fits"
    arguments = (FAILED_RUN, REASON)

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        answer = ctx.in_chats("ops/rerun", {"run_id": args.run, "reason": _text(args.reason)})
        return _said(answer, f"Ran the work of run #{args.run} again as run #{answer['run_id']}")


class OpsRedeliver(ChatCommand):
    name = "redeliver"
    words = "chat ops redeliver"
    help = "hand the work of a failed run of the batch to a healthy worker"
    arguments = (FAILED_RUN, REASON)

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        answer = ctx.in_chats("ops/redeliver", {"run_id": args.run, "reason": _text(args.reason)})
        return _said(answer, f"Redelivered the work of run #{args.run} as run #{answer['run_id']}")


class OpsAnswerWait(ChatCommand):
    name = "answer-wait"
    words = "chat ops answer-wait"
    help = "answer a stuck wait of the batch, which wakes its run"
    arguments = (
        Arg("decision", type=int, metavar="DECISION", help="the wait's decision id"),
        Arg("--text", required=True, metavar="TEXT", help="the answer (- for stdin)"),
        REASON,
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        body = {"decision_id": args.decision, "text": _text(args.text), "reason": _text(args.reason)}
        answer = ctx.in_chats("ops/answer-wait", body)
        return _said(answer, f"Answered wait #{args.decision}, which woke run #{answer['run_id']}")


class OpsCuratorResume(ChatCommand):
    name = "curator-resume"
    words = "chat ops curator-resume"
    help = "resume the Curator's schedules its breaker paused"
    arguments = (REASON,)

    def run(self, ctx: AgentRunContext) -> Result:
        answer = ctx.in_chats("ops/curator-resume", {"reason": _text(ctx.args.reason)})
        return _said(answer, "Resumed the Curator")


class OpsRead(ChatCommand):
    name = "read"
    words = "chat ops read"
    help = "mark a notice of the project read"
    arguments = (Arg("notice", type=int, metavar="NOTICE", help="the notice's id"), REASON)

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        answer = ctx.in_chats("ops/read", {"notification_id": args.notice, "reason": _text(args.reason)})
        return _said(answer, f"Marked notice #{args.notice} read")


class OpsFinding(ChatCommand):
    name = "finding"
    words = "chat ops finding"
    help = "write a finding for the Curator: fix the system, not the run"
    arguments = (
        Arg("--lens", required=True, metavar="LENS", help="the review lens, failed_runs for one"),
        Arg("--severity", required=True, choices=SEVERITIES),
        Arg("--title", required=True, metavar="TEXT", help="the finding's title"),
        Arg("--body", required=True, metavar="TEXT", help="what is wrong, and the fix (- for stdin)"),
        REASON,
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        body = {
            "lens": args.lens,
            "severity": args.severity,
            "title": _text(args.title),
            "body": _text(args.body),
            "reason": _text(args.reason),
        }
        answer = ctx.in_chats("ops/finding", body)
        return _said(answer, f"Wrote finding #{answer['finding_id']} for the Curator")


class OpsPriority(ChatCommand):
    name = "priority"
    words = "chat ops priority"
    help = "give a queued run of the project another priority: the queue's order"
    arguments = (
        Arg("run", type=int, metavar="RUN", help="the queued run's id"),
        Arg("level", choices=PRIORITIES, metavar="LEVEL", help=f"one of {', '.join(PRIORITIES)}"),
        REASON,
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        body = {"run_id": args.run, "level": args.level, "reason": _text(args.reason)}
        answer = ctx.in_chats("ops/priority", body)
        return _said(answer, f"Set the priority of queued run #{args.run} to {args.level}")


class OpsPlanDispatch(ChatCommand):
    name = "plan-dispatch"
    words = "chat ops plan-dispatch"
    help = "hand a stalled plan of the batch again to the entry agent of the default system"
    arguments = (Arg("plan", metavar="PLAN", help="the stalled plan's id"), REASON)

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        answer = ctx.in_chats("ops/plan-dispatch", {"plan_id": args.plan, "reason": _text(args.reason)})
        return _said(answer, f"Handed plan {args.plan} again to the entry agent, in run #{answer['run_id']}")


OPS = Group(
    "ops",
    "as the Orchestrator, inside an ops thread: act on the batch the hub opened it for",
    [
        OpsRerun(),
        OpsRedeliver(),
        OpsAnswerWait(),
        OpsCuratorResume(),
        OpsRead(),
        OpsFinding(),
        OpsPriority(),
        OpsPlanDispatch(),
    ],
)

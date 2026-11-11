"""``evo-agents worker ops rerun|redeliver|answer-wait|curator-resume|read|finding``: what the Orchestrator of a
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

All read the run from EVO_RUN_ID like ``worker chat`` (``chat_cli``) and are for a chat run in an ops thread only. The
hub refuses what is outside the frame, with the reason, and the command prints it on stderr and exits 1. Each success
is one line, and the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No ``--json``: this
module may not import the hub's contract.
"""

from __future__ import annotations

import sys

from evo_agents.worker.chat_cli import agent_run, call, chat_command

SEVERITIES = ("low", "medium", "high")


def _text(value: str) -> str:
    text = sys.stdin.read() if value == "-" else value
    return text.strip() if value == "-" else text


def _act(name: str, path: str, body: dict) -> dict:
    return call(agent_run(f"ops {name}"), f"ops/{path}", body)


def _said(answer: dict, done: str) -> int:
    print(f"{done}; the Decision line is message #{answer['message_id']}.")
    return 0


@chat_command
def cmd_rerun(args) -> int:
    answer = _act("rerun", "rerun", {"run_id": args.run, "reason": _text(args.reason)})
    return _said(answer, f"Ran the work of run #{args.run} again as run #{answer['run_id']}")


@chat_command
def cmd_redeliver(args) -> int:
    answer = _act("redeliver", "redeliver", {"run_id": args.run, "reason": _text(args.reason)})
    return _said(answer, f"Redelivered the work of run #{args.run} as run #{answer['run_id']}")


@chat_command
def cmd_answer_wait(args) -> int:
    body = {"decision_id": args.decision, "text": _text(args.text), "reason": _text(args.reason)}
    answer = _act("answer-wait", "answer-wait", body)
    return _said(answer, f"Answered wait #{args.decision}, which woke run #{answer['run_id']}")


@chat_command
def cmd_curator_resume(args) -> int:
    answer = _act("curator-resume", "curator-resume", {"reason": _text(args.reason)})
    return _said(answer, "Resumed the Curator")


@chat_command
def cmd_read(args) -> int:
    answer = _act("read", "read", {"notification_id": args.notice, "reason": _text(args.reason)})
    return _said(answer, f"Marked notice #{args.notice} read")


@chat_command
def cmd_finding(args) -> int:
    body = {
        "lens": args.lens,
        "severity": args.severity,
        "title": _text(args.title),
        "body": _text(args.body),
        "reason": _text(args.reason),
    }
    answer = _act("finding", "finding", body)
    return _said(answer, f"Wrote finding #{answer['finding_id']} for the Curator")


def _reason(parser) -> None:
    parser.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")


def register_ops(wsub) -> None:
    """``evo-agents worker ops`` and its commands, under the parser ``wsub`` of ``evo-agents worker``."""
    ops = wsub.add_parser(
        "ops", help="as the Orchestrator, inside an ops thread: act on the batch the hub opened it for"
    )
    osub = ops.add_subparsers(dest="ops_command", required=True)
    for name, func, help_text in (
        ("rerun", cmd_rerun, "run the work of a failed run of the batch again, on any worker that fits"),
        ("redeliver", cmd_redeliver, "hand the work of a failed run of the batch to a healthy worker"),
    ):
        parser = osub.add_parser(name, help=help_text)
        parser.add_argument("run", type=int, metavar="RUN", help="the failed run's id")
        _reason(parser)
        parser.set_defaults(func=func)
    wait = osub.add_parser("answer-wait", help="answer a stuck wait of the batch, which wakes its run")
    wait.add_argument("decision", type=int, metavar="DECISION", help="the wait's decision id")
    wait.add_argument("--text", required=True, metavar="TEXT", help="the answer (- for stdin)")
    _reason(wait)
    wait.set_defaults(func=cmd_answer_wait)
    resume = osub.add_parser("curator-resume", help="resume the Curator's schedules its breaker paused")
    _reason(resume)
    resume.set_defaults(func=cmd_curator_resume)
    read = osub.add_parser("read", help="mark a notice of the project read")
    read.add_argument("notice", type=int, metavar="NOTICE", help="the notice's id")
    _reason(read)
    read.set_defaults(func=cmd_read)
    finding = osub.add_parser("finding", help="write a finding for the Curator: fix the system, not the run")
    finding.add_argument("--lens", required=True, metavar="LENS", help="the review lens, failed_runs for one")
    finding.add_argument("--severity", required=True, choices=SEVERITIES)
    finding.add_argument("--title", required=True, metavar="TEXT", help="the finding's title")
    finding.add_argument("--body", required=True, metavar="TEXT", help="what is wrong, and the fix (- for stdin)")
    _reason(finding)
    finding.set_defaults(func=cmd_finding)

"""``evo-agents worker triage rerun|plan``: what the agent of a triage thread does on its own for a failed run
(``evo_agents.hub.server.triage_actions``; ``evo_agents.hub.triage_rules`` is the frame the hub enforces).

- ``rerun --reason TEXT [--worker NAME] [--runtime NAME] [--account NAME] [--confirm-cause]``: run the failed run of
  the thread again for its owner, on a worker, runtime or account that is a fit candidate.
- ``plan STEP --reason TEXT --if-revision N [--verify TEXT] [--what TEXT] [--rerun ...]``: change the ``verify`` or
  narrow the ``what`` of a pending step of the failed run's plan, as a new revision under the owner's name.

Both read the run from EVO_RUN_ID like ``worker chat`` (``chat_cli``) and are for a chat run only. The hub refuses
what is outside the frame, with the reason, and the command prints it on stderr and exits 1. Each success is one line
and the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No ``--json``: this module may not
import the hub's contract.
"""

from __future__ import annotations

import sys

from evo_agents.worker.chat_cli import agent_run, call, chat_command


def _text(value: str) -> str:
    text = sys.stdin.read() if value == "-" else value
    return text.strip() if value == "-" else text


def _options(args) -> dict:
    found = {
        "worker": args.worker,
        "runtime": args.runtime,
        "account": args.account,
        "cause_confirmed": True if args.confirm_cause else None,
    }
    return {key: value for key, value in found.items() if value is not None}


@chat_command
def cmd_rerun(args) -> int:
    run = agent_run("triage rerun")
    answer = call(run, "triage/rerun", {"reason": _text(args.reason), **_options(args)})
    print(f"Ran the failed run again as run #{answer['run_id']}; the Decision line is message #{answer['message_id']}.")
    return 0


@chat_command
def cmd_plan(args) -> int:
    run = agent_run("triage plan")
    body = {"step": args.step, "reason": _text(args.reason), "if_revision": args.if_revision, "rerun": bool(args.rerun)}
    for name in ("verify", "what"):
        if getattr(args, name) is not None:
            body[name] = _text(getattr(args, name))
    answer = call(run, "triage/plan", {**body, **_options(args)})
    again = f" and ran the failed run again as run #{answer['run_id']}" if answer.get("run_id") else ""
    print(
        f"Wrote revision {answer['plan_revision']} of the plan{again}; the Decision line is message "
        f"#{answer['message_id']}."
    )
    return 0


def _frame_options(parser) -> None:
    parser.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
    parser.add_argument("--worker", metavar="NAME", help="rerun on this worker of the owner, a fit candidate")
    parser.add_argument("--runtime", metavar="NAME", help="rerun with this runtime, one the worker has")
    parser.add_argument("--account", metavar="NAME", help="rerun with this account of the owner, ready on the worker")
    parser.add_argument(
        "--confirm-cause",
        action="store_true",
        help="the cause the hub guessed is right: you read the run's log, and it says so",
    )


def register_triage(wsub) -> None:
    """``evo-agents worker triage`` and its commands, under the parser ``wsub`` of ``evo-agents worker``."""
    triage = wsub.add_parser(
        "triage", help="inside a triage thread's chat run: rerun the failed run, or edit its plan, inside the frame"
    )
    tsub = triage.add_subparsers(dest="triage_command", required=True)
    rerun = tsub.add_parser("rerun", help="run the failed run of the thread again for its owner")
    _frame_options(rerun)
    rerun.set_defaults(func=cmd_rerun)
    plan = tsub.add_parser(
        "plan", help="change the verify or narrow the what of a pending step of the failed run's plan"
    )
    plan.add_argument("step", metavar="STEP", help="the step's id, a step that is pending")
    plan.add_argument("--if-revision", type=int, required=True, metavar="REVISION", help="the revision it was read at")
    plan.add_argument(
        "--verify", metavar="TEXT", help="the new verify, no command fewer than the old one (- for stdin)"
    )
    plan.add_argument("--what", metavar="TEXT", help="the new what, shorter than the old one (- for stdin)")
    plan.add_argument("--rerun", action="store_true", help="run the failed step again once the plan is written")
    _frame_options(plan)
    plan.set_defaults(func=cmd_plan)

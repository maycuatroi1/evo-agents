"""``evo-agents worker triage rerun|plan|invite|cancel|merge-check|answer|reassign|ask-owner``: what the agent of a
triage thread does on its own for a failed run (``evo_agents.hub.server.triage_actions``; ``evo_agents.hub.triage_rules``
is the frame the hub enforces), for a merge check the hub holds (``evo_agents.hub.server.merge_holds``), or the
Orchestrator of an agent system in a thread the hub opened to it (``evo_agents.hub.server.orchestrator_actions``).

- ``rerun --reason TEXT [--runtime NAME] [--account NAME] [--confirm-cause]``: run the failed run of
  the thread again for its owner, with a runtime or account that is a fit candidate.
- ``plan STEP --reason TEXT --if-revision N [--verify TEXT] [--what TEXT] [--rerun ...]``: change the ``verify`` or
  narrow the ``what`` of a pending step of the failed run's plan, as a new revision under the owner's name.
  ``plan --edits FILE ...`` does it for several steps at once, one revision and one ``Decision:`` line: FILE (``-``
  for stdin) is a JSON list of ``{"step": ID, "verify": TEXT, "what": TEXT, "why": TEXT}``, verify or what or both
  in each, and ``why`` when that step needs a reason of its own.

- ``cancel --reason TEXT``: in the thread of a stale run (no event for ``triage.STALE_MINUTES`` while its worker
  heartbeats), ask its worker to stop it, for its owner; ``rerun`` runs it again once it has ended.

- ``invite HANDLE --reason TEXT``: invite the agent of another project, one the failed run's error names, into the
  chat, so a mention can hand it work; the hub refuses and says in the thread when the owner is no writer there or the
  chat's label does not hold.

- ``merge-check rejudge|return --reason TEXT``: in the thread of a merge check the hub holds because its own machinery
  failed it, judge the check again once the system is fixed (its verdict wakes the plan run), or hand the failure back
  to the plan run when the pull request caused it after all.

- ``answer --text TEXT --reason TEXT``, ``reassign HANDLE --note TEXT --reason TEXT``, ``ask-owner --question TEXT
  --option KEY=LABEL[:DESCRIPTION] ... --reason TEXT [--context TEXT]``: the Orchestrator answers the thread (an
  escalation's wait wakes with the text; with no wait the text goes to the agent of the thread), gives its work to
  another agent node of the system at the hop of the work, or asks the person at the origin a decision.

All read the run from EVO_RUN_ID like ``worker chat`` (``chat_cli``) and are for a chat run only. The hub refuses
what is outside the frame, with the reason, and the command prints it on stderr and exits 1. Each success is one line
and the hub posts the ``Decision:`` line in the thread. TEXT ``-`` reads stdin. No ``--json``: this module may not
import the hub's contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from evo_agents.worker.chat_cli import EXIT_USAGE, agent_run, call, chat_command


def _text(value: str) -> str:
    text = sys.stdin.read() if value == "-" else value
    return text.strip() if value == "-" else text


def _options(args) -> dict:
    found = {
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


def _edits(value: str) -> list | str:
    """The steps of ``--edits``: the JSON list FILE holds (stdin for ``-``), or the line that says what is wrong."""
    try:
        text = sys.stdin.read() if value == "-" else Path(value).read_text(encoding="utf-8")
        edits = json.loads(text)
    except (OSError, ValueError) as exc:
        return f"--edits {value}: {exc}"
    if not isinstance(edits, list) or not edits or not all(isinstance(edit, dict) for edit in edits):
        return f'--edits {value}: give a JSON list of {{"step": ID, "verify": TEXT, "what": TEXT, "why": TEXT}}, one for each step'
    return [{**edit, "step": str(edit.get("step", ""))} for edit in edits]


@chat_command
def cmd_plan(args) -> int:
    stdin = [value for value in (args.edits, args.reason, args.verify, args.what) if value == "-"]
    if (args.step is None) == (args.edits is None) or (args.edits and (args.verify or args.what)) or len(stdin) > 1:
        print(
            "error: give STEP with --verify or --what, or --edits FILE for several steps, not both; - (stdin) once",
            file=sys.stderr,
        )
        return EXIT_USAGE
    run = agent_run("triage plan")
    body = {"reason": _text(args.reason), "if_revision": args.if_revision, "rerun": bool(args.rerun)}
    if args.edits is not None:
        edits = _edits(args.edits)
        if isinstance(edits, str):
            print(f"error: {edits}", file=sys.stderr)
            return EXIT_USAGE
        body["steps"] = edits
    else:
        body["step"] = args.step
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


@chat_command
def cmd_invite(args) -> int:
    run = agent_run("triage invite")
    answer = call(run, "triage/invite", {"handle": args.handle.lstrip("@"), "reason": _text(args.reason)})
    said = "was already in the chat" if not answer["added"] else "joined the chat"
    print(
        f"Agent {args.handle.lstrip('@')} of project {answer['project']} {said}; the Decision line is message "
        f"#{answer['message_id']}. Hand it work with `evo-agents worker chat mention`."
    )
    return 0


@chat_command
def cmd_cancel(args) -> int:
    run = agent_run("triage cancel")
    answer = call(run, "triage/cancel", {"reason": _text(args.reason)})
    print(
        f"Asked the worker of run #{answer['run_id']} to stop it; the Decision line is message #{answer['message_id']}. "
        "Run it again with `evo-agents worker triage rerun` once it has ended."
    )
    return 0


@chat_command
def cmd_merge_check(args) -> int:
    run = agent_run("triage merge-check")
    answer = call(run, "triage/merge-check", {"action": args.action, "reason": _text(args.reason)})
    done = (
        "The hub judges the merge check again; its verdict wakes the plan run"
        if args.action == "rejudge"
        else "The failure went back to the plan run"
    )
    print(
        f"{done} (check #{answer['check_id']}, {answer['state']}); the Decision line is message #{answer['message_id']}."
    )
    return 0


@chat_command
def cmd_answer(args) -> int:
    run = agent_run("triage answer")
    answer = call(run, "triage/answer", {"text": _text(args.text), "reason": _text(args.reason)})
    print(f"Answered: run #{answer['run_id']} has the answer; the Decision line is message #{answer['message_id']}.")
    return 0


@chat_command
def cmd_reassign(args) -> int:
    run = agent_run("triage reassign")
    body = {"handle": args.handle.lstrip("@"), "note": _text(args.note), "reason": _text(args.reason)}
    answer = call(run, "triage/reassign", body)
    print(
        f"Gave the work to @{body['handle']} in run #{answer['run_id']}; the Decision line is message "
        f"#{answer['message_id']}."
    )
    return 0


@chat_command
def cmd_ask_owner(args) -> int:
    from evo_agents.worker import agent_cli
    from evo_agents.worker.agent_input import decision_option
    from evo_agents.worker.home import WorkerHome

    run = agent_run("triage ask-owner")
    body = {
        "question": _text(args.question),
        "options": [decision_option(text) for text in args.option],
        "reason": _text(args.reason),
        **({"context": _text(args.context)} if args.context else {}),
    }
    answer = call(run, "triage/ask-owner", body)
    decision_id = agent_cli.note_decision(WorkerHome(), run.run_id, answer.get("decision_id"))
    print(
        f"Asked the owner decision #{decision_id}; the Decision line is message #{answer['message_id']}. End your turn: "
        "the answer comes back in this session."
    )
    return 0


def _register_orchestrator(tsub) -> None:
    answer = tsub.add_parser(
        "answer", help="as the Orchestrator: answer the thread (wakes the agent that escalated, or tells the agent)"
    )
    answer.add_argument("--text", required=True, metavar="TEXT", help="the answer (- for stdin)")
    answer.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line")
    answer.set_defaults(func=cmd_answer)
    reassign = tsub.add_parser(
        "reassign", help="as the Orchestrator: give the thread's work to another agent node of the system"
    )
    reassign.add_argument("handle", metavar="HANDLE", help="an agent node of the system, @ left out or not")
    reassign.add_argument("--note", required=True, metavar="TEXT", help="the work as that agent reads it (- for stdin)")
    reassign.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line")
    reassign.set_defaults(func=cmd_reassign)
    ask = tsub.add_parser("ask-owner", help="as the Orchestrator: ask the person at the origin of the work a decision")
    ask.add_argument("--question", required=True, metavar="TEXT", help="the question (- for stdin)")
    ask.add_argument(
        "--option", action="append", required=True, metavar="KEY=LABEL[:DESCRIPTION]", help="2 to 6 options; repeat"
    )
    ask.add_argument("--context", metavar="TEXT", help="markdown the person reads with the question")
    ask.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line")
    ask.set_defaults(func=cmd_ask_owner)


def _frame_options(parser) -> None:
    parser.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
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
    cancel = tsub.add_parser("cancel", help="in a stale run's thread: ask its worker to stop it, for its owner")
    cancel.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
    cancel.set_defaults(func=cmd_cancel)
    invite = tsub.add_parser(
        "invite",
        help="invite the agent of another project the failed run's error names into this chat (the owner needs the "
        "writer role there and the chat's label must hold)",
    )
    invite.add_argument("handle", metavar="HANDLE", help="the agent's handle, @ left out")
    invite.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
    invite.set_defaults(func=cmd_invite)
    plan = tsub.add_parser(
        "plan",
        help="change the verify or narrow the what of pending steps of the failed run's plan, one step or several at "
        "once (--edits), in one revision",
    )
    plan.add_argument("step", nargs="?", metavar="STEP", help="the step's id, a step that is pending")
    plan.add_argument(
        "--edits",
        metavar="FILE",
        help='several steps in one revision and one Decision line: a JSON list of {"step": ID, "verify": TEXT, "what": '
        'TEXT, "why": TEXT} (- for stdin), instead of STEP, --verify and --what',
    )
    plan.add_argument("--if-revision", type=int, required=True, metavar="REVISION", help="the revision it was read at")
    plan.add_argument(
        "--verify", metavar="TEXT", help="the new verify, no command fewer than the old one (- for stdin)"
    )
    plan.add_argument("--what", metavar="TEXT", help="the new what, shorter than the old one (- for stdin)")
    plan.add_argument("--rerun", action="store_true", help="run the failed step again once the plan is written")
    _frame_options(plan)
    plan.set_defaults(func=cmd_plan)
    check = tsub.add_parser(
        "merge-check",
        help="in a held merge check's thread: judge it again once the system is fixed, or hand the failure back to the "
        "plan run",
    )
    check.add_argument(
        "action",
        choices=("rejudge", "return"),
        help="rejudge: judge it again from its first Judge; return: the pull request caused it after all",
    )
    check.add_argument("--reason", required=True, metavar="TEXT", help="why, for the Decision line (- for stdin)")
    check.set_defaults(func=cmd_merge_check)
    _register_orchestrator(tsub)

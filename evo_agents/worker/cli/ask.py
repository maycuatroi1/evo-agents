"""``evo-agents worker ask``: the agent of a plan run or an author run asks the run's owner a decision
(POST /v1/worker/runs/{id}/decisions), noted in the run's file of decisions so the run waits for the answer. Its
options, credential request and approval are checked first (``evo_agents.worker.run_decisions``).
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub import credential_requests, decision_asks, runs
from evo_agents.worker import output_files
from evo_agents.worker.agent_input import decision_extras, read_text
from evo_agents.worker.cli.base import AgentCommand, AgentRunContext
from evo_agents.worker.cli.output import attach_arg
from evo_agents.worker.home import WorkerStateError
from evo_agents.worker.run_decisions import asked_approval, asked_options, credential_request, noted_decision

NO_RECOMMENDED = (
    "warning: no --recommended: the person may answer on a phone with one tap when you recommend an option "
    "(--recommended KEY) and say why (--recommended-reason TEXT). The decision is asked without one."
)


def ask_body(ctx: AgentRunContext, request: dict | None) -> dict:
    """The decision ``worker ask`` asks: its question, options and what else it carries, checked."""
    args = ctx.args
    if request is None:
        options = asked_options(args)
        keys = [option["key"] for option in options]
    else:  # the hub gives it these options
        options = []
        given = credential_requests.options(request.get("env_var"), request.get("secret_name"), request["hub_account"])
        keys = [option["key"] for option in given]
    if args.recommended is not None and args.recommended not in keys:
        raise WorkerStateError(f"--recommended names one of the options: {', '.join(keys)}")
    body: dict = {"category": args.category, "question": args.question, "options": options}
    if request is not None:
        body["credential_request"] = request
    context = read_text(args.context_file, "context", runs.MAX_DECISION_CONTEXT_BYTES) if args.context_file else None
    if context is not None:
        body["context"] = context
    if args.recommended is not None:
        body["recommended"] = args.recommended
    body.update(decision_extras(args.recommended, args.recommended_reason, args.reversible, args.undo, args.evidence))
    if args.step is not None:
        body["step_key"] = args.step
    if getattr(args, "pull", None) is not None:
        body["approval"] = asked_approval(args.category, args.pull)
    if getattr(args, "label", None) is not None:
        body["label"] = args.label
    body.update(output_files.attached(ctx.home, ctx.run_id, args.attach))  # the outputs the question carries
    return body


def asked_lines(answer: dict, decision_id: int, question: str, request: dict | None, kind: str) -> list[str]:
    """What ``worker ask`` prints once the hub took the decision."""
    if request is not None and answer.get("state") == "answered":  # the hub found the owner's secret and answered
        return [
            f"Decision #{decision_id} was answered by the hub at once: {answer.get('answer_text') or ''}".rstrip(),
            "The worker takes the run's leases again as the answer reaches the run: read them with "
            f"`{credential_requests.ENV_COMMAND}` before the command that needs them; nothing waits on the owner.",
        ]
    lines = [f"Decision #{decision_id} is open: {' '.join(question.split())}"]
    if request is not None:
        lines.append(
            "Once the owner gives what it asks for, the worker takes the run's leases again: read them with "
            f"`{credential_requests.ENV_COMMAND}` before the command that needs them."
        )
    if kind == "author":
        lines.append(
            "Ask the other decisions of this turn now, then end your turn with your message to the member in the chat: "
            "what you found, what you decided yourself and the decisions you asked. The run waits until the member "
            "answered each one or replied in the chat, and your next turn in this session takes the answers, each as a "
            f"message naming its decision, #{decision_id} among them."
        )
        return lines
    lines.append(
        "Go on with the work that does not depend on the answer. When none is left, end your turn: the run waits "
        f"for its owner, and the answer comes back in this session as a message naming decision #{decision_id}."
    )
    return lines


class Ask(AgentCommand):
    name = "ask"
    help = "inside a plan run or an author run: ask the run's owner a decision; prints its id"
    kinds = runs.RESUME_KINDS
    arguments = (
        Arg("--category", required=True, choices=runs.ASKED_DECISION_CATEGORIES, help="what kind of decision"),
        Arg("--question", required=True, help="the question, in a sentence or two"),
        Arg("--context-file", help="a markdown file of context for the owner, at most 16 KiB (- for stdin)"),
        Arg(
            "--option",
            action="append",
            metavar="KEY=LABEL[:DESCRIPTION]",
            help="an option; repeat it, 2 to 6 times; none for a credential request, which takes the hub's",
        ),
        Arg(
            "--env-var",
            metavar="NAME",
            help="with --category credential: the variable of your environment the run needs, such as an API key; "
            "the owner pastes its value, which you then read with `evo-agents worker env --run N`",
        ),
        Arg(
            "--secret-name",
            metavar="NAME",
            help="with --env-var: the name of the owner's secret that keeps it (default: the variable in lower case, "
            "- for _)",
        ),
        Arg(
            "--hub-account",
            action="store_true",
            help="with --category credential: the owner's account on the hub, so `evo-agents hub ...` runs as them",
        ),
        Arg("--recommended", metavar="KEY", help="the key of the option you recommend"),
        Arg(
            "--recommended-reason",
            metavar="TEXT",
            help=f"why you recommend that option, at most {decision_asks.MAX_REASON_CHARS} characters; needs "
            "--recommended",
        ),
        Arg("--reversible", choices=("yes", "no"), help="for an act: whether it can be undone (--undo says yes)"),
        Arg("--undo", metavar="TEXT", help=f"how to undo the act, at most {decision_asks.MAX_UNDO_CHARS} characters"),
        Arg(
            "--evidence",
            action="append",
            metavar="REF",
            help=f"what the question rests on, repeat it (at most {decision_asks.MAX_EVIDENCE}): {decision_asks.FORMS}",
        ),
        Arg(
            "--wait-option",
            action="append",
            metavar="KEY",
            help="an option that says not yet: choosing it leaves the decision open and the run waiting, and you hear "
            "nothing; repeat it for another",
        ),
        Arg("--step", help="the step of the run's plan it is about (an author run: once it put the plan)"),
        Arg(
            "--pull",
            metavar="REF",
            help="with --category merge: the pull request to merge (number, URL or branch); the answer holds for its "
            "head and scope now alone, and a push that changes them asks again",
        ),
        Arg(
            "--label",
            metavar="LEVEL",
            help="the level of the project's ladder the question, its options and its context sit at, such as "
            "internal, at most what the run reaches; the channels send the decision by it, and left out it takes the "
            "project's default label",
        ),
        attach_arg("the question"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        ctx.check()
        request = credential_request(ctx.args)
        body = ask_body(ctx, request)
        if ctx.args.recommended is None:
            print(NO_RECOMMENDED, file=sys.stderr)

        async def work(hub) -> dict:
            return await hub.decision(ctx.run_id, body)

        answer = ctx.with_hub(work)
        decision_id = noted_decision(ctx.home, ctx.run_id, answer, credential=request is not None)
        return Result(answer, Text(*asked_lines(answer, decision_id, ctx.args.question, request, ctx.kind)))


ASK = Ask()

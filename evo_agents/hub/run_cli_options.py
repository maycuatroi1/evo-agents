"""Options of ``evo-agents hub run`` that ``evo_agents.hub.run_cli`` registers through these functions: where, how and
how long a plan run or an author run runs, and the account a run takes.

``--account NAME`` on ``dispatch``, ``plan``, ``author`` and ``rerun`` picks one of your env secrets bound to the
project, by name (``evo-agents hub account list``): the run takes it for its variable and keeps it, with no failover
to another account; without it the rules of your policy for the project pick (``docs/credentials.md``, section
"Accounts"). A rerun keeps the account picked by hand for the run it reruns, unless ``--account`` names another one or
``--account-by-rule`` asks the rules. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.hub.author import AUTHOR_RUNTIMES, AUTHOR_TIMEOUT_CHOICES, DEFAULT_TIMEOUT_H
from evo_agents.hub.runs import MODES, PLAN_TIMEOUT_CHOICES, RUNTIMES

REQUESTED_RUNTIMES = ("any", *RUNTIMES)
ACCOUNT_HELP = (
    "one of your env secrets bound to the project, by name, as `hub account list` shows it: the run takes it and "
    "keeps it, with no failover (default: the rules of your policy for the project pick)"
)


def account_option(parser, help_text: str = ACCOUNT_HELP) -> None:
    parser.add_argument("--account", metavar="NAME", help=help_text)


def rerun_options(parser) -> None:
    """The options of `hub run rerun` after its run and --project: the account the rerun takes."""
    group = parser.add_mutually_exclusive_group()
    account_option(
        group,
        "one of your env secrets bound to the project, by name: the rerun takes it, with no failover (default: the "
        "account picked by hand for the run rerun, if one was, else the rules of your policy pick)",
    )
    group.add_argument(
        "--account-by-rule",
        action="store_true",
        help="let the rules of your policy pick the account, not keep the one picked by hand for the run rerun",
    )


def rerun_body(args) -> dict | None:
    """The body of a rerun: none when it keeps everything, so an older hub, which takes none, still takes it."""
    if getattr(args, "account", None):
        return {"account": args.account}
    if getattr(args, "account_by_rule", False):
        return {"account_by_rule": True}
    return None


def plan_options(plan, with_model) -> None:
    """The options of `hub run plan` after its plan and --project: where, how and how long the plan run runs."""
    plan.add_argument(
        "--worker", help="pin the run to this worker of yours, by id or name (default: any of yours with every repo)"
    )
    plan.add_argument("--runtime", choices=REQUESTED_RUNTIMES, help="default: any, the first the claiming worker has")
    with_model(plan)
    plan.add_argument(
        "--mode", choices=MODES, help="default: headless; interactive starts the agent in a terminal on the worker"
    )
    plan.add_argument(
        "--timeout-h",
        type=int,
        choices=PLAN_TIMEOUT_CHOICES,
        metavar="HOURS",
        help=f"hours of agent time, one of {', '.join(map(str, PLAN_TIMEOUT_CHOICES))} (default 4); waiting for a "
        "decision and parked do not count",
    )
    plan.add_argument(
        "--session-per-step",
        action="store_true",
        help="start a new agent session for each step, its prompt the step's with the handoff of the plan, rather "
        "than one session for every step; a worker takes it once its daemon says it runs one",
    )
    account_option(plan)


def author_options(authored) -> None:
    """The options of `hub run author` after its request and --project."""
    authored.add_argument(
        "--plan",
        metavar="PLAN",
        help="the plan to revise, one you can read; the agent puts it back with its revision (default: a new plan)",
    )
    authored.add_argument(
        "--worker",
        required=True,
        help="the worker of yours to run it on, by id or name; it needs the harness checked out",
    )
    authored.add_argument(
        "--runtime",
        choices=REQUESTED_RUNTIMES,
        help=f"default: {AUTHOR_RUNTIMES[0]}, the one runtime an author run takes; the hub refuses another",
    )
    authored.add_argument("--model", help="the model, as Claude Code names it (default: Claude Code's own choice)")
    authored.add_argument(
        "--timeout-h",
        type=int,
        choices=AUTHOR_TIMEOUT_CHOICES,
        metavar="HOURS",
        help=f"hours of agent time, one of {', '.join(map(str, AUTHOR_TIMEOUT_CHOICES))} (default {DEFAULT_TIMEOUT_H})",
    )
    account_option(authored)

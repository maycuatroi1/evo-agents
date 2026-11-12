"""Options of ``evo-agents hub run`` that ``evo_agents.hub.run_cli`` registers through these functions: where, how and
how long a step run, a plan run or an author run runs, the account a run takes and the agent profile it goes under,
and the filters of ``run list``; and how a command refuses options that do not go together (``usage_error``).

``--account NAME`` on ``dispatch``, ``plan``, ``author`` and ``rerun`` picks one of your env secrets bound to the
project, by name (``evo-agents hub account list``): the run takes it for its variable and keeps it, with no failover
to another account; without it the rules of your policy for the project pick (``docs/credentials.md``, section
"Accounts"). A rerun keeps the account picked by hand for the run it reruns, unless ``--account`` names another one or
``--account-by-rule`` asks the rules.

``--agent HANDLE`` on ``dispatch``, ``plan`` and ``author`` names the agent profile the run goes under (``evo-agents
hub agent list``): the hub refuses a dispatch its allowlists leave out, and without it the run goes under the project's
default profile, which allows everything. A rerun keeps the profile of the run it reruns.

``--runtime`` on ``dispatch`` and ``plan`` takes any (the claiming worker picks a runtime of the core it has), a
runtime of the core, or ``<plugin>/<name>``, a runtime a plugin enabled on the project declares (``docs/plugins.md``,
section Runtimes); the hub refuses one the project does not register.

``--browser`` on ``dispatch``, ``plan`` and ``rerun`` gives the run's agent a Chromium of its own on its worker, which
the worker starts before the agent's first turn and closes when the run ends there: only a worker that ran
``evo-agents worker browser install`` takes it. ``--identity NAME`` on ``dispatch`` and ``plan``, once for each, opens
that Chromium with browser identities (``evo-agents hub browser identity list``), which implies ``--browser``: the run
goes to the worker that holds them and waits while another run uses one. A rerun keeps the browser of the run it
reruns, and its identities.

``--as-me`` on ``dispatch`` and ``plan`` lets the run's agent call the hub's API as you, secrets, tokens and workers
aside, through a token of the run's own that ends with it (``docs/credentials.md``, "A run that runs as its owner"); a
rerun and the run that resumes a parked one keep the choice, and ``hub run access`` changes it afterwards
(``run_cli_access``).

``--system SLUG`` on ``plan`` hands the plan to an agent system of the project rather than queueing a plan run: its
entry agent gets the plan in your direct chat with it and shares the steps out (POST .../plans/{id}/system-dispatch).
The system decides how its agents run, so none of the plan run's options goes with it (SYSTEM_REFUSES). Standard
library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import re
import sys

from evo_agents.hub.author import AUTHOR_RUNTIMES, AUTHOR_TIMEOUT_CHOICES, DEFAULT_TIMEOUT_H
from evo_agents.hub.runs import (
    APPROVALS,
    MODES,
    PLAN_TIMEOUT_CHOICES,
    RUNTIME_NAME,
    RUNTIMES,
    SHOWN_KINDS,
    SHOWN_STATES,
)

REQUESTED_RUNTIMES = ("any", *RUNTIMES)
RUNTIME_HELP = (
    f"any (the default: the first runtime of the core the claiming worker has), {', '.join(RUNTIMES)}, or "
    "<plugin>/<name>, a runtime a plugin enabled on the project declares"
)
EXIT_USAGE = 2
ACCOUNT_HELP = (
    "one of your env secrets bound to the project, by name, as `hub account list` shows it: the run takes it and "
    "keeps it, with no failover (default: the rules of your policy for the project pick)"
)


KIND_TEXTS = {  # what `run show` says a run of each kind but step and plan is
    "review": "review run: the Curator reads the project and proposes changes; it pushes nothing",
    "judge": "judge run: the Curator's Judge reads a change of its plan and says whether it passes; it pushes nothing",
    "author": "author run: a plan written from your request with create-exec-plan; it pushes nothing",
    "chat": "chat run: one turn of an agent mentioned in a group chat, on the branch of its session",
    "task": "task run: one turn of a plugin's agent type on a task the plugin handed it, on the branch task/<run>; "
    "it pushes that branch and the hub opens a draft pull request, never merged",
    "local": "local run: a Claude Code session on a member's own machine, which the hub only watches",
    "command": "command run: a cron job's shell command line, run by the machine's shell with no agent and no model; "
    "exit 0 ends it done, any other code failed",
}
QUEUE_JUDGE_TEXT = (  # a judge run on no plan
    "judge run: a Judge of the merge queue reads a merge request and says whether it may merge; it pushes nothing"
)


def kind_text(run: dict) -> str | None:
    """What `run show` says ``run`` is, by its kind: a judge run on no plan is a Judge of the merge queue; None for a
    step or plan run."""
    if run["kind"] == "judge" and not run.get("plan_id"):
        return QUEUE_JUDGE_TEXT
    return KIND_TEXTS.get(run["kind"])


AGENT_HELP = (
    "the agent profile the run goes under, by handle, as `hub agent list` shows it; the hub refuses what it does not "
    "allow (default: the project's default agent, which allows everything)"
)


def runtime_name(value: str) -> str:
    """``value`` as ``--runtime`` takes it: any, a runtime of the core, or a plugin's ``<plugin>/<name>``."""
    if re.fullmatch(RUNTIME_NAME, value):
        return value
    raise argparse.ArgumentTypeError(f"{value!r} is not any, one of {', '.join(RUNTIMES)}, or <plugin>/<name>")


def account_option(parser, help_text: str = ACCOUNT_HELP, *, agent: bool = True) -> None:
    """``--account``, and with ``agent`` ``--agent``: what a dispatched run takes and goes under."""
    parser.add_argument("--account", metavar="NAME", help=help_text)
    if agent:
        parser.add_argument("--agent", metavar="HANDLE", help=AGENT_HELP)


BROWSER_HELP = (
    "give the agent a Chromium of its own on its worker, whose DevTools endpoint it reads in EVO_BROWSER_CDP; only a "
    "worker that ran `evo-agents worker browser install` takes the run"
)


IDENTITY_HELP = (
    "open the run's Chromium logged in as this browser identity of yours, else of the project's (`evo-agents hub "
    "browser identity list`); once for each, the first one's profile with the others' cookies; implies --browser"
)


def browser_option(parser, *, identities: bool = False) -> None:
    """``--browser``: the run's agent gets a Chromium of its own; with ``identities``, ``--identity`` too."""
    parser.add_argument("--browser", action="store_true", help=BROWSER_HELP)
    if identities:
        parser.add_argument("--identity", metavar="NAME", action="append", dest="identities", help=IDENTITY_HELP)


def browser_body(args) -> dict:
    """What a dispatch, a plan run or a rerun sends of ``--browser`` and ``--identity``: only when asked, so an older
    hub takes the rest."""
    body = {"browser": True} if getattr(args, "browser", False) else {}
    if getattr(args, "identities", None):
        body["identities"] = list(args.identities)
    return body


AS_ME_HELP = (
    "let the run's agent call the hub's API as you, with your grants and hub admin if you are one, but never your "
    "secrets, tokens or workers, through a token of the run's own that ends with the run; a step, plan or author run "
    "only, and `hub run access RUN --revoke` stops it"
)


def as_me_option(parser) -> None:
    """``--as-me``: the run runs as you (``AS_ME_HELP``)."""
    parser.add_argument("--as-me", action="store_true", help=AS_ME_HELP)


def as_me_body(args) -> dict:
    """What a dispatch or a plan run sends of ``--as-me``: only when asked, so an older hub takes the rest."""
    return {"as_me": True} if getattr(args, "as_me", False) else {}


def usage_error(text: str) -> int:
    """Say on stderr what is wrong with the options; the exit status of a usage error."""
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def rerun_options(parser) -> None:
    """The options of `hub run rerun` after its run and --project: the account the rerun takes."""
    group = parser.add_mutually_exclusive_group()
    account_option(
        group,
        "one of your env secrets bound to the project, by name: the rerun takes it, with no failover (default: the "
        "account picked by hand for the run rerun, if one was, else the rules of your policy pick)",
        agent=False,
    )
    group.add_argument(
        "--account-by-rule",
        action="store_true",
        help="let the rules of your policy pick the account, not keep the one picked by hand for the run rerun",
    )
    browser_option(parser)


def rerun_body(args) -> dict | None:
    """The body of a rerun: none when it keeps everything, so an older hub, which takes none, still takes it."""
    body = browser_body(args)
    if getattr(args, "account", None):
        body["account"] = args.account
    elif getattr(args, "account_by_rule", False):
        body["account_by_rule"] = True
    return body or None


def dispatch_options(dispatch) -> None:
    """The options of `hub run dispatch` after its plan, steps and --project: where, how and how long each run runs."""
    dispatch.add_argument("--runtime", type=runtime_name, metavar="RUNTIME", help=RUNTIME_HELP)
    dispatch.add_argument(
        "--mode", choices=MODES, help="default: headless; interactive starts the agent in a terminal on the worker"
    )
    dispatch.add_argument(
        "--approval",
        choices=APPROVALS,
        help="default: review, which waits for `hub run approve`; auto marks the step done once every verify command "
        "the worker runs again exits 0",
    )
    dispatch.add_argument("--timeout", type=int, choices=range(5, 241), metavar="MINUTES", help="5 to 240 (default 60)")
    browser_option(dispatch, identities=True)
    as_me_option(dispatch)


def list_options(listed) -> None:
    """The filters and the page of `hub run list` after its --project."""
    listed.add_argument(
        "--state",
        action="append",
        choices=SHOWN_STATES,
        metavar="STATE",
        help=f"only runs in this state, one of {', '.join(SHOWN_STATES)}; repeat it for several",
    )
    listed.add_argument(
        "--kind", choices=SHOWN_KINDS, metavar="KIND", help=f"only runs of this kind, one of {', '.join(SHOWN_KINDS)}"
    )
    listed.add_argument("--account", metavar="EMAIL", help="only local runs of this Claude account")
    listed.add_argument("--plan", help="only runs of this plan")
    listed.add_argument("--step", help="only runs of this step key")
    listed.add_argument(
        "--worker", help="only runs this worker of yours, or one shared with you, claimed, by id, name or owner/name"
    )
    listed.add_argument("--by", metavar="LOGIN", help="only runs this member dispatched")
    listed.add_argument(
        "--search",
        metavar="TEXT",
        help="text in the title, step, plan, repo, branch, worker, login, error, machine, directory or account, or a "
        "run number such as #12",
    )
    listed.add_argument("--limit", type=int, choices=range(1, 201), metavar="N", help="1 to 200 (default 50)")
    listed.add_argument("--offset", type=int, metavar="N", help="skip this many runs, for the next page")


SYSTEM_HELP = (
    "hand the plan to this agent system of the project, by slug, rather than queue a plan run: its entry agent gets "
    "the plan in your direct chat with it, opens a trace and shares the steps out; no other option of a plan run goes "
    "with it"
)
# the options of a plan run, flag and attribute, that `hub run plan --system` refuses: the system runs its agents
SYSTEM_REFUSES = (
    ("--runtime", "runtime"),
    ("--model", "model"),
    ("--mode", "mode"),
    ("--timeout-h", "timeout_h"),
    ("--session-per-step", "session_per_step"),
    ("--browser", "browser"),
    ("--identity", "identities"),
    ("--account", "account"),
    ("--agent", "agent"),
    ("--as-me", "as_me"),
)


def system_conflicts(args) -> list[str]:
    """The options of a plan run given together with ``--system``, as flags; empty without ``--system``."""
    if getattr(args, "system", None) is None:
        return []
    return [flag for flag, name in SYSTEM_REFUSES if getattr(args, name, None) not in (None, False)]


def plan_options(plan, with_model) -> None:
    """The options of `hub run plan` after its plan and --project: where, how and how long the plan run runs."""
    plan.add_argument("--runtime", type=runtime_name, metavar="RUNTIME", help=RUNTIME_HELP)
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
        "than one session for every step, and its steps run one after another, never two at once, where one session "
        "starts the ready steps together; a worker takes it once its daemon says it runs one",
    )
    browser_option(plan, identities=True)
    account_option(plan)
    as_me_option(plan)
    plan.add_argument("--system", metavar="SLUG", help=SYSTEM_HELP)


def author_options(authored) -> None:
    """The options of `hub run author` after its request and --project."""
    authored.add_argument(
        "--plan",
        metavar="PLAN",
        help="the plan to revise, one you can read; the agent puts it back with its revision (default: a new plan)",
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

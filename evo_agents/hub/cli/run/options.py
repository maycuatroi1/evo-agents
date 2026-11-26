"""Options of ``evo-agents hub run``, as the arguments (``Arg``) the commands of ``evo_agents.hub.cli.run`` take:
where, how and how long a step run, a plan run or an author run runs, the account a run takes and the agent profile
it goes under, and the filters of ``run list``. :class:`RunOptions` is the value object that turns them into the body
of a dispatch, a plan run, an author run or a rerun, and refuses (``UsageError``) options that do not go together.

``--account NAME`` on ``dispatch``, ``plan``, ``author`` and ``rerun`` picks one of your env secrets bound to the
project, by name (``evo-agents hub account list``), or ``LOGIN/NAME`` one that member shares with you: the run takes it
for its variable and keeps it, with no failover to another account; without it the rules of your policy for the project pick (``docs/credentials.md``, section
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
(``evo_agents.hub.cli.run.access``).

``--system SLUG`` on ``plan`` hands the plan to an agent system of the project rather than queueing a plan run: its
entry agent gets the plan in your direct chat with it and shares the steps out (POST .../plans/{id}/system-dispatch).
The system decides how its agents run, so none of the plan run's options goes with it (SYSTEM_REFUSES). Standard
library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from evo_agents.clikit import Arg, UsageError
from evo_agents.hub.author import AUTHOR_RUNTIMES, AUTHOR_TIMEOUT_CHOICES, DEFAULT_TIMEOUT_H, MAX_REQUEST_BYTES
from evo_agents.hub.plan_writes import checked_plan_id
from evo_agents.hub.runs import (
    APPROVALS,
    MAX_MODEL_CHARS,
    MODES,
    PLAN_TIMEOUT_CHOICES,
    RUNTIMES,
    SHOWN_KINDS,
    SHOWN_STATES,
)
from evo_agents.hub.runs import runtime_name as runtime_name  # the type of --runtime, which the contract knows

REQUESTED_RUNTIMES = ("any", *RUNTIMES)
RUNTIME_HELP = (
    f"any (the default: the first runtime of the core the claiming worker has), {', '.join(RUNTIMES)}, or "
    "<plugin>/<name>, a runtime a plugin enabled on the project declares"
)
ACCOUNT_HELP = (
    "one of your env secrets bound to the project, by name, as `hub account list` shows it, or LOGIN/NAME for one "
    "that member shares with you: the run takes it and keeps it, with no failover (default: the rules of your policy "
    "for the project pick)"
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
BROWSER_HELP = (
    "give the agent a Chromium of its own on its worker, whose DevTools endpoint it reads in EVO_BROWSER_CDP; only a "
    "worker that ran `evo-agents worker browser install` takes the run"
)
IDENTITY_HELP = (
    "open the run's Chromium logged in as this browser identity of yours, else of the project's (`evo-agents hub "
    "browser identity list`); once for each, the first one's profile with the others' cookies; implies --browser"
)
AS_ME_HELP = (
    "let the run's agent call the hub's API as you, with your grants and hub admin if you are one, but never your "
    "secrets, tokens or workers, through a token of the run's own that ends with the run; a step, plan or author run "
    "only, and `hub run access RUN --revoke` stops it"
)
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

# The arguments

RUN_ID = Arg("run", metavar="RUN", type=int, help="the run's id, as `hub run list` shows it")
PLAN_ID = Arg("plan", metavar="PLAN", help="the plan's id")
RUNTIME = Arg("--runtime", type=runtime_name, metavar="RUNTIME", help=RUNTIME_HELP)
MODEL = Arg(
    "--model",
    help="the model, as the runtime names it (opencode: provider/model); needs --runtime with that runtime "
    "(default: the runtime's own choice)",
)
MODE = Arg("--mode", choices=MODES, help="default: headless; interactive starts the agent in a terminal on the worker")
APPROVAL = Arg(
    "--approval",
    choices=APPROVALS,
    help="default: review, which waits for `hub run approve`; auto marks the step done once every verify command "
    "the worker runs again exits 0",
)
TIMEOUT = Arg("--timeout", type=int, choices=range(5, 241), metavar="MINUTES", help="5 to 240 (default 60)")
PLAN_TIMEOUT_H = Arg(
    "--timeout-h",
    type=int,
    choices=PLAN_TIMEOUT_CHOICES,
    metavar="HOURS",
    help=f"hours of agent time, one of {', '.join(map(str, PLAN_TIMEOUT_CHOICES))} (default 4); waiting for a "
    "decision and parked do not count",
)
SESSION_PER_STEP = Arg(
    "--session-per-step",
    action="store_true",
    help="start a new agent session for each step, its prompt the step's with the handoff of the plan, rather "
    "than one session for every step, and its steps run one after another, never two at once, where one session "
    "starts the ready steps together; a worker takes it once its daemon says it runs one",
)
BROWSER = Arg("--browser", action="store_true", help=BROWSER_HELP)
IDENTITY = Arg("--identity", metavar="NAME", action="append", dest="identities", help=IDENTITY_HELP)
AS_ME = Arg("--as-me", action="store_true", help=AS_ME_HELP)
ACCOUNT = Arg("--account", metavar="NAME", help=ACCOUNT_HELP)
AGENT = Arg("--agent", metavar="HANDLE", help=AGENT_HELP)
SYSTEM = Arg("--system", metavar="SLUG", help=SYSTEM_HELP)

# after the plan, the steps and --project of `hub run dispatch`: where, how and how long each run runs
DISPATCH_ARGS = (RUNTIME, MODE, APPROVAL, TIMEOUT, BROWSER, IDENTITY, AS_ME, MODEL, ACCOUNT, AGENT)
# after the plan and --project of `hub run plan`: where, how and how long the plan run runs
PLAN_ARGS = (RUNTIME, MODEL, MODE, PLAN_TIMEOUT_H, SESSION_PER_STEP, BROWSER, IDENTITY, ACCOUNT, AGENT, AS_ME, SYSTEM)
# after the request and --project of `hub run author`
AUTHOR_ARGS = (
    Arg(
        "--plan",
        metavar="PLAN",
        help="the plan to revise, one you can read; the agent puts it back with its revision (default: a new plan)",
    ),
    Arg(
        "--runtime",
        choices=REQUESTED_RUNTIMES,
        help=f"default: {AUTHOR_RUNTIMES[0]}, the one runtime an author run takes; the hub refuses another",
    ),
    Arg("--model", help="the model, as Claude Code names it (default: Claude Code's own choice)"),
    Arg(
        "--timeout-h",
        type=int,
        choices=AUTHOR_TIMEOUT_CHOICES,
        metavar="HOURS",
        help=f"hours of agent time, one of {', '.join(map(str, AUTHOR_TIMEOUT_CHOICES))} (default {DEFAULT_TIMEOUT_H})",
    ),
    ACCOUNT,
    AGENT,
)
# the filters and the page of `hub run list` after its --project
LIST_ARGS = (
    Arg(
        "--state",
        action="append",
        choices=SHOWN_STATES,
        metavar="STATE",
        help=f"only runs in this state, one of {', '.join(SHOWN_STATES)}; repeat it for several",
    ),
    Arg("--kind", choices=SHOWN_KINDS, metavar="KIND", help=f"only runs of this kind, one of {', '.join(SHOWN_KINDS)}"),
    Arg("--account", metavar="EMAIL", help="only local runs of this Claude account"),
    Arg("--plan", help="only runs of this plan"),
    Arg("--step", help="only runs of this step key"),
    Arg("--worker", help="only runs this worker of yours, or one shared with you, claimed, by id, name or owner/name"),
    Arg("--by", metavar="LOGIN", help="only runs this member dispatched"),
    Arg(
        "--search",
        metavar="TEXT",
        help="text in the title, step, plan, repo, branch, worker, login, error, machine, directory or account, or a "
        "run number such as #12",
    ),
    Arg("--limit", type=int, choices=range(1, 201), metavar="N", help="1 to 200 (default 50)"),
    Arg("--offset", type=int, metavar="N", help="skip this many runs, for the next page"),
)
RERUN_ACCOUNT = Arg(
    "--account",
    metavar="NAME",
    help="one of your env secrets bound to the project, by name: the rerun takes it, with no failover (default: the "
    "account picked by hand for the run rerun, if one was, else the rules of your policy pick)",
)
ACCOUNT_BY_RULE = Arg(
    "--account-by-rule",
    action="store_true",
    help="let the rules of your policy pick the account, not keep the one picked by hand for the run rerun",
)


def rerun_options(parser: argparse.ArgumentParser) -> None:
    """The options of `hub run rerun` after its run and --project: the account the rerun takes, and --browser."""
    group = parser.add_mutually_exclusive_group()
    for arg in (RERUN_ACCOUNT, ACCOUNT_BY_RULE):
        group.add_argument(*arg.flags, **arg.options)
    BROWSER.add_to(parser)


# What the options send


def given(values: dict) -> dict:
    """``values`` without those left out (None)."""
    return {key: value for key, value in values.items() if value is not None}


@dataclass(frozen=True)
class RunOptions:
    """The options of a command that queues a run, as the body the hub takes (Value Object). Each body sends an
    option only when it is given, so an older hub takes the rest; a check raises UsageError for options that do not
    go together."""

    args: argparse.Namespace

    def check_model(self) -> None:
        """Refuse ``--model`` without the ``--runtime`` that names it, or one that is not one short line."""
        args = self.args
        if args.model is None:
            return
        if args.runtime in (None, "any"):
            raise UsageError(
                "--model names a model as one runtime names it, so it needs --runtime with that runtime "
                f"({', '.join(RUNTIMES)}): with any, the claiming worker picks the runtime"
            )
        if not args.model.strip() or len(args.model) > MAX_MODEL_CHARS or not args.model.isprintable():
            raise UsageError(f"--model takes one line of 1 to {MAX_MODEL_CHARS} characters")

    def system_conflicts(self) -> list[str]:
        """The options of a plan run given together with ``--system``, as flags; empty without ``--system``."""
        if getattr(self.args, "system", None) is None:
            return []
        return [flag for flag, name in SYSTEM_REFUSES if getattr(self.args, name, None) not in (None, False)]

    def check_system(self) -> None:
        """Refuse an option of a plan run given with ``--system``."""
        conflicts = self.system_conflicts()
        if conflicts:
            raise UsageError(
                f"--system hands the plan to the system, whose agents run as it sets them, so it does not go with "
                f"{', '.join(conflicts)}"
            )

    def browser(self) -> dict:
        """What a dispatch, a plan run or a rerun sends of ``--browser`` and ``--identity``."""
        body: dict = {"browser": True} if getattr(self.args, "browser", False) else {}
        if getattr(self.args, "identities", None):
            body["identities"] = list(self.args.identities)
        return body

    def as_me(self) -> dict:
        """What a dispatch or a plan run sends of ``--as-me``."""
        return {"as_me": True} if getattr(self.args, "as_me", False) else {}

    def dispatch(self) -> dict:
        """The body of POST .../runs: the plan, its steps, and how each run runs."""
        args = self.args
        body = {"plan_id": checked_plan_id(args.plan), "steps": args.steps, **self.browser(), **self.as_me()}
        chosen = {
            "runtime": args.runtime,
            "model": args.model,
            "mode": args.mode,
            "approval": args.approval,
            "timeout_min": args.timeout,
            "account": args.account,
            "profile": args.agent,
        }
        return body | given(chosen)

    def plan(self) -> dict:
        """The body of POST .../plan-runs; ``session_per_step`` only when asked, as an older hub would ignore it."""
        args = self.args
        body = {"plan_id": checked_plan_id(args.plan), **self.browser(), **self.as_me()}
        chosen = {"runtime": args.runtime, "model": args.model, "mode": args.mode, "timeout_h": args.timeout_h}
        chosen.update(account=args.account, profile=args.agent)
        return body | given({**chosen, "session_per_step": True if args.session_per_step else None})

    def request(self) -> str:
        """The request of an author run, read from stdin for ``-``; UsageError for one empty or too long."""
        text = sys.stdin.read() if self.args.request == "-" else self.args.request
        if not text.strip():
            raise UsageError("the request is empty: say what the plan should achieve")
        if len(text.encode()) > MAX_REQUEST_BYTES:
            raise UsageError(
                f"the request is {len(text.encode())} bytes of UTF-8, over the {MAX_REQUEST_BYTES} it takes"
            )
        return text

    def author(self, text: str) -> dict:
        """The body of POST .../author-runs for the request ``text``."""
        args = self.args
        chosen = {"plan_id": args.plan, "runtime": args.runtime, "model": args.model, "timeout_h": args.timeout_h}
        chosen.update(account=args.account, profile=args.agent)
        return {"request": text} | given(chosen)

    def rerun(self) -> dict | None:
        """The body of a rerun: none when it keeps everything, so an older hub, which takes none, still takes it."""
        body = self.browser()
        if getattr(self.args, "account", None):
            body["account"] = self.args.account
        elif getattr(self.args, "account_by_rule", False):
            body["account_by_rule"] = True
        return body or None

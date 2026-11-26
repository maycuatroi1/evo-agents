"""``evo-agents hub run``: dispatch plan steps to your own workers, and follow, steer and end their runs.

``dispatch`` queues one run per step named (POST /v1/projects/{p}/runs), all of them or none, ``plan`` queues a plan
run, one run that does every step of the plan not done yet (POST /v1/projects/{p}/plan-runs), in one agent session or
with ``--session-per-step`` in a new session for each step, or with ``--system SLUG`` hands the plan to an agent system
of the project instead (POST /v1/projects/{p}/plans/{id}/system-dispatch, ``options``), and ``author`` queues an
author run, a plan written from your request with the create-exec-plan skill on the pool of its agent (POST
/v1/projects/{p}/author-runs, ``evo_agents.hub.author``), or with ``--plan`` a new revision of a plan of the project.
``list`` and ``show`` read runs of every kind, the chat runs of group chats among them. ``logs`` prints a run's events
page by page (GET .../runs/{id}/events?after=SEQ), or with ``--follow`` reads the run's server-sent events (GET
.../runs/{id}/stream) until the hub sends ``end``; ``watch`` waits until a run's state, an open decision or a plan's
step changes (``watch``). ``send`` leaves a message for the run's agent, files too with ``--attach``
(``send``), and ``cancel``, ``approve``, ``takeover``, ``handback``, ``rerun`` and ``access`` are the owner's
controls (``access`` in ``access``), ``priority`` a writer's (``priority``); ``outputs`` lists the files
its agent made (``outputs``). ``credentials`` lists the run's leases of its owner's secrets and the hub's GitHub
App, the owner only, never a value. Every command after ``dispatch``, ``plan``, ``author`` and ``list`` takes the id of
a run, as ``list`` shows it. None of them picks the machine: an agent in pool mode runs on its own pool, and the hub
refuses a worker, pool, label or taint for it. A refusal is the hub's message on stderr, such as why a worker whose
owner set it to take runs dispatched from the web only refuses a run dispatched with a token. ``dispatch``, ``plan``,
``author`` and ``rerun`` take ``--account``, an account picked by hand, and all but ``rerun`` ``--agent``, an agent
profile (``evo_agents.hub.cli.run.options``). ``docs/workers.md`` describes the protocol behind them; ``--json`` prints
what the hub answered, with the keys declared next to the flag.

``--model`` names a model as one runtime names it (opencode: provider/model), so ``dispatch`` and ``plan`` take it
only with ``--runtime`` naming that runtime: with ``any`` the claiming worker picks the runtime, and the model might
not be one it knows. The hub itself takes a model with any runtime; this check is the command line's.

A followed stream that stops before ``end`` (a proxy closed it, the hub restarted, or nothing came for
STREAM_TIMEOUT seconds although the hub pings every 15) is opened again with ``Last-Event-ID`` set to the last event
read, so no event is missed or printed twice. After ``end`` it is never opened again. RECONNECTS tries in a row that
bring nothing, not even a ping, end the command with an error.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory, as ``hub plan`` does. Standard library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands), one per subcommand, the
modules of this package named in parentheses above holding theirs: ``resources`` holds the gateways to the routes
(:class:`Runs`, :class:`PlanRuns`, :class:`AuthorRuns`, :class:`SystemDispatch`), ``options`` the arguments and the
value object :class:`RunOptions` that turns them into the body of a dispatch, a plan run, an author run or a rerun,
and ``describe`` what ``show`` says of a run. :data:`RUN` is the group, attached by ``evo_agents.hub.cli.session``.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, TextIO

from evo_agents.clikit import (
    Arg,
    Group,
    Result,
    Stderr,
    Table,
    Text,
    UsageError,
    View,
    Views,
    returns_array,
    returns_object,
)
from evo_agents.hub.author import MAX_REQUEST_BYTES
from evo_agents.hub.chats import chat_link
from evo_agents.hub.cli.attach import describe as files_text
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, when
from evo_agents.hub.cli.run.access import RunAccess
from evo_agents.hub.cli.run.candidates import RunCandidates
from evo_agents.hub.cli.run.describe import repos_text, shown_lines, step_text, timeout_text
from evo_agents.hub.cli.run.events import clip, follow, format_event, read_events
from evo_agents.hub.cli.run.keys import RUN_KEYS, RUN_OUTPUT
from evo_agents.hub.cli.run.options import (
    AUTHOR_ARGS,
    DISPATCH_ARGS,
    LIST_ARGS,
    PLAN_ARGS,
    PLAN_ID,
    RUN_ID,
    RunOptions,
    rerun_options,
)
from evo_agents.hub.cli.run.outputs import RunOutputs
from evo_agents.hub.cli.run.priority import RunPriority
from evo_agents.hub.cli.run.resources import AuthorRuns, PlanRuns, Runs, SystemDispatch
from evo_agents.hub.cli.run.send import RunSend
from evo_agents.hub.cli.run.spend import usd
from evo_agents.hub.cli.run.watch import RunWatch
from evo_agents.hub.cli.run.workers import worker_id
from evo_agents.hub.client import origin
from evo_agents.hub.plan_writes import checked_plan_id
from evo_agents.hub.runs import EVENT_KINDS, TERMINAL_STATES

__all__ = ["RUN", "RUN_KEYS"]

TITLE_CHARS = 40  # of a step's title in the run table

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
RUN_LIST_KEYS = ("runs", "total", "counts", "limit", "offset")
EVENTS_KEYS = ("run_id", "state", "last_seq", "events", "more")
CHAT_KEYS = ("run_id", "state", "status", "owner", "plan_id", "plan_revision", "messages", "more")
LEASE_KEYS = ("id", "name", "provider", "kind", "target", "worker", "issued_at", "expires_at", "revoked_at")
LEASE_KEYS += ("profile", "profile_id", "profile_version")
DISPATCHED_KEYS = ("system", "system_revision", "trace_id", "plan_id", "plan_revision", "chat_id", "message_id")
DISPATCHED_KEYS += ("run_id",)
PLAN_RUN = returns_object(
    *RUN_KEYS,
    schema="Run",
    variants=(("--system", returns_object(*DISPATCHED_KEYS, schema="SystemDispatched")),),
)
RUN_ARGS = (RUN_ID, PROJECT)


@dataclass(frozen=True)
class Deferred(View):
    """A view made only when it is written: a queued run's text reads fields ``--json`` never needs."""

    make: Callable[[], View]

    def lines(self) -> list[str]:
        return self.make().lines()

    def write(self, out: TextIO) -> None:
        self.make().write(out)


def warning(run: dict, ctx: HubContext) -> View:
    """On stderr, the warning the hub gave with a run it queued: no worker can take it now, and why."""
    if not run.get("warning"):
        return Views()
    where = ctx.with_project()
    return Stderr(Text(f"Warning: {run['warning']}. More: `evo-agents hub run candidates {run['id']}{where}`."))


def follow_hint(run_id: int, ctx: HubContext) -> str:
    return f"Follow it with `evo-agents hub run logs {run_id} --follow{ctx.with_project()}`"


def author_on(run: dict) -> str:
    """Where an author run goes: its pinned worker, or any its selector fits (an agent in pool mode: its pool)."""
    if run.get("pinned_worker_id"):
        return f"on worker #{run['pinned_worker_id']}"
    asked = run.get("selector") or []
    return f"on a worker with {', '.join(asked)}" if asked else "on any worker of yours that can take it"


def as_you(run: dict) -> str:
    """What a queued run says of ``as_owner``: nothing for a run that does not run as its owner."""
    return "; it runs as you, secrets, tokens and workers aside" if run.get("as_owner") else ""


def runtime_text(run: dict) -> str:
    """The runtime a run asked for, with its model when it named one."""
    return run["requested_runtime"] + (f" (model {run['model']})" if run.get("model") else "")


def pinned(run: dict) -> str:
    return f", on worker #{run['pinned_worker_id']} only" if run["pinned_worker_id"] else ""


# Queueing


class RunDispatch(HubCommand):
    name = "dispatch"
    help = "queue a run of each step named for the pool of its agent, all of them or none (needs writer)"
    arguments = (
        PLAN_ID,
        Arg("steps", metavar="STEP", nargs="+", help="a step id (or order); one run each"),
        PROJECT,
        *DISPATCH_ARGS,
    )
    output = returns_array(*RUN_KEYS, schema="Run")

    def check(self, args: argparse.Namespace) -> None:
        RunOptions(args).check_model()

    def run(self, ctx: HubContext) -> Result:
        queued = Runs.of(ctx).dispatch(RunOptions(ctx.args).dispatch())
        return Result(queued, Deferred(lambda: self.said(queued, ctx)))

    @staticmethod
    def said(queued: list[dict], ctx: HubContext) -> View:
        parts: list[View] = []
        for run in queued:
            said = (
                f"Queued run #{run['id']}: {step_text(run)}, repo {run['repo']}; runtime {runtime_text(run)}, "
                f"{run['mode']}, approval {run['approval']}, timeout {run['timeout_min']} min{pinned(run)}"
                f"{as_you(run)}."
            )
            parts += [Text(said), warning(run, ctx)]
        parts.append(Text(f"{follow_hint(queued[0]['id'], ctx)}."))
        return Views(*parts)


class RunPlan(HubCommand):
    name = "plan"
    help = (
        "queue a plan run: one run, on the pool of its agent, that does every step of the plan "
        "not done yet, or with --system hand the plan to an agent system of the project (needs writer)"
    )
    arguments = (PLAN_ID, PROJECT, *PLAN_ARGS)
    output = PLAN_RUN

    def check(self, args: argparse.Namespace) -> None:
        options = RunOptions(args)
        options.check_system()
        if args.system is None:
            options.check_model()

    def run(self, ctx: HubContext) -> Result:
        if ctx.args.system is not None:
            return self.hand(ctx)
        run = PlanRuns.of(ctx).queue(RunOptions(ctx.args).plan())
        return Result(run, Deferred(lambda: self.said(run, ctx)))

    @staticmethod
    def said(run: dict, ctx: HubContext) -> View:
        each = ", each in a new session" if run.get("session_per_step") else ""
        said = (
            f"Queued plan run #{run['id']}: {step_text(run)}, every step not done yet{each}, in {repos_text(run)}; "
            f"runtime {runtime_text(run)}, {run['mode']}, timeout {timeout_text(run)} of agent time{pinned(run)}"
            f"{as_you(run)}."
        )
        decisions = f"`evo-agents hub decision list --run {run['id']}{ctx.with_project()}`"
        hint = f"{follow_hint(run['id'], ctx)}; the decisions its agent asks you: {decisions}."
        return Views(Text(said), warning(run, ctx), Text(hint))

    @staticmethod
    def hand(ctx: HubContext) -> Result:
        """``hub run plan PLAN --system SLUG``: hand the plan to the system's entry agent."""
        resource = SystemDispatch.of(ctx)
        handed = resource.hand(checked_plan_id(ctx.args.plan), ctx.args.system)
        return Result(handed, Deferred(lambda: RunPlan.handed(handed, resource.hub.url, ctx)))

    @staticmethod
    def handed(handed: dict, hub_url: str, ctx: HubContext) -> View:
        link = origin(hub_url) + chat_link(handed["chat_id"], handed["message_id"])
        said = (
            f"Handed plan {handed['plan_id']} (revision {handed['plan_revision']}) to system {handed['system']} "
            f"(revision {handed['system_revision']}): trace #{handed['trace_id']}, run #{handed['run_id']} of its "
            f"entry agent, in your chat #{handed['chat_id']} with it ({link})."
        )
        return Text(said, f"{follow_hint(handed['run_id'], ctx)}.")


class RunAuthor(HubCommand):
    name = "author"
    help = (
        "queue an author run: a plan written from your request with the create-exec-plan skill, on the pool of "
        "its agent (needs writer)"
    )
    arguments = (
        Arg(
            "request",
            metavar="REQUEST",
            help=f"what the plan should achieve, at most {MAX_REQUEST_BYTES // 1024} KiB of UTF-8; - reads it from "
            "stdin",
        ),
        PROJECT,
        *AUTHOR_ARGS,
    )
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        options = RunOptions(ctx.args)
        text = options.request()
        run = AuthorRuns.of(ctx).queue(options.author(text))
        return Result(run, Deferred(lambda: self.said(run, ctx)))

    @staticmethod
    def said(run: dict, ctx: HubContext) -> View:
        said = (
            f"Queued author run #{run['id']}: {step_text(run)}, {author_on(run)} over {repos_text(run)}; "
            f"runtime {runtime_text(run)}, timeout {timeout_text(run)} of agent time. It reads only and pushes nothing."
        )
        return Views(Text(said), warning(run, ctx), Text(f"{follow_hint(run['id'], ctx)}."))


# Reading


class RunList(HubCommand):
    name = "list"
    help = "the runs of a project, newest first, with how many are in each state"
    arguments = (PROJECT, *LIST_ARGS)
    output = returns_object(*RUN_LIST_KEYS, schema="RunList")
    columns: ClassVar[tuple[str, ...]] = (
        "RUN",
        "KIND",
        "STATE",
        "PLAN",
        "STEP",
        "TITLE",
        "RUNTIME",
        "WORKER",
        "BY",
        "QUEUED (UTC)",
        "COST",
    )

    @staticmethod
    def query(ctx: HubContext) -> list[tuple[str, object]]:
        """The filters and the page asked for, in the order the hub gets them."""
        args = ctx.args
        pairs: list[tuple[str, object]] = [("state", state) for state in args.state or ()]
        if args.plan is not None:
            pairs.append(("plan_id", checked_plan_id(args.plan)))
        if args.worker is not None:
            pairs.append(("worker_id", worker_id(ctx.hub, ctx.credentials.login, args.worker)))
        given = {
            "step": args.step,
            "dispatched_by": args.by,
            "kind": args.kind,
            "account": args.account,
            "q": args.search,
            "limit": args.limit,
            "offset": args.offset,
        }
        return pairs + [(key, value) for key, value in given.items() if value is not None]

    @staticmethod
    def row(run: dict) -> tuple:
        return (
            f"#{run['id']}",
            run["kind"],
            run["state"],
            run["plan_id"],
            run["step_key"] or "-",
            clip(run["title"] or "-", TITLE_CHARS),
            run["runtime"],
            run["worker"] or "-",
            run["dispatched_by"],
            when(run["queued_at"]),
            usd((run.get("spend") or {}).get("cost_usd")),
        )

    def run(self, ctx: HubContext) -> Result:
        runs = Runs.of(ctx)
        listed = runs.list(self.query(ctx))
        rows = [self.row(run) for run in listed["runs"]]
        counts = ", ".join(f"{count} {state}" for state, count in listed["counts"].items() if count)
        shown = len(rows)
        lines = [
            f"{shown} of {listed['total']} run(s) of project {ctx.project}"
            + (f"; by state: {counts}" if counts else "")
        ]
        if listed["offset"] + shown < listed["total"]:
            lines.append(f"More with --offset {listed['offset'] + shown}.")
        return Result(listed, Views(Table(self.columns, rows), Text(*lines)))


class RunShow(HubCommand):
    name = "show"
    help = "one run: state, step, worker, commit, verify results, evidence"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        runs = Runs.of(ctx)
        run = runs.get(ctx.args.run)
        lines = shown_lines(run, runs.hub.url)
        if run["last_seq"]:
            lines.append(f"Its events: `evo-agents hub run logs {run['id']}{ctx.with_project()}`.")
        if run["kind"] == "plan":
            lines.append(f"Its decisions: `evo-agents hub decision list --run {run['id']}{ctx.with_project()}`.")
        return Result(run, Text(*lines))


class RunLogs(HubCommand):
    """With ``--follow`` it writes each event as the stream brings it, and the end of the run once it ended."""

    name = "logs"
    help = "a run's events: what its agent said and did, and its moves; --follow prints them as they come"
    arguments = (
        *RUN_ARGS,
        Arg("-f", "--follow", action="store_true", help="keep reading the run's live stream until the run ends"),
        Arg("--after", type=int, metavar="SEQ", help="only the events after this one (default 0)"),
        Arg(
            "--kind",
            action="append",
            choices=EVENT_KINDS,
            metavar="KIND",
            help=f"only events of this kind, one of {', '.join(EVENT_KINDS)}; repeat it for several",
        ),
    )
    output = returns_object(*EVENTS_KEYS, schema="RunEvents")
    json_help = "the events as one object, as the hub answers a page of them; not with --follow"

    def check(self, args: argparse.Namespace) -> None:
        if args.json and args.follow:
            raise UsageError("--json prints the events read as one object, so it does not go with --follow")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        hub, project, after = ctx.hub, ctx.project, args.after or 0
        if args.follow:

            def written(event: dict) -> None:
                print(format_event(event), file=ctx.out, flush=True)

            ended = follow(hub, project, args.run, after, on_event=written, kinds=frozenset(args.kind or ()))
            said = f"Run #{args.run} ended {ended.get('state')}; its last event is {ended.get('last_seq')}."
            return Result(ended, Text(said))
        page = read_events(hub, project, args.run, after, args.kind or ())
        shown = f"{len(page['events'])} event(s) shown, the last of the run is {page['last_seq']}"
        hint = "" if page["state"] in (*TERMINAL_STATES, "ended") else "; --follow prints the next ones as they come"
        lines = [format_event(event) for event in page["events"]]
        return Result(page, Text(*lines, f"Run #{args.run} is {page['state']}: {shown}{hint}."))


CHAT_STATUS_WORDS = {
    "working": "the agent works on its turn",
    "waiting": "it waits for your reply: `evo-agents hub run send {run} TEXT`, or end it with "
    "`evo-agents hub run finish {run}`",
    "ended": "the chat is over",
}


class RunChat(HubCommand):
    name = "chat"
    help = "the chat of an author run: its agent's messages and its owner's, oldest first, and whose turn"
    arguments = RUN_ARGS
    output = returns_object(*CHAT_KEYS, schema="RunChat")

    def run(self, ctx: HubContext) -> Result:
        chat = Runs.of(ctx).chat(ctx.args.run)
        lines = ["(older messages left out)"] if chat["more"] else []
        for message in chat["messages"]:
            who = "agent" if message["author"] == "agent" else message["login"] or "owner"
            lines += [f"[{when(message['at'])}] {who} (run #{message['run_id']}):", message["text"].rstrip()]
            if message.get("attachments"):
                lines.append(f"(files: {files_text(message['attachments'])})")
            lines.append("")
        plan = f"; plan {chat['plan_id']} at revision {chat['plan_revision']}" if chat["plan_id"] else ""
        said = CHAT_STATUS_WORDS[chat["status"]].format(run=chat["run_id"])
        lines.append(f"{len(chat['messages'])} message(s); run #{chat['run_id']} is {chat['state']}: {said}{plan}.")
        return Result(chat, Text(*lines))


def lease_state(lease: dict, now: datetime) -> str:
    """``revoked YYYY-MM-DD HH:MM`` (UTC), ``expired``, or ``out`` for a lease the run still holds."""
    if lease["revoked_at"]:
        return f"revoked {when(lease['revoked_at'])}"
    try:
        ends = datetime.fromisoformat(lease["expires_at"]) if lease["expires_at"] else None
    except ValueError:
        ends = None
    if ends is not None and (ends if ends.tzinfo else ends.replace(tzinfo=UTC)) <= now:
        return "expired"
    return "out"


class RunCredentials(HubCommand):
    name = "credentials"
    help = (
        "the leases a run got: secret or GitHub App, target, worker, when issued, ending and revoked; never the "
        "value (owner only)"
    )
    arguments = RUN_ARGS
    output = returns_array(*LEASE_KEYS, schema="RunLease")
    columns: ClassVar[tuple[str, ...]] = (
        "NAME",
        "PROVIDER",
        "TARGET",
        "WORKER",
        "ISSUED (UTC)",
        "EXPIRES (UTC)",
        "STATE",
    )

    def run(self, ctx: HubContext) -> Result:
        run_id = ctx.args.run
        leases = Runs.of(ctx).credentials(run_id)
        if not leases:
            return Result(
                leases, Text(f"Run #{run_id} got no lease: its worker asked for none, or the hub had nothing for it.")
            )
        now = datetime.now(UTC)
        rows = [
            (
                lease["name"],
                lease["provider"],
                lease["target"],
                lease["worker"],
                when(lease["issued_at"]),
                when(lease["expires_at"]),
                lease_state(lease, now),
            )
            for lease in leases
        ]
        out = sum(row[-1] == "out" for row in rows)
        said = f"{len(leases)} lease(s) of run #{run_id}, " + (
            f"{out} still out." if out else "none still out: each was given back, taken back or ended."
        )
        return Result(leases, Views(Table(self.columns, rows), Text(said)))


# The owner's controls: POST .../runs/{id}/<name>, each answering the run


def control(ctx: HubContext, action: str, body: dict | None = None) -> dict:
    return Runs.of(ctx).control(ctx.args.run, action, body)


class RunCancel(HubCommand):
    name = "cancel"
    help = "cancel a run: a queued run or one in review at once, a held one once its worker hears of it (owner only)"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name)
        if run["state"] == "cancelled":
            return Result(run, Text(f"Cancelled run #{run['id']}, {step_text(run)}."))
        said = (
            f"Asked the worker of run #{run['id']} ({run['state']}) to stop it; it ends cancelled once the worker does."
        )
        return Result(run, Text(said))


class RunApprove(HubCommand):
    name = "approve"
    help = "approve a run in review: the run and its step are done (owner only)"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name)
        return Result(run, Text(f"Approved run #{run['id']}: it is done, and so is {step_text(run)}."))


class RunTakeover(HubCommand):
    name = "takeover"
    help = "ask a run's worker to let a person drive its agent in a terminal on the worker (owner only)"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name)
        said = (
            f"Asked the worker of run #{run['id']} for a takeover: once the agent's turn ends, the run is interactive "
            "and a person drives the agent in a terminal on the worker."
        )
        return Result(run, Text(said))


class RunHandback(HubCommand):
    name = "handback"
    help = "ask a run's worker to let its agent go on headless in the same session (owner only)"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name)
        said = f"Asked the worker of run #{run['id']} to hand back: the agent goes on headless in the same session."
        return Result(run, Text(said))


class RunRerun(HubCommand):
    name = "rerun"
    help = "queue the step of a run that ended again, with its runtime, mode, approval, timeout and worker (owner only)"
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        rerun_options(parser)

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name, RunOptions(ctx.args).rerun())
        return Result(run, Text(f"Queued run #{run['id']}, a rerun of run #{run['parent_run_id']}: {step_text(run)}."))


class RunFinish(HubCommand):
    name = "finish"
    help = (
        "end the chat of an author run: it ends done, at once when parked, else once its agent's turn is over "
        "(owner only)"
    )
    arguments = RUN_ARGS
    output = RUN_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        run = control(ctx, self.name)
        if run["state"] == "done":
            return Result(run, Text(f"Ended the chat of run #{run['id']}: it is done."))
        said = (
            f"Ended the chat of run #{run['id']} ({run['state']}): its worker ends it done once the agent's turn is "
            "over."
        )
        return Result(run, Text(said))


RUN = Group(
    "run",
    "dispatch plan steps, or a whole plan, to the pool of their agent, and follow, steer and end their runs",
    [
        RunDispatch(),
        RunPlan(),
        RunAuthor(),
        RunList(),
        RunShow(),
        RunLogs(),
        RunWatch(),
        RunSend(),
        RunChat(),
        RunCredentials(),
        RunCandidates(),
        RunOutputs(),
        RunAccess(),
        RunPriority(),
        RunCancel(),
        RunApprove(),
        RunTakeover(),
        RunHandback(),
        RunRerun(),
        RunFinish(),
    ],
    dest="run_command",
)

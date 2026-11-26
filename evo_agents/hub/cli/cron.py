"""``evo-agents hub cron list|show|set|pause|resume|run|fires|delete``: the cron jobs of a project (``docs/hub.md``, Cron
jobs), over /v1/projects/{p}/cron-jobs.

A cron job fires on five cron fields read in an IANA time zone, and each fire starts one kind of work: a message to an
agent in a new thread (``--agent`` and ``--prompt``, ``--chat`` for a chat other than your direct one), a run of a plan
(``--plan``), or a shell command (``--command``, ``--repo``, ``--timeout`` in minutes). ``set`` creates a job, owned by
you, or changes what an existing one of yours starts: it reads the job first, keeps what you do not pass and sends the
revision it read, so a change made meanwhile is a refusal rather than lost. Members read the jobs; writers make and
manage their own; an admin pauses, resumes, fires and deletes anyone's.

Every command finds its project with ``--project``, or in the harness.yaml (``hub.project``) of the harness around the
current directory. ``--json`` prints what the hub answered. Standard library only.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`CronJobs` is
the gateway to the cron jobs of a project, :func:`job_body` turns the options of ``set`` into the job to write and
:func:`job_lines` says a job to a person.
"""

from __future__ import annotations

import argparse
from typing import Any

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource, call_object, when
from evo_agents.hub.client import HubError

# The keys of the hub's answers in --json (the contract, `evo-agents hub contract print`).
FIRE_KEYS = ("id", "scheduled_for", "manual", "actor", "outcome", "reason", "run_id", "created_at", "handled_at")
JOB_KEYS = (
    "project",
    "name",
    "cron",
    "timezone",
    "kind",
    "agent",
    "chat_id",
    "prompt",
    "plan",
    "command",
    "repo",
    "timeout_minutes",
    "owner",
    "revision",
    "paused",
    "paused_at",
    "paused_by",
    "paused_reason",
    "next_fire_at",
    "last_fire",
    "cost_24h_usd",
    "can_change",
    "can_manage",
    "created_at",
    "updated_at",
    "updated_by",
)
WORK_FLAGS = {
    "agent": ("agent", "chat", "prompt"),
    "plan": ("plan",),
    "command": ("command", "repo", "timeout"),
}
BODY_KEYS = {"chat": "chat_id", "timeout": "timeout_minutes"}  # flag -> key of the body, where they differ


class CronJobs(ProjectResource):
    """The cron jobs of a project: /v1/projects/{p}/cron-jobs and the routes below it (Gateway)."""

    collection = "cron-jobs"

    def object(self, method: str, *parts: Any, body: dict | None = None) -> dict:
        """The hub's answer, for a route that answers a JSON object; HubError when it did not."""
        return call_object(self.hub, method, self.path(*parts), body)

    def list(self) -> list[dict]:
        return self.call("GET")

    def get(self, name: str) -> dict:
        return self.object("GET", name)

    def find(self, name: str) -> dict | None:
        """The job ``name``, or None when the hub has none of that name."""
        try:
            return self.get(name)
        except HubError as exc:
            if exc.status != 404:
                raise
        return None

    def put(self, name: str, body: dict) -> dict:
        return self.object("PUT", name, body=body)

    def act(self, name: str, action: str, body: dict) -> dict:
        return self.object("POST", name, action, body=body)

    def fires(self, name: str, limit: int) -> list[dict]:
        return self.call("GET", name, "fires", limit=limit)

    def delete(self, name: str) -> dict:
        return self.object("DELETE", name)


def work_text(job: dict) -> str:
    if job["kind"] == "agent":
        where = f" in chat {job['chat_id']}" if job.get("chat_id") else ""
        return f"@{job['agent'] or '(deleted agent)'}{where}"
    if job["kind"] == "plan":
        return f"plan {job['plan']}"
    return f"{job['command']}" + (f" (in {job['repo']})" if job.get("repo") else "")


def state_text(job: dict) -> str:
    return "paused" if job["paused"] else when(job.get("next_fire_at"))


def job_lines(job: dict) -> list[str]:
    """One job as ``show`` prints it."""
    lines = [
        f"cron job {job['name']} of project {job['project']}, revision {job['revision']}, owned by {job['owner']}",
        f"  cron: {job['cron']} ({job['timezone']})",
        f"  {job['kind']}: {work_text(job)}",
    ]
    if job["kind"] == "agent":
        lines.append(f"  prompt: {job['prompt']}")
    if job["kind"] == "command":
        lines.append(f"  time limit: {job['timeout_minutes']} minutes")
    if job["paused"]:
        by = f" by {job['paused_by']}" if job.get("paused_by") else " by the hub"
        why = f": {job['paused_reason']}" if job.get("paused_reason") else ""
        lines.append(f"  paused{by} at {when(job['paused_at'])}{why}")
    else:
        lines.append(f"  next fire: {when(job.get('next_fire_at'))} UTC")
    last = job.get("last_fire")
    if last:
        why = f" ({last['reason']})" if last.get("reason") else ""
        lines.append(f"  last fire: {when(last['scheduled_for'])}, {last.get('outcome') or 'pending'}{why}")
    lines.append(f"  cost of its runs in 24 hours: {job['cost_24h_usd']:.2f} USD")
    return lines


NAME = Arg("name", metavar="NAME", help="the job's name")
ONE_JOB = returns_object(*JOB_KEYS, schema="CronJobOut")


class CronList(HubCommand):
    name = "list"
    help = "the cron jobs of a project, their next fire and cost in 24 hours"
    arguments = (PROJECT,)
    output = returns_array(*JOB_KEYS, schema="CronJobOut")
    columns = ("JOB", "CRON", "KIND", "OWNER", "NEXT FIRE (UTC)", "LAST", "USD 24H")

    def run(self, ctx: HubContext) -> Result:
        found = CronJobs.of(ctx).list()
        if not found:
            return Result(found, Text(f"Project {ctx.project} has no cron job."))
        return Result(found, Table(self.columns, [self.row(job) for job in found]))

    @staticmethod
    def row(job: dict) -> tuple:
        return (
            job["name"],
            f"{job['cron']} {job['timezone']}",
            job["kind"],
            job["owner"],
            state_text(job),
            (job.get("last_fire") or {}).get("outcome") or "-",
            f"{job['cost_24h_usd']:.2f}",
        )


class CronShow(HubCommand):
    name = "show"
    help = "one cron job"
    arguments = (NAME, PROJECT)
    output = ONE_JOB

    def run(self, ctx: HubContext) -> Result:
        found = CronJobs.of(ctx).get(ctx.args.name)
        return Result(found, Text(*job_lines(found)))


def given_work(args: argparse.Namespace) -> dict[str, dict]:
    """kind -> the work flags of that kind given on the command line."""
    given: dict[str, dict] = {}
    for kind, flags in WORK_FLAGS.items():
        values = {flag: getattr(args, flag) for flag in flags if getattr(args, flag) is not None}
        if values:
            given[kind] = values
    return given


def job_body(args: argparse.Namespace, held: dict | None) -> dict:
    """The body of PUT for ``args`` over the job ``held`` (None: a new one); UsageError saying what is wrong."""
    given = given_work(args)
    if len(given) > 1:
        raise UsageError(f"flags of kinds {' and '.join(given)} together: a job starts one kind of work")
    kind = next(iter(given), None) or (held or {}).get("kind")
    if kind is None:
        raise UsageError("what does it start? give --agent and --prompt, --plan, or --command")
    cron = args.cron or (held or {}).get("cron")
    if cron is None:
        raise UsageError("give --cron, five fields such as '0 9 * * 1-5'")
    body: dict[str, Any] = {"cron": cron, "timezone": args.tz or (held or {}).get("timezone") or "UTC", "kind": kind}
    same = held is not None and held["kind"] == kind
    for flag in WORK_FLAGS[kind]:
        key = BODY_KEYS.get(flag) or flag
        value = getattr(args, flag)
        body[key] = value if value is not None else held.get(key) if same and held is not None else None
    if body.get("repo") == "":
        body["repo"] = None
    body["if_revision"] = args.if_revision if args.if_revision is not None else (held or {}).get("revision")
    return body


class CronSet(HubCommand):
    name = "set"
    help = "create a cron job, or change what one of yours starts"
    parser_options = {
        "description": "A new job needs --cron and its work: --agent and --prompt (and --chat), --plan, or --command "
        "(and --repo, --timeout). For a job that exists, what you do not pass stays as it is. Agent and plan jobs fire "
        "at most every 15 minutes, command jobs every 5."
    }
    arguments = (
        NAME,
        PROJECT,
        Arg("--cron", help="five fields: minute, hour, day of month, month, day of week, as '0 9 * * 1-5'"),
        Arg("--tz", help="the IANA time zone the cron reads in (default: UTC, or the job's)"),
        Arg("--agent", metavar="HANDLE", help="kind agent: the agent it sends the prompt to"),
        Arg("--prompt", help="kind agent: the message"),
        Arg("--chat", type=int, metavar="ID", help="kind agent: a chat of yours (default: your direct chat)"),
        Arg("--plan", metavar="PLAN", help="kind plan: the plan it runs"),
        Arg("--command", metavar="LINE", help="kind command: the shell command line"),
        Arg("--repo", help="kind command: the repo it runs in ('' for an empty directory)"),
        Arg("--timeout", type=int, metavar="MINUTES", help="kind command: its time limit, 1 to 240 (30)"),
        Arg("--if-revision", type=int, metavar="N", help="the revision it replaces (default: the one read)"),
    )
    output = returns_object(*JOB_KEYS, "created", "changed", schema="CronJobWritten")

    def run(self, ctx: HubContext) -> Result:
        args, jobs = ctx.args, CronJobs.of(ctx)
        found = jobs.put(args.name, job_body(args, jobs.find(args.name)))
        if not found["changed"]:
            said = f"cron job {args.name} is at revision {found['revision']} already: nothing changed"
        else:
            done = "created" if found["created"] else "stored"
            said = f"{done} cron job {args.name} at revision {found['revision']}; next fire {state_text(found)} UTC"
        return Result(found, Text(said))


def switched(found: dict, name: str) -> Result:
    return Result(
        found, Text(f"cron job {name} {'paused' if found['paused'] else 'runs'}: next fire {state_text(found)}")
    )


class CronPause(HubCommand):
    name = "pause"
    help = "pause a cron job: it does not fire until resumed"
    arguments = (NAME, PROJECT, Arg("--reason", help="why, shown with the pause"))
    output = ONE_JOB

    def run(self, ctx: HubContext) -> Result:
        return switched(CronJobs.of(ctx).act(ctx.args.name, "pause", {"reason": ctx.args.reason}), ctx.args.name)


class CronResume(HubCommand):
    name = "resume"
    help = "resume a paused cron job"
    arguments = (NAME, PROJECT)
    output = ONE_JOB

    def run(self, ctx: HubContext) -> Result:
        return switched(CronJobs.of(ctx).act(ctx.args.name, "resume", {}), ctx.args.name)


class CronRun(HubCommand):
    name = "run"
    help = "fire a cron job now, a fire due at this moment"
    arguments = (NAME, PROJECT)
    output = returns_object(*FIRE_KEYS, schema="CronFireOut")

    def run(self, ctx: HubContext) -> Result:
        name = ctx.args.name
        found = CronJobs.of(ctx).act(name, "run", {})
        return Result(found, Text(f"fired cron job {name}: fire {found['id']}, due {when(found['scheduled_for'])} UTC"))


class CronFires(HubCommand):
    name = "fires"
    help = "the fires of a cron job, the latest first"
    arguments = (NAME, PROJECT, Arg("--limit", type=int, default=50, metavar="N", help="at most N fires (50)"))
    output = returns_array(*FIRE_KEYS, schema="CronFireOut")
    columns = ("FIRE", "DUE (UTC)", "BY", "OUTCOME", "RUN", "REASON")

    def run(self, ctx: HubContext) -> Result:
        found = CronJobs.of(ctx).fires(ctx.args.name, ctx.args.limit)
        empty = f"cron job {ctx.args.name} has not fired yet."
        return Result(found, Table(self.columns, [self.row(fire) for fire in found or ()], empty))

    @staticmethod
    def row(fire: dict) -> tuple:
        return (
            str(fire["id"]),
            when(fire["scheduled_for"]),
            fire["actor"] or "schedule",
            fire.get("outcome") or "pending",
            str(fire["run_id"]) if fire.get("run_id") else "-",
            fire.get("reason") or "",
        )


class CronDelete(HubCommand):
    name = "delete"
    help = "delete a cron job with its fires; its runs stay"
    arguments = (NAME, PROJECT)
    output = returns_object("project", "deleted", schema="CronJobDeleted")

    def run(self, ctx: HubContext) -> Result:
        found = CronJobs.of(ctx).delete(ctx.args.name)
        return Result(found, Text(f"deleted cron job {found['deleted']} of project {found['project']}"))


CRON = Group(  # hub cron
    "cron",
    "list, show, set, pause, resume, run, fires or delete the cron jobs of a project",
    [CronList(), CronShow(), CronSet(), CronPause(), CronResume(), CronRun(), CronFires(), CronDelete()],
    description="A cron job fires on five cron fields in a time zone and starts a message to an agent, a plan run "
    "or a shell command as its owner. Members read them; writers make and manage their own; admins pause, "
    "resume, fire and delete anyone's.",
)

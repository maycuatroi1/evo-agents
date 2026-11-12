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
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_agents import PROJECT_HELP, project_of
from evo_agents.hub.cli_client import call_object, client_command, print_json, signed_in, table, when
from evo_agents.hub.client import EXIT_USAGE, HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

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


def _path(project: str, name: str | None = None) -> str:
    path = f"/v1/projects/{quote(project, safe='')}/cron-jobs"
    return path if name is None else f"{path}/{quote(name, safe='')}"


def _usage(text: str) -> int:
    print(f"error: {text}", file=sys.stderr)
    return EXIT_USAGE


def _work(job: dict) -> str:
    if job["kind"] == "agent":
        where = f" in chat {job['chat_id']}" if job.get("chat_id") else ""
        return f"@{job['agent'] or '(deleted agent)'}{where}"
    if job["kind"] == "plan":
        return f"plan {job['plan']}"
    return f"{job['command']}" + (f" (in {job['repo']})" if job.get("repo") else "")


def _state(job: dict) -> str:
    return "paused" if job["paused"] else when(job.get("next_fire_at"))


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    project = project_of(args)
    found = hub.call("GET", _path(project))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"Project {project} has no cron job.")
        return 0
    rows = [
        (
            job["name"],
            f"{job['cron']} {job['timezone']}",
            job["kind"],
            job["owner"],
            _state(job),
            (job.get("last_fire") or {}).get("outcome") or "-",
            f"{job['cost_24h_usd']:.2f}",
        )
        for job in found
    ]
    table(("JOB", "CRON", "KIND", "OWNER", "NEXT FIRE (UTC)", "LAST", "USD 24H"), rows)
    return 0


def _show(job: dict) -> None:
    print(f"cron job {job['name']} of project {job['project']}, revision {job['revision']}, owned by {job['owner']}")
    print(f"  cron: {job['cron']} ({job['timezone']})")
    print(f"  {job['kind']}: {_work(job)}")
    if job["kind"] == "agent":
        print(f"  prompt: {job['prompt']}")
    if job["kind"] == "command":
        print(f"  time limit: {job['timeout_minutes']} minutes")
    if job["paused"]:
        by = f" by {job['paused_by']}" if job.get("paused_by") else " by the hub"
        why = f": {job['paused_reason']}" if job.get("paused_reason") else ""
        print(f"  paused{by} at {when(job['paused_at'])}{why}")
    else:
        print(f"  next fire: {when(job.get('next_fire_at'))} UTC")
    last = job.get("last_fire")
    if last:
        why = f" ({last['reason']})" if last.get("reason") else ""
        print(f"  last fire: {when(last['scheduled_for'])}, {last.get('outcome') or 'pending'}{why}")
    print(f"  cost of its runs in 24 hours: {job['cost_24h_usd']:.2f} USD")


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "GET", _path(project_of(args), args.name))
    if args.json:
        print_json(found)
        return 0
    _show(found)
    return 0


def _given(args) -> dict[str, dict]:
    """kind -> the work flags of that kind given on the command line."""
    given: dict[str, dict] = {}
    for kind, flags in WORK_FLAGS.items():
        values = {flag: getattr(args, flag) for flag in flags if getattr(args, flag) is not None}
        if values:
            given[kind] = values
    return given


def _body(args, held: dict | None) -> dict | str:
    """The body of PUT for ``args`` over the job ``held`` (None: a new one), or what is wrong with them."""
    given = _given(args)
    if len(given) > 1:
        return f"flags of kinds {' and '.join(given)} together: a job starts one kind of work"
    kind = next(iter(given), None) or (held or {}).get("kind")
    if kind is None:
        return "what does it start? give --agent and --prompt, --plan, or --command"
    cron = args.cron or (held or {}).get("cron")
    if cron is None:
        return "give --cron, five fields such as '0 9 * * 1-5'"
    body = {"cron": cron, "timezone": args.tz or (held or {}).get("timezone") or "UTC", "kind": kind}
    same = held is not None and held["kind"] == kind
    for flag in WORK_FLAGS[kind]:
        key = BODY_KEYS.get(flag, flag)
        value = getattr(args, flag)
        body[key] = value if value is not None else held.get(key) if same else None
    if body.get("repo") == "":
        body["repo"] = None
    body["if_revision"] = args.if_revision if args.if_revision is not None else (held or {}).get("revision")
    return body


@client_command
def cmd_set(args) -> int:
    hub, _ = signed_in()
    path = _path(project_of(args), args.name)
    held = None
    try:
        held = call_object(hub, "GET", path)
    except HubError as exc:
        if exc.status != 404:
            raise
    body = _body(args, held)
    if isinstance(body, str):
        return _usage(body)
    found = call_object(hub, "PUT", path, body)
    if args.json:
        print_json(found)
        return 0
    if not found["changed"]:
        print(f"cron job {args.name} is at revision {found['revision']} already: nothing changed")
    else:
        done = "created" if found["created"] else "stored"
        print(f"{done} cron job {args.name} at revision {found['revision']}; next fire {_state(found)} UTC")
    return 0


@client_command
def cmd_manage(args) -> int:
    hub, _ = signed_in()
    path = f"{_path(project_of(args), args.name)}/{args.cron_command}"
    body = {"reason": args.reason} if args.cron_command == "pause" else {}
    found = call_object(hub, "POST", path, body)
    if args.json:
        print_json(found)
        return 0
    if args.cron_command == "run":
        print(f"fired cron job {args.name}: fire {found['id']}, due {when(found['scheduled_for'])} UTC")
    else:
        print(f"cron job {args.name} {'paused' if found['paused'] else 'runs'}: next fire {_state(found)}")
    return 0


@client_command
def cmd_fires(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", f"{_path(project_of(args), args.name)}/fires?limit={args.limit}")
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"cron job {args.name} has not fired yet.")
        return 0
    rows = [
        (
            str(fire["id"]),
            when(fire["scheduled_for"]),
            fire["actor"] or "schedule",
            fire.get("outcome") or "pending",
            str(fire["run_id"]) if fire.get("run_id") else "-",
            fire.get("reason") or "",
        )
        for fire in found
    ]
    table(("FIRE", "DUE (UTC)", "BY", "OUTCOME", "RUN", "REASON"), rows)
    return 0


@client_command
def cmd_delete(args) -> int:
    hub, _ = signed_in()
    found = call_object(hub, "DELETE", _path(project_of(args), args.name))
    if args.json:
        print_json(found)
        return 0
    print(f"deleted cron job {found['deleted']} of project {found['project']}")
    return 0


def _common(parser) -> None:
    parser.add_argument("name", metavar="NAME", help="the job's name")
    parser.add_argument("--project", help=PROJECT_HELP)


def _register_set(csub) -> None:
    put = csub.add_parser(
        "set",
        help="create a cron job, or change what one of yours starts",
        description="A new job needs --cron and its work: --agent and --prompt (and --chat), --plan, or --command "
        "(and --repo, --timeout). For a job that exists, what you do not pass stays as it is. Agent and plan jobs fire "
        "at most every 15 minutes, command jobs every 5.",
    )
    _common(put)
    put.add_argument("--cron", help="five fields: minute, hour, day of month, month, day of week, as '0 9 * * 1-5'")
    put.add_argument("--tz", help="the IANA time zone the cron reads in (default: UTC, or the job's)")
    put.add_argument("--agent", metavar="HANDLE", help="kind agent: the agent it sends the prompt to")
    put.add_argument("--prompt", help="kind agent: the message")
    put.add_argument("--chat", type=int, metavar="ID", help="kind agent: a chat of yours (default: your direct chat)")
    put.add_argument("--plan", metavar="PLAN", help="kind plan: the plan it runs")
    put.add_argument("--command", metavar="LINE", help="kind command: the shell command line")
    put.add_argument("--repo", help="kind command: the repo it runs in ('' for an empty directory)")
    put.add_argument("--timeout", type=int, metavar="MINUTES", help="kind command: its time limit, 1 to 240 (30)")
    put.add_argument("--if-revision", type=int, metavar="N", help="the revision it replaces (default: the one read)")
    json_option(put, returns_object(*JOB_KEYS, "created", "changed", schema="CronJobWritten"))
    put.set_defaults(func=cmd_set)


def register(hsub) -> None:
    """``hub cron``, under the subparsers ``hsub`` of ``hub``."""
    cron = hsub.add_parser(
        "cron",
        help="list, show, set, pause, resume, run, fires or delete the cron jobs of a project",
        description="A cron job fires on five cron fields in a time zone and starts a message to an agent, a plan run "
        "or a shell command as its owner. Members read them; writers make and manage their own; admins pause, "
        "resume, fire and delete anyone's.",
    )
    csub = cron.add_subparsers(dest="cron_command", required=True)

    listed = csub.add_parser("list", help="the cron jobs of a project, their next fire and cost in 24 hours")
    listed.add_argument("--project", help=PROJECT_HELP)
    json_option(listed, returns_array(*JOB_KEYS, schema="CronJobOut"))
    listed.set_defaults(func=cmd_list)

    show = csub.add_parser("show", help="one cron job")
    _common(show)
    json_option(show, returns_object(*JOB_KEYS, schema="CronJobOut"))
    show.set_defaults(func=cmd_show)

    _register_set(csub)

    for name, text in (
        ("pause", "pause a cron job: it does not fire until resumed"),
        ("resume", "resume a paused cron job"),
    ):
        each = csub.add_parser(name, help=text)
        _common(each)
        if name == "pause":
            each.add_argument("--reason", help="why, shown with the pause")
        json_option(each, returns_object(*JOB_KEYS, schema="CronJobOut"))
        each.set_defaults(func=cmd_manage)

    run = csub.add_parser("run", help="fire a cron job now, a fire due at this moment")
    _common(run)
    json_option(run, returns_object(*FIRE_KEYS, schema="CronFireOut"))
    run.set_defaults(func=cmd_manage)

    fires = csub.add_parser("fires", help="the fires of a cron job, the latest first")
    _common(fires)
    fires.add_argument("--limit", type=int, default=50, metavar="N", help="at most N fires (50)")
    json_option(fires, returns_array(*FIRE_KEYS, schema="CronFireOut"))
    fires.set_defaults(func=cmd_fires)

    delete = csub.add_parser("delete", help="delete a cron job with its fires; its runs stay")
    _common(delete)
    json_option(delete, returns_object("project", "deleted", schema="CronJobDeleted"))
    delete.set_defaults(func=cmd_delete)

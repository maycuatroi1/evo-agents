"""``evo-agents hub task list|show|create|edit|block|unblock|import``: the tasks of a project that enabled the core plugin tasks,
through its routes under /v1/x/tasks/<project>/ (``docs/plugins.md``, Core plugins). The plugin's code is
``evo_agents.hub.server.tasks``, which this client never imports: it speaks HTTP only.

``list`` prints the tasks the filters keep (``--mine``, ``--sprint``, ``--backlog``, ``--status``, ``--assignee``).
``show REF`` prints a task whole: its slots, its links as they are now (a plan step with its status in the plan), its
history and comments. ``create`` makes a task, ``edit REF`` changes one: ``--assignee`` gives the whole list of
assignees, ``--role ROLE=LOGIN`` the whole list of each role it names (``--role qa=`` empties it), ``--use-case``,
``--step PLAN#STEP``, ``--run`` and ``--pull`` add links, ``--unlink`` drops one by its id. ``import FILE`` reads
``evo-tasks/1`` JSON (``-`` for stdin) once; ``--dry-run`` prints what it would do and the problems it found.
``block REF --waiting-on X --reason TEXT`` sets a blocker, a flag beside the status: X is a task when it has the shape
of the project's refs (T-12), a member when it is the login of someone with a grant, else a text for someone outside
(``--text`` makes it a text whatever it looks like). ``unblock REF`` clears every blocker set by hand that is still
open, or the one ``--blocker ID`` names (an open decision of a run is answered, not cleared here). ``list --blocked``
keeps the blocked tasks. ``--json`` prints what the hub answered. A refusal prints the hub's reason and each problem it listed. Standard
library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Tasks` is the gateway
to the plugin's routes, the one place that prints the problems a refusal listed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from evo_agents.clikit import Arg, Group, Result, Table, Text, UsageError, returns_array, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.client import HubError

PRIORITIES = ("urgent", "high", "medium", "low")
ROLES = ("ba", "qa", "dev")
ITEM_KEYS = (
    "number",
    "ref",
    "type",
    "title",
    "status",
    "status_group",
    "priority",
    "due_on",
    "sprint",
    "parent",
    "assignees",
    "roles",
    "steps_done",
    "steps_total",
    "codes",
    "label",
    "blocked",
    "created_by",
    "created_at",
    "updated_at",
)
DETAIL_KEYS = (*ITEM_KEYS, "description_md", "outcome_md", "external_ref", "links", "blockers", "events", "comments")
BLOCKER_KEYS = (
    "id",
    "kind",
    "reason",
    "waiting_kind",
    "waiting_on",
    "since",
    "set_by",
    "cleared_at",
    "cleared_by",
    "decision_id",
    "run_id",
)
IMPORT_KEYS = ("dry_run", "tasks", "sprints", "links", "created", "skipped", "problems")
DETAIL = returns_object(*DETAIL_KEYS, schema="TasksItemDetail")
FIELDS = {  # option -> the field of a task it sets
    "title": "title",
    "description": "description_md",
    "outcome": "outcome_md",
    "type": "type",
    "status": "status",
    "priority": "priority",
    "due": "due_on",
    "sprint": "sprint",
    "parent": "parent",
}
CLEARS = (("no_due", "due_on"), ("no_sprint", "sprint"), ("no_parent", "parent"))  # an edit's --no-<x> and its field

PROJECT = Arg("--project", required=True, metavar="PROJECT", help="the project that enabled plugin tasks")
REF = Arg("ref", metavar="REF", help="the task's number, as T-42 or 42")


def task_options(*, new: bool) -> tuple[Arg, ...]:
    """The options of a task that ``create`` and ``edit`` share; ``--title`` is required of a new one."""
    return (
        Arg("--title", required=new, metavar="TEXT"),
        Arg("--description", metavar="MARKDOWN"),
        Arg(
            "--outcome",
            metavar="MARKDOWN",
            help="what the task came to, read by pm first; - reads stdin, empty clears it",
        ),
        Arg("--type", metavar="TYPE", help="one of the project's types (default: its first)"),
        Arg("--status", metavar="KEY", help="one of the project's statuses (default: its first)"),
        Arg("--priority", choices=PRIORITIES),
        Arg("--due", metavar="YYYY-MM-DD", help="the due date"),
        Arg("--sprint", metavar="NAME", help="a sprint of the project that is not closed"),
        Arg("--parent", metavar="REF", help="the task it is part of"),
        Arg("--assignee", action="append", metavar="LOGIN", help="an assignee; repeat for each"),
        Arg("--role", action="append", metavar="ROLE=LOGIN", help="ba, qa or dev; repeat for each"),
        Arg("--use-case", action="append", metavar="CODE", help="link an SRS item by code; repeatable"),
        Arg("--step", action="append", metavar="PLAN#STEP", help="link a plan step; repeatable"),
        Arg("--run", action="append", type=int, metavar="ID", help="link a run; repeatable"),
        Arg("--pull", action="append", metavar="URL", help="link a pull or merge request; repeatable"),
    )


class Tasks(ProjectResource):
    """The routes of the plugin tasks for one project, under /v1/x/tasks/<project>/ (Gateway). A refusal prints each
    problem the hub listed on stderr, then raises."""

    collection = "tasks"

    def path(self, *parts: Any, **query: Any) -> str:
        path = "/v1/x/tasks/" + "/".join(quote(str(part), safe="") for part in (self.project, *parts))
        given = {key: value for key, value in query.items() if value not in (None, False)}
        return f"{path}?{urlencode(given)}" if given else path

    def call(self, method: str, *parts: Any, body: dict | None = None, **query: Any) -> Any:
        try:
            return self.hub.call(method, self.path(*parts, **query), body)
        except HubError as exc:
            for line in problem_lines(exc):
                print(line, file=sys.stderr)
            raise

    def list(self, query: dict) -> list[dict]:
        return self.call("GET", "tasks", **query)

    def get(self, ref: str) -> dict:
        return self.call("GET", "tasks", ref)

    def create(self, body: dict) -> dict:
        return self.call("POST", "tasks", body=body)

    def edit(self, ref: str, body: dict) -> dict:
        return self.call("PATCH", "tasks", ref, body=body)

    def block(self, ref: str, body: dict) -> dict:
        return self.call("POST", "tasks", ref, "blockers", body=body)

    def clear(self, ref: str, blocker: int) -> dict:
        return self.call("PATCH", "tasks", ref, "blockers", blocker, body={"cleared": True})

    def settings(self) -> dict:
        return self.call("GET", "settings")

    def people(self) -> list[dict]:
        return self.call("GET", "people")

    def import_(self, body: dict) -> dict:
        return self.call("POST", "import", body=body)


def problem_lines(exc: HubError) -> list[str]:
    """Each problem a refusal of the hub listed, one line each."""
    detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
    lines = []
    for problem in detail or []:
        if isinstance(problem, dict) and "level" in problem:
            task = f" {problem['task']}" if problem.get("task") else ""
            lines.append(f"  {problem['level']}{task}: {problem['message']}")
    return lines


# What a person reads


def people_text(task: dict) -> str:
    slots = [f"assignees {', '.join(task['assignees'])}"] if task["assignees"] else []
    slots += [f"{role} {', '.join(task['roles'][role])}" for role in ROLES if task["roles"][role]]
    return "; ".join(slots)


def link_line(link: dict) -> str:
    if link["hidden"]:
        return f"#{link['id']} {link['kind'].replace('_', ' ')}: hidden, above what you may read"
    if link["kind"] == "srs_item":
        retired = ", dropped" if link["retired"] else ""
        return f"#{link['id']} {link['code']}: {link['item_title']} ({link['document']}{retired})"
    if link["kind"] == "plan_step":
        runs = ", ".join(f"run {run['id']} {run['state']}" for run in link["runs"])
        step = f"#{link['id']} plan {link['plan_id']} step {link['step']}: {link['step_title'] or '?'}"
        return f"{step}, {link['step_status'] or 'not in the plan'}" + (f" ({runs})" if runs else "")
    if link["kind"] == "run":
        return f"#{link['id']} run {link['run_id']}: {link['run_kind'] or '?'}, {link['run_state'] or '?'}"
    return f"#{link['id']} pull {link['url']}"


def blocker_line(blocker: dict) -> str:
    on = f"{blocker['waiting_kind']} {blocker['waiting_on']}"
    if blocker["kind"] == "decision":
        return f"Blocked: decision {blocker['decision_id']} of run {blocker['run_id']} waits on {on} since {when(blocker['since'])}"
    return f"Blocked #{blocker['id']}: waiting on {on} since {when(blocker['since'])}: {blocker['reason']}"


def task_head(task: dict) -> list[str]:
    """The lines of a task above its links: its title, its facts, its people, its description and outcome."""
    facts = [f"status {task['status']}" + (f" ({task['status_group']})" if task["status_group"] else "")]
    facts.append(f"priority {task['priority']}")
    facts += [f"due {task['due_on']}"] if task["due_on"] else []
    facts += [f"sprint {task['sprint']}"] if task["sprint"] else []
    facts += [f"part of {task['parent']}"] if task["parent"] else []
    lines = [f"{task['ref']} [{task['type']}] {task['title']}", "  " + ", ".join(facts)]
    if people_text(task):
        lines.append(f"  {people_text(task)}")
    if task["description_md"]:
        lines += [f"  | {line}" for line in task["description_md"].splitlines()]
    if task["outcome_md"]:
        lines.append("  Outcome:")
        lines += [f"  | {line}" for line in task["outcome_md"].splitlines()]
    return lines


def task_lines(task: dict) -> list[str]:
    """A task whole, as ``show`` prints it."""
    lines = task_head(task)
    lines += [f"  {blocker_line(blocker)}" for blocker in task["blockers"]]
    if task["links"]:
        lines.append("Links:")
        lines += [f"  {link_line(link)}" for link in task["links"]]
    lines.append("History:")
    for event in task["events"]:
        change = " -> ".join(str(v) for v in (event["from_value"], event["to_value"]) if v is not None)
        cause = f" ({event['cause']})" if event["cause"] else ""
        lines.append(f"  {when(event['at'])} {event['actor'] or 'hub'} {event['field']} {change}".rstrip() + cause)
    lines += [f"  {when(c['created_at'])} {c['author']} said: {c['body']}" for c in task["comments"]]
    return lines


def import_lines(done: dict) -> list[str]:
    counts = f"{done['tasks']} tasks, {done['sprints']} sprints, {done['links']} links"
    if done["dry_run"]:
        lines = [f"Would import {counts}."]
    else:
        lines = [f"Imported {counts}: {', '.join(done['created']) or 'none'}."]
    lines += [f"  skipped {skip['key']}: imported before as {skip['task']}" for skip in done["skipped"]]
    for problem in done["problems"]:
        task = f" {problem['task']}" if problem["task"] else ""
        lines.append(f"  {problem['level']}{task}: {problem['message']}")
    return lines


# What the options say


def roles_of(given: list[str]) -> dict[str, list[str]]:
    """``--role ROLE=LOGIN`` given as many times as wanted: role -> its logins; ``qa=`` empties qa."""
    roles: dict[str, list[str]] = {}
    for item in given:
        role, sep, login = item.partition("=")
        role = role.strip().lower()
        if not sep or role not in ROLES:
            raise HubError(f"--role takes ROLE=LOGIN with ROLE one of {', '.join(ROLES)}; {item!r} is not")
        roles.setdefault(role, [])
        if login.strip():
            roles[role].append(login.strip())
    return roles


def links_of(args: argparse.Namespace) -> dict:
    return {
        "use_cases": args.use_case or [],
        "steps": args.step or [],
        "runs": args.run or [],
        "pulls": args.pull or [],
    }


def fields_of(args: argparse.Namespace) -> dict:
    found = {key: getattr(args, name) for name, key in FIELDS.items() if getattr(args, name) is not None}
    if found.get("outcome_md") == "-":
        found["outcome_md"] = sys.stdin.read()
    return found


def waiting_of(args: argparse.Namespace, tasks: Tasks) -> dict:
    """The body fields for what ``--waiting-on`` names: a task of the project when it has the shape of its refs, a
    member when it is the login of someone with a grant on it, else a text; ``--text`` makes it a text."""
    given = args.waiting_on.strip()
    if args.text:
        return {"waiting_text": given}
    prefix = tasks.settings()["prefix"]
    if re.fullmatch(rf"{re.escape(prefix)}-[0-9]+", given, re.IGNORECASE):
        return {"waiting_task": given}
    if any(person["login"] == given for person in tasks.people()):
        return {"waiting_member": given}
    return {"waiting_text": given}


def read_json(file: str) -> Any:
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


# The commands


class TaskList(HubCommand):
    name = "list"
    help = "the tasks the filters keep, by number"
    arguments = (
        PROJECT,
        Arg("--mine", action="store_true", help="the tasks you stand in a slot of"),
        Arg("--sprint", metavar="NAME", help="the tasks of this sprint"),
        Arg("--backlog", action="store_true", help="the tasks of no sprint"),
        Arg("--status", metavar="KEY", help="the tasks of this status"),
        Arg("--assignee", metavar="LOGIN", help="the tasks this member is assigned"),
        Arg("--blocked", action="store_true", help="the tasks that are blocked"),
    )
    output = returns_array(*ITEM_KEYS, schema="TasksItem")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        query = {
            "status": args.status,
            "sprint": args.sprint,
            "backlog": "true" if args.backlog else None,
            "mine": "true" if args.mine else None,
            "assignee": args.assignee,
            "blocked": "true" if args.blocked else None,
        }
        found = Tasks.of(ctx).list(query)
        rows = [
            (
                t["ref"],
                t["status"] + (" (blocked)" if t["blocked"] else ""),
                t["priority"],
                ", ".join(t["assignees"]) or "-",
                t["sprint"] or "-",
                f"{t['steps_done']}/{t['steps_total']}" if t["steps_total"] else "-",
                t["title"],
            )
            for t in found
        ]
        header = ("TASK", "STATUS", "PRIORITY", "ASSIGNEES", "SPRINT", "STEPS", "TITLE")
        return Result(found, Table(header, rows, f"No task in project {ctx.project} that the filters keep."))


class TaskShow(HubCommand):
    name = "show"
    help = "a task whole: slots, links as they are now, history, comments"
    arguments = (REF, PROJECT)
    output = DETAIL

    def run(self, ctx: HubContext) -> Result:
        found = Tasks.of(ctx).get(ctx.args.ref)
        return Result(found, Text(*task_lines(found)))


class TaskCreate(HubCommand):
    name = "create"
    help = "a new task, with its people and links"
    arguments = (PROJECT, *task_options(new=True))
    output = DETAIL

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {**fields_of(args), "roles": roles_of(args.role or []), "links": links_of(args)}
        if args.assignee:
            body["assignees"] = args.assignee
        found = Tasks.of(ctx).create(body)
        return Result(found, Text(f"Created {found['ref']}.", *task_lines(found)))


class TaskEdit(HubCommand):
    name = "edit"
    help = "change a task: fields, people, links"
    arguments = (
        REF,
        PROJECT,
        *task_options(new=False),
        Arg("--no-due", action="store_true", help="clear the due date"),
        Arg("--no-sprint", action="store_true", help="take it out of its sprint"),
        Arg("--no-parent", action="store_true", help="make it part of no task"),
        Arg("--no-assignee", action="store_true", help="leave it unassigned"),
        Arg("--unlink", action="append", type=int, metavar="LINK_ID", help="drop a link; repeatable"),
    )
    output = DETAIL

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {
            **fields_of(args),
            "roles": roles_of(args.role or []),
            "links": links_of(args),
            "unlink": args.unlink or [],
        }
        for flag, key in CLEARS:
            if getattr(args, flag):
                if key in body:
                    raise UsageError(f"--{flag.replace('_', '-')} and the value it clears go apart")
                body[key] = None
        if args.assignee is not None or args.no_assignee:
            body["assignees"] = [] if args.no_assignee else args.assignee
        found = Tasks.of(ctx).edit(args.ref, body)
        return Result(found, Text(*task_lines(found)))


class TaskBlock(HubCommand):
    name = "block"
    help = "block a task: why, and who or what it waits on (a flag, not a status)"
    arguments = (
        REF,
        PROJECT,
        Arg("--waiting-on", required=True, metavar="X", help="a task (T-12), a member's login, or a text"),
        Arg("--reason", required=True, metavar="TEXT", help="why the task cannot go on"),
        Arg("--text", action="store_true", help="take --waiting-on as a text, whatever it looks like"),
    )
    output = returns_object(*BLOCKER_KEYS, schema="TasksBlocker")

    def run(self, ctx: HubContext) -> Result:
        args, tasks = ctx.args, Tasks.of(ctx)
        found = tasks.block(args.ref, {"reason": args.reason, **waiting_of(args, tasks)})
        said = f"Blocked {args.ref}: waiting on {found['waiting_kind']} {found['waiting_on']} (blocker #{found['id']})."
        return Result(found, Text(said))


class TaskUnblock(HubCommand):
    name = "unblock"
    help = "clear the blockers set by hand, or one with --blocker"
    arguments = (REF, PROJECT, Arg("--blocker", type=int, metavar="ID", help="clear this blocker only"))
    output = returns_array(*BLOCKER_KEYS, schema="TasksBlocker")

    def run(self, ctx: HubContext) -> Result:
        args, tasks = ctx.args, Tasks.of(ctx)
        if args.blocker is not None:
            ids = [args.blocker]
        else:
            ids = [b["id"] for b in tasks.get(args.ref)["blockers"] if b["kind"] == "manual"]
        cleared = [tasks.clear(args.ref, id_) for id_ in ids]
        if cleared:
            said = f"Cleared {len(cleared)} blocker(s) of {args.ref}: {', '.join('#' + str(b['id']) for b in cleared)}."
        else:
            said = f"{args.ref} has no blocker set by hand that is open."
        return Result(cleared, Text(said))


class TaskImport(HubCommand):
    name = "import"
    help = "evo-tasks/1 JSON, once: sprints and tasks linked by use case code"
    arguments = (
        Arg("file", metavar="FILE", help="the JSON file; - for stdin"),
        PROJECT,
        Arg("--dry-run", action="store_true", help="print what it would import and the problems"),
    )
    output = returns_object(*IMPORT_KEYS, schema="TasksImportResult")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        data = read_json(args.file)
        done = Tasks.of(ctx).import_({"data": data, "dry_run": args.dry_run})
        return Result(done, Text(*import_lines(done)))


TASK = Group(
    "task",
    "tasks of a project: backlog, sprints, people and links (plugin tasks)",
    [TaskList(), TaskShow(), TaskCreate(), TaskEdit(), TaskBlock(), TaskUnblock(), TaskImport()],
)

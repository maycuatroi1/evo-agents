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
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

EXIT_USAGE = 2
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


def _path(project: str, *parts: str, query: dict | None = None) -> str:
    path = "/v1/x/tasks/" + "/".join(quote(str(part), safe="") for part in (project, *parts))
    given = {key: value for key, value in (query or {}).items() if value not in (None, False)}
    return f"{path}?{urlencode(given)}" if given else path


def _problems(exc: HubError) -> None:
    """Each problem a refusal of the hub listed, one line each, on stderr."""
    detail = exc.payload.get("detail") if isinstance(exc.payload, dict) else None
    for problem in detail or []:
        if isinstance(problem, dict) and "level" in problem:
            task = f" {problem['task']}" if problem.get("task") else ""
            print(f"  {problem['level']}{task}: {problem['message']}", file=sys.stderr)


def _call(method: str, path: str, body: dict | None = None):
    hub, _ = signed_in()
    try:
        return hub.call(method, path, body)
    except HubError as exc:
        _problems(exc)
        raise


def _people(task: dict) -> str:
    slots = [f"assignees {', '.join(task['assignees'])}"] if task["assignees"] else []
    slots += [f"{role} {', '.join(task['roles'][role])}" for role in ROLES if task["roles"][role]]
    return "; ".join(slots)


@client_command
def cmd_list(args) -> int:
    query = {
        "status": args.status,
        "sprint": args.sprint,
        "backlog": "true" if args.backlog else None,
        "mine": "true" if args.mine else None,
        "assignee": args.assignee,
        "blocked": "true" if args.blocked else None,
    }
    found = _call("GET", _path(args.project, "tasks", query=query))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No task in project {args.project} that the filters keep.")
        return 0
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
    table(("TASK", "STATUS", "PRIORITY", "ASSIGNEES", "SPRINT", "STEPS", "TITLE"), rows)
    return 0


def _link_line(link: dict) -> str:
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


def _blocker_line(blocker: dict) -> str:
    on = f"{blocker['waiting_kind']} {blocker['waiting_on']}"
    if blocker["kind"] == "decision":
        return f"Blocked: decision {blocker['decision_id']} of run {blocker['run_id']} waits on {on} since {when(blocker['since'])}"
    return f"Blocked #{blocker['id']}: waiting on {on} since {when(blocker['since'])}: {blocker['reason']}"


def _print_task(task: dict) -> None:
    print(f"{task['ref']} [{task['type']}] {task['title']}")
    facts = [f"status {task['status']}" + (f" ({task['status_group']})" if task["status_group"] else "")]
    facts.append(f"priority {task['priority']}")
    facts += [f"due {task['due_on']}"] if task["due_on"] else []
    facts += [f"sprint {task['sprint']}"] if task["sprint"] else []
    facts += [f"part of {task['parent']}"] if task["parent"] else []
    print("  " + ", ".join(facts))
    if _people(task):
        print(f"  {_people(task)}")
    if task["description_md"]:
        for line in task["description_md"].splitlines():
            print(f"  | {line}")
    if task["outcome_md"]:
        print("  Outcome:")
        for line in task["outcome_md"].splitlines():
            print(f"  | {line}")
    for blocker in task["blockers"]:
        print(f"  {_blocker_line(blocker)}")
    if task["links"]:
        print("Links:")
        for link in task["links"]:
            print(f"  {_link_line(link)}")
    print("History:")
    for event in task["events"]:
        change = " -> ".join(str(v) for v in (event["from_value"], event["to_value"]) if v is not None)
        cause = f" ({event['cause']})" if event["cause"] else ""
        print(f"  {when(event['at'])} {event['actor'] or 'hub'} {event['field']} {change}".rstrip() + cause)
    for comment in task["comments"]:
        print(f"  {when(comment['created_at'])} {comment['author']} said: {comment['body']}")


@client_command
def cmd_show(args) -> int:
    found = _call("GET", _path(args.project, "tasks", args.ref))
    if args.json:
        print_json(found)
    else:
        _print_task(found)
    return 0


def _roles(given: list[str]) -> dict[str, list[str]]:
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


def _links(args) -> dict:
    return {
        "use_cases": args.use_case or [],
        "steps": args.step or [],
        "runs": args.run or [],
        "pulls": args.pull or [],
    }


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


def _fields(args) -> dict:
    found = {key: getattr(args, name) for name, key in FIELDS.items() if getattr(args, name) is not None}
    if found.get("outcome_md") == "-":
        found["outcome_md"] = sys.stdin.read()
    return found


@client_command
def cmd_create(args) -> int:
    body = {**_fields(args), "roles": _roles(args.role or []), "links": _links(args)}
    if args.assignee:
        body["assignees"] = args.assignee
    found = _call("POST", _path(args.project, "tasks"), body)
    if args.json:
        print_json(found)
    else:
        print(f"Created {found['ref']}.")
        _print_task(found)
    return 0


@client_command
def cmd_edit(args) -> int:
    body = {**_fields(args), "roles": _roles(args.role or []), "links": _links(args), "unlink": args.unlink or []}
    for flag, key in (("no_due", "due_on"), ("no_sprint", "sprint"), ("no_parent", "parent")):
        if getattr(args, flag):
            if key in body:
                print(f"error: --{flag.replace('_', '-')} and the value it clears go apart", file=sys.stderr)
                return EXIT_USAGE
            body[key] = None
    if args.assignee is not None or args.no_assignee:
        body["assignees"] = [] if args.no_assignee else args.assignee
    found = _call("PATCH", _path(args.project, "tasks", args.ref), body)
    if args.json:
        print_json(found)
    else:
        _print_task(found)
    return 0


def _waiting(args) -> dict:
    """The body fields for what ``--waiting-on`` names: a task of the project when it has the shape of its refs, a
    member when it is the login of someone with a grant on it, else a text; ``--text`` makes it a text."""
    given = args.waiting_on.strip()
    if args.text:
        return {"waiting_text": given}
    prefix = _call("GET", _path(args.project, "settings"))["prefix"]
    if re.fullmatch(rf"{re.escape(prefix)}-[0-9]+", given, re.IGNORECASE):
        return {"waiting_task": given}
    people = _call("GET", _path(args.project, "people"))
    if any(person["login"] == given for person in people):
        return {"waiting_member": given}
    return {"waiting_text": given}


@client_command
def cmd_block(args) -> int:
    body = {"reason": args.reason, **_waiting(args)}
    found = _call("POST", _path(args.project, "tasks", args.ref, "blockers"), body)
    if args.json:
        print_json(found)
    else:
        print(f"Blocked {args.ref}: waiting on {found['waiting_kind']} {found['waiting_on']} (blocker #{found['id']}).")
    return 0


@client_command
def cmd_unblock(args) -> int:
    if args.blocker is not None:
        ids = [args.blocker]
    else:
        task = _call("GET", _path(args.project, "tasks", args.ref))
        ids = [b["id"] for b in task["blockers"] if b["kind"] == "manual"]
    cleared = [
        _call("PATCH", _path(args.project, "tasks", args.ref, "blockers", id_), {"cleared": True}) for id_ in ids
    ]
    if args.json:
        print_json(cleared)
    elif cleared:
        print(f"Cleared {len(cleared)} blocker(s) of {args.ref}: {', '.join('#' + str(b['id']) for b in cleared)}.")
    else:
        print(f"{args.ref} has no blocker set by hand that is open.")
    return 0


def _read(file: str):
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


def _print_import(done: dict) -> None:
    counts = f"{done['tasks']} tasks, {done['sprints']} sprints, {done['links']} links"
    if done["dry_run"]:
        print(f"Would import {counts}.")
    else:
        print(f"Imported {counts}: {', '.join(done['created']) or 'none'}.")
    for skip in done["skipped"]:
        print(f"  skipped {skip['key']}: imported before as {skip['task']}")
    for problem in done["problems"]:
        task = f" {problem['task']}" if problem["task"] else ""
        print(f"  {problem['level']}{task}: {problem['message']}")


@client_command
def cmd_import(args) -> int:
    done = _call("POST", _path(args.project, "import"), {"data": _read(args.file), "dry_run": args.dry_run})
    if args.json:
        print_json(done)
    else:
        _print_import(done)
    return 0


def _project(parser) -> None:
    parser.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin tasks")


def _task_options(parser, *, new: bool) -> None:
    parser.add_argument("--title", required=new, metavar="TEXT")
    parser.add_argument("--description", metavar="MARKDOWN")
    parser.add_argument(
        "--outcome", metavar="MARKDOWN", help="what the task came to, read by pm first; - reads stdin, empty clears it"
    )
    parser.add_argument("--type", metavar="TYPE", help="one of the project's types (default: its first)")
    parser.add_argument("--status", metavar="KEY", help="one of the project's statuses (default: its first)")
    parser.add_argument("--priority", choices=PRIORITIES)
    parser.add_argument("--due", metavar="YYYY-MM-DD", help="the due date")
    parser.add_argument("--sprint", metavar="NAME", help="a sprint of the project that is not closed")
    parser.add_argument("--parent", metavar="REF", help="the task it is part of")
    parser.add_argument("--assignee", action="append", metavar="LOGIN", help="an assignee; repeat for each")
    parser.add_argument("--role", action="append", metavar="ROLE=LOGIN", help="ba, qa or dev; repeat for each")
    parser.add_argument("--use-case", action="append", metavar="CODE", help="link an SRS item by code; repeatable")
    parser.add_argument("--step", action="append", metavar="PLAN#STEP", help="link a plan step; repeatable")
    parser.add_argument("--run", action="append", type=int, metavar="ID", help="link a run; repeatable")
    parser.add_argument("--pull", action="append", metavar="URL", help="link a pull or merge request; repeatable")


def _register_blockers(tsub) -> None:
    block = tsub.add_parser("block", help="block a task: why, and who or what it waits on (a flag, not a status)")
    block.add_argument("ref", metavar="REF", help="the task's number, as T-42 or 42")
    _project(block)
    block.add_argument("--waiting-on", required=True, metavar="X", help="a task (T-12), a member's login, or a text")
    block.add_argument("--reason", required=True, metavar="TEXT", help="why the task cannot go on")
    block.add_argument("--text", action="store_true", help="take --waiting-on as a text, whatever it looks like")
    json_option(block, returns_object(*BLOCKER_KEYS, schema="TasksBlocker"))
    block.set_defaults(func=cmd_block)

    unblock = tsub.add_parser("unblock", help="clear the blockers set by hand, or one with --blocker")
    unblock.add_argument("ref", metavar="REF", help="the task's number, as T-42 or 42")
    _project(unblock)
    unblock.add_argument("--blocker", type=int, metavar="ID", help="clear this blocker only")
    json_option(unblock, returns_array(*BLOCKER_KEYS, schema="TasksBlocker"))
    unblock.set_defaults(func=cmd_unblock)


def register_tasks(hsub) -> None:
    task = hsub.add_parser("task", help="tasks of a project: backlog, sprints, people and links (plugin tasks)")
    tsub = task.add_subparsers(dest="task_command", required=True)

    listed = tsub.add_parser("list", help="the tasks the filters keep, by number")
    _project(listed)
    listed.add_argument("--mine", action="store_true", help="the tasks you stand in a slot of")
    listed.add_argument("--sprint", metavar="NAME", help="the tasks of this sprint")
    listed.add_argument("--backlog", action="store_true", help="the tasks of no sprint")
    listed.add_argument("--status", metavar="KEY", help="the tasks of this status")
    listed.add_argument("--assignee", metavar="LOGIN", help="the tasks this member is assigned")
    listed.add_argument("--blocked", action="store_true", help="the tasks that are blocked")
    json_option(listed, returns_array(*ITEM_KEYS, schema="TasksItem"))
    listed.set_defaults(func=cmd_list)

    show = tsub.add_parser("show", help="a task whole: slots, links as they are now, history, comments")
    show.add_argument("ref", metavar="REF", help="the task's number, as T-42 or 42")
    _project(show)
    json_option(show, returns_object(*DETAIL_KEYS, schema="TasksItemDetail"))
    show.set_defaults(func=cmd_show)

    create = tsub.add_parser("create", help="a new task, with its people and links")
    _project(create)
    _task_options(create, new=True)
    json_option(create, returns_object(*DETAIL_KEYS, schema="TasksItemDetail"))
    create.set_defaults(func=cmd_create)

    edit = tsub.add_parser("edit", help="change a task: fields, people, links")
    edit.add_argument("ref", metavar="REF", help="the task's number, as T-42 or 42")
    _project(edit)
    _task_options(edit, new=False)
    edit.add_argument("--no-due", action="store_true", help="clear the due date")
    edit.add_argument("--no-sprint", action="store_true", help="take it out of its sprint")
    edit.add_argument("--no-parent", action="store_true", help="make it part of no task")
    edit.add_argument("--no-assignee", action="store_true", help="leave it unassigned")
    edit.add_argument("--unlink", action="append", type=int, metavar="LINK_ID", help="drop a link; repeatable")
    json_option(edit, returns_object(*DETAIL_KEYS, schema="TasksItemDetail"))
    edit.set_defaults(func=cmd_edit)

    _register_blockers(tsub)

    imported = tsub.add_parser("import", help="evo-tasks/1 JSON, once: sprints and tasks linked by use case code")
    imported.add_argument("file", metavar="FILE", help="the JSON file; - for stdin")
    _project(imported)
    imported.add_argument("--dry-run", action="store_true", help="print what it would import and the problems")
    json_option(imported, returns_object(*IMPORT_KEYS, schema="TasksImportResult"))
    imported.set_defaults(func=cmd_import)

"""``evo-agents hub curator``: the night shift of a project, as its charter sets it (``evo_agents.hub.curator``).

``curator charter show`` prints the charter at its newest revision, or ``--revision N`` (GET
/v1/projects/{p}/curator/charter); ``--json`` prints it whole, with ``auto_merge`` and ``judge.runtime`` among its keys.
``curator charter set FILE`` writes a new revision from a YAML or JSON file (``-`` reads stdin), which only an admin of
the project may (PUT .../curator/charter); the keys ``show --json`` adds to the body are left out, so what was shown can
be edited and written back. ``curator charter history`` lists every revision.

``curator status`` shows where the night shift stands: the charter's revision, the schedules, whether they are paused,
and the night now with its runs and cost (GET .../curator). ``curator pause`` pauses every schedule of the project and
cancels the runs they queued that are still queued; within a minute nothing of the project's night shift moves.
``curator resume`` lets them run again. Either takes an admin of the project, or the member a schedule dispatches as.

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library and PyYAML only, like the client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlencode

import yaml

from evo_agents.hub.cli_client import _client_command, _print_json, _project_path, _signed_in, _table, _when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.curator import CHARTER_META, money
from evo_agents.hub.plan_cli import _project

# The keys of the hub's answers these commands print with --json (the contract, `evo-agents hub contract print`).
CHARTER_BODY_KEYS = (
    "goals",
    "window",
    "worker",
    "night_budget_usd",
    "run_budget_usd",
    "run_max_turns",
    "run_minutes",
    "max_runs_per_night",
    "night_plans",
    "max_decisions_per_day",
    "brief_at",
    "auto_merge",
    "protected_paths",
    "circuit_breaker",
    "reviewer",
    "builder",
    "judge",
)
CHARTER_KEYS = CHARTER_BODY_KEYS + CHARTER_META
REVISION_KEYS = ("revision", "updated_by", "updated_at", "worker", "worker_id")
STATUS_KEYS = ("project", "charter", "paused", "schedules", "night")
STATUS = returns_object(*STATUS_KEYS, schema="CuratorStatus")
CHARTER = returns_object(*CHARTER_KEYS, schema="Charter")


def _curator_path(project: str, *parts: str) -> str:
    return "/".join((f"{_project_path(project)}/curator", *parts))


def _read_charter(source: str) -> dict:
    """The charter in the YAML or JSON file ``source`` (``-``: stdin), without the keys ``show --json`` adds."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise HubError(f"cannot read {source}: {exc.strerror or exc}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HubError(f"{source} is neither YAML nor JSON: {exc}") from None
    if not isinstance(data, dict):
        raise HubError(f"{source} holds no mapping: a charter is one, with window, worker and night_budget_usd")
    return {key: value for key, value in data.items() if key not in CHARTER_META}


def _role(role: dict) -> str:
    return role["runtime"] + (f" ({role['model']})" if role.get("model") else "")


def _print_charter(charter: dict) -> None:
    window = charter["window"]
    print(
        f"Charter of {charter['project']}, revision {charter['revision']}, by {charter['updated_by']} at "
        f"{_when(charter['updated_at'])} UTC"
    )
    run_cap = money(charter["run_budget_usd"]) if charter["run_budget_usd"] is not None else "what the night has left"
    lines = [
        ("window", f"{window['start']} to {window['end']}, {window['timezone']}"),
        ("worker", f"{charter['worker']} (#{charter['worker_id']}), runs dispatched as {charter['schedule_owner']}"),
        ("night", f"{money(charter['night_budget_usd'])}, at most {charter['max_runs_per_night']} runs"),
        ("each run", f"{run_cap}, {charter['run_max_turns']} turns, {charter['run_minutes']} minutes of agent time"),
        ("plans", ", ".join(charter["night_plans"]) or "none"),
        ("decisions", f"at most {charter['max_decisions_per_day']} a day, brief at {charter['brief_at']}"),
        ("auto merge", ", ".join(f"tier {tier}" for tier in charter["auto_merge"]) or "none"),
        ("protected", ", ".join(charter["protected_paths"]) or "none"),
        ("breaker", f"{charter['circuit_breaker']['max_failed_in_a_row']} failed in a row"),
        ("reviewer", _role(charter["reviewer"])),
        ("builder", _role(charter["builder"])),
        ("judge", _role(charter["judge"])),
    ]
    checks = charter["judge"].get("hidden_checks")
    lines.append(("hidden checks", "shown to admins only" if checks is None else str(len(checks))))
    width = max(len(label) for label, _ in lines)
    for label, text in lines:
        print(f"  {label:<{width}}  {text}")
    for goal in charter["goals"]:
        print(f"  goal {goal['id']}: {goal['what']}")


@_client_command
def cmd_charter_show(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    query = f"?{urlencode({'revision': args.revision})}" if args.revision is not None else ""
    charter = hub.call("GET", _curator_path(project, "charter") + query)
    if args.json:
        _print_json(charter)
        return 0
    _print_charter(charter)
    return 0


@_client_command
def cmd_charter_set(args) -> int:
    body = _read_charter(args.file)
    hub, _ = _signed_in()
    project = _project(args)
    charter = hub.call("PUT", _curator_path(project, "charter"), body)
    if args.json:
        _print_json(charter)
        return 0
    print(f"Charter of {charter['project']} is at revision {charter['revision']}.")
    print(
        f"The night shift runs {', '.join(charter['night_plans']) or 'no plan'} on {charter['worker']} as "
        f"{charter['schedule_owner']}, {charter['window']['start']} to {charter['window']['end']} "
        f"{charter['window']['timezone']}."
    )
    return 0


@_client_command
def cmd_charter_history(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    revisions = hub.call("GET", _curator_path(project, "charter", "revisions"))
    if args.json:
        _print_json(revisions)
        return 0
    rows = [(row["revision"], row["updated_by"], _when(row["updated_at"]), row["worker"]) for row in revisions]
    if rows:
        _table(("REVISION", "BY", "AT (UTC)", "WORKER"), rows)
    print(f"{len(rows)} revision(s) of the charter of {project}")
    return 0


def _print_status(status: dict) -> None:
    charter = status["charter"]
    if charter is None:
        print(
            f"Project {status['project']} has no charter: an admin writes one with `evo-agents hub curator charter set`"
        )
        return
    state = "paused" if status["paused"] else "on"
    print(f"Night shift of {status['project']}: {state}, charter revision {charter['revision']}")
    for schedule in status["schedules"]:
        paused = (
            f", paused by {schedule['paused_by']} at {_when(schedule['paused_at'])} UTC"
            if schedule["paused_at"]
            else ""
        )
        print(f"  {schedule['kind']}: worker {schedule['worker']}, runs dispatched as {schedule['owner']}{paused}")
    night = status["night"]
    if night is not None:
        where = "inside" if night["in_window"] else "outside"
        active = f", run #{night['active_run_id']} active" if night["active_run_id"] else ""
        print(
            f"  night of {night['night']} ({night['local_time']} local, {where} the window): {night['runs']} of "
            f"{night['max_runs']} runs, {money(night['cost_usd'])} of {money(night['budget_usd'])}{active}"
        )


@_client_command
def cmd_status(args) -> int:
    hub, _ = _signed_in()
    project = _project(args)
    status = hub.call("GET", _curator_path(project))
    if args.json:
        _print_json(status)
        return 0
    _print_status(status)
    return 0


def _switch(action: str):
    @_client_command
    def run(args) -> int:
        hub, _ = _signed_in()
        project = _project(args)
        status = hub.call("POST", _curator_path(project, action))
        if args.json:
            _print_json(status)
            return 0
        if action == "pause":
            print(f"Paused the night shift of {project}: it queues no run until `evo-agents hub curator resume`.")
        else:
            print(f"The night shift of {project} runs again inside its window.")
        _print_status(status)
        return 0

    return run


cmd_pause = _switch("pause")
cmd_resume = _switch("resume")


def register_curator(hsub) -> None:
    curator = hsub.add_parser("curator", help="the night shift of a project: its charter, status, pause and resume")
    csub = curator.add_subparsers(dest="curator_command", required=True)
    project_help = "hub project (default: hub.project in the harness.yaml around the current directory)"

    def with_project(parser) -> None:
        parser.add_argument("--project", help=project_help)

    charter = csub.add_parser("charter", help="the project's charter: show it, write a revision, list revisions")
    chsub = charter.add_subparsers(dest="charter_command", required=True)
    show = chsub.add_parser("show", help="the charter at its newest revision, or an older one")
    with_project(show)
    show.add_argument("--revision", type=int, metavar="N", help="an older revision")
    json_option(show, CHARTER)
    show.set_defaults(func=cmd_charter_show)

    write = chsub.add_parser("set", help="write a new revision from a YAML or JSON file; an admin of the project only")
    write.add_argument("file", metavar="FILE", help="the charter, YAML or JSON; - reads stdin")
    with_project(write)
    json_option(write, CHARTER)
    write.set_defaults(func=cmd_charter_set)

    history = chsub.add_parser("history", help="every revision of the charter, newest first")
    with_project(history)
    json_option(history, returns_array(*REVISION_KEYS, schema="CharterRevision"))
    history.set_defaults(func=cmd_charter_history)

    status = csub.add_parser("status", help="the charter, the schedules and the night now")
    with_project(status)
    json_option(status, STATUS)
    status.set_defaults(func=cmd_status)

    pause = csub.add_parser("pause", help="pause every schedule of the project and cancel the runs they still queue")
    with_project(pause)
    json_option(pause, STATUS)
    pause.set_defaults(func=cmd_pause)

    resume = csub.add_parser("resume", help="let the schedules of the project run again")
    with_project(resume)
    json_option(resume, STATUS)
    resume.set_defaults(func=cmd_resume)

"""``evo-agents hub pm daily``: the daily report of a project that enabled the core plugin pm, through its route
``GET /v1/x/pm/<project>/daily`` (``docs/pm-plugin.md``). The plugin's code is ``evo_agents.core_plugins.pm``, which this
client never imports: it speaks HTTP only.

``daily --project X`` prints the report of today in the project's time zone, ``--date YYYY-MM-DD`` that of another day:
what was done on the working day before, the sprint against what it held when it opened, the tasks that are late, in
review, stale or blocked, what to escalate, and the load of each member. ``--json`` prints what the hub answered. Standard
library only, like the rest of the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object

DAILY_KEYS = (
    "project",
    "date",
    "previous_working_day",
    "done_use_cases",
    "done_tasks",
    "sprint",
    "late",
    "review",
    "stale",
    "blocked",
    "escalate",
    "load",
    "hidden",
)


def _path(project: str, date: str | None) -> str:
    path = f"/v1/x/pm/{quote(project, safe='')}/daily"
    return f"{path}?{urlencode({'date': date})}" if date else path


def _names(items: list[dict], key: str = "ref") -> str:
    return ", ".join(item[key] for item in items) or "-"


def _print_sprint(sprint: dict | None) -> None:
    if sprint is None:
        print("Sprint: none active")
        return
    done = "-" if sprint["done_percent"] is None else f"{sprint['done_percent']:.0f}%"
    gone = "-" if sprint["time_percent"] is None else f"{sprint['time_percent']:.0f}%"
    print(f"Sprint {sprint['name']}: scope {sprint['scope']}, done {done}, time gone {gone}, ", end="")
    print(f"+{sprint['added']} added, -{sprint['removed']} removed")
    for case in sprint["use_cases"]:
        print(f"  {case['code']}  {case['done']}/{case['total']}  {case['title']}")


def _print_daily(report: dict) -> None:
    print(f"{report['project']}  {report['date']}  (since {report['previous_working_day']})")
    print(f"Done use cases: {_names(report['done_use_cases'], 'code')}")
    print(f"Done tasks: {_names(report['done_tasks'])}")
    _print_sprint(report["sprint"])
    print("Late:")
    for task in report["late"]:
        when = f"{task['days_late']} working days late" if task["kind"] == "overdue" else "at risk"
        print(f"  {task['ref']}  due {task['due_on']}  {when}  {task['title']}")
    print("In review:")
    for task in report["review"]:
        print(f"  {task['ref']}  {task['age_days']} working days  {task['title']}")
    print("Stale:")
    for task in report["stale"]:
        print(f"  {task['ref']}  idle {task['idle_days']} working days ({task['source']})  {task['title']}")
    print("Blocked:")
    for blocker in report["blocked"]:
        print(f"  {blocker['ref']}  waiting on {blocker['waiting_on']}: {blocker['reason']}")
    print("Escalate:")
    for blocker in report["escalate"]:
        print(f"  {blocker['ref']}  {blocker['age_days']} working days  waiting on {blocker['waiting_on']}")
    print("Load:")
    rows = [
        (m["login"], m["doing"], "overloaded" if m["overloaded"] else "empty" if m["empty"] else "ok", m["late"])
        for m in report["load"]
    ]
    if rows:
        table(("member", "doing", "state", "late"), rows)
    hidden = {name: count for name, count in report["hidden"].items() if count}
    if hidden:
        print("Hidden from you: " + ", ".join(f"{count} {name}" for name, count in hidden.items()))


@client_command
def cmd_daily(args) -> int:
    hub, _ = signed_in()
    report = hub.call("GET", _path(args.project, args.date))
    if args.json:
        print_json(report)
    else:
        _print_daily(report)
    return 0


def register_pm(hsub) -> None:
    pm = hsub.add_parser("pm", help="the daily report of a project (plugin pm)")
    psub = pm.add_subparsers(dest="pm_command", required=True)
    daily = psub.add_parser("daily", help="what was done, late, in review, stale, blocked and to escalate")
    daily.add_argument("--project", required=True, metavar="PROJECT", help="the project that enabled plugin pm")
    daily.add_argument(
        "--date", metavar="YYYY-MM-DD", help="the day of the report (default: today in the project's zone)"
    )
    json_option(daily, returns_object(*DAILY_KEYS, schema="PmDaily"))
    daily.set_defaults(func=cmd_daily)

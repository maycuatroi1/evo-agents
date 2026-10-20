"""``evo-agents hub curator replay``: the replays that measure the Curator's changes (``evo_agents.hub.replay``).

``curator replay tasks`` lists the project's suite of replay tasks, in the order they came in, of ``--split`` select or
holdout when given (GET /v1/projects/{p}/curator/replay/tasks): each a step of a plan done before, with its repo, its
verify and its base commit once a replay read it. ``curator replay show CHANGE`` shows the replays of one change of the
Curator (GET .../curator/changes/{id}/replay): for each task, whether arm a (the harness of the default branch) and arm
b (the harness of the change's branch) passed the step's verify, and what each cost; then both arms added up, and
whether the replays won at the head of the change's pull request, with why (``replay.verdict``).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.curator import money
from evo_agents.hub.plan_cli import project_of_args
from evo_agents.hub.replay import ARMS, SPLITS

TASK_LIST_KEYS = ("project", "split", "tasks", "total", "counts")
CHANGE_REPLAY_KEYS = ("project", "change_id", "tasks", "replays", "arms", "verdict")


def _path(project: str, *parts: str) -> str:
    return "/".join((f"/v1/projects/{quote(project, safe='')}/curator", *parts))


def _arm(item: dict | None) -> str:
    if not item:
        return "-"
    cost = money(item["cost_usd"]) if item.get("cost_usd") is not None else "-"
    return f"{'pass' if item.get('passed') else 'fail'} {cost}"


@client_command
def cmd_tasks(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    query = {"limit": args.limit, **({"split": args.split} if args.split else {})}
    found = hub.call("GET", _path(project, "replay", "tasks") + f"?{urlencode(query)}")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (f"#{task['id']}", task["split"], f"{task['plan_id']}#{task['step_key']}", task["repo"], task["title"] or "-")
        for task in found["tasks"]
    ]
    if rows:
        table(("ID", "SPLIT", "STEP", "REPO", "TITLE"), rows)
    counts = found["counts"]
    print(
        f"{len(rows)} of {found['total']} replay task(s) of {project}; the suite has {counts.get('select', 0)} select "
        f"and {counts.get('holdout', 0)} holdout"
    )
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    project = project_of_args(args)
    found = hub.call("GET", _path(project, "changes", str(args.change), "replay"))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (f"#{item['id']}", f"{item['plan_id']}#{item['step_key']}", item["state"], _arm(item["a"]), _arm(item["b"]))
        for item in found["replays"]
    ]
    if rows:
        table(("ID", "TASK", "STATE", "ARM A", "ARM B"), rows)
    print(f"{len(rows)} of {found['tasks']} replay(s) of the Curator's change #{found['change_id']} in {project}")
    for arm in ARMS:
        total = found["arms"][arm]
        cost = money(total["cost_usd"]) if total.get("cost_usd") is not None else "-"
        which = "the default branch's harness" if arm == "a" else "the change's harness"
        print(f"arm {arm} ({which}): {total['passed']} of {total['replays']} passed, {cost}")
    verdict = found.get("verdict") or {}
    if verdict:
        print(f"{'won' if verdict.get('won') else 'not won'}: {verdict.get('reason')}")
    for item in found["replays"]:
        if item["reason"]:
            print(f"#{item['id']}: {item['reason']}")
    return 0


def register_replay(csub, with_project) -> None:
    """``curator replay`` and its commands."""
    parser = csub.add_parser("replay", help="the replays of past steps that measure the Curator's changes")
    rsub = parser.add_subparsers(dest="replay_command", required=True)
    tasks = rsub.add_parser("tasks", help="the project's suite of replay tasks, in the order they came in")
    with_project(tasks)
    tasks.add_argument("--split", choices=SPLITS, help="the tasks of this split alone")
    tasks.add_argument("--limit", type=int, default=100, choices=range(1, 201), metavar="N", help="1 to 200")
    json_option(tasks, returns_object(*TASK_LIST_KEYS, schema="ReplayTaskList"))
    tasks.set_defaults(func=cmd_tasks)
    shown = rsub.add_parser("show", help="a change's replays: each task with arm a and arm b, then both added up")
    shown.add_argument("change", type=int, metavar="CHANGE", help="the change's id, as `curator changes` lists it")
    with_project(shown)
    json_option(shown, returns_object(*CHANGE_REPLAY_KEYS, schema="ChangeReplay"))
    shown.set_defaults(func=cmd_show)

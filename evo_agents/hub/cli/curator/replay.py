"""``evo-agents hub curator replay``: the replays that measure the Curator's changes (``evo_agents.hub.replay``).

``curator replay tasks`` lists the project's suite of replay tasks, in the order they came in, of ``--split`` select or
holdout when given (GET /v1/projects/{p}/curator/replay/tasks): each a step of a plan done before, with its repo, its
verify and its base commit once a replay read it. ``curator replay show CHANGE`` shows the replays of one change of the
Curator (GET .../curator/changes/{id}/replay): for each task, whether arm a (the harness of the default branch) and arm
b (the harness of the change's branch) passed the step's verify, and what each cost; then both arms added up, and
whether the replays won at the head of the change's pull request, with why (``replay.verdict``).

The project is ``--project``, or ``hub.project`` in the harness.yaml around the current directory. ``--json`` prints
what the hub answered, with the keys declared next to the flag. Standard library only, like the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes): :class:`Replays` is
the gateway to the routes.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import PROJECT, HubCommand, HubContext, ProjectResource
from evo_agents.hub.curator import money
from evo_agents.hub.replay import ARMS, SPLITS

TASK_LIST_KEYS = ("project", "split", "tasks", "total", "counts")
CHANGE_REPLAY_KEYS = ("project", "change_id", "tasks", "replays", "arms", "verdict")


class Replays(ProjectResource):
    """The replays of the Curator: GET /v1/projects/{p}/curator/replay/tasks and .../curator/changes/{id}/replay."""

    collection = "curator"

    def tasks(self, limit: int, split: str | None = None) -> dict:
        return self.call("GET", "replay", "tasks", limit=limit, split=split)

    def of_change(self, change: int) -> dict:
        return self.call("GET", "changes", change, "replay")


def arm_text(item: dict | None) -> str:
    """One arm of a replay: pass or fail, and its cost."""
    if not item:
        return "-"
    cost = money(item["cost_usd"]) if item.get("cost_usd") is not None else "-"
    return f"{'pass' if item.get('passed') else 'fail'} {cost}"


def replay_lines(found: dict, project: str) -> list[str]:
    """What ``replay show`` says under its table: the count, both arms added up, the verdict, each replay's reason."""
    change = found["change_id"]
    lines = [f"{len(found['replays'])} of {found['tasks']} replay(s) of the Curator's change #{change} in {project}"]
    for arm in ARMS:
        total = found["arms"][arm]
        cost = money(total["cost_usd"]) if total.get("cost_usd") is not None else "-"
        which = "the default branch's harness" if arm == "a" else "the change's harness"
        lines.append(f"arm {arm} ({which}): {total['passed']} of {total['replays']} passed, {cost}")
    verdict = found.get("verdict") or {}
    if verdict:
        lines.append(f"{'won' if verdict.get('won') else 'not won'}: {verdict.get('reason')}")
    lines += [f"#{item['id']}: {item['reason']}" for item in found["replays"] if item["reason"]]
    return lines


class ReplayTasks(HubCommand):
    name = "tasks"
    help = "the project's suite of replay tasks, in the order they came in"
    arguments = (
        PROJECT,
        Arg("--split", choices=SPLITS, help="the tasks of this split alone"),
        Arg("--limit", type=int, default=100, choices=range(1, 201), metavar="N", help="1 to 200"),
    )
    output = returns_object(*TASK_LIST_KEYS, schema="ReplayTaskList")
    columns = ("ID", "SPLIT", "STEP", "REPO", "TITLE")

    def run(self, ctx: HubContext) -> Result:
        found = Replays.of(ctx).tasks(ctx.args.limit, ctx.args.split)
        rows = [
            (
                f"#{task['id']}",
                task["split"],
                f"{task['plan_id']}#{task['step_key']}",
                task["repo"],
                task["title"] or "-",
            )
            for task in found["tasks"]
        ]
        counts = found["counts"]
        said = (
            f"{len(rows)} of {found['total']} replay task(s) of {ctx.project}; the suite has "
            f"{counts.get('select', 0)} select and {counts.get('holdout', 0)} holdout"
        )
        return Result(found, Views(Table(self.columns, rows), Text(said)))


class ReplayShow(HubCommand):
    name = "show"
    help = "a change's replays: each task with arm a and arm b, then both added up"
    arguments = (
        Arg("change", type=int, metavar="CHANGE", help="the change's id, as `curator changes` lists it"),
        PROJECT,
    )
    output = returns_object(*CHANGE_REPLAY_KEYS, schema="ChangeReplay")
    columns = ("ID", "TASK", "STATE", "ARM A", "ARM B")

    def run(self, ctx: HubContext) -> Result:
        found = Replays.of(ctx).of_change(ctx.args.change)
        rows = [
            (
                f"#{item['id']}",
                f"{item['plan_id']}#{item['step_key']}",
                item["state"],
                arm_text(item["a"]),
                arm_text(item["b"]),
            )
            for item in found["replays"]
        ]
        return Result(found, Views(Table(self.columns, rows), Text(*replay_lines(found, ctx.project))))


REPLAY = Group("replay", "the replays of past steps that measure the Curator's changes", (ReplayTasks(), ReplayShow()))

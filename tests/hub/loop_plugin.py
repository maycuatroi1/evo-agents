"""growth, a core plugin of the tests with one rule of the Loop (extension point loop_rules): it makes a growth_idea
work item, a job of its own, on a baseline of a canvas, on an event of its own and on the clock, and a growth_draft,
a task of an agent type the project does not have. ``MODE`` steers what its handler answers.
"""

from __future__ import annotations

HERE = "tests.hub.loop_plugin"
WEEK = "2026-W43"
CALLS: list[dict] = []
MODE: dict = {}

MANIFEST = {
    "id": "growth",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Growth",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "contributes": {
        "loop_rules": [
            {
                "name": "ideas",
                "python": f"{HERE}:ideas",
                "on": ["canvas.baseline", "plugin.growth.idea_seen"],
                "emits": ["idea_seen"],
                "schedule": True,
                "kinds": [
                    {"kind": "growth_idea", "run": "job"},
                    {"kind": "growth_draft", "run": "task", "agent_type": "marketer"},
                ],
            }
        ]
    },
}


async def ideas(params, context):
    CALLS.append(params)
    event, effects = params["event"], []
    goal = next(iter(params["goals"]), None)
    if MODE.get("quiet"):
        return {"effects": []}
    if event is None:
        effects.append(
            {"effect": "work", "kind": "growth_idea", "key": f"growth:ideas:{WEEK}", "detail": {"week": WEEK}}
        )
    elif event["kind"] == "canvas.baseline":
        found = {"effect": "work", "kind": "growth_idea", "key": f"growth:baseline:{event['id']}"}
        effects.append({**found, "goal_id": goal["id"] if goal else None})
    if MODE.get("draft") and event is None:
        effects.append(
            {"effect": "work", "kind": "growth_draft", "key": f"growth:draft:{WEEK}", "detail": {"task": "Draft"}}
        )
    if MODE.get("bad"):
        effects += [
            {"effect": "work", "kind": "build", "key": "x"},  # a kind the rule did not declare
            {"effect": "move", "item_id": MODE["bad"], "state": "done"},  # a work item that is not the plugin's
            {"effect": "work", "kind": "growth_idea", "key": "g", "goal_id": 10**9},  # a goal that is not the project's
        ]
    for item in params["items"]:
        if item["state"] == "running" and item["detail"].get("finish"):
            effects.append({"effect": "move", "item_id": item["id"], "state": "done"})
    return {"effects": effects}

"""The MCP tool curator_replay (see the package): the project's suite of replay tasks, or a change's replays, as text
and data."""

from __future__ import annotations

from fastapi import Request

from evo_agents.hub import curator
from evo_agents.hub.server.base.errors import Forbidden
from evo_agents.hub.server.replays import service
from evo_agents.hub.server.replays.models import AGENT_NO_CHANGE, ChangeReplay
from evo_agents.hub.server.security import Principal


def _arm_text(item: dict | None) -> str:
    if not item:
        return "-"
    cost = curator.money(item["cost_usd"]) if item.get("cost_usd") is not None else "cost -"
    word = "passed" if item.get("passed") else "failed"
    return f"{word}, {cost}, {item.get('turns') or 0} turns, {item.get('tool_errors') or 0} tool errors"


def _change_answer(project: str, found: ChangeReplay):
    arms = found.arms
    lines = [
        f"Change #{found.change_id} of project {project}: {len(found.replays)} of {found.tasks} replays; arm a "
        f"passed {arms['a']['passed']} of {arms['a']['replays']}, arm b {arms['b']['passed']} of "
        f"{arms['b']['replays']}. Arm a has the harness of the default branch, arm b the change's.",
        f"{'They won' if found.verdict['won'] else 'They did not win'}: {found.verdict['reason']}.",
    ]
    for item in found.replays:
        lines.append(
            f"#{item.id} {item.plan_id}#{item.step_key} ({item.state}): a {_arm_text(item.a)}; b {_arm_text(item.b)}"
        )
    data = {**found.model_dump(mode="json"), "summary": f"{len(found.replays)} replays"}
    return "\n".join(lines), data


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_replay. The agent of a run (the Reviewer, a Builder) lists the select split
    of the suite alone, and reads no change's replays, which are all of holdout tasks: the holdout split, which changes
    are measured on, stays out of what shapes them."""
    split = arguments.get("split")
    if user.scope is not None:
        if "change_id" in arguments:
            raise Forbidden(AGENT_NO_CHANGE)
        if split == "holdout":
            raise Forbidden("the agent of a run lists the select split of the replay suite alone")
        split = "select"
    if "change_id" in arguments:
        async with request.app.state.engine.begin() as conn:
            access = await service.reader_access(conn, user, project)
            found = await service.change_replay(conn, access, arguments["change_id"], sink)
        return _change_answer(project, found)
    async with request.app.state.engine.begin() as conn:
        access = await service.reader_access(conn, user, project)
        tasks, counts, total = await service.tasks_of(
            conn, access, split=split, limit=arguments.get("limit", 20), sink=sink
        )
    lines = [f"The replay suite of project {project}: {counts['select']} select, {counts['holdout']} holdout."]
    lines += [
        f"#{task.id} {task.split} {task.plan_id}#{task.step_key} in {task.repo}: {task.title or ''}" for task in tasks
    ]
    data = {"project": project, "tasks": [task.model_dump(mode="json") for task in tasks], "total": total}
    return "\n".join(lines), {**data, "counts": counts, "summary": f"{total} tasks"}

"""What a run the Loop hands out may use (plan loop-engine, criterion a5). The Loop has no budget per goal (checkpoint
of step 1, answer 1): it reserves nothing and settles no cost; each run it hands out carries its own caps.

- An author run (``build``) and a plan run (``deliver``): ``runs.budget`` with ``max_turns`` and ``max_seconds`` of
  the policy's ``loop.caps.author`` or ``loop.caps.plan`` and ``max_usd`` empty (``run_budget``), and a timeout of
  that agent time plus GRACE_SECONDS (``timeout_s``), so the cap stops the agent before the timeout does: Claude Code
  stops at its turns, Codex at its time.
- A task run (``brainstorm`` of @strategist, ``use_case`` of @ba): handed to the agent type through
  ``task_dispatch.hand_own_task`` like any task run of a core plugin (``hand_task``): its cost cap is what is left of
  the type's ``daily_usd`` in the last 24 hours, with the time cap of a task run. A type that is off, whose run_as no
  longer holds writer, or whose cap is spent hands out nothing, and the work item stays queued with reason
  ``type_refused`` and the hub's why.

Runs a person dispatches, and those a plugin hands out in a chain (a review of @ba, a split, a settle chat, a judge)
keep their own caps: nothing here touches them.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server.loop.admit import TYPE_REFUSED, Handed
from evo_agents.hub.server.loop.model import BRAINSTORM, BUILD, DELIVER, USE_CASE

CAPS_OF = {BUILD: "author", DELIVER: "plan"}  # the section of loop.caps for the run of each kind
AGENT_OF = {BRAINSTORM: ("canvas", "strategist"), USE_CASE: ("srs", "ba")}  # (plugin, agent type) of a task run
GRACE_SECONDS = 300  # a run's timeout is its agent time plus this, as a night shift's and a release run's


def run_budget(kind: str, policy: Mapping) -> dict:
    """``runs.budget`` of the run the Loop hands out for a work item of ``kind`` (build or deliver), ``policy`` the
    loop section of the effective policy: its turns and agent time, no cost cap."""
    caps = policy["caps"][CAPS_OF[kind]]
    return {"max_usd": None, "max_turns": int(caps["turns"]), "max_seconds": int(caps["minutes"]) * 60}


def timeout_s(kind: str, policy: Mapping) -> int:
    """``runs.timeout_s`` of that run: its agent time plus GRACE_SECONDS."""
    return run_budget(kind, policy)["max_seconds"] + GRACE_SECONDS


async def hand_task(conn: AsyncConnection, project_id: int, kind: str, task: str, title: str | None = None) -> Handed:
    """Hand ``task`` to the agent type of a work item of ``kind`` (brainstorm or use_case) in project ``project_id``,
    in the caller's transaction: the task run, or ``type_refused`` with why the hub handed none."""
    # Imported here: task_dispatch's own imports go round a cycle when it is the first module of the server a process
    # loads (evo_agents.core_plugins.srs.auto_tasks says the same).
    from evo_agents.hub.server.task_dispatch import hand_own_task

    plugin, name = AGENT_OF[kind]
    run_id, why = await hand_own_task(conn, plugin, project_id, name, task, title)
    if run_id is None:
        return Handed(reason=TYPE_REFUSED, why=why)
    return Handed(run_id=run_id)


__all__ = ["AGENT_OF", "CAPS_OF", "GRACE_SECONDS", "hand_task", "run_budget", "timeout_s"]

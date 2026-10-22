"""The rule of a plan run's prompt that says how its agent records a step, and runs the step's verify once.

The verify runs as a job of the worker (``evo-agents worker step KEY verify``, ``evo_agents.worker.verify_jobs``), which
outlives the agent's turn; ``done`` with the same commands reuses its result while the worktree stays as it is.
"""

from __future__ import annotations


def record_rule(key: str, which: str) -> str:
    """How the agent of a plan run records step ``key`` (KEY: any), its verify run once as a job of the worker."""
    return (
        f"- Record progress only with `evo-agents worker step`: `evo-agents worker step {key} in_progress --repo REPO` "
        f"when you start {which}, and `evo-agents worker step {key} done --repo REPO --evidence TEXT --verify COMMAND` "
        "(--verify once for each command) when it is done. The worker runs each verify command again in that repo's "
        "worktree and refuses done when one exits other than 0; then it commits what is left there and pushes the "
        f"repo's branch. Test what you changed, then run the whole verify once: `evo-agents worker step {key} verify "
        "--repo REPO --verify COMMAND` runs it on its own, past your turn, and done with the same commands reuses its "
        "result on an unchanged worktree. Both wait 540 s (--wait): give the call 10 minutes; on exit 124 run it again, "
        f"never a loop of sleep. `evo-agents worker step {key} pending --evidence TEXT` hands {which} back with what is "
        "done of it. Do not call the hub's plan_step tool, `evo harness step` or `evo-agents hub plan`."
    )

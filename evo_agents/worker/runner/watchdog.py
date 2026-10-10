"""The budget of a run, and the watchdog of a run of the Curator.

A run the night shift queued has a budget (``spec["budget"]``, ``evo_agents.hub.curator``). Each agent the run starts
gets what the run spent before it (``RunContext.spent_usd`` and ``spent_seconds``): the cost of its session as the
last outcome reported it, or as the claim said for a run that resumes a parked one, and the agent time used. An agent
the budget stopped fails the run, and the log's last note names the cap (``cap``: cost, turns or time).

A run of the Curator (``spec["curator"]``: a review run, a judge run, or a Builder, the plan run of a plan the Curator
made) is watched: after each heartbeat the daemon compares each of its worktrees with the charter's protected paths,
and its agent time and cost with its caps (``Watchdog.watch``); a protected file changed, or a cap passed, stops the
run, which fails naming why, and the hub tells its owner (notice run_failed).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from evo_agents.worker import gitops
from evo_agents.worker.runner.common import log
from evo_agents.worker.runner.transitions import cap_passed, protected_hit

if TYPE_CHECKING:
    from evo_agents.worker.runner.context import Run


class Watchdog:
    """What a run spent against its budget, and why the watchdog stopped it (``reason``)."""

    def __init__(self, run: Run, budget: dict):
        self.run = run
        self.budget = budget
        self.reason: str | None = None  # why the watchdog stopped the run
        self.cost_seen = 0.0  # the session's cost so far, as the agent's usage events said it
        self.spent_usd = budget.get("spent_usd") or 0.0  # the cost of the agent's session so far
        self.spent_before = budget.get("spent_seconds") or 0.0  # agent time a parked run this one resumes used

    def saw(self, kind: str, body: dict) -> None:
        """An event of the run: a usage update carries the session's running cost."""
        if kind != "usage_update":
            return
        cost = body.get("cost") if isinstance(body.get("cost"), dict) else {}
        amount = cost.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            self.cost_seen = max(self.cost_seen, float(amount))

    async def watch(self, watched: Iterable[tuple[str, Path, str]]) -> str | None:
        """For a run of the Curator, after a heartbeat: compare each of the ``watched`` worktrees (repo, worktree, the
        commit it started at) with the charter's protected paths, and the agent time and cost with the run's caps;
        stop the run when one is passed (``reason`` says why). Why it stopped the run, or None."""
        run = self.run
        if run.curator is None or run.ended or run.stop_reason is not None:
            return None
        protected = [glob for glob in run.curator.get("protected_paths") or [] if isinstance(glob, str)]
        why = None
        for repo, worktree, base in watched:
            if not protected or not worktree.is_dir():
                continue
            try:
                paths = await gitops.changed_paths(worktree, base)
            except gitops.GitError as exc:
                log.warning("the watchdog could not read a worktree", extra={"run_id": run.id, "error": str(exc)})
                continue
            why = protected_hit(repo, paths, protected)
            if why is not None:
                break
        if why is None:
            why = cap_passed(self.budget, self.spent_before + run.agent_seconds(), max(self.cost_seen, self.spent_usd))
        if why is None or run.ended or run.stop_reason is not None:
            return None
        self.reason = why
        log.warning("the watchdog stops a run of the Curator", extra={"run_id": run.id, "why": why})
        run.request_stop("watchdog")
        return why

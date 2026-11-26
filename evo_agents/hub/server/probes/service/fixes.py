"""What the weekly probe report asks of the project's next review run (plan correction-recurrence, criterion a5).

A probe of a merged change that was recurring on the same runtime and model in the two weekly reports before the
review run's week (``probes.repeated``: the two ISO weeks before the one of its night) gives that run a fix assignment:
write a fix proposal of the change's rule (``evo-agents worker propose --fix-of CHANGE``), turning the rule into a hook
or a check where it can, or a finding that says why not (``evo-agents worker finding --fix-of CHANGE``). A probe that
was not reproduced on the same runtime and model in both reports gives a replace assignment: write a probe in its place
(``evo-agents worker probe add --replaces PROBE``); the hub retires the old one. So does a probe stale on the same
runtime and model in both reports (plan probes-measure-both-arms, criterion a3): its arms could be made neither at the
tip of the default branch nor at the change's commits, or it places no file a probe arm places; the assignment names
why it was stale each week, and the probe written in its place takes a base commit or a task that can be made.

The hub, not the agent, decides what was assigned: ``evo_agents.hub.server.proposals`` refuses (422) a fix proposal or
a finding whose change has no fix assignment and ``service.writes`` a replacement whose probe has no replace assignment.
A fix proposal is at least tier 1 (``tiers.tier_of``'s ``fix_of``: the owner merges it, replays that won never make it
tier 0), its evidence holds the two weeks' probe results (kind ``probe``, ``review.evidence_text`` writes it), and a
change has at most one open (or deferred) fix proposal: 409 for a second, and proposals_fix_open_key holds it. It
reaches the owner's Inbox as any proposal does, under the charter's max_decisions_per_day. The hub never merges a fix
proposal alone and reverts nothing on what its probes found.

An assignment stays in the prompt of each review run of the week until one answers it: a fix assignment while the
change has no open fix proposal and no review run of the week recorded a fix proposal or a finding for it; a replace
assignment while the probe is active. Only the probes whose label reaches the run's owner count.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes, runs
from evo_agents.hub.server.memories.service.audience import Reader, label_reaches
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.models.fixes import RESOLVED, WEEK, Assignment
from evo_agents.hub.server.probes.repository import fixes as repository
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal


def _evidence(row, status: str) -> dict:
    on, off = probes.arm_counts(row.sessions or [], "on"), probes.arm_counts(row.sessions or [], "off")
    item = {
        "kind": "probe",
        "probe_id": row.probe_id,
        "change_id": row.change_id,
        "week": row.week.isoformat(),
        "runtime": row.runtime,
        **({"model": row.model} if row.model else {}),
        "status": status,
        "k": row.k,
        "on": {"runs": on[0], "failed": on[1]},
        "off": {"runs": off[0], "failed": off[1]},
        "resolved": RESOLVED,
    }
    return {**item, "run_id": row.run_id} if row.run_id is not None else item


async def assignments(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    night: date,
    *,
    pending: bool = True,
) -> list[Assignment]:
    """The fix and replace assignments of a review run of project ``access`` on ``night``, as ``user`` (its owner)
    reads the probes; with ``pending``, only those no review run of the week answered yet (see the module's
    docstring). Fixes first, by change; then replacements, by probe."""
    current = probes.week_of(night)
    weeks = [current - WEEK * i for i in range(probes.REPEAT_WEEKS, 0, -1)]  # the older first
    rows = await repository.week_rows(conn, access.project_id, weeks)
    reader, through = Reader(conn, user), plan_service.read_sink(access, None)
    seen: dict[int, bool] = {}
    statuses: dict[date, dict] = {week: {} for week in weeks}
    found: dict[tuple, dict] = {}
    held = {}
    for row in rows:
        if row.probe_id not in seen:
            seen[row.probe_id] = await label_reaches(reader, access, row.label, through)  # pyright: ignore[reportArgumentType]
        if not seen[row.probe_id]:
            continue
        on, off = probes.arm_counts(row.sessions or [], "on"), probes.arm_counts(row.sessions or [], "off")
        status = probes.status_of(row.state, on[1], off[0], off[1])
        key = (row.probe_id, row.runtime, row.model)
        statuses[row.week][key] = status
        found[(row.week, key)] = _evidence(row, status)
        held[row.probe_id] = row
    by_week = [statuses[week] for week in weeks]
    fixes: dict[int, Assignment] = {}
    for key in probes.repeated(by_week, probes.FIX_STATUS):
        row = held[key[0]]
        task = fixes.setdefault(row.change_id, Assignment("fix", row.change_id, row.title, row.repo))
        task.results += [found[(week, key)] for week in weeks]
    replaces: dict[int, Assignment] = {}
    reasons = {(row.week, (row.probe_id, row.runtime, row.model)): row.reason for row in rows}
    replaced = [(key, status) for status in probes.REPLACE_STATUSES for key in probes.repeated(by_week, status)]
    for key, status in replaced:
        row = held[key[0]]
        task = replaces.get(row.probe_id)
        if task is None:
            clipped = runs.clip(row.task, probes.MAX_ASSIGNED_TASK_BYTES)
            task = replaces[row.probe_id] = Assignment(
                "replace", row.change_id, row.title, row.repo, probe_id=row.probe_id, task=clipped
            )
        task.results += [found[(week, key)] for week in weeks]
        if status == probes.STALE_STATUS:
            where = f"{key[1]}{f' {key[2]}' if key[2] else ''}"
            task.stale += [
                f"the week of {week.isoformat()} on {where}: {reasons.get((week, key)) or 'no reason'}"
                for week in weeks
            ]
    listed = []
    for change_id in sorted(fixes):
        if not pending or not await repository.answered(conn, change_id, current):
            listed.append(fixes[change_id])
    return listed + [replaces[probe_id] for probe_id in sorted(replaces)]


async def fix_assignment(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, night: date, change_id: int
) -> Assignment | None:
    """The fix assignment of change ``change_id`` on ``night``, answered or not; None without one."""
    found = await assignments(conn, access, user, night, pending=False)
    return next((item for item in found if item.kind == "fix" and item.change_id == change_id), None)


async def replace_assignment(
    conn: AsyncConnection, access: ProjectAccess, user: Principal, night: date, probe_id: int
) -> Assignment | None:
    """The replace assignment of probe ``probe_id`` on ``night``; None without one."""
    found = await assignments(conn, access, user, night, pending=False)
    return next((item for item in found if item.kind == "replace" and item.probe_id == probe_id), None)


async def open_fix(conn: AsyncConnection, change_id: int) -> int | None:
    """The open (or deferred) fix proposal of change ``change_id``, if any."""
    return await repository.open_fix(conn, change_id)


async def run_night(conn: AsyncConnection, run_id: int) -> date:
    """The night of review run ``run_id``: its schedule's night, else today in the database."""
    return await repository.run_night(conn, run_id)

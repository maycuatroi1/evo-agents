"""What the weekly probe report asks of the project's next review run (plan correction-recurrence, criterion a5).

A probe of a merged change that was recurring on the same runtime and model in the two weekly reports before the
review run's week (``probes.repeated``: the two ISO weeks before the one of its night) gives that run a fix assignment:
write a fix proposal of the change's rule (``evo-agents worker propose --fix-of CHANGE``), turning the rule into a hook
or a check where it can, or a finding that says why not (``evo-agents worker finding --fix-of CHANGE``). A probe that
was not reproduced on the same runtime and model in both reports gives a replace assignment: write a probe in its place
(``evo-agents worker probe add --replaces PROBE``); the hub retires the old one.

The hub, not the agent, decides what was assigned: ``evo_agents.hub.server.proposals`` refuses (422) a fix proposal or
a finding whose change has no fix assignment and ``probe_routes`` a replacement whose probe has no replace assignment.
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

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import probes, review, runs, tables
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.memory_audience import Reader, label_reaches
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

WEEK = timedelta(days=7)
OPEN = ("open", "deferred")  # the states of a fix proposal still to answer
RESOLVED = "probe_report"  # how the hub found the probe evidence it adds


@dataclass
class Assignment:
    """What a review run is asked to do of one change (``fix``) or one probe (``replace``)."""

    kind: str  # fix | replace
    change_id: int
    title: str  # the title of the change's proposal
    repo: str  # the probe's repo
    probe_id: int | None = None  # replace: the probe to replace
    task: str | None = None  # replace: its task, clipped
    results: list[dict] = field(default_factory=list)  # the probe evidence of both weeks, the older first

    def prompt(self) -> dict:
        """The assignment as ``review.build_review_prompt`` reads it."""
        shown = sorted({(item["probe_id"], item["runtime"], item.get("model") or "") for item in self.results})
        return {
            "kind": self.kind,
            "change_id": self.change_id,
            "title": self.title,
            "repo": self.repo,
            "probe_id": self.probe_id,
            "task": self.task,
            "weeks": sorted({item["week"] for item in self.results}),
            "probes": [f"probe #{probe} on {runtime}{f' {model}' if model else ''}" for probe, runtime, model in shown],
            "evidence": [review.evidence_text(item) for item in self.results],
        }


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


async def _answered(conn: AsyncConnection, change_id: int, since: date) -> bool:
    """Whether the change has an open fix proposal, or a review run of a night since ``since`` recorded a fix proposal
    or a finding for it."""
    p, f, r = tables.proposals, tables.findings, tables.runs
    open_fix = select(p.c.id).where(p.c.fix_of == change_id, p.c.state.in_(OPEN))
    if (await conn.execute(open_fix.limit(1))).first() is not None:
        return True
    for t in (p, f):
        query = (
            select(t.c.id)
            .join_from(t, r, r.c.id == t.c.run_id)
            .where(t.c.fix_of == change_id, func.coalesce(r.c.schedule_night, func.current_date()) >= since)
            .limit(1)
        )
        if (await conn.execute(query)).first() is not None:
            return True
    return False


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
    pw, pr, c, p = tables.curator_probe_runs, tables.curator_probes, tables.curator_changes, tables.proposals
    rows = (
        await conn.execute(
            select(
                pw.c.probe_id,
                pw.c.change_id,
                pw.c.week,
                pw.c.runtime,
                pw.c.model,
                pw.c.state,
                pw.c.sessions,
                pw.c.k,
                pw.c.run_id,
                pr.c.label,
                pr.c.repo,
                pr.c.task,
                p.c.title,
            )
            .join_from(pw, pr, pr.c.id == pw.c.probe_id)
            .join(c, c.c.id == pw.c.change_id)
            .join(p, p.c.id == c.c.proposal_id)
            .where(
                pw.c.project_id == access.project_id,
                pw.c.week.in_(weeks),
                pr.c.state == "active",
                pr.c.change_id == pw.c.change_id,
                c.c.state == "merged",
            )
            .order_by(pw.c.change_id, pw.c.probe_id, pw.c.id)
        )
    ).all()
    reader, through = Reader(conn, user), plan_routes._sink(access, None)
    seen: dict[int, bool] = {}
    statuses: dict[date, dict] = {week: {} for week in weeks}
    found: dict[tuple, dict] = {}
    held = {}
    for row in rows:
        if row.probe_id not in seen:
            seen[row.probe_id] = await label_reaches(reader, access, row.label, through)
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
    for key in probes.repeated(by_week, probes.REPLACE_STATUS):
        row = held[key[0]]
        task = replaces.get(row.probe_id)
        if task is None:
            clipped = runs.clip(row.task, probes.MAX_ASSIGNED_TASK_BYTES)
            task = replaces[row.probe_id] = Assignment(
                "replace", row.change_id, row.title, row.repo, probe_id=row.probe_id, task=clipped
            )
        task.results += [found[(week, key)] for week in weeks]
    listed = []
    for change_id in sorted(fixes):
        if not pending or not await _answered(conn, change_id, current):
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
    p = tables.proposals
    query = select(p.c.id).where(p.c.fix_of == change_id, p.c.state.in_(OPEN)).order_by(p.c.id).limit(1)
    return (await conn.execute(query)).scalar_one_or_none()


async def run_night(conn: AsyncConnection, run_id: int) -> date:
    """The night of review run ``run_id``: its schedule's night, else today in the database."""
    r = tables.runs
    night = (await conn.execute(select(r.c.schedule_night).where(r.c.id == run_id))).scalar_one_or_none()
    return night if night is not None else await conn.scalar(select(func.current_date()))

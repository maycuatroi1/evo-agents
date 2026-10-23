"""The weekly report of the Curator's probes (plan correction-recurrence, criterion a4): what each probe run of the ISO
weeks found (``evo_agents.hub.server.probe_runs`` queues them, ``evo_agents.hub.probes`` holds their shape).

For each week (its Monday, in the charter's time zone), each probe and each runtime and model: the sessions and the
failed sessions of arm on and of arm off, their 1 - pass^k (``probes.recurrence``), the status (``probes.status_of``:
holds, recurring, not_reproduced, stale, unmeasured) and the status of the week before. An active probe of a merged
change that has no row of the week on a model of the charter is unmeasured too. Each week adds up its statuses and its
sessions, and what its probe runs cost (the usage of the project's runs of kind probe whose night falls in the week)
against the charter's probes.budget_usd. The report also names the merged changes born of a correction (their
proposal's evidence cites ``corrections`` of the night's figures, ``ledger.cites_corrections``) that have no active
probe, each with why, the fix proposals of recurring probes with their state (``fixes``) and the probes written in
place of probes not reproduced (``replacements``), those still open or made since the oldest week shown
(``evo_agents.hub.server.probe_fixes`` says when a review run is asked for them).

GET /v1/projects/{p}/curator/probe-report (reader) answers it for ``weeks`` weeks, the newest first: the week of
``week`` (a day; default today in the charter's time zone, the week in progress, whose ``complete`` is false) and those
before it; the probes and changes whose label the caller's grant reaches through the sink X-Evo-Sink
names (else the project's hub sink). `evo-agents hub curator probe report` prints it, and the MCP tool
curator_probe_report gives it to a review run (``mcp_answer``). No part of it holds a probe's verify.

``send`` is the weekly notice: on Monday, at the charter's brief_at in its time zone (``curator.brief_due``), the morning
brief's job (``brief.send_briefs``) tells the owner of the night shift's schedule of a project whose charter runs
probes the report of the ISO week that just ended, as a notice curator_probe_report on the web and on every channel of
theirs, Telegram included. The notice names its week in ``details``; under the schedule's row lock, a week told is not
told again.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import curator, probes, runs, tables
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server import probe_fixes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.curator import _charter_row, _owner_access
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.memory_audience import Reader, label_reaches
from evo_agents.hub.server.night_cost import usage_cost
from evo_agents.hub.server.notifications import notify
from evo_agents.hub.server.probe_runs import _models
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.protection import need_reader
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal

log = logging.getLogger(__name__)

WEEK = timedelta(days=7)
DEFAULT_WEEKS = 2
MAX_UNPROBED = 50  # the changes without a probe a report names, the newest first
MAX_NOTICE_LISTED = 10  # the flagged probes and the changes without a probe the notice's body names
NOT_YET = "not measured yet this week"
NO_RUN = "no probe run measured it this week"
NOT_PLACED = (
    "it changes no file a probe arm can place (AGENTS.md, CLAUDE.md, a skill, the repo's .claude/settings.json or "
    ".claude/hooks/): a probe cannot measure it"
)
NOT_WRITTEN = "no probe was written for it: a review run or an admin adds one (evo-agents hub curator probe add)"

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}

Status = Literal[probes.STATUSES]


# Models


class ProbeArmCount(BaseModel):
    runs: int = Field(description="the arm's sessions")
    failed: int = Field(description="those whose verify did not pass")
    recurrence: float | None = Field(description="1 - pass^k of the arm; null without a session")


class ProbeWeekResult(BaseModel):
    """One probe on one runtime and model in a week."""

    change_id: int
    probe_id: int
    runtime: str
    model: str | None = Field(description="null: the runtime's own choice")
    k: int | None = Field(description="the sessions of each arm; null when the week queued none")
    on: ProbeArmCount = Field(description="the change's rule in place")
    off: ProbeArmCount = Field(description="the change's diff taken off")
    status: Status
    previous: Status | None = Field(description="the status of the week before; null when it had none")
    reason: str | None = Field(description="why it is stale or unmeasured")
    run_id: int | None = Field(description="the probe run that measured it last")


class ProbeWeekTotals(BaseModel):
    probes: int = Field(description="the results of the week")
    holds: int
    recurring: int
    not_reproduced: int
    stale: int
    unmeasured: int
    on: ProbeArmCount
    off: ProbeArmCount


class ProbeWeek(BaseModel):
    week: date = Field(description="the Monday of the ISO week, in the charter's time zone")
    complete: bool = Field(description="false for the week in progress")
    cost_usd: float = Field(description="what the week's probe runs cost")
    budget_usd: float | None = Field(description="the charter's probes.budget_usd; null: the project runs no probe")
    probes: list[ProbeWeekResult]
    totals: ProbeWeekTotals


class ProbeUnprobed(BaseModel):
    """A merged change born of a correction that has no active probe."""

    change_id: int
    proposal_id: int
    title: str
    merged_at: datetime | None
    reason: str


class ProbeFix(BaseModel):
    """A proposal that fixes the rule of a change whose probe recurs (criterion a5)."""

    proposal_id: int
    change_id: int
    title: str
    state: str


class ProbeReplacement(BaseModel):
    """A probe a review run wrote in place of one not reproduced two weeks in a row (criterion a5)."""

    probe_id: int
    replaces: int = Field(description="the probe it replaced, now retired")
    change_id: int | None
    state: str
    created_at: datetime


class ProbeWeekly(BaseModel):
    project: str
    k: int = Field(description="the charter's probes.k")
    budget_usd: float | None = Field(description="the charter's probes.budget_usd per ISO week; null: no probes")
    weeks: list[ProbeWeek] = Field(description="the newest first")
    unprobed: list[ProbeUnprobed] = Field(description="merged changes born of a correction without an active probe")
    fixes: list[ProbeFix] = Field(description="the fix proposals of recurring probes, with their state")
    replacements: list[ProbeReplacement] = Field(
        default_factory=list, description="the probes written in place of probes not reproduced two weeks in a row"
    )


# Reading


def _arm(sessions, arm: str, k: int | None) -> ProbeArmCount:
    count, failed = probes.arm_counts(sessions or [], arm)
    return ProbeArmCount(runs=count, failed=failed, recurrence=probes.recurrence(count, failed, k or probes.DEFAULT_K))


def _result(row, *, k: int | None = None, reason: str | None = None) -> dict:
    """The week's result of a curator_probe_runs row (``k`` and ``reason`` alone for a pair the week has no row of)."""
    if row is None:
        none = ProbeArmCount(runs=0, failed=0, recurrence=None)
        return {"k": k, "on": none, "off": none, "status": "unmeasured", "reason": reason, "run_id": None}
    on, off = _arm(row.sessions, "on", row.k), _arm(row.sessions, "off", row.k)
    status = probes.status_of(row.state, on.failed, off.runs, off.failed)
    why = row.reason if status in ("stale", "unmeasured") else None
    if status == "unmeasured" and not why:
        why = f"its probe run is {row.state}"
    return {"k": row.k, "on": on, "off": off, "status": status, "reason": why, "run_id": row.run_id}


def _totals(results: list[ProbeWeekResult]) -> ProbeWeekTotals:
    counts = {status: sum(item.status == status for item in results) for status in probes.STATUSES}

    def arm(side: str) -> ProbeArmCount:
        count = sum(getattr(item, side).runs for item in results)
        return ProbeArmCount(runs=count, failed=sum(getattr(item, side).failed for item in results), recurrence=None)

    return ProbeWeekTotals(probes=len(results), **counts, on=arm("on"), off=arm("off"))


async def week_cost(conn: AsyncConnection, project_id: int, week: date) -> float:
    """What the project's probe runs of the nights of the ISO week of Monday ``week`` cost."""
    r = tables.runs
    found = await conn.execute(
        select(func.coalesce(func.sum(usage_cost(r.c.usage)), 0)).where(
            r.c.project_id == project_id,
            r.c.kind == "probe",
            r.c.schedule_night.between(week, week + WEEK - timedelta(1)),
        )
    )
    return round(float(found.scalar_one() or 0), 6)


async def local_today(conn: AsyncConnection, body: dict) -> date:
    """Today in the charter's time zone (UTC without a charter)."""
    zone = ((body or {}).get("window") or {}).get("timezone") or "UTC"
    return (await conn.scalar(select(func.timezone(zone, func.now())))).date()


class _Reach:
    """Whether a label reaches the caller through the sink of the read, each label read once."""

    def __init__(self, conn: AsyncConnection, access: ProjectAccess, user: Principal, sink: str | None):
        self.reader, self.access, self.through = Reader(conn, user), access, plan_routes._sink(access, sink)
        self.seen: dict[str, bool] = {}

    async def __call__(self, label) -> bool:
        key = repr(label)
        if key not in self.seen:
            self.seen[key] = await label_reaches(self.reader, self.access, label, self.through)
        return self.seen[key]


async def _week_results(
    conn: AsyncConnection, access: ProjectAccess, reach: _Reach, weeks: list[date], body: dict, current: date
):
    """{week: {(probe, runtime, model): (change, result)}} of ``weeks`` (any order), the probes the caller reads."""
    pw, pr, c = tables.curator_probe_runs, tables.curator_probes, tables.curator_changes
    rows = (
        await conn.execute(
            select(pw, pr.c.label.label("probe_label"))
            .join(pr, pr.c.id == pw.c.probe_id)
            .where(pw.c.project_id == access.project_id, pw.c.week.in_(weeks))
            .order_by(pw.c.id)
        )
    ).all()
    active = (
        await conn.execute(
            select(pr.c.id, pr.c.change_id, pr.c.label, pr.c.created_at)
            .join(c, c.c.id == pr.c.change_id)
            .where(pr.c.project_id == access.project_id, pr.c.state == "active", c.c.state == "merged")
            .order_by(pr.c.id)
        )
    ).all()
    models = _models(body) if (body.get("probes") or {}).get("budget_usd") is not None else []
    k = int((body.get("probes") or {}).get("k") or probes.DEFAULT_K)
    found: dict[date, dict] = {week: {} for week in weeks}
    for row in rows:
        if await reach(row.probe_label):
            found[row.week][(row.probe_id, row.runtime, row.model)] = (row.change_id, _result(row))
    for probe in active:
        if not await reach(probe.label):
            continue
        for week in weeks:
            if probe.created_at.date() >= week + WEEK:
                continue  # made after the week
            for runtime, model in models:
                key = (probe.id, runtime, model)
                if key not in found[week]:
                    why = NOT_YET if week >= current else NO_RUN
                    found[week][key] = (probe.change_id, _result(None, k=k, reason=why))
    return found


async def _unprobed(conn: AsyncConnection, access: ProjectAccess, reach: _Reach) -> list[ProbeUnprobed]:
    c, p, pr = tables.curator_changes, tables.proposals, tables.curator_probes
    has_active = exists().where(pr.c.change_id == c.c.id, pr.c.state == "active")
    rows = (
        await conn.execute(
            select(
                c.c.id,
                c.c.proposal_id,
                c.c.merged_at,
                p.c.title,
                p.c.run_id,
                p.c.evidence,
                p.c.finding_ids,
                p.c.paths,
                p.c.label,
            )
            .join_from(c, p, p.c.id == c.c.proposal_id)
            .where(c.c.project_id == access.project_id, c.c.state == "merged", ~has_active)
            .order_by(c.c.id.desc())
        )
    ).all()
    found = []
    for row in rows:
        if len(found) >= MAX_UNPROBED:
            break
        if not await reach(row.label) or not await ledger_lines.cites_corrections(conn, row):
            continue
        retired = (
            await conn.execute(
                select(pr.c.id, pr.c.retired_note)
                .where(pr.c.change_id == row.id, pr.c.state == "retired")
                .order_by(pr.c.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if not probes.probe_paths(item.get("path") for item in row.paths or [] if isinstance(item, dict)):
            why = NOT_PLACED
        elif retired is not None:
            why = f"its probe #{retired.id} was retired" + (f": {retired.retired_note}" if retired.retired_note else "")
        else:
            why = NOT_WRITTEN
        found.append(
            ProbeUnprobed(
                change_id=row.id, proposal_id=row.proposal_id, title=row.title, merged_at=row.merged_at, reason=why
            )
        )
    return found


async def _fixes(conn: AsyncConnection, access: ProjectAccess, reach: _Reach, since: date) -> list[ProbeFix]:
    """The fix proposals of the project still open, or made since ``since``, the newest first."""
    p = tables.proposals
    rows = (
        await conn.execute(
            select(p.c.id, p.c.fix_of, p.c.title, p.c.state, p.c.label)
            .where(
                p.c.project_id == access.project_id,
                p.c.fix_of.is_not(None),
                p.c.state.in_(probe_fixes.OPEN) | (func.date(p.c.created_at) >= since),
            )
            .order_by(p.c.id.desc())
            .limit(MAX_UNPROBED)
        )
    ).all()
    return [
        ProbeFix(proposal_id=row.id, change_id=row.fix_of, title=row.title, state=row.state)
        for row in rows
        if await reach(row.label)
    ]


async def _replacements(
    conn: AsyncConnection, access: ProjectAccess, reach: _Reach, since: date
) -> list[ProbeReplacement]:
    """The probes of the project made since ``since`` in place of others, the newest first."""
    pr = tables.curator_probes
    rows = (
        await conn.execute(
            select(pr.c.id, pr.c.replaces, pr.c.change_id, pr.c.state, pr.c.created_at, pr.c.label)
            .where(
                pr.c.project_id == access.project_id,
                pr.c.replaces.is_not(None),
                func.date(pr.c.created_at) >= since,
            )
            .order_by(pr.c.id.desc())
            .limit(MAX_UNPROBED)
        )
    ).all()
    return [
        ProbeReplacement(
            probe_id=row.id, replaces=row.replaces, change_id=row.change_id, state=row.state, created_at=row.created_at
        )
        for row in rows
        if await reach(row.label)
    ]


async def build(
    conn: AsyncConnection,
    access: ProjectAccess,
    user: Principal,
    *,
    weeks: int = DEFAULT_WEEKS,
    newest: date | None = None,
    today: date | None = None,
    sink: str | None = None,
) -> ProbeWeekly:
    """The report of the ``weeks`` ISO weeks up to the one of ``newest`` (the week in progress when None), newest
    first, as ``user`` reads it through ``sink``; ``today`` is the local day (the database's today in the charter's time
    zone when None)."""
    charter = await _charter_row(conn, access.project_id)
    body = dict(charter.body) if charter is not None else {}
    settings = body.get("probes") or {}
    current = probes.week_of(today or await local_today(conn, body))
    top = probes.week_of(newest) if newest is not None else current
    listed = [top - WEEK * i for i in range(weeks)]
    reach = _Reach(conn, access, user, sink)
    found = await _week_results(conn, access, reach, [*listed, listed[-1] - WEEK], body, current)
    shown = []
    for week in listed:
        before = {key: result["status"] for key, (_, result) in found[week - WEEK].items()}
        results = [
            ProbeWeekResult(
                change_id=change_id,
                probe_id=key[0],
                runtime=key[1],
                model=key[2],
                previous=before.get(key),
                **result,
            )
            for key, (change_id, result) in found[week].items()
        ]
        results.sort(key=lambda item: (item.change_id, item.probe_id, item.runtime, item.model or ""))
        shown.append(
            ProbeWeek(
                week=week,
                complete=week < current,
                cost_usd=await week_cost(conn, access.project_id, week),
                budget_usd=settings.get("budget_usd"),
                probes=results,
                totals=_totals(results),
            )
        )
    return ProbeWeekly(
        project=access.name,
        k=int(settings.get("k") or probes.DEFAULT_K),
        budget_usd=settings.get("budget_usd"),
        weeks=shown,
        unprobed=await _unprobed(conn, access, reach),
        fixes=await _fixes(conn, access, reach, listed[-1]),
        replacements=await _replacements(conn, access, reach, listed[-1]),
    )


Weeks = Annotated[
    int, Query(ge=1, le=probes.MAX_REPORT_WEEKS, description="the newest weeks, the one in progress first")
]


@router.get("/{project}/curator/probe-report", response_model=ProbeWeekly, responses=READ_REFUSALS)
async def probe_report(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    weeks: Weeks = DEFAULT_WEEKS,
    week: Annotated[
        date | None, Query(description="a day of the newest week to show; default today, in the charter's time zone")
    ] = None,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> ProbeWeekly:
    """The weekly report of the project's probes."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        return await build(conn, access, user, weeks=weeks, newest=week, sink=sink)


# Words


def _who(item: ProbeWeekResult) -> str:
    return (
        f"probe #{item.probe_id} of change #{item.change_id} on {item.runtime}{f' {item.model}' if item.model else ''}"
    )


def _rate(arm: ProbeArmCount) -> str:
    return "-" if arm.recurrence is None else f"{arm.recurrence:.2f}"


def result_line(item: ProbeWeekResult) -> str:
    """One probe's week, for a person or an agent."""
    words = f"{_who(item)}: {item.status.replace('_', ' ')}"
    if item.status in ("stale", "unmeasured"):
        words += f" ({item.reason})" if item.reason else ""
    else:
        words += (
            f", on failed {item.on.failed} of {item.on.runs} (1 - pass^k {_rate(item.on)}), off failed "
            f"{item.off.failed} of {item.off.runs} (1 - pass^k {_rate(item.off)})"
        )
    if item.previous is not None:
        words += f"; the week before {item.previous.replace('_', ' ')}"
    return words


def _cost(week: ProbeWeek) -> str:
    cap = f" of {curator.money(week.budget_usd)}" if week.budget_usd is not None else ""
    return f"{curator.money(week.cost_usd)}{cap}"


def week_lines(week: ProbeWeek, *, listed: int | None = None) -> list[str]:
    totals = week.totals
    flagged = [item for item in week.probes if item.status in probes.FLAGGED]
    lines = [
        f"Week of {week.week.isoformat()}{'' if week.complete else ' (in progress)'}: {totals.probes} result(s), "
        f"{totals.holds} holds, {totals.recurring} recurring, {totals.not_reproduced} not reproduced, {totals.stale} "
        f"stale, {totals.unmeasured} unmeasured; arm on failed {totals.on.failed} of {totals.on.runs} sessions, arm "
        f"off {totals.off.failed} of {totals.off.runs}; cost {_cost(week)}."
    ]
    shown = week.probes if listed is None else flagged[:listed]
    lines += [f"- {result_line(item)}" for item in shown]
    if listed is not None and len(flagged) > listed:
        lines.append(f"- and {len(flagged) - listed} more")
    return lines


def answer_lines(found: ProbeWeekly, *, listed: int | None = None) -> list[str]:
    """The fix proposals and the replacement probes of the report."""
    lines = []
    if found.fixes:
        shown = found.fixes if listed is None else found.fixes[:listed]
        lines.append(f"Fix proposals of probes that recurred two weeks in a row: {len(found.fixes)}")
        lines += [
            f"- proposal #{fix.proposal_id} of change #{fix.change_id} ({fix.state}): {fix.title}" for fix in shown
        ]
    if found.replacements:
        shown = found.replacements if listed is None else found.replacements[:listed]
        lines.append(f"Probes written in place of probes not reproduced two weeks in a row: {len(found.replacements)}")
        lines += [
            f"- probe #{item.probe_id} of change #{item.change_id} in place of probe #{item.replaces}" for item in shown
        ]
    return lines


def unprobed_lines(found: ProbeWeekly, *, listed: int | None = None) -> list[str]:
    if not found.unprobed:
        return []
    shown = found.unprobed if listed is None else found.unprobed[:listed]
    lines = [f"Changes born of a correction without a probe: {len(found.unprobed)}"]
    lines += [f"- change #{item.change_id} ({item.title}): {item.reason}" for item in shown]
    return lines


async def mcp_answer(request: Request, project: str, user: Principal, arguments: dict, *, sink: str | None):
    """(text, data) of the MCP tool curator_probe_report."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        newest = date.fromisoformat(arguments["week"]) if arguments.get("week") else None
        found = await build(conn, access, user, weeks=arguments.get("weeks", DEFAULT_WEEKS), newest=newest, sink=sink)
    cap = "no probes (probes.budget_usd is null)" if found.budget_usd is None else f"{curator.money(found.budget_usd)}"
    lines = [f"The probe report of project {project}: k {found.k}, a week's budget {cap}."]
    for week in found.weeks:
        lines += week_lines(week)
    lines += unprobed_lines(found)
    lines += answer_lines(found)
    data = {**found.model_dump(mode="json"), "summary": f"{len(found.weeks)} weeks"}
    return "\n".join(lines), data


# The weekly notice


async def send(conn: AsyncConnection, due) -> str | None:
    """The notice of last week's report of schedule ``due``'s project, in the caller's transaction (see the module's
    docstring); a word for the brief job's summary, or None when the project runs no probe or it is not the time."""
    body = due.body
    if (body.get("probes") or {}).get("budget_usd") is None:
        return None
    local = due.local_now
    if local.weekday() != 0 or not curator.brief_due(local, body["brief_at"]):
        return None
    s, n = tables.schedules, tables.notifications
    await conn.execute(select(s.c.id).where(s.c.id == due.id).with_for_update())
    week = probes.week_of(local.date()) - WEEK
    told = await conn.scalar(
        select(n.c.id)
        .where(
            n.c.project_id == due.project_id,
            n.c.notice_kind == runs.PROBE_REPORT_NOTICE,
            n.c.details["week"].astext == week.isoformat(),
        )
        .limit(1)
    )
    if told is not None:
        return "probe_report_sent"
    access = await _owner_access(conn, due.owner_id, due.owner, due.project)
    if access is None or access.role is None:
        return None
    owner = Principal(user_id=due.owner_id, login=due.owner, admin=False, token_id=0, kind=MACHINE, token_hash="")
    found = await build(conn, access, owner, weeks=1, newest=week, today=local.date())
    (shown,) = found.weeks
    totals = shown.totals
    title = (
        f"Probe report of {due.project}, week of {week.isoformat()}: {totals.recurring} recurring, "
        f"{totals.not_reproduced} not reproduced, {totals.stale + totals.unmeasured} unmeasured or stale, "
        f"{_cost(shown)}"
    )
    lines = [
        f"**Probes of {due.project}** (k {found.k}; 1 - pass^k of each arm)",
        *week_lines(shown, listed=MAX_NOTICE_LISTED),
    ]
    lines += unprobed_lines(found, listed=MAX_NOTICE_LISTED)
    lines += answer_lines(found, listed=MAX_NOTICE_LISTED)
    details = {"week": week.isoformat(), **totals.model_dump(include=set(probes.STATUSES) | {"probes"})}
    details |= {"cost_usd": shown.cost_usd, "budget_usd": shown.budget_usd, "unprobed": len(found.unprobed)}
    details |= {"fixes": len(found.fixes), "replacements": len(found.replacements)}
    await notify(
        conn,
        user_id=due.owner_id,
        kind="notice",
        notice_kind=runs.PROBE_REPORT_NOTICE,
        project_id=due.project_id,
        title=title,
        body="\n".join(lines),
        details=details,
        link=f"/p/{due.project}/curator",
    )
    log.info("probe report sent", extra={"project": due.project, "week": week.isoformat()})
    return "probe_report"

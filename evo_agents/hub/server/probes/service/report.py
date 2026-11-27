"""The weekly report of the Curator's probes (plan correction-recurrence, criterion a4): what each probe run of the ISO
weeks found (``service.runs`` queues them, ``evo_agents.hub.probes`` holds their shape, ``models.report`` the report's).

For each week (its Monday, in the charter's time zone), each probe and each runtime and model: the sessions and the
failed sessions of arm on and of arm off, their 1 - pass^k (``probes.recurrence``), the status (``probes.status_of``:
holds, recurring, not_reproduced, stale, unmeasured) and the status of the week before. An active probe of a merged
change that has no row of the week on a model of the charter is unmeasured too. Each week adds up its statuses and its
sessions, and what its probe runs cost (the usage of the project's runs of kind probe whose night falls in the week)
against the charter's probes.budget_usd, and where the arms of each result ran (``arms_at``: ``tip``, the tip of the
default branch, or ``change``, the change's own commits when its diff no longer came off the tip; null without a
session). The report also names the merged changes that have no active probe (``unprobed``, plan
probes-measure-both-arms, criterion a4): each whose proposal changes a file a probe arm places (``probes.probe_paths``),
whatever its evidence, its reason saying whether it was born of a correction (its proposal's evidence cites
``corrections`` of the night's figures, ``ledger.cites_corrections``), and each born of a correction that places no
such file, as a probe cannot measure it; each with why, the fix proposals of recurring probes with their state (``fixes``) and the probes written in
place of probes not reproduced or stale (``replacements``), those still open or made since the oldest week shown
(``service.fixes`` says when a review run is asked for them).

GET /v1/projects/{p}/curator/probe-report (reader, ``routes.report``, ``weekly``) answers it for ``weeks`` weeks, the newest first: the week of
``week`` (a day; default today in the charter's time zone, the week in progress, whose ``complete`` is false) and those
before it; the probes and changes whose label the caller's grant reaches through the sink X-Evo-Sink
names (else the project's hub sink). `evo-agents hub curator probe report` prints it, and the MCP tool
curator_probe_report gives it to a review run (``evo_agents.hub.server.probes.mcp``). No part of it holds a probe's verify.

``send`` is the weekly notice: on Monday, at the charter's brief_at in its time zone (``curator.brief_due``), the morning
brief's job (``brief.send_briefs``) tells the owner of the night shift's schedule of a project whose charter runs
probes the report of the ISO week that just ended, as a notice curator_probe_report on the web and on every channel of
theirs, Telegram included. The notice names its week in ``details``; under the schedule's row lock, a week told is not
told again.
"""

from __future__ import annotations

import logging
from datetime import date

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, probes, runs
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.curator.repository.charter import charter_row
from evo_agents.hub.server.curator.service.shift import owner_access
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.memories.service.audience import Reader, label_reaches
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.probes.models.report import (
    ARMS_AT_WORDS,
    BORN,
    DEFAULT_WEEKS,
    MAX_NOTICE_LISTED,
    MAX_UNPROBED,
    NO_RUN,
    NOT_BORN,
    NOT_PLACED,
    NOT_WRITTEN,
    NOT_YET,
    WEEK,
    ProbeArmCount,
    ProbeFix,
    ProbeReplacement,
    ProbeUnprobed,
    ProbeWeek,
    ProbeWeekly,
    ProbeWeekResult,
    ProbeWeekTotals,
)
from evo_agents.hub.server.probes.repository import report as repository
from evo_agents.hub.server.probes.service.runs import probe_models
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)


# Reading


def _arm(sessions, arm: str, k: int | None) -> ProbeArmCount:
    count, failed = probes.arm_counts(sessions or [], arm)
    return ProbeArmCount(runs=count, failed=failed, recurrence=probes.recurrence(count, failed, k or probes.DEFAULT_K))


def _rule(row) -> dict:
    """The plugin and rule a probe of ``row`` measures; none for the probe of a change."""
    return {"plugin": row.plugin, "ref": row.ref} if row.ref is not None else {}


def _result(row, *, k: int | None = None, reason: str | None = None) -> dict:
    """The week's result of a curator_probe_runs row (``k`` and ``reason`` alone for a pair the week has no row of)."""
    if row is None:
        none = ProbeArmCount(runs=0, failed=0, recurrence=None)
        return {
            "k": k,
            "on": none,
            "off": none,
            "status": "unmeasured",
            "reason": reason,
            "run_id": None,
            "arms_at": None,
        }
    on, off = _arm(row.sessions, "on", row.k), _arm(row.sessions, "off", row.k)
    status = probes.status_of(row.state, on.failed, off.runs, off.failed)
    why = row.reason if status in ("stale", "unmeasured") else None
    if status == "unmeasured" and not why:
        why = f"its probe run is {row.state}"
    arms_at = row.arms_at if row.sessions else None
    return {"k": row.k, "on": on, "off": off, "status": status, "reason": why, "run_id": row.run_id, "arms_at": arms_at}


def _totals(results: list[ProbeWeekResult]) -> ProbeWeekTotals:
    counts = {status: sum(item.status == status for item in results) for status in probes.STATUSES}

    def arm(side: str) -> ProbeArmCount:
        count = sum(getattr(item, side).runs for item in results)
        return ProbeArmCount(runs=count, failed=sum(getattr(item, side).failed for item in results), recurrence=None)

    return ProbeWeekTotals(probes=len(results), **counts, on=arm("on"), off=arm("off"))


async def local_today(conn: AsyncConnection, body: dict) -> date:
    """Today in the charter's time zone (UTC without a charter)."""
    return (await repository.local_now(conn, curator.zone_of(body))).date()


class _Reach:
    """Whether a label reaches the caller through the sink of the read, each label read once."""

    def __init__(self, conn: AsyncConnection, access: ProjectAccess, user: Principal, sink: str | None):
        self.reader, self.access, self.through = Reader(conn, user), access, plan_service.read_sink(access, sink)
        self.seen: dict[str, bool] = {}

    async def __call__(self, label) -> bool:
        key = repr(label)
        if key not in self.seen:
            self.seen[key] = await label_reaches(self.reader, self.access, label, self.through)  # pyright: ignore[reportArgumentType]
        return self.seen[key]


async def _week_results(
    conn: AsyncConnection, access: ProjectAccess, reach: _Reach, weeks: list[date], body: dict, current: date
):
    """{week: {(probe, runtime, model): (change, result)}} of ``weeks`` (any order), the probes the caller reads."""
    rows = await repository.week_rows(conn, access.project_id, weeks)
    active = await repository.active_probes(conn, access.project_id)
    models = probe_models(body) if (body.get("probes") or {}).get("budget_usd") is not None else []
    k = int((body.get("probes") or {}).get("k") or probes.DEFAULT_K)
    found: dict[date, dict] = {week: {} for week in weeks}
    for row in rows:
        if await reach(row.probe_label):
            found[row.week][(row.probe_id, row.runtime, row.model)] = (row.change_id, _result(row) | _rule(row))
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
                    found[week][key] = (probe.change_id, _result(None, k=k, reason=why) | _rule(probe))
    return found


async def _unprobed_reason(conn: AsyncConnection, row) -> str | None:
    """Why merged change ``row`` has no probe, or None when it is not one the report names."""
    placed = probes.probe_paths(item.get("path") for item in row.paths or [] if isinstance(item, dict))  # pyright: ignore[reportArgumentType]
    born = await ledger_lines.cites_corrections(conn, row)
    if not placed and not born:
        return None
    retired = await repository.last_retired(conn, row.id)
    if not placed:
        why = NOT_PLACED
    elif retired is not None:
        why = f"its probe #{retired.id} was retired" + (f": {retired.retired_note}" if retired.retired_note else "")
    else:
        why = NOT_WRITTEN
    return why + f"; {BORN if born else NOT_BORN}"


async def _unprobed(conn: AsyncConnection, access: ProjectAccess, reach: _Reach) -> list[ProbeUnprobed]:
    found = []
    for row in await repository.unprobed(conn, access.project_id):
        if len(found) >= MAX_UNPROBED:
            break
        if not await reach(row.label):
            continue
        why = await _unprobed_reason(conn, row)
        if why is None:
            continue
        found.append(
            ProbeUnprobed(
                change_id=row.id, proposal_id=row.proposal_id, title=row.title, merged_at=row.merged_at, reason=why
            )
        )
    return found


async def _fixes(conn: AsyncConnection, access: ProjectAccess, reach: _Reach, since: date) -> list[ProbeFix]:
    """The fix proposals of the project still open, or made since ``since``, the newest first."""
    return [
        ProbeFix(proposal_id=row.id, change_id=row.fix_of, title=row.title, state=row.state)
        for row in await repository.fixes(conn, access.project_id, since)
        if await reach(row.label)
    ]


async def _replacements(
    conn: AsyncConnection, access: ProjectAccess, reach: _Reach, since: date
) -> list[ProbeReplacement]:
    """The probes of the project made since ``since`` in place of others, the newest first."""
    return [
        ProbeReplacement(
            probe_id=row.id, replaces=row.replaces, change_id=row.change_id, state=row.state, created_at=row.created_at
        )
        for row in await repository.replacements(conn, access.project_id, since)
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
    charter = await charter_row(conn, access.project_id)
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
        results.sort(
            key=lambda item: (
                item.change_id is None,
                item.change_id or 0,
                item.probe_id,
                item.runtime,
                item.model or "",
            )
        )
        shown.append(
            ProbeWeek(
                week=week,
                complete=week < current,
                cost_usd=await repository.week_cost(conn, access.project_id, week),
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


async def weekly(
    engine: AsyncEngine, user: Principal, project: str, *, weeks: int, newest: date | None, sink: str | None
) -> ProbeWeekly:
    """The report of the project's probes the route answers (``build``), in a transaction of its own; 403 without a
    grant on the project."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        need_reader(access)
        return await build(conn, access, user, weeks=weeks, newest=newest, sink=sink)


# Words


def _who(item: ProbeWeekResult) -> str:
    what = f"change #{item.change_id}"
    if item.change_id is None and item.ref is not None:
        what = f"plugin {item.plugin or 'removed'} rule {item.ref}"
    return f"probe #{item.probe_id} of {what} on {item.runtime}{f' {item.model}' if item.model else ''}"


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
    if item.arms_at is not None:
        words += f", {ARMS_AT_WORDS[item.arms_at]}"
    if item.previous is not None:
        words += f"; the week before {item.previous.replace('_', ' ')}"
    return words


def _cost(week: ProbeWeek) -> str:
    cap = f" of {curator.money(week.budget_usd)}" if week.budget_usd is not None else ""
    return f"{curator.money(week.cost_usd)}{cap}"


def arms_at_counts(week: ProbeWeek) -> dict[str, int]:
    """The results of ``week`` whose arms ran at the tip, and at the change's commits."""
    return {where: sum(item.arms_at == where for item in week.probes) for where in probes.ARMS_AT}


def week_lines(week: ProbeWeek, *, listed: int | None = None) -> list[str]:
    totals = week.totals
    flagged = [item for item in week.probes if item.status in probes.FLAGGED]
    lines = [
        f"Week of {week.week.isoformat()}{'' if week.complete else ' (in progress)'}: {totals.probes} result(s), "
        f"{totals.holds} holds, {totals.recurring} recurring, {totals.not_reproduced} not reproduced, {totals.stale} "
        f"stale, {totals.unmeasured} unmeasured; arm on failed {totals.on.failed} of {totals.on.runs} sessions, arm "
        f"off {totals.off.failed} of {totals.off.runs}; cost {_cost(week)}."
    ]
    places = arms_at_counts(week)
    if any(places.values()):
        lines[0] += (
            f" Arms at the tip of the default branch: {places[probes.ARMS_AT_TIP]}; at the change's commits, its diff no "
            f"longer coming off the tip: {places[probes.ARMS_AT_CHANGE]}."
        )
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
        lines.append(
            f"Probes written in place of probes not reproduced or stale two weeks in a row: {len(found.replacements)}"
        )
        lines += [
            f"- probe #{item.probe_id} of change #{item.change_id} in place of probe #{item.replaces}" for item in shown
        ]
    return lines


def unprobed_lines(found: ProbeWeekly, *, listed: int | None = None) -> list[str]:
    if not found.unprobed:
        return []
    shown = found.unprobed if listed is None else found.unprobed[:listed]
    lines = [f"Changes without a probe a probe could measure, or born of a correction: {len(found.unprobed)}"]
    lines += [f"- change #{item.change_id} ({item.title}): {item.reason}" for item in shown]
    return lines


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
    await repository.lock_schedule(conn, due.id)
    week = probes.week_of(local.date()) - WEEK
    if await repository.told(conn, due.project_id, week):
        return "probe_report_sent"
    access = await owner_access(conn, due.owner_id, due.owner, due.project)
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
    details |= {f"arms_at_{where}": count for where, count in arms_at_counts(shown).items()}
    await NOTIFIER.notify(
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

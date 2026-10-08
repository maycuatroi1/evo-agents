"""The outcome of the Curator's merged changes, the revert the hub proposes, and the circuit breaker of the night shift.
``evo_agents.hub.ledger`` holds the model, ``evo_agents.hub.server.ledger`` the ledger's one writer.

``measure_outcomes`` is the job ``curator.outcomes``, every ten minutes: for each change of the Curator merged at least
the charter's ``outcome_days`` (7 by default) ago and not measured yet, in a transaction of its own under the change's
row lock, it counts the figures of the project again with ``curator.collect``'s own count (``collect.compute_figures``),
as the owner of the project's night shift reads it, over the ``outcome_days`` after the merge, and compares them with
those that set the proposal off (its first line in the ledger) per unit of activity (``ledger.outcome_of``). It adds the
line ``outcome`` (keep, revert or unclear) to the ledger, one per change, audited as curator.outcome. A ``revert`` makes
it propose the revert of the merge commit (``propose_revert``): a proposal of kind ``revert``, tier 1 at least, of the
same project, lens and label, whose draft plan reverts that commit alone and whose evidence is what the figures after
the merge hold for the metrics that got worse, ``revert_of`` naming the proposal it undoes. Its owner finds it in the
Inbox (a notification of kind proposal, whatever room the day's ``max_decisions_per_day`` leaves) and on Telegram, and
accepts it as any proposal of tier 1: it becomes the Curator's plan, which the night shift builds on a branch of its own
and whose pull request waits for its owner.

``check_circuit`` is the circuit breaker, which the night shift's gate (``curator.gate_of``) runs under the schedule's
row lock each minute, before anything else and whether the window is open or not: once the charter's
``circuit_breaker.max_failed_in_a_row`` jobs of the night in a row went wrong (``curator.failed_in_a_row``), counting
since the schedule last changed (a resume, a new worker), it pauses every schedule of the project with the reason
(``schedules.pause_reason``, no member in ``paused_by``), cancels the runs they queued that are still queued, tells the
owner of each schedule (the notice ``curator_paused``) and audits curator.circuit. A job of the night is a run of the
schedule for that night, which went wrong when it ended failed and right when it ended done (a cancelled run, and a lost
one the hub tries again, count neither way), or an outcome ``revert`` the hub recorded for the project in the night's
span, which went wrong. ``evo-agents hub curator resume`` lets the night shift run again.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy import exists, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import curator, ledger, review, tables, tiers
from evo_agents.hub.server import audit
from evo_agents.hub.server import ledger as ledger_lines

log = logging.getLogger(__name__)

OUTCOME = "curator.outcome"  # the figures of a merged change counted again: "<project> proposal:<id> change:<id> ..."
CIRCUIT = "curator.circuit"  # the circuit breaker paused the night shift: "<project> night=<date> failed=<n> ..."
NIGHT_SHIFT = "night_shift"
MAX_CHANGES = 50  # merged changes one pass of the job measures
CIRCUIT_DEFAULT = 2  # the charter's circuit_breaker.max_failed_in_a_row by default


def _days(charter: dict | None) -> int:
    found = (charter or {}).get("outcome_days")
    low, high = ledger.OUTCOME_DAYS
    return found if isinstance(found, int) and low <= found <= high else ledger.DEFAULT_OUTCOME_DAYS


async def _charter(conn: AsyncConnection, project_id: int) -> dict:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    return (await conn.execute(query)).scalar_one_or_none() or {}


async def outcome_days(conn: AsyncConnection, project_id: int) -> int:
    """The days after a merge the hub counts the figures of the project's changes over: the charter's
    ``outcome_days``."""
    return _days(await _charter(conn, project_id))


# The outcome


def _unmeasured():
    """The merged changes of the Curator with no outcome yet, the oldest merge first, with their project's name."""
    c, lg, p = tables.curator_changes, tables.curator_ledger, tables.projects
    measured = exists().where(lg.c.change_id == c.c.id, lg.c.action == "outcome")
    return (
        select(c.c.id, c.c.project_id, p.c.name.label("project"), c.c.merged_at)
        .join_from(c, p, p.c.id == c.c.project_id)
        .where(c.c.state == "merged", c.c.merged_at.is_not(None), ~measured)
        .order_by(c.c.merged_at, c.c.id)
        .limit(MAX_CHANGES)
    )


async def _owner(conn: AsyncConnection, project_id: int):
    """(owner id, login) of the project's night shift, or None without one."""
    s, u = tables.schedules, tables.users
    query = (
        select(s.c.owner_id, u.c.login)
        .join_from(s, u, u.c.id == s.c.owner_id)
        .where(s.c.project_id == project_id, s.c.kind == NIGHT_SHIFT)
    )
    return (await conn.execute(query)).one_or_none()


async def _trigger(conn: AsyncConnection, proposal) -> dict:
    """The figures that set ``proposal`` off: those of its first line, else counted now from its night's figures."""
    lg = tables.curator_ledger
    stored = (
        await conn.execute(
            select(lg.c.figures)
            .where(lg.c.proposal_id == proposal.id, lg.c.action == "proposed", lg.c.figures.is_not(None))
            .order_by(lg.c.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return stored if isinstance(stored, dict) else await ledger_lines.trigger_for(conn, proposal)


async def _measure(conn: AsyncConnection, change_id: int, moment: datetime) -> str:
    """Count the figures of merged change ``change_id`` again and record its outcome, in the caller's transaction; a
    word for the job's summary."""
    from evo_agents.hub.server.collect import compute_figures  # it queues runs through modules that import this one
    from evo_agents.hub.server.curator import _owner_access

    c, lg, p = tables.curator_changes, tables.curator_ledger, tables.proposals
    change = (await conn.execute(select(c).where(c.c.id == change_id).with_for_update(skip_locked=True))).one_or_none()
    if change is None or change.state != "merged" or change.merged_at is None:
        return "moved"
    done = (await conn.execute(select(exists().where(lg.c.change_id == change_id, lg.c.action == "outcome")))).scalar()
    if done:
        return "moved"
    charter = await _charter(conn, change.project_id)
    days = _days(charter)
    until = change.merged_at + timedelta(days=days)
    if until > moment:
        return "waiting"
    owner = await _owner(conn, change.project_id)
    project = (
        await conn.execute(select(tables.projects.c.name).where(tables.projects.c.id == change.project_id))
    ).scalar_one()
    access = None if owner is None else await _owner_access(conn, owner.owner_id, owner.login, project)
    if access is None:
        log.info("outcome waits for an owner who reads the project", extra={"change_id": change_id})
        return "owner"
    proposal = (await conn.execute(select(p).where(p.c.id == change.proposal_id))).one()
    trigger = await _trigger(conn, proposal)
    after = await compute_figures(conn, access, change.merged_at, until)
    found = ledger.outcome_of(trigger, after, since=change.merged_at.isoformat(), until=until.isoformat())
    revert_id = None
    if found["result"] == "revert":
        revert_id = await propose_revert(conn, change, proposal, found, after, charter, owner, project)
    what = f"Counted again over the {days} days after the merge: {found['reason']}"
    if revert_id is not None:
        what += f"; the hub proposed reverting it, proposal #{revert_id}"
    await ledger_lines.append(
        conn,
        project_id=change.project_id,
        proposal_id=change.proposal_id,
        change_id=change.id,
        action="outcome",
        actor="curator",
        outcome=found["result"],
        commit_sha=change.merge_sha,
        figures=found,
        what=what,
        details={"revert_proposal_id": revert_id, "outcome_days": days} if revert_id else {"outcome_days": days},
    )
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=OUTCOME,
        target=f"{project} proposal:{change.proposal_id} change:{change.id} outcome={found['result']}"
        + (f" revert:{revert_id}" if revert_id else ""),
        project_id=change.project_id,
    )
    log.info("curator outcome", extra={"change_id": change.id, "outcome": found["result"], "revert": revert_id})
    return found["result"]


def _evidence(after: dict, found: dict) -> list[dict]:
    pieces = []
    for text in ledger.worse_evidence(after, found):
        try:
            pieces.append({**review.parse_evidence(text), "resolved": "figures"})
        except review.EvidenceProblem:
            continue
    return pieces


async def propose_revert(conn: AsyncConnection, change, proposal, found: dict, after: dict, charter, owner, project):
    """Propose reverting the merge commit of ``change`` (of ``proposal``), whose figures got worse (``found``); the new
    proposal's id. Its owner gets it in the Inbox."""
    from evo_agents.hub.server.notifications import notify
    from evo_agents.hub.server.proposals import proposal_link

    p = tables.proposals
    paths = [
        (item["repo"], item["path"])
        for item in proposal.paths or []
        if isinstance(item, dict) and isinstance(item.get("repo"), str) and isinstance(item.get("path"), str)
    ]
    protected = [item for item in charter.get("protected_paths") or [] if isinstance(item, str)]
    tier = tiers.tier_of("revert", paths, protected)
    title = f"Revert: {proposal.title}"[: review.MAX_TITLE_CHARS]
    evidence = _evidence(after, found)
    finding_ids: list[int] = []
    count = len(evidence)
    if not evidence:  # no entry of the figures to point at: the evidence of the proposal it undoes
        evidence = [item for item in proposal.evidence or [] if isinstance(item, dict)]
        finding_ids = list(proposal.finding_ids or [])
        count = max(int(proposal.evidence_count or 0), 1)
    draft = ledger.revert_draft(
        proposal_id=proposal.id,
        title=proposal.title,
        change_id=change.id,
        repo=change.repo,
        merge_sha=change.merge_sha,
        pr_url=change.pr_url,
        reason=found["reason"],
    )
    summary = ledger.revert_summary(
        proposal_id=proposal.id, change_id=change.id, merge_sha=change.merge_sha, pr_url=change.pr_url, outcome=found
    )
    values = {
        "project_id": change.project_id,
        "run_id": proposal.run_id,  # the review run whose figures set off the proposal it undoes
        "lens": proposal.lens,
        "kind": "revert",
        "title": title,
        "summary": summary,
        "paths": [{"repo": repo, "path": path} for repo, path in paths],
        "tier": tier.tier,
        "tier_reasons": tier.reasons,
        "finding_ids": finding_ids,
        "evidence": evidence,
        "evidence_count": count,
        "draft": draft,
        "fingerprint": tiers.fingerprint("revert", paths, title),
        "state": "open",
        "label": proposal.label,
        "revert_of": proposal.id,
        "inbox_at": func.now() if owner is not None else None,
    }
    revert_id = (await conn.execute(insert(p).values(**values).returning(p.c.id))).scalar_one()
    await ledger_lines.append(
        conn,
        project_id=change.project_id,
        proposal_id=revert_id,
        action="proposed",
        actor="curator",
        what=f"The hub proposed reverting {change.merge_sha} of proposal #{proposal.id} (tier {tier.tier}): "
        f"{found['reason']}",
        commit_sha=change.merge_sha,
        figures=ledger.outcome_trigger(found),
        details={"kind": "revert", "tier": tier.tier, "lens": proposal.lens, "revert_of": proposal.id},
    )
    if owner is not None:
        await notify(
            conn,
            user_id=owner.owner_id,
            kind="proposal",
            project_id=change.project_id,
            run_id=proposal.run_id,
            proposal_id=revert_id,
            title=f"Proposal #{revert_id} (tier {tier.tier}): {title}",
            body=summary,
            details={
                "tier": tier.tier,
                "kind": "revert",
                "lens": proposal.lens,
                "evidence": count,
                "revert_of": proposal.id,
            },
            link=proposal_link(revert_id),
        )
    return revert_id


async def measure_outcomes(engine: AsyncEngine, *, now: datetime | None = None) -> dict:
    """One pass of ``curator.outcomes``, each change in a transaction of its own, at ``now`` (the database's now when
    None); how many changes ended in each outcome."""
    async with engine.begin() as conn:
        moment = now or (await conn.execute(select(func.now()))).scalar_one()
        due = (await conn.execute(_unmeasured())).all()
    outcomes: Counter = Counter()
    for row in due:
        if row.merged_at + timedelta(days=ledger.OUTCOME_DAYS[0]) > moment:
            outcomes["waiting"] += 1
            continue
        try:
            async with engine.begin() as conn:
                outcomes[await _measure(conn, row.id, moment)] += 1
        except Exception:  # one change failing leaves the others their turn
            log.exception("an outcome failed", extra={"change_id": row.id, "project": row.project})
            outcomes["failed"] += 1
    return dict(sorted(outcomes.items()))


# The circuit breaker


async def _night_jobs(conn: AsyncConnection, due, night: date, since: datetime) -> list[tuple]:
    """The night's jobs of schedule ``due`` since ``since``, oldest first: (when, went wrong, what), what being
    ``run:<id>`` or ``revert:<proposal id>``."""
    r, lg = tables.runs, tables.curator_ledger
    window = due.body["window"]
    opens = curator.parse_time(window["start"])
    first, last = datetime.combine(night, opens), datetime.combine(night + timedelta(days=1), opens)
    ran = await conn.execute(
        select(r.c.id, r.c.state, r.c.finished_at).where(
            r.c.schedule_id == due.id,
            r.c.schedule_night == night,
            r.c.state.in_(("done", "failed")),
            r.c.finished_at >= since,
        )
    )
    jobs = [(row.finished_at, row.state == "failed", f"run:{row.id}") for row in ran]
    local = func.timezone(window["timezone"], lg.c.created_at)
    reverted = await conn.execute(
        select(lg.c.created_at, lg.c.proposal_id).where(
            lg.c.project_id == due.project_id,
            lg.c.action == "outcome",
            lg.c.outcome == "revert",
            lg.c.created_at >= since,
            local >= first,
            local < last,
        )
    )
    jobs += [(row.created_at, True, f"revert:{row.proposal_id}") for row in reverted]
    return sorted(jobs)


def _named(items: list[str]) -> str:
    runs = [f"run #{item.split(':')[1]}" for item in items if item.startswith("run:")]
    reverts = [f"the change of proposal #{item.split(':')[1]}" for item in items if item.startswith("revert:")]
    parts = []
    if runs:
        parts.append(", ".join(runs) + " failed")
    if reverts:
        parts.append(", ".join(reverts) + " got worse figures and is to be reverted")
    return " and ".join(parts)


async def check_circuit(conn: AsyncConnection, due, night: date, since: datetime) -> bool:
    """Trip the circuit breaker of schedule ``due`` (a row of ``curator._due``, its row locked) for ``night`` when the
    night's last jobs since ``since`` went wrong in a row as often as the charter allows (see the module's docstring):
    pause the project's schedules, cancel what they queued, tell their owners. Whether it tripped."""
    breaker = due.body.get("circuit_breaker") or {}
    most = breaker.get("max_failed_in_a_row")
    most = most if isinstance(most, int) and most >= 1 else CIRCUIT_DEFAULT
    jobs = await _night_jobs(conn, due, night, since)
    count = curator.failed_in_a_row([wrong for _, wrong, _ in jobs])
    if count < most:
        return False
    which = [what for _, _, what in jobs[-count:]]
    reason = (
        f"The circuit breaker paused the night shift of {due.project}: {_named(which)}, {count} jobs of the night of "
        f"{night.isoformat()} in a row (max_failed_in_a_row {most}). Resume it with `evo-agents hub curator resume` "
        "once the cause is fixed."
    )
    await trip(conn, due, night, reason, which, count, most)
    return True


async def trip(conn: AsyncConnection, due, night: date, reason: str, which: list[str], count: int, most: int) -> None:
    from evo_agents.hub.server.curator import _cancel_queued
    from evo_agents.hub.server.notifications import notify

    s = tables.schedules
    paused = await conn.execute(
        update(s)
        .values(paused_at=func.now(), paused_by=None, pause_reason=reason, updated_at=func.now())
        .where(s.c.project_id == due.project_id, s.c.paused_at.is_(None))
        .returning(s.c.id, s.c.owner_id)
    )
    rows = paused.all()
    ids = [row.id for row in rows]
    cancelled = await _cancel_queued(conn, ids, reason) if ids else []
    run_ids = [int(item.split(":")[1]) for item in which if item.startswith("run:")]
    reverts = [int(item.split(":")[1]) for item in which if item.startswith("revert:")]
    for owner_id in sorted({row.owner_id for row in rows}):
        await notify(
            conn,
            user_id=owner_id,
            kind="notice",
            notice_kind="curator_paused",
            project_id=due.project_id,
            run_id=run_ids[-1] if run_ids else None,
            title=f"The night shift of {due.project} paused itself: {count} jobs in a row went wrong",
            body=reason,
            details={
                "night": night.isoformat(),
                "failed_in_a_row": count,
                "max_failed_in_a_row": most,
                "runs": run_ids,
                "reverted": reverts,
                "cancelled": cancelled,
            },
            link=f"/p/{due.project}/curator",
        )
    await audit.record(
        conn,
        actor_id=None,
        token_id=None,
        action=CIRCUIT,
        target=f"{due.project} night={night.isoformat()} failed={count} jobs={','.join(which)}",
        project_id=due.project_id,
    )
    log.warning(
        "circuit breaker tripped",
        extra={"project": due.project, "night": night.isoformat(), "jobs": which, "cancelled": cancelled},
    )

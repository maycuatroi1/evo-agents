"""The Curator's ledger on the hub: its one writer, the lines the hub adds as a proposal moves on, and the route that
reads a proposal's ledger. ``evo_agents.hub.ledger`` holds the model; ``evo_agents.hub.server.outcomes`` adds the
outcome of a merged change and the revert it proposes.

``append`` is the one statement that writes curator_ledger, an INSERT: nothing of the hub updates or deletes a line
(``tests/hub/test_curator_ledger.py`` reads the hub's code for one). The hub adds a line when a review run records a
proposal (``proposal_recorded``: ``proposed``, or ``dropped`` as a repeat of a rejected one, with the figures that set
it off, ``trigger_for``), when an admin answers it (``proposal_answered``), when its draft becomes the Curator's plan or
cannot (``change_recorded``), and at each move of its change (``change_moved``, from ``changes._set``): built by its
Builder, its pull request opened, judged (by the Judge, or by the hub's own detector of score hacking), merged (by the
hub, or by hand), left open for its owner with why, or closed; a Builder that ends otherwise adds ``build_failed``
(``build_failed``).

GET /v1/projects/{p}/curator/proposals/{id}/ledger lists the lines of a proposal the caller may read (as GET
.../proposals/{id} reads it), oldest first, with when the hub counts the figures of its merged change again
(``outcome_due_at``, null before the merge and once the outcome is in).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import exists, insert, null, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import ledger, review, tables
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs import MAX_ID
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["curator"], responses={401: {"model": ErrorBody}})
READ_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
ItemId = Annotated[int, Path(ge=1, le=MAX_ID)]


# Models


class LedgerLine(BaseModel):
    """One line of a proposal's ledger, as the hub added it."""

    id: int
    proposal_id: int
    change_id: int | None = Field(description="the change it became, once it became one")
    action: Literal[ledger.ACTIONS]
    actor: Literal[ledger.ACTORS] = Field(
        description="curator: the hub's own code; agent: an agent of a run of the Curator; user: a member"
    )
    actor_login: str | None = Field(description="the member, for a line of a user the hub knows")
    run_id: int | None = Field(description="the run of the agent: the review run, the Builder or the Judge")
    what: str = Field(description="what happened, in words")
    commit_sha: str | None = Field(description="the commit it is about: the head built, judged or merged")
    before_sha: str | None = Field(description="the default branch before the change")
    after_sha: str | None = Field(description="the default branch after it: the merge commit")
    figures: dict | None = Field(
        description="proposed: the figures that set it off; outcome: those figures before and after the merge"
    )
    verdict: dict | None = Field(description="judged: whether it passed, the agent's verdict, the failures")
    pr_url: str | None
    pr_number: int | None
    merged_at: datetime | None
    outcome: Literal[ledger.OUTCOMES] | None = Field(description="outcome: keep, revert or unclear")
    details: dict | None
    created_at: datetime


class Ledger(BaseModel):
    project: str
    proposal_id: int
    lines: list[LedgerLine] = Field(description="oldest first")
    outcome_due_at: datetime | None = Field(
        description="when the hub counts the figures of its merged change again; null before the merge and once "
        "its outcome is in"
    )


# Writing


def _what(text: str) -> str:
    flat = " ".join(str(text or "").split()) or "-"
    return flat if len(flat) <= ledger.MAX_WHAT_CHARS else flat[: ledger.MAX_WHAT_CHARS - 3].rstrip() + "..."


def _sha(value) -> str | None:
    return value if isinstance(value, str) and SHA.match(value) else None


def _json(value):
    return value if isinstance(value, dict) else null()


async def append(
    conn: AsyncConnection,
    *,
    project_id: int,
    proposal_id: int,
    action: str,
    actor: str,
    what: str,
    change_id: int | None = None,
    actor_id: int | None = None,
    run_id: int | None = None,
    commit_sha: str | None = None,
    before_sha: str | None = None,
    after_sha: str | None = None,
    figures: dict | None = None,
    verdict: dict | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
    merged_at=None,
    outcome: str | None = None,
    details: dict | None = None,
) -> int:
    """Add a line to proposal ``proposal_id``'s ledger, in the caller's transaction; its id. The one write of
    curator_ledger: lines are never changed or deleted."""
    if action not in ledger.ACTIONS or actor not in ledger.ACTORS:
        raise ValueError(f"no ledger line is {action!r} by {actor!r}")
    if actor == "agent" and run_id is None:
        raise ValueError("a line of an agent names its run")
    values = {
        "project_id": project_id,
        "proposal_id": proposal_id,
        "change_id": change_id,
        "action": action,
        "actor": actor,
        "actor_id": actor_id if actor == "user" else None,
        "run_id": run_id,
        "what": _what(what),
        "commit_sha": _sha(commit_sha),
        "before_sha": _sha(before_sha),
        "after_sha": _sha(after_sha),
        "figures": _json(figures),
        "verdict": _json(verdict),
        "pr_url": pr_url or None,
        "pr_number": pr_number if isinstance(pr_number, int) and pr_number >= 1 else None,
        "merged_at": merged_at,
        "outcome": outcome,
        "details": _json(details),
    }
    lg = tables.curator_ledger
    return (await conn.execute(insert(lg).values(**values).returning(lg.c.id))).scalar_one()


async def _evidence_texts(conn: AsyncConnection, proposal) -> list[str]:
    """The evidence of a proposal (a row with ``evidence`` and ``finding_ids``) and of its findings, as text."""
    items = [item for item in proposal.evidence or [] if isinstance(item, dict)]
    ids = list(proposal.finding_ids or [])
    if ids:
        f = tables.findings
        for evidence in (await conn.execute(select(f.c.evidence).where(f.c.id.in_(ids)))).scalars():
            items += [item for item in evidence or [] if isinstance(item, dict)]
    return [review.evidence_text(item) for item in items]


async def trigger_for(conn: AsyncConnection, proposal) -> dict:
    """The figures that set off ``proposal`` (a row with ``run_id``, ``lens``, ``evidence`` and ``finding_ids``):
    ``ledger.trigger_of`` over the night's figures of the review run that wrote it."""
    cf, r = tables.curator_figures, tables.runs
    row = (
        await conn.execute(
            select(cf.c.figures, cf.c.night).where(cf.c.run_id == proposal.run_id).order_by(cf.c.id).limit(1)
        )
    ).one_or_none()
    if row is None:  # a run whose figures were counted, then queued again: the night's figures all the same
        run = (await conn.execute(select(r.c.project_id, r.c.schedule_night).where(r.c.id == proposal.run_id))).one()
        row = (
            await conn.execute(
                select(cf.c.figures, cf.c.night).where(
                    cf.c.project_id == run.project_id, cf.c.night == run.schedule_night
                )
            )
        ).one_or_none()
    figures = row.figures if row is not None else None
    texts = await _evidence_texts(conn, proposal)
    return ledger.trigger_of(figures, proposal.lens, texts, night=row.night.isoformat() if row is not None else None)


async def proposal_recorded(conn: AsyncConnection, project_id: int, proposal_id: int) -> None:
    """The first line of a proposal a review run recorded: ``proposed``, or ``dropped`` as a repeat, by the run's
    agent, with the figures that set it off."""
    p = tables.proposals
    row = (
        await conn.execute(
            select(
                p.c.id,
                p.c.run_id,
                p.c.lens,
                p.c.kind,
                p.c.tier,
                p.c.title,
                p.c.state,
                p.c.duplicate_of,
                p.c.evidence,
                p.c.finding_ids,
                p.c.evidence_count,
            ).where(p.c.id == proposal_id)
        )
    ).one()
    dropped = row.state == "dropped"
    what = (
        f"Review run #{row.run_id} proposed it ({row.kind}, tier {row.tier}): {row.title}"
        if not dropped
        else f"Review run #{row.run_id} proposed it again; dropped as a repeat of rejected proposal #{row.duplicate_of}"
    )
    await append(
        conn,
        project_id=project_id,
        proposal_id=proposal_id,
        action="dropped" if dropped else "proposed",
        actor="agent",
        run_id=row.run_id,
        what=what,
        figures=await trigger_for(conn, row),
        details={
            "kind": row.kind,
            "tier": row.tier,
            "lens": row.lens,
            "evidence": row.evidence_count,
            "duplicate_of": row.duplicate_of,
        },
    )


async def proposal_answered(
    conn: AsyncConnection,
    *,
    project_id: int,
    proposal_id: int,
    user_id: int,
    login: str,
    action: str,
    note: str | None,
    days: int | None,
    via: str | None,
) -> None:
    """The line of an admin's answer: accepted, rejected or deferred, by them."""
    state = review.ANSWERED[action]
    what = f"{login} {state} it"
    if action == "defer" and days:
        what += f" for {days} days"
    if via:
        what += f" ({via})"
    if note:
        what += f": {note}"
    await append(
        conn,
        project_id=project_id,
        proposal_id=proposal_id,
        action=state,
        actor="user",
        actor_id=user_id,
        what=what,
        details={"note": note, "via": via, **({"days": days} if days else {})},
    )


async def change_recorded(conn: AsyncConnection, change_id: int) -> None:
    """The line of what an accepted proposal became: the Curator's plan on its branch, or a change left open."""
    c = tables.curator_changes
    row = (await conn.execute(select(c).where(c.c.id == change_id))).one()
    if row.plan_id is None:
        await append(
            conn,
            project_id=row.project_id,
            proposal_id=row.proposal_id,
            change_id=row.id,
            action="left_open",
            actor="curator",
            what=row.reason or "it could not become the Curator's plan",
        )
        return
    await append(
        conn,
        project_id=row.project_id,
        proposal_id=row.proposal_id,
        change_id=row.id,
        action="planned",
        actor="curator",
        what=f"It became the Curator's plan {row.plan_id}, on {row.branch} of {row.repo} alone",
        details={"plan_id": row.plan_id, "repo": row.repo, "branch": row.branch, "forge": row.forge},
    )


def _verdict(row) -> dict:
    found = row.verdict if isinstance(row.verdict, dict) else {}
    return {
        "passed": bool(row.passed),
        "source": found.get("source"),
        "agent": found.get("agent"),
        "failures": list(found.get("failures") or []),
        "signs": len(found.get("signs") or []),
        "verify": len(found.get("verify") or []),
        "hidden": len(found.get("hidden") or []),
    }


async def _builder_commit(conn: AsyncConnection, run_id: int | None) -> str | None:
    """The commit the Builder's last step report named, else the run's own."""
    if run_id is None:
        return None
    e, r = tables.run_events, tables.runs
    reported = e.c.body["step_report"]["commit_sha"].astext
    found = (
        await conn.execute(
            select(reported)
            .where(e.c.run_id == run_id, e.c.kind == "system", reported.is_not(None))
            .order_by(e.c.seq.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return found or (await conn.execute(select(r.c.commit_sha).where(r.c.id == run_id))).scalar_one_or_none()


async def change_moved(conn: AsyncConnection, change_id: int, old: str, new: str, extra: dict | None = None) -> None:
    """The lines a move of change ``change_id`` from ``old`` to ``new`` adds, from the change as it is now and
    ``extra`` (``actor``, ``actor_id``, ``login``, ``before_sha`` of a merge)."""
    extra = extra or {}
    c = tables.curator_changes
    row = (await conn.execute(select(c).where(c.c.id == change_id))).one()
    line = {"project_id": row.project_id, "proposal_id": row.proposal_id, "change_id": row.id}
    pull = {"pr_url": row.pr_url, "pr_number": row.pr_number}
    if old == "planned" and new in ("pr_pending", "judge_pending") and row.builder_run_id is not None:
        where = "the hub opens its pull request" if new == "pr_pending" else "its merge request is open"
        await append(
            conn,
            **line,
            action="built",
            actor="agent",
            run_id=row.builder_run_id,
            commit_sha=await _builder_commit(conn, row.builder_run_id),
            what=f"Builder run #{row.builder_run_id} did every step on {row.branch} of {row.repo}: {where}",
            details={"branch": row.branch, "repo": row.repo},
        )
        return
    if old == "pr_pending" and new in ("judge_pending", "judged"):
        await append(
            conn,
            **line,
            **pull,
            action="pull_opened",
            actor="curator",
            commit_sha=row.head_sha,
            what=f"The hub opened pull request #{row.pr_number} of {row.branch} into {row.base_branch}",
        )
    if new == "judged":
        verdict = _verdict(row)
        by_hub = verdict["source"] == "hub" or row.judge_run_id is None
        failures = "; ".join(verdict["failures"])
        if by_hub:
            what = f"The hub failed it before any Judge: {failures or 'signs of score hacking'}"
        else:
            what = f"Judge run #{row.judge_run_id} {'passed' if row.passed else 'failed'} it"
            what += f": {failures}" if failures and not row.passed else ""
        base = (row.verdict or {}).get("base_sha") if isinstance(row.verdict, dict) else None
        await append(
            conn,
            **line,
            **pull,
            action="judged",
            actor="curator" if by_hub else "agent",
            run_id=None if by_hub else row.judge_run_id,
            commit_sha=row.head_sha,
            before_sha=base,
            verdict=verdict,
            what=what,
        )
        return
    if new == "merged":
        actor = extra.get("actor") or "curator"
        who = "The hub" if actor == "curator" else (extra.get("login") or "A member")
        await append(
            conn,
            **line,
            **pull,
            action="merged",
            actor=actor,
            actor_id=extra.get("actor_id"),
            commit_sha=row.head_sha,
            before_sha=extra.get("before_sha"),
            after_sha=row.merge_sha,
            merged_at=row.merged_at,
            what=f"{who} merged pull request #{row.pr_number} into {row.base_branch} as {row.merge_sha}",
            details={"github_login": extra["login"]} if extra.get("login") else None,
        )
        return
    if new == "open" and old != "open":
        await append(conn, **line, **pull, action="left_open", actor="curator", what=row.reason or "left open")
        return
    if new == "closed":
        who = extra.get("login") or "A member"
        await append(
            conn,
            **line,
            **pull,
            action="closed",
            actor="user",
            actor_id=extra.get("actor_id"),
            what=f"{who} closed pull request #{row.pr_number} without a merge",
            details={"github_login": extra["login"]} if extra.get("login") else None,
        )


async def build_failed(conn: AsyncConnection, change, run_id: int, state: str, why: str) -> None:
    """The line of a Builder of ``change`` (a row of curator_changes) that ended ``state`` without its plan done."""
    await append(
        conn,
        project_id=change.project_id,
        proposal_id=change.proposal_id,
        change_id=change.id,
        action="build_failed",
        actor="agent",
        run_id=run_id,
        what=f"Builder run #{run_id} ended {state}: {why}",
    )


# Reading


def _lines(proposal_id: int):
    lg, u = tables.curator_ledger, tables.users
    return (
        select(
            *(column for column in lg.c if column.name != "project_id"),
            u.c.login.label("actor_login"),
        )
        .select_from(lg.outerjoin(u, u.c.id == lg.c.actor_id))
        .where(lg.c.proposal_id == proposal_id)
        .order_by(lg.c.id)
    )


async def outcome_due_at(conn: AsyncConnection, proposal_id: int, project_id: int):
    """When the hub counts the figures of proposal ``proposal_id``'s merged change again; None when it is not merged
    or its outcome is in."""
    c, lg = tables.curator_changes, tables.curator_ledger
    row = (
        await conn.execute(
            select(c.c.id, c.c.merged_at).where(c.c.proposal_id == proposal_id, c.c.merged_at.is_not(None))
        )
    ).one_or_none()
    if row is None:
        return None
    measured = (
        await conn.execute(select(exists().where(lg.c.change_id == row.id, lg.c.action == "outcome")))
    ).scalar_one()
    if measured:
        return None
    from evo_agents.hub.server.outcomes import outcome_days

    return row.merged_at + timedelta(days=await outcome_days(conn, project_id))


@router.get("/{project}/curator/proposals/{proposal_id}/ledger", response_model=Ledger, responses=READ_REFUSALS)
async def show_ledger(
    request: Request,
    project: ProjectName,
    proposal_id: ItemId,
    user: CurrentUser,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> Ledger:
    """The ledger of a proposal: what happened to it, line by line, oldest first."""
    from evo_agents.hub.server.proposals import _readable_proposal, _reader  # it writes lines through this module

    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        await _readable_proposal(conn, access, proposal_id, sink)
        rows = (await conn.execute(_lines(proposal_id))).all()
        due = await outcome_due_at(conn, proposal_id, access.project_id)
    return Ledger(
        project=access.name,
        proposal_id=proposal_id,
        lines=[LedgerLine(**row._mapping) for row in rows],
        outcome_due_at=due,
    )

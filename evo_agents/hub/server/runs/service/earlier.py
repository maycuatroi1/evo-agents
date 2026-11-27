"""What earlier review nights found and what became of it (``evo_agents.hub.server.earlier``): the rules that give a
finding its status from its proposals, the reads through a sink, what a review run's prompt shows, and the check that
refuses a closed finding recorded again."""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import review
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.forge.service.protection import need_reader
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.runs.models.earlier import (
    CLOSED,
    PER_LENS,
    PRECEDENCE,
    REJECTED_SHOWN,
    SCAN,
    SIMILAR,
    EarlierFinding,
    EarlierProposal,
    EarlierView,
    RejectedProposal,
)
from evo_agents.hub.server.runs.repository import earlier as repository
from evo_agents.hub.server.security import Principal

# The rules


def title_words(title: str) -> set[str]:
    return set(re.findall(r"\w{3,}", (title or "").lower()))


def similar(a: str, b: str) -> bool:
    """Whether titles ``a`` and ``b`` share SIMILAR of their words."""
    left, right = title_words(a), title_words(b)
    return bool(left and right) and len(left & right) / len(left | right) >= SIMILAR


def proposal_status(state: str, change_state: str | None, merged_at, outcome: str | None) -> str:
    if merged_at is not None or change_state == "merged":
        return {"keep": "kept", "revert": "reverted"}.get(outcome or "", "merged")
    if state in ("rejected", "dropped"):
        return "rejected"
    return "proposed"


def finding_status(statuses: list[str]) -> str:
    return next((status for status in PRECEDENCE if status in statuses), "open")


def texts_of(evidence) -> list[str]:
    return [review.evidence_text(item) for item in evidence or [] if isinstance(item, dict)]


# Reading


async def _proposals_of(
    conn: AsyncConnection, access: ProjectAccess, through: str | None, ids: list[int]
) -> dict[int, list[EarlierProposal]]:
    """The proposals readable through ``through`` that name each of findings ``ids``, oldest first."""
    if through is None or not ids:
        return {}
    found: dict[int, list[EarlierProposal]] = {}
    for row in await repository.proposals_naming(conn, access.project_id, ids):
        if not access.visible(row.label, through):
            continue
        status = proposal_status(row.state, row.change_state, row.merged_at, row.outcome)
        view = EarlierProposal(
            id=row.id,
            kind=row.kind,
            tier=row.tier,
            state=row.state,
            change_state=row.change_state,
            outcome=row.outcome,
            status=status,
            note=row.note,
        )
        for finding_id in row.finding_ids or []:
            if finding_id in ids:
                found.setdefault(finding_id, []).append(view)
    return found


def _view(row, proposals: list[EarlierProposal]) -> EarlierFinding:
    status = finding_status([item.status for item in proposals])
    return EarlierFinding(
        id=row.id,
        run_id=row.run_id,
        night=row.schedule_night or row.created_at.date(),
        lens=row.lens,
        severity=row.severity,
        title=row.title,
        status=status,
        closed=status in CLOSED,
        proposals=proposals,
        evidence=texts_of(row.evidence),
    )


async def findings_of(
    conn: AsyncConnection,
    access: ProjectAccess,
    through: str | None,
    *,
    lenses: list[str] | None = None,
    status: str | None = None,
    limit: int = 20,
    per_lens: int | None = None,
) -> list[EarlierFinding]:
    """The findings of the project readable through ``through``, newest first, each with its status."""
    rows = [
        row
        for row in await repository.findings(conn, access.project_id, lenses)
        if through is not None and access.visible(row.label, through)
    ]
    proposals = await _proposals_of(conn, access, through, [row.id for row in rows])
    found, per = [], {}
    for row in rows:
        view = _view(row, proposals.get(row.id, []))
        if status is not None and view.status != status:
            continue
        if per_lens is not None:
            per[row.lens] = per.get(row.lens, 0) + 1
            if per[row.lens] > per_lens:
                continue
        found.append(view)
        if len(found) >= limit:
            break
    return found


async def rejected_lately(conn: AsyncConnection, access: ProjectAccess, through: str | None, limit: int) -> list:
    found = []
    for row in await repository.rejected_lately(conn, access.project_id):
        if through is not None and access.visible(row.label, through):
            found.append(RejectedProposal(**{key: getattr(row, key) for key in RejectedProposal.model_fields}))
            if len(found) >= limit:
                break
    return found


def _hub_sink(access: ProjectAccess) -> str | None:
    return access.rules.hub_sink.sink if access.rules.hub_sink else None


async def for_prompt(conn: AsyncConnection, access: ProjectAccess, lenses: list[str]) -> dict:
    """What the prompt of a review run of ``lenses`` shows of earlier nights, as its owner (``access``) reads them."""
    through = _hub_sink(access)
    found = await findings_of(conn, access, through, lenses=lenses, limit=PER_LENS * len(lenses), per_lens=PER_LENS)
    by_lens = {lens: [] for lens in lenses}
    for item in found:
        by_lens.setdefault(item.lens, []).append(item.model_dump(mode="json"))
    rejected = await rejected_lately(conn, access, through, REJECTED_SHOWN)
    return {"findings": by_lens, "rejected": [item.model_dump(mode="json") for item in rejected]}


async def closed_like(conn: AsyncConnection, access: ProjectAccess, lens: str, title: str, evidence: list[dict]):
    """The closed finding of ``lens`` the new one repeats (see ``evo_agents.hub.server.earlier``): its view, or
    None."""
    texts = set(texts_of(evidence))
    for found in await findings_of(conn, access, _hub_sink(access), lenses=[lens], status=None, limit=SCAN):
        cited = set(found.evidence)
        if found.closed and texts <= cited and (similar(title, found.title) or texts == cited):
            return found
    return None


async def earlier_view(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    sink: str | None,
    *,
    lens: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> EarlierView:
    """The findings ``user`` may read through ``sink``, with their status, and the proposals rejected lately."""
    access = await project_access(conn, user, project)
    need_reader(access)
    through = plan_service.read_sink(access, sink)
    lenses = [lens] if lens is not None else None
    found = await findings_of(conn, access, through, lenses=lenses, status=status, limit=limit)
    rejected = [item for item in await rejected_lately(conn, access, through, limit) if lens in (None, item.lens)]
    return EarlierView(project=access.name, findings=found, rejected=rejected)


async def show(
    engine: AsyncEngine,
    user: Principal,
    project: str,
    sink: str | None,
    *,
    lens: str | None,
    status: str | None,
    limit: int,
) -> EarlierView:
    """``earlier_view`` in a transaction of its own."""
    async with UnitOfWork(engine) as conn:
        return await earlier_view(conn, user, project, sink, lens=lens, status=status, limit=limit)

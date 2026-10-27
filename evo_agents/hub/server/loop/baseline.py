"""The baseline of a goal that aims at an SRS document, proposed from the knowledge graph and confirmed by an admin
(plan srs-backlog-loop, criterion a5; the pure rules are ``evo_agents.hub.srs_baseline``).

- ``view`` gives each approved use case of the goal's scope with the state the hub proposes (``srs_baseline.propose``)
  and its evidence, or, once the ledger holds a row for it (an admin's fix, a confirmation, the Loop), that row's state.
  Nothing is written.
- ``fix`` sets use cases to delivered or undelivered by code, by heading and for the whole of the unknown ones: ledger
  rows of source ``admin``, which a confirmation keeps. A use case the Loop has under way (``delivering``) is left.
- ``confirm`` writes a ledger row of source ``kg`` for each use case of the scope that has none, in its proposed state,
  and marks the goal confirmed for the document (``srs_baselines``), once: 409 the second time. An admin of the project
  only, never the agent of a run.
- ``confirmed`` is whether the goal's baseline is confirmed for the document it aims at now; the Loop makes no build
  for a goal for which it is False, and a use case the baseline leaves unknown never enters a backlog.
- ``waiting`` lists the active goals of a project that aim at a document and are not confirmed: the items of the
  decision board's gate 3 (``board_gates``).

The graph is read by the caller (``evo_agents.hub.server.kg.delivery_evidence``) and passed in as ``evidence``: use case
code to what it shows, or None when the graph could not be read (``why`` says why), in which case every use case with no
ledger row is proposed unknown, never undelivered.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import goals as goal_rules
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.hub_events import SRS_USE_CASE_APPROVED
from evo_agents.hub.server import audit
from evo_agents.hub.server.loop import backlog, delivery
from evo_agents.hub.srs_backlog import ADMIN, DELIVERING, KG, STATES, UNKNOWN, UseCase, ref_key, under
from evo_agents.hub.srs_baseline import FIX_STATES, Evidence, Proposal, counts, fixes, propose

log = logging.getLogger(__name__)

CONFIRM = "srs.baseline.confirm"  # an admin confirmed the baseline of a goal: "<project>/goal G-<n>"
FIX = "srs.baseline.fix"  # an admin set use cases of a baseline: "<project>/goal G-<n> use_cases=<count>"
MAX_FIXES = 500  # codes and headings one fix names


class BaselineEvidence(BaseModel):
    kind: str = Field(description="plan_completed, step_done, commit_merged, pr_merged or code_comment")
    ref: str = Field(description="the id of the node of the graph it is read from")
    title: str


class BaselineItem(BaseModel):
    """An approved use case of the goal's scope with its state in the baseline."""

    code: str
    title: str
    heading: str | None
    state: Literal[STATES]
    source: Literal["kg", "admin", "deliver", "proposal"] = Field(
        description="proposal: the hub's proposal, not in the ledger yet; kg, admin and deliver: the ledger row's"
    )
    reason: str
    evidence: list[BaselineEvidence]


class SrsBaseline(BaseModel):
    """The baseline of a goal that aims at an SRS document: proposed until an admin confirms it."""

    project: str
    goal: str
    document: str
    heading: str | None
    confirmed: bool = Field(description="an admin confirmed it: the Loop makes builds for the goal's backlog")
    confirmed_at: datetime | None
    confirmed_by: str | None
    graph: bool = Field(description="whether the knowledge graph could be read for the evidence")
    why: str | None = Field(description="why the graph could not be read")
    counts: dict[Literal[STATES], int]
    items: list[BaselineItem]


class BaselineFix(BaseModel):
    """What an admin sets: use cases by code, by heading (every use case under it) and the unknown ones as a whole."""

    codes: dict[str, Literal[FIX_STATES]] = Field(default_factory=dict, max_length=MAX_FIXES)
    headings: dict[str, Literal[FIX_STATES]] = Field(default_factory=dict, max_length=MAX_FIXES)
    unknown: Literal[FIX_STATES] | None = Field(None, description="every use case still unknown becomes this")


def _named(ref: str):
    g = tables.goals
    number = goal_rules.number_of(ref)
    return g.c.number == number if number is not None else g.c.alias == ref


async def goal_of(conn: AsyncConnection, project_id: int, ref: str):
    """(id, code) of the project's goal ``ref`` and its SRS scope; 404 for no such goal, 409 for a goal that aims at no
    SRS document."""
    g = tables.goals
    row = (await conn.execute(select(g.c.id, g.c.number).where(g.c.project_id == project_id, _named(ref)))).first()
    if row is None:
        raise HTTPException(404, f"the project has no goal {ref}")
    aimed = await backlog.scope(conn, row.id)
    if aimed is None:
        raise HTTPException(409, f"goal G-{row.number} aims at no SRS document: it has no baseline")
    return row.id, f"G-{row.number}", aimed


async def confirmed(conn: AsyncConnection, goal_id: int) -> bool:
    """Whether the baseline of goal ``goal_id`` is confirmed for the SRS document it aims at now. Step 5's gate: the
    Loop makes no build for a goal for which this is False; a goal with no SRS document is never confirmed."""
    aimed = await backlog.scope(conn, goal_id)
    if aimed is None:
        return False
    b = tables.srs_baselines
    found = await conn.scalar(select(b.c.document_id).where(b.c.goal_id == goal_id))
    return found == aimed[1]


async def waiting(conn: AsyncConnection, project_id: int) -> list:
    """The active goals of the project that aim at an SRS document whose baseline is not confirmed for it, oldest first:
    rows of ``number``, ``slug`` (the document's), ``since`` (when the goal was made)."""
    g, r, d, b = tables.goals, tables.goal_revisions, tables.srs.documents, tables.srs_baselines
    query = (
        select(g.c.number, d.c.slug, g.c.created_at.label("since"))
        .join_from(g, r, (r.c.goal_id == g.c.id) & (r.c.revision == g.c.revision))
        .join(d, d.c.id == r.c.srs_document_id)
        .outerjoin(b, (b.c.goal_id == g.c.id) & (b.c.document_id == r.c.srs_document_id))
        .where(g.c.project_id == project_id, r.c.status == goal_rules.ACTIVE, b.c.goal_id.is_(None))
        .order_by(g.c.created_at, g.c.number)
    )
    return list((await conn.execute(query)).all())


async def refs(conn: AsyncConnection, goal_id: int) -> list[str]:
    """The codes of the approved use cases in the scope of goal ``goal_id``, which the graph is asked about."""
    project_id, document_id, heading = await backlog.scope(conn, goal_id)
    return [case.ref for case in under(await backlog.use_cases(conn, project_id, document_id), heading)]


async def _aimed_since(conn: AsyncConnection, goal_id: int, document_id: int) -> datetime | None:
    """When the goal first aimed at the document: the baseline's date."""
    r = tables.goal_revisions
    query = select(func.min(r.c.created_at)).where(r.c.goal_id == goal_id, r.c.srs_document_id == document_id)
    return await conn.scalar(query)


async def _approved_at(conn: AsyncConnection, document_id: int) -> dict[str, datetime]:
    """When ``srs.use_case.approved`` last told of each use case of the document, by ``ref_key``."""
    e = tables.hub_events
    query = (
        select(e.c.payload, e.c.created_at)
        .where(
            e.c.kind == SRS_USE_CASE_APPROVED,
            e.c.payload["document_id"].astext == str(document_id),
            cast(e.c.payload["version"].astext, Integer) > 0,
        )
        .order_by(e.c.id)
    )
    found: dict[str, datetime] = {}
    for payload, at in (await conn.execute(query)).all():
        ref = payload.get("code") or payload.get("key")
        if ref:
            found[ref_key(ref)] = at
    return found


def _item(case: UseCase, state: str, source: str, reason: str, evidence: list[dict]) -> BaselineItem:
    return BaselineItem(
        code=case.ref,
        title=case.title,
        heading=case.heading,
        state=state,
        source=source,
        reason=reason,
        evidence=[BaselineEvidence(**line) for line in evidence],
    )


async def _states(
    conn: AsyncConnection, goal_id: int, evidence: Mapping[str, list[Evidence]] | None
) -> tuple[list[UseCase], dict[str, Proposal], dict[str, delivery.Delivery], tuple[int, int, str | None]]:
    """(the use cases of the scope, the proposal for each by ref_key, the ledger, the scope)."""
    aimed = await backlog.scope(conn, goal_id)
    project_id, document_id, heading = aimed
    cases = under(await backlog.use_cases(conn, project_id, document_id), heading)
    since = await _aimed_since(conn, goal_id, document_id)
    told = await _approved_at(conn, document_id)
    proposals = {}
    for case in cases:
        key = ref_key(case.ref)
        if evidence is None:
            proposals[key] = Proposal(UNKNOWN, (), "the knowledge graph could not be read")
            continue
        newer = since is not None and key in told and told[key] > since
        proposals[key] = propose(evidence.get(case.ref, ()), newer)
    return cases, proposals, await delivery.read(conn, document_id), aimed


async def view(
    conn: AsyncConnection,
    project: str,
    goal_id: int,
    code: str,
    evidence: Mapping[str, list[Evidence]] | None,
    why: str | None = None,
) -> SrsBaseline:
    """The baseline of goal ``goal_id`` (``code``, G-<n>) as it stands, from ``evidence`` (see the module)."""
    cases, proposals, ledger, (_, document_id, heading) = await _states(conn, goal_id, evidence)
    items = []
    for case in cases:
        key = ref_key(case.ref)
        row = ledger.get(key)
        if row is not None:
            reason = row.evidence.get("reason") or (f"set by {row.evidence['by']}" if row.evidence.get("by") else "")
            items.append(_item(case, row.state, row.source, str(reason), row.evidence.get("evidence") or []))
        else:
            found = proposals[key]
            items.append(_item(case, found.state, "proposal", found.reason, [e.as_dict() for e in found.evidence]))
    b, d, u = tables.srs_baselines, tables.srs.documents, tables.users
    query = (
        select(b.c.document_id, b.c.confirmed_at, u.c.login)
        .join_from(b, u, u.c.id == b.c.confirmed_by, isouter=True)
        .where(b.c.goal_id == goal_id)
    )
    row = (await conn.execute(query)).first()
    held = row is not None and row.document_id == document_id
    slug = await conn.scalar(select(d.c.slug).where(d.c.id == document_id))
    return SrsBaseline(
        project=project,
        goal=code,
        document=slug,
        heading=heading,
        confirmed=held,
        confirmed_at=row.confirmed_at if held else None,
        confirmed_by=row.login if held else None,
        graph=evidence is not None,
        why=why,
        counts=counts(item.state for item in items),
        items=items,
    )


def _admin(user, access) -> None:
    if user.acts_for_run is not None or not has_role(access.role, "admin"):
        raise HTTPException(403, f"setting the baseline of a goal needs the admin role on project {access.name}")


async def fix(
    conn: AsyncConnection,
    user,
    access,
    goal_id: int,
    code: str,
    body: BaselineFix,
    evidence: Mapping[str, list[Evidence]] | None,
) -> int:
    """An admin sets use cases of the baseline (see the module); how many ledger rows it wrote. 422 for a code that is
    no use case of the goal's scope, 403 for anyone but an admin."""
    _admin(user, access)
    cases, proposals, ledger, (project_id, document_id, _) = await _states(conn, goal_id, evidence)
    now = {key: (ledger[key].state if key in ledger else proposals[key].state) for key in proposals}
    try:
        setting = fixes(now, cases, codes=body.codes, headings=body.headings, unknown=body.unknown)
    except KeyError as exc:
        raise HTTPException(422, f"goal {code} has no use case {exc.args[0]} in its scope") from None
    by_key = {ref_key(case.ref): case for case in cases}
    written = 0
    for key, state in setting.items():
        if now[key] == DELIVERING or (key in ledger and ledger[key].source == "deliver"):
            continue  # a use case the Loop has under way, or has delivered, is not the admin's to move here
        proof = {"by": user.login, "was": now[key]}
        written += await delivery.record(
            conn, project_id, document_id, by_key[key].ref, state, ADMIN, proof, goal_id=goal_id, user_id=user.user_id
        )
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=FIX,
        target=f"{access.name}/goal {code} use_cases={written}",
        project_id=access.project_id,
    )
    return written


async def confirm(
    conn: AsyncConnection,
    user,
    access,
    goal_id: int,
    code: str,
    evidence: Mapping[str, list[Evidence]] | None,
) -> dict[str, int]:
    """An admin confirms the baseline of goal ``goal_id``, once (409 the second time): each use case of the scope with
    no ledger row gets one of source ``kg`` in its proposed state, with the evidence; the count of each state."""
    _admin(user, access)
    cases, proposals, ledger, (project_id, document_id, _) = await _states(conn, goal_id, evidence)
    if await confirmed(conn, goal_id):
        raise HTTPException(409, f"the baseline of goal {code} is confirmed already")
    if evidence is None and any(ref_key(case.ref) not in ledger for case in cases):
        raise HTTPException(
            409, f"the knowledge graph could not be read, so the baseline of goal {code} cannot be proposed"
        )
    for case in cases:
        key = ref_key(case.ref)
        if key in ledger:
            continue
        found = proposals[key]
        proof = {"evidence": [e.as_dict() for e in found.evidence], "reason": found.reason}
        await delivery.record(conn, project_id, document_id, case.ref, found.state, KG, proof, goal_id=goal_id)
    after = await delivery.states(conn, document_id)
    held = counts(after[ref_key(c.ref)] for c in cases)
    b = tables.srs_baselines
    values = {"project_id": project_id, "document_id": document_id, "confirmed_by": user.user_id, "counts": held}
    query = insert(b).values(goal_id=goal_id, **values)
    await conn.execute(
        query.on_conflict_do_update(index_elements=[b.c.goal_id], set_={**values, "confirmed_at": func.now()})
    )
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=CONFIRM,
        target=f"{access.name}/goal {code}",
        project_id=access.project_id,
    )
    log.info("srs baseline confirmed", extra={"project": access.name, "goal": code, "login": user.login})
    return held


__all__ = [
    "BaselineFix",
    "BaselineItem",
    "SrsBaseline",
    "confirm",
    "confirmed",
    "fix",
    "goal_of",
    "refs",
    "view",
    "waiting",
]

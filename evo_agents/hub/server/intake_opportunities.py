"""The candidate opportunities of an ``evo-intake/2`` result (plan opportunity-intake, criterion a3): how each is checked
and what the hub proposes for it. ``evo_agents.hub.server.intake`` calls these in its put, in the same transaction, so a
refusal here writes nothing.

A candidate is a new opportunity or ``matches`` an opportunity of the project (``O-3``):

- a match adds lines of evidence only, proposed as evidence of that opportunity (a lines-only proposal of @builder);
- a new one says statement, beneficiary and leverage, each with its basis (a quote of a source, or inferred), and may
  say window_until, window_note and answers (the problems it answers; a problem of another project needs a grant of
  the writer who puts the result on it). It needs a line of demand (``intakes.DEMAND_SIGNALS``) or an answers code, and
  a line of leverage (``intakes.LEVERAGE_SIGNALS``), unless ``confirmed_by``, a member of the project, stands for it.
  A window_until needs a line of signal window.

Each becomes a pending proposal of @builder, kind ``opportunity``, with no chat; a new opportunity takes the next free
code, the codes of one put following each other. A writer approves them like any proposal.
"""

from __future__ import annotations

from datetime import date

from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import intakes, opportunities, tables
from evo_agents.hub.server import opportunities as routes
from evo_agents.hub.server import opportunity_evidence as lines_of
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal

OPTIONAL = ("window_until", "window_note", "answers")  # what a new opportunity may say besides its texts
MAX_SUMMARY_CHARS = 500  # of a proposal's summary (evo_agents.core_plugins.builder.MAX_SUMMARY_CHARS)


class Candidate:
    """One candidate opportunity, checked: how it joins the project's opportunities."""

    def __init__(self, at: int, raw: dict, lines: list[lines_of.OpportunityEvidenceWrite], held):
        self.at, self.raw, self.lines, self.held = at, raw, lines, held
        self.code: str | None = None  # the code of the opportunity, once given

    @property
    def new(self) -> bool:
        return self.held is None


def _sets(raw: dict) -> dict:
    """The fields a new opportunity's proposal sets."""
    out = {name: raw[name] for name in opportunities.TEXTS}
    out.update({name: raw[name] for name in OPTIONAL if raw.get(name)})
    return out


async def checked(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    document: dict,
    members: dict[str, int],
    reasons: list[str],
) -> list[Candidate]:
    """The ``opportunities`` of ``document`` checked; each thing wrong in any of them is added to ``reasons``.
    ``members`` maps the logins of the members of the project named in ``confirmed_by`` to their ids."""
    out: list[Candidate] = []
    for at, raw in enumerate(document.get("opportunities") or [], start=1):
        name = f"opportunity {at}"
        lines: list[lines_of.OpportunityEvidenceWrite] = []
        for place, item in enumerate(raw["evidence"], start=1):
            try:
                lines.append(lines_of.OpportunityEvidenceWrite.model_validate(item))
            except ValidationError as exc:
                reasons.extend(f"{name} evidence {place}: {e['msg']}" for e in exc.errors())
        ref = raw.get("matches")
        held = None
        if ref is not None:
            held = await routes.found(conn, access.project_id, ref)
            if held is None:
                reasons.append(f"{name}: project {access.name} has no opportunity {ref}")
                continue
            given = [f for f in (*opportunities.TEXTS, *OPTIONAL) if f in raw]
            if given:
                reasons.append(
                    f"{name}: it matches {ref}, so it adds lines of evidence only: leave out {', '.join(given)}"
                )
        else:
            reasons.extend(await _new(conn, user, access, raw, lines, members, name))
        if len(lines) != len(raw["evidence"]):
            continue
        reasons.extend(f"{name}: {why}" for why in await lines_of.wrong_lines(conn, held.id if held else None, lines))
        login = raw.get("confirmed_by")
        if login is not None and login not in members:
            reasons.append(f"{name}: confirmed_by {login} holds no grant on the project")
        out.append(Candidate(at, raw, lines, held))
    return out


async def _new(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    raw: dict,
    lines: list[lines_of.OpportunityEvidenceWrite],
    members: dict[str, int],
    name: str,
) -> list[str]:
    """What is wrong with ``raw`` as a new opportunity."""
    wrong: list[str] = []
    missing = [f for f in opportunities.TEXTS if not raw.get(f)]
    if missing:
        wrong.append(f"{name}: a new opportunity says {', '.join(missing)}")
    basis = raw.get("basis") or {}
    unbased = [f for f in opportunities.TEXTS if f not in basis]
    if unbased:
        wrong.append(f"{name}: say where {', '.join(unbased)} come from: a quote of a source, or inferred")
    given = [f for f in ("window_until", "window_note") if raw.get(f) and f not in basis]
    if given:
        wrong.append(f"{name}: say where {', '.join(given)} come from: a quote of a source, or inferred")
    if raw.get("window_until"):
        try:
            date.fromisoformat(raw["window_until"])
        except ValueError:
            wrong.append(f"{name}: window_until {raw['window_until']} is not a calendar date")
    if missing:
        return wrong
    signals = {line.signal for line in lines}
    confirmed = raw.get("confirmed_by") in members
    if not confirmed:
        if not (signals & set(intakes.DEMAND_SIGNALS)) and not raw.get("answers"):
            wrong.append(
                f"{name}: no demand: add a line of signal {', '.join(intakes.DEMAND_SIGNALS)}, or name the problems "
                "it answers (answers), or have a member of the project confirm it (confirmed_by)"
            )
        if not signals & set(intakes.LEVERAGE_SIGNALS):
            wrong.append(
                f"{name}: no leverage: add a line of signal {' or '.join(intakes.LEVERAGE_SIGNALS)}, or have a "
                "member of the project confirm it (confirmed_by)"
            )
    if raw.get("window_until") and "window" not in signals:
        wrong.append(f"{name}: window_until {raw['window_until']} needs a line of signal window")
    if wrong:
        return wrong
    try:
        body = routes.ProjectOpportunityWrite.model_validate(_sets(raw))
    except ValidationError as exc:
        return [f"{name} {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
    # the answers: the problems must exist, and a problem of another project needs the writer's grant on it
    return [f"{name}: {why}" for why in (await routes._values(conn, user, access, body, None))[1]]


def _summary(intake: str, candidate: Candidate) -> str:
    if candidate.new:
        inferred = [f for f in opportunities.TEXTS if candidate.raw["basis"][f] == "inferred"]
        tail = f" (inferred: {', '.join(inferred)})" if inferred else ""
        text = (
            f"Intake {intake}: a new opportunity{tail}, from {len(candidate.lines)} lines: {candidate.raw['statement']}"
        )
    else:
        text = f"Intake {intake}: {len(candidate.lines)} lines of evidence for {candidate.raw['matches']}"
    return " ".join(text.split())[:MAX_SUMMARY_CHARS]


async def propose(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, intake: str, candidates: list[Candidate]
) -> list[dict]:
    """A pending proposal of @builder for each candidate, with no chat; a new opportunity takes the next free code. Each
    item: candidate, kind (new_opportunity or evidence), opportunity, proposal_id, lines."""
    b = tables.builder_proposals
    code = await routes.next_code(conn, access.project_id)
    number = opportunities.number_of(code) or 1
    made: list[dict] = []
    for candidate in candidates:
        lines = [line.model_dump() for line in candidate.lines]
        if candidate.new:
            candidate.code = opportunities.code(number)
            number += 1
            sets, version, kind = _sets(candidate.raw), 0, "new_opportunity"
        else:
            candidate.code = opportunities.code(candidate.held.number)
            sets, version, kind = {}, candidate.held.revision, "evidence"
        values = {
            "project_id": access.project_id,
            "kind": "opportunity",
            "target": {"opportunity": candidate.code},
            "change": {"set": sets, "unset": [], "evidence": lines},
            "summary": _summary(intake, candidate),
            "base_version": version,
            "proposed_by": user.user_id,
        }
        proposal_id = (await conn.execute(insert(b).values(values).returning(b.c.id))).scalar_one()
        made.append(
            {
                "candidate": candidate.at,
                "kind": kind,
                "opportunity": candidate.code,
                "proposal_id": proposal_id,
                "lines": len(lines),
            }
        )
    return made

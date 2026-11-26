"""The intakes of a project: who may read and start them (``READERS``, ``WRITERS``: the scopes of the problems), what
an intake shows (``view``), and what a put checks and makes (``put``): a pending proposal of @builder for each
candidate and a task for each promise."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents import schema as json_schema
from evo_agents.harness import load_schema
from evo_agents.hub import intakes, problems, task_status
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import audit
from evo_agents.hub.server import problem_evidence as evidence
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.base.scope import Scope
from evo_agents.hub.server.intake import opportunity_candidates, repository
from evo_agents.hub.server.intake.models import (
    MAX_SUMMARY_CHARS,
    RACE,
    TASKS,
    Intake,
    IntakeOpportunityProposal,
    IntakeOutcome,
    IntakeProposal,
    IntakeStart,
    IntakeTask,
)
from evo_agents.hub.server.opportunities import service as project_opportunities
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.problems import service as project_problems
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_tasks import MAX_CAUSE_CHARS, MAX_TITLE_CHARS
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)

READERS = project_problems.READERS  # 404 without a grant (``project_access``), 403 for a hub admin without one
WRITERS = project_problems.WRITERS


# Reading


def view(project: str, row) -> Intake:
    result = row.result or {}
    done = row.status == intakes.DONE
    return Intake(
        project=project,
        code=intakes.code(row.number),
        number=row.number,
        status=row.status,
        requested_by=row.requested_login,
        sources=row.sources,
        note=row.note,
        created_at=row.created_at,
        put_by=row.put_login,
        done_at=row.done_at,
        candidates=len(result.get("candidates") or []) if done else None,
        promises=len(result.get("promises") or []) if done else None,
        questions=len(result.get("questions") or []) if done else None,
        opportunities=len(result.get("opportunities") or []) if done else None,
        outcome=row.outcome,
    )


async def one(conn: AsyncConnection, access: ProjectAccess, ref: str, *, lock: bool = False):
    """Intake ``ref`` of the project; 404 when there is none."""
    row = await repository.found(conn, access.project_id, ref, lock=lock)
    if row is None:
        raise NotFound(f"project {access.name} has no intake {ref}")
    return row


async def listed(scope: Scope, status: str | None = None) -> list[Intake]:
    """The project's intakes, newest first; those of ``status`` alone when given."""
    return await listed_in(scope.conn, scope.access, status)


async def listed_in(conn: AsyncConnection, access: ProjectAccess, status: str | None = None) -> list[Intake]:
    """``listed`` for a caller whose access is checked already (the MCP tool ``intake_list``)."""
    rows = await repository.listed(conn, access.project_id, status)
    return [view(access.name, row) for row in rows]


async def shown(scope: Scope, ref: str) -> Intake:
    """Intake ``ref`` of the project; 404 when there is none."""
    return await shown_in(scope.conn, scope.access, ref)


async def shown_in(conn: AsyncConnection, access: ProjectAccess, ref: str) -> Intake:
    """``shown`` for a caller whose access is checked already (the MCP tool ``intake_show``)."""
    return view(access.name, await one(conn, access, ref))


async def _audit(scope: Scope, action: str, number: int, what: str) -> None:
    await scope.auditor.record(action, f"{scope.access.name}/intake {intakes.code(number)} {what}")


async def start(scope: Scope, body: IntakeStart) -> Intake:
    """A new intake of the project, open, with the next code I-<n>."""
    conn, access = scope.conn, scope.access
    await project_problems.lock(conn, access.project_id)
    number = await repository.next_intake(conn, access.project_id)
    values = {
        "project_id": access.project_id,
        "number": number,
        "requested_by": scope.user.user_id,
        "sources": [s.model_dump(exclude_none=True) for s in body.sources],
        "note": body.note,
    }
    await repository.start(conn, values)
    await _audit(scope, audit.INTAKE_START, number, f"started sources={len(body.sources)}")
    return view(access.name, await one(conn, access, intakes.code(number)))


# The result


class Wrong(Exception):
    """What is wrong with a result, one reason each."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _limit_reasons(document: dict) -> list[str]:
    reasons = []
    for name, limit in (
        ("candidates", intakes.MAX_CANDIDATES),
        ("promises", intakes.MAX_PROMISES),
        ("questions", intakes.MAX_QUESTIONS),
        ("opportunities", intakes.MAX_OPPORTUNITIES),
    ):
        if len(document.get(name) or []) > limit:
            reasons.append(f"{name}: at most {limit}")
    for at, promise in enumerate(document.get("promises") or [], start=1):
        if promise.get("due"):
            try:
                date.fromisoformat(promise["due"])
            except ValueError:
                reasons.append(f"promise {at}: due {promise['due']} is not a calendar date")
    for at, candidate in enumerate(document.get("candidates") or [], start=1):
        if len(candidate["evidence"]) > problems.MAX_PROPOSED_EVIDENCE:
            reasons.append(f"candidate {at}: at most {problems.MAX_PROPOSED_EVIDENCE} lines of evidence")
    for at, candidate in enumerate(document.get("opportunities") or [], start=1):
        if len(candidate["evidence"]) > problems.MAX_PROPOSED_EVIDENCE:
            reasons.append(f"opportunity {at}: at most {problems.MAX_PROPOSED_EVIDENCE} lines of evidence")
    return reasons


def schema_reasons(document: Any) -> list[str]:
    """Each thing wrong with ``document`` as an evo-intake/1 or evo-intake/2 document, and with its limits."""
    named = document.get("schema") if isinstance(document, dict) else None
    name = intakes.SCHEMA_FILES.get(named, "intake")  # pyright: ignore[reportCallIssue, reportArgumentType]
    found = json_schema.errors(json_schema.validate(document, load_schema(name)))
    reasons = [f"{issue.path or 'result'}: {issue.message}" for issue in found]
    return reasons or _limit_reasons(document)


class Candidate:
    """One candidate, checked: how it joins the project's problems."""

    def __init__(self, at: int, raw: dict, lines: list[evidence.ProblemEvidenceWrite], held):
        self.at, self.raw, self.lines, self.held = at, raw, lines, held
        self.code: str | None = None  # a new problem's, once given

    @property
    def new(self) -> bool:
        return self.held is None


def _lines(candidate: dict, name: str, reasons: list[str]) -> list[evidence.ProblemEvidenceWrite]:
    lines: list[evidence.ProblemEvidenceWrite] = []
    for place, item in enumerate(candidate["evidence"], start=1):
        try:
            lines.append(evidence.ProblemEvidenceWrite.model_validate(item))
        except ValidationError as exc:
            reasons.extend(f"{name} evidence {place}: {e['msg']}" for e in exc.errors())
    return lines


def _new_reasons(candidate: dict, name: str) -> list[str]:
    """What is wrong with ``candidate`` as a new problem."""
    reasons = []
    missing = [f for f in problems.TEXTS if not candidate.get(f)]
    if missing:
        reasons.append(f"{name}: a new problem says {', '.join(missing)}")
    basis = candidate.get("basis") or {}
    unbased = [f for f in problems.TEXTS if f not in basis]
    if unbased:
        reasons.append(f"{name}: say where {', '.join(unbased)} come from: a quote of a source, or inferred")
    return reasons


async def _sources_reasons(
    conn: AsyncConnection, candidate: dict, held, lines: list, members: dict, name: str
) -> list[str]:
    """What is wrong with the lines of ``candidate`` and with how many independent sources stand for it."""
    reasons = [f"{name}: {why}" for why in await evidence.wrong_lines(conn, held.id if held else None, lines)]
    confirmed = await _confirmed(conn, candidate, held, members, reasons, name)
    known = await repository.evidence_sources(conn, held.id) if held is not None else []
    count = problems.sources_of([*known, *(line.model_dump() for line in lines)])
    if count < intakes.MIN_INDEPENDENT_SOURCES and not confirmed:
        reasons.append(
            f"{name}: {count} independent source and nobody confirmed it: add a line from a second source, or "
            "have a member of the project confirm it (confirmed_by)"
        )
    return reasons


async def candidates_of(conn: AsyncConnection, access: ProjectAccess, document: dict) -> list[Candidate]:
    """The candidates of ``document`` checked; ``Wrong`` naming each thing wrong in all of them."""
    reasons: list[str] = []
    out: list[Candidate] = []
    raw = document.get("candidates") or []
    members = await repository.members(
        conn, access.project_id, {c["confirmed_by"] for c in raw if c.get("confirmed_by")}
    )
    for at, candidate in enumerate(raw, start=1):
        name = f"candidate {at}"
        lines = _lines(candidate, name, reasons)
        ref = candidate.get("matches")
        held = None
        if ref is not None:
            held = await project_problems.found(conn, access.project_id, ref)
            if held is None:
                reasons.append(f"{name}: project {access.name} has no problem {ref}")
                continue
            given = [f for f in problems.TEXTS if f in candidate]
            if given:
                reasons.append(
                    f"{name}: it matches {ref}, so it adds lines of evidence only: leave out {', '.join(given)}"
                )
        else:
            reasons += _new_reasons(candidate, name)
        if len(lines) != len(candidate["evidence"]):
            continue
        reasons += await _sources_reasons(conn, candidate, held, lines, members, name)
        out.append(Candidate(at, candidate, lines, held))
    if reasons:
        raise Wrong(reasons)
    return out


async def _confirmed(
    conn: AsyncConnection, candidate: dict, held, members: dict, reasons: list[str], name: str
) -> bool:
    """Whether someone confirmed the candidate: a member named in ``confirmed_by``, or a confirmed line of the
    problem it matches."""
    login = candidate.get("confirmed_by")
    if login is not None:
        if login in members:
            return True
        reasons.append(f"{name}: confirmed_by {login} holds no grant on the project")
        return False
    if held is None:
        return False
    return await repository.confirmed_evidence(conn, held.id)


def _summary(intake: str, candidate: Candidate) -> str:
    if candidate.new:
        inferred = [f for f in problems.TEXTS if candidate.raw["basis"][f] == "inferred"]
        tail = f" (inferred: {', '.join(inferred)})" if inferred else ""
        text = f"Intake {intake}: a new problem{tail}, from {len(candidate.lines)} lines: {candidate.raw['statement']}"
    else:
        text = f"Intake {intake}: {len(candidate.lines)} lines of evidence for {candidate.raw['matches']}"
    return " ".join(text.split())[:MAX_SUMMARY_CHARS]


async def propose(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, intake: str, candidates: list[Candidate]
) -> list[IntakeProposal]:
    """A pending proposal of @builder for each candidate, with no chat; a new problem takes the next free code, the
    codes of one put in a row."""
    number = await repository.next_problem(conn, access.project_id)
    made: list[IntakeProposal] = []
    for candidate in candidates:
        lines = [line.model_dump() for line in candidate.lines]
        if candidate.new:
            candidate.code = problems.code(number)
            number += 1
            sets = {name: candidate.raw[name] for name in problems.TEXTS}
            version, kind = 0, "new_problem"
        else:
            candidate.code = problems.code(candidate.held.number)
            sets, version, kind = {}, candidate.held.revision, "evidence"
        values = {
            "project_id": access.project_id,
            "kind": "problem",
            "target": {"problem": candidate.code},
            "change": {"set": sets, "unset": [], "evidence": lines},
            "summary": _summary(intake, candidate),
            "base_version": version,
            "proposed_by": user.user_id,
        }
        proposal_id = await repository.propose(conn, values)
        made.append(
            IntakeProposal(
                candidate=candidate.at, kind=kind, problem=candidate.code, proposal_id=proposal_id, lines=len(lines)
            )
        )
    return made


# The promises


class TasksConfig:
    """What a promise reads of the config of the plugin tasks in the project."""

    def __init__(self, prefix: str, first: str, groups: dict[str, str | None], kind: str):
        self.prefix, self.first, self.groups, self.type = prefix, first, groups, kind

    def ref(self, number: int) -> str:
        return f"{self.prefix}-{number}"


async def tasks_config(conn: AsyncConnection, project_id: int) -> tuple[Any, TasksConfig | None]:
    """(the plugin's row, its config) while the project enables the plugin tasks; (None, None) otherwise, and a row
    with None when the config names no status or no type."""
    plugin = await enabled_in(conn, TASKS, project_id)
    if plugin is None:
        return None, None
    stacks = await stored_stacks(conn, [plugin.id], project_id, None)
    merged = plugin_config.merge(plugin.manifest.get("config"), stacks.get(plugin.id, []))
    values: dict[str, Any] = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = [s for s in values.get("statuses") or [] if isinstance(s, dict) and s.get("key")]
    types = [t for t in values.get("types") or [] if isinstance(t, str)]
    if not statuses or not types:
        return plugin, None
    kind = "task" if "task" in types else types[0]
    groups = {s["key"]: s.get("group") for s in statuses}
    return plugin, TasksConfig(values.get("prefix") or "T", statuses[0]["key"], groups, kind)


def _description(intake: str, requester: str, promise: dict, member: bool) -> str:
    who = promise.get("promiser")
    parts = [
        f"Promised by {who} (not a member of the project; {requester} asked for the intake)"
        if who and not member
        else f"Promised by {who}"
        if who
        else "Promised by someone the sources do not name",
    ]
    parts[0] += f", read in intake {intake}" + (f" from {promise['source']}" if promise.get("source") else "") + "."
    if promise.get("due"):
        parts.append(f"Due {promise['due']}, as the sources give or imply it.")
    if promise.get("delivery_seen"):
        note = f": {promise['delivery_note']}" if promise.get("delivery_note") else ""
        parts.append(f"Delivery evidence was seen{note}. Check it before you close the task.")
    else:
        parts.append("No delivery evidence was seen.")
    return "\n\n".join(parts)


async def make_tasks(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    intake: str,
    config: TasksConfig,
    promises: list[dict],
) -> list[IntakeTask]:
    """A task for each promise: assigned to the promiser when they hold a grant on the project, else to the member
    who put the result, which is the requester's session; due when the session inferred a date."""
    held = await repository.members(conn, access.project_id, {x["promiser"] for x in promises if x.get("promiser")})
    label = access.push_label()
    cause = f"promise read in intake {intake}"[:MAX_CAUSE_CHARS]
    made: list[IntakeTask] = []
    for at, promise in enumerate(promises, start=1):
        number = await repository.next_task(conn, access.project_id)
        login: str = promise.get("promiser") or ""
        member = login in held
        assignee_id = held[login] if member else user.user_id
        values = {
            "project_id": access.project_id,
            "number": number,
            "type": config.type,
            "title": " ".join(promise["text"].split())[:MAX_TITLE_CHARS],
            "description_md": _description(intake, user.login, promise, member),
            "status": config.first,
            "due_on": date.fromisoformat(promise["due"]) if promise.get("due") else None,
            "label": label,
            "external_key": f"intake:{access.project_id}:{intake}:promise:{at}",
            "external_ref": {"source": "intake", "intake": intake, "promise": at},
            "created_by": user.user_id,
        }
        task_id = await repository.add_task(conn, values)
        await repository.task_history(conn, task_id, user.user_id, "created", config.ref(number), cause)
        await repository.assign(conn, task_id, assignee_id)
        assignee = login if member else user.login
        await repository.task_history(conn, task_id, user.user_id, "assignee", assignee, cause)
        change = task_status.StatusChange(
            task_id,
            None,
            config.first,
            None,
            config.groups.get(config.first),
            task_status.CREATE,
            config.prefix,
            user.user_id,
        )
        await task_status.emit(conn, access.project_id, change)
        made.append(
            IntakeTask(
                promise=at,
                task=config.ref(number),
                assignee=assignee,
                due_on=values["due_on"],
                delivery_seen=bool(promise.get("delivery_seen")),
            )
        )
    return made


async def _promised(scope: Scope, ref: str, promises: list[dict]) -> tuple[list[IntakeTask], str | None]:
    """The tasks the promises of a result made, and why they made none when the plugin tasks cannot take them."""
    if not promises:
        return [], None
    plugin, config = await tasks_config(scope.conn, scope.access.project_id)
    if config is None:
        why = "is not enabled in" if plugin is None else "has no status or no type in"
        return [], f"plugin {TASKS} {why} project {scope.access.name}: the {len(promises)} promises stay in the intake"
    return await make_tasks(scope.conn, scope.user, scope.access, ref, config, promises), None


# The put


async def _checked(scope: Scope, body: dict) -> tuple[list[Candidate], list]:
    """The candidates and candidate opportunities of ``body``, checked; ``Wrong`` naming each thing wrong."""
    conn, access = scope.conn, scope.access
    reasons = schema_reasons(body)
    if reasons:
        raise Wrong(reasons)
    await project_opportunities.lock(conn, access.project_id)
    candidates: list[Candidate] = []
    try:
        candidates = await candidates_of(conn, access, body)
    except Wrong as exc:
        reasons += exc.reasons
    logins = {c["confirmed_by"] for c in body.get("opportunities") or [] if c.get("confirmed_by")}
    members = await repository.members(conn, access.project_id, logins)
    chosen = await opportunity_candidates.checked(conn, scope.user, access, body, members, reasons)
    if reasons:
        raise Wrong(reasons)
    return candidates, chosen


async def _put(scope: Scope, ref: str, body: dict) -> Intake:
    conn, user, access = scope.conn, scope.user, scope.access
    await project_problems.lock(conn, access.project_id)
    row = await one(conn, access, ref, lock=True)
    if row.status == intakes.DONE:
        raise Conflict(f"intake {ref} of project {access.name} was put already; nothing changed")
    candidates, chosen = await _checked(scope, body)
    made = await propose(conn, user, access, ref, candidates)
    made_opportunities = await opportunity_candidates.propose(conn, user, access, ref, chosen)
    promises = body.get("promises") or []
    made_tasks, skipped = await _promised(scope, ref, promises)
    outcome = IntakeOutcome(
        proposals=made,
        opportunity_proposals=[IntakeOpportunityProposal(**m) for m in made_opportunities],
        tasks=made_tasks,
        tasks_skipped=skipped,
        questions=body.get("questions") or [],
    )
    stamp = {"status": intakes.DONE, "result": body, "outcome": outcome.model_dump(mode="json"), "put_by": user.user_id}
    await repository.done(conn, row.id, stamp)
    what = f"put candidates={len(candidates)} promises={len(promises)} questions={len(outcome.questions)}"
    what += f" opportunities={len(chosen)}" if chosen else ""
    proposed = [str(m.proposal_id) for m in made] + [str(m["proposal_id"]) for m in made_opportunities]
    what += f" proposals={','.join(proposed) or '-'} tasks={len(made_tasks)}"
    await _audit(scope, audit.INTAKE_PUT, row.number, what)
    return view(access.name, await one(conn, access, ref))


async def put(scope: Scope, ref: str, body: dict) -> Intake:
    """Put the result of intake ``ref``, once (409 when it is done): 422 naming each thing wrong with it, else a
    proposal for each candidate and a task for each promise."""
    try:
        return await _put(scope, ref, body)
    except Wrong as exc:
        raise Unprocessable(f"{'; '.join(exc.reasons)}; nothing changed") from None
    except IntegrityError:
        raise Conflict(RACE) from None

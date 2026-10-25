"""The intakes of a project (plan problem-intake, criterion a2; the model in ``evo_agents.hub.intakes``). Table
intakes, schema 0138.

An intake is one reading of vague context (a chat, a mail thread, a folder) by the session of a member: the member
starts it with the kinds and labels of the sources the session will read, never their content, and puts what the
session made of them once, as an ``evo-intake/1`` document (``evo_agents/harness/schemas/intake.schema.json``).

- POST /v1/projects/{project}/intakes: a new intake I-<n>, open, for a writer of the project. GET .../intakes (``?status=``)
  and GET .../intakes/{ref}: for a member with a grant on the project.
- PUT .../intakes/{ref}: the result, once (409 when the intake is done). The document is checked against the schema;
  every refusal is a 422 that names each reason and writes nothing:
  - a candidate with one independent source (a source is a kind and an address) that nobody confirmed. A candidate is
    confirmed by ``confirmed_by``, a member of the project, or by a confirmed line of the problem it matches; for a
    candidate that matches a problem, the problem's lines count with its own;
  - a line labelled personal, an excerpt over 300 characters, a line the problem holds already, a P-<n> the project
    lacks, a date that does not exist.
- Each candidate becomes a pending proposal of @builder, kind ``problem``, without a chat: a new problem takes the next
  free code (the codes of one put follow each other), a candidate that matches a problem proposes its lines as
  evidence of it. A writer approves them as any proposal of @builder; the agent of a run never puts a result.
- ``evo-intake/2`` adds ``opportunities``: candidate opportunities, checked and proposed by
  ``evo_agents.hub.server.intake_opportunities`` (a pending proposal of @builder, kind ``opportunity``, for each); a
  document of ``evo-intake/1`` is accepted as before.
- Each promise becomes a task of the plugin tasks when the project enabled it: due on the date the session inferred,
  assigned to the promiser when they hold a grant on the project and to the member who asked for the intake otherwise,
  its description saying who promised, from which source and whether the session saw evidence that it was delivered.
  Without the plugin the promises stay in the intake, and the answer says so (``tasks_skipped``).

Writes of one project's intakes, problems and proposals wait for each other (the lock of ``problems.write``). The hub
layer cannot import a core plugin, so the proposal rows and the tasks are made here through the tables, as the plugin
srs does for its frame tasks.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents import schema as json_schema
from evo_agents.harness import load_schema
from evo_agents.hub import intakes, problems, tables, task_status
from evo_agents.hub.plugin_calls import enabled_in
from evo_agents.hub.server import audit, intake_opportunities
from evo_agents.hub.server import opportunities as project_opportunities
from evo_agents.hub.server import problem_evidence as evidence
from evo_agents.hub.server import problems as project_problems
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.plugin_configs import stored_stacks
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.tables_tasks import MAX_CAUSE_CHARS, MAX_TITLE_CHARS
from evo_agents.plugins import config as plugin_config

log = logging.getLogger(__name__)

REFUSALS = {code: {"model": ErrorBody} for code in (403, 404)}
WRITE_REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
IntakeRef = Annotated[str, Path(pattern=intakes.REF, description="I-<number>")]
Said = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TASKS = "tasks"  # the plugin whose tasks a promise becomes (evo_agents.hub.server.tasks)
MAX_SUMMARY_CHARS = 500  # of a proposal's summary (evo_agents.core_plugins.builder.MAX_SUMMARY_CHARS)
CLOSED_GROUPS = ("done", "cancelled")
RACE = "another write of the project's tasks or problems took the number first; put the result again"

router = APIRouter(prefix="/v1/projects", tags=["intake"], responses={401: {"model": ErrorBody}})


class IntakeSource(BaseModel):
    """A source the session reads: what it is, never what it says."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description=f"one of {', '.join(intakes.SOURCE_KINDS)}")
    label: str = Field(description=f"one of {', '.join(intakes.SOURCE_LABELS)}")
    name: Said | None = Field(
        None, max_length=intakes.MAX_SOURCE_NAME_CHARS, description="a short name, such as a group's; never content"
    )

    @field_validator("kind")
    @classmethod
    def _kind(cls, kind: str) -> str:
        if kind not in intakes.SOURCE_KINDS:
            raise ValueError(f"kind is one of {', '.join(intakes.SOURCE_KINDS)}")
        return kind

    @field_validator("label")
    @classmethod
    def _label(cls, label: str) -> str:
        if label not in intakes.SOURCE_LABELS:
            raise ValueError(f"label is one of {', '.join(intakes.SOURCE_LABELS)}")
        return label


class IntakeStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sources: list[IntakeSource] = Field(min_length=1, max_length=intakes.MAX_SOURCES)
    note: Said | None = Field(None, max_length=intakes.MAX_NOTE_CHARS, description="why the member asked for it")


class IntakeProposal(BaseModel):
    candidate: int = Field(description="the candidate's place in the result, from 1")
    kind: Literal["new_problem", "evidence"]
    problem: str = Field(description="P-<number>: the code a new problem would take, or the problem the lines join")
    proposal_id: int = Field(description="the pending proposal of @builder a writer approves")
    lines: int


class IntakeOpportunityProposal(BaseModel):
    candidate: int = Field(description="the candidate opportunity's place in the result, from 1")
    kind: Literal["new_opportunity", "evidence"]
    opportunity: str = Field(description="O-<number>: the code a new opportunity would take, or the one the lines join")
    proposal_id: int = Field(description="the pending proposal of @builder a writer approves")
    lines: int


class IntakeTask(BaseModel):
    promise: int = Field(description="the promise's place in the result, from 1")
    task: str = Field(description="the task's code, such as T-12")
    assignee: str
    due_on: date | None
    delivery_seen: bool


class IntakeOutcome(BaseModel):
    """What the hub made of a result."""

    proposals: list[IntakeProposal]
    tasks: list[IntakeTask]
    opportunity_proposals: list[IntakeOpportunityProposal] = Field(
        default_factory=list, description="the proposals made for the candidate opportunities of an evo-intake/2 result"
    )
    tasks_skipped: str | None = Field(
        description="why the promises made no task (the plugin tasks is not enabled in the project); null otherwise"
    )
    questions: list[dict] = Field(description="the questions the session could not answer, as it put them")


class Intake(BaseModel):
    """An intake of a project."""

    project: str
    code: str = Field(description="I-<number>, unique in the project")
    number: int
    status: Literal[intakes.STATUSES]
    requested_by: str
    sources: list[IntakeSource]
    note: str | None
    created_at: datetime
    put_by: str | None = Field(description="who put the result; null while it is open")
    done_at: datetime | None
    candidates: int | None = Field(description="how many candidates the result held; null while it is open")
    promises: int | None
    questions: int | None
    opportunities: int | None = Field(
        None, description="how many candidate opportunities the result held (evo-intake/2); null while it is open"
    )
    outcome: IntakeOutcome | None = Field(description="what the hub made of the result; null while it is open")


def _select():
    i, u = tables.intakes, tables.users
    by, put = u.alias("by"), u.alias("put")
    return (
        select(i, by.c.login.label("requested_login"), put.c.login.label("put_login"))
        .join(by, by.c.id == i.c.requested_by)
        .outerjoin(put, put.c.id == i.c.put_by)
    )


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


async def _one(conn: AsyncConnection, access: ProjectAccess, ref: str, *, lock: bool = False):
    query = _select().where(tables.intakes.c.project_id == access.project_id)
    query = query.where(tables.intakes.c.number == intakes.number_of(ref))
    if lock:
        query = query.with_for_update(of=tables.intakes)
    row = (await conn.execute(query)).first()
    if row is None:
        raise HTTPException(404, f"project {access.name} has no intake {ref}")
    return row


async def listed(conn: AsyncConnection, access: ProjectAccess, status: str | None = None) -> list[Intake]:
    """The project's intakes, newest first; those of ``status`` alone when given."""
    query = _select().where(tables.intakes.c.project_id == access.project_id)
    if status is not None:
        query = query.where(tables.intakes.c.status == status)
    rows = (await conn.execute(query.order_by(tables.intakes.c.number.desc()))).all()
    return [view(access.name, row) for row in rows]


async def shown(conn: AsyncConnection, access: ProjectAccess, ref: str) -> Intake:
    """Intake ``ref`` of the project; 404 when there is none."""
    return view(access.name, await _one(conn, access, ref))


# The result


class _Wrong(Exception):
    """What is wrong with a result, one reason each."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _schema_reasons(document: Any) -> list[str]:
    name = intakes.SCHEMA_FILES.get(document.get("schema") if isinstance(document, dict) else None, "intake")
    found = json_schema.errors(json_schema.validate(document, load_schema(name)))
    reasons = [f"{issue.path or 'result'}: {issue.message}" for issue in found]
    if reasons:
        return reasons
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


async def _members(conn: AsyncConnection, access: ProjectAccess, logins: set[str]) -> dict[str, int]:
    """login -> user id of each of ``logins`` who holds a grant on the project."""
    if not logins:
        return {}
    u, g = tables.users, tables.grants
    query = (
        select(u.c.login, u.c.id)
        .join_from(u, g, g.c.user_id == u.c.id)
        .where(g.c.project_id == access.project_id, u.c.login.in_(sorted(logins)))
    )
    return {row.login: row.id for row in await conn.execute(query)}


class _Candidate:
    """One candidate, checked: how it joins the project's problems."""

    def __init__(self, at: int, raw: dict, lines: list[evidence.ProblemEvidenceWrite], held):
        self.at, self.raw, self.lines, self.held = at, raw, lines, held
        self.code: str | None = None  # a new problem's, once given

    @property
    def new(self) -> bool:
        return self.held is None


async def _candidates(conn: AsyncConnection, access: ProjectAccess, document: dict) -> list[_Candidate]:
    """The candidates of ``document`` checked; ``_Wrong`` naming each thing wrong in all of them."""
    reasons: list[str] = []
    out: list[_Candidate] = []
    raw = document.get("candidates") or []
    members = await _members(conn, access, {c["confirmed_by"] for c in raw if c.get("confirmed_by")})
    for at, candidate in enumerate(raw, start=1):
        name = f"candidate {at}"
        lines: list[evidence.ProblemEvidenceWrite] = []
        for place, item in enumerate(candidate["evidence"], start=1):
            try:
                lines.append(evidence.ProblemEvidenceWrite.model_validate(item))
            except ValidationError as exc:
                reasons.extend(f"{name} evidence {place}: {e['msg']}" for e in exc.errors())
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
            missing = [f for f in problems.TEXTS if not candidate.get(f)]
            if missing:
                reasons.append(f"{name}: a new problem says {', '.join(missing)}")
            basis = candidate.get("basis") or {}
            unbased = [f for f in problems.TEXTS if f not in basis]
            if unbased:
                reasons.append(f"{name}: say where {', '.join(unbased)} come from: a quote of a source, or inferred")
        if len(lines) != len(candidate["evidence"]):
            continue
        reasons.extend(f"{name}: {why}" for why in await evidence.wrong_lines(conn, held.id if held else None, lines))
        confirmed = await _confirmed(candidate, held, members, conn, reasons, name)
        known = []
        if held is not None:
            e = tables.problem_evidence
            known = [
                row._mapping
                for row in await conn.execute(select(e.c.source_kind, e.c.source_uri).where(e.c.problem_id == held.id))
            ]
        count = problems.sources_of([*known, *(line.model_dump() for line in lines)])
        if count < intakes.MIN_INDEPENDENT_SOURCES and not confirmed:
            reasons.append(
                f"{name}: {count} independent source and nobody confirmed it: add a line from a second source, or "
                "have a member of the project confirm it (confirmed_by)"
            )
        out.append(_Candidate(at, candidate, lines, held))
    if reasons:
        raise _Wrong(reasons)
    return out


async def _confirmed(
    candidate: dict, held, members: dict, conn: AsyncConnection, reasons: list[str], name: str
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
    e = tables.problem_evidence
    query = select(func.count()).where(e.c.problem_id == held.id, e.c.confirmed_by.is_not(None))
    return bool(await conn.scalar(query))


def _summary(intake: str, candidate: _Candidate) -> str:
    if candidate.new:
        inferred = [f for f in problems.TEXTS if candidate.raw["basis"][f] == "inferred"]
        tail = f" (inferred: {', '.join(inferred)})" if inferred else ""
        text = f"Intake {intake}: a new problem{tail}, from {len(candidate.lines)} lines: {candidate.raw['statement']}"
    else:
        text = f"Intake {intake}: {len(candidate.lines)} lines of evidence for {candidate.raw['matches']}"
    return " ".join(text.split())[:MAX_SUMMARY_CHARS]


async def _propose(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, intake: str, candidates: list[_Candidate]
) -> list[IntakeProposal]:
    """A pending proposal of @builder for each candidate, with no chat; a new problem takes the next free code, the
    codes of one put in a row."""
    b = tables.builder_proposals
    number = (
        int(
            await conn.scalar(
                select(func.coalesce(func.max(tables.problems.c.number), 0)).where(
                    tables.problems.c.project_id == access.project_id
                )
            )
        )
        + 1
    )
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
        proposal_id = (await conn.execute(insert(b).values(values).returning(b.c.id))).scalar_one()
        made.append(
            IntakeProposal(
                candidate=candidate.at, kind=kind, problem=candidate.code, proposal_id=proposal_id, lines=len(lines)
            )
        )
    return made


# The promises


class _TasksConfig:
    """What a promise reads of the config of the plugin tasks in the project."""

    def __init__(self, prefix: str, first: str, groups: dict[str, str | None], kind: str):
        self.prefix, self.first, self.groups, self.type = prefix, first, groups, kind

    def ref(self, number: int) -> str:
        return f"{self.prefix}-{number}"


async def _tasks_config(conn: AsyncConnection, project_id: int) -> tuple[Any, _TasksConfig | None]:
    """(the plugin's row, its config) while the project enables the plugin tasks; (None, None) otherwise, and a row
    with None when the config names no status or no type."""
    plugin = await enabled_in(conn, TASKS, project_id)
    if plugin is None:
        return None, None
    stacks = await stored_stacks(conn, [plugin.id], project_id, None)
    merged = plugin_config.merge(plugin.manifest.get("config"), stacks.get(plugin.id, []))
    values = {name: item.value for name, item in merged.fields.items() if not item.secret}
    statuses = [s for s in values.get("statuses") or [] if isinstance(s, dict) and s.get("key")]
    types = [t for t in values.get("types") or [] if isinstance(t, str)]
    if not statuses or not types:
        return plugin, None
    kind = "task" if "task" in types else types[0]
    groups = {s["key"]: s.get("group") for s in statuses}
    return plugin, _TasksConfig(values.get("prefix") or "T", statuses[0]["key"], groups, kind)


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


async def _history(conn: AsyncConnection, task_id: int, actor: int, name: str, new: str, cause: str) -> None:
    values = {"task_id": task_id, "actor_id": actor, "field": name, "from_value": None, "to_value": new, "cause": cause}
    await conn.execute(insert(tables.tasks.events).values(**values))


async def _make_tasks(
    conn: AsyncConnection,
    user: Principal,
    access: ProjectAccess,
    intake: str,
    config: _TasksConfig,
    promises: list[dict],
) -> list[IntakeTask]:
    """A task for each promise: assigned to the promiser when they hold a grant on the project, else to the member
    who put the result, which is the requester's session; due when the session inferred a date."""
    t, p = tables.tasks.items, tables.tasks.people
    held = await _members(conn, access, {x["promiser"] for x in promises if x.get("promiser")})
    label = access.push_label()
    cause = f"promise read in intake {intake}"[:MAX_CAUSE_CHARS]
    made: list[IntakeTask] = []
    for at, promise in enumerate(promises, start=1):
        number = (
            int(
                await conn.scalar(
                    select(func.coalesce(func.max(t.c.number), 0)).where(t.c.project_id == access.project_id)
                )
            )
            + 1
        )
        login, member = promise.get("promiser"), promise.get("promiser") in held
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
        task_id = await conn.scalar(insert(t).values(**values).returning(t.c.id))
        await _history(conn, task_id, user.user_id, "created", config.ref(number), cause)
        await conn.execute(insert(p).values(task_id=task_id, role="assignee", user_id=assignee_id))
        assignee = login if member else user.login
        await _history(conn, task_id, user.user_id, "assignee", assignee, cause)
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


# Routes


async def _audit(conn: AsyncConnection, user: Principal, access: ProjectAccess, action: str, number: int, what: str):
    target = f"{access.name}/intake {intakes.code(number)} {what}"
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=access.project_id
    )


@router.get("/{project}/intakes", response_model=list[Intake], responses=REFUSALS)
async def list_intakes(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    status: Annotated[Literal[intakes.STATUSES] | None, Query(description="keep the intakes of one status")] = None,
) -> list[Intake]:
    """The project's intakes, newest first, for a member with a grant on the project."""
    async with request.app.state.engine.begin() as conn:
        access = await project_problems.reader(conn, user, project)
        return await listed(conn, access, status)


@router.get("/{project}/intakes/{ref}", response_model=Intake, responses=REFUSALS)
async def show_intake(request: Request, project: ProjectName, ref: IntakeRef, user: CurrentUser) -> Intake:
    """One intake: its requester, sources, status and, once done, what the hub made of its result."""
    async with request.app.state.engine.begin() as conn:
        access = await project_problems.reader(conn, user, project)
        return await shown(conn, access, ref)


@router.post("/{project}/intakes", response_model=Intake, status_code=201, responses=WRITE_REFUSALS)
async def start_intake(request: Request, project: ProjectName, body: IntakeStart, user: CurrentUser) -> Intake:
    """Start an intake: a writer of the project asks their session to read sources; it gets the next code I-<n>."""
    i = tables.intakes
    async with request.app.state.engine.begin() as conn:
        access = await project_problems._writer(conn, user, project)
        await project_problems._lock(conn, access.project_id)
        number = (
            int(
                await conn.scalar(
                    select(func.coalesce(func.max(i.c.number), 0)).where(i.c.project_id == access.project_id)
                )
            )
            + 1
        )
        values = {
            "project_id": access.project_id,
            "number": number,
            "requested_by": user.user_id,
            "sources": [s.model_dump(exclude_none=True) for s in body.sources],
            "note": body.note,
        }
        await conn.execute(insert(i).values(values))
        await _audit(conn, user, access, audit.INTAKE_START, number, f"started sources={len(body.sources)}")
        row = await _one(conn, access, intakes.code(number))
    return view(access.name, row)


@router.put("/{project}/intakes/{ref}", response_model=Intake, responses=WRITE_REFUSALS)
async def put_intake(request: Request, project: ProjectName, ref: IntakeRef, body: dict, user: CurrentUser) -> Intake:
    """Put the result of an intake, once, as an evo-intake/1 or evo-intake/2 document (see the module)."""
    i = tables.intakes
    try:
        async with request.app.state.engine.begin() as conn:
            access = await project_problems._writer(conn, user, project)
            await project_problems._lock(conn, access.project_id)
            row = await _one(conn, access, ref, lock=True)
            if row.status == intakes.DONE:
                raise HTTPException(409, f"intake {ref} of project {access.name} was put already; nothing changed")
            reasons = _schema_reasons(body)
            if reasons:
                raise _Wrong(reasons)
            await project_opportunities._lock(conn, access.project_id)
            reasons, candidates, chosen = [], [], []
            try:
                candidates = await _candidates(conn, access, body)
            except _Wrong as exc:
                reasons += exc.reasons
            logins = {c["confirmed_by"] for c in body.get("opportunities") or [] if c.get("confirmed_by")}
            members = await _members(conn, access, logins)
            chosen = await intake_opportunities.checked(conn, user, access, body, members, reasons)
            if reasons:
                raise _Wrong(reasons)
            made = await _propose(conn, user, access, ref, candidates)
            made_opportunities = await intake_opportunities.propose(conn, user, access, ref, chosen)
            promises = body.get("promises") or []
            skipped = None
            made_tasks: list[IntakeTask] = []
            if promises:
                plugin, config = await _tasks_config(conn, access.project_id)
                if config is None:
                    why = "is not enabled in" if plugin is None else "has no status or no type in"
                    skipped = (
                        f"plugin {TASKS} {why} project {access.name}: the {len(promises)} promises stay in the intake"
                    )
                else:
                    made_tasks = await _make_tasks(conn, user, access, ref, config, promises)
            outcome = IntakeOutcome(
                proposals=made,
                opportunity_proposals=[IntakeOpportunityProposal(**m) for m in made_opportunities],
                tasks=made_tasks,
                tasks_skipped=skipped,
                questions=body.get("questions") or [],
            )
            stamp = {
                "status": intakes.DONE,
                "result": body,
                "outcome": outcome.model_dump(mode="json"),
                "put_by": user.user_id,
                "done_at": func.now(),
            }
            await conn.execute(update(i).where(i.c.id == row.id).values(stamp))
            what = f"put candidates={len(candidates)} promises={len(promises)} questions={len(outcome.questions)}"
            what += f" opportunities={len(chosen)}" if chosen else ""
            proposed = [str(m.proposal_id) for m in made] + [str(m["proposal_id"]) for m in made_opportunities]
            what += f" proposals={','.join(proposed) or '-'} tasks={len(made_tasks)}"
            await _audit(conn, user, access, audit.INTAKE_PUT, row.number, what)
            done = await _one(conn, access, ref)
    except _Wrong as exc:
        raise HTTPException(422, f"{'; '.join(exc.reasons)}; nothing changed") from None
    except IntegrityError:
        raise HTTPException(409, RACE) from None
    return view(access.name, done)

"""Workers: the machines members register to run plan steps for them, how a machine joins, and how it is stopped.

A machine joins in one of two ways. A member creates a pairing (POST /v1/workers/pairings, with a web session and its
CSRF header or with a machine token) naming the worker, its projects, slots, labels and whether it allows the web
terminal; every project must be one the member holds the writer role on. The answer carries a code of CODE_LENGTH
characters of Crockford base32, written XXXX-XXXX, shown once: the hub keeps the SHA-256 of the code and its first
SELECTOR_LENGTH characters in clear, the selector, which no two unused pairings share. A code lasts PAIRING_TTL, and a
member has at most MAX_LIVE_PAIRINGS codes that are neither used, expired nor locked. The web follows a pairing with
GET /v1/workers/pairings/{id}. On the machine, the daemon sends the code and its host facts to POST /v1/worker/join,
the one public worker route: the hub finds the pairing by the selector and compares the hash of the whole code, so a
wrong rest counts against that pairing, and MAX_WRONG_TRIES wrong tries lock it for good. Unknown, wrong, expired,
used and locked codes all get the same 403, which names none of them. A code that matches makes the worker and its
``evw_`` token, which the answer carries once; the pairing is used from then on. A machine signed in with
``evo-agents hub login`` registers directly instead, with POST /v1/workers and its machine token.

A worker belongs to the member who registered it. GET /v1/workers lists the caller's workers, every worker for a hub
admin; GET /v1/workers/{id} shows one; drain stops new claims, undrain resumes them, and revoke ends the worker and its
token at once. Each of them answers 404 for another member's worker, so an id tells nothing about workers one does not
own; a hub admin may see, drain, undrain and revoke any worker. Revoking releases the runs the worker holds as the
reaper would (``release_runs``): a run whose cancel was asked for is cancelled, a run pinned to this worker or on its
last attempt fails, and any other becomes lost with a new attempt queued for the same step.

Every pairing, join, registration, drain, undrain and revocation adds an audit row naming the pairing or worker, never
the code or the token, and neither ever reaches a log line.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from evo_agents.hub import runs
from evo_agents.hub.access import ROLES, has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import MACHINE, WORKER, CurrentUser, Principal, issue_token, same

log = logging.getLogger(__name__)

MAX_ID = 2**63 - 1  # bigint
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U
_READ_AS = str.maketrans({"O": "0", "I": "1", "L": "1"})  # what Crockford base32 reads the look-alikes as
CODE_LENGTH = 8
SELECTOR_LENGTH = 4  # the start of a code, kept in clear so a wrong try finds the pairing it is against
PAIRING_TTL = timedelta(minutes=10)
PAIRING_KEPT = timedelta(days=1)  # an unused pairing is deleted once its expiry is this far behind
MAX_LIVE_PAIRINGS = 5  # per member: neither used, expired nor locked
MAX_WRONG_TRIES = 5
DRAWS = 20  # codes drawn before giving up on a selector no unused pairing holds
WRITER_ROLES = [role for role in ROLES if has_role(role, "writer")]
REFUSED_CODE = (
    "the pairing code is wrong, expired, used already or locked after 5 wrong tries: create a new one on the web"
)
NO_WORKER = "you have no worker {id} on this hub: see GET /v1/workers"
WORKER_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"  # as the workers table accepts it
LABEL = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
LINE = r"^[^\x00-\x1f\x7f]+$"  # one line, without control characters

router = APIRouter(prefix="/v1/workers", tags=["workers"], responses={401: {"model": ErrorBody}})
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"])

WorkerId = Annotated[int, Path(ge=1, le=MAX_ID)]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}, 409: {"model": ErrorBody}}


# Codes


def new_code() -> str:
    """A pairing code in its stored form: CODE_LENGTH characters of Crockford base32, without the hyphen."""
    return "".join(secrets.choice(CROCKFORD) for _ in range(CODE_LENGTH))


def normal_code(text: str) -> str | None:
    """The code ``text`` names, read as Crockford base32 does: any case, hyphens and spaces left out, O read as 0 and
    I and L as 1. None when that is not CODE_LENGTH characters of the alphabet."""
    code = "".join(text.split()).replace("-", "").upper().translate(_READ_AS)
    if len(code) != CODE_LENGTH or any(char not in CROCKFORD for char in code):
        return None
    return code


def shown_code(code: str) -> str:
    return f"{code[:SELECTOR_LENGTH]}-{code[SELECTOR_LENGTH:]}"


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


# Models


class WorkerScope(BaseModel):
    name: str = Field(pattern=WORKER_NAME, description="unique among the owner's workers that are not revoked")
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1, max_length=100, description="projects the worker takes runs of; the writer role on each"
    )
    slots: int = Field(1, ge=1, le=8, description="runs the worker holds at once")
    labels: list[Annotated[str, Field(pattern=LABEL)]] = Field(default_factory=list, max_length=16)
    allow_web_terminal: bool = Field(False, description="whether the owner may open a terminal on it from the web")


class HostFacts(BaseModel):
    hostname: str = Field(min_length=1, max_length=255, pattern=LINE)
    os: str = Field(min_length=1, max_length=40, pattern=LINE)
    arch: str = Field(min_length=1, max_length=40, pattern=LINE)
    agent_version: str = Field(min_length=1, max_length=100, pattern=LINE, description="of the evo-agents daemon")


class PairingRequest(WorkerScope):
    pass


class WorkerRegistration(WorkerScope, HostFacts):
    pass


class JoinRequest(HostFacts):
    code: str = Field(min_length=1, max_length=32, description="the pairing code, XXXX-XXXX")


class Pairing(BaseModel):
    id: int
    code: str = Field(description="XXXX-XXXX; shown once, the hub keeps only its hash and its first four characters")
    expires_at: datetime
    name: str
    projects: list[str]
    slots: int
    labels: list[str]
    allow_web_terminal: bool


class PairingState(BaseModel):
    id: int
    status: Literal["waiting", "joined", "expired", "locked"]
    name: str
    projects: list[str]
    slots: int
    labels: list[str]
    allow_web_terminal: bool
    tries_left: int = Field(description="wrong codes the pairing takes before it locks")
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None
    worker_id: int | None = Field(description="the worker the pairing made, once a machine joined with it")


class Worker(BaseModel):
    id: int
    name: str
    owner: str = Field(description="the login of the member who registered it")
    status: Literal[runs.WORKER_STATUSES]
    hostname: str
    os: str
    arch: str
    agent_version: str
    slots: int
    labels: list[str]
    projects: list[str]
    runtimes: dict = Field(description="the runtimes its last heartbeat reported")
    checkouts: dict = Field(description="the checkouts its last heartbeat reported")
    allow_web_terminal: bool
    held_runs: int = Field(description="runs it holds now (leased, running, interactive or verifying)")
    created_at: datetime
    last_heartbeat_at: datetime | None
    drained_at: datetime | None
    revoked_at: datetime | None


class WorkerCredential(BaseModel):
    token: str = Field(description="the worker token, evw_...; shown once, the hub keeps only its SHA-256")
    token_id: int
    expires_at: datetime = Field(description="moves forward with every use")
    worker: Worker


# Reading workers

WORKERS = """
SELECT w.id, w.name, u.login, w.hostname, w.os, w.arch, w.agent_version, w.slots, w.labels,
       ARRAY(SELECT p.name FROM worker_projects wp JOIN projects p ON p.id = wp.project_id
              WHERE wp.worker_id = w.id ORDER BY p.name),
       w.runtimes, w.checkouts, w.allow_web_terminal,
       (SELECT count(*) FROM runs r WHERE r.worker_id = w.id AND r.state = ANY(%(held)s)),
       w.created_at, w.last_heartbeat_at, w.drained_at, w.revoked_at, now()
  FROM workers w JOIN users u ON u.id = w.owner_id
 WHERE {where}
 ORDER BY w.created_at DESC, w.id DESC
"""
WORKER_FIELDS = (
    "id",
    "name",
    "owner",
    "hostname",
    "os",
    "arch",
    "agent_version",
    "slots",
    "labels",
    "projects",
    "runtimes",
    "checkouts",
    "allow_web_terminal",
    "held_runs",
    "created_at",
    "last_heartbeat_at",
    "drained_at",
    "revoked_at",
)


def _worker(row) -> Worker:
    *values, now = row
    fields = dict(zip(WORKER_FIELDS, values, strict=True))
    status = runs.worker_status(
        last_heartbeat_at=fields["last_heartbeat_at"],
        drained_at=fields["drained_at"],
        revoked_at=fields["revoked_at"],
        now=now,
    )
    return Worker(**fields, status=status)


async def _workers(conn, where: str, params: dict) -> list[Worker]:
    cursor = await conn.execute(WORKERS.format(where=where), {"held": list(runs.HELD_STATES), **params})
    return [_worker(row) for row in await cursor.fetchall()]


async def _one_worker(conn, worker_id: int) -> Worker:
    (found,) = await _workers(conn, "w.id = %(id)s", {"id": worker_id})
    return found


async def _owned(conn, user: Principal, worker_id: int, *, lock: bool = False):
    """(owner_id, owner login, name, token_id, drained_at, revoked_at) of a worker ``user`` owns, or any worker for a
    hub admin; 404 otherwise, the same as for an id the hub never gave out."""
    statement = (
        "SELECT w.owner_id, u.login, w.name, w.token_id, w.drained_at, w.revoked_at "
        "FROM workers w JOIN users u ON u.id = w.owner_id WHERE w.id = %s"
    )
    row = await (await conn.execute(statement + (" FOR UPDATE OF w" if lock else ""), (worker_id,))).fetchone()
    if row is None or (row[0] != user.user_id and not user.admin):
        raise HTTPException(404, NO_WORKER.format(id=worker_id))
    return row


# Creating workers


async def _writable(conn, user: Principal, names: list[str]) -> list[int]:
    """The ids of the projects ``names``; 404 for one ``user`` cannot see, 403 for one without the writer role."""
    ids = []
    for name in names:
        access = await project_access(conn, user, name)
        if not has_role(access.role, "writer"):
            role = access.role or "no role"
            raise HTTPException(403, f"a worker of project {name} needs the writer role on it; you hold {role}")
        ids.append(access.project_id)
    return ids


async def _name_taken(conn, owner_id: int, name: str) -> bool:
    statement = "SELECT 1 FROM workers WHERE owner_id = %s AND lower(name) = lower(%s) AND revoked_at IS NULL"
    return await (await conn.execute(statement, (owner_id, name))).fetchone() is not None


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(409, f"there is a worker named {name} already: revoke it first, or choose another name")


INSERT_WORKER = """
INSERT INTO workers (owner_id, token_id, name, hostname, os, arch, agent_version, slots, labels, allow_web_terminal)
VALUES (%(owner)s, %(token)s, %(name)s, %(hostname)s, %(os)s, %(arch)s, %(agent_version)s, %(slots)s,
        %(labels)s::text[], %(allow_web_terminal)s)
RETURNING id
"""


async def _create_worker(
    conn,
    owner_id: int,
    *,
    name: str,
    slots: int,
    labels: list[str],
    allow_web_terminal: bool,
    project_ids,
    host: HostFacts,
):
    """A new worker of ``owner_id`` and its token, in the caller's transaction; (Issued, worker id)."""
    if await _name_taken(conn, owner_id, name):
        raise _name_conflict(name)
    issued = await issue_token(conn, owner_id, WORKER, host.hostname)
    params = {
        "owner": owner_id,
        "token": issued.token_id,
        "name": name,
        "slots": slots,
        "labels": labels,
        "allow_web_terminal": allow_web_terminal,
        **host.model_dump(include={"hostname", "os", "arch", "agent_version"}),
    }
    try:
        worker_id = (await (await conn.execute(INSERT_WORKER, params)).fetchone())[0]
    except psycopg.errors.UniqueViolation:  # another registration took the name meanwhile
        raise _name_conflict(name) from None
    await conn.execute(
        "INSERT INTO worker_projects (worker_id, project_id) SELECT %s, unnest(%s::bigint[])",
        (worker_id, list(project_ids)),
    )
    return issued, worker_id


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


async def _credential(conn, issued, worker_id: int) -> WorkerCredential:
    worker = await _one_worker(conn, worker_id)
    return WorkerCredential(token=issued.token, token_id=issued.token_id, expires_at=issued.expires_at, worker=worker)


# Pairings

PRUNE_PAIRINGS = "DELETE FROM worker_pairings WHERE used_at IS NULL AND expires_at < now() - %s"
LIVE_PAIRINGS = (
    "SELECT count(*) FROM worker_pairings "
    "WHERE owner_id = %s AND used_at IS NULL AND expires_at > now() AND attempts < %s"
)
INSERT_PAIRING = """
INSERT INTO worker_pairings (code_selector, code_hash, owner_id, name, projects, slots, labels, allow_web_terminal,
                             expires_at)
VALUES (%(selector)s, %(hash)s, %(owner)s, %(name)s, %(projects)s::bigint[], %(slots)s, %(labels)s::text[],
        %(allow_web_terminal)s, now() + %(ttl)s)
ON CONFLICT (code_selector) WHERE used_at IS NULL DO NOTHING
RETURNING id, expires_at
"""


@router.post("/pairings", status_code=201, response_model=Pairing, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def create_pairing(request: Request, body: PairingRequest, user: CurrentUser, response: Response) -> Pairing:
    """A code a machine joins with as a worker of the caller; shown once."""
    projects, labels = _unique(body.projects), _unique(body.labels)
    async with request.app.state.pool.connection() as conn:
        project_ids = await _writable(conn, user, projects)
        # One pairing of a member at a time, so two of them cannot both pass the count.
        await conn.execute("SELECT 1 FROM users WHERE id = %s FOR UPDATE", (user.user_id,))
        await conn.execute(PRUNE_PAIRINGS, (PAIRING_KEPT,))
        live = (await (await conn.execute(LIVE_PAIRINGS, (user.user_id, MAX_WRONG_TRIES))).fetchone())[0]
        if live >= MAX_LIVE_PAIRINGS:
            raise HTTPException(
                409,
                f"you have {MAX_LIVE_PAIRINGS} pairing codes waiting already: join with one of them, or wait until "
                f"one expires ({int(PAIRING_TTL.total_seconds() // 60)} minutes after it was made)",
            )
        if await _name_taken(conn, user.user_id, body.name):
            raise _name_conflict(body.name)
        params = {
            "owner": user.user_id,
            "name": body.name,
            "projects": project_ids,
            "slots": body.slots,
            "labels": labels,
            "allow_web_terminal": body.allow_web_terminal,
            "ttl": PAIRING_TTL,
        }
        for _ in range(DRAWS):
            code = new_code()
            params |= {"selector": code[:SELECTOR_LENGTH], "hash": code_hash(code)}
            row = await (await conn.execute(INSERT_PAIRING, params)).fetchone()
            if row is not None:
                break
        else:
            raise HTTPException(503, "no free pairing code was found; try again shortly")
        pairing_id, expires_at = row
        target = f"pairing:{pairing_id} name={body.name}"
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.WORKER_PAIR, target=target)
    log.info("worker pairing created", extra={"pairing_id": pairing_id, "login": user.login, "worker_name": body.name})
    response.headers.update(NO_STORE)
    return Pairing(
        id=pairing_id,
        code=shown_code(code),
        expires_at=expires_at,
        name=body.name,
        projects=projects,
        slots=body.slots,
        labels=labels,
        allow_web_terminal=body.allow_web_terminal,
    )


async def _project_names(conn, project_ids: list[int]) -> list[str]:
    cursor = await conn.execute(
        "SELECT name FROM projects WHERE id = ANY(%s::bigint[]) ORDER BY array_position(%s::bigint[], id)",
        (project_ids, project_ids),
    )
    return [row[0] for row in await cursor.fetchall()]


@router.get("/pairings/{pairing_id}", response_model=PairingState, responses={404: {"model": ErrorBody}})
async def pairing_state(request: Request, pairing_id: WorkerId, user: CurrentUser, response: Response) -> PairingState:
    """Whether a machine has joined with one of the caller's pairings yet."""
    async with request.app.state.pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT owner_id, name, projects, slots, labels, allow_web_terminal, attempts, created_at, expires_at, "
                "used_at, worker_id, expires_at <= now() FROM worker_pairings WHERE id = %s",
                (pairing_id,),
            )
        ).fetchone()
        if row is None or row[0] != user.user_id:
            raise HTTPException(404, f"you have no pairing {pairing_id} on this hub")
        _, name, project_ids, slots, labels, terminal, attempts, created_at, expires_at, used_at, worker_id = row[:11]
        expired = row[11]
        projects = await _project_names(conn, project_ids)
    if used_at is not None:
        status = "joined"
    elif attempts >= MAX_WRONG_TRIES:
        status = "locked"
    elif expired:
        status = "expired"
    else:
        status = "waiting"
    response.headers.update(NO_STORE)
    return PairingState(
        id=pairing_id,
        status=status,
        name=name,
        projects=projects,
        slots=slots,
        labels=labels,
        allow_web_terminal=terminal,
        tries_left=max(MAX_WRONG_TRIES - attempts, 0),
        created_at=created_at,
        expires_at=expires_at,
        used_at=used_at,
        worker_id=worker_id,
    )


FIND_PAIRING = """
SELECT id, code_hash, owner_id, name, projects, slots, labels, allow_web_terminal, attempts, expires_at > now()
  FROM worker_pairings
 WHERE code_selector = %s AND used_at IS NULL
   FOR UPDATE
"""
OWNER_WRITES = "SELECT count(*) FROM grants WHERE user_id = %s AND project_id = ANY(%s::bigint[]) AND role = ANY(%s)"


@worker_router.post(
    "/join",
    status_code=201,
    response_model=WorkerCredential,
    responses={403: {"model": ErrorBody}, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
async def join(request: Request, body: JoinRequest, response: Response) -> WorkerCredential:
    """Trade a pairing code for the token of a new worker; the token is shown once."""
    code = normal_code(body.code)
    if code is None:
        raise HTTPException(422, f"a pairing code is {CODE_LENGTH} characters of Crockford base32, written XXXX-XXXX")
    refused = False
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(FIND_PAIRING, (code[:SELECTOR_LENGTH],))).fetchone()
        if row is None or row[8] >= MAX_WRONG_TRIES or not row[9]:
            refused = True
        elif not same(code_hash(code), row[1]):
            # Counted in this transaction, which commits: a refusal must not roll the wrong try back.
            await conn.execute("UPDATE worker_pairings SET attempts = attempts + 1 WHERE id = %s", (row[0],))
            refused = True
            log.warning("wrong pairing code", extra={"pairing_id": row[0], "tries_left": MAX_WRONG_TRIES - row[8] - 1})
        else:
            credential = await _join(conn, row, body)
    if refused:
        raise HTTPException(403, REFUSED_CODE)
    response.headers.update(NO_STORE)
    return credential


async def _join(conn, pairing, host: JoinRequest) -> WorkerCredential:
    pairing_id, _, owner_id, name, project_ids, slots, labels, terminal, _, _ = pairing
    writes = (await (await conn.execute(OWNER_WRITES, (owner_id, project_ids, WRITER_ROLES))).fetchone())[0]
    if writes != len(set(project_ids)):
        raise HTTPException(
            409,
            "the member who made this pairing no longer holds the writer role on every project it names: "
            "they need to create a new pairing",
        )
    issued, worker_id = await _create_worker(
        conn,
        owner_id,
        name=name,
        slots=slots,
        labels=list(labels),
        allow_web_terminal=terminal,
        project_ids=project_ids,
        host=host,
    )
    await conn.execute(
        "UPDATE worker_pairings SET used_at = now(), worker_id = %s WHERE id = %s", (worker_id, pairing_id)
    )
    target = f"worker:{worker_id} name={name} pairing:{pairing_id}"
    await audit.record(conn, actor_id=owner_id, token_id=issued.token_id, action=audit.WORKER_JOIN, target=target)
    log.info("worker joined", extra={"worker_id": worker_id, "pairing_id": pairing_id, "token_id": issued.token_id})
    return await _credential(conn, issued, worker_id)


# Registering with a machine token


@router.post("", status_code=201, response_model=WorkerCredential, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def register(
    request: Request, body: WorkerRegistration, user: CurrentUser, response: Response
) -> WorkerCredential:
    """Register the calling machine as a worker of the caller, with its machine token; the worker token is shown
    once."""
    if user.kind != MACHINE:
        raise HTTPException(
            403,
            "registering a worker directly takes the machine token of that machine (`evo-agents hub login` on it); "
            "on the web, create a pairing code instead",
        )
    projects = _unique(body.projects)
    async with request.app.state.pool.connection() as conn:
        project_ids = await _writable(conn, user, projects)
        issued, worker_id = await _create_worker(
            conn,
            user.user_id,
            name=body.name,
            slots=body.slots,
            labels=_unique(body.labels),
            allow_web_terminal=body.allow_web_terminal,
            project_ids=project_ids,
            host=body,
        )
        target = f"worker:{worker_id} name={body.name}"
        action = audit.WORKER_REGISTER
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        credential = await _credential(conn, issued, worker_id)
    log.info("worker registered", extra={"worker_id": worker_id, "login": user.login, "token_id": issued.token_id})
    response.headers.update(NO_STORE)
    return credential


# Listing and changing workers


@router.get("", response_model=list[Worker])
async def list_workers(
    request: Request,
    user: CurrentUser,
    revoked: Annotated[bool, Query(description="also list revoked workers")] = False,
) -> list[Worker]:
    """The caller's workers, newest first; every worker for a hub admin."""
    where = ["true"]
    if not user.admin:
        where.append("w.owner_id = %(owner)s")
    if not revoked:
        where.append("w.revoked_at IS NULL")
    async with request.app.state.pool.connection() as conn:
        return await _workers(conn, " AND ".join(where), {"owner": user.user_id})


@router.get("/{worker_id}", response_model=Worker, responses={404: {"model": ErrorBody}})
async def show_worker(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    async with request.app.state.pool.connection() as conn:
        await _owned(conn, user, worker_id)
        return await _one_worker(conn, worker_id)


def _target(worker_id: int, name: str, owner: str) -> str:
    return f"worker:{worker_id} name={name} owner={owner}"


async def _set_drain(request: Request, user: Principal, worker_id: int, drain: bool) -> Worker:
    async with request.app.state.pool.connection() as conn:
        _, owner, name, _, drained_at, revoked_at = await _owned(conn, user, worker_id, lock=True)
        if revoked_at is not None:
            raise HTTPException(409, f"worker {worker_id} was revoked at {revoked_at.isoformat()}; it takes no runs")
        if (drained_at is not None) != drain:  # drained already, or not drained: nothing changes, nothing is audited
            statement = "UPDATE workers SET drained_at = CASE WHEN %s THEN now() END WHERE id = %s"
            await conn.execute(statement, (drain, worker_id))
            action = audit.WORKER_DRAIN if drain else audit.WORKER_UNDRAIN
            target = _target(worker_id, name, owner)
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
            log.info("worker drained" if drain else "worker undrained", extra={"worker_id": worker_id})
        return await _one_worker(conn, worker_id)


@router.post("/{worker_id}/drain", response_model=Worker, responses=REFUSALS)
async def drain(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Stop new claims; the worker finishes the runs it holds."""
    return await _set_drain(request, user, worker_id, True)


@router.post("/{worker_id}/undrain", response_model=Worker, responses=REFUSALS)
async def undrain(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """Let a drained worker claim runs again."""
    return await _set_drain(request, user, worker_id, False)


@router.post("/{worker_id}/revoke", response_model=Worker, responses=REFUSALS)
async def revoke(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """End the worker and its token at once, and release the runs it holds."""
    async with request.app.state.pool.connection() as conn:
        _, owner, name, token_id, _, revoked_at = await _owned(conn, user, worker_id, lock=True)
        if revoked_at is not None:
            raise HTTPException(409, f"worker {worker_id} was revoked already, at {revoked_at.isoformat()}")
        await conn.execute("UPDATE workers SET revoked_at = now() WHERE id = %s", (worker_id,))
        await conn.execute("UPDATE tokens SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL", (token_id,))
        released = await release_runs(conn, worker_id, f"its worker {name} was revoked")
        target, action = _target(worker_id, name, owner), audit.WORKER_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        worker = await _one_worker(conn, worker_id)
    log.info("worker revoked", extra={"worker_id": worker_id, "by": user.login, "runs_released": released})
    return worker


# The runs of a worker that goes away

HELD_RUNS = """
SELECT id, state, attempt, max_attempts, pinned_worker_id, cancel_requested_at IS NOT NULL
  FROM runs
 WHERE worker_id = %s AND state = ANY(%s)
 ORDER BY id
   FOR UPDATE
"""
PINNED_QUEUED = "SELECT id, state FROM runs WHERE pinned_worker_id = %s AND state = 'queued' ORDER BY id FOR UPDATE"
MOVE = """
UPDATE runs SET state = %(to)s, error = coalesce(%(error)s, error), event_seq = event_seq + 1,
       finished_at = CASE WHEN %(to)s = ANY(%(terminal)s) THEN now() ELSE finished_at END
 WHERE id = %(id)s
RETURNING event_seq
"""
NEXT_ATTEMPT = """
INSERT INTO runs (project_id, plan_id, step_key, plan_revision, dispatched_by, pinned_worker_id, runtime, mode,
                  approval, timeout_s, attempt, max_attempts, parent_run_id, repo, branch)
SELECT project_id, plan_id, step_key, plan_revision, dispatched_by, pinned_worker_id, runtime, mode, approval,
       timeout_s, attempt + 1, max_attempts, id, repo, branch
  FROM runs WHERE id = %s
RETURNING id
"""
RUNS_CHANNEL = "evo_runs"  # notified when a run is queued, so a waiting claim looks again


async def move_run(conn, run_id: int, old: str, new: str, actor: str, *, reason: str, error: str | None = None) -> None:
    """Move run ``run_id`` from ``old`` to ``new`` as ``actor``, with the ``state`` event the hub writes for each
    move. Raises ``runs.TransitionRefused`` for a move the table refuses."""
    runs.check_transition(old, new, actor)
    params = {"id": run_id, "to": new, "error": error, "terminal": list(runs.TERMINAL_STATES)}
    seq = (await (await conn.execute(MOVE, params)).fetchone())[0]
    body = {"from": old, "to": new, "actor": actor, "reason": reason}
    await conn.execute(
        "INSERT INTO run_events (run_id, seq, kind, body) VALUES (%s, %s, 'state', %s)", (run_id, seq, Jsonb(body))
    )


async def release_runs(conn, worker_id: int, reason: str) -> int:
    """What the reaper does to the runs of a worker that will never extend a lease again, in the caller's
    transaction: each run it holds is cancelled when its cancel was asked for, fails when it is pinned to this worker
    or on its last attempt, and is otherwise lost, with the next attempt queued for the same step. Queued runs pinned
    to the worker fail, since no other worker may claim them. Returns how many runs were moved."""
    moved = 0
    for run_id, state, attempt, max_attempts, pinned, cancel in await (
        await conn.execute(HELD_RUNS, (worker_id, list(runs.HELD_STATES)))
    ).fetchall():
        if cancel:
            await move_run(conn, run_id, state, "cancelled", "reaper", reason=reason)
        elif pinned == worker_id or attempt >= max_attempts:
            why = "the run was pinned to it" if pinned == worker_id else f"it was attempt {attempt} of {max_attempts}"
            await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=f"{reason}, and {why}")
        else:
            await move_run(conn, run_id, state, "lost", "reaper", reason=reason, error=reason)
            next_id = (await (await conn.execute(NEXT_ATTEMPT, (run_id,))).fetchone())[0]
            await conn.execute("SELECT pg_notify(%s, %s)", (RUNS_CHANNEL, str(next_id)))
        moved += 1
    for run_id, state in await (await conn.execute(PINNED_QUEUED, (worker_id,))).fetchall():
        error = f"{reason}, and the run was pinned to it"
        await move_run(conn, run_id, state, "failed", "reaper", reason=reason, error=error)
        moved += 1
    return moved

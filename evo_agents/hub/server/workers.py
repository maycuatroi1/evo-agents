"""Workers: the machines members register to run plan steps for them, how a machine joins, and how it is stopped.

A machine joins in one of two ways. A member creates a pairing (POST /v1/workers/pairings, with a web session and its
CSRF header or with a machine token) naming the worker, its projects, slots, labels and whether it allows the web
terminal; every project must be one the member holds the writer role on. The answer carries a code of CODE_LENGTH
characters of Crockford base32, written XXXX-XXXX, shown once: the hub keeps an HMAC-SHA256 of the code under the
session secret (EVO_HUB_SESSION_SECRET; without it pairing and joining answer 503) and its first SELECTOR_LENGTH
characters in clear, the selector, which no two unused pairings share. A code lasts PAIRING_TTL, and a member has at
most MAX_LIVE_PAIRINGS codes that are neither used nor expired; a locked code counts until it expires, so locking codes
on purpose cannot take more selectors. The web follows a pairing with GET /v1/workers/pairings/{id}. On the machine,
the daemon sends the code and its host facts to POST /v1/worker/join, the one public worker route: the hub finds the
pairing by the selector and compares the HMAC of the whole code, so a wrong rest counts against that pairing, and
MAX_WRONG_TRIES wrong tries lock it for good. Unknown, wrong, expired, used and locked codes all get the same 403,
which names none of them, and an address with JOIN_REFUSALS of them within JOIN_WINDOW gets 429 with Retry-After
until the oldest leaves the window (``RefusalLimit``, in this process's memory; behind a reverse proxy the address is
the one it forwards only for a proxy listed in EVO_HUB_FORWARDED_ALLOW_IPS). A code that matches makes the worker and
its ``evw_`` token, which the answer carries once; the pairing is used from then on. A machine signed in with
``evo-agents hub login`` registers directly instead, with POST /v1/workers and its machine token.

A worker belongs to the member who registered it. GET /v1/workers lists the caller's workers, every worker for a hub
admin; GET /v1/workers/{id} shows one; drain stops new claims, undrain resumes them, and revoke ends the worker and its
token at once. Each of them answers 404 for another member's worker, so an id tells nothing about workers one does not
own; a hub admin may see, drain and revoke any worker, but only its owner undrains one (403 for an admin). Revoking
releases the runs the worker holds as the reaper would (``run_state.release_runs``): a run whose cancel was asked
for is cancelled, a run pinned to this worker or on its last attempt fails, and any other becomes lost with a new
attempt queued for the same step; the steps of the runs that ended go back to pending in their plans. Every lease
of the worker's runs is given back with them, and their GitHub tokens are revoked once the revocation commits
(``credentials``). Revoking a worker's token (DELETE /v1/tokens/{id}, DELETE /v1/admin/tokens/{id}) revokes its worker
the same way, in the same transaction (``lock_worker_of_token``, then ``end_worker``).

POST /v1/workers/{id}/dispatch-from {"value": "any" | "web"} sets who may hand the worker its runs (``dispatch_from``,
``evo_agents.hub.credentials.DISPATCH_FROM``): with ``web`` it claims only runs dispatched from a web session, and a
dispatch pinned to it with a token gets 403 (``runs``). Only its owner sets it, signed in on the web: a machine token
gets 403 whoever holds it, so a token that leaked cannot open the worker again, a hub admin who does not own the
worker gets 403, and anyone else 404. Setting the value it has changes nothing and is not audited.

Every pairing, join, registration, drain, undrain, change of dispatch_from and revocation adds an audit row naming the
pairing or worker, never the code or the token, and neither ever reaches a log line.
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Text, any_, delete, exists, func, insert, literal, select, update
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables
from evo_agents.hub.access import ROLES, has_role
from evo_agents.hub.credentials import DISPATCH_FROM
from evo_agents.hub.server import audit, credentials
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.paging import PAGED, WHOLE, Paging
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.run_state import release_runs
from evo_agents.hub.server.security import (
    MACHINE,
    WEB,
    WORKER,
    CurrentUser,
    Principal,
    issue_token,
    keyed_digest,
    same,
)

log = logging.getLogger(__name__)

MAX_ID = 2**63 - 1  # bigint
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O or U
_READ_AS = str.maketrans({"O": "0", "I": "1", "L": "1"})  # what Crockford base32 reads the look-alikes as
CODE_LENGTH = 8
SELECTOR_LENGTH = 4  # the start of a code, kept in clear so a wrong try finds the pairing it is against
PAIRING_TTL = timedelta(minutes=10)
PAIRING_KEPT = timedelta(days=1)  # an unused pairing is deleted once its expiry is this far behind
MAX_LIVE_PAIRINGS = 5  # per member: neither used nor expired, locked ones included
MAX_WRONG_TRIES = 5
PAIRING_PURPOSE = "pairing-code"  # keeps the HMAC of a code apart from every other HMAC of the session secret
JOIN_REFUSALS = 10  # refused joins from one address within JOIN_WINDOW, before the next join gets 429
JOIN_WINDOW = 600.0  # seconds
MAX_JOIN_CLIENTS = 10_000  # addresses RefusalLimit tracks at once; past that it forgets the least recently refused
DRAWS = 20  # codes drawn before giving up on a selector no unused pairing holds
WRITER_ROLES = [role for role in ROLES if has_role(role, "writer")]
REFUSED_CODE = (
    "the pairing code is wrong, expired, used already or locked after 5 wrong tries: create a new one on the web"
)
NO_WORKER = "you have no worker {id} on this hub: see GET /v1/workers"
NO_SECRET = "pairing codes need EVO_HUB_SESSION_SECRET, which this hub does not have: ask its operator to set it"
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


def code_hash(secret: str, code: str) -> str:
    """How the hub keeps a code: its HMAC under the session secret. A plain hash next to the selector in clear would
    let whoever reads worker_pairings try the 32**4 rests of the code offline."""
    return keyed_digest(secret, PAIRING_PURPOSE, code)


def _pairing_secret(request: Request) -> str:
    secret = request.app.state.config.session_secret
    if not secret:
        raise HTTPException(503, NO_SECRET)
    return secret


# Refused joins


class RefusalLimit:
    """Refusals per client address in a sliding window, in this process's memory: an address refused ``limit``
    times within ``window`` seconds waits until the oldest of those refusals leaves the window. At most
    ``max_clients`` addresses are tracked; past that the one refused least recently is forgotten."""

    def __init__(
        self,
        limit: int = JOIN_REFUSALS,
        window: float = JOIN_WINDOW,
        max_clients: int = MAX_JOIN_CLIENTS,
        clock=time.monotonic,
    ):
        self.limit, self.window, self.max_clients, self.clock = limit, window, max_clients, clock
        self._refused: dict[str, deque[float]] = {}  # oldest refused first

    def _recent(self, client: str, now: float) -> deque[float] | None:
        times = self._refused.get(client)
        while times and times[0] <= now - self.window:
            times.popleft()
        if times is not None and not times:
            del self._refused[client]
            return None
        return times

    def retry_after(self, client: str) -> int | None:
        """Whole seconds until ``client`` may try again; None while it is under the limit."""
        now = self.clock()
        times = self._recent(client, now)
        if times is None or len(times) < self.limit:
            return None
        return max(1, math.ceil(times[0] + self.window - now))

    def refuse(self, client: str) -> None:
        """Count one refusal of ``client``."""
        now = self.clock()
        times = self._recent(client, now) or deque(maxlen=self.limit)
        self._refused.pop(client, None)
        times.append(now)
        self._refused[client] = times  # moved to the end: refused most recently
        while len(self._refused) > self.max_clients:
            del self._refused[next(iter(self._refused))]


def client_address(request: Request) -> str:
    """Where a request came from: the peer's address, or the client's that a trusted proxy forwarded (uvicorn's
    forwarded_allow_ips, set by EVO_HUB_FORWARDED_ALLOW_IPS)."""
    return request.client.host if request.client else "unknown"


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
    runtimes: dict = Field(
        description="the runtimes its last heartbeat reported, keyed claude-code, opencode or codex, each "
        "{available, version, reason, models}; models lists what the runtime offers there, null when it lists none"
    )
    checkouts: dict = Field(
        description="the checkouts its last heartbeat reported, keyed <project>/<repo>, each {path, branch}"
    )
    free_slots: int | None = Field(description="the free slots its last heartbeat reported; null before the first")
    allow_web_terminal: bool
    dispatch_from: Literal[DISPATCH_FROM] = Field(
        description="who may hand it runs: any, runs its owner dispatched with any credential; web, only runs "
        "dispatched from a web session"
    )
    held_runs: int = Field(description="runs it holds now (leased, running, interactive or verifying)")
    created_at: datetime
    last_heartbeat_at: datetime | None
    drained_at: datetime | None
    revoked_at: datetime | None


class DispatchFrom(BaseModel):
    value: Literal[DISPATCH_FROM] = Field(
        description="any: runs dispatched with any credential of the owner; web: only runs dispatched from a web "
        "session, so a token cannot hand the worker work"
    )


class WorkerCredential(BaseModel):
    token: str = Field(description="the worker token, evw_...; shown once, the hub keeps only its SHA-256")
    token_id: int
    expires_at: datetime = Field(description="moves forward with every use")
    worker: Worker


# Reading workers


def _listed(*where):
    """The workers that match ``where``, newest first, with what a Worker shows of each and the database's now."""
    w, users, wp, projects = tables.workers, tables.users, tables.worker_projects, tables.projects
    names = (
        select(projects.c.name)
        .join_from(wp, projects, projects.c.id == wp.c.project_id)
        .where(wp.c.worker_id == w.c.id)
        .order_by(projects.c.name)
    )
    held_runs = (
        select(func.count())
        .select_from(tables.runs)
        .where(tables.runs.c.worker_id == w.c.id, tables.runs.c.state.in_(runs.HELD_STATES))
        .scalar_subquery()
    )
    return (
        select(
            w.c.id,
            w.c.name,
            users.c.login.label("owner"),
            w.c.hostname,
            w.c.os,
            w.c.arch,
            w.c.agent_version,
            w.c.slots,
            w.c.labels,
            func.array(names.scalar_subquery(), type_=ARRAY(Text)).label("projects"),
            w.c.runtimes,
            w.c.checkouts,
            w.c.free_slots,
            w.c.allow_web_terminal,
            w.c.dispatch_from,
            held_runs.label("held_runs"),
            w.c.created_at,
            w.c.last_heartbeat_at,
            w.c.drained_at,
            w.c.revoked_at,
            func.now().label("now"),
        )
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(*where)
        .order_by(w.c.created_at.desc(), w.c.id.desc())
    )


def _worker(row) -> Worker:
    fields = dict(row._mapping)
    now = fields.pop("now")
    status = runs.worker_status(
        last_heartbeat_at=fields["last_heartbeat_at"],
        drained_at=fields["drained_at"],
        revoked_at=fields["revoked_at"],
        now=now,
    )
    return Worker(**fields, status=status)


async def _workers(conn: AsyncConnection, *where) -> list[Worker]:
    return [_worker(row) for row in await conn.execute(_listed(*where))]


async def _one_worker(conn: AsyncConnection, worker_id: int) -> Worker:
    (found,) = await _workers(conn, tables.workers.c.id == worker_id)
    return found


async def _owned(conn: AsyncConnection, user: Principal, worker_id: int, *, lock: bool = False):
    """(owner_id, owner login, name, token_id, drained_at, revoked_at) of a worker ``user`` owns, or any worker for a
    hub admin; 404 otherwise, the same as for an id the hub never gave out."""
    w, users = tables.workers, tables.users
    statement = (
        select(w.c.owner_id, users.c.login, w.c.name, w.c.token_id, w.c.drained_at, w.c.revoked_at)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.id == worker_id)
    )
    if lock:
        statement = statement.with_for_update(of=w)
    row = (await conn.execute(statement)).one_or_none()
    if row is None or (row.owner_id != user.user_id and not user.admin):
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


async def _name_taken(conn: AsyncConnection, owner_id: int, name: str) -> bool:
    w = tables.workers
    taken = exists().where(w.c.owner_id == owner_id, func.lower(w.c.name) == func.lower(name), w.c.revoked_at.is_(None))
    return (await conn.execute(select(taken))).scalar_one()


def _name_conflict(name: str) -> HTTPException:
    return HTTPException(409, f"there is a worker named {name} already: revoke it first, or choose another name")


async def _create_worker(
    conn: AsyncConnection,
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
    w = tables.workers
    statement = (
        insert(w)
        .values(
            owner_id=owner_id,
            token_id=issued.token_id,
            name=name,
            slots=slots,
            labels=labels,
            allow_web_terminal=allow_web_terminal,
            **host.model_dump(include={"hostname", "os", "arch", "agent_version"}),
        )
        .returning(w.c.id)
    )
    try:
        worker_id = (await conn.execute(statement)).scalar_one()
    except sa_exc.IntegrityError as exc:  # another registration took the name meanwhile
        if not isinstance(exc.orig, psycopg.errors.UniqueViolation):
            raise
        raise _name_conflict(name) from None
    wp = tables.worker_projects
    projects = literal(list(project_ids), ARRAY(BigInteger))
    await conn.execute(
        insert(wp).from_select(
            ["worker_id", "project_id"], select(literal(worker_id, BigInteger), func.unnest(projects))
        )
    )
    return issued, worker_id


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


async def _credential(conn: AsyncConnection, issued, worker_id: int) -> WorkerCredential:
    worker = await _one_worker(conn, worker_id)
    return WorkerCredential(token=issued.token, token_id=issued.token_id, expires_at=issued.expires_at, worker=worker)


# Pairings


async def _prune_pairings(conn: AsyncConnection) -> None:
    """Delete the unused pairings whose expiry is PAIRING_KEPT behind."""
    wp = tables.worker_pairings
    await conn.execute(delete(wp).where(wp.c.used_at.is_(None), wp.c.expires_at < func.now() - PAIRING_KEPT))


async def _live_pairings(conn: AsyncConnection, owner_id: int) -> int:
    """The pairings of ``owner_id`` neither used nor expired. A locked code holds its selector until it is pruned, so
    it counts as live until it expires: a member cannot take more selectors by locking codes with wrong tries."""
    wp = tables.worker_pairings
    live = (
        select(func.count())
        .select_from(wp)
        .where(wp.c.owner_id == owner_id, wp.c.used_at.is_(None), wp.c.expires_at > func.now())
    )
    return (await conn.execute(live)).scalar_one()


def _insert_pairing(code: str, secret: str, owner_id: int, body: PairingRequest, project_ids, labels):
    """A pairing of ``code``, unless an unused pairing holds its selector already: then it returns no row."""
    wp = tables.worker_pairings
    statement = pg_insert(wp).values(
        code_selector=code[:SELECTOR_LENGTH],
        code_hash=code_hash(secret, code),
        owner_id=owner_id,
        name=body.name,
        projects=project_ids,
        slots=body.slots,
        labels=labels,
        allow_web_terminal=body.allow_web_terminal,
        expires_at=func.now() + PAIRING_TTL,
    )
    return statement.on_conflict_do_nothing(
        index_elements=[wp.c.code_selector], index_where=wp.c.used_at.is_(None)
    ).returning(wp.c.id, wp.c.expires_at)


@router.post(
    "/pairings",
    status_code=201,
    response_model=Pairing,
    responses={**REFUSALS, 422: {"model": ErrorBody}, 503: {"model": ErrorBody}},
)
async def create_pairing(request: Request, body: PairingRequest, user: CurrentUser, response: Response) -> Pairing:
    """A code a machine joins with as a worker of the caller; shown once."""
    projects, labels = _unique(body.projects), _unique(body.labels)
    secret = _pairing_secret(request)
    async with request.app.state.engine.begin() as conn:
        project_ids = await _writable(conn, user, projects)
        # One pairing of a member at a time, so two of them cannot both pass the count.
        users = tables.users
        await conn.execute(select(users.c.id).where(users.c.id == user.user_id).with_for_update())
        await _prune_pairings(conn)
        live = await _live_pairings(conn, user.user_id)
        if live >= MAX_LIVE_PAIRINGS:
            raise HTTPException(
                409,
                f"you have {MAX_LIVE_PAIRINGS} pairing codes waiting or locked already: join with one of them, or "
                f"wait until one expires ({int(PAIRING_TTL.total_seconds() // 60)} minutes after it was made)",
            )
        if await _name_taken(conn, user.user_id, body.name):
            raise _name_conflict(body.name)
        for _ in range(DRAWS):
            code = new_code()
            statement = _insert_pairing(code, secret, user.user_id, body, project_ids, labels)
            row = (await conn.execute(statement)).one_or_none()
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


async def _project_names(conn: AsyncConnection, project_ids: list[int]) -> list[str]:
    projects = tables.projects
    ids = literal(list(project_ids), ARRAY(BigInteger))
    names = select(projects.c.name).where(projects.c.id == any_(ids)).order_by(func.array_position(ids, projects.c.id))
    return list((await conn.execute(names)).scalars())


@router.get("/pairings/{pairing_id}", response_model=PairingState, responses={404: {"model": ErrorBody}})
async def pairing_state(request: Request, pairing_id: WorkerId, user: CurrentUser, response: Response) -> PairingState:
    """Whether a machine has joined with one of the caller's pairings yet."""
    wp = tables.worker_pairings
    statement = select(
        wp.c.owner_id,
        wp.c.name,
        wp.c.projects,
        wp.c.slots,
        wp.c.labels,
        wp.c.allow_web_terminal,
        wp.c.attempts,
        wp.c.created_at,
        wp.c.expires_at,
        wp.c.used_at,
        wp.c.worker_id,
        (wp.c.expires_at <= func.now()).label("expired"),
    ).where(wp.c.id == pairing_id)
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(statement)).one_or_none()
        if row is None or row.owner_id != user.user_id:
            raise HTTPException(404, f"you have no pairing {pairing_id} on this hub")
        projects = await _project_names(conn, row.projects)
    if row.used_at is not None:
        status = "joined"
    elif row.attempts >= MAX_WRONG_TRIES:
        status = "locked"
    elif row.expired:
        status = "expired"
    else:
        status = "waiting"
    response.headers.update(NO_STORE)
    return PairingState(
        id=pairing_id,
        status=status,
        name=row.name,
        projects=projects,
        slots=row.slots,
        labels=row.labels,
        allow_web_terminal=row.allow_web_terminal,
        tries_left=max(MAX_WRONG_TRIES - row.attempts, 0),
        created_at=row.created_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        worker_id=row.worker_id,
    )


def _find_pairing(selector: str):
    """The unused pairing that holds ``selector``, its row locked, and whether it has not expired."""
    wp = tables.worker_pairings
    return (
        select(
            wp.c.id,
            wp.c.code_hash,
            wp.c.owner_id,
            wp.c.name,
            wp.c.projects,
            wp.c.slots,
            wp.c.labels,
            wp.c.allow_web_terminal,
            wp.c.attempts,
            (wp.c.expires_at > func.now()).label("live"),
        )
        .where(wp.c.code_selector == selector, wp.c.used_at.is_(None))
        .with_for_update()
    )


@worker_router.post(
    "/join",
    status_code=201,
    response_model=WorkerCredential,
    responses={
        403: {"model": ErrorBody},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
        429: {"model": ErrorBody, "description": "too many refused codes from this address; see Retry-After"},
        503: {"model": ErrorBody},
    },
)
async def join(request: Request, body: JoinRequest, response: Response) -> WorkerCredential:
    """Trade a pairing code for the token of a new worker; the token is shown once."""
    limit: RefusalLimit = request.app.state.join_refusals
    client = client_address(request)
    wait = limit.retry_after(client)
    if wait is not None:
        raise HTTPException(
            429,
            f"too many refused pairing codes from this address: try again in {wait} seconds",
            headers={"Retry-After": str(wait)},
        )
    code = normal_code(body.code)
    if code is None:
        raise HTTPException(422, f"a pairing code is {CODE_LENGTH} characters of Crockford base32, written XXXX-XXXX")
    secret = _pairing_secret(request)
    refused = False
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(_find_pairing(code[:SELECTOR_LENGTH]))).one_or_none()
        if row is None or row.attempts >= MAX_WRONG_TRIES or not row.live:
            refused = True
        elif not same(code_hash(secret, code), row.code_hash):
            # Counted in this transaction, which commits: a refusal must not roll the wrong try back.
            wp = tables.worker_pairings
            await conn.execute(update(wp).values(attempts=wp.c.attempts + 1).where(wp.c.id == row.id))
            refused = True
            tries_left = MAX_WRONG_TRIES - row.attempts - 1
            log.warning("wrong pairing code", extra={"pairing_id": row.id, "tries_left": tries_left})
        else:
            credential = await _join(conn, row, body)
    if refused:
        limit.refuse(client)
        if limit.retry_after(client) is not None:
            log.warning("pairing joins limited", extra={"refusals": limit.limit, "window_s": limit.window})
        raise HTTPException(403, REFUSED_CODE)
    response.headers.update(NO_STORE)
    return credential


async def _join(conn: AsyncConnection, pairing, host: JoinRequest) -> WorkerCredential:
    pairing_id, owner_id, name, project_ids = pairing.id, pairing.owner_id, pairing.name, pairing.projects
    grants = tables.grants
    owner_writes = (
        select(func.count())
        .select_from(grants)
        .where(
            grants.c.user_id == owner_id,
            grants.c.project_id == any_(literal(list(project_ids), ARRAY(BigInteger))),
            grants.c.role.in_(WRITER_ROLES),
        )
    )
    writes = (await conn.execute(owner_writes)).scalar_one()
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
        slots=pairing.slots,
        labels=list(pairing.labels),
        allow_web_terminal=pairing.allow_web_terminal,
        project_ids=project_ids,
        host=host,
    )
    wp = tables.worker_pairings
    await conn.execute(update(wp).values(used_at=func.now(), worker_id=worker_id).where(wp.c.id == pairing_id))
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
    async with request.app.state.engine.begin() as conn:
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


@router.get("", response_model=list[Worker], responses=PAGED)
async def list_workers(
    request: Request,
    user: CurrentUser,
    revoked: Annotated[bool, Query(description="also list revoked workers")] = False,
    page: Paging = WHOLE,
) -> list[Worker]:
    """The caller's workers, newest first; every worker for a hub admin."""
    w = tables.workers
    where = []
    if not user.admin:
        where.append(w.c.owner_id == user.user_id)
    if not revoked:
        where.append(w.c.revoked_at.is_(None))
    async with request.app.state.engine.begin() as conn:
        return [_worker(row) for row in await page.rows(conn, _listed(*where))]


@router.get("/{worker_id}", response_model=Worker, responses={404: {"model": ErrorBody}})
async def show_worker(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    async with request.app.state.engine.begin() as conn:
        await _owned(conn, user, worker_id)
        return await _one_worker(conn, worker_id)


def _target(worker_id: int, name: str, owner: str) -> str:
    return f"worker:{worker_id} name={name} owner={owner}"


async def _set_drain(request: Request, user: Principal, worker_id: int, drain: bool) -> Worker:
    async with request.app.state.engine.begin() as conn:
        owner_id, owner, name, _, drained_at, revoked_at = await _owned(conn, user, worker_id, lock=True)
        if not drain and owner_id != user.user_id:  # a hub admin may stop a worker, never set one going again
            raise HTTPException(
                403, f"only {owner}, who owns worker {worker_id}, may undrain it; a hub admin may drain or revoke it"
            )
        if revoked_at is not None:
            raise HTTPException(409, f"worker {worker_id} was revoked at {revoked_at.isoformat()}; it takes no runs")
        if (drained_at is not None) != drain:  # drained already, or not drained: nothing changes, nothing is audited
            w = tables.workers
            await conn.execute(update(w).values(drained_at=func.now() if drain else None).where(w.c.id == worker_id))
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
    """Let a drained worker claim runs again; its owner only."""
    return await _set_drain(request, user, worker_id, False)


DISPATCH_FROM_SESSION = (
    "who may dispatch to a worker is set from a web session only, so a token that leaked cannot open the worker "
    "again: sign in on the web"
)


@router.post("/{worker_id}/dispatch-from", response_model=Worker, responses={**REFUSALS, 422: {"model": ErrorBody}})
async def set_dispatch_from(request: Request, worker_id: WorkerId, body: DispatchFrom, user: CurrentUser) -> Worker:
    """Set who may hand the worker its runs: its owner, from a web session only."""
    if user.kind != WEB:
        raise HTTPException(403, DISPATCH_FROM_SESSION)
    async with request.app.state.engine.begin() as conn:
        owner_id, owner, name, _, _, revoked_at = await _owned(conn, user, worker_id, lock=True)
        if owner_id != user.user_id:
            raise HTTPException(403, f"only {owner}, who owns worker {worker_id}, may set who dispatches to it")
        if revoked_at is not None:
            raise HTTPException(409, f"worker {worker_id} was revoked at {revoked_at.isoformat()}; it takes no runs")
        w = tables.workers
        value = (await conn.execute(select(w.c.dispatch_from).where(w.c.id == worker_id))).scalar_one()
        if value != body.value:  # the value it has already: nothing changes, nothing is audited
            await conn.execute(update(w).values(dispatch_from=body.value).where(w.c.id == worker_id))
            target = f"{_target(worker_id, name, owner)} dispatch_from={body.value}"
            action = audit.WORKER_DISPATCH_FROM
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
            log.info("worker dispatch_from set", extra={"worker_id": worker_id, "dispatch_from": body.value})
        return await _one_worker(conn, worker_id)


@router.post("/{worker_id}/revoke", response_model=Worker, responses=REFUSALS)
async def revoke(request: Request, worker_id: WorkerId, user: CurrentUser) -> Worker:
    """End the worker and its token at once, and release the runs it holds."""
    async with request.app.state.engine.begin() as conn:
        _, owner, name, token_id, _, revoked_at = await _owned(conn, user, worker_id, lock=True)
        if revoked_at is not None:
            raise HTTPException(409, f"worker {worker_id} was revoked already, at {revoked_at.isoformat()}")
        released = await end_worker(conn, user, worker_id, name, owner, token_id)
        worker = await _one_worker(conn, worker_id)
    log.info("worker revoked", extra={"worker_id": worker_id, "by": user.login, "runs_released": released})
    await revoke_leased_tokens(request.app.state, worker_id)
    return worker


async def lock_worker_of_token(
    conn: AsyncConnection, token_id: int, owner_id: int | None = None
) -> tuple[int, str, str] | None:
    """(id, name, owner login) of the live worker whose token is ``token_id``, of ``owner_id`` when given, with its
    row locked; None for any other token. Called before the token's row is changed, so a token revocation locks the
    two rows in the order POST /v1/workers/{id}/revoke does, and neither waits on the other in the opposite order."""
    w, users = tables.workers, tables.users
    statement = (
        select(w.c.id, w.c.name, users.c.login)
        .join_from(w, users, users.c.id == w.c.owner_id)
        .where(w.c.token_id == token_id, w.c.revoked_at.is_(None))
    )
    if owner_id is not None:
        statement = statement.where(w.c.owner_id == owner_id)
    row = (await conn.execute(statement.with_for_update(of=w))).one_or_none()
    return None if row is None else tuple(row)


async def end_worker(
    conn: AsyncConnection, actor: Principal, worker_id: int, name: str, owner: str, token_id: int
) -> int:
    """Revoke a live worker whose row the caller holds locked, and its token, release the runs it holds, give back
    every lease it still has and add the worker.revoke audit row, all in the caller's transaction; the caller revokes
    their GitHub tokens once it commits (``revoke_leased_tokens``). Returns how many runs were released."""
    w, tokens = tables.workers, tables.tokens
    await conn.execute(update(w).values(revoked_at=func.now()).where(w.c.id == worker_id))
    await conn.execute(
        update(tokens).values(revoked_at=func.now()).where(tokens.c.id == token_id, tokens.c.revoked_at.is_(None))
    )
    released = await release_runs(conn, worker_id, f"its worker {name} was revoked")
    # The runs it released gave theirs back as they moved; this takes the rest, of runs that left it otherwise.
    await credentials.end_leases(
        conn, worker_id=worker_id, actor_id=actor.user_id, token_id=actor.token_id, by="worker-revoked"
    )
    target, action = _target(worker_id, name, owner), audit.WORKER_REVOKE
    await audit.record(conn, actor_id=actor.user_id, token_id=actor.token_id, action=action, target=target)
    return released


async def revoke_leased_tokens(app_state, worker_id: int) -> None:
    """Once ``end_worker`` committed: revoke at GitHub the tokens the worker's leases held. GitHub failing leaves them
    to the reaper's next pass."""
    await credentials.revoke_tokens(
        app_state.engine, app_state.sealer, credentials.revoker(app_state), worker_id=worker_id
    )

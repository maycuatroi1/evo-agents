"""A project's data loss prevention policy, its routes, and its enforcement before a model call leaves the hub
(``evo_agents.hub.dlp``, ``docs/hub.md``, section "Data loss prevention").

GET /v1/projects/{p}/dlp-policy gives a reader the policy and its counts; PUT sets it, for the project's admin; POST
/v1/projects/{p}/dlp/inspect gives a reader what the policy makes of a text, as a quick check that counts nothing.

``enforce`` applies the policy of a run's project to the body of a model call, for the gateway route and the hub
runtime: it gives the body to send, or raises ``Blocked``, and counts in dlp_counts what it redacted or blocked, by
rule kind. A log line names the kinds and their counts, never a matched value, and so does a refusal.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import dlp, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit, route_autonomy
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["dlp"], responses={401: {"model": ErrorBody}})

BLOCKED = "the project's DLP policy blocked this call: it holds {what}"

Mode = Literal["off", "redact", "block"]
Kind = Literal["cif", "national_id", "account_number", "financial_figure", "contract_keyword"]
Keyword = Annotated[str, Field(min_length=1, max_length=dlp.MAX_KEYWORD_CHARS)]


class DlpPolicyWrite(BaseModel):
    mode: Mode
    kinds: list[Kind] = Field(default_factory=lambda: list(dlp.KINDS))
    keywords: list[Keyword] = Field(default_factory=list, max_length=dlp.MAX_KEYWORDS)


class DlpCount(BaseModel):
    kind: Kind
    surface: Literal["gateway_proxy", "hub_runtime"]
    action: Literal["redacted", "blocked"]
    count: int


class DlpPolicy(BaseModel):
    project: str
    mode: Mode
    kinds: list[Kind]
    keywords: list[str]
    builtin_keywords: list[str]
    updated_at: str | None
    counts: list[DlpCount]


class DlpInspectIn(BaseModel):
    text: str = Field(max_length=dlp.MAX_INSPECT_BYTES)


class DlpFinding(BaseModel):
    kind: Kind
    count: int


class DlpInspection(BaseModel):
    mode: Mode
    blocked: bool
    text: str  # the text as the policy sends it: redacted in redact mode, as given otherwise
    findings: list[DlpFinding]


class Blocked(Exception):
    """A model call the policy blocked; ``kinds`` counts its matches by rule kind."""

    def __init__(self, counts):
        self.counts = dict(counts)
        super().__init__(BLOCKED.format(what=what(self.counts)))


def what(counts) -> str:
    """``counts`` as words: kinds and numbers, never a value."""
    return ", ".join(f"{count} {kind}" for kind, count in sorted(dict(counts).items()))


async def policy_of(conn: AsyncConnection, project_id: int) -> dlp.Policy:
    """The policy of project ``project_id``: off when its admin set none."""
    d = tables.dlp_policies
    row = (await conn.execute(select(d.c.mode, d.c.kinds, d.c.keywords).where(d.c.project_id == project_id))).first()
    if row is None:
        return dlp.Policy()
    return dlp.Policy(row.mode, tuple(row.kinds), tuple(row.keywords))


async def count(conn: AsyncConnection, project_id: int, surface: str, action: str, counts) -> None:
    """Add ``counts`` (by rule kind) to dlp_counts of ``project_id``, ``surface`` and ``action``."""
    c = tables.dlp_counts
    for kind, number in sorted(dict(counts).items()):
        keys = {"project_id": project_id, "kind": kind, "surface": surface, "action": action}
        statement = pg_insert(c).values(**keys, count=number)
        await conn.execute(
            statement.on_conflict_do_update(
                index_elements=[c.c.project_id, c.c.kind, c.c.surface, c.c.action],
                set_={"count": c.c.count + statement.excluded.count, "last_at": func.now()},
            )
        )


async def enforce(engine: AsyncEngine, project_id: int, surface: str, content: bytes, *, run_id: int) -> bytes:
    """The JSON body ``content`` of a model call of run ``run_id`` as its project's policy sends it; ``Blocked`` when
    the policy blocks it. What the policy found is counted on ``surface``."""
    async with engine.begin() as conn:
        policy = await policy_of(conn, project_id)
        if not policy.on:
            return content
        made, scan = dlp.apply_bytes(policy, content)
        if not scan.counts:
            return content
        action = "blocked" if scan.blocked else "redacted"
        await count(conn, project_id, surface, action, scan.counts)
    log.info("dlp " + action, extra={"run_id": run_id, "surface": surface, "kinds": dict(scan.counts)})
    if scan.blocked:
        raise Blocked(scan.counts)
    return made


async def enforce_json(engine: AsyncEngine, project_id: int, surface: str, body: dict, *, run_id: int) -> dict:
    """``enforce`` on a body not encoded yet."""
    async with engine.begin() as conn:
        policy = await policy_of(conn, project_id)
        if not policy.on:
            return body
        made, scan = dlp.apply_json(policy, body)
        if not scan.counts:
            return body
        action = "blocked" if scan.blocked else "redacted"
        await count(conn, project_id, surface, action, scan.counts)
    log.info("dlp " + action, extra={"run_id": run_id, "surface": surface, "kinds": dict(scan.counts)})
    if scan.blocked:
        raise Blocked(scan.counts)
    return made


# The routes


def _reader(access: ProjectAccess) -> None:
    if access.role is None:
        raise HTTPException(403, f"reading the DLP policy of project {access.name} needs a grant on it")


async def _view(conn: AsyncConnection, access: ProjectAccess) -> DlpPolicy:
    d, c = tables.dlp_policies, tables.dlp_counts
    row = (await conn.execute(select(d).where(d.c.project_id == access.project_id))).first()
    counted = (
        await conn.execute(
            select(c.c.kind, c.c.surface, c.c.action, c.c.count)
            .where(c.c.project_id == access.project_id)
            .order_by(c.c.kind, c.c.surface, c.c.action)
        )
    ).all()
    counts = [DlpCount(kind=r.kind, surface=r.surface, action=r.action, count=r.count) for r in counted]
    builtin = list(dlp.CONTRACT_KEYWORDS)
    if row is None:
        return DlpPolicy(
            project=access.name,
            mode="off",
            kinds=list(dlp.KINDS),
            keywords=[],
            builtin_keywords=builtin,
            updated_at=None,
            counts=counts,
        )
    return DlpPolicy(
        project=access.name,
        mode=row.mode,
        kinds=list(row.kinds),
        keywords=list(row.keywords),
        builtin_keywords=builtin,
        updated_at=row.updated_at.isoformat(),
        counts=counts,
    )


@router.get("/{project}/dlp-policy", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def show_policy(request: Request, project: ProjectName, user: CurrentUser) -> DlpPolicy:
    """The project's DLP policy, off when none was set, and the counts of what it redacted and blocked."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        return await _view(conn, access)


@router.put(
    "/{project}/dlp-policy",
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
    dependencies=[route_autonomy.K12],
)
async def write_policy(request: Request, project: ProjectName, body: DlpPolicyWrite, user: CurrentUser) -> DlpPolicy:
    """Set the project's DLP policy, for its admin: mode off, redact or block, the rule kinds, and the keywords of
    confidential contracts it adds to the built-in ones."""
    d = tables.dlp_policies
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "admin"):
            held = access.role or "no grant"
            raise HTTPException(
                403, f"setting the DLP policy of project {project} needs the admin role; you hold {held}"
            )
        values = {
            "mode": body.mode,
            "kinds": sorted(set(body.kinds), key=dlp.KINDS.index),
            "keywords": sorted({word.strip() for word in body.keywords if word.strip()}),
            "updated_by": user.user_id,
        }
        statement = pg_insert(d).values(project_id=access.project_id, **values)
        await conn.execute(
            statement.on_conflict_do_update(index_elements=[d.c.project_id], set_={**values, "updated_at": func.now()})
        )
        target = f"{project} mode={body.mode} kinds={len(values['kinds'])} keywords={len(values['keywords'])}"
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.DLP_POLICY,
            target=target,
            project_id=access.project_id,
        )
        return await _view(conn, access)


@router.post("/{project}/dlp/inspect", responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def inspect(request: Request, project: ProjectName, body: DlpInspectIn, user: CurrentUser) -> DlpInspection:
    """What the project's policy makes of ``text``: the text it sends, whether it blocks it, and its matches by kind.
    A quick check: nothing is stored or counted."""
    async with request.app.state.engine.connect() as conn:
        access = await project_access(conn, user, project)
        _reader(access)
        policy = await policy_of(conn, access.project_id)
    scan = dlp.apply(policy, body.text)
    findings = [DlpFinding(kind=kind, count=n) for kind, n in sorted(scan.counts.items(), key=lambda k: k[0])]
    return DlpInspection(mode=policy.mode, blocked=scan.blocked, text=scan.text, findings=findings)  # type: ignore[arg-type]

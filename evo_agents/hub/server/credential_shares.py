"""Shares of secrets and accounts: GET and POST /v1/secrets/{name}/shares, DELETE /v1/shares/{id}
(``docs/credentials.md``; the rules and the terms warning are ``evo_agents.hub.credential_shares``).

POST /v1/secrets/{name}/shares shares the caller's own secret ``name``, an account being an env secret, with one
member (``grantee_kind`` user and ``login``), a unit of the org chart that is live, neither archived nor in a cohort
that ended (unit and ``unit``), or everyone (all). ``projects`` are some of the projects the secret is bound to now.
``level``, ``budget_usd``, ``usage_ceiling_pct``, ``max_running`` and ``expires_at`` bound what its use may be; each
is left out for none. A secret of kind forge is never shared (422), nor is one with a live share to the same grantee
already (409). A personal subscription token (``credential_class`` subscription) is shared only with ``terms_ack``
true: without it the answer is 409 whose message holds TERMS_WARNING word for word; with it the share keeps
terms_ack_at and the audit gets a row share.terms_ack holding the warning, after share.create.

GET /v1/secrets/{name}/shares lists the shares of the caller's secret, revoked ones too, newest first; GET
/v1/secrets and GET /v1/accounts give the live ones of each (``share_views``). DELETE /v1/shares/{id} revokes a share:
its secret's owner may, and a hub admin may revoke anyone's; a revoked share stays, with revoked_at and revoked_by.
Only the owner creates a share, a hub admin included: the routes read the caller's own secrets.

Each write adds audit rows share.create, share.terms_ack or share.revoke (``audit``), which name the secret by id; no
row, log line or answer holds a value.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org, tables
from evo_agents.hub.credential_shares import (
    ALL,
    GRANTEE_KINDS,
    MAX_BUDGET_USD,
    MAX_RUNNING,
    MAX_SHARE_PROJECTS,
    MAX_SHARES_PER_SECRET,
    TERMS_WARNING,
    UNIT,
    USER,
    credential_class,
    needs_terms_ack,
    terms_refusal,
)
from evo_agents.hub.credentials import FORGE_KIND, SECRET_NAME
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.admin import LOGIN_NAME, PROJECT_NAME
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.secrets import NO_SECRET, audit_target
from evo_agents.hub.server.security import CurrentUser, Principal
from evo_agents.hub.server.share_views import CredentialShare, share_of, share_query

log = logging.getLogger(__name__)

NO_SHARE = "there is no share {share_id} of yours on this hub: see GET /v1/secrets/{{name}}/shares"
MAX_SHARE_ID = 2**63 - 1

router = APIRouter(prefix="/v1", tags=["shares"], responses={401: {"model": ErrorBody}})

SecretName = Annotated[str, Path(pattern=f"^{SECRET_NAME.pattern}$", description="one of the caller's secrets")]
ShareId = Annotated[int, Path(ge=1, le=MAX_SHARE_ID, description="the id of the share")]
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
WRITE_REFUSALS = {**REFUSALS, 409: {"model": ErrorBody}, 422: {"model": ErrorBody}}


class ShareWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grantee_kind: Literal[GRANTEE_KINDS] = Field(
        description="user: one member, named by login; unit: a unit of the org chart and every unit below it, named "
        "by unit; all: everyone on the hub"
    )
    login: str | None = Field(None, pattern=LOGIN_NAME, description="grantee_kind user only: the member's login")
    unit: str | None = Field(None, pattern=org.SLUG_PATTERN, description="grantee_kind unit only: the unit's slug")
    projects: list[Annotated[str, Field(pattern=PROJECT_NAME)]] = Field(
        min_length=1,
        max_length=MAX_SHARE_PROJECTS,
        description="the projects whose runs may use it, each one the secret is bound to",
    )
    level: Literal[org.LEVELS] | None = Field(None, description="the autonomy level a run using it is held to")
    budget_usd: float | None = Field(
        None, gt=0, le=MAX_BUDGET_USD, description="the most the runs using it may cost a month, in USD"
    )
    usage_ceiling_pct: int | None = Field(
        None, ge=1, le=100, description="no run takes the account while its usage is at this percent or above"
    )
    max_running: int | None = Field(None, ge=1, le=MAX_RUNNING, description="the most runs that may hold it at once")
    expires_at: AwareDatetime | None = Field(None, description="when the share ends; in the future")
    terms_ack: bool = Field(
        False,
        description="the owner acknowledges the terms warning, which sharing a personal subscription token needs",
    )


# Reading


async def _own_secret(conn: AsyncConnection, user: Principal, name: str, *, lock: bool = False):
    """(id, kind, env_var) of the caller's live secret ``name``; 404 for any other name, whoever's it is."""
    s = tables.secrets
    query = select(s.c.id, s.c.kind, s.c.env_var).where(
        s.c.owner_id == user.user_id, s.c.name == name, s.c.deleted_at.is_(None)
    )
    row = (await conn.execute(query.with_for_update() if lock else query)).first()
    if row is None:
        raise HTTPException(404, NO_SECRET.format(name=name))
    return row


async def _share(conn: AsyncConnection, share_id: int) -> CredentialShare:
    cs = tables.credential_shares
    return share_of((await conn.execute(share_query().where(cs.c.id == share_id))).one())


@router.get("/secrets/{name}/shares", response_model=list[CredentialShare], responses=REFUSALS)
async def list_shares(
    request: Request, name: SecretName, user: CurrentUser, response: Response
) -> list[CredentialShare]:
    """The shares of the caller's secret or account ``name``, revoked ones too, newest first."""
    cs = tables.credential_shares
    async with request.app.state.engine.begin() as conn:
        secret = await _own_secret(conn, user, name)
        rows = (await conn.execute(share_query().where(cs.c.secret_id == secret.id))).all()
    response.headers.update(NO_STORE)
    return [share_of(row) for row in rows]


# Creating


def _grantee_refusal(body: ShareWrite) -> str | None:
    """Why ``body`` does not name its grantee as its kind needs; None when it does."""
    if body.grantee_kind == USER and (body.login is None or body.unit is not None):
        return "a share of grantee_kind user names login, the member, and no unit"
    if body.grantee_kind == UNIT and (body.unit is None or body.login is not None):
        return "a share of grantee_kind unit names unit, the slug of a unit of the org chart, and no login"
    if body.grantee_kind == ALL and (body.login is not None or body.unit is not None):
        return "a share of grantee_kind all names neither login nor unit"
    return None


async def _grantee_user(conn: AsyncConnection, user: Principal, login: str) -> int:
    found = await org_store.user_by_login(conn, login)
    if found is None:
        raise HTTPException(422, f"no member of this hub has the login {login}")
    if found.id == user.user_id:
        raise HTTPException(422, "you own this secret: share it with someone else")
    return found.id


async def _grantee_unit(conn: AsyncConnection, slug: str) -> int:
    """The id of the live unit ``slug``: neither it nor a unit above it archived, nor a cohort holding it ended."""
    chart, today = await org_store.load(conn), await org_store.today(conn)
    unit = chart.unit_by_slug(slug)
    if unit is None:
        raise HTTPException(422, f"the org chart has no unit {slug}: see GET /v1/org")
    for above in chart.ancestors(unit.id):
        if above.archived_at is not None:
            raise HTTPException(422, f"unit {slug} is not live: unit {above.slug} is archived")
        if above.kind == org.COHORT and above.ends_on is not None and above.ends_on < today:
            raise HTTPException(422, f"unit {slug} is not live: cohort {above.slug} ended on {above.ends_on}")
    return unit.id


async def _bound_projects(conn: AsyncConnection, secret_id: int, names: list[str]) -> list[int]:
    """The ids of the projects ``names``, each one the secret is bound to now; 422 naming those it is not."""
    b, p = tables.secret_bindings, tables.projects
    bound = select(p.c.name, p.c.id).distinct().join_from(b, p, p.c.id == b.c.project_id)
    found = {row.name: row.id for row in await conn.execute(bound.where(b.c.secret_id == secret_id))}
    missing = [name for name in names if name not in found]
    if missing:
        listed = ", ".join(sorted(found)) or "none"
        raise HTTPException(
            422, f"the secret is not bound to {', '.join(missing)}: a share is for its projects ({listed})"
        )
    return sorted({found[name] for name in names})


async def _check_room(conn: AsyncConnection, secret_id: int, grantee: dict) -> None:
    """409 when the secret has a live share to the same grantee, or MAX_SHARES_PER_SECRET live ones already."""
    cs = tables.credential_shares
    live = select(cs.c.grantee_kind, cs.c.grantee_user_id, cs.c.grantee_unit_id).where(
        cs.c.secret_id == secret_id, cs.c.revoked_at.is_(None)
    )
    rows = (await conn.execute(live)).all()
    key = (grantee["grantee_kind"], grantee["grantee_user_id"], grantee["grantee_unit_id"])
    if key in {tuple(row) for row in rows}:
        raise HTTPException(409, "this secret has a live share to that grantee already: revoke it to change it")
    if len(rows) >= MAX_SHARES_PER_SECRET:
        raise HTTPException(409, f"this secret has {MAX_SHARES_PER_SECRET} live shares already: revoke one first")


def _target(share: CredentialShare, secret_id: int) -> str:
    """The audit target of a new share: ids and limits, never the secret's name."""
    grantee = share.grantee_kind if share.grantee is None else f"{share.grantee_kind}:{share.grantee}"
    parts = [
        f"share:{share.id}",
        audit_target(secret_id),
        f"credential={share.credential}",
        f"grantee={grantee}",
        f"projects={','.join(share.projects)}",
    ]
    for key in ("level", "budget_usd", "usage_ceiling_pct", "max_running", "expires_at"):
        value = getattr(share, key)
        if value is not None:
            parts.append(f"{key}={value.isoformat() if isinstance(value, datetime) else value}")
    return " ".join(parts)


async def _grantee(conn: AsyncConnection, user: Principal, body: ShareWrite) -> dict:
    grantee = {"grantee_kind": body.grantee_kind, "grantee_user_id": None, "grantee_unit_id": None}
    if body.grantee_kind == USER:
        grantee["grantee_user_id"] = await _grantee_user(conn, user, body.login or "")
    elif body.grantee_kind == UNIT:
        grantee["grantee_unit_id"] = await _grantee_unit(conn, body.unit or "")
    return grantee


async def _insert(conn: AsyncConnection, user: Principal, secret_id: int, body: ShareWrite, values: dict) -> int:
    cs, csp = tables.credential_shares, tables.credential_share_projects
    project_ids = await _bound_projects(conn, secret_id, list(dict.fromkeys(body.projects)))
    limits = body.model_dump(include={"level", "budget_usd", "usage_ceiling_pct", "max_running", "expires_at"})
    made = insert(cs).values(secret_id=secret_id, created_by=user.user_id, **values, **limits).returning(cs.c.id)
    share_id = (await conn.execute(made)).scalar_one()
    await conn.execute(insert(csp), [{"share_id": share_id, "project_id": project_id} for project_id in project_ids])
    return share_id


@router.post("/secrets/{name}/shares", status_code=201, response_model=CredentialShare, responses=WRITE_REFUSALS)
async def create_share(
    request: Request, name: SecretName, body: ShareWrite, user: CurrentUser, response: Response
) -> CredentialShare:
    """Share the caller's secret or account ``name``; a personal subscription token needs ``terms_ack``."""
    refusal = _grantee_refusal(body)
    if refusal is not None:
        raise HTTPException(422, refusal)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise HTTPException(422, "expires_at is past already: a share's end is in the future")
    async with request.app.state.engine.begin() as conn:
        secret = await _own_secret(conn, user, name, lock=True)  # one write of a secret's shares at a time
        if secret.kind == FORGE_KIND:
            raise HTTPException(422, "a secret of kind forge is never shared: only the hub uses it")
        credential = credential_class(secret.kind, secret.env_var)
        acked = needs_terms_ack(credential)
        if acked and not body.terms_ack:
            raise HTTPException(409, terms_refusal())
        grantee = await _grantee(conn, user, body)
        await _check_room(conn, secret.id, grantee)
        share_id = await _insert(
            conn, user, secret.id, body, {**grantee, "terms_ack_at": func.now() if acked else None}
        )
        share = await _share(conn, share_id)
        record = {"actor_id": user.user_id, "token_id": user.token_id}
        await audit.record(conn, **record, action=audit.SHARE_CREATE, target=_target(share, secret.id))
        if acked:
            warning = f"share:{share_id} {audit_target(secret.id)} warning={TERMS_WARNING}"
            await audit.record(conn, **record, action=audit.SHARE_TERMS_ACK, target=warning)
    extra = {"share": share_id, "login": user.login, "credential": credential, "grantee_kind": body.grantee_kind}
    log.info("credential shared", extra=extra)
    response.headers.update(NO_STORE)
    return share


# Revoking


@router.delete("/shares/{share_id}", response_model=CredentialShare, responses={**REFUSALS, 409: {"model": ErrorBody}})
async def revoke_share(request: Request, share_id: ShareId, user: CurrentUser, response: Response) -> CredentialShare:
    """Revoke a share: its secret's owner may, and a hub admin may revoke any."""
    cs, s = tables.credential_shares, tables.secrets
    found = select(cs.c.secret_id, cs.c.revoked_at, s.c.owner_id).join_from(cs, s, s.c.id == cs.c.secret_id)
    async with request.app.state.engine.begin() as conn:
        row = (await conn.execute(found.where(cs.c.id == share_id).with_for_update(of=cs))).first()
        if row is None or (row.owner_id != user.user_id and not user.admin):
            raise HTTPException(404, NO_SHARE.format(share_id=share_id))
        if row.revoked_at is not None:
            raise HTTPException(409, f"share {share_id} is revoked already")
        await conn.execute(update(cs).values(revoked_at=func.now(), revoked_by=user.user_id).where(cs.c.id == share_id))
        by = "owner" if row.owner_id == user.user_id else "admin"
        target = f"share:{share_id} {audit_target(row.secret_id)} by={by}"
        await audit.record(
            conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SHARE_REVOKE, target=target
        )
        share = await _share(conn, share_id)
    log.info("credential share revoked", extra={"share": share_id, "login": user.login, "by": by})
    response.headers.update(NO_STORE)
    return share

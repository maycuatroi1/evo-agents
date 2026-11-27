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

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import org
from evo_agents.hub.credential_shares import (
    ALL,
    MAX_SHARES_PER_SECRET,
    TERMS_WARNING,
    UNIT,
    USER,
    credential_class,
    needs_terms_ack,
    terms_refusal,
)
from evo_agents.hub.credentials import FORGE_KIND
from evo_agents.hub.server import audit, org_store
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.credentials.models.secrets import NO_SECRET
from evo_agents.hub.server.credentials.models.shares import NO_SHARE, ShareWrite
from evo_agents.hub.server.credentials.repository import shares as repository
from evo_agents.hub.server.credentials.service.secrets import audit_target
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.share_leases import end_share_leases
from evo_agents.hub.server.share_views import CredentialShare

log = logging.getLogger(__name__)


# Reading


async def _own_secret(conn: AsyncConnection, user: Principal, name: str, *, lock: bool = False):
    """(id, kind, env_var) of the caller's live secret ``name``; 404 for any other name, whoever's it is."""
    row = await repository.own_secret(conn, user.user_id, name, lock=lock)
    if row is None:
        raise NotFound(NO_SECRET.format(name=name))
    return row


async def listed(conn: AsyncConnection, user: Principal, name: str) -> list[CredentialShare]:
    """The shares of the caller's secret or account ``name``, revoked ones too, newest first."""
    secret = await _own_secret(conn, user, name)
    return await repository.shares_of_secret(conn, secret.id)


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
        raise Unprocessable(f"no member of this hub has the login {login}")
    if found.id == user.user_id:
        raise Unprocessable("you own this secret: share it with someone else")
    return found.id


async def live_unit(conn: AsyncConnection, slug: str) -> int:
    """The id of the live unit ``slug``: neither it nor a unit above it archived, nor a cohort holding it ended."""
    chart, today = await org_store.load(conn), await org_store.today(conn)
    unit = chart.unit_by_slug(slug)
    if unit is None:
        raise Unprocessable(f"the org chart has no unit {slug}: see GET /v1/org")
    for above in chart.ancestors(unit.id):
        if above.archived_at is not None:
            raise Unprocessable(f"unit {slug} is not live: unit {above.slug} is archived")
        if above.kind == org.COHORT and above.ends_on is not None and above.ends_on < today:
            raise Unprocessable(f"unit {slug} is not live: cohort {above.slug} ended on {above.ends_on}")
    return unit.id


async def _bound_projects(conn: AsyncConnection, secret_id: int, names: list[str]) -> list[int]:
    """The ids of the projects ``names``, each one the secret is bound to now; 422 naming those it is not."""
    found = await repository.bound_projects(conn, secret_id)
    missing = [name for name in names if name not in found]
    if missing:
        listed = ", ".join(sorted(found)) or "none"
        raise Unprocessable(f"the secret is not bound to {', '.join(missing)}: a share is for its projects ({listed})")
    return sorted({found[name] for name in names})


async def _check_room(conn: AsyncConnection, secret_id: int, grantee: dict) -> None:
    """409 when the secret has a live share to the same grantee, or MAX_SHARES_PER_SECRET live ones already."""
    rows = await repository.live_grantees(conn, secret_id)
    key = (grantee["grantee_kind"], grantee["grantee_user_id"], grantee["grantee_unit_id"])
    if key in set(rows):
        raise Conflict("this secret has a live share to that grantee already: revoke it to change it")
    if len(rows) >= MAX_SHARES_PER_SECRET:
        raise Conflict(f"this secret has {MAX_SHARES_PER_SECRET} live shares already: revoke one first")


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
        grantee["grantee_unit_id"] = await live_unit(conn, body.unit or "")
    return grantee


async def _insert(conn: AsyncConnection, user: Principal, secret_id: int, body: ShareWrite, values: dict) -> int:
    project_ids = await _bound_projects(conn, secret_id, list(dict.fromkeys(body.projects)))
    limits = body.model_dump(include={"level", "budget_usd", "usage_ceiling_pct", "max_running", "expires_at"})
    return await repository.insert_share(
        conn, {"secret_id": secret_id, "created_by": user.user_id, **values, **limits}, project_ids
    )


def checked(body: ShareWrite) -> None:
    """422 for a share ``body`` that cannot be, before any transaction."""
    refusal = _grantee_refusal(body)
    if refusal is not None:
        raise Unprocessable(refusal)
    if body.expires_at is not None and body.expires_at <= datetime.now(UTC):
        raise Unprocessable("expires_at is past already: a share's end is in the future")


async def create(conn: AsyncConnection, user: Principal, name: str, body: ShareWrite) -> CredentialShare:
    """Share the caller's secret or account ``name`` as ``body`` says, in the caller's transaction (``checked`` read it
    first); audit rows share.create, and share.terms_ack for a personal subscription token."""
    secret = await _own_secret(conn, user, name, lock=True)  # one write of a secret's shares at a time
    if secret.kind == FORGE_KIND:
        raise Unprocessable("a secret of kind forge is never shared: only the hub uses it")
    credential = credential_class(secret.kind, secret.env_var)
    acked = needs_terms_ack(credential)
    if acked and not body.terms_ack:
        raise Conflict(terms_refusal())
    grantee = await _grantee(conn, user, body)
    await _check_room(conn, secret.id, grantee)
    share_id = await _insert(
        conn, user, secret.id, body, {**grantee, "terms_ack_at": repository.NOW if acked else None}
    )
    share = await repository.share(conn, share_id)
    record = {"actor_id": user.user_id, "token_id": user.token_id}
    await audit.record(conn, **record, action=audit.SHARE_CREATE, target=_target(share, secret.id))
    if acked:
        warning = f"share:{share_id} {audit_target(secret.id)} warning={TERMS_WARNING}"
        await audit.record(conn, **record, action=audit.SHARE_TERMS_ACK, target=warning)
    extra = {"share": share_id, "login": user.login, "credential": credential, "grantee_kind": body.grantee_kind}
    log.info("credential shared", extra=extra)
    return share


# Revoking


async def revoke(conn: AsyncConnection, user: Principal, share_id: int) -> CredentialShare:
    """Revoke share ``share_id``, in the caller's transaction: its secret's owner may, and a hub admin may revoke any;
    one audit row share.revoke."""
    row = await repository.share_to_revoke(conn, share_id)
    if row is None or (row.owner_id != user.user_id and not user.admin):
        raise NotFound(NO_SHARE.format(share_id=share_id))
    if row.revoked_at is not None:
        raise Conflict(f"share {share_id} is revoked already")
    await repository.revoke(conn, share_id, user.user_id)
    ended = await end_share_leases(conn, share_id)  # a lease under way waits on the share's row, then ends here
    by = "owner" if row.owner_id == user.user_id else "admin"
    target = f"share:{share_id} {audit_target(row.secret_id)} by={by}"
    await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=audit.SHARE_REVOKE, target=target)
    share = await repository.share(conn, share_id)
    log.info("credential share revoked", extra={"share": share_id, "login": user.login, "by": by, "leases": ended})
    return share

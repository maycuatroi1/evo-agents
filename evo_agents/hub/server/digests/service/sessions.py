"""Session digests: who may push the digest of a session (``put``), list the project's (``listed``) and read one
(``shown``), and their retention (``prune_digests``); the package's docstring says the rules."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.server.base.errors import Forbidden, NotFound
from evo_agents.hub.server.digests.models.sessions import (
    DIGEST_DAYS,
    DigestList,
    DigestRecord,
    DigestSummary,
    SessionDigest,
    not_found,
    not_theirs,
)
from evo_agents.hub.server.digests.repository import sessions as repository
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def put(
    conn: AsyncConnection, user: Principal, project: str, session_id: str, body: SessionDigest
) -> DigestSummary:
    """Write the digest of session ``session_id``, replacing the one this member pushed before; 403 when another
    member pushed the session first."""
    access = await project_access(conn, user, project)
    label = access.push_label()
    written = await repository.written(conn, access.project_id, user.user_id, session_id, label, body)
    if written is None:
        raise Forbidden(not_theirs(project, session_id))
    return await repository.summary(conn, written)


async def listed(
    conn: AsyncConnection,
    user: Principal,
    project: str,
    *,
    since: datetime | None,
    login: str | None,
    limit: int,
    offset: int,
    sink: str | None,
) -> DigestList:
    """The digests of the project the caller may read through ``sink``, the latest pushed first."""
    access = await project_access(conn, user, project)
    plan_service.reader(access)
    rows = await repository.listed(conn, access.project_id, since, login)
    visible = [DigestSummary(**row._mapping) for row in rows if plan_service.sees(access, row.label, sink)]
    return DigestList(digests=visible[offset : offset + limit], total=len(visible), limit=limit, offset=offset)


async def shown(
    conn: AsyncConnection, user: Principal, project: str, session_id: str, sink: str | None
) -> DigestRecord:
    """The digest of session ``session_id``, when the caller may read it through ``sink``; 404 otherwise."""
    access = await project_access(conn, user, project)
    plan_service.reader(access)
    row = await repository.one(conn, access.project_id, session_id)
    if row is None or not plan_service.sees(access, row.label, sink):
        raise NotFound(not_found(project, session_id))
    found = dict(row._mapping)
    body = SessionDigest.model_validate(found.pop("body"))
    return DigestRecord(**found, digest=body)


# Retention


async def prune_digests(engine: AsyncEngine, days: int = DIGEST_DAYS) -> dict:
    """Delete the digests not pushed again for ``days`` days."""
    async with engine.begin() as conn:
        deleted = await repository.pruned(conn, days)
    log.info("session digests pruned", extra={"deleted": deleted, "days": days})
    return {"deleted": deleted, "days": days}

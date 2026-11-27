"""The audit trail and every user's tokens (``evo_agents.hub.server.admin_console``): the cursors of their pages, a
page of each, and revoking any token, its worker with it."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server import audit, run_access
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.hub_admin.models.admin_console import MAX_ID, AdminToken, AuditPage, AuditRow, TokenPage
from evo_agents.hub.server.hub_admin.repository import admin_console as repository
from evo_agents.hub.server.workers.repository.workers import lock_worker_of_token
from evo_agents.hub.server.workers.service import workers


def encode_cursor(at: datetime, row_id: int) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).rstrip(b"=").decode()


def decode_cursor(value: str) -> tuple[datetime, int]:
    """The sort key a cursor holds; 422 for anything ``encode_cursor`` did not write."""
    try:
        text = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        at_text, _, id_text = text.partition("|")
        at, row_id = datetime.fromisoformat(at_text), int(id_text)
    except (ValueError, UnicodeError, binascii.Error):
        at, row_id = None, 0
    if at is None or at.tzinfo is None or not 1 <= row_id <= MAX_ID:
        raise HTTPException(422, "cursor is not one this hub handed out: start again without it")
    return at, row_id


def check_range(since: datetime | None, until: datetime | None) -> None:
    if since is not None and until is not None and since >= until:
        raise HTTPException(422, "since must be earlier than until")


async def audit_page(engine: AsyncEngine, actor, action, project, since, until, cursor: str | None, limit: int):
    """The audit trail, newest first, filtered and paged by cursor."""
    check_range(since, until)
    after = decode_cursor(cursor) if cursor else None
    async with UnitOfWork(engine) as conn:
        rows = await repository.trail_rows(conn, actor, action, project, since, until, after, limit + 1)
    items = [AuditRow(**row._mapping) for row in rows[:limit]]
    more = len(rows) > limit
    return AuditPage(items=items, next_cursor=encode_cursor(items[-1].at, items[-1].id) if more else None)


async def audit_actions(engine: AsyncEngine) -> list[str]:
    """Every action the trail holds at least one row of, in alphabetical order."""
    async with UnitOfWork(engine) as conn:
        return await repository.actions(conn)


async def token_page(engine: AsyncEngine, user, login, kind, state: str, cursor: str | None, limit: int) -> TokenPage:
    """The tokens and web sessions of every user, newest first, paged by cursor; ``current`` marks ``user``'s."""
    after = decode_cursor(cursor) if cursor else None
    async with UnitOfWork(engine) as conn:
        rows = await repository.token_rows(conn, login, kind, state, after, limit + 1)
    items = [AdminToken(**row._mapping, current=row.id == user.token_id) for row in rows[:limit]]
    more = len(rows) > limit
    return TokenPage(items=items, next_cursor=encode_cursor(items[-1].created_at, items[-1].id) if more else None)


async def revoke_any_token(engine: AsyncEngine, user, token_id: int):
    """Revoke a token or web session of any user, with its audit row; a worker token's worker is revoked with it.
    The worker (id and owner) or None, and the runs it released; 404 for a token never issued, 409 for one revoked."""
    async with UnitOfWork(engine) as conn:
        worker = await lock_worker_of_token(conn, token_id)  # before the token's row
        owner = await repository.revoke_token(conn, token_id)
        if owner is None:
            row = await repository.token_revoked_at(conn, token_id)
            if row is None:
                raise HTTPException(404, f"this hub never issued token {token_id}")
            raise HTTPException(409, f"token {token_id} was revoked already, at {row.revoked_at.isoformat()}")
        target = f"{audit.token_target(token_id)} login={owner}"
        action = audit.TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        await run_access.token_revoked(conn, user, token_id)  # a run's token: the run no longer runs as its owner
        released = await workers.end_worker(conn, user, *worker, token_id) if worker else 0
    return worker, released

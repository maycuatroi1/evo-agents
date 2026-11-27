"""The caller's own tokens (``evo_agents.hub.server.tokens``): list them, and revoke one with the worker it belongs
to, in one transaction."""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server import audit, run_access
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.identity.models.tokens import TokenRow
from evo_agents.hub.server.identity.repository import tokens as repository
from evo_agents.hub.server.security import Principal, revoke_token
from evo_agents.hub.server.workers.repository.workers import lock_worker_of_token
from evo_agents.hub.server.workers.service import workers

log = logging.getLogger("evo_agents.hub.server.tokens")


async def list_tokens(engine: AsyncEngine, user: Principal, include_inactive: bool) -> list[TokenRow]:
    async with UnitOfWork(engine) as conn:
        rows = await repository.listed(conn, user.user_id, include_inactive)
    return [TokenRow(**row._mapping, current=row.id == user.token_id) for row in rows]


async def revoke(engine: AsyncEngine, state, user: Principal, token_id: int) -> None:
    """Revoke token ``token_id`` of ``user``, and the worker it is the token of; 404 for no such unrevoked token.
    ``state`` is the app's state, whose leases of the worker end too."""
    async with UnitOfWork(engine) as conn:
        worker = await lock_worker_of_token(conn, token_id, user.user_id)  # before the token's row
        if not await revoke_token(conn, token_id, user.user_id):
            raise HTTPException(404, f"you have no unrevoked token {token_id}: see `evo-agents hub token list`")
        target = audit.token_target(token_id)
        action = audit.TOKEN_REVOKE
        await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target)
        await run_access.token_revoked(conn, user, token_id)  # a run's token: the run no longer runs as you
        released = await workers.end_worker(conn, user, *worker, token_id) if worker else 0
    if worker:
        log.info("worker revoked", extra={"worker_id": worker[0], "by": user.login, "runs_released": released})
        await workers.revoke_leased_tokens(state, worker[0])

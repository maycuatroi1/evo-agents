"""What the services of the seller share: an amount in USD as the API shows it, and the audit row of an action on
money or a licence."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import payments
from evo_agents.hub.server import audit
from evo_agents.hub.server.security import Principal


def usd(value) -> float:
    return float(payments.usd(value))


async def record(conn: AsyncConnection, user: Principal | None, action: str, target: str) -> None:
    """The audit row of ``action`` on ``target`` by ``user``, or by the hub itself when None."""
    await audit.record(
        conn,
        actor_id=None if user is None else user.user_id,
        token_id=None if user is None else user.token_id,
        action=action,
        target=target,
    )

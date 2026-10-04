"""The audit trail: one row per command that changes something, saying who did it (the user and the token they
used), the action, its target and when. A target names things, such as a project, a login, a host or a token id;
it never holds content or a credential. Rows are only ever inserted (schema 0001 rejects updates), in the same
transaction as the change they describe, so a change that rolls back leaves no row.
"""

from __future__ import annotations

LOGIN = "auth.login"
LOGOUT = "auth.logout"
TOKEN_REVOKE = "token.revoke"
GRANT_PUT = "grant.put"
GRANT_DELETE = "grant.delete"
PROJECT_REGISTER = "project.register"
PROJECT_UPDATE = "project.update"


async def record(conn, *, actor_id: int, token_id: int | None, action: str, target: str) -> None:
    await conn.execute(
        "INSERT INTO audit (actor_id, token_id, action, target) VALUES (%s, %s, %s, %s)",
        (actor_id, token_id, action, target),
    )


def token_target(token_id: int) -> str:
    return f"token:{token_id}"

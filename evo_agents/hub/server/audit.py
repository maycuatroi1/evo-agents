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
BLOB_COMMIT = "blob.commit"  # the target is the project, never the hashes
KG_CONFIG = "kg.config"  # the knowledge config a project's graph is built with changed; the target is the project
KG_INGEST = "kg.ingest"  # a pushed run became part of the project's corpus; the target is the project, never the run
KG_BUILD = "kg.build"  # a member queued a build; the target is the project


async def record(conn, *, actor_id: int, token_id: int | None, action: str, target: str) -> None:
    await conn.execute(
        "INSERT INTO audit (actor_id, token_id, action, target) VALUES (%s, %s, %s, %s)",
        (actor_id, token_id, action, target),
    )


def token_target(token_id: int) -> str:
    return f"token:{token_id}"

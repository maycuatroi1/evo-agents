"""Give an agent an account pool by rows, for the tests whose agents run in pool mode and need an account to be
dispatched and claimed, on a hub with no sealing key (the account's value is never opened there)."""

from __future__ import annotations

from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from tests.hub.live import sql

TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def give_account_pool(
    db, project: str, handle: str, login: str, *, pool: str = "main", env_var: str = TOKEN_VAR
) -> int:
    """Member ``login`` keeps an env secret of ``env_var``, which goes in account pool ``pool`` of ``project`` (made
    here), attached to agent ``handle``. The id of the secret."""
    u, p, ap = tables.users, tables.projects, tables.agent_profiles
    user_id = sql(db, select(u.c.id).where(u.c.login == login))[0][0]
    project_id = sql(db, select(p.c.id).where(p.c.name == project))[0][0]
    secret_id = sql(
        db,
        insert(tables.secrets)
        .values(
            owner_id=user_id,
            name=f"token-{handle}",
            kind="env",
            env_var=env_var,
            sealed=b"x" * 32,
            nonce=b"n" * 12,
            key_id="0123abcd",
        )
        .returning(tables.secrets.c.id),
    )[0][0]
    pool_id = sql(
        db,
        insert(tables.account_pools)
        .values(project_id=project_id, name=f"{pool}-{handle}", created_by=user_id)
        .returning(tables.account_pools.c.id),
    )[0][0]
    sql(db, insert(tables.account_pool_members).values(pool_id=pool_id, secret_id=secret_id, added_by=user_id))
    sql(db, update(ap).where(ap.c.project_id == project_id, ap.c.handle == handle).values(account_pool_id=pool_id))
    return secret_id

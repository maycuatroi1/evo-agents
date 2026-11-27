"""What the live view of a run's browser reads: whether the viewer's web session is fresh, the run viewed with who
dispatched it, whether it waits on a login, and the worker of a token with the run it would attach to."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.browser.models import view as models


async def fresh(conn: AsyncConnection, token_id: int, max_age: timedelta) -> bool:
    """Whether the web session of token ``token_id`` was created within ``max_age`` (the query of the terminal's
    ``fresh_session``, which this layer may not import)."""
    tokens = tables.tokens
    query = select(tokens.c.created_at > func.now() - max_age).where(tokens.c.id == token_id)
    return bool((await conn.execute(query)).scalar_one_or_none())


async def viewed_run(conn: AsyncConnection, run_id: int, project_id: int):
    """Run ``run_id`` of the project, with the login of who dispatched it."""
    runs_, users = tables.runs, tables.users
    query = (
        select(
            runs_.c.dispatched_by,
            users.c.login.label("owner"),
            runs_.c.state,
            runs_.c.browser,
            runs_.c.worker_id,
            runs_.c.plan_id,
            runs_.c.step_key,
        )
        .select_from(runs_.join(users, users.c.id == runs_.c.dispatched_by))
        .where(runs_.c.id == run_id, runs_.c.project_id == project_id)
    )
    return (await conn.execute(query)).one()


async def login_open(conn: AsyncConnection, run_id: int) -> bool:
    """Whether run ``run_id`` has an open decision of LOGIN_CATEGORY."""
    decisions = tables.decisions
    query = select(
        exists().where(
            decisions.c.run_id == run_id, decisions.c.state == "open", decisions.c.category == models.LOGIN_CATEGORY
        )
    )
    return bool((await conn.execute(query)).scalar())


async def worker_and_run(conn: AsyncConnection, token_id: int, run_id: int):
    """The worker of token ``token_id`` (None for none) and the worker that holds run ``run_id`` with the run's state
    and whether it has a browser (None for no such run)."""
    runs_ = tables.runs
    workers = tables.workers
    holding = select(workers.c.id, workers.c.name, workers.c.revoked_at).where(workers.c.token_id == token_id)
    worker = (await conn.execute(holding)).one_or_none()
    held = select(runs_.c.worker_id, runs_.c.state, runs_.c.browser).where(runs_.c.id == run_id)
    return worker, (await conn.execute(held)).one_or_none()

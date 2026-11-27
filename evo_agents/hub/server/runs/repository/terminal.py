"""The reads of the web terminal (``evo_agents.hub.server.terminal``), on SQLAlchemy Core: the age of a web session,
the run a browser opens, locked, and the worker of a token with the run it holds."""

from __future__ import annotations

from sqlalchemy import Row, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.runs.models.terminal import SESSION_MAX_AGE


def fresh_session(token_id: int):
    """Whether the web session of token ``token_id`` was created within SESSION_MAX_AGE; no row without the token."""
    tokens = tables.tokens
    return select(tokens.c.created_at > func.now() - SESSION_MAX_AGE).where(tokens.c.id == token_id)


def _terminal_run(run_id: int, project_id: int):
    """Run ``run_id`` of the project, locked, with its owner and the worker that holds it."""
    runs_, users, workers = tables.runs, tables.users, tables.workers
    return (
        select(
            runs_.c.dispatched_by,
            users.c.login.label("owner"),
            runs_.c.state,
            runs_.c.plan_id,
            runs_.c.step_key,
            runs_.c.worker_id,
            workers.c.owner_id.label("worker_owner"),
            workers.c.name.label("worker"),
            workers.c.allow_web_terminal,
        )
        .select_from(
            runs_.join(users, users.c.id == runs_.c.dispatched_by).outerjoin(workers, workers.c.id == runs_.c.worker_id)
        )
        .where(runs_.c.id == run_id, runs_.c.project_id == project_id)
        .with_for_update(of=runs_)
    )


def holding_worker(token_id: int):
    """The worker of token ``token_id``, revoked or not."""
    workers = tables.workers
    return select(workers.c.id, workers.c.name, workers.c.revoked_at).where(workers.c.token_id == token_id)


def _held_run(run_id: int):
    """The worker that holds run ``run_id``, and its state."""
    runs_ = tables.runs
    return select(runs_.c.worker_id, runs_.c.state).where(runs_.c.id == run_id)


async def is_fresh(conn: AsyncConnection, token_id: int) -> bool | None:
    """``fresh_session`` of token ``token_id``: None without the token."""
    return (await conn.execute(fresh_session(token_id))).scalar_one_or_none()


async def terminal_run(conn: AsyncConnection, run_id: int, project_id: int) -> Row:
    """Run ``run_id`` of the project, locked, with its owner and the worker that holds it."""
    return (await conn.execute(_terminal_run(run_id, project_id))).one()


async def worker_of_token(conn: AsyncConnection, token_id: int) -> Row | None:
    """The worker of token ``token_id`` (id, name, revoked_at), revoked or not."""
    return (await conn.execute(holding_worker(token_id))).one_or_none()


async def held_run(conn: AsyncConnection, run_id: int) -> Row | None:
    """The worker that holds run ``run_id`` and its state (worker_id, state)."""
    return (await conn.execute(_held_run(run_id))).one_or_none()

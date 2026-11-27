"""The checks and audits of the web terminal (``evo_agents.hub.server.terminal`` is the relay and its protocol): open
a browser's session on a run, audit a session that closed, and read what a worker's end needs checked, each in one
``UnitOfWork``."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import runs
from evo_agents.hub import terminal as frames
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.runs.models.terminal import OPEN_STATES, SESSION_MAX_AGE, Refusal, TerminalSession
from evo_agents.hub.server.runs.repository import terminal as repository
from evo_agents.hub.server.runs.service.audits import run_target
from evo_agents.hub.server.runs.service.controls import ask_takeover
from evo_agents.hub.server.runs.service.views import readable_run
from evo_agents.hub.server.security import Principal


async def open_session(engine: AsyncEngine, session: TerminalSession, project: str) -> None:
    """Check the run and the session's age, reserve the run's terminal for ``session`` and audit it, in one
    transaction: the reservation stands only once it committed (the caller drops it when this raises)."""
    async with UnitOfWork(engine) as conn:
        await _open_session(conn, session, project)


def _check_run(run, user: Principal, run_id: int) -> None:
    """Refusal unless ``user`` dispatched run ``run_id``, which is open on a worker of theirs that allows the web
    terminal."""
    if run.dispatched_by != user.user_id:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"only {run.owner}, who dispatched run {run_id}, may open its terminal")
    if run.state not in OPEN_STATES or run.worker_id is None:
        raise Refusal(
            frames.CLOSE_FORBIDDEN,
            f"run {run_id} is {run.state}: a terminal opens on a run that is leased, running or interactive",
        )
    if run.worker_owner != user.user_id:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"run {run_id} is on worker {run.worker}, which is not yours")
    if not run.allow_web_terminal:
        raise Refusal(frames.CLOSE_FORBIDDEN, f"worker {run.worker} does not allow the web terminal")


async def _open_session(conn: AsyncConnection, session: TerminalSession, project: str) -> None:
    user, run_id = session.user, session.run_id
    if not await repository.is_fresh(conn, user.token_id):
        hours = int(SESSION_MAX_AGE.total_seconds() // 3600)
        raise Refusal(frames.CLOSE_FORBIDDEN, f"the web session is older than {hours} hours: sign in again")
    try:
        access = await readable_run(conn, user, project, run_id, None)
    except HTTPException as exc:
        raise Refusal(frames.CLOSE_FORBIDDEN, str(exc.detail)) from None
    run = await repository.terminal_run(conn, run_id, access.project_id)
    _check_run(run, user, run_id)
    session.worker_id = run.worker_id
    if not session.terminals.reserve(session):
        raise Refusal(frames.CLOSE_BUSY, f"the terminal of run {run_id} is open in another browser")
    target = run_target(project, run.plan_id, run.step_key, run_id)
    if run.state in runs.TAKEOVER_STATES and await ask_takeover(conn, run_id):
        await _audit(conn, user, access.project_id, audit.RUN_TAKEOVER, target)
    await _audit(conn, user, access.project_id, audit.TERMINAL_OPEN, target)
    session.project_id = access.project_id
    session.target = target


async def _audit(conn: AsyncConnection, user: Principal, project_id: int, action: str, target: str) -> None:
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=action, target=target, project_id=project_id
    )


async def record_close(engine: AsyncEngine, user: Principal, project_id: int, target: str) -> None:
    """Audit a terminal session that closed (terminal.close), ``target`` naming the run and how it ended."""
    async with UnitOfWork(engine) as conn:
        await _audit(conn, user, project_id, audit.TERMINAL_CLOSE, target)


async def worker_and_run(engine: AsyncEngine, token_id: int, run_id: int) -> tuple[Row | None, Row | None]:
    """The worker of token ``token_id`` (id, name, revoked_at) and who holds run ``run_id`` (worker_id, state)."""
    async with UnitOfWork(engine) as conn:
        return await repository.worker_of_token(conn, token_id), await repository.held_run(conn, run_id)

"""The agent of a run with a browser asks a person to sign in (plan agent-browser, criterion a8):
POST /v1/worker/runs/{run}/browser-login, which ``evo-agents worker browser login`` calls.

For a run that waits for its owner (RESUME_KINDS) it stores a decision of category ``login`` with the options done and
skip, whose context carries the reason, the page and the link to the live view of the run's browser, and notifies the
dispatcher in the Inbox and on Telegram as any decision does. The worker's daemon moves the run to waiting when the
agent's turn ends, keeping its slot and its browser; the answer reaches the agent through its inbox, and a Done also
makes the daemon save the browser's profile to the identity before the agent goes on.

A run that cannot wait (a step run) gets no decision: the identity is set needs_login, its owner is told, and the run
fails with the reason, so the person logs the identity in and dispatches it again.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs, tables
from evo_agents.hub.server.browser_leases import REFUSALS, set_needs_login, usable_identity
from evo_agents.hub.server.decisions import DecisionAsk, OptionIn, store_decision
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.notifications import one_line
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

MAX_REASON_CHARS = 1000
worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


class LoginAsk(DecisionAsk):
    """The decision a login asks: DecisionAsk, in the category the agent cannot name with `worker ask`."""

    category: Literal["login"]  # browser_login.CATEGORY


class BrowserLoginBody(BaseModel):
    identity: str = Field(
        min_length=1, max_length=200, description="the id or the name of one of the run's browser identities"
    )
    url: str = Field(min_length=1, max_length=browser_login.MAX_URL_CHARS, description="the page to sign in on")
    reason: str = Field(min_length=1, max_length=MAX_REASON_CHARS, description="why the agent needs the login")


class BrowserLoginAsked(BaseModel):
    outcome: Annotated[Literal["asked", "failed"], Field(description="asked: a decision is open; failed: the run ends")]
    decision_id: int | None = Field(description="the login decision, for outcome asked")
    identity_id: int = Field(description="the browser identity the login is for")
    message: str = Field(description="what the agent is told")


async def _identity_id(conn: AsyncConnection, run_id: int, wanted: str) -> int:
    """The id of the browser identity of run ``run_id`` that ``wanted`` names (an id, or a name); 422 listing the
    run's identities when it names none of them."""
    r, b = tables.runs, tables.browser_identities
    held = (await conn.execute(select(r.c.browser_identities).where(r.c.id == run_id))).scalar_one() or []
    rows = (await conn.execute(select(b.c.id, b.c.name).where(b.c.id.in_(held)).order_by(b.c.id))).all()
    for row in rows:
        if wanted.strip() in (str(row.id), row.name):
            return row.id
    names = ", ".join(f"{row.name} (#{row.id})" for row in rows) or "none"
    raise HTTPException(422, f"run {run_id} has no browser identity {wanted!r}: it was dispatched with {names}")


def _ask(name: str, run_id: int, project: str, body: BrowserLoginBody) -> LoginAsk:
    options = [OptionIn(key=key, label=label, description=text) for key, label, text in browser_login.LOGIN_OPTIONS]
    return LoginAsk(
        category=browser_login.CATEGORY,
        question=browser_login.LOGIN_QUESTION.format(name=one_line(name, 80), run=run_id),
        context=browser_login.login_context(name, body.url, body.reason, project, run_id),
        options=options,
        recommended=browser_login.LOGIN_DONE,
    )


@worker_router.post(
    "/runs/{run_id}/browser-login",
    status_code=201,
    response_model=BrowserLoginAsked,
    responses={**REFUSALS, 422: {"model": ErrorBody}},
)
async def ask_login(request: Request, run_id: RunId, body: BrowserLoginBody, user: CurrentUser) -> BrowserLoginAsked:
    """Ask the dispatcher of a run with a browser to sign in on one of its identities, or, for a run that cannot wait,
    set the identity needs_login and fail the run."""
    if not browser_login.valid_url(body.url):
        raise HTTPException(422, "the login page is an http or https address with a host")
    async with request.app.state.engine.begin() as conn:
        _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RUN_KINDS)
        if row.kind == browser_login.KIND:
            raise HTTPException(422, f"run {run_id} is a login session: a person is logging in already")
        found = await usable_identity(conn, user, run_id, await _identity_id(conn, run_id, body.identity))
        name = found.identity.name
        if row.kind not in runs.RESUME_KINDS:
            await set_needs_login(conn, user, found, "asked_by_run")
            message = browser_login.STEP_REFUSAL.format(run=run_id, kind=row.kind, name=name, project=row.name)
            await move_run(conn, run_id, row.state, "failed", "worker", reason="browser login needed", error=message)
            log.info("run failed for a login", extra={"run_id": run_id, "browser_identity": found.identity.id})
            return BrowserLoginAsked(outcome="failed", decision_id=None, identity_id=found.identity.id, message=message)
        d = tables.decisions
        open_one = select(d.c.id).where(
            d.c.run_id == run_id, d.c.category == browser_login.CATEGORY, d.c.state == "open"
        )
        existing = (await conn.execute(open_one)).scalars().first()
        if existing is not None:
            return BrowserLoginAsked(
                outcome="asked",
                decision_id=existing,
                identity_id=found.identity.id,
                message=f"login decision #{existing} is open already",
            )
        view = await store_decision(conn, run_id, row, _ask(name, run_id, row.name, body))
    log.info("login asked", extra={"run_id": run_id, "decision_id": view.id})
    return BrowserLoginAsked(
        outcome="asked",
        decision_id=view.id,
        identity_id=found.identity.id,
        message=f"login decision #{view.id} is open",
    )

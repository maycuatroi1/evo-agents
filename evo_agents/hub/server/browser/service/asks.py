"""What a login a run's agent asks for does: a decision of category ``login`` for a run that waits for its owner, or,
for a run that cannot wait, the identity set needs_login and the run failed (see ``routes.asks``)."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import browser_login, runs
from evo_agents.hub.server.base.errors import Unprocessable
from evo_agents.hub.server.browser.models.asks import BrowserLoginAsked, BrowserLoginBody, LoginAsk
from evo_agents.hub.server.browser.repository import asks as repository
from evo_agents.hub.server.browser.service.leases import set_needs_login, usable_identity
from evo_agents.hub.server.decisions.models import OptionIn
from evo_agents.hub.server.decisions.service.asks import store_decision
from evo_agents.hub.server.notifications.service.notifier import NOTIFIER
from evo_agents.hub.server.run_state import move_run
from evo_agents.hub.server.runs.service.plan_runs import held_plan_run
from evo_agents.hub.server.security import Principal

log = logging.getLogger(__name__)


async def _identity_id(conn: AsyncConnection, run_id: int, wanted: str) -> int:
    """The id of the browser identity of run ``run_id`` that ``wanted`` names (an id, or a name); 422 listing the
    run's identities when it names none of them."""
    rows = await repository.run_identities(conn, run_id)
    for row in rows:
        if wanted.strip() in (str(row.id), row.name):
            return row.id
    names = ", ".join(f"{row.name} (#{row.id})" for row in rows) or "none"
    raise Unprocessable(f"run {run_id} has no browser identity {wanted!r}: it was dispatched with {names}")


def _ask(name: str, run_id: int, project: str, body: BrowserLoginBody) -> LoginAsk:
    options = [OptionIn(key=key, label=label, description=text) for key, label, text in browser_login.LOGIN_OPTIONS]
    return LoginAsk(
        category=browser_login.CATEGORY,
        question=browser_login.LOGIN_QUESTION.format(name=NOTIFIER.one_line(name, 80), run=run_id),
        context=browser_login.login_context(name, body.url, body.reason, project, run_id),
        options=options,
        recommended=browser_login.LOGIN_DONE,
        recommended_reason=browser_login.LOGIN_REASON,
    )


def check_url(body: BrowserLoginBody) -> None:
    """422 for a login page that is no address."""
    if not browser_login.valid_url(body.url):
        raise Unprocessable("the login page is an http or https address with a host")


async def ask_login(conn: AsyncConnection, user: Principal, run_id: int, body: BrowserLoginBody) -> BrowserLoginAsked:
    """Ask the dispatcher of the run to sign in on one of its identities, or, for a run that cannot wait, set the
    identity needs_login and fail the run."""
    _, row = await held_plan_run(conn, user, run_id, lock=True, kinds=runs.RUN_KINDS)
    if row.kind == browser_login.KIND:
        raise Unprocessable(f"run {run_id} is a login session: a person is logging in already")
    found = await usable_identity(conn, user, run_id, await _identity_id(conn, run_id, body.identity))
    name = found.identity.name
    if row.kind not in runs.RESUME_KINDS:
        await set_needs_login(conn, user, found, "asked_by_run")
        message = browser_login.STEP_REFUSAL.format(run=run_id, kind=row.kind, name=name, project=row.name)
        await move_run(conn, run_id, row.state, "failed", "worker", reason="browser login needed", error=message)
        log.info("run failed for a login", extra={"run_id": run_id, "browser_identity": found.identity.id})
        return BrowserLoginAsked(outcome="failed", decision_id=None, identity_id=found.identity.id, message=message)
    existing = await repository.open_decision(conn, run_id, browser_login.CATEGORY)
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

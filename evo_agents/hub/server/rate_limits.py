"""What a worker says when the agent of a run it holds hits its account's usage limit, and what the hub answers:
POST /v1/worker/runs/{run_id}/rate-limit (``docs/credentials.md``, section "Accounts").

The worker sends what the agent's CLI said of the account behind one of the run's leases (``lease_id``, an env
secret's lease the run holds): ``status`` (``allowed``, ``allowed_warning`` or ``rejected``), the limit's
``rate_limit_type``, the time it resets (``resets_at``) when the CLI gave one, its ``utilization`` and the usage of
each window. The hub keeps that usage on the account (accounts.last_usage) window by window: the report's windows
replace those of the same name, the windows it does not list stay as the last report of them gave them, and the rest
(status, rate_limit_type, resets_at, utilization) is the newest report's. A window whose reset has passed stays too,
until a report lists it again; the web shows it as reset. An ``allowed`` or ``allowed_warning`` that comes while the
account cools down from a ``rejected`` one changes nothing: that report is older than the limit, so the limit stays
the account's last usage. With ``allowed`` and ``allowed_warning`` that is all, and the answer's action is ``none``.

With ``rejected`` the account cools down until ``resets_at``, else for the owner's ``cooldown_seconds``
(``account_choice.fail_over``). ``unauthorized`` says the account failed to authenticate (the CLI's token was refused,
a 401): no usage is kept, and the account cools down for the owner's ``cooldown_seconds``, its reason saying to log in
again (there is no state "needs login"; a token renewed with ``evo-agents hub account login`` is taken once that
cooldown ends). When the run may fail over (failover on in its owner's policy, the account not picked by hand, fewer
than ``max_failovers`` moves so far, another account ready), the run's run_accounts row moves to the next account by
the rules, the old lease is revoked, the answer's action is ``failover``, and the worker asks for its leases again:
they carry the new account's value. Else the action is ``stop`` with the reason and the earliest time an account is
ready again, and the worker ends the run failed with cause ``rate_limit``, or ``account_auth`` for ``unauthorized``.
Either way a ``system`` event of the run says so, naming each account by its secret's id, as the audit row
``account.failover`` does: never a name or a value. A worker of an older release never calls the route, and its runs
run as before.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, not_, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.account_choice import Failover, fail_over
from evo_agents.hub.server.credentials import survey_run
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}
MAX_WINDOWS = 16  # windows of usage one report carries
LIMITS = ("rejected", "unauthorized")  # the statuses that cool the account down and may move the run
Name = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class UsageWindow(BaseModel):
    utilization: float | None = Field(default=None, ge=0, le=100)
    resets_at: datetime | None = None


class RateLimitReport(BaseModel):
    lease_id: int = Field(ge=1, description="the lease of the env secret the agent ran with, one the run holds")
    status: Literal["allowed", "allowed_warning", "rejected", "unauthorized"] = Field(
        description="what the CLI said: rejected when the account hit its usage limit, unauthorized when it failed to "
        "authenticate (its token refused), else its usage alone"
    )
    rate_limit_type: Name | None = Field(default=None, description="the limit, such as five_hour or seven_day")
    resets_at: datetime | None = Field(default=None, description="when the limit resets, when the CLI said")
    utilization: float | None = Field(default=None, ge=0, le=100, description="the share of the limit used")
    windows: dict[Name, UsageWindow] | None = Field(
        default=None, max_length=MAX_WINDOWS, description="the usage of each window the CLI listed"
    )


class RateLimitAnswer(BaseModel):
    action: Literal["none", "failover", "stop"] = Field(
        description="none: usage kept, go on; failover: ask for the run's leases again, the new account's value is "
        "in them, and go on in the same session; stop: end the run failed with cause rate_limit, or account_auth "
        "for unauthorized"
    )
    secret_id: int = Field(description="the account the run has now, by its secret's id")
    failovers: int = Field(description="the times the run moved to another account")
    cooldown_until: datetime | None = Field(
        default=None, description="with rejected or unauthorized: until when the account cools"
    )
    ready_at: datetime | None = Field(default=None, description="with stop: when an account is ready first")
    reason: str | None = Field(default=None, description="with stop: why the run cannot move")


def _usage(report: RateLimitReport) -> dict:
    return report.model_dump(mode="json", exclude={"lease_id"}, exclude_none=True)


async def _lease(conn: AsyncConnection, run_id: int, worker_id: int, lease_id: int):
    cl = tables.credential_leases
    row = (
        await conn.execute(
            select(cl.c.secret_id, cl.c.target, cl.c.revoked_at).where(
                cl.c.id == lease_id,
                cl.c.run_id == run_id,
                cl.c.worker_id == worker_id,
                cl.c.provider == "secret",
                cl.c.secret_id.is_not(None),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, f"run {run_id} holds no lease {lease_id} of a secret on this worker")
    return row


def _object(value):
    """``value`` when it is a JSON object, else an empty one."""
    return case((func.jsonb_typeof(value) == "object", value), else_=func.jsonb_build_object(type_=JSONB))


def _merged(kept, new):
    """The usage ``new`` with the windows of ``kept`` it does not list: a window is replaced by name, and the usage
    holds no ``windows`` when neither lists one."""
    windows = _object(kept["windows"]).op("||", return_type=JSONB)(_object(new["windows"]))
    listed = func.jsonb_build_object("windows", func.nullif(windows, func.jsonb_build_object(type_=JSONB)), type_=JSONB)
    return func.jsonb_strip_nulls(new.op("||", return_type=JSONB)(listed), type_=JSONB)


async def _keep_usage(conn: AsyncConnection, secret_id: int, usage: dict) -> None:
    """Keep ``usage`` as the account's last, window by window as the module says; one not rejected leaves a rejected
    one of a cooling account alone."""
    a = tables.accounts
    statement = insert(a).values(secret_id=secret_id, last_usage=usage, last_usage_at=func.now())
    values = {"last_usage": _merged(a.c.last_usage, statement.excluded.last_usage), "last_usage_at": func.now()}
    where = None
    if usage.get("status") != "rejected":
        limited = and_(a.c.cooldown_until > func.now(), a.c.last_usage["status"].astext == "rejected")
        where = not_(func.coalesce(limited, False))
    await conn.execute(statement.on_conflict_do_update(index_elements=[a.c.secret_id], set_=values, where=where))


def _note(env_var: str, current: int, moved: Failover, status: str) -> str:
    what = "failed to authenticate" if status == "unauthorized" else "hit its usage limit"
    limited = f"account secret {current} for {env_var} {what}; it cools down until {moved.cooling_until.isoformat()}"
    if moved.secret is not None:
        return f"{limited}: the run goes on with secret {moved.secret.id} (failover {moved.failovers})"
    ready = moved.ready_at.isoformat() if moved.ready_at else "unknown"
    return f"{limited}: the run stops, {moved.reason}; an account is ready again at {ready}"


def _event(env_var: str, current: int, moved: Failover, status: str) -> dict:
    """The run's ``system`` event of a usage limit or a failed authentication: the note, and what the run page reads
    of it (``account_views``): ``account_event`` failover with the account moved to, or stop; accounts by secret id
    alone."""
    body = {"text": _note(env_var, current, moved, status), "env_var": env_var, "secret_id": current}
    body["cooldown_until"] = moved.cooling_until.isoformat()
    if moved.secret is None:
        return {**body, "account_event": "stop"}
    return {**body, "account_event": "failover", "to_secret_id": moved.secret.id, "failovers": moved.failovers}


def _cooldown(report: RateLimitReport, run_id: int) -> tuple[datetime | None, str]:
    """(the reset the cooldown lasts until, else None for the policy's cooldown_seconds; its reason) of a report that
    cools its account down."""
    if report.status == "unauthorized":
        return None, f"authentication failed, reported by run {run_id}: log in again with evo-agents hub account login"
    return report.resets_at, f"usage limit {report.rate_limit_type or 'reached'}, reported by run {run_id}"


async def _revoke(conn: AsyncConnection, lease_id: int) -> None:
    cl = tables.credential_leases
    await conn.execute(update(cl).where(cl.c.id == lease_id, cl.c.revoked_at.is_(None)).values(revoked_at=func.now()))


@worker_router.post("/runs/{run_id}/rate-limit", response_model=RateLimitAnswer, responses=REFUSALS)
async def report_rate_limit(
    request: Request, run_id: RunId, report: RateLimitReport, user: CurrentUser
) -> RateLimitAnswer:
    """Keep the usage the CLI gave for the account of one of the run's leases; on a usage limit or a failed
    authentication, cool the account down and move the run to the next one when its owner's policy lets it, as the
    module says."""
    async with request.app.state.engine.begin() as conn:
        worker_id, survey = await survey_run(conn, user, run_id, lock=True)
        lease = await _lease(conn, run_id, worker_id, report.lease_id)
        current, env_var = lease.secret_id, lease.target
        if report.status != "unauthorized":
            await _keep_usage(conn, current, _usage(report))
        if report.status not in LIMITS:
            return RateLimitAnswer(action="none", secret_id=current, failovers=0)
        limit = _cooldown(report, run_id)
        moved = await fail_over(conn, survey.run, survey.secrets, env_var, current, limit)
        await write_event(conn, run_id, _event(env_var, current, moved, report.status))
        if moved.secret is None:
            why = {"run_id": run_id, "secret_id": current, "status": report.status, "why": moved.reason}
            log.info("account limited, no failover", extra=why)
            return RateLimitAnswer(
                action="stop",
                secret_id=current,
                failovers=moved.failovers,
                cooldown_until=moved.cooling_until,
                ready_at=moved.ready_at,
                reason=moved.reason,
            )
        await _revoke(conn, report.lease_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.ACCOUNT_FAILOVER,
            target=f"{survey.run.target} env={env_var} from=secret:{current} to=secret:{moved.secret.id}",
            project_id=survey.run.project_id,
        )
    moves = {"run_id": run_id, "status": report.status, "from": current, "to": moved.secret.id}
    log.info("account limited, failed over", extra=moves)
    return RateLimitAnswer(
        action="failover", secret_id=moved.secret.id, failovers=moved.failovers, cooldown_until=moved.cooling_until
    )

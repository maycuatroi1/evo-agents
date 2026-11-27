"""A worker's report of the usage of a run's account, and the failover of a run whose account hit its limit
(``evo_agents.hub.server.rate_limits``), in one transaction."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub.server import audit
from evo_agents.hub.server.accounts.models.choice import Failover
from evo_agents.hub.server.accounts.service.choice import fail_over
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.credentials.service.survey import survey_run
from evo_agents.hub.server.run_state import write_event
from evo_agents.hub.server.runs.models.rate_limits import LIMITS, RateLimitAnswer, RateLimitReport
from evo_agents.hub.server.runs.repository import rate_limits as repository
from evo_agents.hub.server.security import Principal

log = logging.getLogger("evo_agents.hub.server.rate_limits")


def _usage(report: RateLimitReport) -> dict:
    return report.model_dump(mode="json", exclude={"lease_id"}, exclude_none=True)


def _note(env_var: str, current: int, moved: Failover, status: str) -> str:
    what = "failed to authenticate" if status == "unauthorized" else "hit its usage limit"
    limited = f"account secret {current} for {env_var} {what}; it cools down until {moved.cooling_until.isoformat()}"
    if moved.secret is not None:
        return f"{limited}: the run goes on with secret {moved.secret.id} (failover {moved.failovers})"
    ready = moved.ready_at.isoformat() if moved.ready_at else "unknown"
    return f"{limited}: the run stops, {moved.reason}; an account is ready again at {ready}"


def _event(env_var: str, current: int, moved: Failover, status: str) -> dict:
    """The run's ``system`` event of a usage limit or a failed authentication: the note, and what the run page reads
    of it (``accounts.service.views``): ``account_event`` failover with the account moved to, or stop; accounts by secret id
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


async def report_rate_limit(
    engine: AsyncEngine, user: Principal, run_id: int, report: RateLimitReport
) -> RateLimitAnswer:
    """Keep the usage the CLI gave for the account of one of the run's leases; on a usage limit or a failed
    authentication, cool the account down and move the run to the next one when its owner's policy lets it, as
    ``rate_limits`` says."""
    async with UnitOfWork(engine) as conn:
        worker_id, survey = await survey_run(conn, user, run_id, lock=True)
        lease = await repository.lease(conn, run_id, worker_id, report.lease_id)
        if lease is None:
            raise HTTPException(404, f"run {run_id} holds no lease {report.lease_id} of a secret on this worker")
        current, env_var = lease.secret_id, lease.target
        if report.status != "unauthorized":
            await repository.keep_usage(conn, current, _usage(report))
        if report.status not in LIMITS:
            return RateLimitAnswer(action="none", secret_id=current, failovers=0)
        limit = _cooldown(report, run_id)
        moved = await fail_over(conn, survey.run, survey.secrets, env_var, current, limit)  # pyright: ignore[reportArgumentType]
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
        await repository.revoke(conn, report.lease_id)
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

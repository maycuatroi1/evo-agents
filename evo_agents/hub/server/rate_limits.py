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
(``accounts.service.choice.fail_over``). ``unauthorized`` says the account failed to authenticate (the CLI's token was refused,
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

from fastapi import APIRouter, Request

from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.runs.models.rate_limits import MAX_WINDOWS as MAX_WINDOWS
from evo_agents.hub.server.runs.models.rate_limits import RateLimitAnswer as RateLimitAnswer
from evo_agents.hub.server.runs.models.rate_limits import RateLimitReport as RateLimitReport
from evo_agents.hub.server.runs.models.rate_limits import UsageWindow as UsageWindow
from evo_agents.hub.server.runs.service import rate_limits as service
from evo_agents.hub.server.security import CurrentUser

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})
REFUSALS = {403: {"model": ErrorBody}, 404: {"model": ErrorBody}}


@worker_router.post("/runs/{run_id}/rate-limit", response_model=RateLimitAnswer, responses=REFUSALS)
async def report_rate_limit(
    request: Request, run_id: RunId, report: RateLimitReport, user: CurrentUser
) -> RateLimitAnswer:
    """Keep the usage the CLI gave for the account of one of the run's leases; on a usage limit or a failed
    authentication, cool the account down and move the run to the next one when its owner's policy lets it, as the
    module says."""
    return await service.report_rate_limit(request.app.state.engine, user, run_id, report)

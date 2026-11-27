"""The routes GET and PATCH under /v1/accounts and GET, PUT and DELETE under /v1/account-policies, which read and write
a member's accounts and policies through ``evo_agents.hub.server.accounts.service.accounts``; that module says what
each gives."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from evo_agents.hub.server.accounts.models.accounts import (
    REFUSALS,
    Account,
    AccountChange,
    AccountName,
    AccountPolicy,
    AccountPolicyWrite,
    OwnerLogin,
    SharedAccount,
)
from evo_agents.hub.server.accounts.service import accounts as service
from evo_agents.hub.server.auth import NO_STORE
from evo_agents.hub.server.autonomy.service import guards as route_autonomy
from evo_agents.hub.server.base.models import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1", tags=["accounts"], dependencies=[route_autonomy.K8], responses={401: {"model": ErrorBody}}
)


def _seen(request: Request) -> dict | None:
    """The gateway's connections of the last round, by secret id; None when the hub runs no gateway."""
    found = getattr(request.app.state, "gateway_sync", None)
    return None if found is None else found.status


# Accounts


@router.get("/accounts", response_model=list[Account])
async def list_accounts(request: Request, user: CurrentUser, response: Response) -> list[Account]:
    """The caller's own env secrets as accounts, by variable and name, without their values, with their figures
    (``account_views``); a hub admin's own too."""
    found = await service.list_own(request.app.state.engine, user, _seen(request))
    response.headers.update(NO_STORE)
    return found


@router.get("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def get_account(
    request: Request, name: AccountName, user: CurrentUser, response: Response, owner: OwnerLogin = None
) -> Account:
    """The caller's account ``name`` with its figures, or with ``owner`` the one that member shared with the caller;
    404 for a name that is neither."""
    found = await service.read(request.app.state.engine, user, name, owner, _seen(request))
    response.headers.update(NO_STORE)
    return found


@router.get("/shared-accounts", response_model=list[SharedAccount])
async def list_shared_accounts(request: Request, user: CurrentUser, response: Response) -> list[SharedAccount]:
    """The env accounts other members share with the caller through a live share for every run, by variable, owner
    and name, as ``_shared_account`` gives each one, with the projects of those shares its owner binds it to; one bound
    to none of them is left out."""
    found = await service.list_shared(request.app.state.engine, user, _seen(request))
    response.headers.update(NO_STORE)
    return found


@router.patch("/accounts/{name}", response_model=Account, responses=REFUSALS)
async def change_account(
    request: Request, name: AccountName, body: AccountChange, user: CurrentUser, response: Response
) -> Account:
    """Change the weight, priority or weekly budget of the caller's account ``name``, or pause or resume it; a resume
    also ends its cooldown, save one that needs a new sign-in (``end_cooldown``)."""
    found = await service.change(request.app.state.engine, user, name, body, _seen(request))
    log.info("account changed", extra={"secret": name, "login": user.login, "state": found.state})
    response.headers.update(NO_STORE)
    return found


# Policies


@router.get("/account-policies", response_model=list[AccountPolicy])
async def list_account_policies(request: Request, user: CurrentUser, response: Response) -> list[AccountPolicy]:
    """The caller's own policies, by project; a project without one has the defaults."""
    found = await service.list_policies(request.app.state.engine, user)
    response.headers.update(NO_STORE)
    return found


@router.get("/account-policies/{project}", response_model=AccountPolicy, responses=REFUSALS)
async def get_account_policy(request: Request, project: ProjectName, user: CurrentUser) -> AccountPolicy:
    """The caller's policy for ``project``, the defaults when they set none."""
    return await service.read_policy(request.app.state.engine, user, project)


@router.put("/account-policies/{project}", response_model=AccountPolicy, responses=REFUSALS)
async def put_account_policy(
    request: Request, project: ProjectName, body: AccountPolicyWrite, user: CurrentUser
) -> AccountPolicy:
    """Set the caller's policy for ``project`` whole; the writer role on it."""
    found = await service.put_policy(request.app.state.engine, user, project, body)
    log.info("account policy set", extra={"project": project, "login": user.login, "strategy": body.strategy})
    return found


@router.delete("/account-policies/{project}", status_code=204, response_class=Response, responses=REFUSALS)
async def delete_account_policy(request: Request, project: ProjectName, user: CurrentUser) -> Response:
    """Drop the caller's policy for ``project``: the defaults hold again. 404 when they set none."""
    await service.drop_policy(request.app.state.engine, user, project)
    log.info("account policy dropped", extra={"project": project, "login": user.login})
    return Response(status_code=204)

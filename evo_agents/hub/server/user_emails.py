"""A member's emails: list them, add one, remove one (``evo-agents hub email list|add|remove``).

A member manages their own addresses; a hub admin manages anyone's, naming the member with ``user``, and anyone else
naming another member gets 403. An address belongs to one member on the hub, whatever its case: adding one another
member holds is a 409, adding one the member holds already answers that row with 200 instead of 201. A member holds
at most ``MAX_EMAILS``. GitHub's noreply address of a login maps to that login without a row, so one naming another
login is refused, and the member's own adds nothing but is taken. The hub never checks that the member reads an
address: sign-in asks GitHub for ``read:user`` only. An address travels in the body, never in a path, so no access
line holds it; removing names the row's id. Every change adds an audit row that names the row and the member, never
the address.

``logins_of_emails`` names the member of each of a few addresses for the hub's own use, through
``evo_agents.hub.user_emails.login_of_email``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Path, Query, Request, Response

from evo_agents.hub.server.base.models import LOGIN_NAME
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.identity.models.user_emails import MAX_BODY_CHARS as MAX_BODY_CHARS
from evo_agents.hub.server.identity.models.user_emails import MAX_ID as MAX_ID
from evo_agents.hub.server.identity.models.user_emails import EmailAdd as EmailAdd
from evo_agents.hub.server.identity.models.user_emails import UserEmail as UserEmail
from evo_agents.hub.server.identity.service import user_emails as service
from evo_agents.hub.server.identity.service.user_emails import logins_of_emails as logins_of_emails
from evo_agents.hub.server.security import CurrentUser

router = APIRouter(prefix="/v1/emails", tags=["emails"], responses={401: {"model": ErrorBody}})

Member = Annotated[
    str | None,
    Query(alias="user", pattern=LOGIN_NAME, description="the member, any case; another's needs a hub admin"),
]


@router.get("", response_model=list[UserEmail], responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}})
async def list_emails(request: Request, user: CurrentUser, login: Member = None) -> list[UserEmail]:
    """The emails of the caller, or of the member ``user`` names, in the order of their addresses."""
    return await service.list_emails(request.app.state.engine, user, login)


@router.post(
    "",
    response_model=UserEmail,
    status_code=201,
    responses={200: {"model": UserEmail}, **{code: {"model": ErrorBody} for code in (403, 404, 409, 422)}},
)
async def add_email(
    request: Request, response: Response, body: EmailAdd, user: CurrentUser, login: Member = None
) -> UserEmail:
    """Give the caller, or the member ``user`` names, the address ``email``: 201 with the new row, 200 with the row
    they had, 409 when another member holds it."""
    added, new = await service.add_email(request.app.state.engine, user, login, body.email.strip())
    if not new:
        response.status_code = 200
    return added


@router.delete(
    "/{email_id}",
    response_model=UserEmail,
    responses={403: {"model": ErrorBody}, 404: {"model": ErrorBody}},
)
async def remove_email(
    request: Request, user: CurrentUser, email_id: Annotated[int, Path(ge=1, le=MAX_ID)]
) -> UserEmail:
    """Remove the email ``email_id`` of the caller, or of anyone for a hub admin; answers the row it removed."""
    return await service.remove_email(request.app.state.engine, user, email_id)

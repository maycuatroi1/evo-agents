"""The route of whom a person may add to a group chat: GET /v1/chats/people (``chats.service.people``)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request

from evo_agents.hub import chats, profiles
from evo_agents.hub.server.chats.models.people import ChatCandidate
from evo_agents.hub.server.chats.service.people import candidates
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/chats", tags=["chats"], responses={401: {"model": ErrorBody}})


@router.get("/people", response_model=list[ChatCandidate])
async def list_candidates(
    request: Request,
    user: CurrentUser,
    agents: Annotated[
        list[str],
        Query(max_length=chats.MAX_AGENTS, description="a handle of the chat's agents, @ left out; repeat it"),
    ] = [],  # noqa: B006
) -> list[ChatCandidate]:
    """The people the caller may add to a chat, by login, each with why they could not be in it with ``agents``."""
    handles = [handle for handle in agents if profiles.HANDLE.fullmatch(handle)]
    async with request.app.state.engine.begin() as conn:
        found = await candidates(conn, user, handles)
    log.info("chat candidates read", extra={"people": len(found), "agents": len(handles)})
    return found

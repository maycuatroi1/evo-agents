"""The routes of the agent of a run in group chats: ``evo-agents worker chat post|react|create|mention``, the pull
request of a chat run's branch and the repos its agent opens. ``docs/workers.md`` (Chat runs) is the protocol;
``chats.service.agents`` and ``chats.service.pulls`` say what each does."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from evo_agents.hub.server.chats.models.agents import (
    REFUSALS,
    AgentChatIn,
    AgentHanded,
    AgentMentionIn,
    AgentMessageIn,
    AgentReactionIn,
    ChatPull,
    ChatPushed,
    ChatRepo,
    ChatRepoIn,
)
from evo_agents.hub.server.chats.models.views import Chat, GroupMessage
from evo_agents.hub.server.chats.service import agents, pulls
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import RunId
from evo_agents.hub.server.security import CurrentUser

log = logging.getLogger(__name__)

worker_router = APIRouter(prefix="/v1/worker", tags=["worker protocol"], responses={401: {"model": ErrorBody}})


@worker_router.post("/runs/{run_id}/chat/messages", status_code=201, response_model=GroupMessage, responses=REFUSALS)
async def post_message(request: Request, run_id: RunId, body: AgentMessageIn, user: CurrentUser) -> GroupMessage:
    """A message of the run's agent in a chat it is in: the run's thread for a chat run."""
    async with request.app.state.engine.begin() as conn:
        view, chat_id, message_id = await agents.post_message(conn, request.app.state.sealer, user, run_id, body)
    log.info("chat message of an agent", extra={"run_id": run_id, "chat_id": chat_id, "message_id": message_id})
    return view


@worker_router.post("/runs/{run_id}/chat/reactions", response_model=GroupMessage, responses=REFUSALS)
async def react(request: Request, run_id: RunId, body: AgentReactionIn, user: CurrentUser) -> GroupMessage:
    """Put an emoji of the run's agent on a message of a chat it is in, or take it off."""
    async with request.app.state.engine.begin() as conn:
        return await agents.react(conn, user, run_id, body)


@worker_router.post("/runs/{run_id}/chat/mentions", status_code=201, response_model=AgentHanded, responses=REFUSALS)
async def mention(request: Request, run_id: RunId, body: AgentMentionIn, user: CurrentUser) -> AgentHanded:
    """A message of the run's agent that hands the work to agent ``handle`` of the chat (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        handed = await agents.mention(conn, request.app.state.sealer, user, run_id, body)
    extra = {"run_id": run_id, "handle": body.handle, "made": handed.run_id, "refused": bool(handed.refusal)}
    log.info("agent mention", extra=extra)
    return handed


@worker_router.post("/runs/{run_id}/chats", status_code=201, response_model=Chat, responses=REFUSALS)
async def create_chat(request: Request, run_id: RunId, body: AgentChatIn, user: CurrentUser) -> Chat:
    """A new chat of the run's agent and the person at the origin of the chain, with the people named."""
    async with request.app.state.engine.begin() as conn:
        view, chat_id, people, agent_count = await agents.create_chat(conn, user, run_id, body)
    extra = {"run_id": run_id, "chat_id": chat_id, "people": people, "agents": agent_count}
    log.info("chat created by an agent", extra=extra)
    return view


@worker_router.post("/runs/{run_id}/chat/pulls", status_code=201, response_model=ChatPull, responses=REFUSALS)
async def pushed(request: Request, run_id: RunId, body: ChatPushed, user: CurrentUser) -> ChatPull:
    """The worker pushed the chat run's branch of a repo: its draft pull request into the branch it was made from
    (``draft_pulls.open_for``), kept on the run and linked in the thread; none for a branch with nothing to merge."""
    state = request.app.state
    pull = await pulls.pushed(state.engine, state.github_app, user, run_id, body)
    extra = {"run_id": run_id, "repo": body.repo, "pull": pull.number, "opened": pull.opened}
    log.info("chat run branch pushed", extra=extra)
    return pull


@worker_router.post("/runs/{run_id}/chat/repos", response_model=ChatRepo, responses=REFUSALS)
async def open_repo(request: Request, run_id: RunId, body: ChatRepoIn, user: CurrentUser) -> ChatRepo:
    """The worker opened repo ``repo`` of the project in the chat run, as its agent asked: the hub adds it to the run's
    repos on the run's branch (see the module's docstring)."""
    async with request.app.state.engine.begin() as conn:
        repo = await pulls.open_repo(conn, user, run_id, body)
    log.info("chat run repo opened", extra={"run_id": run_id, "repo": repo.repo, "added": repo.added})
    return repo

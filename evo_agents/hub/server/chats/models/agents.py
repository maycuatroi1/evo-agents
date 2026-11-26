"""The agent of a run in group chats (``chats.service.agents``): the bodies of the worker's commands, what they
answer, and the run whose agent acts."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, Field, field_validator

from evo_agents.hub import chats, outputs, profiles
from evo_agents.hub.server.chats.models.members import LOGIN_PATTERN
from evo_agents.hub.server.chats.models.views import GroupMessage
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.runs.models import MAX_ID, OBJECT_NAME, RunRepo

REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (404, 409, 422)}
Login = Annotated[str, Field(pattern=LOGIN_PATTERN)]
Handle = Annotated[str, Field(pattern=profiles.HANDLE_PATTERN)]
MessageRef = Annotated[int | None, Field(None, ge=1, le=MAX_ID)]


def _bounded(value: str) -> str:
    if not value.strip():
        raise ValueError("a message has some text")
    if len(value.encode()) > chats.MAX_MESSAGE_BYTES:
        raise ValueError(f"a message is at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    return value


class AgentMessageIn(BaseModel):
    body: str = Field(min_length=1, description=f"at most {chats.MAX_MESSAGE_BYTES} bytes of UTF-8")
    chat_id: int | None = Field(
        None, ge=1, le=MAX_ID, description="a chat the run's agent is in; null: the chat of a chat run"
    )
    reply_to: MessageRef = Field(None, description="a message whose thread it goes in; null: the run's thread")
    output_ids: list[Annotated[int, Field(ge=1, le=MAX_ID)]] = Field(
        default_factory=list,
        max_length=outputs.MAX_ATTACHED,
        description="outputs of the run the message carries (POST .../runs/{id}/outputs), none of source tool",
    )

    _body = field_validator("body")(_bounded)


class AgentMentionIn(AgentMessageIn):
    handle: Handle = Field(description="the agent of the chat the message hands work to, @ left out")


class AgentReactionIn(BaseModel):
    message_id: int = Field(ge=1, le=MAX_ID)
    emoji: str = Field(min_length=1, max_length=chats.MAX_EMOJI_CHARS)
    remove: bool = Field(False, description="take the emoji off instead")

    @field_validator("emoji")
    @classmethod
    def _one_emoji(cls, value: str) -> str:
        problem = chats.emoji_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class AgentChatIn(BaseModel):
    title: str = Field(min_length=1, max_length=chats.MAX_TITLE_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")
    people: list[Login] = Field(
        default_factory=list,
        max_length=chats.MAX_PEOPLE - 1,
        description="logins besides the person who dispatched the run, who is in it anyway",
    )
    agents: list[Handle] = Field(
        default_factory=list,
        max_length=chats.MAX_AGENTS - 1,
        description="handles, @ left out, of agents besides the run's, which is in it anyway",
    )


class AgentHanded(BaseModel):
    message: GroupMessage
    run_id: int | None = Field(description="the run of the agent mentioned: new, or the one of its session it woke")
    refusal: str | None = Field(description="why the mention handed no work, as the thread says too")
    group_id: int | None = Field(
        None, description="the group chat of the direct chat's thread the message went to (chat_groups); null: none"
    )


class ChatPushed(BaseModel):
    repo: str = Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")
    branch: str = Field(min_length=1, max_length=255, description="the run's branch, chat/<id>")
    head: str = Field(pattern=OBJECT_NAME, description="the commit the branch has now")
    base: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        pattern=r"^[^\x00-\x20\x7f~^:?*\[\\]+$",
        description="the branch of origin the branch was made from, which its pull request goes into; null: the hub "
        "picks one (``draft_pulls.pick_base``)",
    )


class ChatPull(BaseModel):
    repo: str
    branch: str
    url: str | None = Field(description="the draft pull request, or the branch; null when the hub knows neither")
    number: int | None = Field(description="the pull request's number on GitHub")
    opened: bool = Field(description="the hub opened it now; false for one open already, or none")
    message_id: int = Field(description="the agent's message in the thread that says so")


class ChatRepoIn(BaseModel):
    repo: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^\x00-\x1f\x7f]+$",
        description="a repo of the run's project, as the project names it, that the worker has a checkout of",
    )


class ChatRepo(BaseModel):
    repo: str
    branch: str = Field(description="the run's branch, chat/<id of its session's first run>, the repo's worktree is on")
    added: bool = Field(description="the hub added the repo to the run now; false when the run worked in it already")
    repos: list[RunRepo] = Field(description="the run's repos now: those it started with, then those opened, in order")

"""The shapes of session digests (``evo_agents.hub.server.digests``): the digest of one session as the Stop hook
pushes it and as the hub keeps it (``SessionDigest``, checked against the limits of ``evo_agents.hub.digest``), what
a list and a read answer, the limits of a request (``BoundedRoute``) and the texts of the refusals."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import HTTPException, Path, Request
from fastapi.routing import APIRoute
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evo_agents.hub import digest
from evo_agents.hub.server.errors import ErrorBody

DIGEST_DAYS = 90  # a digest not pushed again for this long is deleted
MAX_REQUEST = 2 * 1024 * 1024  # a digest at its limits, escaped as JSON at worst, fits
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SCAN_ROWS = 10_000
TOO_LARGE = f"a session digest is at most {MAX_REQUEST // 1024} KiB of request"
SESSION_PATTERN = rf"^{digest.SESSION_ID.pattern}$"
REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404, 413, 422)}
READ_REFUSALS: dict[int | str, dict[str, Any]] = {code: {"model": ErrorBody} for code in (403, 404)}

SessionId = Annotated[
    str, Path(pattern=SESSION_PATTERN, description="the id Claude Code gives the session, as its transcript names it")
]
Count = Annotated[int, Field(ge=0, le=digest.MAX_COUNT)]
Name = Annotated[str, Field(min_length=1, max_length=digest.MAX_NAME)]


class BoundedRoute(APIRoute):
    """Reads at most MAX_REQUEST bytes of a request before FastAPI parses it: a larger one is a 413, whether it says
    its length or not."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded(request: Request):
            declared = request.headers.get("content-length")
            if declared is not None and (not declared.isdigit() or int(declared) > MAX_REQUEST):
                raise HTTPException(413, TOO_LARGE)
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_REQUEST:
                    raise HTTPException(413, TOO_LARGE)
                chunks.append(chunk)
            request._body = b"".join(chunks)  # what Request.body() returns from now on
            return await handler(request)

        return bounded


# The shapes


class ModelCount(BaseModel):
    model: Name
    messages: Count = Field(description="messages of the model that name it")


class TokenUsage(BaseModel):
    input_tokens: Count = 0
    output_tokens: Count = 0
    cache_creation_input_tokens: Count = 0
    cache_read_input_tokens: Count = 0


class ToolCount(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Name = Field(alias=digest.TOOL_NAME, description="the tool, as the runtime names it")
    calls: Count
    errors: Count = Field(description="calls whose result was an error")


class ProgramCount(BaseModel):
    program: Name = Field(description="the program a Bash call ran, with its subcommand: git push, python -m pytest")
    calls: Count
    errors: Count


class ProgramKinds(BaseModel):
    program: Name
    usage_errors: Count = Field(description="failures of a wrong call: a usage line, an unknown option or choice")
    env_errors: Count = Field(description="failures the environment caused: the hub or the network, a timeout, a block")
    other_errors: Count


class CompoundBash(BaseModel):
    calls: Count = Field(description="Bash commands that ran several programs")
    errors: Count = Field(description="their failures whose output named none of their programs")
    usage_errors: Count = 0
    env_errors: Count = 0
    other_errors: Count = 0


class SkillCount(BaseModel):
    skill: Name = Field(description="the skill, as the Skill tool or the person's /name named it")
    calls: Count = Field(description="loads: through the Skill tool, and typed by the person")
    typed: Count = Field(description="loads the person typed as /name (Codex: named with $name)")
    failed: Count = Field(0, description="calls of the Skill tool that answered an error")
    bash: Count = Field(0, description="Bash commands from each load to the person's next turn")
    usage_errors: Count = 0
    env_errors: Count = 0
    other_errors: Count = 0
    interrupts: Count = Field(0, description="times the person interrupted the agent in those windows")
    corrections: Count = Field(0, description="turns that closed a window and read as a correction of the agent")


class CommandCount(BaseModel):
    command: str = Field(max_length=digest.COMMAND_CHARS)
    n: Count


class ErrorText(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Name = Field(alias=digest.TOOL_NAME)
    text: str = Field(max_length=digest.ERROR_CHARS, description="the tool's result, cut")
    n: Count = Field(description="how many results said exactly this")


class ToolChars(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: Name = Field(alias=digest.TOOL_NAME)
    chars: Count = Field(description="characters of the text of the tool's results, which the model read")


class SessionDigest(BaseModel):
    """The digest of one session, as ``evo_agents.hub.digest.build`` makes it."""

    cwd: str = Field(min_length=1, max_length=digest.MAX_CWD, description="the session's working directory")
    messages: int = Field(ge=digest.MIN_MESSAGES, le=digest.MAX_COUNT, description="of the person and the model")
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    model: Name | None = Field(None, description="the model most messages name")
    models: list[ModelCount] = Field(default_factory=list, max_length=digest.MAX_MODELS)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    tools: list[ToolCount] = Field(default_factory=list, max_length=digest.MAX_TOOLS)
    bash: list[ProgramCount] = Field(default_factory=list, max_length=digest.MAX_PROGRAMS)
    user_turns: list[Annotated[str, Field(max_length=digest.USER_TURN_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_USER_TURNS
    )
    commands: list[Annotated[str, Field(max_length=digest.COMMAND_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_COMMANDS
    )
    repeated_commands: list[CommandCount] = Field(default_factory=list, max_length=digest.MAX_REPEATED)
    errors: list[ErrorText] = Field(default_factory=list, max_length=digest.MAX_ERRORS)
    files_read: Count = 0
    files_edited: list[Annotated[str, Field(max_length=digest.PATH_CHARS)]] = Field(
        default_factory=list, max_length=digest.MAX_FILES
    )
    peak_context_tokens: Count | None = Field(
        None,
        description="the largest context one message of the model read: its input, cache creation and cache read "
        "tokens; null when no message said its usage, or a client that did not count it pushed the digest",
    )
    compactions: Count | None = Field(
        None, description="times the session's context was compacted; null when a client that did not count it pushed"
    )
    tool_result_chars: list[ToolChars] | None = Field(
        None,
        max_length=digest.MAX_TOOLS,
        description="per tool, the characters of its results the model read, the most first; null when a client that "
        "did not count them pushed the digest",
    )
    bash_kinds: list[ProgramKinds] | None = Field(
        None,
        max_length=digest.MAX_PROGRAMS,
        description="per program with failures, how many of each kind; null when an older client pushed the digest",
    )
    bash_compound: CompoundBash | None = Field(
        None, description="Bash commands that ran several programs; null when a client that did not count them pushed"
    )
    skills: list[SkillCount] | None = Field(
        None,
        max_length=digest.MAX_SKILLS,
        description="per skill loaded, its loads and the friction after them, the most loaded first; null when a "
        "client that did not count them pushed the digest",
    )
    interrupts: Count | None = Field(None, description="times the person interrupted the agent")
    turns: Count | None = Field(None, description="turns of the person, the commands they typed among them")
    spool_results: Count | None = Field(
        None,
        description="results the spool hook cut, each written whole to a file on the machine; null when a client that "
        "did not count them pushed the digest",
    )
    spool_original_chars: Count | None = Field(
        None,
        description="the characters those results had before the cut; null when a client that did not count them "
        "pushed the digest",
    )
    spool_reads: Count | None = Field(
        None,
        description="calls that named the spool directory, the agent reading a spool file again; null when a client "
        "that did not count them pushed the digest",
    )


class DigestSummary(BaseModel):
    session_id: str
    login: str = Field(description="the member who pushed it")
    cwd: str
    messages: int
    model: str | None
    started_at: datetime | None
    ended_at: datetime | None
    label: dict
    created_at: datetime = Field(description="its first push")
    updated_at: datetime = Field(description="its latest push")


class DigestRecord(DigestSummary):
    digest: SessionDigest


class DigestList(BaseModel):
    digests: list[DigestSummary] = Field(description="the latest pushed first")
    total: int = Field(description="digests the caller may read that match the filters")
    limit: int
    offset: int


def not_found(project: str, session_id: str) -> str:
    return f"project {project} has no digest of session {session_id}: see GET /v1/projects/{project}/digests"


def not_theirs(project: str, session_id: str) -> str:
    return f"the digest of session {session_id} in project {project} is another member's"

"""The server side of the core plugin builder (``evo_agents.core_plugins.builder``): the tool its agent proposes a
change with, the event that reads an admin's answer in the thread, and the routes of the proposals.

- ``propose`` is the tool ``builder__propose`` of the hub's /mcp. It answers the agent of a chat run of @builder:
  the run (X-Evo-Run) is a chat run whose profile is the one the project's agent type builder created, the type is on,
  and the member who dispatched the run is an admin of the project still. It checks the change against the setting as
  it is now, keeps it pending, shows it in the thread and tells each member who may approve it (a notice
  ``builder_proposal`` in the Inbox). It applies nothing. It also answers the agent of the Curator's review run on a
  night of the lens product (plan loop-engine, criterion a9), for a change of the autonomy policy alone, while the
  night shift's owner the run proposes for holds the writer role: the proposal has no thread, and its notice links to
  the run.
- ``on_message`` takes ``chat.message``: a member's message in the thread of a proposal that is only ``approve N``,
  ``reject N`` or ``rollback N`` answers it, as that member, then the hub says what happened in the thread. A member
  who may not decide it gets the hub's refusal there. A delivery made again finds the answer done, or the refusal
  said, and says nothing.
- ``router`` lists and shows a project's proposals, and answers and rolls one back, under /v1/x/builder/<project>/
  for a member with a grant on the project; the agent of a run never answers one. ``proposals/waiting`` lists the
  pending ones the caller may decide, where the web and ``evo-agents hub intake proposal`` find those an intake made,
  which have no thread.

Approving a proposal of kind plugin_install installs the release from GitHub (``market_install.install_github``) with
what the request's app holds, or, in the thread, with the hub's engine, config, blob store and Sigstore.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents import __version__
from evo_agents.core_plugins.builder import plugin_changes, proposals
from evo_agents.core_plugins.builder.proposals import APPROVE, REJECT, ROLLBACK, BuilderProposal, ProposalError
from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.blobs import BlobStore
from evo_agents.hub.plugin_calls import CoreContext, enabled_in
from evo_agents.hub.review import PRODUCT
from evo_agents.hub.self_managed import ProjectScope, state_of
from evo_agents.hub.server import notifications
from evo_agents.hub.server.admin import PROJECT_NAME
from evo_agents.hub.server.agent_types import enabled_type
from evo_agents.hub.server.chat_runs import ChatRun, chat_run, hub_reply
from evo_agents.hub.server.collect import looks_through
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.market_install import GitHubInstallIn, Installing, install_github
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import MACHINE, CurrentUser, Principal
from evo_agents.hub.tables_settings import PROPOSAL_KINDS, PROPOSAL_STATES
from evo_agents.plugins.signing import default_sigstore

log = logging.getLogger(__name__)

TYPE = "builder"  # the agent type of the manifest
REVIEW = "review"  # the kind of the Curator's review run, which proposes through the lens product
CURATOR = "The Curator"  # who proposes, in the title of the notice of a proposal of a review run
COMMAND = re.compile(r"\s*(approve|reject|roll\s*back)\s+#?([1-9][0-9]{0,17})\s*[.!]?\s*", re.IGNORECASE)
REFUSALS = {code: {"model": ErrorBody} for code in (403, 404, 409, 422)}
ProjectName = Annotated[str, Path(pattern=PROJECT_NAME)]
ProposalId = Annotated[int, Path(ge=1)]


def _text(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def _is_admin(config, login: str) -> bool:
    """Whether ``login`` is a hub admin; never without the hub's config, which names them."""
    return config is not None and config.is_admin(login)


async def _member(conn: AsyncConnection, user_id: int, config) -> Principal:
    """Member ``user_id`` as the hub sees a request of theirs that carries no token."""
    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    return Principal(user_id, login, _is_admin(config, login), None, MACHINE, "")


async def _access(conn: AsyncConnection, member: Principal, project: str) -> ProjectAccess:
    try:
        return await project_access(conn, member, project)
    except HTTPException:
        raise ProposalError(403, f"{member.login} holds no grant on project {project} now") from None


# The tool


async def _builder_run(conn: AsyncConnection, context: CoreContext) -> ChatRun:
    """The chat run of @builder the tool is called from; ProposalError (403) for any other caller."""
    refused = ProposalError(
        403, f"only the agent of a chat run of @{TYPE} in project {context.project} proposes a change"
    )
    if context.run_id is None or context.project_id is None:
        raise refused
    run = await chat_run(conn, context.run_id)
    plugin = await enabled_in(conn, context.plugin, context.project_id)
    if run is None or plugin is None:
        raise refused
    kind = await enabled_type(conn, context.project_id, plugin.id, TYPE)
    if kind is None or kind.disabled_at is not None or kind.profile_id != run.profile_id:
        raise refused
    return run


@dataclass(frozen=True)
class ReviewRun:
    """The Curator's review run a proposal comes from, as ``proposals.store`` reads a run: no chat and no thread."""

    run_id: int
    dispatcher_id: int
    chat_id: None = None
    root_id: None = None


async def _tell_approvers(
    conn: AsyncConnection,
    access: ProjectAccess,
    change: proposals.Change,
    proposal_id: int,
    run,
    link: str,
    config,
    by: str = f"@{TYPE}",
) -> int:
    admins = config.admins if config is not None else frozenset()
    own = await proposals.own_install(conn, access, change.kind, change.sets)
    told = await proposals.approvers(conn, access, change.kind, admins, own)
    for user_id in told:
        await notifications.notify(
            conn,
            user_id=user_id,
            kind="notice",
            notice_kind=proposals.NOTICE,
            title=f"{by} proposes a change of {change.kind.replace('_', ' ')} in {access.name}",
            project_id=access.project_id,
            run_id=run.run_id,
            details={"proposal": proposal_id, "kind": change.kind, "project": access.name},
            link=link,
        )
    return len(told)


async def _review_run(conn: AsyncConnection, context: CoreContext) -> ReviewRun | None:
    """The Curator's review run the tool is called from, on a night of the lens product; None when the caller is no
    review run; ProposalError (403) for a review run of another night."""
    if context.run_id is None or context.project_id is None:
        return None
    r = tables.runs
    row = (await conn.execute(select(r.c.kind, r.c.dispatched_by).where(r.c.id == context.run_id))).first()
    if row is None or row.kind != REVIEW:
        return None
    if not await looks_through(conn, context.run_id, context.project_id, PRODUCT):
        raise ProposalError(
            403,
            f"the Curator's review run #{context.run_id} does not look through the lens {PRODUCT} tonight: a review "
            "run proposes a change of the autonomy policy on a night of that lens alone",
        )
    return ReviewRun(context.run_id, row.dispatched_by)


async def _propose_for_review(
    conn: AsyncConnection, change: proposals.Change, context: CoreContext, run: ReviewRun
) -> str:
    """Keep ``change`` as a proposal of the Curator's review run ``run``, for the night shift's owner."""
    if change.kind != proposals.POLICY:
        raise ProposalError(
            403, f"the Curator's review run proposes a change of the autonomy policy alone, not of {change.kind}"
        )
    owner = await _member(conn, run.dispatcher_id, context.config)
    access = await _access(conn, owner, context.project)
    if not has_role(access.role, "writer"):
        raise ProposalError(
            403, f"the Curator proposes for a writer of project {access.name}, and {owner.login} is none now"
        )
    target = await proposals.locate(conn, access, change)
    await proposals.check(conn, access, change, target, __version__)
    proposal_id = await proposals.store(conn, access, change, target, run)
    link = notifications.run_link(access.name, run.run_id)
    told = await _tell_approvers(conn, access, change, proposal_id, run, link, context.config, by=CURATOR)
    await proposals.record_proposal(conn, owner, access, proposal_id, run)
    log.info("builder proposal made", extra={"proposal": proposal_id, "kind": change.kind, "run_id": run.run_id})
    approver = proposals.approver_words(
        change.kind, access.name, await proposals.own_install(conn, access, change.kind, change.sets)
    )
    return (
        f"Proposal {proposal_id} waits for {approver}: the hub told {told} member(s) in their Inbox. Nothing is "
        "applied until an admin approves it; record no finding to repeat it."
    )


async def _propose(conn: AsyncConnection, change: proposals.Change, context: CoreContext) -> str:
    review = await _review_run(conn, context)
    if review is not None:
        return await _propose_for_review(conn, change, context, review)
    run = await _builder_run(conn, context)
    dispatcher = await _member(conn, run.dispatcher_id, context.config)
    access = await _access(conn, dispatcher, context.project)
    if not has_role(access.role, "admin"):
        raise ProposalError(
            403, f"@{TYPE} proposes for an admin of project {access.name}, and {dispatcher.login} is none now"
        )
    target = await proposals.locate(conn, access, change)
    await proposals.check(conn, access, change, target, __version__)
    proposal_id = await proposals.store(conn, access, change, target, run)
    approver = proposals.approver_words(
        change.kind, access.name, await proposals.own_install(conn, access, change.kind, change.sets)
    )
    shown = proposals.thread_text(proposal_id, change, target, approver)
    message_id = await hub_reply(conn, run.chat_id, run.root_id, shown)
    await proposals.shown_at(conn, proposal_id, message_id)
    link = proposals.chats.chat_link(run.chat_id, message_id)
    told = await _tell_approvers(conn, access, change, proposal_id, run, link, context.config)
    await proposals.record_proposal(conn, dispatcher, access, proposal_id, run)
    log.info("builder proposal made", extra={"proposal": proposal_id, "kind": change.kind, "run_id": run.run_id})
    return (
        f"Proposal {proposal_id} waits for {approver}: the hub showed it in the thread and told {told} member(s). "
        f"Nothing is applied until {approver} approves it; say so to the member, and do not answer for them."
    )


async def propose(params: dict, context: CoreContext) -> dict:
    """The tool ``builder__propose``: keep a change of a setting as a proposal an admin decides; apply nothing."""
    try:
        change = proposals.change_of(params.get("arguments") or {})
        async with context.engine.begin() as conn:
            return _text(await _propose(conn, change, context))
    except ProposalError as exc:
        return _text(f"error: {exc.message}", error=True)


# Answers in the thread


def outcome(found: BuilderProposal) -> str:
    """What the hub says in the thread once proposal ``found`` was decided or rolled back."""
    head = f"Proposal {found.id}"
    if found.state == proposals.APPLIED and found.kind in proposals.ACTIONS:
        return f"{head} is approved by {found.decided_by}: {found.outcome}."
    if found.state == proposals.APPLIED:
        return (
            f"{head} is applied by {found.decided_by}: the setting is at version {found.applied_version} now. "
            f"`rollback {found.id}` brings back the version before it."
        )
    if found.state == proposals.FAILED:
        return f"{head} was approved by {found.decided_by}, and the hub could not apply it: {found.error}"
    if found.state == proposals.REJECTED:
        return f"{head} is rejected by {found.decided_by}: nothing changed."
    return (
        f"{head} is rolled back: version {found.rollback_version} of the setting is a copy of the one before "
        f"version {found.applied_version}."
    )


def _verb(word: str) -> str:
    word = word.lower()
    return ROLLBACK if word.startswith("roll") else word


async def answer_as(
    conn: AsyncConnection,
    member: Principal,
    project: str,
    proposal_id: int,
    verb: str,
    hub_version: str,
    install: plugin_changes.Install | None = None,
) -> BuilderProposal:
    """Decide (APPROVE, REJECT) or roll back (ROLLBACK) proposal ``proposal_id`` as ``member``, ``install``
    installing a release for one of kind plugin_install; ProposalError when they may not, or the hub cannot."""
    if member.acts_for_run is not None:
        raise ProposalError(403, "the agent of a run never answers a proposal of @builder: an admin does")
    access = await _access(conn, member, project)
    if verb == ROLLBACK:
        return await proposals.rollback(conn, member, access, proposal_id, hub_version)
    return await proposals.decide(conn, member, access, proposal_id, verb, hub_version, install)


def installer(hub: Installing, transport=None) -> plugin_changes.Install:
    """Installing a GitHub release with what ``hub`` holds, reading GitHub through ``transport``."""

    async def install(user: Principal, body: GitHubInstallIn, scope: ProjectScope | None = None):
        return await install_github(hub, transport, user, body, scope)

    return install


def _context_installer(context: CoreContext) -> plugin_changes.Install:
    """Installing a GitHub release from the event's context, as the worker's jobs install one."""

    async def install(user: Principal, body: GitHubInstallIn, scope: ProjectScope | None = None):
        store = BlobStore.from_config(context.config)
        if store is None:
            missing = ", ".join(context.config.blob_store_missing())
            raise ProposalError(503, f"the blob store is not configured on this hub: set {missing}")
        hub = Installing(context.engine, context.config, default_sigstore(), __version__, store)
        return await install_github(hub, None, user, body, scope)

    return install


async def _refused_already(conn: AsyncConnection, payload: dict, seq: int, text: str) -> bool:
    """Whether the hub said refusal ``text`` in the thread after the message of sequence ``seq``: a delivery of the
    same message made again."""
    m = tables.chat_messages
    query = select(m.c.id).where(
        m.c.chat_id == payload["chat_id"],
        m.c.thread_root_id == payload["thread_root_id"],
        m.c.seq > seq,
        m.c.author_user_id.is_(None),
        m.c.author_profile_id.is_(None),
        m.c.body == text,
    )
    return (await conn.execute(query.limit(1))).first() is not None


async def _thread_answer(conn: AsyncConnection, payload: dict, context: CoreContext) -> str | None:
    m = tables.chat_messages
    query = select(m.c.body, m.c.author_user_id, m.c.seq).where(
        m.c.id == payload["message_id"], m.c.chat_id == payload["chat_id"]
    )
    said = (await conn.execute(query)).first()
    match = COMMAND.fullmatch(said.body) if said is not None and said.author_user_id is not None else None
    if match is None:
        return None
    verb, proposal_id = _verb(match[1]), int(match[2])
    found = await proposals.in_thread(
        conn, context.project_id, proposal_id, payload["chat_id"], payload["thread_root_id"]
    )
    if found is None or proposals.done_already(found, verb, said.author_user_id):
        return None
    member = await _member(conn, said.author_user_id, context.config)
    try:
        install = _context_installer(context) if context.config is not None else None
        found = await answer_as(conn, member, context.project, proposal_id, verb, __version__, install)
        text = outcome(found)
    except ProposalError as exc:
        text = f"{member.login}: {exc.message}"
        if await _refused_already(conn, payload, said.seq, text):
            return None
    await hub_reply(conn, payload["chat_id"], payload["thread_root_id"], text)
    return text


async def on_message(params: dict, context: CoreContext) -> dict:
    """The event ``answers`` (``chat.message``): a member's ``approve N``, ``reject N`` or ``rollback N`` in the thread
    of proposal N of the project."""
    payload = params.get("payload") or {}
    if payload.get("by") != "person" or not payload.get("thread_root_id") or context.project_id is None:
        return {"answered": False}
    if context.config is None:
        log.warning("builder answers read without the hub's config: hub admins are not known")
    async with context.engine.begin() as conn:
        text = await _thread_answer(conn, payload, context)
    return {"answered": text is not None}


# Routes


class BuilderProposalAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Literal[APPROVE, REJECT]


router = APIRouter(tags=["plugins"], responses={401: {"model": ErrorBody}})


async def _say_in_thread(conn: AsyncConnection, proposal_id: int, found: BuilderProposal) -> None:
    thread = await proposals.thread_of(conn, proposal_id)
    if thread is not None:
        await hub_reply(conn, *thread, outcome(found))


def _request_installer(request: Request) -> plugin_changes.Install:
    async def install(user: Principal, body: GitHubInstallIn, scope: ProjectScope | None = None):
        hub = Installing.of(request)
        return await installer(hub, getattr(request.app.state, "market_transport", None))(user, body, scope)

    return install


async def _answered(request: Request, user: Principal, project: str, proposal_id: int, verb: str) -> BuilderProposal:
    install = _request_installer(request)
    try:
        async with request.app.state.engine.begin() as conn:
            found = await answer_as(conn, user, project, proposal_id, verb, request.app.state.hub_version, install)
            await _say_in_thread(conn, proposal_id, found)
    except ProposalError as exc:
        raise HTTPException(exc.status, exc.message) from None
    return found


@router.get("/{project}/proposals", response_model=list[BuilderProposal])
async def list_proposals(
    request: Request, project: ProjectName, user: CurrentUser, state: Literal[PROPOSAL_STATES] | None = None
) -> list[BuilderProposal]:
    """The project's proposals of @builder, the newest first; those in ``state`` alone when given."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        return await proposals.listed(conn, access.project_id, state)


# Before /{project}/proposals/{proposal_id}, which would take "waiting" for an id and refuse it.
@router.get("/{project}/proposals/waiting", response_model=list[BuilderProposal])
async def waiting_proposals(
    request: Request, project: ProjectName, user: CurrentUser, kind: Literal[PROPOSAL_KINDS] | None = None
) -> list[BuilderProposal]:
    """The project's pending proposals that the caller may decide, the newest first; those of ``kind`` alone when
    given. A writer has those of a problem and an opportunity, an intake's among them, which have no thread to answer
    in; an admin of the project has the project's others too, a hub admin those of the branding and the features; a
    reader, and the agent of a run, none."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        self_managed = (await state_of(conn, access.project_id)).enabled
        decides = [
            k
            for k in PROPOSAL_KINDS
            if kind in (None, k)
            and not proposals.refusal(user, access, k, "deciding", own=self_managed and k == proposals.PLUGIN_INSTALL)
        ]
        return await proposals.listed(conn, access.project_id, proposals.PENDING, kinds=decides) if decides else []


@router.get("/{project}/proposals/{proposal_id}", response_model=BuilderProposal, responses=REFUSALS)
async def show_proposal(
    request: Request, project: ProjectName, proposal_id: ProposalId, user: CurrentUser
) -> BuilderProposal:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        try:
            return await proposals.shown(conn, access, proposal_id)
        except ProposalError as exc:
            raise HTTPException(exc.status, exc.message) from None


@router.post("/{project}/proposals/{proposal_id}/answer", response_model=BuilderProposal, responses=REFUSALS)
async def answer_proposal(
    request: Request, project: ProjectName, proposal_id: ProposalId, body: BuilderProposalAnswer, user: CurrentUser
) -> BuilderProposal:
    """Approve or reject a pending proposal: approved, the hub applies it as the setting's next version, or does its
    action once (plugin, plugin_install, plugin_listing), or keeps why it could not (state failed). A hub admin
    decides one of the branding, the features or an install, a writer of the project one of a problem or an
    opportunity, an admin of the project the others."""
    return await _answered(request, user, project, proposal_id, body.answer)


@router.post("/{project}/proposals/{proposal_id}/rollback", response_model=BuilderProposal, responses=REFUSALS)
async def roll_back_proposal(
    request: Request, project: ProjectName, proposal_id: ProposalId, user: CurrentUser
) -> BuilderProposal:
    """Roll back an applied proposal: the setting's next version is a copy of the one before it, while the setting is
    still at the version the proposal made. An action never rolls back (409)."""
    return await _answered(request, user, project, proposal_id, ROLLBACK)

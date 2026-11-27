"""The triage chats a thread opens in (``triage.service.threads`` is the design): the project's, one for all its plans
and its runs with no plan, or the chat of a plan kept apart; made when there is none, with the run's owner and the
project's default agent put in them, by a person in the chat who clears its label."""

from __future__ import annotations

import logging
from dataclasses import replace

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats
from evo_agents.hub.access import UNTRUSTED
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.chats.models.access import NOT_A_MEMBER, ChatAccess
from evo_agents.hub.server.chats.models.views import person_author
from evo_agents.hub.server.chats.repository.access import projects_by_id
from evo_agents.hub.server.chats.repository.store import add_member, member_event
from evo_agents.hub.server.chats.service.access import chat_access
from evo_agents.hub.server.chats.service.members import label_now, put_agent, put_person
from evo_agents.hub.server.profile_store import ensure_default
from evo_agents.hub.server.run_state import RunStep
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.triage.models.threads import About
from evo_agents.hub.server.triage.repository import threads as store

log = logging.getLogger(__name__)


async def person(conn: AsyncConnection, user_id: int) -> Principal:
    """Member ``user_id`` as the hub acts for them, with no token."""
    return Principal(user_id, await store.login_of(conn, user_id), False, None, MACHINE, "")  # pyright: ignore[reportArgumentType]


async def apart(conn: AsyncConnection, found: RunStep) -> bool:
    """Whether the run's plan keeps a triage chat of its own: its label is not at or below the project's default label,
    the project's part of the label of the project's triage chat, so a person in that chat might not read its runs; or
    the plan is not on the hub, so its label is unknown. Integrity is left out: no grant or sink tells it apart."""
    label = await store.plan_label(conn, found.project_id, found.plan_id)  # pyright: ignore[reportArgumentType]
    (project,) = await projects_by_id(conn, [found.project_id])  # pyright: ignore[reportArgumentType]
    plan, default = project.rules.stored(label), project.rules.stored(project.label)
    return plan is None or default is None or not plan.below(replace(default, integrity=UNTRUSTED))


async def made(conn: AsyncConnection, found: RunStep | About, owner: Principal, plan_id: str | None) -> int:
    """The id of the triage chat of ``plan_id`` (the project's for None) the hub makes now, with ``owner`` its creator."""
    title = f"Triage: {plan_id or found.project}"[: chats.MAX_TITLE_CHARS]
    row = await store.insert_chat(conn, title, owner.user_id)
    await add_member(conn, row, user_id=owner.user_id, added_by=owner.user_id)
    await member_event(conn, row, "member_added", person_author(owner.login), owner.login)
    await store.insert_triage_chat(conn, found.project_id, plan_id, row)  # pyright: ignore[reportArgumentType]
    await Auditor(conn, found.project_id, owner).record(audit.CHAT_CREATE, f"chat:{row}")
    return row


async def as_member(conn: AsyncConnection, chat_id: int) -> tuple[ChatAccess, Principal]:
    """(chat ``chat_id``, locked, as a person in it sees it; that person), who puts the agent and the owner in it: its
    creator, else whoever of the people in it now came first, the first of them who clears its label. So a chat its
    creator left still takes threads; the ops chat of an agent system takes its threads the same way
    (``orchestrator``). The refusal of ``chat_access`` when nobody in it does."""
    refused = HTTPException(404, NOT_A_MEMBER.format(id=chat_id))
    for user_id in await store.member_ids(conn, chat_id):
        member = await person(conn, user_id)
        try:
            return await chat_access(conn, member, chat_id, lock=True), member
        except HTTPException as exc:
            refused = exc
    raise refused


async def joined(
    conn: AsyncConnection, found: RunStep | About, owner: Principal, profile, plan_id: str | None
) -> ChatAccess:
    """The triage chat of ``plan_id`` (the project's for None) as ``owner`` sees it: made when there is none; the owner
    and agent ``profile`` put in it, as the group chat's rules let them (``put_person``, ``put_agent``)."""
    row = await store.triage_chat_id(conn, found.project_id, plan_id)  # pyright: ignore[reportArgumentType]
    if row is None:
        row = await made(conn, found, owner, plan_id)
    chat, member = await as_member(conn, row)
    if await put_agent(conn, member, chat, profile):
        chat = await label_now(conn, chat)
    await put_person(conn, member, chat, owner.user_id, owner.login)
    return await chat_access(conn, owner, row)


async def chat_of(conn: AsyncConnection, found: RunStep, owner: Principal):
    """(the triage chat of the run as ``owner`` sees it, the project's default agent): the project's (``plan_id``
    NULL), the owner and the agent put in it. A run of a plan kept apart (``apart``) goes to the chat of its plan
    instead, and so does one whose owner the project's chat cannot take (the refusal of its checks, rolled back to a
    savepoint); for a run with no plan that refusal stands. One lock per project, taken before the savepoint so its
    rollback keeps it: two failures at once make one chat of each key."""
    await store.lock(conn, found.project_id)  # pyright: ignore[reportArgumentType]
    profile = await ensure_default(conn, found.project_id)  # pyright: ignore[reportArgumentType]
    if found.plan_id is not None and await apart(conn, found):
        return await joined(conn, found, owner, profile, found.plan_id), profile
    try:
        async with conn.begin_nested():
            return await joined(conn, found, owner, profile, None), profile
    except HTTPException as exc:
        if found.plan_id is None:
            raise
        log.info("triage chat of the project refused", extra={"run_id": found.run_id, "error": str(exc.detail)})
    return await joined(conn, found, owner, profile, found.plan_id), profile

"""The thread a failed run opens in the triage chat (``evo_agents.hub.tables_triage``), with no model call.

When a run of kind step, plan or author ends ``failed``, ``open_thread`` finds or makes the triage chat of its plan, or of
its project when it has no plan ("Triage: <plan>"), with the run's owner and the project's default agent in it, and
writes the first message of the thread: what the hub knows of the failure (state, cause and whether the hub guessed it,
error, plan and step, repo and branch, the last verify, links to the run's page, log and diff, and who could run it
again: ``runs.service.candidates``) and, last, the mention of the agent, which starts a chat run for the owner at hop 1
(``chat_runs.hand_work``: the chat's budget, the writer role and a worker of the owner decide, and a refusal is said in
the thread). A run has one thread: ``run_triage`` holds it, so a second call finds it. Review, judge, chat and local runs
open none, so the failure of a chat run of triage starts no triage. The switch of a project is
``projects.triage_enabled``; the hub's ``chats`` feature must be on.

``on_failed`` does it in a savepoint of the caller's transaction: whatever goes wrong inside (a label the owner does
not clear, a full chat, a database error) rolls back to it and leaves the run failed with the notice it always had.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, features, runs, tables, triage_rules
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_members import label_now, put_agent, put_person
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply
from evo_agents.hub.server.chat_store import add_member, insert_mentions, insert_message, member_event
from evo_agents.hub.server.chat_views import person_author
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.profile_store import ensure_default
from evo_agents.hub.server.run_state import RunStep
from evo_agents.hub.server.runs.service.candidates import candidates_of
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

KINDS = ("step", "plan", "author")  # the kinds of run whose failure opens a thread
ERROR_CHARS = 1200
SHOWN = 3  # candidates of each sort the message names


@dataclass(frozen=True)
class Thread:
    """The thread of a failed run: its chat, its first message and where the web shows it."""

    chat_id: int
    root_id: int
    link: str


def _thread(chat_id: int, root_id: int) -> Thread:
    return Thread(chat_id, root_id, chats.chat_link(chat_id, root_id))


async def thread_of(conn: AsyncConnection, run_id: int) -> Thread | None:
    """The thread run ``run_id`` opened, or None."""
    t = tables.run_triage
    row = (await conn.execute(select(t.c.chat_id, t.c.root_message_id).where(t.c.run_id == run_id))).one_or_none()
    return None if row is None else _thread(row.chat_id, row.root_message_id)


async def on_failed(conn: AsyncConnection, found: RunStep) -> Thread | None:
    """The thread failed run ``found`` has, opened now unless it is not due or cannot be opened; None then, and the
    caller sends the notice a failure always had."""
    if found.kind not in KINDS or found.project_id is None:
        return None
    try:
        async with conn.begin_nested():
            return await open_thread(conn, found)
    except Exception as exc:  # a thread is a help: nothing inside it may undo the failure it is about
        log.warning(
            "triage thread not opened",
            extra={"run_id": found.run_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


async def _due(conn: AsyncConnection, project_id: int) -> bool:
    instance, _ = await hub_settings.feature_values(conn)
    if not features.enabled(features.BY_ID["chats"], instance):
        return False
    p = tables.projects
    return bool((await conn.execute(select(p.c.triage_enabled).where(p.c.id == project_id))).scalar_one())


async def _inferred(conn: AsyncConnection, run_id: int) -> bool:
    e = tables.run_events
    guessed = e.c.body["failure_cause_inferred"].astext == "true"
    return bool(
        (await conn.execute(select(exists().where(e.c.run_id == run_id, e.c.kind == "system", guessed)))).scalar()
    )


async def _person(conn: AsyncConnection, user_id: int) -> Principal:
    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    return Principal(user_id, login, False, None, MACHINE, "")


async def _chat(conn: AsyncConnection, found: RunStep, owner: Principal):
    """(the triage chat of the run's plan or project as ``owner`` sees it, the person that made it): made when there is
    none; the owner and the project's default agent put in it."""
    tc = tables.triage_chats
    await conn.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"triage:{found.project_id}:{found.plan_id}", 0)))
    )
    row = (
        await conn.execute(
            select(tc.c.chat_id).where(
                tc.c.project_id == found.project_id, tc.c.plan_id.is_not_distinct_from(found.plan_id)
            )
        )
    ).scalar_one_or_none()
    profile = await ensure_default(conn, found.project_id)
    if row is None:
        title = f"Triage: {found.plan_id or found.project}"[: chats.MAX_TITLE_CHARS]
        c = tables.chats
        row = (
            await conn.execute(insert(c).values(title=title, created_by=owner.user_id).returning(c.c.id))
        ).scalar_one()
        await add_member(conn, row, user_id=owner.user_id, added_by=owner.user_id)
        await member_event(conn, row, "member_added", person_author(owner.login), owner.login)
        await conn.execute(insert(tc).values(project_id=found.project_id, plan_id=found.plan_id, chat_id=row))
        creator = owner
        await audit.record(
            conn,
            actor_id=owner.user_id,
            token_id=None,
            action=audit.CHAT_CREATE,
            target=f"chat:{row}",
            project_id=found.project_id,
        )
    else:
        c = tables.chats
        creator = await _person(conn, (await conn.execute(select(c.c.created_by).where(c.c.id == row))).scalar_one())
    chat = await chat_access(conn, creator, row, lock=True)
    if await put_agent(conn, creator, chat, profile):
        chat = await label_now(conn, chat)
    await put_person(conn, creator, chat, owner.user_id, owner.login)
    return await chat_access(conn, owner, row), profile


def _verify(found: RunStep) -> str:
    items = [item for item in found.verify or [] if isinstance(item, dict)]
    if not items:
        return "none reported"
    last = items[-1]
    return f"`{' '.join(str(last.get('command', '')).split())}` exit {last.get('exit_code')}"


def _line(text: str) -> str:
    return " ".join(str(text).split())


async def _candidates(conn: AsyncConnection, found: RunStep, role: str | None) -> list[str]:
    """The lines that say who could run the run again; one saying why there are none when they cannot be read."""
    try:
        async with conn.begin_nested():
            found_for = await candidates_of(
                conn,
                project=found.project,
                project_id=found.project_id,
                role=role or "reader",
                owner_id=found.dispatcher_id,
                kind=found.kind,
                state="failed",
                run_id=found.run_id,
            )
    except Exception as exc:
        log.warning("triage candidates not read", extra={"run_id": found.run_id, "error": type(exc).__name__})
        return ["Run again: the hub could not read who could."]
    workers = [w for w in found_for.workers if w.fit][:SHOWN]
    agents = [a for a in found_for.agents if a.fit][:SHOWN]
    lines = ["Run again with:"]
    lines += [f"- worker {w.worker}" + (" (the run's own)" if w.is_run_worker else "") for w in workers]
    lines += [f"- agent {a.handle}" for a in agents]
    if not workers:
        lines.append(
            "- no worker of the owner is fit: "
            + "; ".join(f"{w.worker} {w.reasons[0]}" for w in found_for.workers[:SHOWN] if w.reasons)
            if found_for.workers
            else "- the owner has no worker that takes this project"
        )
    return lines


async def _role(conn: AsyncConnection, user_id: int, project_id: int) -> str | None:
    g = tables.grants
    return (
        await conn.execute(select(g.c.role).where(g.c.user_id == user_id, g.c.project_id == project_id))
    ).scalar_one_or_none()


def _head(found: RunStep, inferred: bool) -> list[str]:
    what = {"plan": "Plan run", "author": "Author run"}.get(found.kind, "Run")
    where = (
        f" of step {found.step_key} of {found.plan_id}"
        if found.kind == "step"
        else (f" of {found.plan_id}" if found.plan_id else "")
    )
    cause = found.failure_cause or "unknown"
    guess = "guessed by the hub from the error" if inferred else "as the worker reported it"
    lines = [f"{what} #{found.run_id}{where} failed.", f"Cause: {cause} ({guess})."]
    error = _line(found.error or "")
    if error:
        lines.append("Error: " + runs.clip(error, ERROR_CHARS))
    place = [f"plan {found.plan_id}"] if found.plan_id else []
    place += [f"step {found.step_key}"] if found.step_key else []
    place += [f"repo {found.repo}"] if found.repo else []
    place += [f"branch {found.branch}"] if found.branch else []
    lines.append("Where: " + (", ".join(place) or found.project) + ".")
    lines.append(f"Last verify: {_verify(found)}.")
    base = f"/v1/projects/{found.project}/runs/{found.run_id}"
    lines.append(f"Run: /p/{found.project}/runs/{found.run_id}, log: {base}/events, diff: {base}/diff")
    return lines


async def chain_of(conn: AsyncConnection, run_id: int) -> tuple[int, list[str]]:
    """(how many automatic reruns led to run ``run_id``, the cause of each): the runs it reruns, by ``parent_run_id``,
    for as long as the triage of that run reran it (``run_triage.auto_reruns``). A rerun by hand starts a chain."""
    r, t = tables.runs, tables.run_triage
    causes: list[str] = []
    current = run_id
    while len(causes) <= triage_rules.RERUN_CAP:
        parent = (await conn.execute(select(r.c.parent_run_id).where(r.c.id == current))).scalar_one_or_none()
        if parent is None:
            break
        row = (await conn.execute(select(t.c.cause, t.c.auto_reruns).where(t.c.run_id == parent))).one_or_none()
        if row is None or row.auto_reruns < 1:
            break
        causes.append(row.cause or "unknown")
        current = parent
    return len(causes), causes


async def _ask_agent(
    conn: AsyncConnection,
    found: RunStep,
    chat,
    profile,
    owner: Principal,
    message_id: int,
    body: str,
) -> int | None:
    """Hand the first message of the thread to ``profile`` for the owner (hop 1); the chat run it made, if any. A
    refusal (the budget, a worker) is said in the thread."""
    kept, _, _ = await resolve_mentions(conn, chat, owner, [profile.handle])
    r = tables.runs
    via = (
        await conn.execute(select(func.coalesce(r.c.dispatched_via, MACHINE)).where(r.c.id == found.run_id))
    ).scalar_one()
    hand = Hand(chat, message_id, message_id, profile, owner.user_id, owner.login, via, 1, "the hub", body)
    await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals = [mention["refusal"] for mention in kept if mention["refusal"]]
    if refusals:
        await hub_reply(conn, chat.chat_id, message_id, "\n".join(refusals))
    return next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)


async def open_thread(conn: AsyncConnection, found: RunStep) -> Thread | None:
    """Open the thread of failed run ``found`` (the caller holds its row locked, and a savepoint to undo this); the
    thread it already has, or None when the run is not due one. A run that ends a chain of automatic reruns that is
    full (``triage_rules.RERUN_CAP``) gets the thread and the facts, and no agent: the owner decides."""
    existing = await thread_of(conn, found.run_id)
    if existing is not None or not await _due(conn, found.project_id):
        return existing
    owner = await _person(conn, found.dispatcher_id)
    chat, profile = await _chat(conn, found, owner)
    inferred = await _inferred(conn, found.run_id)
    depth, _ = await chain_of(conn, found.run_id)
    full = depth >= triage_rules.RERUN_CAP
    lines = _head(found, inferred)
    lines += await _candidates(conn, found, await _role(conn, owner.user_id, found.project_id))
    if full:
        lines.append(
            f"The hub ran this step again {depth} times by itself already, the most: it asks no agent, and the "
            "owner decides."
        )
    body = runs.clip("\n".join(lines), chats.MAX_MESSAGE_BYTES - 200) + ("" if full else f"\n\n@{profile.handle}")
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
    agent_run = None if full else await _ask_agent(conn, found, chat, profile, owner, message_id, body)
    await conn.execute(
        insert(tables.run_triage).values(
            run_id=found.run_id,
            chat_id=chat.chat_id,
            root_message_id=message_id,
            cause=found.failure_cause,
            cause_inferred=inferred,
            agent_run_id=agent_run,
            chain_depth=depth,
        )
    )
    log.info("triage thread opened", extra={"run_id": found.run_id, "chat_id": chat.chat_id, "agent_run_id": agent_run})
    return _thread(chat.chat_id, message_id)

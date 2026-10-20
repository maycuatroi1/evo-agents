"""The thread a failed run opens in the triage chat (``evo_agents.hub.tables_triage``), with no model call.

When a run of kind step, plan or author ends ``failed``, ``open_thread`` finds or makes the triage chat of its project
("Triage: <project>"), one for all its plans and its runs with no plan, with the run's owner and the project's default
agent in it, and writes the first message of the thread: what the hub knows of the failure (state, cause and whether the
hub guessed it, error, plan and step, repo and branch, the last verify, links to the run's page, log and diff, and who
could run it again: ``runs.service.candidates``) and, last, the mention of the agent, which starts a chat run for the
owner at hop 1 (``chat_runs.hand_work``: the chat's budget, the writer role and a worker of the owner decide, and a
refusal is said in the thread). A run has one thread: ``run_triage`` holds it, so a second call finds it. Review, judge,
chat and local runs open none, so the failure of a chat run of triage starts no triage. The switch of a project is
``projects.triage_enabled``; the hub's ``chats`` feature must be on. The first message names the plan and the step, so
each thread says which plan it is about.

Everyone in the project's chat clears its label, which holds the project's default label (``chat_access``), so they read
by grant what is labelled at or below it, as the project's runs on no plan are. A plan whose label is not at or below it
(a higher level or location, a label that names another project or cannot be read), or that is no longer on the hub,
keeps a chat of its own, "Triage: <plan>", as every plan had before (``_apart``): there its failures reach the owners of
its runs, who could read the plan when they dispatched them. So does a run whose owner the project's chat cannot take
(an agent of another project invited into it brings a label they do not clear, or the chat is full); a run with no plan
then opens no thread. All the threads of the project's chat share its one budget. The triage chats each plan had before
stay as they are, with their threads.

A run the hub stopped on a usage limit or a refused login (cause ``rate_limit`` or ``account_auth``, an ``account_event``
stop in its log) while no account of its owner for that variable is ready for the project gets the thread and the facts,
and no agent: the chat run of the agent would take the same accounts (``account_choice``) and fail at once. The thread
says when the first of them is ready again.

``on_failed`` does it in a savepoint of the caller's transaction: whatever goes wrong inside (a label the owner does
not clear, a full chat, a database error) rolls back to it and leaves the run failed with the notice it always had.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import case, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chats, features, runs, tables, triage_rules
from evo_agents.hub.access import UNTRUSTED
from evo_agents.hub.accounts import account_state
from evo_agents.hub.server import audit, hub_settings
from evo_agents.hub.server.chat_access import NOT_A_MEMBER, ChatAccess, chat_access, projects_by_id
from evo_agents.hub.server.chat_members import label_now, label_problems, put_agent, put_person
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply
from evo_agents.hub.server.chat_store import add_member, insert_mentions, insert_message, member_event
from evo_agents.hub.server.chat_views import person_author
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.profile_store import by_id, ensure_default
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


async def _no_account(conn: AsyncConnection, found: RunStep) -> str | None:
    """The line that says why no agent is asked, when the hub stopped run ``found`` on the accounts of a variable and
    none of its owner's accounts for it is ready for the project now (expired, paused or cooling); None otherwise."""
    if found.failure_cause not in triage_rules.NEW_ACCOUNT:
        return None
    e = tables.run_events
    stopped = e.c.body["account_event"].astext == "stop"
    names = select(e.c.body["env_var"].astext).where(e.c.run_id == found.run_id, e.c.kind == "system", stopped)
    s, a, b = tables.secrets, tables.accounts, tables.secret_bindings
    bound = exists().where(b.c.secret_id == s.c.id, b.c.project_id == found.project_id)
    now = datetime.now(UTC)
    for env_var in sorted({name for name in (await conn.execute(names)).scalars() if name}):
        rows = await conn.execute(
            select(s.c.expires_at, a.c.paused_at, a.c.cooldown_until)
            .outerjoin_from(s, a, a.c.secret_id == s.c.id)
            .where(
                s.c.owner_id == found.dispatcher_id,
                s.c.kind == "env",
                s.c.env_var == env_var,
                s.c.deleted_at.is_(None),
                bound,
            )
        )
        states = [(account_state(r.paused_at, r.cooldown_until, r.expires_at, now), r.cooldown_until) for r in rows]
        if not states or any(state == "ready" for state, _ in states):
            continue
        cooling = [until for state, until in states if state == "cooling"]
        when = f"before {min(cooling).isoformat()}" if cooling else "until the owner resumes or renews one"
        return (
            f"No account of the owner for {env_var} is ready {when}: the hub asks no agent, whose run would take "
            "the same accounts. Run it again once one is ready, or on another runtime."
        )
    return None


async def _person(conn: AsyncConnection, user_id: int) -> Principal:
    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    return Principal(user_id, login, False, None, MACHINE, "")


async def _apart(conn: AsyncConnection, found: RunStep) -> bool:
    """Whether the run's plan keeps a triage chat of its own: its label is not at or below the project's default label,
    the project's part of the label of the project's triage chat, so a person in that chat might not read its runs; or
    the plan is not on the hub, so its label is unknown. Integrity is left out: no grant or sink tells it apart."""
    pl = tables.plans
    query = select(pl.c.label).where(pl.c.project_id == found.project_id, pl.c.plan_id == found.plan_id)
    label = (await conn.execute(query)).scalar_one_or_none()
    (project,) = await projects_by_id(conn, [found.project_id])
    plan, default = project.rules.stored(label), project.rules.stored(project.label)
    return plan is None or default is None or not plan.below(replace(default, integrity=UNTRUSTED))


async def _made(conn: AsyncConnection, found: RunStep, owner: Principal, plan_id: str | None) -> int:
    """The id of the triage chat of ``plan_id`` (the project's for None) the hub makes now, with ``owner`` its creator."""
    title = f"Triage: {plan_id or found.project}"[: chats.MAX_TITLE_CHARS]
    c = tables.chats
    row = (await conn.execute(insert(c).values(title=title, created_by=owner.user_id).returning(c.c.id))).scalar_one()
    await add_member(conn, row, user_id=owner.user_id, added_by=owner.user_id)
    await member_event(conn, row, "member_added", person_author(owner.login), owner.login)
    await conn.execute(insert(tables.triage_chats).values(project_id=found.project_id, plan_id=plan_id, chat_id=row))
    await audit.record(
        conn,
        actor_id=owner.user_id,
        token_id=None,
        action=audit.CHAT_CREATE,
        target=f"chat:{row}",
        project_id=found.project_id,
    )
    return row


async def _as_member(conn: AsyncConnection, chat_id: int) -> tuple[ChatAccess, Principal]:
    """(chat ``chat_id``, locked, as a person in it sees it; that person), who puts the agent and the owner in it: its
    creator, else whoever of the people in it now came first, the first of them who clears its label. So a chat its
    creator left still takes threads. The refusal of ``chat_access`` when nobody in it does."""
    c, m = tables.chats, tables.chat_members
    query = (
        select(m.c.user_id)
        .join_from(m, c, c.c.id == m.c.chat_id)
        .where(m.c.chat_id == chat_id, m.c.user_id.is_not(None), m.c.removed_at.is_(None))
        .order_by(case((m.c.user_id == c.c.created_by, 0), else_=1), m.c.added_at, m.c.id)
    )
    refused = HTTPException(404, NOT_A_MEMBER.format(id=chat_id))
    for user_id in (await conn.execute(query)).scalars().all():
        person = await _person(conn, user_id)
        try:
            return await chat_access(conn, person, chat_id, lock=True), person
        except HTTPException as exc:
            refused = exc
    raise refused


async def _joined(conn: AsyncConnection, found: RunStep, owner: Principal, profile, plan_id: str | None) -> ChatAccess:
    """The triage chat of ``plan_id`` (the project's for None) as ``owner`` sees it: made when there is none; the owner
    and agent ``profile`` put in it, as the group chat's rules let them (``put_person``, ``put_agent``)."""
    tc = tables.triage_chats
    query = select(tc.c.chat_id).where(tc.c.project_id == found.project_id, tc.c.plan_id.is_not_distinct_from(plan_id))
    row = (await conn.execute(query)).scalar_one_or_none()
    if row is None:
        row = await _made(conn, found, owner, plan_id)
    chat, member = await _as_member(conn, row)
    if await put_agent(conn, member, chat, profile):
        chat = await label_now(conn, chat)
    await put_person(conn, member, chat, owner.user_id, owner.login)
    return await chat_access(conn, owner, row)


async def _chat(conn: AsyncConnection, found: RunStep, owner: Principal):
    """(the triage chat of the run as ``owner`` sees it, the project's default agent): the project's (``plan_id``
    NULL), the owner and the agent put in it. A run of a plan kept apart (``_apart``) goes to the chat of its plan
    instead, and so does one whose owner the project's chat cannot take (the refusal of its checks, rolled back to a
    savepoint); for a run with no plan that refusal stands. One lock per project, taken before the savepoint so its
    rollback keeps it: two failures at once make one chat of each key."""
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"triage:{found.project_id}", 0))))
    profile = await ensure_default(conn, found.project_id)
    if found.plan_id is not None and await _apart(conn, found):
        return await _joined(conn, found, owner, profile, found.plan_id), profile
    try:
        async with conn.begin_nested():
            return await _joined(conn, found, owner, profile, None), profile
    except HTTPException as exc:
        if found.plan_id is None:
            raise
        log.info("triage chat of the project refused", extra={"run_id": found.run_id, "error": str(exc.detail)})
    return await _joined(conn, found, owner, profile, found.plan_id), profile


def _verify(found: RunStep) -> str:
    items = [item for item in found.verify or [] if isinstance(item, dict)]
    if not items:
        return "none reported"
    last = items[-1]
    return f"`{' '.join(str(last.get('command', '')).split())}` exit {last.get('exit_code')}"


def _line(text: str) -> str:
    return " ".join(str(text).split())


async def _candidates(conn: AsyncConnection, found: RunStep, role: str | None, chat) -> list[str]:
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
    agents = [a for a in found_for.agents if a.fit and a.project == found.project][:SHOWN]
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
    return lines + await _other_projects(conn, found_for, chat)


async def _other_projects(conn: AsyncConnection, found_for, chat) -> list[str]:
    """The lines about the projects the error names: whose agent can be invited, and what stops it for the others (no
    writer role, a label of the chat someone does not clear). Nobody is invited here."""
    if not found_for.other_projects:
        return []
    lines = ["The error names another project:"]
    for other in found_for.other_projects:
        fit = [a for a in found_for.agents if a.project == other.project and a.fit]
        if not other.fit or not fit:
            why = (
                other.reasons[0]
                if not other.fit
                else f"no agent of {other.project} has a worker of the owner to run on"
            )
            lines.append(f"- {other.project} (the error names {other.named}): no one is invited: {why}.")
            continue
        profile = await by_id(conn, fit[0].profile_id)
        try:
            async with conn.begin_nested():
                problems = await label_problems(conn, chat, profile) if profile is not None else []
        except Exception as exc:
            log.warning("triage label not read", extra={"error": type(exc).__name__})
            lines.append(f"- {other.project}: the hub could not read the chat's label. No one is invited.")
            continue
        if problems:
            lines.append(
                f"- {other.project} (the error names {other.named}): agent {fit[0].handle} cannot join, the chat's label "
                f"would pass someone in it: {'; '.join(problems)}. No one is invited."
            )
        else:
            lines.append(
                f"- {other.project} (the error names {other.named}): the owner is a writer there; the agent of this "
                f"thread may invite one of its agents ({', '.join(a.handle for a in fit[:SHOWN])}) with `evo-agents worker triage invite`."
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
    full (``triage_rules.RERUN_CAP``) gets the thread and the facts, and no agent: the owner decides. So does one the
    hub stopped on accounts of which none is ready (``_no_account``)."""
    existing = await thread_of(conn, found.run_id)
    if existing is not None or not await _due(conn, found.project_id):
        return existing
    owner = await _person(conn, found.dispatcher_id)
    chat, profile = await _chat(conn, found, owner)
    inferred = await _inferred(conn, found.run_id)
    depth, _ = await chain_of(conn, found.run_id)
    full = depth >= triage_rules.RERUN_CAP
    waiting = None if full else await _no_account(conn, found)
    lines = _head(found, inferred)
    lines += await _candidates(conn, found, await _role(conn, owner.user_id, found.project_id), chat)
    if full:
        lines.append(
            f"The hub ran this step again {depth} times by itself already, the most: it asks no agent, and the "
            "owner decides."
        )
    elif waiting is not None:
        lines.append(waiting)
    ask = not full and waiting is None
    body = runs.clip("\n".join(lines), chats.MAX_MESSAGE_BYTES - 200) + (f"\n\n@{profile.handle}" if ask else "")
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
    agent_run = await _ask_agent(conn, found, chat, profile, owner, message_id, body) if ask else None
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

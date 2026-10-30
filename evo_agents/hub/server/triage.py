"""The thread a failed run opens in the triage chat (``evo_agents.hub.tables_triage``), with no model call.

When a run of kind step, plan or author ends ``failed``, ``open_thread`` finds or makes the triage chat of its project
("Triage: <project>"), one for all its plans and its runs with no plan, with the run's owner and the project's default
agent in it, and writes the first message of the thread: what the hub knows of the failure (state, cause and whether the
hub guessed it, error, plan and step, repo and branch, the last verify, links to the run's page, log and diff, and who
could run it again: ``runs.service.candidates``) and, last, the mention of the agent, which starts a chat run for the
owner at hop 1 (``chat_runs.hand_work``: the writer role and a worker of the owner decide, and a refusal is said in
the thread). A run has one thread: ``run_triage`` holds it, so a second call finds it. Review, judge,
chat and local runs open none, so the failure of a chat run of triage starts no triage. The switch of a project is
``projects.triage_enabled``; the hub's ``chats`` feature must be on. The first message names the plan and the step, so
each thread says which plan it is about.

Everyone in the project's chat clears its label, which holds the project's default label (``chat_access``), so they read
by grant what is labelled at or below it, as the project's runs on no plan are. A plan whose label is not at or below it
(a higher level or location, a label that names another project or cannot be read), or that is no longer on the hub,
keeps a chat of its own, "Triage: <plan>", as every plan had before (``_apart``): there its failures reach the owners of
its runs, who could read the plan when they dispatched them. So does a run whose owner the project's chat cannot take
(an agent of another project invited into it brings a label they do not clear, or the chat is full); a run with no plan
then opens no thread. The triage chats each plan had before stay as they are, with their threads.

A run the hub stopped on a usage limit or a refused login (cause ``rate_limit`` or ``account_auth``, an ``account_event``
stop in its log) while no account of its owner for that variable is ready for the project gets the thread and the facts,
and no agent: the chat run of the agent would take the same accounts (``account_choice``) and fail at once. The thread
says when the first of them is ready again.

A run that stops moving gets a thread too, once (``run_triage`` holds it): one running or verifying that has written
no event for STALE_MINUTES while its worker still heartbeats, which each pass of the reaper finds (``open_stale``), with
the cause ``stale``; and one the reaper failed on its last attempt (cause ``lease_lost``), as any failed run. The first
message of both adds the diagnosis (``_diagnosis``): the last event, the worker and its last heartbeat, the lease and
the attempt. The agent of a stale run's thread may ask its worker to stop it (``triage_actions``, ``triage/cancel``).

``on_failed`` does it in a savepoint of the caller's transaction: whatever goes wrong inside (a label the owner does
not clear, a full chat, a database error) rolls back to it and leaves the run failed with the notice it always had.

``project_thread`` is the hub's thread of something that is no failed run, a release cycle that stopped
(``release_cycles``): the hub writes it in the project's triage chat, with its owner put in it, and mentions no agent,
since the owner decides. ``asking_thread`` is the same thread with the project's default agent mentioned, and
``tell_thread`` adds the hub's message to the thread a failed run opened: the triage of a work item of the Loop that
failed twice (``loop.stall``). ``asking_reply`` adds the hub's message to a thread ``asking_thread`` opened and mentions
the agent again: a proposal of the Curator open again after a defer (``curator_threads``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import case, exists, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

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
from evo_agents.hub.server.run_state import RunStep, run_step
from evo_agents.hub.server.runs.service.candidates import candidates_of
from evo_agents.hub.server.security import MACHINE, Principal

log = logging.getLogger(__name__)

KINDS = ("step", "plan", "author")  # the kinds of run whose failure opens a thread
ERROR_CHARS = 1200
SHOWN = 3  # candidates of each sort the message names
STALE_MINUTES = 45  # a run running or verifying with no event this long, while its worker heartbeats, opens a thread
STALE_STATES = ("running", "verifying")  # the held states a stale run is in: a waiting or interactive run is not stuck
STALE_CAUSE = "stale"  # the cause run_triage holds for the thread of a stale run
STALE_BATCH = 50  # stale runs one pass of the reaper opens a thread for at most; the next pass takes the rest
EVENT_CHARS = 300  # how much of the last event the diagnosis quotes


@dataclass(frozen=True)
class Thread:
    """The thread of a failed run: its chat, its first message and where the web shows it."""

    chat_id: int
    root_id: int
    link: str


@dataclass(frozen=True)
class About:
    """What the chat helpers read of what a thread is about, when it is no run: its project."""

    project_id: int
    project: str
    plan_id: str | None = None
    run_id: int | None = None


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
            select(s.c.expires_at, a.c.oauth_sealed, a.c.paused_at, a.c.cooldown_until)
            .outerjoin_from(s, a, a.c.secret_id == s.c.id)
            .where(
                s.c.owner_id == found.dispatcher_id,
                s.c.kind == "env",
                s.c.env_var == env_var,
                s.c.deleted_at.is_(None),
                bound,
            )
        )
        states = [
            (
                account_state(
                    r.paused_at,
                    r.cooldown_until,
                    None if r.oauth_sealed is not None else r.expires_at,  # an OAuth token is renewed at lease time
                    now,
                ),
                r.cooldown_until,
            )
            for r in rows
        ]
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


async def _made(conn: AsyncConnection, found: RunStep | About, owner: Principal, plan_id: str | None) -> int:
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


async def _joined(
    conn: AsyncConnection, found: RunStep | About, owner: Principal, profile, plan_id: str | None
) -> ChatAccess:
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


def _lock(project_id: int):
    return select(func.pg_advisory_xact_lock(func.hashtextextended(f"triage:{project_id}", 0)))


async def project_thread(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, body: str
) -> Thread | None:
    """A thread of the hub's in the project's triage chat whose first message is ``body``, the member ``owner_id``
    put in the chat, no agent mentioned; None while the project's triage, or the hub's chats, are off. The caller
    holds a savepoint: a chat that cannot take the owner raises."""
    if not await _due(conn, project_id):
        return None
    owner = await _person(conn, owner_id)
    await conn.execute(_lock(project_id))
    profile = await ensure_default(conn, project_id)
    chat = await _joined(conn, About(project_id, project), owner, profile, None)
    message_id, _ = await insert_message(
        conn, chat, runs.clip(body, chats.MAX_MESSAGE_BYTES - 200), thread_root_id=None
    )
    log.info("triage thread opened", extra={"project": project, "chat_id": chat.chat_id, "message_id": message_id})
    return _thread(chat.chat_id, message_id)


async def asking_thread(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, body: str
) -> tuple[Thread, int | None] | None:
    """``project_thread`` with the project's default agent mentioned, as the thread of a failed run has it: (the
    thread, the chat run it starts for the owner at hop 1, None when that is refused, which the thread says); None
    while the project's triage, or the hub's chats, are off. The Loop's triage of a work item whose run opened no
    thread of its own (``loop.stall``). The caller holds a savepoint."""
    if not await _due(conn, project_id):
        return None
    owner = await _person(conn, owner_id)
    await conn.execute(_lock(project_id))
    profile = await ensure_default(conn, project_id)
    chat = await _joined(conn, About(project_id, project), owner, profile, None)
    text = runs.clip(body, chats.MAX_MESSAGE_BYTES - 200) + f"\n\n@{profile.handle}"
    message_id, _ = await insert_message(conn, chat, text, thread_root_id=None)
    agent_run = await _ask_agent(conn, None, chat, profile, owner, message_id, text)
    log.info("triage thread opened", extra={"project": project, "chat_id": chat.chat_id, "agent_run_id": agent_run})
    return _thread(chat.chat_id, message_id), agent_run


async def asking_reply(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, thread: Thread, body: str
) -> tuple[Thread, int | None] | None:
    """A message of the hub in ``thread`` of the project's triage chat that mentions the project's default agent, as
    the first message of ``asking_thread`` does: (the thread, the chat run it reaches for the owner, a new one or the
    session's, which the message goes on in; None when that is refused, which the thread says); None while the
    project's triage, or the hub's chats, are off, or when ``thread`` is not in the project's triage chat. The caller
    holds a savepoint."""
    if not await _due(conn, project_id):
        return None
    owner = await _person(conn, owner_id)
    await conn.execute(_lock(project_id))
    profile = await ensure_default(conn, project_id)
    chat = await _joined(conn, About(project_id, project), owner, profile, None)
    if chat.chat_id != thread.chat_id:
        return None
    text = runs.clip(body, chats.MAX_MESSAGE_BYTES - 200) + f"\n\n@{profile.handle}"
    message_id, _ = await insert_message(conn, chat, text, thread_root_id=thread.root_id)
    agent_run = await _ask_agent(conn, None, chat, profile, owner, message_id, text, root_id=thread.root_id)
    log.info(
        "triage thread asked again", extra={"project": project, "chat_id": chat.chat_id, "agent_run_id": agent_run}
    )
    return thread, agent_run


async def tell_thread(conn: AsyncConnection, run_id: int, body: str) -> tuple[Thread, int | None] | None:
    """A message of the hub in the thread failed run ``run_id`` opened: (the thread, the chat run of its agent); None
    when the run opened none. The caller holds a savepoint."""
    thread = await thread_of(conn, run_id)
    if thread is None:
        return None
    t = tables.run_triage
    agent_run = (await conn.execute(select(t.c.agent_run_id).where(t.c.run_id == run_id))).scalar_one_or_none()
    await hub_reply(conn, thread.chat_id, thread.root_id, runs.clip(body, chats.MAX_MESSAGE_BYTES - 200))
    return thread, agent_run


async def _chat(conn: AsyncConnection, found: RunStep, owner: Principal):
    """(the triage chat of the run as ``owner`` sees it, the project's default agent): the project's (``plan_id``
    NULL), the owner and the agent put in it. A run of a plan kept apart (``_apart``) goes to the chat of its plan
    instead, and so does one whose owner the project's chat cannot take (the refusal of its checks, rolled back to a
    savepoint); for a run with no plan that refusal stands. One lock per project, taken before the savepoint so its
    rollback keeps it: two failures at once make one chat of each key."""
    await conn.execute(_lock(found.project_id))
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


def _head(found: RunStep, inferred: bool, *, stale: bool = False) -> list[str]:
    what = {"plan": "Plan run", "author": "Author run"}.get(found.kind, "Run")
    where = (
        f" of step {found.step_key} of {found.plan_id}"
        if found.kind == "step"
        else (f" of {found.plan_id}" if found.plan_id else "")
    )
    if stale:
        lines = [
            f"{what} #{found.run_id}{where} has written no event for {STALE_MINUTES} minutes while its worker still "
            "sends heartbeats.",
            f"Cause: {STALE_CAUSE} (seen by the hub; the run still holds its worker).",
        ]
    else:
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


def _when(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "never"
    minutes = max(0, int((now - at).total_seconds() // 60))
    return f"{at.astimezone(UTC):%Y-%m-%d %H:%M} UTC, {minutes} min ago"


def _event_text(kind: str, body) -> str:
    body = body if isinstance(body, dict) else {}
    if kind == "state":
        text = f"{body.get('from')} -> {body.get('to')}" + (f": {body['reason']}" if body.get("reason") else "")
    else:
        text = next((str(body[key]) for key in ("text", "message", "summary", "tool") if body.get(key)), "")
    return runs.clip(_line(text), EVENT_CHARS)


async def _diagnosis(conn: AsyncConnection, found: RunStep) -> list[str]:
    """The lines of a run that stopped moving, stale or lost on its last attempt: its last event (the hub's own move to
    the end it is in left out), its worker and that worker's last heartbeat, its lease and its attempt."""
    r, w, e = tables.runs, tables.workers, tables.run_events
    row = (
        await conn.execute(
            select(r.c.state, r.c.lease_expires_at, r.c.finished_at, w.c.last_heartbeat_at, func.now().label("now"))
            .select_from(r.outerjoin(w, w.c.id == r.c.worker_id))
            .where(r.c.id == found.run_id)
        )
    ).one()
    events = select(e.c.seq, e.c.at, e.c.kind, e.c.body).where(e.c.run_id == found.run_id)
    if row.state in runs.TERMINAL_STATES:  # the hub's move to that end, and what it wrote with it, left out
        end = select(func.max(e.c.seq)).where(e.c.run_id == found.run_id, e.c.kind == "state").scalar_subquery()
        events = events.where(e.c.seq < end)
    tail = (await conn.execute(events.order_by(e.c.seq.desc()).limit(1))).all()
    if tail:
        last = tail[0]
        said = _event_text(last.kind, last.body)
        lines = [f"Last event: #{last.seq} {last.kind} at {_when(last.at, row.now)}" + (f": {said}" if said else "")]
    else:
        lines = ["Last event: none."]
    if found.worker:
        lines.append(f"Worker: {found.worker}, last heartbeat {_when(row.last_heartbeat_at, row.now)}.")
    else:
        lines.append("Worker: none.")
    if row.state in runs.HELD_STATES:
        until = (
            "no end" if row.lease_expires_at is None else f"{row.lease_expires_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        )
        lines.append(f"Lease: held until {until}; the worker still extends it.")
    else:
        lines.append(
            f"Lease: ended with the run at {_when(row.finished_at, row.now)}; the worker stopped extending it."
        )
    last_try = " (the last)" if found.attempt >= found.max_attempts else ""
    lines.append(f"Attempt: {found.attempt} of {found.max_attempts}{last_try}.")
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
    found: RunStep | None,
    chat,
    profile,
    owner: Principal,
    message_id: int,
    body: str,
    *,
    root_id: int | None = None,
) -> int | None:
    """Hand message ``message_id``, the first of the thread or one in the thread of ``root_id``, to ``profile`` for the
    owner (hop 1), with the credential failed run ``found`` was dispatched with (a machine's without one); the chat run
    it made, if any. A refusal (the writer role, a worker) is said in the thread."""
    kept, _, _ = await resolve_mentions(conn, chat, owner, [profile.handle])
    r = tables.runs
    via = MACHINE
    if found is not None:
        query = select(func.coalesce(r.c.dispatched_via, MACHINE)).where(r.c.id == found.run_id)
        via = (await conn.execute(query)).scalar_one()
    root = message_id if root_id is None else root_id
    hand = Hand(chat, message_id, root, profile, owner.user_id, owner.login, via, 1, "the hub", body)
    await hand_mentions(conn, hand, kept)
    await insert_mentions(conn, message_id, kept)
    refusals = [mention["refusal"] for mention in kept if mention["refusal"]]
    if refusals:
        await hub_reply(conn, chat.chat_id, root, "\n".join(refusals))
    return next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)


async def open_thread(conn: AsyncConnection, found: RunStep, *, stale: bool = False) -> Thread | None:
    """Open the thread of failed run ``found``, or of held run ``found`` that is ``stale`` (``open_stale``), the caller
    holding its row locked and a savepoint to undo this; the thread it already has, or None when the run is not due
    one. A run that ends a chain of automatic reruns that is full (``triage_rules.RERUN_CAP``) gets the thread and the
    facts, and no agent: the owner decides. So does one the hub stopped on accounts of which none is ready
    (``_no_account``). A stale run, and one the reaper failed on its last attempt (cause ``lease_lost``), also get the
    diagnosis: the last event, the worker, the lease and the attempt (``_diagnosis``)."""
    existing = await thread_of(conn, found.run_id)
    if existing is not None or not await _due(conn, found.project_id):
        return existing
    owner = await _person(conn, found.dispatcher_id)
    chat, profile = await _chat(conn, found, owner)
    inferred = False if stale else await _inferred(conn, found.run_id)
    depth, _ = await chain_of(conn, found.run_id)
    full = depth >= triage_rules.RERUN_CAP
    waiting = None if full or stale else await _no_account(conn, found)
    lines = _head(found, inferred, stale=stale)
    if stale or found.failure_cause == "lease_lost":
        lines += await _diagnosis(conn, found)
    if stale:
        lines.append(
            "The agent of this thread may ask its worker to stop it (`evo-agents worker triage cancel`) and, once it "
            "has ended, run it again on a fit worker (`evo-agents worker triage rerun`)."
        )
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
            cause=STALE_CAUSE if stale else found.failure_cause,
            cause_inferred=inferred,
            agent_run_id=agent_run,
            chain_depth=depth,
        )
    )
    log.info(
        "triage thread opened",
        extra={"run_id": found.run_id, "chat_id": chat.chat_id, "agent_run_id": agent_run, "stale": stale},
    )
    return _thread(chat.chat_id, message_id)


def _stale_runs():
    """The runs of KINDS running or verifying on a live worker that still heartbeats (within
    ``runs.OFFLINE_AFTER_SECONDS``), of a project with triage on, with no thread yet, whose last event, else start, is
    more than STALE_MINUTES old."""
    r, w, e, p, t = tables.runs, tables.workers, tables.run_events, tables.projects, tables.run_triage
    last = select(e.c.at).where(e.c.run_id == r.c.id).order_by(e.c.seq.desc()).limit(1).scalar_subquery()
    quiet = func.coalesce(last, r.c.started_at, r.c.leased_at)
    return (
        select(r.c.id)
        .join_from(r, w, w.c.id == r.c.worker_id)
        .join(p, p.c.id == r.c.project_id)
        .where(
            r.c.state.in_(STALE_STATES),
            r.c.kind.in_(KINDS),
            p.c.triage_enabled,
            w.c.revoked_at.is_(None),
            w.c.last_heartbeat_at > func.now() - timedelta(seconds=runs.OFFLINE_AFTER_SECONDS),
            quiet < func.now() - timedelta(minutes=STALE_MINUTES),
            ~exists().where(t.c.run_id == r.c.id),
        )
    )


async def on_stale(conn: AsyncConnection, found: RunStep) -> Thread | None:
    """The thread stale run ``found`` has, opened now unless it cannot be; in a savepoint, as ``on_failed``."""
    if found.kind not in KINDS or found.project_id is None:
        return None
    try:
        async with conn.begin_nested():
            return await open_thread(conn, found, stale=True)
    except Exception as exc:  # a thread is a help: nothing inside it may stop the reaper's pass
        log.warning(
            "stale triage thread not opened",
            extra={"run_id": found.run_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


async def open_stale(engine: AsyncEngine, batch: int = STALE_BATCH) -> int:
    """One pass, of the reaper (``run_state.recover_runs``): a thread for each stale run (``_stale_runs``), each in a
    transaction of its own that locks the run and checks again; a run has one thread, so it is opened once. How many
    were opened."""
    r = tables.runs
    async with engine.connect() as conn:
        found = (await conn.execute(_stale_runs().order_by(r.c.id).limit(batch))).scalars().all()
    opened = 0
    for run_id in found:
        async with engine.begin() as conn:
            lock = _stale_runs().where(r.c.id == run_id).with_for_update(of=r, skip_locked=True)
            if (await conn.execute(lock)).one_or_none() is None:  # it moved, wrote an event or has a thread now
                continue
            thread = await on_stale(conn, await run_step(conn, run_id))
            opened += thread is not None
    return opened

"""Chat runs on the hub (``evo_agents.hub.chat_runs``): the run a mention of an agent in a group chat makes, the worker
it goes to, the session it goes on in, the chat's budget, the agent's messages in the thread and the reactions that
follow the run.

``hand_work`` is what a mention asks, of a person (``hand_mentions``, from ``chats``) or of an agent
(``chat_agents``): the run of the profile's agent for the person at the origin of the chain. Refused, with why, when
that person is not a writer on the profile's project, when the chat's budget is spent, or when no worker of the person
can take it now. Each (thread, agent, person) is one session: a run of it that is queued or held takes the message in
its inbox; one that ended done is gone on from by a new run, pinned to its worker, in its session, with the message in
its inbox; else a new session starts on the person's worker the profile allows, with the most of the project's repos
checked out first, on the branch ``chat/<run>``.

A chat run's moves (``chat_run_moved``, from ``run_state.record_move``) put the agent's reaction on the message that
started it by rule (``chat_store.status_reaction``); a run that failed says why in the thread, and one that ended
done with messages of its inbox untaken goes on in its session at once, with them. Its prompt is built when it is
claimed (``chat_prompt``). The agent's reply at the end of each turn, and the messages of its commands, are messages of
its profile in the thread (``post_agent``): a person it mentions gets the notice, an agent none, and no run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from sqlalchemy import exists, func, insert, literal, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, curator, profiles, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.author import harness_repo
from evo_agents.hub.server.chat_access import ChatAccess, agent_projects, label_reader
from evo_agents.hub.server.chat_notices import notice_mention
from evo_agents.hub.server.chat_store import insert_mentions, insert_message, status_reaction
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import notify_queued, write_event
from evo_agents.hub.server.runs.models import available
from evo_agents.hub.server.runs.service.dispatch import insert_run

log = logging.getLogger(__name__)

OFFLINE = func.make_interval(0, 0, 0, 0, 0, 0, runs.OFFLINE_AFTER_SECONDS)
WINDOW = func.make_interval(0, 0, 0, 0, rules.BUDGET_HOURS)
AGENT_REPLY_REFUSAL = f"an agent's @ in a message hands no work: it uses `{rules.MENTION_COMMAND}`"


@dataclass(frozen=True)
class Hand:
    """What one mention asks: work of ``profile`` for ``dispatcher_id`` (``dispatcher``), the person at the origin of
    the chain, at ``hop``, in the thread of ``root_id`` of ``chat``, woken by message ``message_id`` that ``author``
    wrote, with the credential kind ``via`` (runs.dispatched_via)."""

    chat: ChatAccess
    message_id: int
    root_id: int
    profile: Profile
    dispatcher_id: int
    dispatcher: str
    via: str
    hop: int
    author: str
    body: str


# The chat's budget


async def chat_spend(conn: AsyncConnection, chat_id: int) -> tuple[float, float]:
    """(what the chat runs of chat ``chat_id`` queued in the last BUDGET_HOURS cost, its budget). A run that went on
    in the session of another counts what its session cost after that run's (Claude Code's total is the session's)."""
    r, before, c = tables.runs, tables.runs.alias("before"), tables.chats
    query = (
        select(r.c.usage, before.c.usage.label("before"))
        .select_from(r.outerjoin(before, before.c.id == r.c.resume_of_run_id))
        .where(r.c.chat_id == chat_id, r.c.queued_at > func.now() - WINDOW)
    )
    spent = sum(
        max(curator.run_cost(row.usage) - curator.run_cost(row.before), 0.0) for row in await conn.execute(query)
    )
    budget = (await conn.execute(select(c.c.budget_usd).where(c.c.id == chat_id))).scalar_one()
    return round(spent, 4), float(budget)


def _spent(spent: float, budget: float) -> str:
    return (
        f"the chat's budget of {curator.money(budget)} for {rules.BUDGET_HOURS} hours is spent: its agents' runs cost "
        f"{curator.money(spent)} in that time; the chat's creator may raise it"
    )


# The worker


def _workers(user_id: int, project_id: int):
    """The workers of ``user_id`` that serve project ``project_id`` and are not revoked, with how many runs each
    holds or has waiting for it (queued, pinned to it), and whether it is online."""
    w, wp, r = tables.workers, tables.worker_projects, tables.runs
    held = (r.c.worker_id == w.c.id) & r.c.state.in_(runs.HELD_STATES)
    waiting = (r.c.pinned_worker_id == w.c.id) & (r.c.state == "queued")
    holding = select(func.count()).where(or_(held, waiting)).scalar_subquery()
    serves = exists().where(wp.c.worker_id == w.c.id, wp.c.project_id == project_id)
    return (
        select(
            w.c.id,
            w.c.name,
            w.c.labels,
            w.c.runtimes,
            w.c.checkouts,
            w.c.features,
            w.c.run_kinds,
            w.c.slots,
            w.c.dispatch_from,
            w.c.agent_version,
            w.c.drained_at,
            (w.c.last_heartbeat_at >= func.now() - OFFLINE).label("online"),
            holding.label("holding"),
        )
        .where(w.c.owner_id == user_id, w.c.revoked_at.is_(None), serves)
        .order_by(w.c.id)
    )


def unfit_worker(row, profile: Profile, via: str) -> str | None:
    """Why worker ``row`` (``_workers``) cannot take a chat run of ``profile`` dispatched with ``via`` now, or None."""
    allow = profiles.allowed(profile.permissions, "runtime")
    usable = [name for name in runs.RUNTIMES if available((row.runtimes or {}).get(name))]
    checks = (
        (not row.online, "is offline"),
        (row.drained_at is not None, "is draining"),
        ("chat" not in (row.run_kinds or ()), f"runs evo-agents {row.agent_version or '?'}, which takes no chat run"),
        (row.dispatch_from == "web" and via != "web", "takes runs dispatched from the web only"),
        (row.holding >= row.slots, "has no free slot"),  # its runs and the runs queued for it
        (not row.checkouts, "has no checkout, and claims nothing"),
        (
            not profiles.allows_worker(profile.permissions, row.name, row.labels or ()),
            f"is not one @{profile.handle} allows",
        ),
        (not [name for name in usable if allow is None or name in allow], f"has no runtime @{profile.handle} allows"),
        (
            profiles.needs_worker(profile.permissions, profile.instructions)
            and runs.AGENT_PROFILES not in (row.features or ()),
            "has a daemon that does not apply agent profiles",
        ),
    )
    return next((why for failed, why in checks if failed), None)


async def project_repos(conn: AsyncConnection, project_id: int) -> list[str]:
    """The repos of a project a chat run works in: its harness, then the repos it lists, by name."""
    p, pr = tables.projects, tables.project_repos
    path = (await conn.execute(select(p.c.harness_path).where(p.c.id == project_id))).scalar_one_or_none()
    names = (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).scalars()
    harness = harness_repo(path)
    return list(dict.fromkeys([*([harness] if harness else []), *names]))


async def pick_worker(conn: AsyncConnection, hand: Hand):
    """(the worker a new session goes to, None) or (None, why none can take it): of the dispatcher, fit for the
    profile (``unfit_worker``), the one with a checkout of the most repos of the project, then the most free slots."""
    profile = hand.profile
    rows = (await conn.execute(_workers(hand.dispatcher_id, profile.project_id))).all()
    if not rows:
        return None, (
            f"{hand.dispatcher} has no worker that takes runs of project {profile.project}, which @{profile.handle} "
            "works in: join one, or add the project to one"
        )
    repos = await project_repos(conn, profile.project_id)
    fit, unfit = [], []
    for row in rows:
        why = unfit_worker(row, profile, hand.via)
        if why is None:
            fit.append(row)
        else:
            unfit.append(f"{row.name} {why}")
    if not fit:
        return None, f"no worker of {hand.dispatcher} can take a run of @{profile.handle} now: {'; '.join(unfit)}"

    def rank(row):
        held = sum(1 for name in repos if f"{profile.project}/{name}" in (row.checkouts or {}))
        return (-held, row.holding - row.slots, row.id)

    return min(fit, key=rank), None


# Sessions


def _sessions(chat_id: int, root_id: int, dispatcher_id: int, profile_id: int | None = None):
    """The chat runs in the thread of ``root_id`` dispatched by ``dispatcher_id`` (of ``profile_id`` when given), the
    latest first, with the state of their worker."""
    r, m, w = tables.runs, tables.chat_messages, tables.workers
    query = (
        select(
            r.c.id,
            r.c.state,
            r.c.profile_id,
            r.c.worker_id,
            r.c.session_id,
            r.c.usage,
            w.c.revoked_at.is_not(None).label("revoked"),
        )
        .select_from(r.join(m, m.c.id == r.c.chat_message_id).outerjoin(w, w.c.id == r.c.worker_id))
        .where(
            r.c.chat_id == chat_id,
            r.c.dispatched_by == dispatcher_id,
            func.coalesce(m.c.thread_root_id, m.c.id) == root_id,
        )
        .order_by(r.c.id.desc())
    )
    return query if profile_id is None else query.where(r.c.profile_id == profile_id)


async def thread_session(conn: AsyncConnection, chat_id: int, root_id: int, dispatcher_id: int) -> int | None:
    """The one agent with a session of ``dispatcher_id`` in the thread of ``root_id``, its profile id; None when there
    is none or more than one: a message without an @ in a chat of one person goes to it."""
    found = {row.profile_id for row in await conn.execute(_sessions(chat_id, root_id, dispatcher_id))}
    return found.pop() if len(found) == 1 else None


async def _to_inbox(conn: AsyncConnection, run_id: int, hand: Hand) -> None:
    """Leave the message of ``hand`` in the inbox of run ``run_id``, which the worker hands to the agent."""
    i = tables.run_inbox
    text = rules.inbox_text(hand.author, hand.message_id, hand.body)
    left = insert(i).values(run_id=run_id, sent_by=hand.dispatcher_id, body=text).returning(i.c.id)
    inbox_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": hand.dispatcher, "message_id": inbox_id, "chat_message_id": hand.message_id}
    await write_user_message(conn, run_id, event)


def _continue_run(previous: int, values: dict):
    """The run that goes on in the session of chat run ``previous``: its project, profile, repos, branch, runtime and
    model, pinned to its worker, in its session; ``values`` names the rest."""
    r = tables.runs
    copied = ("kind", "project_id", "title", "mode", "approval", "timeout_s", "repos", "branch", "model", "chat_id")
    copied += ("profile_id", "profile_version", "session_id", "runtime")
    source = select(
        *(r.c[name] for name in copied),
        r.c.worker_id.label("pinned_worker_id"),
        r.c.runtime.label("requested_runtime"),
        r.c.id.label("resume_of_run_id"),
        *(literal(value, r.c[name].type).label(name) for name, value in values.items()),
    ).where(r.c.id == previous)
    columns = [*copied, "pinned_worker_id", "requested_runtime", "resume_of_run_id", *values]
    return insert(r).from_select(columns, source).returning(r.c.id)


async def _go_on(conn: AsyncConnection, previous, hand: Hand, left: float) -> int:
    """Queue the run that goes on in the session of chat run ``previous`` with the message of ``hand``."""
    budget = rules.run_budget(left + curator.run_cost(previous.usage))  # the claim takes the session's cost off
    values = {
        "dispatched_by": hand.dispatcher_id,
        "dispatched_via": hand.via,
        "chat_message_id": hand.message_id,
        "hop": hand.hop,
        "budget": budget,
    }
    new_id = (await conn.execute(_continue_run(previous.id, values))).scalar_one()
    await _to_inbox(conn, new_id, hand)
    shown = f"goes on in the session of run #{previous.id}, as {hand.author} wrote in the thread"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": previous.id})
    await notify_queued(conn, new_id)
    return new_id


async def _start(conn: AsyncConnection, hand: Hand, worker, left: float) -> int:
    """Queue the first run of a session of ``hand`` on ``worker``, on the branch chat/<its id>."""
    profile = hand.profile
    names = [
        name
        for name in await project_repos(conn, profile.project_id)
        if f"{profile.project}/{name}" in (worker.checkouts or {})
    ]
    allow = profiles.allowed(profile.permissions, "model")
    title = runs.step_title({"title": f"@{profile.handle} in {hand.chat.title}: {hand.body}"})
    values = {
        "kind": "chat",
        "project_id": profile.project_id,
        "title": title,
        "dispatched_by": hand.dispatcher_id,
        "dispatched_via": hand.via,
        "pinned_worker_id": worker.id,
        "requested_runtime": "any",
        "runtime": "any",
        "model": allow[0] if allow else None,
        "mode": "headless",
        "approval": "auto",
        "timeout_s": rules.TIMEOUT_MIN * 60,
        "profile_id": profile.id,
        "profile_version": profile.version,
        "chat_id": hand.chat.chat_id,
        "chat_message_id": hand.message_id,
        "hop": hand.hop,
        "budget": rules.run_budget(left),
    }
    run_id = await insert_run(conn, values)
    branch = rules.branch_of(run_id)
    repos = [{"repo": name, "branch": branch} for name in names] or null()  # SQL NULL, not a JSON null
    r = tables.runs
    await conn.execute(update(r).values(branch=branch, repos=repos).where(r.c.id == run_id))
    await notify_queued(conn, run_id)
    return run_id


async def _writer_refusal(conn: AsyncConnection, hand: Hand) -> str | None:
    profile, g = hand.profile, tables.grants
    if profile.deleted_at is not None:
        return f"@{profile.handle} was deleted: it takes no new work"
    query = select(g.c.role).where(g.c.user_id == hand.dispatcher_id, g.c.project_id == profile.project_id)
    role = (await conn.execute(query)).scalar_one_or_none()
    if not has_role(role, "writer"):
        return (
            f"@{profile.handle} works for the writers of project {profile.project}, and {hand.dispatcher} holds "
            f"{role or 'no grant'} there"
        )
    return None


async def hand_work(conn: AsyncConnection, hand: Hand) -> tuple[int | None, str | None]:
    """(the run the mention reached, None) or (None, why it reached none), in the caller's transaction: the session's
    run that is queued or held takes the message in its inbox; one that ended done is gone on from; else a new
    session starts on a worker of the dispatcher (see the module's docstring)."""
    refusal = await _writer_refusal(conn, hand)
    if refusal is not None:
        return None, refusal
    query = _sessions(hand.chat.chat_id, hand.root_id, hand.dispatcher_id, hand.profile.id).limit(1)
    session = (await conn.execute(query.with_for_update(of=tables.runs))).one_or_none()
    if session is not None and session.state in runs.MESSAGE_STATES:
        await _to_inbox(conn, session.id, hand)
        return session.id, None
    spent, budget = await chat_spend(conn, hand.chat.chat_id)
    if spent >= budget:
        return None, _spent(spent, budget)
    if session is not None and session.state == "done" and session.session_id and not session.revoked:
        return await _go_on(conn, session, hand, budget - spent), None
    worker, why = await pick_worker(conn, hand)
    if worker is None:
        return None, why
    return await _start(conn, hand, worker, budget - spent), None


async def hand_mentions(conn: AsyncConnection, hand: Hand, kept: list[dict]) -> list[str]:
    """Hand work to each agent ``kept`` (the mentions of a person's message, as ``chats.resolve_mentions`` keeps them)
    names and does not refuse, at most MAX_RUNS_PER_MESSAGE, setting each mention's run or refusal; the refusals."""
    targets = [mention for mention in kept if mention["profile_id"] is not None and mention["refusal"] is None]
    refused = []
    for count, mention in enumerate(targets, start=1):
        if count > rules.MAX_RUNS_PER_MESSAGE:
            why = f"@{mention['name']}: a message hands work to at most {rules.MAX_RUNS_PER_MESSAGE} agents"
        else:
            profile = await by_id(conn, mention["profile_id"])
            mention["run_id"], why = await hand_work(conn, replace(hand, profile=profile))
            why = None if why is None else f"@{mention['name']}: {why}"
        mention["refusal"] = why
        refused += [why] if why else []
    return refused


# The agent's messages


@dataclass(frozen=True)
class ChatRun:
    """What the hub needs of a chat run: its chat, the message that started it, the thread it answers in, its
    profile, its dispatcher and hop."""

    run_id: int
    chat_id: int
    message_id: int
    root_id: int
    profile_id: int
    dispatcher_id: int
    dispatcher: str
    via: str
    hop: int
    error: str | None


async def chat_run(conn: AsyncConnection, run_id: int) -> ChatRun | None:
    """Chat run ``run_id``; None for a run of another kind."""
    r, m, u = tables.runs, tables.chat_messages, tables.users
    query = (
        select(
            r.c.id,
            r.c.chat_id,
            r.c.chat_message_id,
            func.coalesce(m.c.thread_root_id, m.c.id),
            r.c.profile_id,
            r.c.dispatched_by,
            u.c.login,
            func.coalesce(r.c.dispatched_via, "machine"),
            r.c.hop,
            r.c.error,
        )
        .select_from(r.join(m, m.c.id == r.c.chat_message_id).join(u, u.c.id == r.c.dispatched_by))
        .where(r.c.id == run_id, r.c.kind == "chat")
    )
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else ChatRun(*row)


async def agent_access(conn: AsyncConnection, chat_id: int, profile: Profile, dispatcher_id: int) -> ChatAccess | None:
    """Agent ``profile``'s access to chat ``chat_id``, acting for person ``dispatcher_id``: its login is the agent's
    @handle, as the notices name the sender; None when the agent is not in the chat."""
    c, m, u = tables.chats, tables.chat_members, tables.users
    member = (m.c.chat_id == c.c.id) & (m.c.profile_id == profile.id) & m.c.removed_at.is_(None)
    query = (
        select(c.c.id, c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq, m.c.id, m.c.last_read_seq)
        .select_from(c.join(m, member).join(u, u.c.id == c.c.created_by))
        .where(c.c.id == chat_id)
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    projects = await agent_projects(conn, chat_id)
    return ChatAccess(*row, user_id=dispatcher_id, login=f"@{profile.handle}", projects=projects)


async def post_agent(
    conn: AsyncConnection,
    chat: ChatAccess,
    profile: Profile,
    run_id: int,
    body: str,
    root_id: int | None,
    *,
    hand: Hand | None = None,
    target: str | None = None,
) -> tuple[int, list[dict]]:
    """Store a message of agent ``profile`` of run ``run_id`` in ``chat``, in the thread of ``root_id``; (its id, its
    mentions). A person it mentions gets the notice. The agent ``target`` names gets the work ``hand`` asks
    (``_hand_on``), and any other agent it mentions none (AGENT_REPLY_REFUSAL)."""
    from evo_agents.hub.server.chats import resolve_mentions  # its routes hand work through this module

    origin = await _principal(conn, chat.user_id)
    kept, people, _ = await resolve_mentions(conn, chat, origin, chats.mention_names(body))
    message_id, _ = await insert_message(
        conn, chat, body, thread_root_id=root_id, author_profile_id=profile.id, run_id=run_id
    )
    for mention in kept:
        if mention["profile_id"] is None or mention["refusal"] is not None:
            continue
        if hand is None or mention["name"] != target:
            mention["refusal"] = AGENT_REPLY_REFUSAL
            continue
        asked = replace(hand, message_id=message_id, root_id=root_id or message_id)
        mention["run_id"], mention["refusal"] = await _hand_on(conn, asked, mention["profile_id"])
    await insert_mentions(conn, message_id, kept)
    for user_id in dict.fromkeys(people):
        await notice_mention(conn, chat, user_id, message_id, body)
    return message_id, kept


async def _hand_on(conn: AsyncConnection, hand: Hand, profile_id: int) -> tuple[int | None, str | None]:
    """An agent's mention of the agent ``profile_id``: the work for the origin of the chain one hop further, never
    to the agent's own profile (``hand.profile``)."""
    problem = rules.hop_problem(hand.hop, profile_id == hand.profile.id)
    if problem is not None:
        return None, problem
    return await hand_work(conn, replace(hand, profile=await by_id(conn, profile_id)))


async def _principal(conn: AsyncConnection, user_id: int):
    from evo_agents.hub.server.security import MACHINE, Principal

    u = tables.users
    login = (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()
    return Principal(user_id, login, False, None, MACHINE, "")


async def agent_reply(conn: AsyncConnection, run_id: int, text: str) -> int:
    """The agent's last message of a turn of chat run ``run_id``, its reply in the thread, kept in the run's log too
    as an author run's chat message is; the seq of that event. An agent taken out of the chat meanwhile keeps it in
    the log alone."""
    found = await chat_run(conn, run_id)
    profile = await _run_profile(conn, run_id)
    chat = await agent_access(conn, found.chat_id, profile, found.dispatcher_id)
    message_id = None
    if chat is not None:
        message_id, _ = await post_agent(conn, chat, profile, run_id, text, found.root_id)
    return await write_event(conn, run_id, {"text": text, "chat": "agent", "chat_message_id": message_id})


async def _run_profile(conn: AsyncConnection, run_id: int) -> Profile:
    r = tables.runs
    profile_id = (await conn.execute(select(r.c.profile_id).where(r.c.id == run_id))).scalar_one()
    return await by_id(conn, profile_id)


async def hub_reply(conn: AsyncConnection, chat_id: int, root_id: int, text: str) -> int:
    """A message of the hub itself in the thread of ``root_id`` of chat ``chat_id``; its id."""
    projects = await agent_projects(conn, chat_id)
    c, u = tables.chats, tables.users
    row = (
        await conn.execute(
            select(c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq)
            .join_from(c, u, u.c.id == c.c.created_by)
            .where(c.c.id == chat_id)
        )
    ).one()
    chat = ChatAccess(
        chat_id, row.title, row.created_by, row.login, row.created_at, row.event_seq, 0, 0, 0, "hub", projects
    )
    message_id, _ = await insert_message(conn, chat, runs.clip(text, chats.MAX_MESSAGE_BYTES), thread_root_id=root_id)
    return message_id


# The run's moves


async def _untaken(conn: AsyncConnection, run_id: int) -> list:
    i = tables.run_inbox
    query = select(i.c.id).where(i.c.run_id == run_id, i.c.delivered_at.is_(None), i.c.decision_id.is_(None))
    return (await conn.execute(query.order_by(i.c.id))).scalars().all()


async def chat_run_moved(conn: AsyncConnection, run_id: int, new: str) -> int | None:
    """What chat run ``run_id`` moving to ``new`` does in its chat: the agent's reaction on the message that started it,
    the hub's reply in the thread when it failed, and when it ended done with messages of its inbox untaken, the run
    that goes on in its session with them (its id)."""
    found = await chat_run(conn, run_id)
    if found is None:
        return None
    await status_reaction(conn, message_id=found.message_id, profile_id=found.profile_id, state=new)
    if new == "failed":
        profile = await _run_profile(conn, run_id)
        why = found.error or "its worker gave no reason"
        await hub_reply(conn, found.chat_id, found.root_id, f"Run #{run_id} of @{profile.handle} failed: {why}")
    if new != "done":
        return None
    untaken = await _untaken(conn, run_id)
    r = tables.runs
    session = (await conn.execute(select(r.c.session_id, r.c.usage).where(r.c.id == run_id))).one()
    if not untaken or not session.session_id:
        return None
    spent, budget = await chat_spend(conn, found.chat_id)
    values = {
        "dispatched_by": found.dispatcher_id,
        "dispatched_via": found.via,
        "chat_message_id": found.message_id,
        "hop": found.hop,
        "budget": rules.run_budget(max(budget - spent, 0.0) + curator.run_cost(session.usage)),
    }
    new_id = (await conn.execute(_continue_run(run_id, values))).scalar_one()
    i = tables.run_inbox
    await conn.execute(update(i).values(run_id=new_id).where(i.c.id.in_(untaken)))
    await write_event(conn, new_id, {"text": f"goes on in the session of run #{run_id} with its messages untaken"})
    await notify_queued(conn, new_id)
    log.info("chat run goes on with its inbox", extra={"run_id": new_id, "resume_of": run_id})
    return new_id


# The prompt


def _message(row, profiles_by_id: dict) -> dict:
    if row.author_profile_id is not None:
        author = f"@{profiles_by_id[row.author_profile_id].handle} (agent)"
    else:
        author = row.author_login or "hub"
    return {"id": row.id, "author": author, "at": row.created_at, "body": row.body}


async def chat_prompt(conn: AsyncConnection, run_id: int, branch: str | None, repos: list) -> str:
    """The prompt of chat run ``run_id`` as its worker claims it (``chat_runs.build_chat_prompt``): the snapshot holds
    the messages its dispatcher clears the label of."""
    from evo_agents.hub.server.chat_views import message_select, profiles_by_id

    found = await chat_run(conn, run_id)
    profile = await _run_profile(conn, run_id)
    cm = tables.chat_messages
    root_id = found.root_id
    thread = message_select().where(
        cm.c.chat_id == found.chat_id, or_(cm.c.id == root_id, cm.c.thread_root_id == root_id)
    )
    before = message_select().where(cm.c.chat_id == found.chat_id, cm.c.thread_root_id.is_(None), cm.c.id < root_id)
    before = before.order_by(cm.c.id.desc()).limit(rules.BEFORE_MESSAGES + 1)
    rows = [*reversed((await conn.execute(before)).all()), *(await conn.execute(thread.order_by(cm.c.seq))).all()]
    reader = await label_reader(conn, found.dispatcher_id, [row.label for row in rows])
    authors = await profiles_by_id(conn, [row.author_profile_id for row in rows if row.author_profile_id])
    shown = {row.id: _message(row, authors) for row in rows if reader.sees(row.label)}
    trigger = shown.pop(found.message_id, None) or {"id": found.message_id, "author": found.dispatcher, "body": ""}
    root = shown.pop(root_id, None) if root_id != found.message_id else None
    blocks = rules.snapshot(
        root,
        [shown[row.id] for row in rows if row.id in shown and row.thread_root_id == root_id],
        [shown[row.id] for row in rows if row.id in shown and row.thread_root_id is None],
    )
    people, agents = await _member_names(conn, found.chat_id)
    _, budget = await chat_spend(conn, found.chat_id)
    c = tables.chats
    title = (await conn.execute(select(c.c.title).where(c.c.id == found.chat_id))).scalar_one()
    return rules.build_chat_prompt(
        agent={
            "handle": profile.handle,
            "name": profile.name,
            "project": profile.project,
            "description": profile.description,
        },
        chat={"id": found.chat_id, "title": title, "people": people, "agents": agents},
        trigger=trigger,
        login=found.dispatcher,
        hop=found.hop,
        branch=branch or rules.branch_of(run_id),
        budget_usd=budget,
        blocks=blocks,
        repos=repos,
    )


async def _member_names(conn: AsyncConnection, chat_id: int) -> tuple[list[str], list[str]]:
    from evo_agents.hub.server.chat_views import member_names

    return (await member_names(conn, [chat_id]))[chat_id]

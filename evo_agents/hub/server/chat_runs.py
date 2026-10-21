"""Chat runs on the hub (``evo_agents.hub.chat_runs``): the run a mention of an agent in a group chat makes, the worker
it goes to, the session it goes on in, the agent's messages in the thread and the reactions that follow the run.

``hand_work`` is what a mention asks, of a person (``hand_mentions``, from ``chats``) or of an agent (``chat_agents``):
the run of the profile's agent for the person at the origin of the chain. Refused, with why, when that person is not a
writer on the profile's project (in a settle chat, any grant on it wakes the chat's own agent: ``chats.SETTLE``), or
when no worker of the person can take it now. Each (thread, agent, person) is one session: a run of it that is queued or held takes the message in its inbox; one that
ended done, or failed because the remote declined its push (``rules.goes_on``), is gone on from by a new run, pinned to
its worker, in its session, with the message in its inbox; else a new session starts on the person's worker the profile
allows, with the most of the project's repos checked out first, on the branch ``chat/<run>``, in the repos of the
project that worker has a checkout of; on a daemon of ``runs.LAZY_CHAT_REPOS``, in the project's harness alone, and its
agent opens the others it needs (``chat_agents.open_repo``), which the runs after it in the session start with
(``_continue_run``). Each run after the first takes its title from the message that woke it (``rules.turn_title``), not
from the run before; the first run's stays the session's. The profile of an agent type that routes its chat runs to its
``run_as`` (``chat_route``) takes the worker of that member instead, who must hold writer on the project, and its run's
row is dispatched by them; the person stays the origin the hub's chat code acts for (``ChatRun.dispatcher_id``).

A chat run's moves (``chat_run_moved``, from ``run_state.record_move``) wake the chat's streams, whose progress of the
run under its messages changed (``chat_progress``), and put the agent's reaction on the message that started it by
rule (``chat_store.status_reaction``); a run that failed says why in the thread, and one that ended
done with messages of its inbox untaken goes on in its session at once, with them. Its prompt is built when it is
claimed (``chat_prompt``); on a daemon of ``runs.LAZY_CHAT_REPOS`` it lists every repo of the project, those open and
those the message or the thread names first (``rules.ranked_repos``). The agent's reply at the end of each turn, and
the messages of its commands, are messages of its profile in the thread (``post_agent``): a person it mentions gets the
notice, an agent none, and no run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from sqlalchemy import func, insert, literal, null, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, profiles, runs, tables
from evo_agents.hub.access import has_role
from evo_agents.hub.author import harness_repo
from evo_agents.hub.server.chat_access import ChatAccess, agent_projects, label_reader
from evo_agents.hub.server.chat_notices import notice_mention
from evo_agents.hub.server.chat_route import Route, origin, route_of
from evo_agents.hub.server.chat_store import insert_mentions, insert_message, status_reaction, wake
from evo_agents.hub.server.profile_store import Profile, by_id
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.runs.service.worker_fit import unfit_worker, workers_of
from evo_agents.hub.server.security import MACHINE

log = logging.getLogger(__name__)

AGENT_REPLY_REFUSAL = f"an agent's @ in a message hands no work: it uses `{rules.MENTION_COMMAND}`"


@dataclass(frozen=True)
class Hand:
    """What one mention asks: work of ``profile`` for ``dispatcher_id`` (``dispatcher``), the person at the origin of
    the chain, at ``hop``, in the thread of ``root_id`` of ``chat``, woken by message ``message_id`` that ``author``
    wrote, with the credential kind ``via`` (runs.dispatched_via). ``route`` is set by ``hand_work`` for a profile
    whose agent type sends its chat runs to the workers of its run_as."""

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
    route: Route | None = None

    @property
    def owner_id(self) -> int:
        """The member the run's row is dispatched by, whose worker takes it."""
        return self.route.user_id if self.route is not None else self.dispatcher_id

    @property
    def owner(self) -> str:
        return self.route.login if self.route is not None else self.dispatcher

    @property
    def run_via(self) -> str:
        """The credential kind of the run's row: the person's, or machine for a run that goes under run_as."""
        return MACHINE if self.route is not None else self.via


# The worker


async def project_harness(conn: AsyncConnection, project_id: int) -> str | None:
    """The repo a worker names the checkout of the project's harness by (``author.harness_repo``); None for a project
    registered without one."""
    p = tables.projects
    path = (await conn.execute(select(p.c.harness_path).where(p.c.id == project_id))).scalar_one_or_none()
    return harness_repo(path)


async def project_repos(conn: AsyncConnection, project_id: int) -> list[str]:
    """The repos of a project a chat run works in: its harness, then the repos it lists, by name."""
    pr = tables.project_repos
    names = (await conn.execute(select(pr.c.name).where(pr.c.project_id == project_id).order_by(pr.c.name))).scalars()
    harness = await project_harness(conn, project_id)
    return list(dict.fromkeys([*([harness] if harness else []), *names]))


def _no_worker(hand: Hand) -> str:
    profile, route = hand.profile, hand.route
    if route is None:
        return (
            f"{hand.dispatcher} has no worker that takes runs of project {profile.project}, which @{profile.handle} "
            "works in, and no member shares one there: join one, or add the project to one"
        )
    named = f" among those its agent type names ({', '.join(route.workers)})" if route.workers else ""
    return (
        f"@{profile.handle} runs on the workers of {route.login}, who has no worker{named} that takes runs of project "
        f"{profile.project}: {route.login} joins one or adds the project to one, or an admin names other workers"
    )


async def pick_worker(conn: AsyncConnection, hand: Hand):
    """(the worker a new session goes to, None) or (None, why none can take it): of the dispatcher or one another member
    shares in the project, or of the run_as of the agent's type among its own workers the type names (``hand.route``),
    fit for the profile (``unfit_worker``); the dispatcher's own before a shared one, then the one with a checkout of
    the most repos of the project, then the most free slots."""
    profile = hand.profile
    rows = (await conn.execute(workers_of(hand.owner_id, profile.project_id))).all()
    if hand.route is not None:
        rows = [row for row in rows if row.owner_id == hand.owner_id and hand.route.named(row.name)]
    if not rows:
        return None, _no_worker(hand)
    repos = await project_repos(conn, profile.project_id)
    fit, unfit = [], []
    for row in rows:
        why = unfit_worker(row, profile, hand.run_via)
        if why is None:
            fit.append(row)
        else:
            unfit.append(f"{row.name} {why}")
    if not fit:
        return None, f"no worker of {hand.owner} can take a run of @{profile.handle} now: {'; '.join(unfit)}"

    def rank(row):
        held = sum(1 for name in repos if f"{profile.project}/{name}" in (row.checkouts or {}))
        return (row.owner_id != hand.owner_id, -held, row.holding - row.slots, row.id)

    return min(fit, key=rank), None


# Sessions


def _sessions(chat_id: int, root_id: int, dispatcher_id: int, profile_id: int | None = None):
    """The chat runs in the thread of ``root_id`` dispatched by ``dispatcher_id`` (of ``profile_id`` when given), the
    latest first, with the state of their worker and the person who wrote the message that started each."""
    r, m, w = tables.runs, tables.chat_messages, tables.workers
    query = (
        select(
            r.c.id,
            r.c.state,
            r.c.profile_id,
            r.c.worker_id,
            r.c.session_id,
            r.c.failure_cause,
            w.c.revoked_at.is_not(None).label("revoked"),
            m.c.author_user_id.label("author_id"),
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
    is none or more than one: a message without an @ in a chat of one person goes to it. A session that went to the
    workers of an agent type's run_as counts for the person whose message started it."""
    r, m = tables.runs, tables.chat_messages
    started = (
        select(r.c.profile_id)
        .join_from(r, m, m.c.id == r.c.chat_message_id)
        .where(
            r.c.chat_id == chat_id,
            func.coalesce(m.c.thread_root_id, m.c.id) == root_id,
            m.c.author_user_id == dispatcher_id,
        )
    )
    found = {row.profile_id for row in await conn.execute(_sessions(chat_id, root_id, dispatcher_id))}
    found |= set((await conn.execute(started)).scalars())
    return found.pop() if len(found) == 1 else None


async def _session(conn: AsyncConnection, hand: Hand):
    """The latest chat run of ``hand``'s profile in its thread for its person, its row locked; None when there is
    none. One that went to the workers of run_as (``hand.route``) is the person's when its origin is."""
    query = _sessions(hand.chat.chat_id, hand.root_id, hand.owner_id, hand.profile.id)
    if hand.route is None:
        return (await conn.execute(query.limit(1).with_for_update(of=tables.runs))).one_or_none()
    for row in (await conn.execute(query)).all():
        person = row.author_id if row.author_id is not None else await origin(conn, row.id)
        if (person or hand.owner_id) == hand.dispatcher_id:
            locked = query.where(tables.runs.c.id == row.id).with_for_update(of=tables.runs)
            return (await conn.execute(locked)).one()
    return None


async def _to_inbox(conn: AsyncConnection, run_id: int, hand: Hand) -> None:
    """Leave the message of ``hand`` in the inbox of run ``run_id``, which the worker hands to the agent."""
    i = tables.run_inbox
    text = rules.inbox_text(hand.author, hand.message_id, hand.body)
    left = insert(i).values(run_id=run_id, sent_by=hand.dispatcher_id, body=text).returning(i.c.id)
    inbox_id = (await conn.execute(left)).scalar_one()
    event = {"text": text, "from": hand.dispatcher, "message_id": inbox_id, "chat_message_id": hand.message_id}
    await write_user_message(conn, run_id, event)


CONTINUED = ("kind", "project_id", "title", "mode", "approval", "timeout_s", "repos", "branch", "model", "chat_id")
CONTINUED += ("profile_id", "profile_version", "session_id", "runtime")


def _continue_run(previous: int, values: dict):
    """The run that goes on in the session of chat run ``previous``: its project, profile, repos, branch, runtime and
    model, pinned to its worker, in its session; ``values`` names the rest, and a title (``_titled``), or else the
    run takes the title of ``previous``."""
    r = tables.runs
    copied = tuple(name for name in CONTINUED if name not in values)
    source = select(
        *(r.c[name] for name in copied),
        r.c.worker_id.label("pinned_worker_id"),
        r.c.runtime.label("requested_runtime"),
        r.c.id.label("resume_of_run_id"),
        *(literal(value, r.c[name].type).label(name) for name, value in values.items()),
    ).where(r.c.id == previous)
    columns = [*copied, "pinned_worker_id", "requested_runtime", "resume_of_run_id", *values]
    return insert(r).from_select(columns, source).returning(r.c.id)


def _titled(values: dict, title: str | None) -> dict:
    """``values`` of a run that goes on in a session, with ``title`` when there is one."""
    return {**values, "title": title} if title else values


async def _woken_title(conn: AsyncConnection, run_id: int, text: str) -> str | None:
    """The title of the run that goes on in the session of chat run ``run_id``, woken by ``text``; None when the run
    has no profile or chat any more."""
    r, c, ap = tables.runs, tables.chats, tables.agent_profiles
    query = (
        select(ap.c.handle, c.c.title)
        .select_from(r.join(c, c.c.id == r.c.chat_id).join(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )
    row = (await conn.execute(query)).one_or_none()
    return None if row is None else rules.turn_title(row.handle, row.title, text)


async def _go_on(conn: AsyncConnection, previous, hand: Hand) -> int:
    """Queue the run that goes on in the session of chat run ``previous`` with the message of ``hand``, its title."""
    values = {
        "dispatched_by": hand.owner_id,
        "dispatched_via": hand.run_via,
        "chat_message_id": hand.message_id,
        "hop": hand.hop,
        "budget": rules.run_budget(),
    }
    values = _titled(values, rules.turn_title(hand.profile.handle, hand.chat.title, hand.body))
    new_id = (await conn.execute(_continue_run(previous.id, values))).scalar_one()
    await _to_inbox(conn, new_id, hand)
    failed = f" (failed: {previous.failure_cause})" if previous.state == "failed" else ""
    shown = f"goes on in the session of run #{previous.id}{failed}, as {hand.author} wrote in the thread"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": previous.id})
    await notify_queued(conn, new_id)
    return new_id


async def go_on_parked(
    conn: AsyncConnection, parked_id: int, why: str, *, woken_by: str | None, token_id: int | None = None
) -> int:
    """Queue the run that goes on in the session of chat run ``parked_id``, which waited for its owner's answer and was
    parked, whose row the caller holds locked, and end the parked run done; the new run's id. The new run is the
    parked one's for the thread, the person, the worker and the agent time used so far; the decisions of the parked
    run still open and the messages of its inbox the worker has not handed to the agent go to it. ``why`` ends the
    new run's first event, such as "as owner answered decision #3", and ``woken_by``, the answer or the message that
    made it go on, is its title (the parked run's when None)."""
    r = tables.runs
    row = (
        await conn.execute(
            select(
                r.c.dispatched_by,
                r.c.dispatched_via,
                r.c.chat_id,
                r.c.chat_message_id,
                r.c.hop,
                r.c.run_seconds,
            ).where(r.c.id == parked_id)
        )
    ).one()
    values = {
        "dispatched_by": row.dispatched_by,
        "dispatched_via": row.dispatched_via,
        "chat_message_id": row.chat_message_id,
        "hop": row.hop,
        "run_seconds": row.run_seconds,
        "budget": rules.run_budget(),
    }
    if woken_by is not None:
        values = _titled(values, await _woken_title(conn, parked_id, woken_by))
    await move_run(
        conn, parked_id, "parked", "done", "owner", reason="resumed in a new run", token_id=token_id, decisions=None
    )
    new_id = (await conn.execute(_continue_run(parked_id, values))).scalar_one()
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    await conn.execute(update(i).values(run_id=new_id).where(i.c.run_id == parked_id, i.c.delivered_at.is_(None)))
    await write_event(
        conn, new_id, {"text": f"goes on in the session of run #{parked_id}, {why}", "resume_of_run_id": parked_id}
    )
    await notify_queued(conn, new_id)
    return new_id


async def _start(conn: AsyncConnection, hand: Hand, worker) -> int:
    """Queue the first run of a session of ``hand`` on ``worker``, on the branch chat/<its id>, with the repos of the
    project the worker has a checkout of: of them, the project's harness alone on a daemon of
    ``runs.LAZY_CHAT_REPOS``, whose agent opens the others it needs (``chat_agents.open_repo``)."""
    profile = hand.profile
    names = [
        name
        for name in await project_repos(conn, profile.project_id)
        if f"{profile.project}/{name}" in (worker.checkouts or {})
    ]
    if runs.LAZY_CHAT_REPOS in (worker.features or ()):
        harness = await project_harness(conn, profile.project_id)
        names = [name for name in names if name == harness]
    allow = profiles.allowed(profile.permissions, "model")
    title = rules.turn_title(profile.handle, hand.chat.title, hand.body)
    values = {
        "kind": "chat",
        "project_id": profile.project_id,
        "title": title,
        "dispatched_by": hand.owner_id,
        "dispatched_via": hand.run_via,
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
        "budget": rules.run_budget(),
    }
    run_id = await insert_run(conn, values)
    branch = rules.branch_of(run_id)
    repos = [{"repo": name, "branch": branch} for name in names] or null()  # SQL NULL, not a JSON null
    r = tables.runs
    await conn.execute(update(r).values(branch=branch, repos=repos).where(r.c.id == run_id))
    await notify_queued(conn, run_id)
    return run_id


async def _admins_only(conn: AsyncConnection, profile_id: int) -> bool:
    """Whether the profile ``profile_id`` is that of an agent type a version of its plugin says takes work from the
    project's admins alone (``mentioned_by: admin``); any version the hub holds counts, so it never opens up."""
    t, v = tables.project_agent_types, tables.plugin_versions
    query = select(t.c.name, v.c.manifest).join_from(t, v, v.c.plugin_id == t.c.plugin_id)
    for row in await conn.execute(query.where(t.c.profile_id == profile_id)):
        for item in (row.manifest.get("contributes") or {}).get("agent_types") or ():
            if item.get("name") == row.name and item.get("mentioned_by") == "admin":
                return True
    return False


async def _attends(conn: AsyncConnection, hand: Hand) -> bool:
    """Whether ``hand``'s profile is the agent of its settle chat, which every member of its project wakes."""
    if hand.chat.kind != chats.SETTLE:
        return False
    c = tables.chats
    agent = (await conn.execute(select(c.c.direct_profile_id).where(c.c.id == hand.chat.chat_id))).scalar_one()
    return agent == hand.profile.id


async def _writer_refusal(conn: AsyncConnection, hand: Hand) -> str | None:
    profile, g = hand.profile, tables.grants
    if profile.deleted_at is not None:
        return f"@{profile.handle} was deleted: it takes no new work"
    query = select(g.c.role).where(g.c.user_id == hand.dispatcher_id, g.c.project_id == profile.project_id)
    role = (await conn.execute(query)).scalar_one_or_none()
    if role is None and await _attends(conn, hand):
        return (
            f"@{profile.handle} works for the members of project {profile.project} in this settle chat, and "
            f"{hand.dispatcher} holds no grant there"
        )
    if not has_role(role, "writer") and not await _attends(conn, hand):
        return (
            f"@{profile.handle} works for the writers of project {profile.project}, and {hand.dispatcher} holds "
            f"{role or 'no grant'} there"
        )
    if not has_role(role, "admin") and await _admins_only(conn, profile.id):
        return (
            f"@{profile.handle} takes work from the admins of project {profile.project} alone, and {hand.dispatcher} "
            f"holds {role} there"
        )
    return None


async def _run_as_refusal(conn: AsyncConnection, hand: Hand) -> str | None:
    """Why the run_as of ``hand``'s route takes no run of its profile now: they no longer hold writer on the project."""
    profile, route, g = hand.profile, hand.route, tables.grants
    query = select(g.c.role).where(g.c.user_id == route.user_id, g.c.project_id == profile.project_id)
    role = (await conn.execute(query)).scalar_one_or_none()
    if has_role(role, "writer"):
        return None
    return (
        f"@{profile.handle} runs on the workers of {route.login}, who holds {role or 'no grant'} on project "
        f"{profile.project} now, not writer: an admin of the project names another member"
    )


async def hand_work(conn: AsyncConnection, hand: Hand) -> tuple[int | None, str | None]:
    """(the run the mention reached, None) or (None, why it reached none), in the caller's transaction: the session's
    run that is queued or held takes the message in its inbox; one that ended done, or failed because the remote
    declined its push, is gone on from; else a new session starts on a worker of the dispatcher, or of the run_as of
    the agent's type when it routes its chat runs there (see the module's docstring)."""
    refusal = await _writer_refusal(conn, hand)
    if refusal is not None:
        return None, refusal
    hand = replace(hand, route=await route_of(conn, hand.profile.id))
    if hand.route is not None:
        refusal = await _run_as_refusal(conn, hand)
        if refusal is not None:
            return None, refusal
    session = await _session(conn, hand)
    if session is not None and session.state in runs.MESSAGE_STATES:
        await _to_inbox(conn, session.id, hand)
        return session.id, None
    if session is not None and session.state == "parked" and session.session_id:
        new_id = await go_on_parked(conn, session.id, f"as {hand.author} wrote in the thread", woken_by=hand.body)
        await _to_inbox(conn, new_id, hand)
        return new_id, None
    if (
        session is not None
        and rules.goes_on(session.state, session.failure_cause)
        and session.session_id
        and not session.revoked
    ):
        return await _go_on(conn, session, hand), None
    worker, why = await pick_worker(conn, hand)
    if worker is None:
        return None, why
    return await _start(conn, hand, worker), None


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
    profile, its dispatcher (the person at the origin of the chain, whom the agent acts for) and hop, and
    ``dispatched_by``, the member its row is dispatched by: the dispatcher, or the run_as of an agent type that sends
    its chat runs to that member's workers (``chat_route``)."""

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
    dispatched_by: int


async def chat_run(conn: AsyncConnection, run_id: int) -> ChatRun | None:
    """Chat run ``run_id``; None for a run of another kind."""
    r, m, u = tables.runs, tables.chat_messages, tables.users
    query = (
        select(
            r.c.id,
            r.c.chat_id,
            r.c.chat_message_id,
            func.coalesce(m.c.thread_root_id, m.c.id).label("root_id"),
            r.c.profile_id,
            r.c.dispatched_by,
            u.c.login,
            func.coalesce(r.c.dispatched_via, "machine").label("via"),
            r.c.hop,
            r.c.error,
            m.c.author_user_id,
            m.c.run_id.label("asked_by_run"),
        )
        .select_from(r.join(m, m.c.id == r.c.chat_message_id).join(u, u.c.id == r.c.dispatched_by))
        .where(r.c.id == run_id, r.c.kind == "chat")
    )
    row = (await conn.execute(query)).one_or_none()
    if row is None:
        return None
    person = row.author_user_id
    if person is None and row.asked_by_run is not None:  # an agent's message: the origin of that agent's run
        person = await origin(conn, row.asked_by_run)
    person, login = person or row.dispatched_by, row.login
    if person != row.dispatched_by:  # the run went to the workers of its agent type's run_as
        login = (await conn.execute(select(u.c.login).where(u.c.id == person))).scalar_one()
    head = (row.id, row.chat_id, row.chat_message_id, row.root_id, row.profile_id)
    return ChatRun(*head, person, login, row.via, row.hop, row.error, row.dispatched_by)


async def agent_access(conn: AsyncConnection, chat_id: int, profile: Profile, dispatcher_id: int) -> ChatAccess | None:
    """Agent ``profile``'s access to chat ``chat_id``, acting for person ``dispatcher_id``: its login is the agent's
    @handle, as the notices name the sender; None when the agent is not in the chat."""
    c, m, u = tables.chats, tables.chat_members, tables.users
    member = (m.c.chat_id == c.c.id) & (m.c.profile_id == profile.id) & m.c.removed_at.is_(None)
    head = (c.c.id, c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq, m.c.id, m.c.last_read_seq)
    query = select(*head, c.c.kind).select_from(c.join(m, member).join(u, u.c.id == c.c.created_by))
    row = (await conn.execute(query.where(c.c.id == chat_id))).one_or_none()
    if row is None:
        return None
    projects = await agent_projects(conn, chat_id)
    return ChatAccess(*row[:-1], user_id=dispatcher_id, login=f"@{profile.handle}", projects=projects, kind=row.kind)


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
    (``_hand_on``), and any other agent it mentions none (AGENT_REPLY_REFUSAL). The agent naming itself ("I am
    @reviewer"), as the agent of a direct chat does, asks no work of anyone and is refused nothing."""
    from evo_agents.hub.server.chats import resolve_mentions  # its routes hand work through this module

    origin = await _principal(conn, chat.user_id)
    kept, people, _ = await resolve_mentions(conn, chat, origin, chats.mention_names(body))
    message_id, _ = await insert_message(
        conn, chat, body, thread_root_id=root_id, author_profile_id=profile.id, run_id=run_id
    )
    for mention in kept:
        if mention["profile_id"] is None or mention["refusal"] is not None:
            continue
        if mention["profile_id"] == profile.id and mention["name"] != target:
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


async def hub_reply(conn: AsyncConnection, chat_id: int, root_id: int | None, text: str) -> int:
    """A message of the hub itself in the thread of ``root_id`` of chat ``chat_id``, or, with no root, one that
    starts none; its id."""
    projects = await agent_projects(conn, chat_id)
    c, u = tables.chats, tables.users
    row = (
        await conn.execute(
            select(c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq, c.c.kind)
            .join_from(c, u, u.c.id == c.c.created_by)
            .where(c.c.id == chat_id)
        )
    ).one()
    chat = ChatAccess(
        chat_id, row.title, row.created_by, row.login, row.created_at, row.event_seq, 0, 0, 0, "hub", projects, row.kind
    )
    message_id, _ = await insert_message(conn, chat, runs.clip(text, chats.MAX_MESSAGE_BYTES), thread_root_id=root_id)
    return message_id


# The run's moves


async def _untaken(conn: AsyncConnection, run_id: int) -> list:
    """The messages of the inbox of run ``run_id`` its worker did not hand to the agent, but answers, oldest first."""
    i = tables.run_inbox
    query = select(i.c.id, i.c.body).where(i.c.run_id == run_id, i.c.delivered_at.is_(None), i.c.decision_id.is_(None))
    return (await conn.execute(query.order_by(i.c.id))).all()


async def chat_run_moved(conn: AsyncConnection, run_id: int, new: str, old: str | None = None) -> int | None:
    """What chat run ``run_id`` moving to ``new`` does in its chat: the chat's streams woken, since the run's progress
    under its messages changed (``chat_progress``); the agent's reaction on the message that started it, the hub's
    reply in the thread when it failed, and when it ended done with messages of its inbox untaken, the run that goes on
    in its session with them (its id), titled by the first of them."""
    if old == "parked" and new == "done":  # it waited for an answer and a new run goes on in its session (go_on_parked)
        return None
    found = await chat_run(conn, run_id)
    if found is None:
        return None
    await wake(conn, found.chat_id)
    await status_reaction(conn, message_id=found.message_id, profile_id=found.profile_id, state=new)
    if new == "failed":
        profile = await _run_profile(conn, run_id)
        why = found.error or "its worker gave no reason"
        await hub_reply(conn, found.chat_id, found.root_id, f"Run #{run_id} of @{profile.handle} failed: {why}")
    if new != "done":
        return None
    untaken = await _untaken(conn, run_id)
    r = tables.runs
    session = (await conn.execute(select(r.c.session_id).where(r.c.id == run_id))).one()
    if not untaken or not session.session_id:
        return None
    values = {
        "dispatched_by": found.dispatched_by,
        "dispatched_via": found.via,
        "chat_message_id": found.message_id,
        "hop": found.hop,
        "budget": rules.run_budget(),
    }
    values = _titled(values, await _woken_title(conn, run_id, rules.woken_text(untaken[0].body)))
    new_id = (await conn.execute(_continue_run(run_id, values))).scalar_one()
    i = tables.run_inbox
    await conn.execute(update(i).values(run_id=new_id).where(i.c.id.in_([row.id for row in untaken])))
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


def _clearers(found: ChatRun) -> list[int]:
    return list(dict.fromkeys([found.dispatcher_id, found.dispatched_by]))


async def _listing(
    conn: AsyncConnection, run_id: int, profile: Profile, repos: list, texts: list[str]
) -> tuple[list[dict], str | None]:
    """(every repo of the project of chat run ``run_id`` as its prompt lists them, ``rules.ranked_repos``: the run's
    ``repos`` open, those ``texts`` name, and whether the run's worker has a checkout of each; the project's
    harness)."""
    r, w = tables.runs, tables.workers
    held_by = select(w.c.checkouts).join_from(r, w, w.c.id == r.c.worker_id).where(r.c.id == run_id)
    checkouts = (await conn.execute(held_by)).scalar_one_or_none() or {}
    names = await project_repos(conn, profile.project_id)
    opened = [entry.get("repo") for entry in repos if isinstance(entry, dict)]
    held = {name for name in [*names, *opened] if isinstance(name, str) and f"{profile.project}/{name}" in checkouts}
    listing = rules.ranked_repos(names, opened, held, rules.named_repos(names, texts))
    return listing, await project_harness(conn, profile.project_id)


async def run_clearers(conn: AsyncConnection, run_id: int) -> list[int] | None:
    """The people what chat run ``run_id`` reads must be cleared for, as its prompt is; None for another kind of run."""
    found = await chat_run(conn, run_id)
    return None if found is None else _clearers(found)


async def chat_prompt(
    conn: AsyncConnection, run_id: int, branch: str | None, repos: list, features: tuple[str, ...] = ()
) -> str:
    """The prompt of chat run ``run_id`` as its worker claims it (``chat_runs.build_chat_prompt``): the snapshot holds
    the messages its dispatcher clears the label of, and the member whose worker runs it too. Before the thread it
    shows the messages that start no thread, or in a direct chat the latest turns (``chat_runs.snapshot``). On a daemon
    that lists ``runs.LAZY_CHAT_REPOS`` among its ``features``, every repo of the project, those the message that woke
    it or a message of the thread the prompt shows names before the others not open (``_listing``)."""
    from evo_agents.hub.server.chat_views import message_select, profiles_by_id

    found = await chat_run(conn, run_id)
    profile = await _run_profile(conn, run_id)
    c, cm = tables.chats, tables.chat_messages
    title, kind = (await conn.execute(select(c.c.title, c.c.kind).where(c.c.id == found.chat_id))).one()
    direct, root_id = kind == chats.DIRECT, found.root_id
    thread = message_select().where(
        cm.c.chat_id == found.chat_id, or_(cm.c.id == root_id, cm.c.thread_root_id == root_id)
    )
    if direct:  # the latest turns: the messages of the threads before this one, the agent's answers in them too
        earlier = func.coalesce(cm.c.thread_root_id, cm.c.id) < root_id
    else:  # the chat before the thread: the messages that start no thread
        earlier = cm.c.thread_root_id.is_(None) & (cm.c.id < root_id)
    limit = rules.DIRECT_MESSAGES if direct else rules.BEFORE_MESSAGES
    before = message_select().where(cm.c.chat_id == found.chat_id, earlier).order_by(cm.c.id.desc()).limit(limit + 1)
    before_rows = list(reversed((await conn.execute(before)).all()))
    thread_rows = (await conn.execute(thread.order_by(cm.c.seq))).all()
    rows = [*before_rows, *thread_rows]
    # the dispatcher clears what the prompt shows, and so does the member whose worker runs it, when another
    readers = [await label_reader(conn, user_id, [row.label for row in rows]) for user_id in _clearers(found)]
    authors = await profiles_by_id(conn, [row.author_profile_id for row in rows if row.author_profile_id])
    shown = {row.id: _message(row, authors) for row in rows if all(reader.sees(row.label) for reader in readers)}
    in_thread = [shown[row.id]["body"] for row in rows if row.id in shown and root_id in (row.id, row.thread_root_id)]
    trigger = shown.pop(found.message_id, None) or {"id": found.message_id, "author": found.dispatcher, "body": ""}
    root = shown.pop(root_id, None) if root_id != found.message_id else None
    blocks = rules.snapshot(
        root,
        [shown[row.id] for row in thread_rows if row.id in shown and row.thread_root_id == root_id],
        [shown[row.id] for row in before_rows if row.id in shown],
        direct=direct,
    )
    people, agents = await _member_names(conn, found.chat_id)
    listing, harness = None, None
    if runs.LAZY_CHAT_REPOS in features:
        listing, harness = await _listing(conn, run_id, profile, repos, [trigger["body"], *in_thread])
    return rules.build_chat_prompt(
        agent={
            "handle": profile.handle,
            "name": profile.name,
            "project": profile.project,
            "description": profile.description,
        },
        chat={"id": found.chat_id, "title": title, "people": people, "agents": agents, "kind": kind},
        trigger=trigger,
        login=found.dispatcher,
        hop=found.hop,
        branch=branch or rules.branch_of(run_id),
        blocks=blocks,
        repos=repos,
        listing=listing,
        harness=harness,
    )


async def _member_names(conn: AsyncConnection, chat_id: int) -> tuple[list[str], list[str]]:
    from evo_agents.hub.server.chat_views import member_names

    return (await member_names(conn, [chat_id]))[chat_id]

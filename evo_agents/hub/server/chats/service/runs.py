"""Chat runs on the hub (``evo_agents.hub.chat_runs``): the run a mention of an agent in a group chat makes, the worker
it goes to, the session it goes on in, the agent's messages in the thread and the reactions that follow the run.

``hand_work`` is what a mention asks, of a person (``hand_mentions``, from ``chats.service.messages``) or of an agent
(``chats.service.agents``): the run of the profile's agent for the person at the origin of the chain. Refused, with why,
when that person is not a writer on the profile's project (in a settle chat, any grant on it wakes the chat's own agent:
``chats.SETTLE``), when their primary seat in the org chart ended, or when no worker of the person can take it now (an
operations event of the agent's project, ``mention.refused``, tells its Orchestrator, unless the mention is an ops
thread's run's own: that event would come back to it as a batch of its own, ``ops_queue.ops_run``). When the ready nodes
of the agent's pool only lack a free slot, the run queues for the first of them instead, by its priority
(``Hand.priority``), and the thread says how many runs it waits behind. Each (thread, agent, person) is one session: a
run of it that is queued or held takes the message in its inbox; one that ended done, or failed because the remote
declined its push (``rules.goes_on``), is gone on from by a new run, pinned to its worker, in its session, with the
message in its inbox; else a new session starts on the person's worker the profile allows, with the most of the
project's repos checked out first, on the branch ``chat/<run>``, in the repos of the project that worker has a checkout
of; on a daemon of ``runs.LAZY_CHAT_REPOS``, in the project's harness alone, and its agent opens the others it needs
(``chats.service.pulls.open_repo``), which the runs after it in the session start with (``_continue_run``). Each run
after the first takes its title from the message that woke it (``rules.turn_title``), not from the run before; the first
run's stays the session's. The profile of an agent type that routes its chat runs to its ``run_as``
(``chats.service.route``) takes the worker of that member instead, who must hold writer on the project, and its run's
row is dispatched by them; the person stays the origin the hub's chat code acts for (``ChatRun.dispatcher_id``).

A chat run's moves (``chat_run_moved``, from ``run_state.record_move``) wake the chat's streams, whose progress of the
run under its messages changed (``chats.service.progress``), and put the agent's reaction on the message that started it
by rule (``chats.repository.store.status_reaction``); once it ended, the files it sent after its last message go under
that message (``output_store.attach_leftovers``); a run that failed says why in the thread, and one that ended done with
messages of its inbox untaken goes on in its session at once, with them. Its prompt is built when it is claimed
(``chat_prompt``); on a daemon of ``runs.LAZY_CHAT_REPOS`` it lists every repo of the project, those open and those the
message or the thread names first (``rules.ranked_repos``). The agent's reply at the end of each turn, and the messages
of its commands, are messages of its profile in the thread (``post_agent``): a person it mentions gets the notice, an
agent none, and no run.
"""

from __future__ import annotations

import logging
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_bridge_events, chats, ops_events, ops_queue, profiles, runs, system_context
from evo_agents.hub import chat_runs as rules
from evo_agents.hub.access import has_role
from evo_agents.hub.server import (
    gateway_accounts,
    pool_accounts,
    run_access_rules,
)
from evo_agents.hub.server.agents.models.placement import Home
from evo_agents.hub.server.agents.service import placement as agent_placement
from evo_agents.hub.server.agents.service import tasking as agent_tasking
from evo_agents.hub.server.attachments import repository as attachment_store
from evo_agents.hub.server.autonomy.service import hold as autonomy_hold
from evo_agents.hub.server.autonomy.service import runs as run_autonomy
from evo_agents.hub.server.chats.models.access import ChatAccess
from evo_agents.hub.server.chats.models.runs import ChatRun, Hand
from evo_agents.hub.server.chats.repository import runs as store
from evo_agents.hub.server.chats.repository.members import role_on
from evo_agents.hub.server.chats.repository.runs import project_harness, project_repos
from evo_agents.hub.server.chats.repository.store import insert_mentions, status_reaction, wake
from evo_agents.hub.server.chats.service.access import agent_projects, label_reader
from evo_agents.hub.server.chats.service.notices import notice_mention
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.route import origin, route_of
from evo_agents.hub.server.org_store import ended_refusal
from evo_agents.hub.server.outputs.service.store import attach_leftovers, link_message
from evo_agents.hub.server.profile_store import Profile, by_id, version_rules
from evo_agents.hub.server.repo_origins import repo_origins
from evo_agents.hub.server.run_events import write_user_message
from evo_agents.hub.server.run_state import move_run, notify_queued, write_event
from evo_agents.hub.server.runs.models.attachment_parts import as_event
from evo_agents.hub.server.runs.service.dispatch import insert_run
from evo_agents.hub.server.runs.service.priority import ahead
from evo_agents.hub.server.runs.service.worker_fit import NO_FREE_SLOT, worker_problems
from evo_agents.hub.server.systems.repository import store as system_store
from evo_agents.hub.server.systems.service import traces as system_traces

log = logging.getLogger(__name__)


# The worker


async def pick_worker(conn: AsyncConnection, hand: Hand):
    """(the worker a new session goes to, None), (None, why none can take it) or (None, None) when no node is ready, so
    the run queues and the admins hear of it (``agent_placement.watch``): a node of the agent's own pool, whoever
    mentioned the agent and whatever the agent type's run_as names. ``hand.on_worker`` when it is one of them and fit,
    else the node that ran the thread's last turn of the agent when it is still fit, else the one with a checkout of
    the most repos of the project, then the most free slots. When no ready node is fit and some only lack a free slot,
    the first of those by the same order: the run queues for it and starts when it has a slot."""
    home, why = await agent_tasking.work_home(conn, hand.profile)
    if home is None:
        return None, why
    return await _pool_worker(conn, hand, home)


async def _pool_worker(conn: AsyncConnection, hand: Hand, home: Home):
    """The pick of ``pick_worker`` among the nodes of ``home``'s pool."""
    profile = hand.profile
    rows = await store.pool_workers(conn, hand.owner_id, profile.project_id, home.pool_id)
    ready = {row.id for row in await agent_placement.ready_nodes(conn, home)}
    rows = [row for row in rows if row.id in ready]
    if not rows:
        return None, None
    fit, busy, unfit = [], [], []
    for row in rows:
        problems = worker_problems(row, profile, hand.run_via)
        if not problems:
            fit.append(row)
            continue
        unfit.append(f"{row.name} {problems[0]}")
        if problems == [NO_FREE_SLOT]:
            busy.append(row)
    if not fit and not busy:
        return None, f"no node of pool {home.pool} can take a run of @{profile.handle} now: {'; '.join(unfit)}"
    return await _first_node(conn, hand, fit or busy), None


async def _first_node(conn: AsyncConnection, hand: Hand, nodes: list):
    """The node of ``nodes`` a run of ``hand`` goes to: ``hand.on_worker``, else the node of the thread's last turn of
    the agent, else the one with a checkout of the most repos of the project, then the most free slots."""
    asked = next((row for row in nodes if row.id == hand.on_worker), None)
    if asked is not None:
        return asked
    profile = hand.profile
    last = await store.last_node(conn, hand.chat.chat_id, hand.root_id, hand.profile.id)
    repos = await project_repos(conn, profile.project_id)

    def rank(row):
        held = sum(1 for name in repos if f"{profile.project}/{name}" in (row.checkouts or {}))
        return (row.id != last, -held, row.holding - row.slots, row.id)

    return min(nodes, key=rank)


# Sessions


async def _session(conn: AsyncConnection, hand: Hand):
    """The latest chat run of ``hand``'s profile in its thread for its person, its row locked; None when there is
    none. One that went to the workers of run_as (``hand.route``) is the person's when its origin is."""
    thread = (hand.chat.chat_id, hand.root_id, hand.owner_id, hand.profile.id)
    if hand.route is None:
        return await store.latest_session(conn, *thread)
    for row in await store.sessions_of(conn, *thread):
        person = row.author_id if row.author_id is not None else await origin(conn, row.id)
        if (person or hand.owner_id) == hand.dispatcher_id:
            return await store.lock_session(conn, *thread, row.id)
    return None


async def _quote(conn: AsyncConnection, run_id: int, hand: Hand) -> tuple[int, str, str] | None:
    """(id, author, text) of the agent's message ``hand``'s message answers, for the inbox of run ``run_id``: None when
    it answers none, when a run of the same session wrote it (the agent has it already), or when a person the run is
    cleared for (``_clearers``) does not clear its label."""
    found = hand.answers
    if found is None:
        return None
    if found.run_id is not None:
        sessions = await store.session_ids(conn, {run_id, found.run_id})
        if found.run_id == run_id or (sessions.get(run_id) and sessions.get(run_id) == sessions.get(found.run_id)):
            return None
    for user_id in dict.fromkeys([hand.dispatcher_id, hand.owner_id]):
        if not (await label_reader(conn, user_id, [found.label])).sees(found.label):
            return None
    return found.message_id, found.author, found.body


async def _to_inbox(conn: AsyncConnection, run_id: int, hand: Hand) -> None:
    """Leave the message of ``hand`` in the inbox of run ``run_id``, which the worker hands to the agent, with the
    agent's message it answers when another session wrote that (``_quote``)."""
    text = rules.inbox_text(hand.author, hand.message_id, hand.body, await _quote(conn, run_id, hand))
    inbox_id = await store.insert_inbox(conn, run_id, hand.dispatcher_id, text)
    files = (await attachment_store.of_messages(conn, [hand.message_id])).get(hand.message_id, [])
    await attachment_store.link_inbox(conn, inbox_id, files)  # the chat message's files go with it
    event = {"text": text, "from": hand.dispatcher, "message_id": inbox_id, "chat_message_id": hand.message_id}
    await write_user_message(conn, run_id, event | ({"attachments": as_event(files)} if files else {}))


def _titled(values: dict, title: str | None) -> dict:
    """``values`` of a run that goes on in a session, with ``title`` when there is one."""
    return {**values, "title": title} if title else values


async def _woken_title(conn: AsyncConnection, run_id: int, text: str) -> str | None:
    """The title of the run that goes on in the session of chat run ``run_id``, woken by ``text``; None when the run
    has no profile or chat any more."""
    row = await store.woken_row(conn, run_id)
    return None if row is None else rules.turn_title(row.handle, row.title, text)


async def _missed(conn: AsyncConnection, previous_id: int, hand: Hand) -> list[Hand]:
    """The messages ``hand``'s person wrote in its thread after the one that started chat run ``previous_id`` and
    before ``hand``'s, that handed no agent work (a reply without an @ in a chat of more people), oldest first, at most
    MISSED_MESSAGES: the run that goes on in the session takes them before ``hand``'s, so "merge it" then a bare
    "@agent" is one request."""
    rows = await store.missed_rows(conn, previous_id, hand.root_id, hand.dispatcher_id, hand.message_id)
    return [replace(hand, message_id=row.id, body=row.body, answers=None) for row in reversed(rows)]


async def _pooled(conn: AsyncConnection, previous_id: int, values: dict) -> dict:
    """``values`` of the run that goes on in the session of chat run ``previous_id``, placed on the pool of its agent
    when the agent is in pool mode: the pool's selector and tolerations, pinned to the node of the session while it is
    ready and to no node when it is gone, so another node of the pool takes the run."""
    row = await store.profile_worker(conn, previous_id)
    home = await agent_placement.home_of(conn, row.profile_id)
    if home is None:
        return values
    await agent_placement.watch(conn, home)
    ready = {node.id for node in await agent_placement.ready_nodes(conn, home)}
    kept = {} if row.worker_id in ready else {"pinned_worker_id": None}
    return {**values, **kept, **agent_placement.stamped(home)}


async def _go_on(conn: AsyncConnection, previous, hand: Hand) -> int:
    """Queue the run that goes on in the session of chat run ``previous`` with the message of ``hand``, its title;
    ``autonomy_hold.Stopped`` while a stop of autonomy holds its principal."""
    run_access_rules.refuse_new_run()
    await autonomy_hold.hold_run(conn, previous.id)
    values = {
        "dispatched_by": hand.owner_id,
        "dispatched_via": hand.run_via,
        "chat_message_id": hand.message_id,
        "hop": hand.hop,
        "budget": rules.run_budget(),
        "priority": hand.priority,
    }
    values = _titled(values, rules.turn_title(hand.profile.handle, hand.chat.title, hand.body))
    values = await _pooled(conn, previous.id, values)
    new_id = await store.continue_run(conn, previous.id, values)
    for missed in await _missed(conn, previous.id, hand):
        await _to_inbox(conn, new_id, missed)
    await _to_inbox(conn, new_id, hand)
    failed = f" (failed: {previous.failure_cause})" if previous.state == "failed" else ""
    shown = f"goes on in the session of run #{previous.id}{failed}, as {hand.author} wrote in the thread"
    await write_event(conn, new_id, {"text": shown, "resume_of_run_id": previous.id})
    await system_traces.join(conn, new_id, previous_id=previous.id)
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
    run_access_rules.refuse_new_run()
    row = await store.parked_row(conn, parked_id)
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
    values = await _pooled(conn, parked_id, values)
    new_id = await store.continue_run(conn, parked_id, values)
    await store.hand_over(conn, parked_id, new_id)
    await write_event(
        conn, new_id, {"text": f"goes on in the session of run #{parked_id}, {why}", "resume_of_run_id": parked_id}
    )
    await system_traces.join(conn, new_id, previous_id=parked_id)
    await notify_queued(conn, new_id)
    return new_id


async def _pinned(conn: AsyncConnection, profile: Profile, message_id: int | None = None) -> tuple[int, dict]:
    """The version of ``profile`` a chat run of it, woken by message ``message_id``, takes, with that version's
    permissions: the one the agent system it runs in pins (``system_traces.pinned_for``: the system of the trace of
    the run that wrote the message, else the one whose entry agent it is, else one of its chat or project that has
    it), its version now otherwise."""
    held = await system_traces.pinned_for(conn, profile.id, profile.handle, profile.project_id, message_id)
    version = None if held is None else system_traces.pinned_version(held.spec, profile.handle)
    if version is None:
        pin = await system_store.entry_pin(conn, profile.id)
        version = None if pin is None else pin[1]
    if version is None or version == profile.version:
        return profile.version, profile.permissions
    body = await version_rules(conn, profile.id, version)
    if body is None:
        return profile.version, profile.permissions
    return version, body.get("permissions") or profile.permissions


async def _start(conn: AsyncConnection, hand: Hand, worker) -> int:
    """Queue the first run of a session of ``hand`` on ``worker`` (on none, for an agent in pool mode whose pool has no
    node ready: the run waits for one, and starts with the project's harness alone), on the branch chat/<its id>, with
    the repos of the project the worker has a checkout of: of them, the project's harness alone on a daemon of
    ``runs.LAZY_CHAT_REPOS``, whose agent opens the others it needs (``chats.routes.agents.open_repo``). A daemon that clones
    what it lacks (``runs.CLONES_CHECKOUTS``) starts with the harness without a checkout of it too, when a project of
    the owner's lists an origin to clone it from (``repo_origins``)."""
    profile = hand.profile
    listed = await project_repos(conn, profile.project_id)
    checkouts, features = (None, ()) if worker is None else (worker.checkouts, worker.features)
    names = [name for name in listed if f"{profile.project}/{name}" in (checkouts or {})]
    on_hub = profile.runs_on == runs.HUB_RUNTIME  # a job of the hub takes its turns, with no checkout (hub_runtime)
    if worker is None and not on_hub:
        harness = await project_harness(conn, profile.project_id)
        names = [harness] if harness is not None else []
    elif runs.LAZY_CHAT_REPOS in (features or ()):
        harness = await project_harness(conn, profile.project_id)
        if harness is not None and harness not in names and runs.CLONES_CHECKOUTS in (features or ()):
            if harness in await repo_origins(conn, profile.project_id, hand.owner_id, [harness]):
                names.append(harness)
        names = [name for name in names if name == harness]
    version, permissions = await _pinned(conn, profile, hand.message_id)
    allow = profiles.allowed(permissions, "model")
    model = allow[0] if allow else None
    owner, project = hand.owner_id, profile.project_id
    asker = gateway_accounts.Asker(0, owner, owner, project, profile.id, project, agent_owned=not profile.is_default)
    title, runtime = rules.turn_title(profile.handle, hand.chat.title, hand.body), runs.HUB_RUNTIME if on_hub else "any"
    values = {
        "kind": "chat",
        "project_id": profile.project_id,
        "title": title,
        "dispatched_by": hand.owner_id,
        "dispatched_via": hand.run_via,
        "pinned_worker_id": None if worker is None else worker.id,
        "requested_runtime": runtime,
        "runtime": runtime,
        "model": model,
        "gateway_provider": await gateway_accounts.prefix_for(conn, asker, runtime, model),
        "mode": "headless",
        "approval": "auto",
        "timeout_s": rules.TIMEOUT_MIN * 60,
        "profile_id": profile.id,
        "profile_version": version,
        "chat_id": hand.chat.chat_id,
        "chat_message_id": hand.message_id,
        "hop": hand.hop,
        "budget": rules.run_budget(),
        "priority": hand.priority,
    }
    run_as = None if hand.route is None else hand.route.user_id
    who = await run_autonomy.of_message(conn, hand.message_id, run_as, hand.dispatcher_id, names)
    run_id = await insert_run(conn, values, who)
    branch = rules.branch_of(run_id)
    await store.set_branch(conn, run_id, branch, [{"repo": name, "branch": branch} for name in names])
    await system_traces.join(conn, run_id)
    await notify_queued(conn, run_id)
    return run_id


async def _attends(conn: AsyncConnection, hand: Hand) -> bool:
    """Whether ``hand``'s profile is the agent of its settle chat, which every member of its project wakes."""
    if hand.chat.kind != chats.SETTLE:
        return False
    return await store.settle_agent(conn, hand.chat.chat_id) == hand.profile.id


async def _writer_refusal(conn: AsyncConnection, hand: Hand) -> str | None:
    """Why the person at the origin of ``hand`` may not give its agent work (``agent_tasking.task_refusal``)."""
    attends = await _attends(conn, hand)
    return await agent_tasking.task_refusal(conn, hand.profile, hand.dispatcher_id, hand.dispatcher, attends=attends)


async def _run_as_refusal(conn: AsyncConnection, hand: Hand) -> str | None:
    """Why the run_as of ``hand``'s route takes no run of its profile now: they no longer hold writer on the project."""
    profile, route = hand.profile, hand.route
    role = await role_on(conn, route.user_id, profile.project_id)
    if has_role(role, "writer"):
        return None
    return (
        f"@{profile.handle} runs on the workers of {route.login}, who holds {role or 'no grant'} on project "
        f"{profile.project} now, not writer: an admin of the project names another member"
    )


async def _seat_refusal(conn: AsyncConnection, user_id: int, login: str) -> str | None:
    """Why a chat run of ``login`` is refused: their primary seat in the org chart ended (plan org-chart, a5)."""
    return await ended_refusal(conn, user_id, login, what="a chat run")


async def hand_work(conn: AsyncConnection, hand: Hand) -> tuple[int | None, str | None]:
    """(the run the mention reached, None) or (None, why it reached none), in the caller's transaction: the session's
    run that is queued or held takes the message in its inbox; one that ended done, or failed because the remote
    declined its push, is gone on from; else a new session starts on a worker of the dispatcher, or of the run_as of
    the agent's type when it routes its chat runs there (see the module's docstring). A message sent with a run's token
    hands no work and reaches no run's inbox (``run_access_rules.new_run_refusal``)."""
    refusal = run_access_rules.new_run_refusal()
    if refusal is not None:
        return None, refusal
    refusal = await _writer_refusal(conn, hand) or await _seat_refusal(conn, hand.dispatcher_id, hand.dispatcher)
    if refusal is not None:
        return None, refusal
    hand = replace(hand, route=await route_of(conn, hand.profile.id))
    if hand.route is not None:
        refusal = await _run_as_refusal(conn, hand) or await _seat_refusal(conn, hand.route.user_id, hand.route.login)
        if refusal is not None:
            return None, refusal
    if (why := pool_accounts.chat_needs_pool(hand.profile, hand.profile.project)) is not None:
        return None, why
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
        try:
            return await _go_on(conn, session, hand), None
        except autonomy_hold.Stopped as exc:
            return None, exc.detail
    worker, why = await pick_worker(conn, hand)
    if worker is None and why is not None:  # no node can take it, for a reason a free slot would not end
        asker = hand.asker_run_id if hand.asker_run_id is not None else run_access_rules.ACTING_RUN.get()
        if not await ops_queue.ops_run(conn, asker):  # an ops run's own refusal would wake it again, and again
            project_id, chat_id = hand.profile.project_id, hand.chat.chat_id
            await ops_events.mention_refused(
                conn, project_id=project_id, profile_id=hand.profile.id, chat_id=chat_id, message_id=hand.message_id
            )
        return None, why
    try:
        run_id = await _start(conn, hand, worker)
    except autonomy_hold.Stopped as exc:  # refused before anything was written
        return None, exc.detail
    if worker is not None and worker.holding >= worker.slots:
        await _say_queued(conn, hand, run_id, worker)
    return run_id, None


async def _say_queued(conn: AsyncConnection, hand: Hand, run_id: int, worker) -> None:
    """Say in the thread of ``hand`` that its run ``run_id`` waits for a free slot of ``worker``, which every node of
    the agent's pool lacks now, and how many runs queued for that node it takes first (``priority.ahead``)."""
    before = await ahead(conn, run_id, worker.id)
    runs_ahead = f"{before} run{'' if before == 1 else 's'} ahead of it"
    text = f"run #{run_id} of @{hand.profile.handle} is queued for a free slot on node {worker.name}: {runs_ahead}"
    await hub_reply(conn, hand.chat.chat_id, hand.root_id, text)


async def hand_mentions(conn: AsyncConnection, hand: Hand, kept: list[dict]) -> list[str]:
    """Hand work to each agent ``kept`` (the mentions of a person's message, as ``chats.service.messages.resolve_mentions`` keeps them)
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


async def chat_run(conn: AsyncConnection, run_id: int) -> ChatRun | None:
    """Chat run ``run_id``; None for a run of another kind."""
    row = await store.chat_run_row(conn, run_id)
    if row is None:
        return None
    person = row.author_user_id
    if person is None and row.asked_by_run is not None:  # an agent's message: the origin of that agent's run
        person = await origin(conn, row.asked_by_run)
    person, login = person or row.dispatched_by, row.login
    if person != row.dispatched_by:  # the run went to the workers of its agent type's run_as
        login = await store.login_of(conn, person)
    head = (row.id, row.chat_id, row.chat_message_id, row.root_id, row.profile_id)
    return ChatRun(*head, person, login, row.via, row.hop, row.error, row.dispatched_by)


async def agent_access(conn: AsyncConnection, chat_id: int, profile: Profile, dispatcher_id: int) -> ChatAccess | None:
    """Agent ``profile``'s access to chat ``chat_id``, acting for person ``dispatcher_id``: its login is the agent's
    @handle, as the notices name the sender; None when the agent is not in the chat."""
    row = await store.agent_chat(conn, chat_id, profile.id)
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
    output_ids: list[int] | None = None,
) -> tuple[int, list[dict]]:
    """Store a message of agent ``profile`` of run ``run_id`` in ``chat``, in the thread of ``root_id``; (its id, its
    mentions). A person it mentions gets the notice. The agent ``target`` names gets the work ``hand`` asks
    (``_hand_on``), and any other agent it mentions none: a name in the agent's words, in the chat or not, deleted or
    not, the agent itself too ("I am @reviewer"), asks no work and is refused nothing, so nothing shows under it. An agent the run's
    session asked (``agent_asks``, ``_asked``) is refused nothing either, in the chat or not: its mention names the
    latest run asked of it, the work the agent did give it. The message
    carries the outputs of the run ``output_ids`` names and, for a chat run, those no message carries yet
    (``output_store.link_message``)."""
    from evo_agents.hub.server.chats.service.messages import resolve_mentions  # it hands work through this module

    origin = await _principal(conn, chat.user_id)
    kept, people, _ = await resolve_mentions(conn, chat, origin, chats.mention_names(body))
    message_id, _ = await insert_message(
        conn, chat, body, thread_root_id=root_id, author_profile_id=profile.id, run_id=run_id
    )
    others = any(mention["profile_id"] not in (None, profile.id) for mention in kept)
    asked = await store.asked_agents(conn, run_id) if others else {}
    for mention in kept:
        if mention["profile_id"] in asked and mention["name"] != target:
            mention["run_id"], mention["refusal"] = asked[mention["profile_id"]], None
            continue
        if mention["profile_id"] is None:
            continue
        if hand is None or mention["name"] != target:  # named, not asked
            mention["refusal"] = None
            continue
        if mention["refusal"] is not None:
            continue
        handed = replace(hand, message_id=message_id, root_id=root_id or message_id)
        mention["run_id"], mention["refusal"] = await _hand_on(conn, handed, mention["profile_id"], run_id)
    await insert_mentions(conn, message_id, kept)
    await link_message(conn, run_id, message_id, output_ids or [])
    for user_id in dict.fromkeys(people):
        await notice_mention(conn, chat, user_id, message_id, body)
    from evo_agents.hub.server.work_chats.service import copy_out  # it hands work through this module

    await copy_out(conn, chat, profile, run_id, message_id, root_id or message_id, body)  # the coordinator's words
    return message_id, kept


async def _hand_on(
    conn: AsyncConnection, hand: Hand, profile_id: int, run_id: int | None = None
) -> tuple[int | None, str | None]:
    """An agent's mention of the agent ``profile_id``, by run ``run_id``: the work for the origin of the chain one hop
    further, never to the agent's own profile (``hand.profile``). In a trace of an agent system, a mention past the
    trace's budget is refused (``system_traces.budget_problem``), and one the spec has no interacts edge for runs as
    before and is recorded off design, with no reason (``system_traces.record_tool``)."""
    problem = rules.hop_problem(hand.hop, profile_id == hand.profile.id)
    if problem is not None:
        return None, problem
    target = await by_id(conn, profile_id)
    assert target is not None  # a mention names a profile the chat resolved (resolve_mentions)
    found = None if run_id is None else await system_traces.of_run(conn, run_id)
    over = await system_traces.budget_problem(conn, found)
    if over is not None and found is not None:  # budget_problem answers None for a run in no trace
        await system_traces.record_tool(
            conn, found, system_context.TOOL_MENTION, to_handle=target.handle, status="refused", reason=over
        )
        return None, over
    child, why = await hand_work(conn, replace(hand, profile=target, asker_run_id=run_id))
    await system_traces.record_mention(conn, found, target.handle, child, why, hand.message_id)
    return child, why


async def _principal(conn: AsyncConnection, user_id: int):
    from evo_agents.hub.server.security import MACHINE, Principal

    login = await store.login_of(conn, user_id)
    return Principal(user_id, login, False, None, MACHINE, "")


async def agent_reply(conn: AsyncConnection, run_id: int, text: str) -> int:
    """The agent's last message of a turn of chat run ``run_id``, its reply in the thread, kept in the run's log too
    as an author run's chat message is; the seq of that event. An agent taken out of the chat meanwhile keeps it in
    the log alone."""
    found = await chat_run(conn, run_id)
    profile = await run_profile(conn, run_id)
    chat = await agent_access(conn, found.chat_id, profile, found.dispatcher_id)
    message_id = None
    if chat is not None:
        message_id, _ = await post_agent(conn, chat, profile, run_id, text, found.root_id)
    return await write_event(conn, run_id, {"text": text, "chat": "agent", "chat_message_id": message_id})


async def run_profile(conn: AsyncConnection, run_id: int) -> Profile:
    profile_id = await store.profile_of_run(conn, run_id)
    return await by_id(conn, profile_id)


async def hub_access(conn: AsyncConnection, chat_id: int) -> ChatAccess:
    """Chat ``chat_id`` as the hub itself writes in it: no member of its own, the projects of its agents now."""
    projects = await agent_projects(conn, chat_id)
    row = await store.hub_chat(conn, chat_id)
    return ChatAccess(
        chat_id, row.title, row.created_by, row.login, row.created_at, row.event_seq, 0, 0, 0, "hub", projects, row.kind
    )


async def hub_reply(conn: AsyncConnection, chat_id: int, root_id: int | None, text: str) -> int:
    """A message of the hub itself in the thread of ``root_id`` of chat ``chat_id``, or, with no root, one that
    starts none; its id."""
    chat = await hub_access(conn, chat_id)
    message_id, _ = await insert_message(conn, chat, runs.clip(text, chats.MAX_MESSAGE_BYTES), thread_root_id=root_id)
    return message_id


# The run's moves


async def chat_run_moved(conn: AsyncConnection, run_id: int, new: str, old: str | None = None) -> int | None:
    """What chat run ``run_id`` moving to ``new`` does in its chat: the chat's streams woken, since the run's progress
    under its messages changed (``chats.service.progress``); the agent's reaction on the message that started it, the hub's
    reply in the thread when it failed, and when it ended done with messages of its inbox untaken, the run that goes on
    in its session with them (its id), titled by the first of them. A run another agent's run asked that ended, and
    goes on in no run, answers that run's wait (``agent_asks.answer_asker``); a lost one does not, since its next
    attempt carries the wait and the run may come back (``run_reattach``): the attempt that really ends answers. Then the work cards of its traces are
    refreshed (``work_cards``)."""
    went_on = await _moved(conn, run_id, new, old)
    from evo_agents.hub.server.work_cards import run_moved  # it reads the traces' work through this module

    await run_moved(conn, run_id)
    return went_on


async def _moved(conn: AsyncConnection, run_id: int, new: str, old: str | None) -> int | None:
    if new in runs.TERMINAL_STATES:
        await system_traces.end_run(conn, run_id, new)
    if old == "parked" and new == "done":  # it waited for an answer and a new run goes on in its session (go_on_parked)
        return None
    found = await chat_run(conn, run_id)
    if found is None:
        return None
    await wake(conn, found.chat_id)
    await chat_bridge_events.progress(conn, found.chat_id, found.message_id, run_id, new)
    await status_reaction(conn, message_id=found.message_id, profile_id=found.profile_id, state=new)
    if new in runs.TERMINAL_STATES:  # what it sent after its last message goes under that one
        await attach_leftovers(conn, run_id)
    profile = await run_profile(conn, run_id) if new in runs.TERMINAL_STATES else None
    if new == "failed" and profile is not None:  # failed is a terminal state: profile is read
        why = found.error or "its worker gave no reason"
        await hub_reply(conn, found.chat_id, found.root_id, f"Run #{run_id} of @{profile.handle} failed: {why}")
        from evo_agents.hub.server.orchestrator.service.threads import (
            on_turn_failed,  # it hands work through this module
        )

        await on_turn_failed(conn, run_id)
    went_on = await _go_on_untaken(conn, run_id, found) if new == "done" else None
    if profile is not None and went_on is None and new != "lost":  # a lost run's next attempt, or itself, answers
        from evo_agents.hub.server.agents.service.asks import answer_asker  # its route hands work through this module

        await answer_asker(conn, run_id, new, profile.handle)
    return went_on


async def _go_on_untaken(conn: AsyncConnection, run_id: int, found: ChatRun) -> int | None:
    """The run that goes on in the session of chat run ``run_id``, which ended done, with the messages of its inbox
    untaken; None when there are none, or it has no session."""
    untaken = await store.untaken(conn, run_id)
    session_id = await store.session_of(conn, run_id)
    if not untaken or not session_id:
        return None
    values = {
        "dispatched_by": found.dispatched_by,
        "dispatched_via": found.via,
        "chat_message_id": found.message_id,
        "hop": found.hop,
        "budget": rules.run_budget(),
    }
    values = _titled(values, await _woken_title(conn, run_id, rules.woken_text(untaken[0].body)))
    values = await _pooled(conn, run_id, values)
    new_id = await store.continue_run(conn, run_id, values)
    await store.move_inbox(conn, [row.id for row in untaken], new_id)
    await write_event(conn, new_id, {"text": f"goes on in the session of run #{run_id} with its messages untaken"})
    await system_traces.join(conn, new_id, previous_id=run_id)
    await notify_queued(conn, new_id)
    log.info("chat run goes on with its inbox", extra={"run_id": new_id, "resume_of": run_id})
    return new_id

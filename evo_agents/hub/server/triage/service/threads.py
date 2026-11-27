"""The thread a failed run opens in the triage chat (``evo_agents.hub.tables_triage``), with no model call.

When a run of kind step, plan or author ends ``failed``, ``open_thread`` finds or makes the triage chat of its project
("Triage: <project>"), one for all its plans and its runs with no plan, with the run's owner and the project's default
agent in it, and writes the first message of the thread: what the hub knows of the failure (state, cause and whether the
hub guessed it, error, plan and step, repo and branch, the last verify, links to the run's page, log and diff, and who
could run it again: ``runs.service.candidates``) and, last, the mention of the agent, which starts a chat run for the
owner at hop 1 (``chats.service.runs.hand_work``: the writer role and a worker of the owner decide, and a refusal is said in
the thread). A run has one thread: ``run_triage`` holds it, so a second call finds it. Review, judge,
chat and local runs open none, so the failure of a chat run of triage starts no triage. The switch of a project is
``projects.triage_enabled``; the hub's ``chats`` feature must be on. The first message names the plan and the step, so
each thread says which plan it is about.

Everyone in the project's chat clears its label, which holds the project's default label (``chats.service.access``), so they read
by grant what is labelled at or below it, as the project's runs on no plan are. A plan whose label is not at or below it
(a higher level or location, a label that names another project or cannot be read), or that is no longer on the hub,
keeps a chat of its own, "Triage: <plan>", as every plan had before (``triage.service.chats.apart``): there its failures reach the owners of
its runs, who could read the plan when they dispatched them. So does a run whose owner the project's chat cannot take
(an agent of another project invited into it brings a label they do not clear, or the chat is full); a run with no plan
then opens no thread. The triage chats each plan had before stay as they are, with their threads.

A run the hub stopped on a usage limit or a refused login (cause ``rate_limit`` or ``account_auth``, an ``account_event``
stop in its log) while no account of its owner for that variable is ready for the project gets the thread and the facts,
and no agent: the chat run of the agent would take the same accounts (``accounts.service.choice``) and fail at once. The thread
says when the first of them is ready again.

A run that failed for a program its worker lacks (``triage_rules.lacks_program``) and that no other worker is left to
take (``runs.service.reroute``) has its agent put on a worker it was tried on, its owner's own before a shared one
(``triage.service.facts.install_worker``), so the agent installs the program on that machine and runs the run again from there.

A run that stops moving gets a thread too, once (``run_triage`` holds it): one running or verifying that has written
no event for STALE_MINUTES while its worker still heartbeats, which each pass of the reaper finds (``open_stale``), with
the cause ``stale``; and one the reaper failed on its last attempt (cause ``lease_lost``), as any failed run. The first
message of both adds the diagnosis (``triage.service.facts.diagnosis``): the last event, the worker and its last heartbeat, the lease and
the attempt. The agent of a stale run's thread may ask its worker to stop it (``triage.service.actions``, ``triage/cancel``).

``on_failed`` does it in a savepoint of the caller's transaction: whatever goes wrong inside (a label the owner does
not clear, a full chat, a database error) rolls back to it and leaves the run failed with the notice it always had.

``project_thread`` is the hub's thread of something that is no failed run, a release cycle that stopped
(``release_cycles``): the hub writes it in the project's triage chat, with its owner put in it, and mentions no agent,
since the owner decides. ``asking_thread`` is the same thread with the project's default agent mentioned, and
``tell_thread`` adds the hub's message to the thread a failed run opened: the triage of a work item of the Loop that
failed twice (``loop.stall``). ``asking_reply`` adds the hub's message to a thread ``asking_thread`` opened and mentions
the agent again: a proposal of the Curator open again after a defer (``curator.service.threads``).
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub import chats, features, runs, triage_rules
from evo_agents.hub.server import hub_settings
from evo_agents.hub.server.chats.models.runs import Hand
from evo_agents.hub.server.chats.repository.store import insert_mentions
from evo_agents.hub.server.chats.service.messages import resolve_mentions
from evo_agents.hub.server.chats.service.posting import insert_message
from evo_agents.hub.server.chats.service.runs import hand_mentions, hub_reply
from evo_agents.hub.server.profile_store import ensure_default
from evo_agents.hub.server.run_state import RunStep, run_step
from evo_agents.hub.server.security import MACHINE, Principal
from evo_agents.hub.server.triage.models.threads import (
    KINDS,
    STALE_BATCH,
    STALE_CAUSE,
    About,
    Thread,
    install_lines,
    thread_at,
)
from evo_agents.hub.server.triage.repository import threads as store
from evo_agents.hub.server.triage.service.chats import chat_of, joined, person
from evo_agents.hub.server.triage.service.facts import (
    candidates,
    chain_of,
    diagnosis,
    head_lines,
    install_worker,
    no_account,
)

log = logging.getLogger(__name__)


async def thread_of(conn: AsyncConnection, run_id: int) -> Thread | None:
    """The thread run ``run_id`` opened, or None."""
    row = await store.triage_row(conn, run_id)
    return None if row is None else thread_at(row.chat_id, row.root_message_id)


async def on_failed(conn: AsyncConnection, found: RunStep) -> Thread | None:
    """The thread failed run ``found`` has, opened now unless it is not due or cannot be opened; None then, and the
    caller sends the notice a failure always had."""
    if found.kind not in KINDS or found.project_id is None:
        return None
    led = await orchestrated(conn, found, stale=False)
    if led is not None:
        return led
    try:
        async with conn.begin_nested():
            return await open_thread(conn, found)
    except Exception as exc:  # a thread is a help: nothing inside it may undo the failure it is about
        log.warning(
            "triage thread not opened",
            extra={"run_id": found.run_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return None


async def orchestrated(conn: AsyncConnection, found: RunStep, *, stale: bool) -> Thread | None:
    """The thread the Orchestrator of the agent of run ``found`` gets of it in its system's ops chat, in place of the
    project's triage chat (``orchestrator.on_run``); None for a run with a thread already, or of an agent no
    Orchestrator orchestrates."""
    from evo_agents.hub.server.orchestrator.service import threads as orchestrator  # it opens its chat here

    if await thread_of(conn, found.run_id) is not None:
        return None
    opened = await orchestrator.on_run(conn, found, head_lines(found, False, stale=stale), stale=stale)
    return None if opened is None else thread_at(opened.chat_id, opened.root_id)


async def due(conn: AsyncConnection, project_id: int) -> bool:
    """Whether the hub's chats and the project's triage are on."""
    instance, _ = await hub_settings.feature_values(conn)
    if not features.enabled(features.BY_ID["chats"], instance):
        return False
    return await store.triage_enabled(conn, project_id)


async def project_thread(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, body: str
) -> Thread | None:
    """A thread of the hub's in the project's triage chat whose first message is ``body``, the member ``owner_id``
    put in the chat, no agent mentioned; None while the project's triage, or the hub's chats, are off. The caller
    holds a savepoint: a chat that cannot take the owner raises."""
    if not await due(conn, project_id):
        return None
    owner = await person(conn, owner_id)
    await store.lock(conn, project_id)
    profile = await ensure_default(conn, project_id)
    chat = await joined(conn, About(project_id, project), owner, profile, None)
    message_id, _ = await insert_message(
        conn, chat, runs.clip(body, chats.MAX_MESSAGE_BYTES - 200), thread_root_id=None
    )
    log.info("triage thread opened", extra={"project": project, "chat_id": chat.chat_id, "message_id": message_id})
    return thread_at(chat.chat_id, message_id)


async def asking_thread(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, body: str
) -> tuple[Thread, int | None] | None:
    """``project_thread`` with the project's default agent mentioned, as the thread of a failed run has it: (the
    thread, the chat run it starts for the owner at hop 1, None when that is refused, which the thread says); None
    while the project's triage, or the hub's chats, are off. The Loop's triage of a work item whose run opened no
    thread of its own (``loop.stall``). The caller holds a savepoint."""
    if not await due(conn, project_id):
        return None
    owner = await person(conn, owner_id)
    await store.lock(conn, project_id)
    profile = await ensure_default(conn, project_id)
    chat = await joined(conn, About(project_id, project), owner, profile, None)
    text = runs.clip(body, chats.MAX_MESSAGE_BYTES - 200) + f"\n\n@{profile.handle}"
    message_id, _ = await insert_message(conn, chat, text, thread_root_id=None)
    agent_run = await ask_agent(conn, None, chat, profile, owner, message_id, text)
    log.info("triage thread opened", extra={"project": project, "chat_id": chat.chat_id, "agent_run_id": agent_run})
    return thread_at(chat.chat_id, message_id), agent_run


async def asking_reply(
    conn: AsyncConnection, project_id: int, project: str, owner_id: int, thread: Thread, body: str
) -> tuple[Thread, int | None] | None:
    """A message of the hub in ``thread`` of the project's triage chat that mentions the project's default agent, as
    the first message of ``asking_thread`` does: (the thread, the chat run it reaches for the owner, a new one or the
    session's, which the message goes on in; None when that is refused, which the thread says); None while the
    project's triage, or the hub's chats, are off, or when ``thread`` is not in the project's triage chat. The caller
    holds a savepoint."""
    if not await due(conn, project_id):
        return None
    owner = await person(conn, owner_id)
    await store.lock(conn, project_id)
    profile = await ensure_default(conn, project_id)
    chat = await joined(conn, About(project_id, project), owner, profile, None)
    if chat.chat_id != thread.chat_id:
        return None
    text = runs.clip(body, chats.MAX_MESSAGE_BYTES - 200) + f"\n\n@{profile.handle}"
    message_id, _ = await insert_message(conn, chat, text, thread_root_id=thread.root_id)
    agent_run = await ask_agent(conn, None, chat, profile, owner, message_id, text, root_id=thread.root_id)
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
    agent_run = await store.agent_run_of(conn, run_id)
    await hub_reply(conn, thread.chat_id, thread.root_id, runs.clip(body, chats.MAX_MESSAGE_BYTES - 200))
    return thread, agent_run


async def ask_agent(
    conn: AsyncConnection,
    found: RunStep | None,
    chat,
    profile,
    owner: Principal,
    message_id: int,
    body: str,
    *,
    root_id: int | None = None,
    on_worker: int | None = None,
) -> int | None:
    """Hand message ``message_id``, the first of the thread or one in the thread of ``root_id``, to ``profile`` for the
    owner (hop 1), with the credential failed run ``found`` was dispatched with (a machine's without one), on worker
    ``on_worker`` when it can take it; the chat run it made, if any. A refusal (the writer role, a worker) is said in
    the thread."""
    kept, _, _ = await resolve_mentions(conn, chat, owner, [profile.handle])
    via = MACHINE if found is None else await store.dispatched_via(conn, found.run_id, MACHINE)
    root = message_id if root_id is None else root_id
    hand = Hand(
        chat, message_id, root, profile, owner.user_id, owner.login, via, 1, "the hub", body, on_worker=on_worker
    )
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
    (``triage.service.facts.no_account``). A stale run, and one the reaper failed on its last attempt (cause ``lease_lost``), also get the
    diagnosis: the last event, the worker, the lease and the attempt (``triage.service.facts.diagnosis``)."""
    existing = await thread_of(conn, found.run_id)
    if existing is not None or not await due(conn, found.project_id):  # pyright: ignore[reportArgumentType]
        return existing
    owner = await person(conn, found.dispatcher_id)
    chat, profile = await chat_of(conn, found, owner)
    inferred = False if stale else await store.cause_inferred(conn, found.run_id)
    depth, _ = await chain_of(conn, found.run_id)
    full = depth >= triage_rules.RERUN_CAP
    waiting = None if full or stale else await no_account(conn, found)
    lines = head_lines(found, inferred, stale=stale)
    if stale or found.failure_cause == "lease_lost":
        lines += await diagnosis(conn, found)
    if stale:
        lines.append(
            "The agent of this thread may ask its worker to stop it (`evo-agents worker triage cancel`) and, once it "
            "has ended, run it again on a fit worker (`evo-agents worker triage rerun`)."
        )
    lines += await candidates(conn, found, await store.role_of(conn, owner.user_id, found.project_id), chat)  # pyright: ignore[reportArgumentType]
    if full:
        lines.append(
            f"The hub ran this step again {depth} times by itself already, the most: it asks no agent, and the "
            "owner decides."
        )
    elif waiting is not None:
        lines.append(waiting)
    ask = not full and waiting is None
    install = await install_worker(conn, found) if ask and not stale else None
    if install is not None:
        lines += install_lines(install[1], install[2])
    on_worker = None if install is None else install[0]
    body = runs.clip("\n".join(lines), chats.MAX_MESSAGE_BYTES - 200) + (f"\n\n@{profile.handle}" if ask else "")
    message_id, _ = await insert_message(conn, chat, body, thread_root_id=None)
    agent_run = (
        await ask_agent(conn, found, chat, profile, owner, message_id, body, on_worker=on_worker) if ask else None
    )
    await store.insert_run_triage(
        conn,
        run_id=found.run_id,
        chat_id=chat.chat_id,
        root_message_id=message_id,
        cause=STALE_CAUSE if stale else found.failure_cause,
        cause_inferred=inferred,
        agent_run_id=agent_run,
        chain_depth=depth,
    )
    log.info(
        "triage thread opened",
        extra={"run_id": found.run_id, "chat_id": chat.chat_id, "agent_run_id": agent_run, "stale": stale},
    )
    return thread_at(chat.chat_id, message_id)


async def on_stale(conn: AsyncConnection, found: RunStep) -> Thread | None:
    """The thread stale run ``found`` has, opened now unless it cannot be; in a savepoint, as ``on_failed``."""
    if found.kind not in KINDS or found.project_id is None:
        return None
    led = await orchestrated(conn, found, stale=True)
    if led is not None:
        return led
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
    """One pass, of the reaper (``run_reaper.recover_runs``): a thread for each stale run
    (``triage.repository.threads.stale_runs``), each in a transaction of its own that locks the run and checks again;
    a run has one thread, so it is opened once. How many were opened."""
    async with engine.connect() as conn:
        found = await store.stale_ids(conn, batch)
    opened = 0
    for run_id in found:
        async with engine.begin() as conn:
            if not await store.lock_stale(conn, run_id):  # it moved, wrote an event or has a thread now
                continue
            thread = await on_stale(conn, await run_step(conn, run_id))
            opened += thread is not None
    return opened

"""The prompt of a chat run (``evo_agents.hub.chat_runs`` holds its rules): the thread the agent answers, as the people
it is cleared for read it, the repos of its project and the members of its chat. Split from
``evo_agents.hub.server.chat_runs``, which hands work to agents and posts what they write.

``run_clearers`` names the people what a chat run reads must be cleared for: the person who started it and the member
whose worker runs it, when another. ``chat_prompt`` builds the prompt the worker gives the agent, and the hub's own
runtime gives a hub agent (``hub_runtime``).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import chats, runs, tables
from evo_agents.hub.server import attachment_store, system_traces
from evo_agents.hub.server.chat_access import LabelProject, agent_projects, label_reader
from evo_agents.hub.server.chat_runs import ChatRun, chat_run, project_harness, project_repos, run_profile
from evo_agents.hub.server.profile_store import Profile


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
    opened = [entry["repo"] for entry in repos if isinstance(entry, dict) and isinstance(entry.get("repo"), str)]
    held = {name for name in [*names, *opened] if f"{profile.project}/{name}" in checkouts}
    listing = rules.ranked_repos(names, opened, held, rules.named_repos(names, texts))
    return listing, await project_harness(conn, profile.project_id)


async def run_clearers(conn: AsyncConnection, run_id: int) -> list[int] | None:
    """The people what chat run ``run_id`` reads must be cleared for, as its prompt is; None for another kind of run."""
    found = await chat_run(conn, run_id)
    return None if found is None else _clearers(found)


async def chat_prompt(
    conn: AsyncConnection,
    run_id: int,
    branch: str | None,
    repos: list,
    features: tuple[str, ...] = (),
    *,
    ships: bool = False,
) -> str:
    """The prompt of chat run ``run_id`` as its worker claims it (``chat_runs.build_chat_prompt``): the snapshot holds
    the messages its dispatcher clears the label of, and the member whose worker runs it too. Before the thread it
    shows the messages that start no thread, or in a direct chat the latest turns (``chat_runs.snapshot``). On a daemon
    that lists ``runs.LAZY_CHAT_REPOS`` among its ``features``, every repo of the project, those the message that woke
    it or a message of the thread the prompt shows names before the others not open (``_listing``). With ``ships``, its
    principal's card leaves merging to the agent, and its rules say so. Its rules let an interactive answer load
    libraries from the CDNs only while the chat is not restricted, as it is at the claim."""
    from evo_agents.hub.server.chat_views import message_select, profiles_by_id

    found = await chat_run(conn, run_id)
    assert found is not None  # run_specs asks for the prompt of a chat run alone, which a message started
    profile = await run_profile(conn, run_id)
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
    files = (await attachment_store.of_messages(conn, [found.message_id])).get(found.message_id, [])
    if files:  # the worker saves them before the first turn and lists their paths after the prompt
        trigger["files"] = [{"name": f.name, "media_type": f.media_type, "size": f.size} for f in files]
    root = shown.pop(root_id, None) if root_id != found.message_id else None
    blocks = rules.snapshot(
        root,
        [shown[row.id] for row in thread_rows if row.id in shown and row.thread_root_id == root_id],
        [shown[row.id] for row in before_rows if row.id in shown],
        direct=direct,
    )
    people, agents = await _member_names(conn, found.chat_id)
    projects = await agent_projects(conn, found.chat_id)
    listing, harness = None, None
    if runs.LAZY_CHAT_REPOS in features:
        listing, harness = await _listing(conn, run_id, profile, repos, [trigger["body"], *in_thread])
    system, brief = await system_traces.prompt_parts(conn, run_id)
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
        ships=ships,
        label=_lowered(projects, profile.project),
        cdn=not any(p.restricted for p in projects),  # as ChatAccess.restricted reads it, now
        system=system,
        brief=brief,
    )


def _lowered(projects: Sequence[LabelProject], project: str) -> dict | None:
    """{level, default} when the chat, of the agents' ``projects`` (``agent_projects``), was set below the default
    level of ``project``; None otherwise."""
    found = next((p for p in projects if p.name == project), None)
    if found is None or not found.lowered:
        return None
    return {"level": found.label["level"], "default": found.default_level}


async def _member_names(conn: AsyncConnection, chat_id: int) -> tuple[list[str], list[str]]:
    from evo_agents.hub.server.chat_views import member_names

    return (await member_names(conn, [chat_id]))[chat_id]

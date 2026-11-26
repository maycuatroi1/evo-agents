"""The queries of chat runs (``chats.service.runs``): the repos of a project, the sessions of a thread, the inbox of a
run, the run that goes on in a session, and what a chat run and the hub's own access to a chat read."""

from __future__ import annotations

from sqlalchemy import func, insert, literal, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import chat_runs as rules
from evo_agents.hub import tables
from evo_agents.hub.author import harness_repo
from evo_agents.hub.server.chats.models.runs import CONTINUED, MISSED_MESSAGES
from evo_agents.hub.server.runs.service.worker_fit import workers_of

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


# Sessions


def _sessions(chat_id: int, root_id: int, dispatcher_id: int | None, profile_id: int | None = None):
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
            func.coalesce(m.c.thread_root_id, m.c.id) == root_id,
        )
        .order_by(r.c.id.desc())
    )
    query = query if dispatcher_id is None else query.where(r.c.dispatched_by == dispatcher_id)
    return query if profile_id is None else query.where(r.c.profile_id == profile_id)


async def last_node(conn: AsyncConnection, chat_id: int, root_id: int, profile_id: int) -> int | None:
    """The node that ran the latest turn of agent ``profile_id`` in the thread of ``root_id``, for any dispatcher; None
    when it ran none."""
    query = _sessions(chat_id, root_id, None, profile_id).where(tables.runs.c.worker_id.is_not(None))
    row = (await conn.execute(query.limit(1))).one_or_none()
    return None if row is None else row.worker_id


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


async def latest_session(conn: AsyncConnection, chat_id: int, root_id: int, owner_id: int, profile_id: int):
    """The latest chat run of ``profile_id`` in the thread of ``root_id`` dispatched by ``owner_id``, its row locked;
    None when there is none."""
    query = _sessions(chat_id, root_id, owner_id, profile_id)
    return (await conn.execute(query.limit(1).with_for_update(of=tables.runs))).one_or_none()


async def sessions_of(conn: AsyncConnection, chat_id: int, root_id: int, owner_id: int, profile_id: int) -> list:
    """The chat runs of ``profile_id`` in the thread of ``root_id`` dispatched by ``owner_id``, the latest first."""
    return list((await conn.execute(_sessions(chat_id, root_id, owner_id, profile_id))).all())


async def lock_session(conn: AsyncConnection, chat_id: int, root_id: int, owner_id: int, profile_id: int, run_id: int):
    """Run ``run_id`` of ``sessions_of``, its row locked."""
    query = _sessions(chat_id, root_id, owner_id, profile_id)
    locked = query.where(tables.runs.c.id == run_id).with_for_update(of=tables.runs)
    return (await conn.execute(locked)).one()


async def session_ids(conn: AsyncConnection, run_ids: set[int]) -> dict[int, str | None]:
    """The session of each run of ``run_ids``, by run id."""
    r = tables.runs
    query = select(r.c.id, r.c.session_id).where(r.c.id.in_(run_ids))
    return {row.id: row.session_id for row in await conn.execute(query)}


async def session_of(conn: AsyncConnection, run_id: int) -> str | None:
    r = tables.runs
    return (await conn.execute(select(r.c.session_id).where(r.c.id == run_id))).one().session_id


async def insert_inbox(conn: AsyncConnection, run_id: int, sent_by: int, body: str) -> int:
    """A message ``sent_by`` left in the inbox of run ``run_id``; its id."""
    i = tables.run_inbox
    left = insert(i).values(run_id=run_id, sent_by=sent_by, body=body).returning(i.c.id)
    return (await conn.execute(left)).scalar_one()


def _continue_run(previous: int, values: dict):
    """The run that goes on in the session of chat run ``previous``: its project, profile, repos, branch, runtime and
    model, pinned to its worker, in its session, with the timeout of a chat run today (``rules.TIMEOUT_MIN``, not that
    of ``previous``); ``values`` names the rest, and a title (``_titled``), or else the run takes the title of
    ``previous``."""
    r = tables.runs
    values = {"timeout_s": rules.TIMEOUT_MIN * 60, **values}
    copied = tuple(name for name in CONTINUED if name not in values)
    pin = [] if "pinned_worker_id" in values else [r.c.worker_id.label("pinned_worker_id")]  # values may unpin it
    source = select(
        *(r.c[name] for name in copied),
        *pin,
        r.c.runtime.label("requested_runtime"),
        r.c.id.label("resume_of_run_id"),
        *(literal(value, r.c[name].type).label(name) for name, value in values.items()),
    ).where(r.c.id == previous)
    columns = [*copied, *(["pinned_worker_id"] if pin else []), "requested_runtime", "resume_of_run_id", *values]
    return insert(r).from_select(columns, source).returning(r.c.id)


async def continue_run(conn: AsyncConnection, previous: int, values: dict) -> int:
    """Insert the run that goes on in the session of chat run ``previous`` (``_continue_run``); its id."""
    return (await conn.execute(_continue_run(previous, values))).scalar_one()


async def woken_row(conn: AsyncConnection, run_id: int):
    """The handle of the agent and the title of the chat of run ``run_id``; None when it has no profile or chat."""
    r, c, ap = tables.runs, tables.chats, tables.agent_profiles
    query = (
        select(ap.c.handle, c.c.title)
        .select_from(r.join(c, c.c.id == r.c.chat_id).join(ap, ap.c.id == r.c.profile_id))
        .where(r.c.id == run_id)
    )
    return (await conn.execute(query)).one_or_none()


async def missed_rows(conn: AsyncConnection, previous_id: int, root_id: int, dispatcher_id: int, before: int) -> list:
    """The messages ``dispatcher_id`` wrote in the thread of ``root_id`` after the one that started chat run
    ``previous_id`` and before message ``before``, that handed no agent work, the latest first, at most
    MISSED_MESSAGES."""
    r, m, mn = tables.runs, tables.chat_messages, tables.chat_mentions
    started = select(r.c.chat_message_id).where(r.c.id == previous_id).scalar_subquery()
    handed = select(mn.c.message_id).where(mn.c.message_id == m.c.id, mn.c.profile_id.is_not(None)).exists()
    query = (
        select(m.c.id, m.c.body)
        .where(
            m.c.thread_root_id == root_id,
            m.c.author_user_id == dispatcher_id,
            m.c.id > started,
            m.c.id < before,
            ~handed,
        )
        .order_by(m.c.id.desc())
        .limit(MISSED_MESSAGES)
    )
    return list((await conn.execute(query)).all())


async def profile_worker(conn: AsyncConnection, run_id: int):
    """The profile and worker of run ``run_id``."""
    r = tables.runs
    return (await conn.execute(select(r.c.profile_id, r.c.worker_id).where(r.c.id == run_id))).one()


async def parked_row(conn: AsyncConnection, run_id: int):
    """What the run that goes on in the session of parked run ``run_id`` takes of it."""
    r = tables.runs
    query = select(
        r.c.dispatched_by,
        r.c.dispatched_via,
        r.c.chat_id,
        r.c.chat_message_id,
        r.c.hop,
        r.c.run_seconds,
    ).where(r.c.id == run_id)
    return (await conn.execute(query)).one()


async def hand_over(conn: AsyncConnection, parked_id: int, new_id: int) -> None:
    """Move the open decisions of run ``parked_id`` and the messages of its inbox not handed to the agent to run
    ``new_id``."""
    d, i = tables.decisions, tables.run_inbox
    await conn.execute(update(d).values(run_id=new_id).where(d.c.run_id == parked_id, d.c.state == "open"))
    await conn.execute(update(i).values(run_id=new_id).where(i.c.run_id == parked_id, i.c.delivered_at.is_(None)))


async def set_branch(conn: AsyncConnection, run_id: int, branch: str, repos: list[dict]) -> None:
    """Put run ``run_id`` on ``branch`` in ``repos``; none: SQL NULL, not a JSON null."""
    r = tables.runs
    await conn.execute(update(r).values(branch=branch, repos=repos or null()).where(r.c.id == run_id))


async def settle_agent(conn: AsyncConnection, chat_id: int) -> int | None:
    """The agent profile of chat ``chat_id`` of its own (``direct_profile_id``)."""
    c = tables.chats
    return (await conn.execute(select(c.c.direct_profile_id).where(c.c.id == chat_id))).scalar_one()


# The agent's messages


async def chat_run_row(conn: AsyncConnection, run_id: int):
    """Chat run ``run_id`` with the message that started it and its dispatcher; None for a run of another kind."""
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
    return (await conn.execute(query)).one_or_none()


async def login_of(conn: AsyncConnection, user_id: int) -> str:
    u = tables.users
    return (await conn.execute(select(u.c.login).where(u.c.id == user_id))).scalar_one()


async def agent_chat(conn: AsyncConnection, chat_id: int, profile_id: int):
    """Chat ``chat_id`` with the membership of agent ``profile_id`` in it, as ``ChatAccess`` takes it, then its kind;
    None when the agent is not in the chat."""
    c, m, u = tables.chats, tables.chat_members, tables.users
    member = (m.c.chat_id == c.c.id) & (m.c.profile_id == profile_id) & m.c.removed_at.is_(None)
    head = (c.c.id, c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq, m.c.id, m.c.last_read_seq)
    query = select(*head, c.c.kind).select_from(c.join(m, member).join(u, u.c.id == c.c.created_by))
    return (await conn.execute(query.where(c.c.id == chat_id))).one_or_none()


async def asked_agents(conn: AsyncConnection, run_id: int) -> dict[int, int]:
    """The agents that run ``run_id`` asked, it or the parked runs it goes on from (``resume_of_run_id``), through
    ``agent_asks``: profile id to the latest run asked of it."""
    r = tables.runs
    chain = select(r.c.id, r.c.resume_of_run_id).where(r.c.id == run_id).cte("asking", recursive=True)
    earlier = r.alias("earlier")
    chain = chain.union(
        select(earlier.c.id, earlier.c.resume_of_run_id).join_from(
            earlier, chain, earlier.c.id == chain.c.resume_of_run_id
        )
    )
    query = (
        select(r.c.profile_id, func.max(r.c.id))
        .join_from(r, chain, r.c.spawned_by_run_id == chain.c.id)
        .where(r.c.profile_id.is_not(None))
        .group_by(r.c.profile_id)
    )
    return {profile_id: asked for profile_id, asked in await conn.execute(query)}


async def profile_of_run(conn: AsyncConnection, run_id: int) -> int:
    """The profile of chat run ``run_id``, which a chat run always has."""
    r = tables.runs
    return (await conn.execute(select(r.c.profile_id).where(r.c.id == run_id))).scalar_one()


async def hub_chat(conn: AsyncConnection, chat_id: int):
    """Chat ``chat_id``'s title, creator (id and login), creation, event seq and kind."""
    c, u = tables.chats, tables.users
    query = (
        select(c.c.title, c.c.created_by, u.c.login, c.c.created_at, c.c.event_seq, c.c.kind)
        .join_from(c, u, u.c.id == c.c.created_by)
        .where(c.c.id == chat_id)
    )
    return (await conn.execute(query)).one()


# The run's moves


async def untaken(conn: AsyncConnection, run_id: int) -> list:
    """The messages of the inbox of run ``run_id`` its worker did not hand to the agent, but answers, oldest first."""
    i = tables.run_inbox
    query = select(i.c.id, i.c.body).where(i.c.run_id == run_id, i.c.delivered_at.is_(None), i.c.decision_id.is_(None))
    return list((await conn.execute(query.order_by(i.c.id))).all())


async def move_inbox(conn: AsyncConnection, inbox_ids: list[int], run_id: int) -> None:
    """Move the messages ``inbox_ids`` of an inbox to run ``run_id``."""
    i = tables.run_inbox
    await conn.execute(update(i).values(run_id=run_id).where(i.c.id.in_(inbox_ids)))


async def pool_workers(conn: AsyncConnection, owner_id: int, project_id: int, pool_id: int) -> list:
    """The workers of ``owner_id`` that serve project ``project_id`` in pool ``pool_id``, as ``worker_fit.workers_of``
    reads them."""
    return list((await conn.execute(workers_of(owner_id, project_id, pool_id=pool_id))).all())

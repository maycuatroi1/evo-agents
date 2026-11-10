"""The work chat of a trace of an agent system (``evo_agents.hub.server.work_chats``; plan work-chats, step 1): the
first hand-off of a trace opens one chat of kind work, which ``system_traces.work_chat_id`` names, with the person who
opened the trace, the coordinator (the entry agent) and each agent the work goes to; concurrent hand-offs open one. An
ask's question opens the thread of the agent asked there, and asking it again goes back to that thread; the lines of
the hub go there too. The coordinator keeps its one session, the thread of the trace's first message: what it writes
there is copied onto the work chat's main line, a copy that hands no work, and a person's message on that line that
mentions no one goes to that session. An agent of another project, or one the chat cannot take, gets the work in the
direct chat, with a line in the work chat. A thread of the Orchestrator about the trace opens in the work chat, and
the trace is read by the readers of either chat.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import chats, tables
from evo_agents.hub.server import work_chats
from evo_agents.hub.server.security import MACHINE, Principal
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import sql
from tests.hub.test_agent_delegation import KB, KB_AGENT, kb_node, kb_project
from tests.hub.test_chat_runs import (  # noqa: F401
    OTHER,
    OWNER,
    PROJECT,
    agent,
    app_key,
    chat_worker,
    claim,
    client,
    created,
    ended,
    hub,
    in_chats,
    inbox,
    moved,
    recover,
    reply,
    sent,
    started,
)
from tests.hub.test_system_traces import SPEC, put_system, traces_path

QUESTION = "fix the retry loop of the wait helper"


def asked(client, worker: dict, run_id: int, handle: str, body: str = QUESTION, **brief) -> dict:
    response = in_chats(client, worker, run_id, "agent/asks", {"handle": handle, "body": body, **brief})
    assert response.status_code == 201, response.text
    return response.json()


def desk(client, hub, spec: dict = SPEC) -> tuple:
    """The system review (in -> reviewer -> fixer) in a chat of @reviewer alone, a worker for each agent, and the
    owner's run of @reviewer, running: (studio, fixer node, root chat, the owner's message, the run)."""
    put_system(client, hub["owner"], spec)
    studio = chat_worker(client, hub["owner"], "studio", slots=3)
    fixer = chat_worker(client, hub["owner"], "fixer-node", slots=3, pool="fixer")
    chat = created(client, hub["owner"])
    mention, spec_run = started(client, hub, studio, chat)
    return studio, fixer, chat, mention, spec_run["id"]


def claimed(client, worker: dict) -> int:
    spec = claim(client, worker)
    assert spec is not None, "a run to claim"
    return spec["id"]


def rows(db, statement) -> list[tuple]:
    return sql(db, statement) or []


def work_of(db) -> tuple[int, int | None]:
    t = tables.system_traces
    ((trace_id, work),) = rows(db, select(t.c.id, t.c.work_chat_id))
    return trace_id, work


def opened(db) -> tuple[int, int]:
    """The trace and its work chat, which it has."""
    trace_id, work = work_of(db)
    assert work is not None, "the trace has a work chat"
    return trace_id, work


def members_of(db, chat_id: int) -> tuple[set, set]:
    cm, u, p = tables.chat_members, tables.users, tables.agent_profiles
    query = (
        select(u.c.login, p.c.handle)
        .select_from(cm.outerjoin(u, u.c.id == cm.c.user_id).outerjoin(p, p.c.id == cm.c.profile_id))
        .where(cm.c.chat_id == chat_id, cm.c.removed_at.is_(None))
    )
    found = rows(db, query)
    return {login for login, _ in found if login}, {handle for _, handle in found if handle}


def messages_in(db, chat_id: int) -> list[dict]:
    m = tables.chat_messages
    query = select(m.c.id, m.c.body, m.c.thread_root_id, m.c.copied_from_id, m.c.run_id).where(m.c.chat_id == chat_id)
    return [dict(zip(("id", "body", "root", "copied", "run_id"), row, strict=True)) for row in rows(db, query)]


def asking(item: dict, text: str) -> bool:
    """Whether chat message ``item`` is the question of @reviewer that asks @fixer ``text``."""
    return item["body"].startswith("@fixer, @reviewer") and text in item["body"]


def run_row(db, run_id: int) -> tuple:
    r, m = tables.runs, tables.chat_messages
    root = func.coalesce(m.c.thread_root_id, m.c.id)
    query = select(r.c.chat_id, root, r.c.state).join_from(r, m, m.c.id == r.c.chat_message_id).where(r.c.id == run_id)
    return rows(db, query)[0]


def chat_runs(db) -> list[int]:
    r = tables.runs
    return [row[0] for row in rows(db, select(r.c.id).where(r.c.kind == "chat").order_by(r.c.id))]


def test_a_trace_with_no_hand_off_has_no_work_chat(client, hub, hub_db):
    desk(client, hub)
    _, work = work_of(hub_db)
    assert work is None
    assert rows(hub_db, select(func.count()).where(tables.chats.c.kind == chats.WORK)) == [(0,)]


def test_the_first_ask_opens_one_work_chat_where_the_question_opens_the_thread_of_the_agent_asked(client, hub, hub_db):
    studio, fixer, chat, mention, parent = desk(client, hub)
    made = asked(client, studio, parent, "fixer")
    child = made["run_id"]
    assert made["refusal"] is None and child is not None
    trace_id, work = opened(hub_db)
    assert work != chat["id"]
    c = tables.chats
    kind, title, by = rows(hub_db, select(c.c.kind, c.c.title, c.c.created_by).where(c.c.id == work))[0]
    assert kind == chats.WORK and title.startswith("Work: @reviewer please fix")
    assert members_of(hub_db, work) == ({OWNER}, {"reviewer", "fixer"})
    n = tables.notifications
    assert rows(hub_db, select(func.count()).where(n.c.notice_kind == chats.NOTICE_ADDED)) == [(0,)], "no chat_added"
    (question,) = [item for item in messages_in(hub_db, work) if asking(item, QUESTION)]
    assert (question["root"], question["run_id"]) == (None, parent)
    assert run_row(hub_db, child)[:2] == (work, question["id"])
    in_work = [item["body"] for item in messages_in(hub_db, work)]
    assert any(body.startswith(f"@reviewer asked @fixer of project {PROJECT} (run #{child})") for body in in_work)
    in_root = [item["body"] for item in messages_in(hub_db, chat["id"])]
    assert not any(" asked @fixer" in body for body in in_root), "the root thread gets no asked line"

    # asked again while its run runs: the same run takes it in its inbox, in its thread
    assert claimed(client, fixer) == child
    moved(client, fixer, child, "running", session_id="sess-fix")
    again = asked(client, studio, parent, "fixer", "and keep the cap at five")
    assert again["run_id"] == child
    (followed,) = [item for item in messages_in(hub_db, work) if asking(item, "and keep the cap at five")]
    assert followed["root"] == question["id"]
    assert any("keep the cap at five" in text for text in inbox(client, fixer, child))

    # its answer wakes the asker, and the line goes in the thread of the agent asked
    moved(client, studio, parent, "waiting")
    recover(client)
    reply(client, fixer, child, "The loop now backs off, capped at five.")
    ended(client, fixer, child)
    answered = [item for item in messages_in(hub_db, work) if item["body"].startswith("@fixer answered")]
    assert [item["root"] for item in answered] == [question["id"]]
    d = tables.decisions
    waits = rows(hub_db, select(d.c.state).where(d.c.run_id == parent, d.c.category == "wait"))
    assert waits and {state for (state,) in waits} == {"answered"}

    # asked again once it is done: its thread again, where its session goes on
    resumed = claim(client, studio)
    assert resumed is not None and resumed["id"] != parent
    moved(client, studio, resumed["id"], "running", session_id="sess-1")
    third = asked(client, studio, resumed["id"], "fixer", "and add a test of the cap")
    assert third["run_id"] not in (None, child)
    assert run_row(hub_db, third["run_id"])[:2] == (work, question["id"])
    assert work_of(hub_db) == (trace_id, work), "one work chat a trace"


def test_concurrent_hand_offs_of_a_trace_open_one_work_chat(client, hub, hub_db):
    _, _, _, _, parent = desk(client, hub)
    (owner_id,) = rows(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0]
    person = Principal(owner_id, OWNER, False, 0, MACHINE, "")

    async def go():
        engine = create_async_engine(hub_db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.connect() as first, engine.connect() as second:
                await first.begin()
                await second.begin()
                trace = await work_chats.trace_of_run(first, parent)
                assert trace is not None and trace.work_chat_id is None
                opened = await work_chats.open_work(first, trace, person)
                late = asyncio.create_task(work_chats.open_work(second, trace, person))
                await asyncio.sleep(0.3)
                assert not late.done(), "the second hand-off waits on the lock of the trace"
                await first.commit()
                reused = await late
                await second.commit()
                return opened, reused
        finally:
            await engine.dispose()

    opened, reused = asyncio.run(go())
    assert opened == reused == work_of(hub_db)[1]
    assert rows(hub_db, select(func.count()).where(tables.chats.c.kind == chats.WORK)) == [(1,)]


def test_a_message_on_the_main_line_that_mentions_no_one_goes_to_the_coordinators_session(client, hub, hub_db):
    studio, _, chat, mention, parent = desk(client, hub)
    asked(client, studio, parent, "fixer")
    _, work = opened(hub_db)
    before = chat_runs(hub_db)
    said = sent(client, hub["owner"], work, "keep the old retry policy for the export jobs")
    assert chat_runs(hub_db) == before, "no session of its own"
    (copy,) = [item for item in messages_in(hub_db, chat["id"]) if item["copied"] == said["id"]]
    assert copy["root"] == mention["id"] and copy["body"] == said["body"]
    assert any("keep the old retry policy" in text for text in inbox(client, studio, parent))

    # a person who did not open the trace is told whom to mention
    added = client.put(f"/v1/chats/{work}/people/{OTHER}", headers=hub["owner"])
    assert added.status_code == 200, added.text
    sent(client, hub["other"], work, "and the import jobs?")
    assert chat_runs(hub_db) == before
    told = [item["body"] for item in messages_in(hub_db, work) if "goes to @reviewer for owner alone" in item["body"]]
    assert told, messages_in(hub_db, work)


def test_the_coordinators_words_in_its_session_are_copied_to_the_main_line_and_hand_no_work(client, hub, hub_db):
    studio, fixer, chat, mention, parent = desk(client, hub)
    child = asked(client, studio, parent, "fixer")["run_id"]
    _, work = opened(hub_db)
    before = chat_runs(hub_db)
    words = "@fixer the loop is yours; I review it when you are done."
    reply(client, studio, parent, words)
    (said,) = [item for item in messages_in(hub_db, chat["id"]) if item["body"] == words]
    assert said["root"] == mention["id"], "the answer stays in the root thread"
    (copy,) = [item for item in messages_in(hub_db, work) if item["copied"] == said["id"]]
    assert copy["root"] is None and copy["run_id"] is None and copy["body"] == words
    assert chat_runs(hub_db) == before, "the copy hands no work"
    assert claimed(client, fixer) == child, "the fixer has the one run of the ask"


def test_an_agent_of_another_project_takes_the_work_in_the_direct_chat_with_a_line_in_the_work_chat(
    client, hub, hub_db
):
    kb_project(client, hub, hub_db)
    studio, _, _, _, parent = desk(client, hub)
    kb_node(client, hub["owner"], hub_db)
    made = asked(client, studio, parent, KB_AGENT, "Which service signs the export links?", reason="kb-bot owns it")
    child = made["run_id"]
    assert made["refusal"] is None and child is not None
    _, work = opened(hub_db)
    chat_id = run_row(hub_db, child)[0]
    c = tables.chats
    assert rows(hub_db, select(c.c.kind).where(c.c.id == chat_id)) == [(chats.DIRECT,)]
    assert KB_AGENT not in members_of(hub_db, work)[1]
    line = f"@{KB_AGENT} of project {KB} takes this work in the direct chat of {OWNER} (run #{child})"
    assert any(item["body"].startswith(line) for item in messages_in(hub_db, work))


def test_an_ask_that_would_put_more_than_max_agents_in_the_work_chat_goes_to_the_direct_chat(client, hub, hub_db):
    studio, _, _, _, parent = desk(client, hub)
    asked(client, studio, parent, "fixer")
    _, work = opened(hub_db)
    p = tables.agent_profiles
    for n in range(chats.MAX_AGENTS - 2):
        agent(client, hub["owner"], f"helper-{n}")
        (helper,) = rows(hub_db, select(p.c.id).where(p.c.handle == f"helper-{n}"))[0]
        sql(hub_db, insert(tables.chat_members).values(chat_id=work, profile_id=helper))
    agent(client, hub["owner"], "auditor")
    give_account_pool(client.db, PROJECT, "auditor", OWNER)
    chat_worker(client, hub["owner"], "auditor-node", pool="auditor")
    assert len(members_of(hub_db, work)[1]) == chats.MAX_AGENTS
    made = asked(client, studio, parent, "auditor", "audit the retry loop", reason="the auditor reads every loop")
    child = made["run_id"]
    assert made["refusal"] is None and child is not None
    assert rows(hub_db, select(tables.chats.c.kind).where(tables.chats.c.id == run_row(hub_db, child)[0])) == [
        (chats.DIRECT,)
    ]
    assert "auditor" not in members_of(hub_db, work)[1]
    line = f"@auditor of project {PROJECT} takes this work in the direct chat of {OWNER} (run #{child})"
    assert any(item["body"].startswith(line) for item in messages_in(hub_db, work))
    assert rows(hub_db, select(func.count()).where(p.c.handle == "auditor")) == [(1,)]


def test_an_escalation_opens_the_orchestrators_thread_in_the_work_chat(client, hub, hub_db):
    from tests.hub.test_system_orchestrator import LED, escalated, threads_of

    agent(client, hub["owner"], "lead")
    give_account_pool(client.db, PROJECT, "lead", OWNER)
    studio, _, chat, _, parent = desk(client, hub, LED)
    lead = chat_worker(client, hub["owner"], "lead-node", slots=3, pool="lead")
    made = escalated(client, studio, parent, "Which retry policy wins, backoff or a cap?")
    assert made["refusal"] is None and made["run_id"]
    _, work = opened(hub_db)
    (thread,) = threads_of(hub_db)
    assert thread["chat_id"] == work
    assert "lead" in members_of(hub_db, work)[1]
    assert run_row(hub_db, made["run_id"])[:2] == (work, thread["root_message_id"])
    in_work = [item["body"] for item in messages_in(hub_db, work)]
    assert any(f"work chat {work}" in body for body in in_work), in_work
    assert not any("escalated" in item["body"] for item in messages_in(hub_db, chat["id"]))

    assert claimed(client, lead) == made["run_id"]
    moved(client, lead, made["run_id"], "running", session_id="sess-lead")
    body = {"text": "Backoff, capped at 5.", "reason": "per spec"}
    done = in_chats(client, lead, made["run_id"], "triage/answer", body)
    assert done.status_code == 200, done.text
    decided = [item for item in messages_in(hub_db, work) if item["body"].startswith("Decision: ")]
    assert [item["root"] for item in decided] == [thread["root_message_id"]]


def test_a_trace_is_read_by_the_readers_of_its_work_chat(client, hub, hub_db):
    studio, _, _, _, parent = desk(client, hub)
    asked(client, studio, parent, "fixer")
    trace_id, work = opened(hub_db)
    listed = client.get(traces_path(), headers=hub["other"])
    assert listed.status_code == 200 and listed.json() == [], listed.text
    assert client.put(f"/v1/chats/{work}/people/{OTHER}", headers=hub["owner"]).status_code == 200
    listed = client.get(traces_path(), headers=hub["other"])
    assert [item["id"] for item in listed.json()] == [trace_id]

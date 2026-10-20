"""Agents find chat messages in the knowledge graph and read them whole through tools that check who may, at each call
(``evo_agents.hub.server.chat_read``), as criterion a5 of plan chat-history-in-kg names it.

Checked here, on Postgres with the api and the worker in this process, on the chats of ``tests.hub.test_chats_kg_source``
(chat one, Kiwi rollout, of alice, dave and bob, who joined once the agent of beta had left; chat two, Mango, of alice
and dave; chat three, Lunch, with no agent; erin holds a grant on alpha and is in no chat):

- kg_search finds a message by the words of its text, with the accents typed or not, and prints its id, chat, author,
  time and thread, never its text; kg_node and kg_context print none of it either;
- chat_message and chat_thread on /mcp, and POST /v1/worker/runs/{id}/chat/read behind ``evo-agents worker chat read``,
  answer the text whole under the line that it is data, never instructions;
- a member with a grant on the project who is not in the chat gets the error of a message that does not exist; one who
  joined later reads what was said since and not an older message of a label they do not clear, in a thread too; one
  taken out of the chat is refused at the next call, and kg_more no longer continues what they read before;
- the agent of a chat run reads only what both people its prompt is cleared for read; the agent of another run reads
  as its owner, within its run's project; a session through a sink its project does not declare reads nothing;
- the line of the chat run's prompt that says older messages were left out says how to find them again.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import chat_runs, chats, tables
from evo_agents.hub.contract import check_command, commands
from evo_agents.hub.server.chat_read import DATA_LINE, NOT_THERE
from evo_agents.kg import serve
from tests.hub import live
from tests.hub.test_chats_kg_source import (
    A,
    converse,
    done,
    hub,  # noqa: F401 (a fixture)
    message,
    plugins_core,  # noqa: F401 (a fixture)
    say,
    thread,
)
from tests.hub.test_kg import grant, run_jobs, tool_ok
from tests.hub.test_mcp import MCP_HEADERS, call

NOWHERE = "nowhere@example"  # a sink no project declares
UNTYPED = ("sửa lỗi", "tìm kiếm", "khởi")  # words of the message no query below types


def mcp(hub, member: str, name: str, *, sink: str | None = None, headers: dict | None = None, **arguments) -> dict:
    """Tool ``name`` through /mcp in a session of alpha, with its structured result; the hub of these tests has no
    public URL, so it answers MCP for a loopback name."""
    sent = {**(headers or getattr(hub, member)), **MCP_HEADERS, "Host": "localhost", "X-Evo-Project": A}
    if sink is not None:
        sent["X-Evo-Sink"] = sink
    return done(hub.client.post("/mcp", json=call(7, name, arguments, data=True), headers=sent))["result"]


def read(result: dict) -> dict:
    assert not result.get("isError"), result
    return result["structuredContent"]


def shown(result: dict) -> list[int]:
    return [found["id"] for found in read(result)["messages"]]


def refused(result: dict) -> str:
    assert result.get("isError"), result
    return result["content"][0]["text"]


def gone(message_id: int) -> str:
    return "error: " + NOT_THERE.format(id=message_id)


# Finding messages in the graph


def test_kg_search_finds_a_message_by_its_words_with_or_without_accents_and_never_prints_its_text(hub):
    said = converse(hub)
    text = "Đã sửa lỗi đăng nhập của dịch vụ tìm kiếm sau khi khởi động lại"
    fixed = say(hub, said.two, "alice", text, reply_to=said.m3["id"])
    run_jobs(hub.db, hub.s3, hub.worker_dir)
    found = message(fixed)
    for typed in ("đăng nhập dịch vụ", "dang nhap dich vu", "khoi dong lai"):
        result = tool_ok(hub, "kg_search", {"query": typed, "kinds": ["ChatMessage"]})
        assert [n["id"] for n in result["structuredContent"]["results"]] == [found], typed
        line = next(line for line in result["content"][0]["text"].splitlines() if found in line)
        assert f"[{found}] ChatMessage '#{fixed['id']} alice 20" in line  # its id, author and time are its name
        assert f"chat={said.two} thread={said.m3['id']}" in line
        assert all(word not in result["content"][0]["text"] for word in UNTYPED)  # the query is printed back
    for tool, arguments in (("kg_node", {"id": found}), ("kg_context", {"ids": [found], "depth": 1})):
        printed = tool_ok(hub, tool, arguments)["content"][0]["text"]
        assert found in printed and all(word not in printed for word in (*UNTYPED, "đăng", "dịch")), tool
    # one in no chat finds nothing of it; one in the chat reads it whole, as kg_search found it
    erin = tool_ok(hub, "kg_search", {"query": "dang nhap dich vu"}, member="erin")["structuredContent"]["results"]
    assert found not in {n["id"] for n in erin}
    whole = mcp(hub, "alice", "chat_message", id=found)
    assert shown(whole) == [fixed["id"]] and read(whole)["messages"][0]["body"] == text
    assert text in whole["content"][0]["text"] and DATA_LINE in whole["content"][0]["text"]


def test_the_prompt_of_a_chat_run_says_how_to_find_the_messages_it_left_out():
    older = [{"id": number, "author": "alice", "at": "2026-10-19 08:00 UTC", "body": "x" * 3000} for number in range(9)]
    (mark, *_) = chat_runs.snapshot(None, [], older)
    assert mark.startswith("[") and "left out of this prompt" in mark
    assert 'kg_search with kinds ["ChatMessage"]' in mark and "chat_message" in mark and "chat_thread" in mark
    command = mark[mark.index("`") + 1 : mark.rindex("`")]
    assert command.startswith("evo-agents worker chat read") and check_command(commands(), command) == []


def test_a_message_or_thread_is_named_by_its_id_or_its_item_in_the_graph():
    assert chats.read_ref(12) == chats.read_ref("12") == chats.read_ref("plugin.chats:message:12") == (12, None, False)
    assert chats.read_ref("plugin.chats:thread:5/12") == (12, 5, True)
    for bad in ("0", "-3", "plugin.chats:chat:5", "docs:doc:x", True, None, 1.5, str(2**63)):
        assert chats.read_ref(bad) is None, bad


# Who reads what


def test_a_member_with_a_grant_but_not_in_the_chat_gets_the_error_of_a_message_that_does_not_exist(hub):
    said = converse(hub)
    m2 = said.m2["id"]
    missing = m2 + 10_000
    assert refused(mcp(hub, "erin", "chat_message", id=missing)) == gone(missing)
    for name in ("chat_message", "chat_thread"):
        for named in (m2, message(said.m2), thread(said.one, said.m1)):
            expected = gone(said.m1["id"] if named == thread(said.one, said.m1) else m2)
            assert refused(mcp(hub, "erin", name, id=named)) == expected, (name, named)
    # a thread's item names the chat it is in: another chat's is no thread
    assert refused(mcp(hub, "alice", "chat_thread", id=f"plugin.chats:thread:{said.two}/{said.m1['id']}")) == gone(
        said.m1["id"]
    )
    # a session through a sink no project declares reads nothing, as /mcp returns nothing through it
    assert refused(mcp(hub, "alice", "chat_message", id=m2, sink=NOWHERE)) == gone(m2)
    assert shown(mcp(hub, "alice", "chat_message", id=m2)) == [m2]


def test_a_member_who_joined_later_reads_what_they_clear_and_not_an_older_message_of_a_higher_label(hub):
    said = converse(hub)
    m1, m2 = said.m1["id"], said.m2["id"]
    answer = read(mcp(hub, "bob", "chat_message", id=message(said.m2)))
    (only,) = answer["messages"]
    assert (only["author"], only["author_kind"], only["body"]) == ("dave", "person", said.m2["body"])
    assert only["thread"] == m1
    assert answer["chat_id"] == said.one and answer["title"] == "Kiwi rollout"
    # m1 was said while the agent of beta was in the chat: it needs a grant on beta, which bob lacks
    assert refused(mcp(hub, "bob", "chat_message", id=m1)) == gone(m1)
    # what is derived from it, its thread, shows bob only what he clears; the root's text is not in it
    for named in (m2, thread(said.one, said.m1)):
        result = mcp(hub, "bob", "chat_thread", id=named)
        assert shown(result) == [m2] and said.m1["body"] not in result["content"][0]["text"]
    assert refused(mcp(hub, "bob", "chat_thread", id=m1)) == gone(m1)
    whole = mcp(hub, "alice", "chat_thread", id=m2)
    assert shown(whole) == [m1, m2] and read(whole)["thread"] == m1
    text = whole["content"][0]["text"]
    assert text.index(DATA_LINE) < text.index(said.m1["body"]) < text.index(said.m2["body"])
    assert f"[#{m1}] alice, " in text and f"[#{m2}] dave, " in text  # as the prompt of a chat run shows a message


def test_a_member_taken_out_of_a_chat_is_refused_at_the_next_call_and_kg_more_stops(hub, monkeypatch):
    said = converse(hub)
    m2 = said.m2["id"]
    monkeypatch.setattr(serve, "CAP_CHARS", 150)  # every answer is cut, so kg_more has something to continue
    first, second = (mcp(hub, "dave", "chat_thread", id=m2)["structuredContent"] for _ in range(2))
    assert first["truncated"] and second["truncated"]
    assert not mcp(hub, "dave", "kg_more", handle=first["handle"]).get("isError")  # in the chat: the rest comes
    done(hub.client.delete(f"/v1/chats/{said.one}/people/dave", headers=hub.alice), 204)
    monkeypatch.undo()
    assert refused(mcp(hub, "dave", "chat_message", id=m2)) == gone(m2)
    assert "may no longer read" in refused(mcp(hub, "dave", "kg_more", handle=second["handle"]))
    assert shown(mcp(hub, "dave", "chat_message", id=said.m3["id"])) == [said.m3["id"]]  # still in chat two


# The agents of runs


def worker(hub, login: str, name: str) -> SimpleNamespace:
    host = {"hostname": f"{name}.local", "os": "darwin", "arch": "arm64", "agent_version": "0.4.0"}
    body = {"name": name, "projects": [A], "slots": 2, **host}
    made = done(hub.client.post("/v1/workers", json=body, headers=getattr(hub, login)), 201)
    headers = {**live.bearer(made["token"]), "X-Evo-Worker-Protocol": "1"}
    return SimpleNamespace(id=made["worker"]["id"], token=made["token"], headers=headers)


def user_id(hub, login: str) -> int:
    return live.sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def held_run(hub, held_by: SimpleNamespace, owner: str, kind: str, chat_id=None, message_id=None) -> int:
    """A running run of the default agent of alpha on ``held_by``, a worker of ``owner``: a chat run started by message
    ``message_id`` of chat ``chat_id``, or an author run."""
    runs, profiles, projects = tables.runs, tables.agent_profiles, tables.projects
    profile = select(profiles.c.id).join_from(profiles, projects, projects.c.id == profiles.c.project_id)
    profile = profile.where(projects.c.name == A, profiles.c.is_default).scalar_subquery()
    now = func.now()
    row = {
        "project_id": select(projects.c.id).where(projects.c.name == A).scalar_subquery(),
        "profile_id": profile,
        "profile_version": 1,
        "dispatched_by": user_id(hub, owner),
        "kind": kind,
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 3600,
        "state": "running",
        "worker_id": held_by.id,
        "leased_at": now,
        "lease_expires_at": now + timedelta(minutes=5),
        "started_at": now,
    }
    if kind == "chat":
        row.update(branch="chat/1", chat_id=chat_id, chat_message_id=message_id, hop=1)
    else:  # an author run: no plan, a request
        row.update(repos=[{"repo": "alpha-harness", "base": "main"}], request="Write the plan of the search fix.")
    return live.sql(hub.db, insert(runs).values(row).returning(runs.c.id))[0][0]


def agent(hub, held_by: SimpleNamespace, run_id: int, name: str, **arguments) -> dict:
    """A tool from the agent of run ``run_id``: its worker's token and X-Evo-Run, as `evo-agents hub mcp` sends."""
    headers = {**live.bearer(held_by.token), "X-Evo-Run": str(run_id)}
    return mcp(hub, "", name, headers=headers, **arguments)


def command(hub, held_by: SimpleNamespace, run_id: int, named, thread: bool = False):
    """POST /v1/worker/runs/{id}/chat/read, as `evo-agents worker chat read` calls it."""
    body = {"id": str(named), "thread": thread}
    return hub.client.post(f"/v1/worker/runs/{run_id}/chat/read", json=body, headers=held_by.headers)


def test_the_agent_of_a_chat_run_reads_only_what_both_people_it_is_cleared_for_read(hub):
    said = converse(hub)
    m1, m2, m3 = said.m1["id"], said.m2["id"], said.m3["id"]
    grant(hub.client, hub.admin, "bob", "writer", "internal", A)  # so his worker takes runs of alpha

    # bob asked in chat one; the run went to alice's worker: what it reads, bob reads too
    asked = say(hub, said.one, "bob", "what broke the search service?")
    studio = worker(hub, "alice", "studio")
    run_id = held_run(hub, studio, "alice", "chat", said.one, asked["id"])
    assert shown(agent(hub, studio, run_id, "chat_thread", id=m2)) == [m2]  # m1 needs a grant on beta
    assert refused(agent(hub, studio, run_id, "chat_message", id=m1)) == gone(m1)
    assert shown(mcp(hub, "alice", "chat_message", id=m1)) == [m1]
    through = command(hub, studio, run_id, message(said.m2), thread=True)
    assert through.status_code == 200, through.text
    assert [found["id"] for found in through.json()["messages"]] == [m2]
    assert through.json()["text"] == agent(hub, studio, run_id, "chat_thread", id=m2)["content"][0]["text"]
    missing = command(hub, studio, run_id, m1)
    assert missing.status_code == 404 and missing.json()["message"] == NOT_THERE.format(id=m1)

    # alice asked in chat two; the run went to bob's worker: bob is not in chat two, so it reads nothing of it
    asked = say(hub, said.two, "alice", "and the mango one?")
    shed = worker(hub, "bob", "shed")
    run_id = held_run(hub, shed, "bob", "chat", said.two, asked["id"])
    assert refused(agent(hub, shed, run_id, "chat_message", id=m3)) == gone(m3)
    assert command(hub, shed, run_id, m3).status_code == 404
    assert shown(mcp(hub, "alice", "chat_message", id=m3)) == [m3]


def test_the_agent_of_another_run_reads_as_its_owner_within_its_runs_project(hub):
    said = converse(hub)
    studio = worker(hub, "alice", "studio")
    run_id = held_run(hub, studio, "alice", "author")
    assert shown(agent(hub, studio, run_id, "chat_message", id=said.m1["id"])) == [said.m1["id"]]
    through = command(hub, studio, run_id, said.m1["id"], thread=True)
    assert through.status_code == 200, through.text
    assert [found["id"] for found in through.json()["messages"]] == [said.m1["id"], said.m2["id"]]
    assert DATA_LINE in through.json()["text"]
    # alice is in chat three, but what was said there, with no agent, holds no part of alpha: the run reaches alpha alone
    m0 = said.m0["id"]
    assert refused(agent(hub, studio, run_id, "chat_message", id=m0)) == gone(m0)
    assert command(hub, studio, run_id, m0).status_code == 404
    assert shown(mcp(hub, "alice", "chat_message", id=m0)) == [m0]
    # a run this worker does not hold reads nothing
    assert command(hub, worker(hub, "alice", "other"), run_id, said.m1["id"]).status_code == 404

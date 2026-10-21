"""Memories derived from chat messages carry the audience of those chats, and every read of memories holds the reader's
audience, computed from the database at that read (``evo_agents.hub.server.memory_audience``), as criterion a6 of plan
chat-history-in-kg names it.

Checked on the hub of ``tests.hub.test_kg_audience`` (project aud with a graph of chats one and two; alice and dave in
both, bob in chat one since the agent of aud-b left it, carol in neither, all with a grant on aud):

- a memory the agent of a run wrote after reading a message of chat one, through chat_message or kg_node, goes to who
  is in chat one alone: carol, with a grant on aud, finds it by no search, get, listing, pull, web page or /mcp tool;
  the agent of a chat run carries its chat without reading anything, and a run that goes on in the session of another
  carries what that one read;
- carol sees it once she joins chat one and clears its label, and dave loses it at the next read once he is taken out,
  with nothing written in between; a memory derived from both chats needs both;
- a writer, the run's owner included, keeps or raises the audience and never lowers it; an admin of aud who reads the
  memory lowers it by hand, with a row of audit;
- a Claude Code session: what chat_message read joins the session's label through `evo-agents hub mcp`, which then
  adds that audience to a memory_write, and the Stop hook pushes each file with it; a label that cannot be read stops
  both, and nothing leaves.
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select, update

from evo_agents.hub import hooks, tables
from evo_agents.hub.client import hub_dir
from evo_agents.hub.mcp_proxy import note_read, with_session_audience
from evo_agents.hub.server.run_audience import seen
from evo_agents.hub.server.security import SESSION_COOKIE, WEB
from evo_agents.kg import sessions
from evo_agents.kg.sessions import READ_META
from tests.hub import live
from tests.hub.test_kg_audience import (
    M2,
    A,
    chat_run,
    data,
    done,
    hub,  # noqa: F401 (a fixture)
    on_hub,
    user_id,
    worker,
)
from tests.hub.test_kg_audience import agent as run_tool
from tests.hub.test_mcp import MCP_HEADERS, call
from tests.hub.test_memory import InProcessHub, Machine

NAME = "kiwi-flag.md"
BODY = "---\nname: Kiwi flag\ndescription: the kiwi cache flag\nmetadata:\n  type: project\n---\nOff until Friday.\n"
WORD = "kiwi"  # in the memory's name and description


def say(hub, chat_id: int, member: str, text: str) -> dict:
    return done(
        hub.client.post(f"/v1/chats/{chat_id}/messages", json={"body": text}, headers=getattr(hub, member)), 201
    )


def author_run(hub, held_by: SimpleNamespace, owner: str) -> int:
    """A running author run of the default agent of aud on ``held_by``, a worker of ``owner``: a run of no chat."""
    runs, profiles, projects = tables.runs, tables.agent_profiles, tables.projects
    profile = select(profiles.c.id).join_from(profiles, projects, projects.c.id == profiles.c.project_id)
    now = func.now()
    row = {
        "project_id": select(projects.c.id).where(projects.c.name == A).scalar_subquery(),
        "profile_id": profile.where(projects.c.name == A, profiles.c.is_default).scalar_subquery(),
        "profile_version": 1,
        "dispatched_by": user_id(hub, owner),
        "kind": "author",
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
        "repos": [{"repo": f"{A}-harness", "base": "main"}],
        "request": "Note what the chat decided.",
    }
    return live.sql(hub.db, insert(runs).values(row).returning(runs.c.id))[0][0]


def written(result: dict) -> dict:
    return data(result)["memory"]


def web(hub, login: str) -> dict:
    return {"Cookie": f"{SESSION_COOKIE}={live.insert_token(hub.db, login, kind=WEB)}"}


def mcp(hub, login: str, tool: str, **arguments) -> dict:
    headers = {**getattr(hub, login), **MCP_HEADERS, "X-Evo-Project": A}
    return done(hub.client.post("/mcp", json=call(7, tool, arguments, data=True), headers=headers))["result"]


def reads(hub, login: str, memory_id: int) -> dict:
    """What each read of memories gives ``login`` of memory ``memory_id``: True where it is found."""
    found = {}
    for kind, headers in (("token", getattr(hub, login)), ("web", web(hub, login))):
        got = hub.client.get(f"/v1/memories/{memory_id}", headers=headers)
        assert got.status_code in (200, 404), got.text
        found[kind, "get"] = got.status_code == 200
        searched = done(hub.client.get("/v1/memories/search", params={"q": WORD, "project": A}, headers=headers))
        found[kind, "search"] = memory_id in [m["id"] for m in searched["items"]]
        params = {"project": A, "deleted": True, "limit": 500}
        listed = done(hub.client.get("/v1/memories", params=params, headers=headers))
        found[kind, "list"] = memory_id in [m["id"] for m in listed["items"]]
        history = hub.client.get(f"/v1/memories/{memory_id}/revisions", headers=headers)
        found[kind, "history"] = history.status_code == 200
    by_mcp = mcp(hub, login, "memory_get", id=memory_id)
    found["mcp", "get"] = not by_mcp.get("isError")
    searched = mcp(hub, login, "memory_search", query=WORD)
    found["mcp", "search"] = memory_id in [m["id"] for m in data(searched)["results"]]
    return found


def sees(hub, login: str, memory_id: int) -> bool:
    found = reads(hub, login, memory_id)
    assert len(set(found.values())) == 1, found  # every way of reading agrees
    return next(iter(found.values()))


def hidden_as_missing(hub, login: str, memory_id: int) -> None:
    """Memory ``memory_id`` answers ``login`` exactly as one that does not exist."""
    hidden = hub.client.get(f"/v1/memories/{memory_id}", headers=getattr(hub, login))
    missing = hub.client.get("/v1/memories/999999", headers=getattr(hub, login))
    assert hidden.status_code == missing.status_code == 404
    shown = re.sub(r"\b\d+\b", "N", hidden.json()["message"], count=1)
    assert shown == re.sub(r"\b\d+\b", "N", missing.json()["message"], count=1)


def pulled(hub, root: Path, login: str, monkeypatch) -> dict:
    machine = Machine(root / f"{login}-laptop", InProcessHub(hub.client, getattr(hub, login)), login, monkeypatch)
    report = machine.run("pull")
    assert not report.errors, report.errors
    return machine.files()


def from_chat_one(hub) -> SimpleNamespace:
    """The memory alice's author run writes after reading a message of chat one, and what that reading revealed."""
    said = say(hub, hub.one, "alice", "The kiwi cache flag stays off until Friday")
    studio = worker(hub, "alice", "studio")
    run_id = author_run(hub, studio, "alice")
    shown = data(run_tool(hub, studio, run_id, "chat_message", id=said["id"]))["label"]["audience"]
    memory = written(run_tool(hub, studio, run_id, "memory_write", name=NAME, body=BODY))
    return SimpleNamespace(memory=memory, shown=shown, run_id=run_id, studio=studio)


# What a run's agent read


def test_a_memory_a_run_wrote_after_reading_chat_one_goes_to_who_is_in_chat_one_alone(hub, tmp_path, monkeypatch):
    made = from_chat_one(hub)
    assert f"chat:{hub.one}" in made.shown and all(p.startswith(("chat:", f"grant:{A}:")) for p in made.shown)
    assert made.memory["label"]["audience"] == made.shown  # sorted, as a kg/1 label holds it
    assert set(made.shown) <= on_hub(hub, lambda conn: seen(conn, made.run_id))

    memory_id = made.memory["id"]
    assert not sees(hub, "carol", memory_id)  # a grant on aud, in no chat: by no search, get, list, web or tool
    hidden_as_missing(hub, "carol", memory_id)
    assert all(sees(hub, login, memory_id) for login in ("alice", "bob", "dave"))
    assert NAME not in {name for _, name in pulled(hub, tmp_path, "carol", monkeypatch)}
    assert NAME in {name for _, name in pulled(hub, tmp_path, "bob", monkeypatch)}


def test_what_kg_node_shows_a_run_goes_into_its_memory_and_a_chat_run_carries_its_chat(hub):
    done(
        hub.client.put(
            f"/v1/admin/projects/{A}/grants/bob", json={"role": "writer", "max_level": "internal"}, headers=hub.admin
        )
    )
    shed = worker(hub, "bob", "shed")
    run_id = author_run(hub, shed, "bob")
    shown = data(run_tool(hub, shed, run_id, "kg_node", id=M2))["label"]["audience"]
    assert f"chat:{hub.one}" in shown and f"chat:{hub.two}" not in shown  # m3 and K-7 show bob nothing
    memory = written(run_tool(hub, shed, run_id, "memory_write", name="kiwi-restart.md", body=BODY))
    assert memory["label"]["audience"] == shown
    assert not sees(hub, "carol", memory["id"]) and sees(hub, "alice", memory["id"])
    # A memory a run's agent is given counts as what it was shown, as a message does.
    fresh = author_run(hub, shed, "bob")
    assert data(run_tool(hub, shed, fresh, "memory_get", id=memory["id"]))["memory"]["id"] == memory["id"]
    assert set(shown) <= on_hub(hub, lambda conn: seen(conn, fresh))

    # The agent of a chat run was shown its chat in its prompt: its memory carries it without a read.
    asked = say(hub, hub.one, "alice", "write down the kiwi flag?")
    studio = worker(hub, "alice", "studio")
    chat = chat_run(hub, studio, "alice", asked["id"])
    noted = written(run_tool(hub, studio, chat, "memory_write", name="kiwi-asked.md", body=BODY))
    assert noted["label"]["audience"] == [f"chat:{hub.one}"]

    # A run that goes on in the session of another holds what that one read.
    mango = say(hub, hub.two, "alice", "the mango flag too")
    read = data(run_tool(hub, studio, chat, "chat_message", id=mango["id"]))["label"]["audience"]
    later = chat_run(hub, studio, "alice", asked["id"])
    live.sql(hub.db, update(tables.runs).where(tables.runs.c.id == later).values(resume_of_run_id=chat))
    assert set(read) <= on_hub(hub, lambda conn: seen(conn, later))
    again = written(run_tool(hub, studio, later, "memory_write", name="kiwi-later.md", body=BODY))
    assert set(again["label"]["audience"]) == {f"chat:{hub.one}", *read}


# Changes of members count at the next read


def test_who_joins_chat_one_sees_the_memory_and_who_leaves_it_loses_it_at_the_next_read(hub):
    made = from_chat_one(hub)
    memory_id = made.memory["id"]
    assert not sees(hub, "carol", memory_id)
    done(hub.client.put(f"/v1/chats/{hub.one}/people/carol", headers=hub.alice))
    assert sees(hub, "carol", memory_id)  # she clears the chat's label: a grant on aud
    assert sees(hub, "dave", memory_id)
    done(hub.client.delete(f"/v1/chats/{hub.one}/people/dave", headers=hub.alice), 204)
    assert not sees(hub, "dave", memory_id)
    hidden_as_missing(hub, "dave", memory_id)


def test_a_memory_derived_from_two_chats_needs_both(hub):
    one = say(hub, hub.one, "alice", "The kiwi flag stays off")
    two = say(hub, hub.two, "alice", "Mango waits for the kiwi flag")
    studio = worker(hub, "alice", "studio")
    run_id = author_run(hub, studio, "alice")
    shown = set()
    for said in (one, two):
        shown |= set(data(run_tool(hub, studio, run_id, "chat_message", id=said["id"]))["label"]["audience"])
    memory = written(run_tool(hub, studio, run_id, "memory_write", name=NAME, body=BODY))
    assert {f"chat:{hub.one}", f"chat:{hub.two}"} <= set(memory["label"]["audience"]) == shown
    assert not sees(hub, "bob", memory["id"])  # in chat one alone
    assert sees(hub, "dave", memory["id"]) and sees(hub, "alice", memory["id"])
    done(hub.client.put(f"/v1/chats/{hub.one}/people/carol", headers=hub.alice))
    assert not sees(hub, "carol", memory["id"])  # chat one is not enough


# Who may lower an audience


def test_a_writer_never_lowers_the_audience_and_an_admin_lowers_it_with_a_row_of_audit(hub):
    made = from_chat_one(hub)
    memory_id, revision = made.memory["id"], made.memory["revision"]

    # alice, a writer and the run's owner, writes it again with a label of her own and no audience: it stays
    body = {"scope": "project", "project": A, "location": "harness", "name": NAME, "type": "project"}
    again = {**body, "body": BODY + "And the mango one.\n", "label": {"level": "internal"}, "if_revision": revision}
    rewritten = done(hub.client.put("/v1/memories", params={"sink": "hub"}, json=again, headers=hub.alice))
    assert rewritten["changed"] and rewritten["label"]["audience"] == made.shown
    revision = rewritten["revision"]
    lower = {"audience": [], "if_revision": revision}
    for login in ("alice", "bob"):  # a writer; a reader in the chat
        refused = hub.client.put(f"/v1/memories/{memory_id}/audience", json=lower, headers=getattr(hub, login))
        assert refused.status_code == 403, refused.text
    assert hub.client.put(f"/v1/memories/{memory_id}/audience", json=lower, headers=hub.carol).status_code == 404

    # erin, an admin of aud, lowers it once she reads it: in chat one
    done(
        hub.client.put(
            f"/v1/admin/projects/{A}/grants/erin", json={"role": "admin", "max_level": "internal"}, headers=hub.admin
        )
    )
    erin = live.bearer(live.insert_token(hub.db, "erin"))
    assert hub.client.put(f"/v1/memories/{memory_id}/audience", json=lower, headers=erin).status_code == 404
    done(hub.client.put(f"/v1/chats/{hub.one}/people/erin", headers=hub.alice))
    raised = {"audience": [*made.shown, "chat:999"], "if_revision": revision}
    assert hub.client.put(f"/v1/memories/{memory_id}/audience", json=raised, headers=erin).status_code == 422
    stale = {"audience": [], "if_revision": revision - 1}
    assert hub.client.put(f"/v1/memories/{memory_id}/audience", json=stale, headers=erin).status_code == 409
    lowered = done(hub.client.put(f"/v1/memories/{memory_id}/audience", json=lower, headers=erin))
    assert lowered["changed"] and "audience" not in lowered["label"] and lowered["revision"] == revision + 1
    assert lowered["body"] == rewritten["body"] and lowered["updated_by"] == "erin"
    audit = tables.audit
    rows = live.sql(
        hub.db,
        select(audit.c.action, audit.c.project_id).where(audit.c.target == f"memory:{memory_id}").order_by(audit.c.id),
    )
    project_id = live.sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == A))[0][0]
    assert rows[-1] == ("memory.audience", project_id) and [r[0] for r in rows].count("memory.audience") == 1
    assert sees(hub, "carol", memory_id)
    # the history keeps what it was: a revision with the audience still goes to who holds it alone
    assert hub.client.get(f"/v1/memories/{memory_id}/revisions/1", headers=hub.carol).status_code == 404
    assert (
        done(
            hub.client.put(
                f"/v1/memories/{memory_id}/audience", json={**lower, "if_revision": revision + 1}, headers=erin
            )
        )["changed"]
        is False
    )


# A Claude Code session


def session_record(tmp_path: Path, monkeypatch) -> str:
    session_id = "71111111-2222-4333-8444-555555555555"
    monkeypatch.setenv("EVO_KG_HOME", str(tmp_path / "kg-home-session"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session_id)
    return session_id


def test_a_sessions_chat_reads_go_with_its_memory_write_and_its_stop_push(hub, tmp_path, monkeypatch):
    session_id = session_record(tmp_path, monkeypatch)
    said = say(hub, hub.one, "alice", "The kiwi cache flag stays off until Friday")
    request = call(3, "chat_message", {"id": said["id"]})
    result = mcp(hub, "alice", "chat_message", id=said["id"])
    meta = result["content"][0]["_meta"][READ_META]
    assert f"chat:{hub.one}" in meta["label"]["audience"] and meta["results_read"] == 1
    note_read(request, {k: v for k, v in result.items() if k != "structuredContent"})  # as the proxy gets it
    audience = sessions.session_audience(session_id)
    assert audience == meta["label"]["audience"]

    # memory_write through `evo-agents hub mcp`: the session's audience goes with it
    write = call(4, "memory_write", {"name": NAME, "body": BODY, "audience": [f"chat:{hub.two}"]})
    sent = with_session_audience(write)
    assert sent["params"]["arguments"]["audience"] == sorted({*audience, f"chat:{hub.two}"})
    assert with_session_audience(call(5, "memory_search", {"query": "x"})) == call(5, "memory_search", {"query": "x"})
    stored = written(mcp(hub, "alice", "memory_write", **sent["params"]["arguments"]))
    assert stored["label"]["audience"] == sorted({*audience, f"chat:{hub.two}"})
    assert not sees(hub, "bob", stored["id"]) and not sees(hub, "carol", stored["id"])

    # the Stop hook: every file the push sends carries the session's audience
    laptop = Machine(tmp_path / "laptop", InProcessHub(hub.client, hub.alice), "alice", monkeypatch)
    laptop.write(f"ws/{A}-harness", "mango-plan.md", BODY.replace("Kiwi flag", "Mango plan"))
    monkeypatch.setenv("HOME", str(laptop.home))
    outcome = hooks.Outcome()
    where = laptop.home / "ws" / f"{A}-harness"
    hooks._push_memories(laptop.hub, "alice", where, hooks.HookState(hub_dir()), outcome, {"session_id": session_id})
    assert outcome.quiet is None and outcome.loud is None, outcome
    (pushed,) = [
        m
        for m in done(hub.client.get("/v1/memories", params={"project": A}, headers=hub.alice))["items"]
        if m["name"] == "mango-plan.md"
    ]
    assert pushed["label"]["audience"] == audience
    assert not sees_name(hub, "carol", "mango-plan.md")

    # a label that cannot be read stops both: nothing leaves this machine without the audience it read
    sessions.session_path(session_id).write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be read"):
        with_session_audience(write)
    laptop.write(f"ws/{A}-harness", "lime-plan.md", BODY.replace("Kiwi flag", "Lime plan"))
    outcome = hooks.Outcome()
    hooks._push_memories(laptop.hub, "alice", where, hooks.HookState(hub_dir()), outcome, {"session_id": session_id})
    assert "cannot be read" in (outcome.quiet or "")
    names = [m["name"] for m in done(hub.client.get("/v1/memories", params={"project": A}, headers=hub.alice))["items"]]
    assert "lime-plan.md" not in names


def sees_name(hub, login: str, name: str) -> bool:
    listed = done(hub.client.get("/v1/memories", params={"project": A, "limit": 500}, headers=getattr(hub, login)))
    return name in [m["name"] for m in listed["items"]]


def test_the_proxy_answers_a_memory_write_it_cannot_label_without_reaching_the_hub(tmp_path, monkeypatch):
    from evo_agents.hub.mcp_proxy import Proxy
    from tests.hub.test_mcp import FakeHub, mcp_reply, run_proxy, signed_in

    session_id = session_record(tmp_path, monkeypatch)
    record = sessions.session_path(session_id)
    label = {"level": "internal", "integrity": "U", "projects": [A], "audience": ["chat:5"]}
    sessions.record_read(session_id, {"project": A, "label": label, "results_read": 1})
    fake = FakeHub(mcp_reply)
    try:
        replies = run_proxy(
            Proxy(A, credentials=signed_in(fake.url)), call(1, "memory_write", {"name": NAME, "body": BODY})
        )
        record.write_text("[]", encoding="utf-8")  # a record that holds no object
        refused = run_proxy(
            Proxy(A, credentials=signed_in(fake.url)), call(2, "memory_write", {"name": NAME, "body": BODY})
        )
    finally:
        fake.stop()
    assert not replies[0]["result"]["isError"]
    (sent,) = [message for _, message in fake.requests]  # the refused write never reached the hub
    assert sent["params"]["arguments"]["audience"] == ["chat:5"]
    assert refused[0]["result"]["isError"] and "nothing was written" in refused[0]["result"]["content"][0]["text"]

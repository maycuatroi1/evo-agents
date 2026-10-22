"""The index of an agent's memories in the prompt of its runs, the reads the hub records, and the daily upkeep that
archives and marks stale (plan agent-profile-workspace, criterion a3; ``evo_agents.hub.server.memory_upkeep``,
``evo_agents.hub.server.memory_state``).

Checked on the hub and the profiles of ``tests.hub.test_memory_agents`` (project aud, @scout and @critic besides its
default profile, alice a writer and carol a reader):

- the prompt of a run under @scout ends with the index of @scout's memories (name, id, type, description, stale mark),
  at most 4 KiB and within the prompt's limit; a run under the default profile, or under a profile with no memory, gets
  the prompt it had;
- memory_get by the agent of a run records a read; the index, a search and a member's read do not;
- the daily job archives an agent's memory nobody opened for 30 days (out of the index and the searches of runs, still
  listed for members) and a writer restores it, through the API and `evo-agents hub memory restore`;
- the job marks stale an agent's memory that cites code:REPO:PATH the project's graph has no file for, clears the mark
  once the graph has it again, and marks nothing for a project whose graph it cannot read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import cli_memory, tables
from evo_agents.hub.runs import MAX_PROMPT_BYTES
from evo_agents.hub.server import memory_upkeep
from evo_agents.hub.server.change_tiers import GraphView
from evo_agents.hub.server.runs.service import claims
from tests.hub import live
from tests.hub.test_kg_audience import A, data, done, on_hub
from tests.hub.test_kg_audience import agent as run_tool
from tests.hub.test_memory import InProcessHub
from tests.hub.test_memory_agents import (  # noqa: F401 (hub and agents are fixtures)
    a_run,
    agents,
    body,
    found,
    hub,
    write,
)

NOW = datetime.now(UTC)


def prompt(hub, run_id: int) -> str:
    return on_hub(hub, lambda conn: claims._run_spec(conn, run_id)).prompt


def index(hub, run_id: int, room: int = memory_upkeep.INDEX_BYTES) -> str:
    return on_hub(hub, lambda conn: memory_upkeep.memory_index(conn, run_id, room))


def state(hub, memory_id: int):
    m = tables.memories
    query = select(m.c.read_at, m.c.archived_at, m.c.stale_at).where(m.c.id == memory_id)
    return SimpleNamespace(**dict(zip(("read_at", "archived_at", "stale_at"), live.sql(hub.db, query)[0], strict=True)))


def set_memory(hub, memory_id: int, **values) -> None:
    live.sql(hub.db, update(tables.memories).where(tables.memories.c.id == memory_id).values(**values))


def by_member(hub, name: str, agent: str = "scout", text: str | None = None) -> dict:
    """A memory of @``agent`` alice writes through the API."""
    memory = {"scope": "project", "project": A, "location": "harness", "name": name, "type": "project"}
    return done(
        hub.client.put("/v1/memories", json={**memory, "agent": agent, "body": text or body(name[:-3])}, headers=hub.alice)
    )


def kept(hub, data_dir=None) -> dict:
    return hub.client.portal.call(partial(memory_upkeep.upkeep, hub.client.app.state.engine, None, data_dir, now=NOW))


# The index in the prompt


def test_the_prompt_of_an_agent_profile_run_ends_with_the_index_of_its_memories(hub, agents):
    studio = agents.studio
    critic = a_run(hub, studio, "alice", "critic")
    plain = prompt(hub, critic)
    assert "# Memories of @critic" not in plain and index(hub, critic) == ""  # no memory: the prompt as it was
    scout = a_run(hub, studio, "alice", "scout")
    first = data(write(hub, studio, scout, "orbit-scout.md"))["memory"]
    second = data(write(hub, studio, scout, "launch-window.md", kind="reference"))["memory"]
    write(hub, studio, critic, "orbit-critic.md")

    later = a_run(hub, studio, "alice", "scout")
    text = prompt(hub, later)
    head, _, listed = text.partition("# Memories of @scout\n")
    assert listed and head.strip() and "memory_get" in listed
    assert f"- launch-window.md (id {second['id']}, reference): the launch-window note" in listed
    assert f"- orbit-scout.md (id {first['id']}, project): the orbit-scout note" in listed
    assert listed.index("launch-window.md") < listed.index("orbit-scout.md")  # newest first
    assert "orbit-critic" not in text and "orbit-plan" not in listed  # never another agent's, nor a shared one
    assert "# Memories of" not in prompt(hub, a_run(hub, studio, "alice"))  # the default profile: as before

    set_memory(hub, first["id"], stale_at=NOW)
    assert f"- orbit-scout.md (id {first['id']}, project, stale): " in prompt(hub, later)


def test_the_index_of_memories_in_a_prompt_is_at_most_4_kib_and_within_the_prompt_limit(hub, agents):
    long = "a note of the orbit with many words " * 8
    for number in range(60):
        by_member(hub, f"orbit-{number:02}.md", text=f"---\nname: n{number}\ndescription: {long}\n---\nx\n")
    scout = a_run(hub, agents.studio, "alice", "scout")
    whole = index(hub, scout)
    assert whole.startswith("# Memories of @scout") and len(whole.encode()) <= memory_upkeep.INDEX_BYTES
    assert "orbit-59.md" in whole and "orbit-00.md" not in whole  # the newest, as many as fit
    text = prompt(hub, scout)
    assert text.endswith(whole) and len(text.encode()) <= MAX_PROMPT_BYTES
    small = index(hub, scout, room=700)
    assert 0 < len(small.encode()) <= 700 and small.count("\n- ") < whole.count("\n- ")
    assert index(hub, scout, room=100) == ""  # no room for one memory: no index


def test_the_index_of_memories_lists_none_that_names_an_audience(hub, agents):
    shown = by_member(hub, "orbit-open.md")
    hidden = {"scope": "project", "project": A, "location": "harness", "name": "orbit-few.md", "type": "project"}
    hidden |= {"agent": "scout", "body": body("orbit-few"), "audience": [f"chat:{hub.one}"]}
    done(hub.client.put("/v1/memories", json=hidden, headers=hub.alice))
    listed = index(hub, a_run(hub, agents.studio, "alice", "scout"))
    assert f"id {shown['id']}" in listed and "orbit-few.md" not in listed


# Reads


def test_memory_get_in_a_run_records_a_read_and_the_index_or_a_search_does_not(hub, agents):
    studio = agents.studio
    scout = a_run(hub, studio, "alice", "scout")
    memory = data(write(hub, studio, scout, "orbit-scout.md"))["memory"]
    later = a_run(hub, studio, "alice", "scout")
    assert f"id {memory['id']}" in prompt(hub, later)
    assert memory["id"] in found(hub, studio, later, "orbit")
    done(hub.client.get(f"/v1/memories/{memory['id']}", headers=hub.carol))  # a member reads it
    assert state(hub, memory["id"]).read_at is None
    opened = data(run_tool(hub, studio, later, "memory_get", id=memory["id"]))["memory"]
    assert opened["id"] == memory["id"]
    read_at = state(hub, memory["id"]).read_at
    assert read_at is not None
    shown = done(hub.client.get(f"/v1/memories/{memory['id']}", headers=hub.carol))
    assert shown["read_at"] is not None and shown["archived_at"] is None and shown["stale_at"] is None
    data(run_tool(hub, studio, later, "memory_get", id=agents.shared["id"]))  # a shared memory: no read recorded
    assert state(hub, agents.shared["id"]).read_at is None


# Archive


def test_the_daily_upkeep_archives_an_agent_memory_nobody_opened_for_30_days_and_a_writer_restores_it(
    hub, agents, capsys, monkeypatch
):
    studio = agents.studio
    old, opened, fresh = (by_member(hub, name) for name in ("orbit-old.md", "orbit-opened.md", "orbit-fresh.md"))
    month = NOW - timedelta(days=31)
    for memory in (old, opened):
        set_memory(hub, memory["id"], created_at=month, updated_at=month)
    set_memory(hub, opened["id"], read_at=NOW - timedelta(days=2))
    shared = agents.shared["id"]
    set_memory(hub, shared, created_at=month, updated_at=month)

    assert kept(hub)["archived"] == 1
    assert state(hub, old["id"]).archived_at is not None
    for other in (opened["id"], fresh["id"], shared):
        assert state(hub, other).archived_at is None
    scout = a_run(hub, studio, "alice", "scout")
    assert "orbit-old.md" not in prompt(hub, scout)
    assert found(hub, studio, scout, "orbit") == {opened["id"], fresh["id"], shared}
    listed = data(run_tool(hub, studio, scout, "memory_search", query="orbit", project=A))["results"]
    assert old["id"] not in {m["id"] for m in listed}
    members = done(hub.client.get("/v1/memories", params={"agent": "scout"}, headers=hub.carol))["items"]
    assert {m["id"]: m["archived_at"] is not None for m in members}[old["id"]] is True  # members still see it
    assert kept(hub)["archived"] == 0  # archived once

    refused = hub.client.post(f"/v1/memories/{old['id']}/restore", headers=hub.carol)
    assert refused.status_code == 403, refused.text  # a reader
    not_agent = hub.client.post(f"/v1/memories/{shared}/restore", headers=hub.alice)
    assert not_agent.status_code == 422, not_agent.text
    member = InProcessHub(hub.client, hub.alice)
    monkeypatch.setattr(cli_memory, "_signed_in", lambda: (member, None))
    assert cli_memory.cmd_memory_restore(SimpleNamespace(id=old["id"], json=False)) == 0
    assert "is active" in capsys.readouterr().out
    restored = state(hub, old["id"])
    assert restored.archived_at is None and restored.read_at is not None
    assert "orbit-old.md" in prompt(hub, scout) and old["id"] in found(hub, studio, scout, "orbit")
    again = done(hub.client.post(f"/v1/memories/{old['id']}/restore", headers=hub.alice))
    assert again["archived_at"] is None  # nothing to restore: as it is
    audit = tables.audit
    rows = live.sql(hub.db, select(audit.c.action).where(audit.c.target == f"memory:{old['id']}"))
    assert [r[0] for r in rows].count("memory.restore") == 1


def test_a_new_revision_of_an_archived_agent_memory_brings_it_back(hub, agents):
    memory = by_member(hub, "orbit-old.md")
    set_memory(hub, memory["id"], archived_at=NOW, stale_at=NOW)
    changed = {"scope": "project", "project": A, "location": "harness", "name": "orbit-old.md", "type": "project"}
    changed |= {"agent": "scout", "body": body("orbit-old, again"), "if_revision": memory["revision"]}
    done(hub.client.put("/v1/memories", json=changed, headers=hub.alice))
    assert state(hub, memory["id"]).archived_at is None and state(hub, memory["id"]).stale_at is None


# Stale


def test_citations_of_code_read_as_the_reviewers_evidence_for_the_stale_mark():
    text = "See code:aud-harness:docs/a.md:12, and code:aud-harness:src/b.py. Not code:../x nor code:bad repo:x."
    assert memory_upkeep.citations(text) == {"aud-harness:docs/a.md", "aud-harness:src/b.py"}
    assert memory_upkeep.citations("no citation here") == set()


def test_the_daily_upkeep_marks_stale_an_agent_memory_citing_a_path_the_graph_lost(hub, agents, monkeypatch):
    gone = by_member(hub, "orbit-gone.md", text=body("orbit-gone") + "From code:aud-harness:docs/gone.md:3.\n")
    held = by_member(hub, "orbit-held.md", text=body("orbit-held") + "From code:aud-harness:docs/held.md.\n")
    plain = by_member(hub, "orbit-plain.md")
    graphs, asked = [GraphView(frozenset({"aud-harness:docs/gone.md"}), frozenset())], []

    async def impact(engine, project_id, targets, reader, *, owner_id=None):
        asked.append((targets, owner_id))
        return graphs[0]

    monkeypatch.setattr(memory_upkeep, "impact_as_owner", impact)
    counts = kept(hub)
    assert (counts["stale"], counts["fresh"]) == (1, 0)
    assert asked == [(["aud-harness:docs/gone.md", "aud-harness:docs/held.md"], gone_owner(hub, gone["id"]))]
    assert state(hub, gone["id"]).stale_at is not None
    assert state(hub, held["id"]).stale_at is None and state(hub, plain["id"]).stale_at is None
    assert "orbit-gone.md (id" in index(hub, a_run(hub, agents.studio, "alice", "scout"))
    assert ", stale): the orbit-gone note" in index(hub, a_run(hub, agents.studio, "alice", "scout"))

    graphs[0] = None  # the graph cannot be read now: the marks stay
    assert kept(hub)["stale"] == 0 and state(hub, gone["id"]).stale_at is not None
    graphs[0] = GraphView(frozenset(), frozenset())  # the graph has the file again
    assert kept(hub)["fresh"] == 1 and state(hub, gone["id"]).stale_at is None


def test_the_daily_upkeep_marks_nothing_stale_when_no_graph_of_the_project_can_be_read(hub, agents):
    gone = by_member(hub, "orbit-gone.md", text=body("orbit-gone") + "From code:aud-harness:docs/gone.md:3.\n")
    counts = kept(hub)  # no data directory, so no cache of graphs: impact_as_owner reads no graph, as for none built
    assert counts["stale"] == 0 and counts["unread"] == 1
    assert state(hub, gone["id"]).stale_at is None


def gone_owner(hub, memory_id: int) -> int:
    return live.sql(hub.db, select(tables.memories.c.owner_id).where(tables.memories.c.id == memory_id))[0][0]

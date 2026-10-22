"""The memories of agent profiles and the fences of memories in a run (plan agent-profile-workspace, criteria a1 and
a2; ``evo_agents.hub.server.memory_agents``).

Checked on the hub of ``tests.hub.test_kg_audience`` (project aud; alice a writer, bob, carol and dave readers), with
two profiles of aud besides its default, @scout and @critic, and runs written straight into the runs table as the
dispatcher would hold them:

- a1: the agent of a run under @scout writes into the memories of @scout and reads the shared memories with those,
  never @critic's, and a run under the default profile reads none of them; members read them by id or by naming the
  agent, a writer changes and deletes them, a pull never brings them, and every revision a run wrote says where it came
  from (run, profile version, the person at its origin, hop, chat and message); `evo-agents hub memory list --agent`
  lists them;
- a2: that agent writes no shared memory, and the refusal says where its memories go and that the Curator considers
  promoting them; a chat run neither reads nor writes the person's memories of type user, feedback stays readable; a run
  at hop 2 writes nothing; one run writes at most five memories. Every refusal is an MCP error the agent reads.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import cli_memory, tables
from tests.hub import live
from tests.hub.test_kg_audience import (
    A,
    data,
    done,
    hub,  # noqa: F401 (a fixture)
    user_id,
    worker,
)
from tests.hub.test_kg_audience import agent as run_tool
from tests.hub.test_memory import InProcessHub
from tests.hub.test_memory_audience import pulled, say


def body(title: str, kind: str = "project") -> str:
    return f"---\nname: {title}\ndescription: the {title} note\nmetadata:\n  type: {kind}\n---\nThe {title} text.\n"


def make_profile(hub, handle: str) -> int:
    made = hub.client.post(f"/v1/projects/{A}/agents", json={"handle": handle, "name": handle}, headers=hub.admin)
    assert made.status_code == 201, made.text
    profiles = tables.agent_profiles
    return live.sql(hub.db, select(profiles.c.id).where(profiles.c.handle == handle))[0][0]


def a_run(hub, held_by: SimpleNamespace, owner: str, handle: str | None = None, **chat) -> int:
    """A running run of profile ``handle`` of aud (its default when None) on ``held_by``, a worker of ``owner``; a
    chat run when ``chat`` gives chat_id, chat_message_id and hop."""
    runs, profiles, versions = tables.runs, tables.agent_profiles, tables.agent_profile_versions
    projects = tables.projects
    chosen = profiles.c.handle == handle if handle else profiles.c.is_default
    profile = select(profiles.c.id).join_from(profiles, projects, projects.c.id == profiles.c.project_id)
    profile_id = live.sql(hub.db, profile.where(projects.c.name == A, chosen))[0][0]
    version = live.sql(hub.db, select(func.max(versions.c.version)).where(versions.c.profile_id == profile_id))[0][0]
    now = func.now()
    row = {
        "project_id": select(projects.c.id).where(projects.c.name == A).scalar_subquery(),
        "profile_id": profile_id,
        "profile_version": version,
        "dispatched_by": user_id(hub, owner),
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
    if chat:
        row.update(kind="chat", branch="chat/1", **chat)
    else:
        row.update(kind="author", repos=[{"repo": f"{A}-harness", "base": "main"}], request="Note what matters.")
    return live.sql(hub.db, insert(runs).values(row).returning(runs.c.id))[0][0]


def write(hub, held_by, run_id: int, name: str, kind: str = "project", **more) -> dict:
    return run_tool(hub, held_by, run_id, "memory_write", name=name, body=body(name[:-3], kind), type=kind, **more)


def refusal(result: dict) -> str:
    assert result.get("isError"), result
    return result["content"][0]["text"].removeprefix("error: ")


def shared(hub, name: str, kind: str = "project") -> dict:
    """A memory of aud alice writes through the API: shared for project and reference, else her own."""
    memory = {"scope": "project", "project": A, "location": "harness", "name": name, "type": kind}
    return done(hub.client.put("/v1/memories", json={**memory, "body": body(name[:-3], kind)}, headers=hub.alice))


def ids(hub, login: str, **params) -> set[int]:
    listed = done(hub.client.get("/v1/memories", params={"project": A, **params}, headers=getattr(hub, login)))
    return {m["id"] for m in listed["items"]}


def found(hub, held_by, run_id: int, query: str) -> set[int]:
    return {m["id"] for m in data(run_tool(hub, held_by, run_id, "memory_search", query=query))["results"]}


@pytest.fixture
def agents(hub):  # noqa: F811 (the fixture above)
    """Profiles @scout and @critic of aud, a worker of alice, and a shared memory of aud."""
    grant = {"role": "admin", "max_level": "internal"}
    done(hub.client.put(f"/v1/admin/projects/{A}/grants/{live.ADMIN}", json=grant, headers=hub.admin))
    return SimpleNamespace(
        scout=make_profile(hub, "scout"),
        critic=make_profile(hub, "critic"),
        studio=worker(hub, "alice", "studio"),
        shared=shared(hub, "orbit-plan.md"),
    )


# a1: namespaces, reads, provenance


def test_an_agent_profile_run_writes_its_memory_into_its_namespace_and_reads_the_shared_ones_with_it(  # noqa: F811
    hub, agents, tmp_path, monkeypatch
):
    studio = agents.studio
    scout = a_run(hub, studio, "alice", "scout")
    written = write(hub, studio, scout, "orbit-scout.md")
    memory = data(written)["memory"]
    assert memory["agent"] == "scout" and memory["scope"] == "project" and memory["project"] == A
    assert "@scout" in written["content"][0]["text"] and "Curator" in written["content"][0]["text"]
    assert found(hub, studio, scout, "orbit") == {memory["id"], agents.shared["id"]}
    assert not run_tool(hub, studio, scout, "memory_get", id=memory["id"]).get("isError")

    # another profile and the default one read the shared memory alone, and the memory of @scout as one that is not
    for other in (a_run(hub, studio, "alice", "critic"), a_run(hub, studio, "alice")):
        assert found(hub, studio, other, "orbit") == {agents.shared["id"]}
        assert refusal(run_tool(hub, studio, other, "memory_get", id=memory["id"])).startswith("no memory")
    critic = a_run(hub, studio, "alice", "critic")
    own = data(write(hub, studio, critic, "orbit-critic.md"))["memory"]
    assert own["agent"] == "critic" and found(hub, studio, scout, "orbit") == {memory["id"], agents.shared["id"]}

    # members read it under the project's read rule, by id or by naming the agent; listings of no agent leave it out
    for login in ("alice", "carol"):
        shown = done(hub.client.get(f"/v1/memories/{memory['id']}", headers=getattr(hub, login)))
        assert shown["agent"] == "scout"
        assert ids(hub, login, agent="scout") == {memory["id"]}
        assert ids(hub, login, agent="critic") == {own["id"]}
        assert memory["id"] not in ids(hub, login) and agents.shared["id"] in ids(hub, login)
    searched = done(hub.client.get("/v1/memories/search", params={"q": "orbit", "agent": "scout"}, headers=hub.carol))
    assert [m["id"] for m in searched["items"]] == [memory["id"]]
    unknown = hub.client.get("/v1/memories", params={"agent": "nobody"}, headers=hub.carol)
    assert unknown.status_code == 404, unknown.text

    # a pull never brings the memories of agents
    names = {name for _, name in pulled(hub, tmp_path, "alice", monkeypatch)}
    assert "orbit-plan.md" in names and not {"orbit-scout.md", "orbit-critic.md"} & names


def test_a_member_writer_edits_and_deletes_an_agent_profile_memory_and_a_reader_does_not(hub, agents):
    scout = a_run(hub, agents.studio, "alice", "scout")
    memory = data(write(hub, agents.studio, scout, "orbit-scout.md"))["memory"]
    change = {
        "scope": "project",
        "project": A,
        "location": "harness",
        "name": "orbit-scout.md",
        "type": "project",
        "body": body("orbit-scout, corrected"),
        "agent": "scout",
        "if_revision": memory["revision"],
    }
    refused = hub.client.put("/v1/memories", json=change, headers=hub.carol)
    assert refused.status_code == 403, refused.text
    changed = done(hub.client.put("/v1/memories", json=change, headers=hub.alice))
    assert changed["id"] == memory["id"] and changed["agent"] == "scout" and changed["revision"] == 2
    default = hub.client.put("/v1/memories", json={**change, "agent": "aud"}, headers=hub.alice)
    assert default.status_code in (404, 422), default.text  # the default profile keeps no memory
    gone = hub.client.delete(f"/v1/memories/{memory['id']}", params={"if_revision": 2}, headers=hub.alice)
    assert done(gone)["deleted"] is True
    assert ids(hub, "alice", agent="scout") == set()


def test_every_memory_revision_a_run_writes_records_its_provenance(hub, agents):
    asked = say(hub, hub.one, "alice", "remember the orbit window?")
    run_id = a_run(hub, agents.studio, "alice", "scout", chat_id=hub.one, chat_message_id=asked["id"], hop=1)
    memory = data(write(hub, agents.studio, run_id, "orbit-window.md"))["memory"]
    history = done(hub.client.get(f"/v1/memories/{memory['id']}/revisions", headers=hub.alice))
    (revision,) = history["items"]
    version = live.sql(hub.db, select(tables.runs.c.profile_version).where(tables.runs.c.id == run_id))[0][0]
    assert revision["provenance"] == {
        "run": run_id,
        "proposal": None,  # no promotion wrote it (test_memory_promotion)
        "agent": "scout",
        "agent_version": version,
        "origin": "alice",
        "hop": 1,
        "chat": hub.one,
        "chat_message": asked["id"],
    }
    one = done(hub.client.get(f"/v1/memories/{memory['id']}/revisions/1", headers=hub.alice))
    assert one["provenance"] == revision["provenance"]
    by_member = done(hub.client.get(f"/v1/memories/{agents.shared['id']}/revisions", headers=hub.alice))
    assert by_member["items"][0]["provenance"] is None  # a person wrote it, from no run


def test_memory_list_of_one_agent_profile_through_the_cli(hub, agents, capsys, monkeypatch):
    scout = a_run(hub, agents.studio, "alice", "scout")
    memory = data(write(hub, agents.studio, scout, "orbit-scout.md"))["memory"]
    member = InProcessHub(hub.client, hub.carol)
    assert [m["id"] for m in cli_memory.listed(member, agent="scout")] == [memory["id"]]
    assert memory["id"] not in [m["id"] for m in cli_memory.listed(member, project=A)]

    monkeypatch.setattr(cli_memory, "_signed_in", lambda: (member, None))
    args = SimpleNamespace(project=None, agent="@scout", limit=100, json=False)
    assert cli_memory.cmd_memory_list(args) == 0
    shown = capsys.readouterr().out
    assert "orbit-scout.md" in shown and "@scout" in shown


# a2: the fences of a run


def test_an_agent_profile_run_writes_no_shared_memory_and_hears_where_its_memories_go(hub, agents):
    scout = a_run(hub, agents.studio, "alice", "scout")
    text = refusal(write(hub, agents.studio, scout, "orbit-plan.md", if_revision=1))
    assert "own namespace" in text and "Curator considers promoting" in text and "nothing was written" in text
    text = refusal(write(hub, agents.studio, scout, "orbit-plan.md"))
    assert "Curator considers promoting" in text and "under another name" in text
    shown = done(hub.client.get(f"/v1/memories/{agents.shared['id']}", headers=hub.alice))
    assert shown["revision"] == 1 and shown["agent"] is None  # untouched
    assert "type project, reference or feedback" in refusal(write(hub, agents.studio, scout, "me.md", "user"))


def test_a_chat_run_neither_reads_nor_writes_memories_of_type_user_and_reads_feedback(hub, agents):
    mine = shared(hub, "orbit-habits.md", "user")
    told = shared(hub, "orbit-feedback.md", "feedback")
    asked = say(hub, hub.one, "alice", "what do you know about orbit?")
    for handle in (None, "scout"):
        chat = a_run(hub, agents.studio, "alice", handle, chat_id=hub.one, chat_message_id=asked["id"], hop=1)
        assert refusal(run_tool(hub, agents.studio, chat, "memory_get", id=mine["id"])).startswith("no memory")
        assert not run_tool(hub, agents.studio, chat, "memory_get", id=told["id"]).get("isError")
        seen = found(hub, agents.studio, chat, "orbit")
        assert told["id"] in seen and mine["id"] not in seen
        text = refusal(write(hub, agents.studio, chat, f"orbit-{handle}-me.md", "user"))
        assert "type user" in text and "nothing was written" in text
    author = a_run(hub, agents.studio, "alice")  # a run of no chat reads them, as before
    assert not run_tool(hub, agents.studio, author, "memory_get", id=mine["id"]).get("isError")


def test_a_run_at_hop_two_writes_no_memory(hub, agents):
    asked = say(hub, hub.one, "alice", "@scout ask @critic about orbit")
    for handle in (None, "critic"):
        handed = a_run(hub, agents.studio, "alice", handle, chat_id=hub.one, chat_message_id=asked["id"], hop=2)
        text = refusal(write(hub, agents.studio, handed, f"orbit-hop-{handle}.md"))
        assert "hop 2" in text and "nothing was written" in text
        assert found(hub, agents.studio, handed, "orbit") == {agents.shared["id"]}  # reading still works


def test_one_run_writes_at_most_five_memories_limit(hub, agents):
    for handle in (None, "scout"):
        run_id = a_run(hub, agents.studio, "alice", handle)
        made = [data(write(hub, agents.studio, run_id, f"orbit-{handle}-{n}.md"))["memory"] for n in range(5)]
        text = refusal(write(hub, agents.studio, run_id, f"orbit-{handle}-5.md"))
        assert "5 memories" in text and "nothing was written" in text
        first = made[0]
        again = write(hub, agents.studio, run_id, first["name"], if_revision=first["revision"])
        assert not again.get("isError"), again  # an unchanged memory it wrote is no new one
        changed = run_tool(
            hub,
            agents.studio,
            run_id,
            "memory_write",
            name=first["name"],
            body=body("orbit, again"),
            type="project",
            if_revision=first["revision"],
        )
        assert data(changed)["memory"]["revision"] == 2  # changing one it wrote is no new one either

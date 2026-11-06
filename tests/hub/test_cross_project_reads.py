"""What of another project enters a chat run (``evo_agents.hub.server.agent_reads``, ``cross_labels``), as criterion
b5 of the plan chat-first-home names it.

b5: the agent of a chat run reads the memories (and the knowledge graph) of another project that the person at the
origin of the chain reads; the hub returns only the items the hub sink of the run's project clears by label, leaves
out the rest and says how many and why. The answer of an agent of another project, asked with ``agent ask``, passes
the same check before it enters the asking run.

Here the chat run is one of evo-agents, whose hub sink clears internal. lumen-kb clears customer for the hub and the
owner reads it up to customer, so the owner may read what the run may not take.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub.account_pool_setup import give_account_pool
from tests.hub.live import sql
from tests.hub.test_agent_delegation import KB, KB_AGENT, ask, bodies, kb_node
from tests.hub.test_chat_runs import (  # noqa: F401
    OWNER,
    app_key,
    chat_worker,
    claim,
    client,
    created,
    ended,
    hub,
    in_chats,
    moved,
    reply,
    started,
)
from tests.hub.test_chats import agent
from tests.hub.test_plans import registration

KB_SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "customer", "location": "any"}},
]
ABOVE = "level customer of project lumen-kb is above what hub sink 'hub' of project evo-agents clears (internal/any)"


def kb_above(client, hub, db, default: str = "internal") -> None:
    """lumen-kb with a hub sink that clears customer, labelled ``default`` by default, with the agent kb-bot; the
    owner is its admin up to customer."""
    body = {**registration(sinks=KB_SINKS), "default_label": {"level": default, "location": "any", "integrity": "U"}}
    assert client.put(f"/v1/projects/{KB}", json=body, headers=hub["admin"]).status_code == 200
    admin = {"role": "admin", "max_level": "customer"}
    assert client.put(f"/v1/admin/projects/{KB}/grants/{OWNER}", json=admin, headers=hub["admin"]).status_code == 200
    agent(client, hub["owner"], KB_AGENT, project=KB)
    give_account_pool(db, KB, KB_AGENT, OWNER)


def memory(client, headers, name: str, level: str, text: str) -> int:
    body = {
        "scope": "project",
        "project": KB,
        "location": "harness",
        "name": name,
        "type": "project",
        "body": f"---\nname: {name}\n---\n{text}\n",
        "label": {"level": level, "location": "any"},
    }
    response = client.put("/v1/memories", params={"sink": "hub"}, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def read(client, worker: dict, run_id: int, status: int = 200, **body) -> dict:
    response = in_chats(client, worker, run_id, "agent/reads", {"project": KB, **body})
    assert response.status_code == status, response.text
    return response.json()


def test_read_refused_above_run_sink(client, hub, hub_db):
    kb_above(client, hub, hub_db)
    shared = memory(client, hub["owner"], "export-keys.md", "internal", "The export signing key lives in the vault.")
    held = memory(client, hub["owner"], "export-customer.md", "customer", "The export signing key of Northwind.")
    # the owner reads both through lumen-kb's own hub sink
    params = {"q": "export signing key", "project": KB, "sink": "hub"}
    seen = client.get("/v1/memories/search", params=params, headers=hub["owner"])
    assert sorted(m["name"] for m in seen.json()["items"]) == ["export-customer.md", "export-keys.md"]

    studio = chat_worker(client, hub["owner"], "studio")
    chat = created(client, hub["owner"])
    _, spec = started(client, hub, studio, chat)
    run = spec["id"]

    found = read(client, studio, run, tool="memory_search", query="export signing key")
    assert (found["shown"], found["withheld"]) == (1, 1)
    assert "export-keys.md" in found["text"] and "export-customer.md" not in found["text"]
    assert "Northwind" not in found["text"]
    assert found["why"] == f"1 item of what you may read was left out, since {ABOVE}."
    assert found["text"].endswith(found["why"])

    whole = read(client, studio, run, tool="memory_get", memory_id=shared)
    assert (whole["shown"], whole["withheld"], whole["why"]) == (1, 0, None)
    assert "lives in the vault" in whole["text"]
    refused = read(client, studio, run, tool="memory_get", memory_id=held)
    assert (refused["shown"], refused["withheld"]) == (0, 1) and "Northwind" not in refused["text"]
    assert ABOVE in refused["why"]

    # the run's own project is read with the evo-hub tools; a project the origin holds no grant on is not found
    own = in_chats(client, studio, run, "agent/reads", {"project": "evo-agents", "tool": "memory_search", "query": "x"})
    assert own.status_code == 422 and "evo-hub tools" in own.text
    other = in_chats(
        client, studio, run, "agent/reads", {"project": "lumen-ops", "tool": "memory_search", "query": "x"}
    )
    assert other.status_code == 404


def test_delegated_answer_filtered_by_parent_sink(client, hub, hub_db):
    """kb-bot's runs carry lumen-kb's default label, customer, which the hub sink of evo-agents does not clear: the
    asking run learns that the run ended and why its answer was withheld, never the answer."""
    kb_above(client, hub, hub_db, default="customer")
    studio = chat_worker(client, hub["owner"], "studio")
    node = kb_node(client, hub["owner"], hub_db)
    chat = created(client, hub["owner"])
    mention, spec = started(client, hub, studio, chat)
    parent = spec["id"]

    asked = ask(client, studio, parent)
    child = asked["run_id"]
    assert asked["refusal"] is None and child is not None
    assert claim(client, node)["id"] == child
    moved(client, node, child, "running", session_id="sess-kb")
    reply(client, node, child, "Northwind's export key is read in kb/export/keys.py::load_key.")
    ended(client, node, child)

    d = tables.decisions
    ((state, answer),) = sql(hub_db, select(d.c.state, d.c.answer_text).where(d.c.id == asked["decision_id"]))
    assert state == "answered"
    assert f"run #{child} of @{KB_AGENT}, which you asked, ended done, and the hub withheld what it said" in answer
    assert ABOVE in answer and "load_key" not in answer and "Northwind" not in answer
    shown = bodies(client, hub["owner"], chat, mention["id"])
    assert f"@{KB_AGENT} ended run #{child} done; its answer was withheld from this run: {ABOVE}" in shown
    assert not any("load_key" in item for item in shown if item.startswith(f"@{KB_AGENT} answered"))

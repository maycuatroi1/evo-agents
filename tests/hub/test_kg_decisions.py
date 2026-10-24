"""The decisions on a project's knowledge graph (``evo_agents.hub.server.kg_decisions``), the MCP tool ``kg_propose``
and ``evo-agents hub kg decisions``.

Checked here: an outsider gets 404, a reader reads but may not decide (403); every write names the decision revision
it read and a stale one gets 409 with nothing changed; a writer's decision is active at once, writes the audit log,
queues a build that applies it (the node carries its deprecation, kg_search leaves it out unless asked); an agent's
proposal changes nothing until a writer accepts it, and a rejected one stays rejected; a link needs a relation the
ontology knows and nodes the writer sees; an active decision whose node a later build no longer holds is an orphan,
which never breaks the build and a writer undoes; a member never sees a decision about a node above their level; the
command line prints the decisions as a JSON array, each with its state, and takes one.

One hub serves every test of the module; xdist may give a worker any of the tests, so each one reads the revision as
it finds it and decides about subjects of its own.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import get_args

import pytest

from tests.hub import live, pg
from tests.hub.kg_fixture import AGENT, CONTRACT, RUNBOOK, RUNBOOK_SECTION, SHARED_NODE, Harness, registration

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import kg_decisions, kg_decisions_cli, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.kg_decisions import Kind, State, Step
from tests.hub.s3 import fake_s3
from tests.hub.test_kg import InProcessHub, make_config, run_jobs
from tests.hub.test_mcp import call as mcp_call
from tests.hub.test_mcp import rpc

PROJECT = "kgdecide"
EMPTY = "kgdecide-empty"  # registered, never pushed: no graph
MEMBERS = {
    "wendy": ("writer", "customer"),  # pushed the fixture
    "ivan": ("reader", "internal"),
}
ARCHITECTURE = "docs:doc:architecture"
HUB_URL = "https://hub.test"


@pytest.fixture(scope="module")
def hub(hub_template, tmp_path_factory):
    base = tmp_path_factory.mktemp("kg-decisions")
    db = pg.clone_database(hub_template)
    try:
        with fake_s3() as s3:
            app = create_app(replace(make_config(db, base / "api", s3), public_url=HUB_URL))  # /mcp answers that host
            with TestClient(app, base_url=HUB_URL) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                for name in (PROJECT, EMPTY):
                    response = client.put(f"/v1/projects/{name}", json=registration(name), headers=admin)
                    assert response.status_code == 200, response.text
                members = {}
                for login, (role, level) in MEMBERS.items():
                    for name in (PROJECT, EMPTY):
                        response = client.put(
                            f"/v1/admin/projects/{name}/grants/{login}",
                            json={"role": role, "max_level": level},
                            headers=admin,
                        )
                        assert response.status_code in (200, 201), response.text
                    members[login] = live.bearer(live.insert_token(db, login))
                members["olga"] = live.bearer(live.insert_token(db, "olga"))  # no grant on either project
                harness = Harness(base / "machine", PROJECT)
                harness.sync()
                harness.push(InProcessHub(client, members["wendy"]))
                run_jobs(db, s3, base / "worker")

                def build(extra: str | None = None, *, sync: bool = False) -> dict:
                    """Run the queued builds, after a new sync of the harness when ``sync``; the newest build."""
                    if sync:
                        harness.sync(extra)
                        harness.push(InProcessHub(client, members["wendy"]))
                    run_jobs(db, s3, base / "worker")
                    return client.get(f"/v1/kg/{PROJECT}/builds", headers=members["wendy"]).json()["builds"][0]

                assert build()["status"] == "succeeded"
                yield SimpleNamespace(client=client, db=db, build=build, **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def listed(hub, member: str, **params) -> dict:
    response = hub.client.get(f"/v1/kg/{PROJECT}/decisions", params=params, headers=getattr(hub, member))
    assert response.status_code == 200, response.text
    return response.json()


def rev(hub) -> int:
    return listed(hub, "wendy")["rev"]


def take(hub, member: str, body: dict, *, base_rev: int | None = None):
    sent = {"reason": "said in the test", **body, "base_rev": rev(hub) if base_rev is None else base_rev}
    return hub.client.post(f"/v1/kg/{PROJECT}/decisions", json=sent, headers=getattr(hub, member))


def step(hub, member: str, number: int, action: str, reason: str = "checked", *, base_rev: int | None = None):
    sent = {"reason": reason, "base_rev": rev(hub) if base_rev is None else base_rev}
    return hub.client.post(f"/v1/kg/{PROJECT}/decisions/{number}/{action}", json=sent, headers=getattr(hub, member))


def node(hub, node_id: str, member: str = "wendy") -> dict:
    response = hub.client.get(f"/v1/kg/{PROJECT}/node", params={"id": node_id}, headers=getattr(hub, member))
    assert response.status_code == 200, response.text
    return response.json()["node"]


def search(hub, query: str, **arguments) -> str:
    response = hub.client.post(
        f"/v1/kg/{PROJECT}/tools/kg_search",
        json={"arguments": {"query": query, "limit": 25, **arguments}, "sink": AGENT},
        headers=hub.wendy,
    )
    assert response.status_code == 200, response.text
    return response.json()["content"][0]["text"]


def mine(found: dict, number: int) -> dict:
    return next(d for d in found["decisions"] if d["number"] == number)


def test_the_kg_decision_routes_name_the_kinds_states_and_steps_of_the_model():
    assert get_args(Kind) == kg_decisions.KINDS
    assert get_args(State) == (*kg_decisions.STATES, kg_decisions.ORPHAN)
    assert get_args(Step) == tuple(kg_decisions.TRANSITIONS)


def test_kg_decisions_answer_404_to_an_outsider_and_403_to_a_reader_who_decides(hub):
    assert hub.client.get(f"/v1/kg/{PROJECT}/decisions", headers=hub.olga).status_code == 404
    assert take(hub, "olga", {"kind": "deprecate", "src": RUNBOOK_SECTION}, base_rev=0).status_code == 404
    seen = listed(hub, "ivan")
    assert seen["can_decide"] is False and seen["graph"] is not None
    assert take(hub, "ivan", {"kind": "deprecate", "src": RUNBOOK_SECTION}).status_code == 403
    assert step(hub, "ivan", 1, "undo").status_code == 403
    empty = hub.client.get(f"/v1/kg/{EMPTY}/decisions", headers=hub.ivan).json()
    assert empty == {"graph": None, "rev": 0, "can_decide": False, "decisions": []}
    response = hub.client.post(
        f"/v1/kg/{EMPTY}/decisions",
        json={"kind": "deprecate", "src": RUNBOOK, "reason": "x", "base_rev": 0},
        headers=hub.wendy,
    )
    assert response.status_code == 404  # no graph to decide on yet


def test_a_kg_decision_needs_a_reason_a_shape_a_known_relation_and_nodes_the_writer_sees(hub):
    assert take(hub, "wendy", {"kind": "deprecate", "src": RUNBOOK, "reason": "   "}).status_code == 422
    assert take(hub, "wendy", {"kind": "merge", "src": RUNBOOK, "dst": RUNBOOK}).status_code == 422
    assert take(hub, "wendy", {"kind": "link", "src": RUNBOOK, "dst": SHARED_NODE}).status_code == 422
    assert take(hub, "wendy", {"kind": "deprecate", "src": RUNBOOK, "rel": "mentions"}).status_code == 422
    unknown = take(hub, "wendy", {"kind": "link", "src": RUNBOOK, "rel": "loves", "dst": SHARED_NODE})
    assert unknown.status_code == 422 and "loves" in unknown.text
    assert take(hub, "wendy", {"kind": "deprecate", "src": "docs:doc:nothing-here"}).status_code == 404
    missing_edge = {"kind": "deprecate", "src": RUNBOOK, "rel": "calls", "dst": SHARED_NODE}
    assert take(hub, "wendy", missing_edge).status_code == 404


def test_a_stale_revision_gets_409_for_a_kg_decision_and_nothing_changes(hub):
    now = rev(hub)
    made = take(hub, "wendy", {"kind": "deprecate", "src": "requirement:KB-02", "reason": "folded into KB-01"})
    assert made.status_code == 201, made.text
    stale = take(hub, "wendy", {"kind": "deprecate", "src": "requirement:KB-03"}, base_rev=now)
    assert stale.status_code == 409 and f"revision is {now + 1}" in stale.json()["message"]
    assert not [d for d in listed(hub, "wendy")["decisions"] if d["src"] == "requirement:KB-03"]
    number = made.json()["number"]
    assert step(hub, "wendy", number, "undo", base_rev=now).status_code == 409
    assert mine(listed(hub, "wendy"), number)["state"] == "active"
    twice = take(hub, "wendy", {"kind": "deprecate", "src": "requirement:KB-02"})
    assert twice.status_code == 409  # an active decision already says that


def test_a_writers_kg_decision_is_active_audited_and_the_next_build_deprecates_the_node(hub):
    made = take(hub, "wendy", {"kind": "deprecate", "src": ARCHITECTURE, "reason": "  merged into the runbook  "})
    assert made.status_code == 201, made.text
    decision = made.json()
    assert decision["state"] == "active" and decision["kind"] == "deprecate" and decision["rel"] is None
    [event] = decision["history"]
    assert event["action"] == "decide" and event["by"] == "wendy" and event["reason"] == "merged into the runbook"
    assert event["build_id"] and event["rev"] >= 1
    a = tables.audit
    target = f"{PROJECT} decision={decision['number']} action=decide"
    assert live.sql(hub.db, select(a.c.target).where(a.c.action == audit.KG_DECISION, a.c.target == target))

    built = hub.build()
    assert built["status"] == "succeeded" and built["id"] != event["build_id"]
    deprecated = node(hub, ARCHITECTURE)["props"]["deprecated"]
    assert deprecated == {"reason": "merged into the runbook", "decision": decision["number"]}
    assert f"[{ARCHITECTURE}]" not in search(hub, "architecture")
    assert f"[{ARCHITECTURE}]" in search(hub, "architecture", deprecated=True)
    assert mine(listed(hub, "ivan"), decision["number"])["state"] == "active"


def test_an_agents_kg_decision_proposal_waits_for_a_writer_who_accepts_or_rejects_it(hub):
    base = rev(hub)
    arguments = {"kind": "link", "id": RUNBOOK, "rel": "verifies", "to": SHARED_NODE, "reason": "it checks KB-01"}
    stale = rpc(
        hub, "wendy", mcp_call(7, "kg_propose", {**arguments, "base_rev": base + 5}), project=PROJECT, sink=AGENT
    ).json()
    assert stale["result"]["isError"] and str(base) in stale["result"]["content"][0]["text"]
    answer = rpc(
        hub, "wendy", mcp_call(8, "kg_propose", {**arguments, "base_rev": base}), project=PROJECT, sink=AGENT
    ).json()
    result = answer["result"]
    assert not result.get("isError"), result
    number = int(result["content"][0]["text"].split()[2])
    proposed = mine(listed(hub, "ivan"), number)
    assert proposed["state"] == "proposed" and proposed["history"][0]["action"] == "propose"
    assert step(hub, "ivan", number, "accept").status_code == 403
    assert step(hub, "wendy", number, "undo").status_code == 409  # only an active one is undone
    accepted = step(hub, "wendy", number, "accept", "a fair link")
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "active" and [e["action"] for e in accepted.json()["history"]] == [
        "propose",
        "accept",
    ]
    hub.build()
    edges = hub.client.get(f"/v1/kg/{PROJECT}/node", params={"id": RUNBOOK}, headers=hub.wendy).json()["outgoing"]
    assert any(e["rel"] == "verifies" and e["node"] == SHARED_NODE and e["status"] == "declared" for e in edges)

    other = {"kind": "merge", "id": RUNBOOK_SECTION, "to": RUNBOOK, "reason": "the same", "base_rev": rev(hub)}
    reply = rpc(hub, "wendy", mcp_call(9, "kg_propose", other), project=PROJECT, sink=AGENT).json()["result"]
    assert not reply.get("isError"), reply
    second = int(reply["content"][0]["text"].split()[2])
    rejected = step(hub, "wendy", second, "reject", "a section is not its document")
    assert rejected.status_code == 200 and rejected.json()["state"] == "rejected"
    assert step(hub, "wendy", second, "accept").status_code == 409
    assert [d["number"] for d in listed(hub, "wendy", state="rejected")["decisions"]] == [second]


def test_a_kg_decision_whose_node_a_later_build_drops_is_an_orphan_a_writer_undoes(hub):
    built = hub.build("orphan-note", sync=True)
    assert built["status"] == "succeeded"
    note = "docs:doc:orphan-note"
    made = take(hub, "wendy", {"kind": "deprecate", "src": note, "reason": "a stray note"})
    assert made.status_code == 201, made.text
    number = made.json()["number"]
    again = hub.build("another-note", sync=True)  # the stray note is gone from the sources
    assert again["status"] == "succeeded", again
    assert mine(listed(hub, "wendy"), number)["state"] == "orphan"
    assert number in [d["number"] for d in listed(hub, "wendy", state="orphan")["decisions"]]
    undone = step(hub, "wendy", number, "undo", "the note went")
    assert undone.status_code == 200 and undone.json()["state"] == "undone"
    assert mine(listed(hub, "wendy"), number)["state"] == "undone"


def test_a_member_never_sees_a_kg_decision_about_a_node_above_their_level(hub):
    made = take(hub, "wendy", {"kind": "deprecate", "src": CONTRACT, "reason": "the contract ended"})
    assert made.status_code == 201, made.text
    number = made.json()["number"]
    assert mine(listed(hub, "wendy"), number)["state"] == "active"
    seen = listed(hub, "ivan")
    assert number not in [d["number"] for d in seen["decisions"]] and CONTRACT not in json.dumps(seen)
    assert step(hub, "ivan", number, "undo").status_code == 403


def test_the_kg_decisions_command_prints_an_array_with_states_and_takes_a_decision(hub, capsys):
    client = InProcessHub(hub.client, hub.wendy)
    args = SimpleNamespace(
        project=PROJECT, action=None, target=None, state=None, rel=None, to=None, reason=None, base_rev=None, json=True
    )
    assert kg_decisions_cli._list(client, args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert isinstance(printed, list)
    for d in printed:
        assert set(d) == set(kg_decisions_cli.DECISION) and d["state"] in get_args(State)

    took = SimpleNamespace(**{**vars(args), "action": "deprecate", "target": "requirement:KB-77", "reason": "noise"})
    assert kg_decisions_cli._write(client, took) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == set(kg_decisions_cli.DECISION) and shown["state"] == "active"

    args.json = False
    assert kg_decisions_cli._list(client, args) == 0
    text = capsys.readouterr().out
    assert "STATE" in text and "requirement:KB-77" in text and "revision" in text
    assert kg_decisions_cli._problem(SimpleNamespace(**{**vars(args), "action": "link", "target": "x"})) is not None

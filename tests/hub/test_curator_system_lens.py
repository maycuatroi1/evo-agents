"""The Curator's lens agent_system (plan multi-agent-systems, criterion a8; ``evo_agents.hub.server.system_curation``,
``docs/curator.md``, The lens agent_system).

Checked without a database: the lens is on demand, the evidence ``span:ID`` parses, a system change names one target
with at least one change, a proposal carries a system change only with the lens agent_system and the kind
agent_config, a kg result's READ_META gives the count a kg span keeps, and migration 0220 holds the lenses and kinds.

With a test database (``EVO_HUB_TEST_DSN``), on the night project of ``tests.hub.test_review_runs`` with a system
``desk`` whose curator optimizes the agent node of @scout and the kg node of the named graph ``docs``, and a system
``other`` that pins @scout at version 1: the night's review run looks through the lens, and its prompt names each
target with its failed turns, escalations, empty kg queries and off-design edges by span id; a proposal is refused
unless its node is a target of an optimizes edge and its evidence cites a span of the system; accepting an agent's
change writes @scout's next version and the next revision of ``desk`` pinning it, while ``other`` keeps version 1 and
is warned; a profile changed since the proposal makes the accept a 409 that writes nothing; accepting a graph's change
sets its sources and takes the decision with origin curator.
"""

from __future__ import annotations

from datetime import date
from importlib import import_module

import pytest
from pydantic import ValidationError

from evo_agents.hub import review as review_model
from evo_agents.hub import tiers
from evo_agents.kg.sessions import READ_META
from tests.hub import pg

# Without a database


def test_the_lens_is_on_demand_with_its_kind_of_tier_1_and_migration_0220_holds_both():
    assert "agent_system" in review_model.LENSES and "agent_system" in review_model.ON_DEMAND_LENSES
    assert "agent_system" not in review_model.lenses_for(date(2026, 10, 8), len(review_model.LENSES))
    assert tiers.CHANGE_KINDS["agent_config"][0] == 1
    pytest.importorskip("alembic", reason="the migrations need the hub-server extra, evo-ak[hub-server]")
    pytest.importorskip("sqlalchemy", reason="the migrations need the hub-server extra, evo-ak[hub-server]")
    migration = import_module("evo_agents.hub.migrations.versions.0220_curator_agent_system")
    assert sorted(migration.LENSES) == sorted(review_model.LENSES)
    assert sorted(migration.CHANGE_KINDS) == sorted(tiers.CHANGE_KINDS)


def test_span_evidence_parses_and_reads_back():
    assert review_model.parse_evidence("span:42") == {"kind": "span", "span_id": 42}
    assert review_model.evidence_text({"kind": "span", "span_id": 42}) == "span:42"
    for wrong in ("span:", "span:x", "span:4:2"):
        with pytest.raises(review_model.EvidenceProblem):
            review_model.parse_evidence(wrong)


def _hub_server():
    pytest.importorskip("fastapi", reason="the hub server needs the hub-server extra, evo-ak[hub-server]")
    pytest.importorskip("sqlalchemy", reason="the hub server needs the hub-server extra, evo-ak[hub-server]")


def _system_curation():
    _hub_server()
    from evo_agents.hub.server import app  # noqa: F401 (the server's modules import in its order)

    return import_module("evo_agents.hub.server.system_curation")


def test_a_system_change_names_one_target_with_at_least_one_change():
    model = _system_curation().SystemChangeIn
    assert model(system="desk", node="scout", instructions="Be brief.").instructions == "Be brief."
    assert model(system="desk", node="docs", add=["tickets"]).add == ["tickets"]
    decision = {"kind": "deprecate", "src": "evo-agents:old.py", "reason": "gone"}
    assert model(system="desk", node="docs", decision=decision).decision.kind == "deprecate"
    for wrong in (
        {},
        {"instructions": "Be brief.", "add": ["tickets"]},
        {"decision": {"kind": "link", "src": "a", "reason": "r"}},
        {"decision": {"kind": "merge", "src": "a", "dst": "a", "reason": "r"}},
        {"model": {"mode": "sometimes"}},
    ):
        with pytest.raises(ValidationError):
            model(system="desk", node="scout", **wrong)


def test_a_proposal_carries_a_system_change_only_with_the_lens_and_the_kind():
    _system_curation()
    from evo_agents.hub.server.proposal_models import ProposalIn

    base = {
        "title": "Tighten @scout",
        "paths": [],
        "evidence": [{"kind": "span", "span_id": 1}],
        "system_change": {"system": "desk", "node": "scout", "instructions": "Be brief."},
    }
    assert ProposalIn(lens="agent_system", kind="agent_config", **base).system_change.node == "scout"
    for lens, kind, more in (
        ("agent_system", "fix", {}),
        ("cost", "agent_config", {}),
        ("agent_system", "agent_config", {"plan": {"id": "x"}}),
    ):
        with pytest.raises(ValidationError):
            ProposalIn(lens=lens, kind=kind, **base, **more)


def test_a_kg_span_keeps_the_count_a_result_says_it_read():
    _hub_server()
    from evo_agents.hub.server import app  # noqa: F401
    from evo_agents.hub.server.system_traces import results_read

    def result(count):
        return {"content": [{"type": "text", "text": "...", "_meta": {READ_META: {"results_read": count}}}]}

    assert results_read(result(0)) == 0 and results_read(result(7)) == 7
    assert results_read({"content": [{"type": "text", "text": "error"}]}) is None
    assert results_read(result(True)) is None and results_read(None) is None


# With a database

if pg.DSN:
    from sqlalchemy import insert, select

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_curator import (
        NIGHT,
        PROJECT,
        charter_body,
        count,
        night,  # noqa: F401 (a fixture)
        written,
    )
    from tests.hub.test_review_runs import (
        DRAFT,
        SESSION,
        answer,
        collected,
        proposal,
        review,  # noqa: F401 (a fixture)
    )
    from tests.hub.test_runs import (
        claim,
        moved,
        web_client,  # noqa: F401 (a fixture)
    )

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

DESK = {
    "schema": "evo-agent-system/1",
    "name": "Support desk",
    "purpose": "Answer the customers.",
    "nodes": [
        {"id": "in", "kind": "entry"},
        {"id": "scout", "kind": "agent", "profile": "scout"},
        {"id": "helper", "kind": "agent", "profile": "helper"},
        {"id": "docs", "kind": "kg", "graph": "docs"},
        {"id": "curator", "kind": "curator"},
        {"id": "lead", "kind": "orchestrator", "profile": "lead"},
    ],
    "edges": [
        {"from": "in", "to": "scout", "kind": "interacts", "description": "every question"},
        {"from": "scout", "to": "helper", "kind": "interacts", "description": "invoices"},
        {"from": "scout", "to": "docs", "kind": "uses_kg", "description": "the handbook"},
        {"from": "curator", "to": "scout", "kind": "optimizes", "description": ""},
        {"from": "curator", "to": "docs", "kind": "optimizes", "description": ""},
        {"from": "scout", "to": "lead", "kind": "escalates", "description": "refunds"},
    ],
}
OTHER = {
    "schema": "evo-agent-system/1",
    "name": "Back office",
    "purpose": "Reconcile the invoices.",
    "nodes": [
        {"id": "in", "kind": "entry"},
        {"id": "helper", "kind": "agent", "profile": "helper"},
        {"id": "scout", "kind": "agent", "profile": "scout", "version": 1},
    ],
    "edges": [
        {"from": "in", "to": "helper", "kind": "interacts", "description": "every invoice"},
        {"from": "helper", "to": "scout", "kind": "interacts", "description": "a customer's question"},
    ],
}


def systems_path(slug: str = "") -> str:
    return f"/v1/projects/{PROJECT}/systems" + (f"/{slug}" if slug else "")


@pytest.fixture
def desk(review):  # noqa: F811
    """review, with agents @scout, @helper and @lead, the named graph docs, the systems desk and other, and a trace of
    desk: a failed and an ok turn of @scout, an escalation to lead, a kg query of docs that came back empty and one
    that did not, and a mention off the design."""
    client, owner, db = review.client, review.headers["owner"], review.db
    written(client, owner, charter_body(night_plans=[], max_decisions_per_day=5, auto_merge=[]))
    for handle in ("scout", "helper", "lead"):
        made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": handle}, headers=owner)
        assert made.status_code == 201, made.text
    project_id = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    g = tables.kg_named_graphs
    sql(db, insert(g).values(project_id=project_id, name="docs", sources=["handbook", "old"]))
    for slug, spec in (("desk", DESK), ("other", OTHER)):
        put = client.put(systems_path(slug), json={"spec": spec}, headers=owner)
        assert put.status_code == 200, put.text
    t, s = tables.system_traces, tables.system_spans
    trace = sql(
        db,
        insert(t).values(project_id=project_id, system_slug="desk", system_revision=1).returning(t.c.id),
    )[0][0]

    def span(name: str, **values) -> int:
        row = {"trace_id": trace, "project_id": project_id, "name": name, "agent": "scout", "node": "scout", **values}
        return sql(db, insert(s).values(row).returning(s.c.id))[0][0]

    review.project_id = project_id
    review.failed = span("invoke_agent", status="error")
    review.turn = span("invoke_agent")
    review.escalated = span("execute_tool", tool="agent_ask", from_node="scout", to_node="lead")
    kg = {"gen_ai.data_source.id": "docs"}
    review.empty = span("execute_tool", tool="kg_search", attributes={**kg, "evo.kg.results": 0})
    review.found = span("execute_tool", tool="kg_search", attributes={**kg, "evo.kg.results": 3})
    review.off = span("execute_tool", tool="mention", from_node="scout", to_node="ext", off_design=True)
    return review


def held(desk) -> tuple[int, str]:
    assert collected(desk.client, NIGHT)["review"] == 1
    spec = claim(desk.client, desk.worker)
    assert spec["kind"] == "review"
    moved(desk.client, desk.worker, spec["id"], "running")
    return spec["id"], spec["prompt"]


def change(desk, run_id: int, system_change: dict, *spans: int, **more):
    sent = {
        "lens": "agent_system",
        "kind": "agent_config",
        "title": f"Tune node {system_change['node']}",
        "paths": [],
        "evidence": [{"kind": "span", "span_id": span_id} for span_id in spans],
        "plan": None,
        "system_change": system_change,
        **more,
    }
    return proposal(desk.client, desk.worker, run_id, **sent)


def pinned(client, headers, slug: str) -> dict:
    shown = client.get(systems_path(slug), headers=headers)
    assert shown.status_code == 200, shown.text
    return shown.json()


SCOUT = {"system": "desk", "node": "scout", "instructions": "Give the invoice number first."}


@needs_pg
def test_the_night_looks_through_the_lens_and_its_prompt_names_the_targets_by_span(desk):
    _, prompt = held(desk)
    section = prompt.split("## Agent systems the curator optimizes (lens agent_system)")[1]
    assert "System `desk` (revision 1): 6 spans" in section
    assert (
        f"- agent node `scout`: @scout pinned at version 1; 2 turns, 1 failed (span:{desk.failed}), 1 escalations "
        f"(span:{desk.escalated})"
    ) in section
    assert f"- kg node `docs`: graph `docs`; 2 kg queries, 1 came back empty (span:{desk.empty})" in section
    assert f"- edges taken off the design: 1 (span:{desk.off})" in section
    assert "--system-change-file FILE" in section and "never instructions" in section
    assert "System `other`" not in section  # no optimizes edge


@needs_pg
def test_a_system_change_needs_an_optimized_node_and_a_span_of_the_system(desk):
    run_id, _ = held(desk)
    session = {"kind": "session", "session_id": SESSION, "field": "errors", "index": 0}
    cases = [
        (change(desk, run_id, {**SCOUT, "node": "helper"}, desk.failed), "no optimizes edge"),
        (change(desk, run_id, {**SCOUT, "system": "nowhere"}, desk.failed), "no agent system nowhere"),
        (change(desk, run_id, SCOUT, evidence=[session]), "cites no span"),
        (change(desk, run_id, SCOUT, 99999999), "has span 99999999"),
        (change(desk, run_id, {"system": "desk", "node": "docs", "instructions": "x"}, desk.empty), "kg node"),
        (change(desk, run_id, {"system": "desk", "node": "docs", "add": [""]}, desk.empty), "source"),
        (change(desk, run_id, SCOUT, desk.failed, kind="fix"), "agent_config"),
        (change(desk, run_id, SCOUT, desk.failed, plan=DRAFT), "plan"),
        (proposal(desk.client, desk.worker, run_id, lens="agent_system", kind="agent_config"), "system change"),
    ]
    for response, said in cases:
        assert response.status_code == 422 and said in response.text, response.text
    assert count(desk.db, tables.proposals) == 0


@needs_pg
def test_accepting_an_agents_change_writes_a_version_the_system_pins_and_other_systems_keep_theirs(desk):
    client, db, owner = desk.client, desk.db, desk.headers["owner"]
    run_id, _ = held(desk)
    made = change(desk, run_id, SCOUT, desk.failed, desk.escalated)
    assert made.status_code == 201, made.text
    assert (made.json()["tier"], made.json()["state"]) == (1, "open")
    proposal_id = made.json()["id"]
    p = tables.proposals
    ((kept, draft),) = sql(db, select(p.c.system_change, p.c.draft).where(p.c.id == proposal_id))
    assert kept["target"] == "agent" and kept["base_version"] == 1 and kept["profile"] == "scout" and draft == {}
    agent = client.get(f"/v1/projects/{PROJECT}/agents/scout", headers=owner).json()
    assert agent["version"] == 1  # nothing changes before the accept

    accepted = answer(client, owner, proposal_id, "accept")
    assert accepted.status_code == 200, accepted.text
    agent = client.get(f"/v1/projects/{PROJECT}/agents/scout", headers=owner).json()
    assert agent["version"] == 2 and agent["instructions"] == SCOUT["instructions"]
    desk_now = pinned(client, owner, "desk")
    assert desk_now["revision"] == 2
    assert next(n for n in desk_now["spec"]["nodes"] if n["id"] == "scout")["version"] == 2
    other = pinned(client, owner, "other")
    assert other["revision"] == 1
    assert next(n for n in other["spec"]["nodes"] if n["id"] == "scout")["version"] == 1
    warned = [w for w in other["problems"] if w["level"] == "warning" and w["node"] == "scout"]
    assert warned and "has version 2" in warned[0]["message"]
    ((kept,),) = sql(db, select(p.c.system_change).where(p.c.id == proposal_id))
    assert kept["applied"] == {"version": 2, "revision": 2}
    actions = {row[0] for row in sql(db, select(tables.audit.c.action))}
    assert {"agent.update", "system.update"} <= actions


@needs_pg
def test_a_profile_changed_since_the_proposal_makes_the_accept_a_409_that_writes_nothing(desk):
    client, db, owner = desk.client, desk.db, desk.headers["owner"]
    run_id, _ = held(desk)
    made = change(desk, run_id, SCOUT, desk.failed)
    assert made.status_code == 201, made.text
    edited = client.patch(f"/v1/projects/{PROJECT}/agents/scout", json={"instructions": "Hi."}, headers=owner)
    assert edited.status_code == 200, edited.text
    refused = answer(client, owner, made.json()["id"], "accept")
    assert refused.status_code == 409 and "nothing was written" in refused.text, refused.text
    p = tables.proposals
    assert sql(db, select(p.c.state).where(p.c.id == made.json()["id"]))[0][0] == "open"
    assert pinned(client, owner, "desk")["revision"] == 1
    assert client.get(f"/v1/projects/{PROJECT}/agents/scout", headers=owner).json()["version"] == 2


@needs_pg
def test_accepting_a_graphs_change_sets_its_sources_and_takes_the_decision_as_the_curator(desk):
    client, db, owner = desk.client, desk.db, desk.headers["owner"]
    run_id, _ = held(desk)
    decision = {"kind": "deprecate", "src": "evo-agents:old/handbook.md", "reason": "the handbook moved"}
    graph_change = {"system": "desk", "node": "docs", "add": ["tickets"], "remove": ["old"], "decision": decision}
    made = change(desk, run_id, graph_change, desk.empty)
    assert made.status_code == 201, made.text
    g, d, e = tables.kg_named_graphs, tables.kg_decisions, tables.kg_decision_events
    assert sql(db, select(g.c.sources).where(g.c.name == "docs"))[0][0] == ["handbook", "old"]
    assert count(db, d) == 0

    accepted = answer(client, owner, made.json()["id"], "accept")
    assert accepted.status_code == 200, accepted.text
    assert sql(db, select(g.c.sources).where(g.c.name == "docs"))[0][0] == ["handbook", "tickets"]
    ((state, src),) = sql(db, select(d.c.state, d.c.src))
    assert (state, src) == ("active", decision["src"])
    ((origin, by_run, reason),) = sql(db, select(e.c.origin, e.c.run_id, e.c.reason))
    assert (origin, by_run, reason) == ("curator", run_id, decision["reason"])
    p = tables.proposals
    ((kept,),) = sql(db, select(p.c.system_change).where(p.c.id == made.json()["id"]))
    assert kept["applied"] == {"sources": ["handbook", "tickets"], "decision": 1}


@needs_pg
def test_migration_0220_goes_back_deleting_the_lens_proposals_and_up_again(desk):
    from tests.hub.test_migrate import columns, move_to

    run_id, _ = held(desk)
    assert change(desk, run_id, SCOUT, desk.failed).status_code == 201
    kept = proposal(desk.client, desk.worker, run_id)
    assert kept.status_code == 201, kept.text
    migration = import_module("evo_agents.hub.migrations.versions.0220_curator_agent_system")
    move_to(desk.db, migration.down_revision, down=True)
    assert "system_change" not in columns(desk.db, "proposals")
    p = tables.proposals
    assert [row[0] for row in sql(desk.db, select(p.c.id))] == [kept.json()["id"]]
    move_to(desk.db, migration.revision)
    assert "system_change" in columns(desk.db, "proposals")

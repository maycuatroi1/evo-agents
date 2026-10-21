"""@strategist, the agent type strategist of the core plugin canvas, on the hub (``evo_agents.core_plugins.canvas``:
``agent``, ``acting``, ``commands``, ``proposals``, ``brainstorm``), as criterion a5 of plan living-lean-canvas names it.

An admin of a project that enabled canvas enables the type under a member (``run_as``): the profile @strategist with
the pack canvas/strategist. The plugin's tools are on the hub's /mcp only where the project enables it. A writer
mentions @strategist in a chat: its run goes to a worker of run_as and acts for that writer, whose command applies at
once as an agent's rev for them, at the rev the agent read (a claim changed since is refused with what changed), and
an undo in chat takes back the writer's last command. A brainstorm is a task run that only proposes: new claims at
assumption, each with a test card; a writer accepts or rejects each, and a proposal whose claim changed goes stale.
What rests on simulated users carries the synthetic flag; @strategist never moves a claim to validated, confirms
evidence or decides a proposal. The agent on the worker is a stand-in: the test calls the tools with the worker's token
and the run, as its MCP client would. The pack's files are ``tests.worker.test_strategist_agent``'s. The product of
the canvas, Northwind Pantry, is made up.
"""

from __future__ import annotations

import dataclasses
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.core_plugins.canvas import content, proposals, tools, writes
from evo_agents.core_plugins.canvas.tools import STRATEGIST_INSTRUCTIONS
from evo_agents.hub import plugin_calls, tables, tables_canvas, tables_srs
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import agent_of, enable, granted, members, mention, running, tool  # noqa: F401
from tests.hub.test_chat_runs import chat_worker, created, ended, run_of
from tests.hub.test_plugin_points import tool_names
from tests.hub.test_plugin_runtimes import set_plugins
from tests.hub.test_runs import PROJECT

app_key = credentials_api.app_key  # a fixture
OWNER, OTHER, CHI, READER = credentials_api.OWNER, credentials_api.OTHER, "chi", "reader"
READ_ONLY = {"role": "reader", "max_level": "internal"}
CORE = {
    name: EntryPoint(name, f"evo_agents.core_plugins.{name}:MANIFEST", ENTRY_POINT_GROUP) for name in ("canvas", "srs")
}
BASE = f"/v1/x/canvas/{PROJECT}"
SLUG = "northwind-pantry"
CANVAS = f"{BASE}/canvases/{SLUG}"
TYPE = f"/v1/projects/{PROJECT}/agent-types/canvas/strategist"
TOOLS = [
    "canvas__edit",
    "canvas__evidence",
    "canvas__judge",
    "canvas__propose",
    "canvas__read",
    "canvas__signals",
    "canvas__undo",
]
CARD = {"hypothesis": "Corner shops reorder weekly", "test": "Interview ten shop owners", "deadline": "2026-12-01"}
SIGNAL_KEY = "pantry.reorder.weekly_share"


@pytest.fixture
def strategist_core(monkeypatch):
    """The entry points of the core plugins with canvas's and srs's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, s3, strategist_core):  # noqa: F811
    config = dataclasses.replace(credentials_api.hub_config(hub_db, tmp_path, github, app_key), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, members) -> dict:  # noqa: F811
    """``members`` with plugin canvas enabled, chi's canvas of Northwind Pantry and its first claim CL-1."""
    enable(client, members["owner"], "canvas")
    made = client.post(f"{BASE}/canvases", json={"slug": SLUG, "name": "Northwind Pantry"}, headers=members["chi"])
    assert made.status_code == 201, made.text
    edit(client, members["chi"], {"cell": "problem", "text": "Corner shops run out of stock between deliveries"})
    return members


def enable_strategist(client, hub, run_as: str = OWNER) -> dict:
    typed = client.put(TYPE, json={"run_as": run_as, "daily_usd": 5}, headers=hub["owner"])
    assert typed.status_code == 200, typed.text
    return typed.json()


def owners_worker(client, hub, name: str = "studio", slots: int = 3) -> dict:
    """A worker of owner that runs chat and task runs and builds the packs of plugin canvas."""
    worker = chat_worker(client, hub["owner"], name, slots=slots)
    assert set_plugins(client, hub["owner"], worker, ["canvas"]).status_code == 200
    return worker


def edit(client, headers, *changes: dict, expect: int = 200) -> dict:
    answer = client.post(f"{CANVAS}/edit", json={"changes": list(changes)}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def claim_of(client, headers, ref: str) -> dict:
    answer = client.get(f"{CANVAS}/claims/{ref}", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def listed(client, headers, *statuses: str) -> list[dict]:
    answer = client.get(f"{CANVAS}/proposals", params=[("status", s) for s in statuses], headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def decided(client, headers, proposal_id: int, verb: str = "accept", **body):
    return client.post(f"{CANVAS}/proposals/{proposal_id}/{verb}", json=body or None, headers=headers)


def chat_agent(client, hub, studio, text: str, *, by: str = "other", reply_to=None) -> tuple[dict, dict, dict]:
    """``by``'s mention of @strategist, its run claimed by ``studio`` and running: (message, spec, the agent)."""
    chat = created(client, hub[by], "Canvas", people=[OWNER, CHI], agents=("strategist",))
    message, found = mention(client, hub[by], chat["id"], text)
    assert found["refusal"] is None, found
    spec = running(client, studio, "chat")
    assert spec["id"] == found["run_id"]
    return {**message, "chat": chat}, spec, agent_of(studio, spec["id"])


def enums(schema) -> set:
    """Every value an enum of ``schema`` allows, at any depth."""
    if isinstance(schema, list):
        return set().union(*(enums(item) for item in schema)) if schema else set()
    if not isinstance(schema, dict):
        return set()
    return set(schema.get("enum") or ()) | enums([value for key, value in schema.items() if key != "enum"])


# The agent type and its tools


def test_strategist_type_makes_its_profile_with_the_pack_and_its_tools_show_only_where_canvas_is_on(client, members):
    session = {"project": PROJECT}
    assert not [name for name in tool_names(client, members["other"], **session) if name.startswith("canvas__")]
    assert client.put(TYPE, json={"run_as": OWNER, "daily_usd": 5}, headers=members["owner"]).status_code == 409
    enable(client, members["owner"], "canvas")
    names = tool_names(client, members["other"], **session)
    assert sorted(name for name in names if name.startswith("canvas__")) == TOOLS
    assert not [name for name in names if name.startswith("srs__")], "srs is not enabled"
    typed = enable_strategist(client, members)
    assert (typed["handle"], typed["enabled"], typed["run_as"], typed["packs"]) == (
        "strategist",
        True,
        OWNER,
        ["canvas/strategist"],
    )
    made = client.get(f"/v1/projects/{PROJECT}/agents/strategist", headers=members["reader"]).json()
    assert (made["name"], made["instructions"]) == ("Strategist", STRATEGIST_INSTRUCTIONS)
    assert "never move a claim to validated and never approve a baseline" in made["instructions"]
    # it reads the SRS with the tools of plugin srs, where the project keeps it
    enable(client, members["owner"], "srs")
    assert {"srs__read", "srs__search"} <= set(tool_names(client, members["other"], **session))
    # a member's own session reads, and never edits, undoes, proposes or adds evidence
    client.post(f"{BASE}/canvases", json={"slug": SLUG, "name": "Northwind Pantry"}, headers=members["chi"])
    text, error = tool(client, members["other"], "canvas__read", PROJECT)
    assert not error and f"- {SLUG}: Northwind Pantry; no claim" in text, text
    for name, arguments in (
        ("canvas__edit", {"canvas": SLUG, "changes": [{"cell": "problem", "text": "x"}], "reason": "x"}),
        ("canvas__undo", {"canvas": SLUG}),
        (
            "canvas__propose",
            {"canvas": SLUG, "proposals": [{"cell": "problem", "text": "x"}], "reason": "x", "synthetic": False},
        ),
        (
            "canvas__evidence",
            {
                "canvas": SLUG,
                "claim": "CL-1",
                "kind": "interview",
                "direction": "supports",
                "summary": "x",
                "synthetic": False,
            },
        ),
    ):
        text, error = tool(client, members["other"], name, PROJECT, **arguments)
        assert error and f"calls {name}" in text and "only the agent of" in text, text


def test_strategist_tool_schemas_copy_the_plugins_names_and_leave_validated_out():
    assert tools.CELLS == tables_canvas.CELLS
    assert set(tools.AGENT_STATUSES) == set(tables_canvas.STATUSES) - {"validated"}
    assert (tools.CONFIDENCES, tools.DIRECTIONS) == (tables_canvas.CONFIDENCES, tables_canvas.DIRECTIONS)
    assert (tools.AGGREGATES, tools.COMPARATORS) == (content.AGGREGATES, content.COMPARATORS)
    assert set(tools.AGENT_EVIDENCE_KINDS) < set(writes.ADDED_KINDS)
    assert (tools.MAX_TEXT_CHARS, tools.MAX_REASON_CHARS) == (
        tables_canvas.MAX_TEXT_CHARS,
        tables_canvas.MAX_REASON_CHARS,
    )
    assert (tools.MAX_CHANGES, tools.MAX_PROPOSED) == (writes.MAX_CHANGES, proposals.MAX_PROPOSED)
    assert (tools.MAX_LINKS, tools.MAX_CODE_CHARS) == (content.MAX_LINKS, tables_srs.MAX_CODE_CHARS)
    assert set(tools.TEST_CARD["properties"]) == set(content.CARD_FIELDS)
    for _, _, _, schema in tools.TOOLS:
        assert "validated" not in enums(schema), "no tool of @strategist takes the status validated"


# Edits in chat


def test_strategist_applies_a_writers_command_at_once_as_theirs_and_refuses_a_claim_changed_since(client, hub, hub_db):
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    message, spec, agent = chat_agent(client, hub, studio, "@strategist sharpen the problem and add a segment")
    assert (spec["profile"]["handle"], [pack["pack"] for pack in spec["packs"]]) == (
        "strategist",
        ["canvas/strategist"],
    )
    assert run_of(client, hub["chi"], spec["id"])["dispatched_by"] == OWNER, "it runs on a worker of run_as"
    assert sorted(name for name in tool_names(client, agent) if name.startswith("canvas__")) == TOOLS
    text, error = tool(client, agent, "canvas__read", canvas=SLUG)
    assert not error and "- CL-1 [assumption, medium, rev 1] Corner shops run out of stock" in text, text
    segment = {"cell": "customer_segments", "text": "Corner shops with one owner", "test_card": CARD}
    sharper = {"claim": "CL-1", "base_rev": 1, "text": "Corner shops run out of milk twice a week"}
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[sharper, segment], reason="as asked")
    assert not error and text.startswith(f"Written at once as {OTHER}'s, by run #{spec['id']}:"), text
    assert "- CL-1 [assumption] rev 2: Corner shops run out of milk" in text and "- CL-2 [assumption] rev 1" in text
    rev = claim_of(client, hub["reader"], "CL-1")["revs"][-1]
    assert (rev["change"], rev["reason"], rev["provenance"]) == (
        "edit",
        "as asked",
        {
            "actor": "agent",
            "login": OTHER,
            "run_id": spec["id"],
            "runtime": run_of(client, hub["chi"], spec["id"])["runtime"],
            "model": None,
            "chat_message_id": message["id"],
        },
    )
    made = claim_of(client, hub["reader"], "CL-2")
    assert (made["status"], made["test_card"]["hypothesis"]) == ("assumption", CARD["hypothesis"])
    # a new claim starts as an assumption; validated is no status of the tool's, and the hub refuses it anyway
    testing = {**segment, "status": "testing"}
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[testing], reason="x")
    assert error and "starts as an assumption" in text, text
    validated = {"claim": "CL-1", "base_rev": 2, "status": "validated"}
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[validated], reason="x")
    assert error and text.startswith("error: bad arguments for canvas__edit"), text
    # someone else changed CL-1 since the agent read it: the whole edit is refused with what changed
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "confidence": "high"})
    stale = [
        {"claim": "CL-1", "base_rev": 2, "text": "Shops run out of milk"},
        {"claim": "CL-2", "base_rev": 1, "confidence": "low"},
    ]
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=stale, reason="x")
    assert error and "CL-1 changed since you read them: nothing was written" in text, text
    assert f"- CL-1 is at rev 3, changed by {CHI}; you read rev 2: confidence: 'medium' -> 'high'" in text, text
    assert claim_of(client, hub["reader"], "CL-2")["rev"] == 1
    # the run's token reaches none of the routes: no accept, no confirm, no edit through the API
    assert client.post(f"{CANVAS}/edit", json={"changes": [sharper]}, headers=agent).status_code in (401, 403)
    # a writer who lost writer has their command refused
    granted(client, hub, OTHER, READ_ONLY)
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[segment], reason="x")
    assert error and "needs the writer role" in text, text
    r = tables.canvas.revs
    assert sql(hub_db, select(r.c.actor).where(r.c.run_id == spec["id"]).order_by(r.c.id)) == [("agent",), ("agent",)]


def test_strategist_undo_takes_back_the_writers_last_command_then_the_one_before(client, hub):
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    first, spec, agent = chat_agent(client, hub, studio, "@strategist add a segment and sharpen CL-1")
    segment = {"cell": "customer_segments", "text": "Corner shops with one owner", "test_card": CARD}
    sharper = {"claim": "CL-1", "base_rev": 1, "text": "Corner shops run out of milk twice a week"}
    tool(client, agent, "canvas__edit", canvas=SLUG, changes=[segment, sharper], reason="first command")
    again = {"claim": "CL-1", "base_rev": 2, "confidence": "low"}
    tool(client, agent, "canvas__edit", canvas=SLUG, changes=[again], reason="first command, once more")
    ended(client, studio, spec["id"], usage={"total_cost_usd": 0.1})
    # the second command, in the same thread: a new run of the session, started by its own message
    second, found = mention(client, hub["other"], first["chat"]["id"], "@strategist mark CL-1 retired", first["id"])
    spec2 = running(client, studio, "chat")
    agent2 = agent_of(studio, spec2["id"])
    text, error = tool(
        client,
        agent2,
        "canvas__edit",
        canvas=SLUG,
        changes=[{"claim": "CL-1", "base_rev": 3, "status": "retired"}],
        reason="second",
    )
    assert not error, text
    # undo: the second command goes; undo again: the first, back to before its first rev of each claim
    text, error = tool(client, agent2, "canvas__undo", canvas=SLUG)
    assert not error and f"Took back the command of chat message {second['id']}" in text, text
    assert "- CL-1: back to rev 3, as rev 5" in text, text
    assert claim_of(client, hub["reader"], "CL-1")["status"] == "assumption"
    text, error = tool(client, agent2, "canvas__undo", canvas=SLUG)
    assert not error and f"Took back the command of chat message {first['id']}" in text, text
    assert "- CL-1: back to rev 1, as rev 6" in text and "- CL-2: archived, as rev 2" in text, text
    one = claim_of(client, hub["reader"], "CL-1")
    assert (one["text"], one["confidence"], one["revs"][-1]["undo_of"]) == (
        "Corner shops run out of stock between deliveries",
        "medium",
        2,
    )
    assert claim_of(client, hub["reader"], "CL-2")["archived"]
    text, error = tool(client, agent2, "canvas__undo", canvas=SLUG)
    assert error and "nothing to undo" in text, text
    # a claim changed after the command refuses its undo whole
    tool(
        client,
        agent2,
        "canvas__edit",
        canvas=SLUG,
        changes=[{"claim": "CL-1", "base_rev": 6, "text": "Out of milk"}],
        reason="third",
    )
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 7, "confidence": "high"})
    text, error = tool(client, agent2, "canvas__undo", canvas=SLUG)
    assert error and "CL-1 changed since the command" in text and f"changed by {CHI}" in text, text
    # an undo of a rev named, as the API's
    text, error = tool(client, agent2, "canvas__undo", canvas=SLUG, claims=[{"claim": "CL-1", "rev": 8}])
    assert not error and "- CL-1 [assumption] rev 9" in text, text


def test_strategist_never_moves_a_claim_to_validated_even_by_undo(client, hub):
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    interview = {"kind": "interview", "direction": "supports", "summary": "Eight of ten owners reorder on Mondays"}
    added = client.post(f"{CANVAS}/claims/CL-1/evidence", json=interview, headers=hub["chi"])
    assert added.status_code == 201, added.text
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "status": "validated"})
    _, _, agent = chat_agent(client, hub, studio, "@strategist set CL-1 back to testing")
    card = {"claim": "CL-1", "base_rev": 2, "status": "invalidated"}
    text, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[card], reason="as asked")
    assert not error, text
    text, error = tool(client, agent, "canvas__undo", canvas=SLUG)
    assert error and "an agent never moves CL-1 to validated, even when a member asks" in text, text
    assert claim_of(client, hub["reader"], "CL-1")["status"] == "invalidated"
    # the member does it themselves
    undone = client.post(f"{CANVAS}/undo", json={"revs": [{"claim": "CL-1", "rev": 3}]}, headers=hub["other"])
    assert undone.status_code == 200 and undone.json()["claims"][0]["status"] == "validated", undone.text


# A brainstorm proposes


def test_strategist_brainstorm_is_a_task_run_that_proposes_assumptions_with_test_cards(client, hub, hub_db):
    refused = client.post(f"{CANVAS}/brainstorm", json={}, headers=hub["chi"])
    assert refused.status_code == 409 and "no brainstorm of canvas" in refused.json()["message"], refused.text
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    assert client.post(f"{CANVAS}/brainstorm", json={}, headers=hub["reader"]).status_code == 403
    asked = client.post(
        f"{CANVAS}/brainstorm", json={"focus": "who pays", "cells": ["revenue_streams"]}, headers=hub["chi"]
    )
    assert asked.status_code == 202, asked.text
    run_id = asked.json()["run_id"]
    assert asked.json()["title"] == f"@strategist brainstorms {SLUG}"
    run = run_of(client, hub["reader"], run_id)
    assert (run["kind"], run["profile"], run["dispatched_by"]) == ("task", "strategist", OWNER)
    spec = running(client, studio, "task")
    assert spec["id"] == run_id and [pack["pack"] for pack in spec["packs"]] == ["canvas/strategist"]
    for said in (
        f"Brainstorm the Lean Canvas {SLUG}",
        f"which {CHI} asked for",
        "the cells revenue_streams",
        "who pays",
        "Follow the skill lean-canvas",
        "canvas__propose",
        "You change no claim",
    ):
        assert said in spec["prompt"], said
    agent = agent_of(studio, run_id)
    # a task run changes no claim
    text, error = tool(
        client, agent, "canvas__edit", canvas=SLUG, changes=[{"cell": "problem", "text": "x"}], reason="x"
    )
    assert error and "only the agent of a chat run of @strategist" in text, text
    # a proposed claim carries a test card; validated is not proposed
    bare = {"cell": "revenue_streams", "text": "Shops pay a monthly fee"}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[bare], reason="x", synthetic=False)
    assert error and "carries a test card with at least a hypothesis and a test" in text, text
    up = {"claim": "CL-1", "base_rev": 1, "status": "validated"}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[up], reason="x", synthetic=False)
    assert error and text.startswith("error: bad arguments for canvas__propose"), text
    fee = {**bare, "confidence": "low", "test_card": {**CARD, "hypothesis": "Shops pay 20 USD a month"}}
    persona = {"cell": "channels", "text": "Owners hear of it from their wholesaler", "test_card": CARD}
    tighter = {"claim": "CL-1", "base_rev": 1, "confidence": "high"}
    text, error = tool(
        client,
        agent,
        "canvas__propose",
        canvas=SLUG,
        proposals=[fee, persona, tighter],
        reason="the riskiest guesses of revenue and channels",
        synthetic=True,
    )
    assert not error and text.startswith(f"3 proposal(s) of canvas {SLUG}, open"), text
    assert "Nothing changed yet" in text
    shown = client.get(CANVAS, headers=hub["reader"]).json()
    assert shown["claims"] == 1, "a proposal changes nothing"
    opened = listed(client, hub["reader"])
    assert [(p["kind"], p["claim"], p["base_rev"], p["synthetic"], p["status"]) for p in opened] == [
        ("change", "CL-1", 1, True, "open"),
        ("create", None, None, True, "open"),
        ("create", None, None, True, "open"),
    ]
    assert opened[0]["provenance"] | {"runtime": None} == {
        "actor": "agent",
        "login": OWNER,
        "run_id": run_id,
        "runtime": None,
        "model": None,
        "chat_message_id": None,
    }
    assert {"field": "confidence", "before": "medium", "after": "high"} in opened[0]["changes"]
    assert {"field": "test_card.hypothesis", "before": None, "after": "Shops pay 20 USD a month"} in opened[2][
        "changes"
    ]
    # the agent's token decides nothing; a reader neither
    assert decided(client, agent, opened[2]["id"]).status_code in (401, 403)
    assert decided(client, hub["reader"], opened[2]["id"]).status_code == 403
    # a writer accepts a new claim: an assumption at rev 1, written as theirs
    accepted = decided(client, hub["other"], opened[2]["id"])
    assert accepted.status_code == 200, accepted.text
    assert (accepted.json()["status"], accepted.json()["claim"], accepted.json()["rev"]) == ("accepted", "CL-2", 1)
    fee_claim = claim_of(client, hub["reader"], "CL-2")
    assert (fee_claim["status"], fee_claim["cell"], fee_claim["test_card"]["hypothesis"]) == (
        "assumption",
        "revenue_streams",
        "Shops pay 20 USD a month",
    )
    first = fee_claim["revs"][0]
    assert (first["change"], first["provenance"]["actor"], first["provenance"]["login"]) == (
        "proposal",
        "person",
        OTHER,
    )
    assert first["reason"].startswith(f"proposal {opened[2]['id']}: the riskiest guesses")
    again = decided(client, hub["chi"], opened[2]["id"])
    assert again.status_code == 409 and "is accepted" in again.json()["message"], again.text
    # rejected, with why
    rejected = decided(client, hub["chi"], opened[1]["id"], "reject", reason="wholesalers do not talk to them")
    assert (rejected.json()["status"], rejected.json()["decided_by"], rejected.json()["decided_reason"]) == (
        "rejected",
        CHI,
        "wholesalers do not talk to them",
    )
    # the claim of the change moved since: the proposal is stale and accepted no more
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 1, "text": "Shops run out of milk"})
    (stale,) = listed(client, hub["reader"], "stale")
    assert stale["id"] == opened[0]["id"]
    refused = decided(client, hub["other"], stale["id"])
    assert refused.status_code == 409 and "is stale" in refused.json()["message"], refused.text
    assert listed(client, hub["reader"]) == []
    assert [p["status"] for p in listed(client, hub["reader"], "open", "accepted", "rejected", "stale")] == [
        "stale",
        "rejected",
        "accepted",
    ]
    p = tables.canvas.proposals
    assert sql(hub_db, select(p.c.label).where(p.c.id == stale["id"]))[0][0] == shown["label"]


def test_strategist_accepting_a_proposal_keeps_the_rules_of_every_edit(client, hub, hub_db):
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    _, _, agent = chat_agent(client, hub, studio, "@strategist propose CL-1 to testing")
    testing = {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": {"hypothesis": "Weekly"}}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[testing], reason="x", synthetic=False)
    assert error and "goes to testing only with a test card that holds a hypothesis, a test and a deadline" in text
    testing["test_card"] = CARD
    text, error = tool(
        client, agent, "canvas__propose", canvas=SLUG, proposals=[testing], reason="ready", synthetic=False
    )
    assert not error, text
    (made,) = listed(client, hub["reader"])
    assert decided(client, hub["chi"], made["id"]).json()["rev"] == 2
    assert claim_of(client, hub["reader"], "CL-1")["status"] == "testing"


# Signals and evidence


def test_strategist_finds_signals_and_adds_evidence_that_counts_once_a_writer_confirms_it(client, hub):
    key = {"name": SIGNAL_KEY, "kind": "number", "unit": "ratio", "description": "shops that reorder weekly"}
    assert client.post(f"/v1/projects/{PROJECT}/signal-keys", json=key, headers=hub["owner"]).status_code == 201
    items = [{"key": SIGNAL_KEY, "value": value, "tenant": "northwind"} for value in (0.25, 0.4)]
    pushed = client.post(f"/v1/signals/{PROJECT}", json={"items": items}, headers=hub["chi"])
    assert pushed.status_code == 202, pushed.text
    enable_strategist(client, hub)
    studio = owners_worker(client, hub)
    _, spec, agent = chat_agent(client, hub, studio, "@strategist what do the signals say?")
    text, error = tool(client, agent, "canvas__signals")
    assert not error and f"- {SIGNAL_KEY} (number, ratio): shops that reorder weekly; latest " in text, text
    text, error = tool(client, agent, "canvas__signals", key=SIGNAL_KEY, days=7)
    assert not error and text.startswith(f"{SIGNAL_KEY}, the last 7 days, newest first (2):"), text
    assert "(tenant northwind)" in text
    text, error = tool(client, agent, "canvas__signals", key="pantry.reorder.unknown")
    assert not error and "No value of pantry.reorder.unknown" in text, text
    # a simulation is synthetic whatever the agent says, and never real
    simulated = {"canvas": SLUG, "claim": "CL-1", "direction": "supports", "synthetic": False}
    text, error = tool(client, agent, "canvas__evidence", kind="simulation", summary="Five personas agree", **simulated)
    assert not error and "simulation, supports, synthetic" in text and "and never, being synthetic" in text, text
    text, error = tool(client, agent, "canvas__evidence", kind="experiment", summary="A/B test", **simulated)
    assert error and text.startswith("error: bad arguments for canvas__evidence"), text
    text, error = tool(
        client,
        agent,
        "canvas__evidence",
        kind="interview",
        summary="Shop owner, 12 Oct: reorders every Monday",
        url="https://notes.example.org/i/12",
        observed_on="2026-10-12",
        **simulated,
    )
    assert not error and "only once a writer confirms it." in text, text
    pieces = claim_of(client, hub["reader"], "CL-1")["evidence"]
    assert [
        (e["kind"], e["synthetic"], e["real"], e["provenance"]["actor"], e["provenance"]["run_id"]) for e in pieces
    ] == [("simulation", True, False, "agent", spec["id"]), ("interview", False, False, "agent", spec["id"])]
    assert pieces[1]["not_real_because"] == "an agent added it and no writer confirmed it"
    confirmed = client.post(f"{CANVAS}/claims/CL-1/evidence/{pieces[1]['id']}/confirm", headers=hub["chi"])
    assert confirmed.status_code == 200 and confirmed.json()["real"], confirmed.text

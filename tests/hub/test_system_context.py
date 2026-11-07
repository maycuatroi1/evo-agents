"""What an agent of an agent system knows of it (``evo_agents.hub.system_context``): the System section of its prompt,
the brief of an ask, the refusal off its interacts edges, the budget of a trace, the metrics of a trace, and the prompt
budget that holds them all (criterion a4 of plan multi-agent-systems)."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import chat_runs, system_context, system_spec, tables_traces
from evo_agents.hub.chat_runs import MAX_PROMPT_BYTES

SPEC = {
    "schema": system_spec.SCHEMA,
    "name": "Northwind support",
    "purpose": "Answer the customers of Northwind.",
    "nodes": [
        {"id": "in", "kind": "entry"},
        {"id": "triage", "kind": "agent", "profile": "lumen-triage"},
        {"id": "billing", "kind": "agent", "profile": "lumen-billing"},
        {"id": "docs", "kind": "kg", "graph": "docs"},
        {"id": "lead", "kind": "orchestrator", "profile": "lumen-lead"},
    ],
    "edges": [
        {"from": "in", "to": "triage", "kind": "interacts", "description": "every question"},
        {"from": "triage", "to": "billing", "kind": "interacts", "description": "invoices"},
        {"from": "triage", "to": "docs", "kind": "uses_kg", "description": "product docs"},
        {"from": "lead", "to": "billing", "kind": "orchestrates", "description": ""},
        {"from": "billing", "to": "lead", "kind": "escalates", "description": "refunds over 100 USD"},
    ],
}


def checked(spec: dict) -> dict:
    found, problems = system_spec.check(spec)
    assert system_spec.errors(problems) == []
    return found


def test_the_span_names_of_the_table_are_those_of_the_module():
    assert tables_traces.SPAN_NAMES == system_context.SPAN_NAMES == ("invoke_agent", "execute_tool")
    assert tables_traces.SPAN_STATUSES == system_context.SPAN_STATUSES


def test_the_system_section_names_purpose_edges_in_and_out_graphs_and_the_orchestrator():
    spec = checked(SPEC)
    triage = "\n".join(system_context.system_lines(system_context.place_of("support", spec, "lumen-triage")))
    assert "# Your agent system: Northwind support (support)" in triage
    assert "Its purpose: Answer the customers of Northwind." in triage
    assert "Agents you hand work to (with `evo-agents worker agent ask`):\n- @lumen-billing: invoices" in triage
    assert "Who hands you work:\n- the people who write to the system: every question" in triage
    assert "read them when you name no graph):\n- docs: product docs" in triage
    assert "`--goal`" in triage and "`--output`" in triage and "`--done-when`" in triage
    assert "`--reason WHY`" in triage and "evo-agents worker agent forward --run RUN" in triage
    assert "at most 12 runs and 2 USD" in triage
    billing = "\n".join(system_context.system_lines(system_context.place_of("support", spec, "lumen-billing")))
    assert "Who hands you work:\n- @lumen-triage: invoices" in billing
    assert "The Orchestrator you escalate to: @lumen-lead: refunds over 100 USD" in billing
    assert "Agents you hand work to" not in billing and "Knowledge graphs" not in billing
    assert system_context.place_of("support", spec, "lumen-other") is None


def test_the_helpers_read_edges_graphs_orchestrator_and_design():
    spec = checked(SPEC)
    assert system_context.edge_targets(spec, "lumen-triage") == ["lumen-billing"]
    assert system_context.edge_targets(spec, "lumen-billing") == []
    assert system_context.kg_graphs(spec, "lumen-triage") == ["docs"]
    assert system_context.orchestrator_of(spec, "lumen-billing") == ("lumen-lead", "refunds over 100 USD")
    assert system_context.on_design(spec, "in", "triage") and system_context.on_design(spec, "triage", "billing")
    assert not system_context.on_design(spec, "billing", "triage")
    assert not system_context.on_design(spec, "triage", "@lumen-outside")
    assert system_context.external_node("lumen-outside") == "@lumen-outside"
    assert system_context.entry_node(spec) == "in"


def test_the_system_section_keeps_to_its_byte_budget():
    many = [
        {"id": f"a{i}", "kind": "agent", "profile": f"lumen-agent-{i}"} for i in range(40)
    ]  # every edge with the longest description a spec takes
    spec = {
        **SPEC,
        "purpose": "p" * 2000,
        "nodes": [*SPEC["nodes"], *many],
        "edges": [
            *SPEC["edges"],
            *({"from": "triage", "to": node["id"], "kind": "interacts", "description": "d" * 500} for node in many),
        ],
    }
    lines = system_context.system_lines(system_context.Place("support", spec, "lumen-triage", "triage"))
    text = "\n".join(lines)
    assert len(text.encode()) <= system_context.SYSTEM_BYTES
    assert text.endswith(system_context.CUT.strip())


def test_a_brief_heads_the_prompt_with_the_fields_given_and_says_which_were():
    brief = {"goal": " Find the invoice ", "done_when": "the invoice id is known", "refs": ["northwind:billing.py", ""]}
    assert system_context.clean_brief(brief) == {
        "goal": "Find the invoice",
        "done_when": "the invoice id is known",
        "refs": ["northwind:billing.py"],
    }
    assert system_context.brief_present(brief) == ["goal", "done_when", "refs"]
    lines = system_context.brief_lines(brief, "lumen-triage", 41)
    assert lines[0] == "# Your brief from @lumen-triage (run #41)"
    assert "Goal: Find the invoice" in lines and "Done when: the invoice id is known" in lines
    assert "Refs: northwind:billing.py" in lines and not any(line.startswith("Output:") for line in lines)
    assert system_context.brief_lines(None) == [] and system_context.brief_lines({"reason": "why"}) == []
    assert system_context.brief_text(brief) == "Goal: Find the invoice\nDone when: the invoice id is known\nRefs: " + (
        "northwind:billing.py"
    )
    huge = {field: "x" * 5000 for field in system_context.BRIEF_FIELDS} | {"refs": ["r" * 900] * 30}
    assert len("\n".join(system_context.brief_lines(huge, "a" * 63, 1)).encode()) <= system_context.BRIEF_BYTES


def test_the_refusal_off_the_edges_lists_the_targets_and_how_to_give_a_reason():
    refusal = system_context.off_edge_refusal("lumen-triage", "lumen-lead", ["lumen-billing"])
    assert "@lumen-lead is off the interacts edges of @lumen-triage" in refusal
    assert "lead to @lumen-billing" in refusal and "`--reason WHY`" in refusal
    assert "lead to no agent" in system_context.off_edge_refusal("lumen-billing", "lumen-triage", [])


def test_the_budget_of_a_trace_is_its_runs_and_its_cost():
    spec = checked(SPEC)
    assert system_context.limits_of(spec) == (2.0, 12)
    assert system_context.budget_problem(spec, 11, 1.99) is None
    assert "budget of 12 runs (limits.trace_runs" in system_context.budget_problem(spec, 12, 0)
    assert "budget of 2 USD (limits.trace_usd" in system_context.budget_problem(spec, 1, 2.0)
    tight = checked({**SPEC, "limits": {"trace_runs": 2, "trace_usd": 0.5}})
    assert system_context.budget_problem(tight, 2, 0) is not None
    assert system_context.budget_problem(tight, 1, 0.6) is not None


def test_metrics_count_nodes_and_edges_and_leave_resumes_off_the_edges():
    spans = [
        {"node": "triage", "from_node": "in", "status": "ok", "seconds": 4.0, "started_at": "1", "usd": 0.1},
        {"node": "billing", "from_node": "triage", "status": "error", "seconds": 2.0, "started_at": "2"},
        {"node": "billing", "from_node": "triage", "status": "ok", "seconds": 6.0, "started_at": "3", "resume": True},
        {
            "node": "@lumen-outside",
            "from_node": "billing",
            "status": "running",
            "off_design": True,
            "seconds": None,
            "started_at": "4",
            "input_tokens": 10,
            "output_tokens": 5,
        },
    ]
    found = system_context.metrics(spans)
    nodes = {item["node"]: item for item in found["nodes"]}
    assert nodes["billing"]["count"] == 2 and nodes["billing"]["errors"] == 1
    assert nodes["billing"]["p50_seconds"] == 4.0 and nodes["billing"]["last_seconds"] == 6.0
    assert nodes["@lumen-outside"]["off_design"] == 1 and nodes["@lumen-outside"]["input_tokens"] == 10
    edges = {(item["from_node"], item["to_node"]): item for item in found["edges"]}
    assert edges[("triage", "billing")]["count"] == 1, "the resumed turn crosses no edge"
    assert edges[("billing", "@lumen-outside")]["off_design"] == 1
    assert edges[("in", "triage")]["usd"] == 0.1


def test_the_largest_prompt_keeps_rules_brief_and_system_whole_under_the_prompt_budget():
    many = [{"id": f"a{i}", "kind": "agent", "profile": f"lumen-agent-{i}"} for i in range(40)]
    spec = {
        **SPEC,
        "name": "n" * 200,
        "purpose": "p" * 2000,
        "nodes": [*SPEC["nodes"], *many],
        "edges": [
            *SPEC["edges"],
            *({"from": "triage", "to": n["id"], "kind": "interacts", "description": "d" * 500} for n in many),
        ],
    }
    system = system_context.system_lines(system_context.Place("support", spec, "lumen-triage", "triage"))
    huge = {field: "x" * 5000 for field in system_context.BRIEF_FIELDS} | {"refs": ["r" * 900] * 30}
    brief = system_context.brief_lines(huge, "a" * 63, 10**12)
    listing = [
        {"repo": f"repo-{i:03d}-" + "r" * 40, "open": i < 3, "checkout": True, "named": False} for i in range(200)
    ]
    body = "m" * chat_runs.MAX_MESSAGE_BYTES
    prompt = chat_runs.build_chat_prompt(
        agent={"handle": "h" * 63, "name": "N" * 200, "project": "p" * 63, "description": "d" * 1000},
        chat={"id": 10**12, "title": "t" * 200, "people": ["u" * 39] * 20, "agents": ["a" * 63] * 20},
        trigger={"id": 1, "author": "owner", "at": "2026-11-07 08:00 UTC", "body": body},
        login="u" * 39,
        hop=chat_runs.MAX_HOPS,
        branch="chat/" + "b" * 60,
        blocks=["[#2] owner:\n" + "s" * 4000] * 10,
        listing=listing,
        worktrees={},
        harness="northwind-harness",
        ships=True,
        label={"level": "internal", "default": "confidential"},
        cdn=True,
        system=system,
        brief=brief,
    )
    assert len(prompt.encode()) <= MAX_PROMPT_BYTES
    head = prompt.split("\nPeople of the chat:")[0]
    for line in (*brief, *system):
        assert line in head, f"left out of the prompt: {line[:60]!r}"
    rules = chat_runs._rules(
        "h" * 63, "u" * 39, "chat/" + "b" * 60, chat_runs.MAX_HOPS, True, "northwind-harness", ships=True, cdn=True
    )
    for line in rules:
        assert line in head, f"a rule left out: {line[:60]!r}"
    assert head.index(brief[0]) < head.index(rules[0]) < head.index(system[0])
    assert len(head.encode()) < MAX_PROMPT_BYTES - 4096, "the snapshot and the message keep room"

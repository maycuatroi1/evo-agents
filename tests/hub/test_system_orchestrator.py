"""The Orchestrator of an agent system (criterion a7 of plan multi-agent-systems): an agent an Orchestrator
orchestrates opens a thread to it in the system's ops chat when its run or turn fails, goes stale or escalates, or when
a rule of progress (loop, idle, rounds, budget) holds, naming the rule and the spans that show it; the Orchestrator
answers, gives the work to another agent of the system or asks the owner, each with a ``Decision:`` line and an audit
row. ``evo-agents worker escalate --question`` asks it and the run waits on it; an agent of no Orchestrator keeps the
triage it had (a2).
"""

from __future__ import annotations

import argparse

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import system_context, tables_orchestrator, triage_rules
from evo_agents.worker import escalate_cli, triage_cli

ORCHESTRATED = {
    "nodes": [
        {"id": "in", "kind": "entry"},
        {"id": "lead", "kind": "orchestrator", "profile": "lead"},
        {"id": "review", "kind": "agent", "profile": "reviewer"},
        {"id": "fix", "kind": "agent", "profile": "fixer"},
        {"id": "docs", "kind": "agent", "profile": "writer"},
    ],
    "edges": [
        {"from": "in", "to": "review", "kind": "interacts"},
        {"from": "lead", "to": "review", "kind": "orchestrates"},
        {"from": "docs", "to": "lead", "kind": "escalates"},
    ],
}


def test_the_orchestrator_of_an_agent_is_its_orchestrates_edge_else_its_escalates_edge():
    assert system_context.lead_of(ORCHESTRATED, "reviewer") == "lead"
    assert system_context.lead_of(ORCHESTRATED, "writer") is None, "escalates is no orchestrates edge"
    assert system_context.escalation_target(ORCHESTRATED, "writer") == "lead"
    assert system_context.escalation_target(ORCHESTRATED, "fixer") is None
    assert system_context.lead_of(ORCHESTRATED, "stranger") is None
    assert [system_context.node_kind(ORCHESTRATED, h) for h in ("lead", "fixer", "nobody")] == [
        "orchestrator",
        "agent",
        None,
    ]


def test_the_rules_actions_and_the_table_agree():
    assert tables_orchestrator.RULES == triage_rules.ORCHESTRATOR_RULES
    assert tables_orchestrator.OUTCOMES == triage_rules.ORCHESTRATOR_ACTIONS
    assert set(triage_rules.RULE_TEXT) == set(triage_rules.ORCHESTRATOR_RULES)
    from importlib import import_module

    migration = import_module("evo_agents.hub.migrations.versions.0238_ops_threads")
    assert (
        migration.RULES == triage_rules.ORCHESTRATOR_RULES and migration.OUTCOMES == triage_rules.ORCHESTRATOR_ACTIONS
    )


def test_the_loop_rule_matches_the_same_work_three_times_by_text_or_goal():
    assert triage_rules.normalized("Fix run #41, the retry loop!") == "fix run the retry loop"
    same = [(1, "fix the retry loop (run 3)", None), (2, "other work", None), (3, "Fix the retry loop, run #9", None)]
    assert triage_rules.loop_of(same) == [], "twice is no loop"
    assert triage_rules.loop_of([*same, (4, "fix the retry loop: run 12", None)]) == [1, 3, 4]
    by_goal = [(1, "a", "green test"), (2, "b", "Green test."), (3, "c", "green test")]
    assert triage_rules.loop_of(by_goal) == [1, 2, 3]
    assert triage_rules.loop_of([]) == []
    assert triage_rules.loop_of([(1, "", None), (2, "", None), (3, None, None)]) == [], "no text is no work"


def test_the_idle_limit_is_the_specs_else_ten_minutes_on_the_hub_and_thirty_on_a_worker():
    assert triage_rules.idle_minutes(None, "hub") == 10
    assert triage_rules.idle_minutes({}, "claude-code") == 30
    limits = {"idle_minutes_hub": 3, "idle_minutes_worker": 45}
    assert (triage_rules.idle_minutes(limits, "hub"), triage_rules.idle_minutes(limits, "codex")) == (3, 45)
    assert triage_rules.idle_minutes({"idle_minutes_hub": True}, "hub") == 10


def test_work_goes_only_to_another_agent_node_of_the_system():
    assert triage_rules.reassign_problem("agent", "fixer", "reviewer") is None
    assert "no agent of this system" in triage_rules.reassign_problem(None, "stranger", "reviewer")
    assert "not an agent node" in triage_rules.reassign_problem("orchestrator", "lead", "reviewer")
    assert "answer it instead" in triage_rules.reassign_problem("agent", "reviewer", "reviewer")


def test_reassign_refuses_an_agent_whose_run_waits_on_the_asker():
    assert triage_rules.reassign_problem("agent", "fixer", "reviewer", {"writer"}) is None
    assert "a cycle" in triage_rules.reassign_problem("agent", "fixer", "reviewer", {"fixer"})


def worker_args(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evo-agents worker")
    sub = parser.add_subparsers(dest="command")
    escalate_cli.register_escalate(sub)
    triage_cli.register_triage(sub)
    return parser.parse_args(list(argv))


def test_escalate_takes_a_question_and_sends_only_the_brief_fields_given():
    args = worker_args("escalate", "--question", "which retry policy wins?", "--goal", "one policy", "--ref", "run:4")
    assert args.func is escalate_cli.cmd_escalate
    assert escalate_cli.payload(args, args.question) == {
        "question": "which retry policy wins?",
        "goal": "one policy",
        "refs": ["run:4"],
    }


def test_the_orchestrator_commands_parse():
    answer = worker_args("triage", "answer", "--text", "use backoff", "--reason", "the spec says so")
    assert answer.func is triage_cli.cmd_answer
    reassign = worker_args("triage", "reassign", "@fixer", "--note", "fix it", "--reason", "fixer owns it")
    assert (reassign.func, reassign.handle) == (triage_cli.cmd_reassign, "@fixer")
    ask = worker_args(
        "triage", "ask-owner", "--question", "ship?", "--option", "y=Yes", "--option", "n=No", "--reason", "r"
    )
    assert (ask.func, ask.option) == (triage_cli.cmd_ask_owner, ["y=Yes", "n=No"])


def test_the_hub_runtime_has_the_escalate_and_orchestrator_tools():
    from evo_agents.hub.server import hub_tools

    for name in ("escalate", "orchestrator_answer", "orchestrator_reassign", "orchestrator_ask_owner"):
        assert name in hub_tools.NAMES


# On the hub

from tests.hub import pg  # noqa: E402

if pg.DSN:
    from datetime import timedelta

    from sqlalchemy import select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server import orchestrator
    from tests.hub.account_pool_setup import give_account_pool
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        OWNER,
        agent,
        app_key,
        chat_worker,
        claim,
        client,
        created,
        hub,
        in_chats,
        moved,
        started,
    )
    from tests.hub.test_runs import PROJECT
    from tests.hub.test_system_traces import SPEC, put_system, spans_of

    LED = {
        **SPEC,
        "nodes": [*SPEC["nodes"], {"id": "lead", "kind": "orchestrator", "profile": "lead"}],
        "edges": [
            *SPEC["edges"],
            {"from": "lead", "to": "review", "kind": "orchestrates", "description": "the reviews"},
            {"from": "lead", "to": "fix", "kind": "orchestrates", "description": "the fixes"},
        ],
    }

    def threads_of(db) -> list[dict]:
        t = tables.orchestrator_threads
        return [dict(zip(t.c.keys(), row, strict=True)) for row in sql(db, select(t).order_by(t.c.id))]

    def audited(db, action: str) -> list[str]:
        a = tables.audit
        return [row[0] for row in sql(db, select(a.c.target).where(a.c.action == action).order_by(a.c.id))]

    def in_thread(db, root_id: int) -> list[str]:
        m = tables.chat_messages
        query = select(m.c.body).where(m.c.thread_root_id == root_id).order_by(m.c.id)
        return [row[0] for row in sql(db, query)]

    def led_desk(client, hub, spec: dict = LED) -> tuple:
        """The system review led by @lead, a worker for each agent, and the owner's run of @reviewer, running."""
        agent(client, hub["owner"], "lead")
        give_account_pool(client.db, PROJECT, "lead", OWNER)
        put_system(client, hub["owner"], spec)
        studio = chat_worker(client, hub["owner"], "studio", slots=3)
        fixer = chat_worker(client, hub["owner"], "fixer-node", slots=3, pool="fixer")
        lead = chat_worker(client, hub["owner"], "lead-node", slots=3, pool="lead")
        chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
        _, spec_run = started(client, hub, studio, chat)
        return studio, fixer, lead, spec_run["id"]

    def escalated(client, worker: dict, run_id: int, question: str, **brief) -> dict:
        response = in_chats(client, worker, run_id, "escalate", {"question": question, **brief})
        assert response.status_code == 201, response.text
        return response.json()

    def lead_run(client, lead: dict, run_id: int) -> None:
        spec = claim(client, lead)
        assert spec is not None and spec["id"] == run_id, spec
        moved(client, lead, run_id, "running", session_id="sess-lead")

    def acted(client, lead: dict, run_id: int, action: str, body: dict, status: int = 200):
        response = in_chats(client, lead, run_id, f"triage/{action}", body)
        assert response.status_code == status, response.text
        return response.json()

    def test_an_escalation_opens_a_thread_at_the_askers_hop_and_the_answer_wakes_its_wait(client, hub, hub_db):
        studio, _, lead, parent = led_desk(client, hub)
        made = escalated(client, studio, parent, "Which retry policy wins, backoff or a cap?", goal="one policy")
        assert made["refusal"] is None and made["run_id"] and made["decision_id"]
        (thread,) = threads_of(hub_db)
        r = tables.runs
        hops = dict(sql(hub_db, select(r.c.id, r.c.hop).where(r.c.id.in_([parent, made["run_id"]]))))
        assert (thread["rule"], thread["subject"], thread["asker_run_id"], thread["wait_id"]) == (
            "escalate",
            "reviewer",
            parent,
            made["decision_id"],
        )
        assert thread["hop"] == hops[parent] == hops[made["run_id"]], "an escalation adds no hop"
        assert thread["orchestrator_run_id"] == made["run_id"] and thread["key"] is None
        m = tables.chat_messages
        root = sql(hub_db, select(m.c.body).where(m.c.id == thread["root_message_id"]))[0][0]
        assert root.startswith("Rule: escalate:") and "Which retry policy wins" in root and "@lead" in root, root
        (tool,) = [s for s in spans_of(hub_db) if s["tool"] == system_context.TOOL_ESCALATE]
        assert (tool["status"], tool["to_node"]) == ("ok", "lead")

        lead_run(client, lead, made["run_id"])
        refused = in_chats(client, studio, parent, "triage/answer", {"text": "x", "reason": "y"})
        assert refused.status_code == 404, refused.text
        done = acted(client, lead, made["run_id"], "answer", {"text": "Backoff, capped at 5.", "reason": "per spec"})
        assert done["run_id"] is not None
        d = tables.decisions
        assert sql(hub_db, select(d.c.state).where(d.c.id == made["decision_id"]))[0][0] == "answered"
        (thread,) = threads_of(hub_db)
        assert thread["outcome"] == "answer" and thread["closed_at"] is not None
        assert any(
            body.startswith("Decision: answered the wait") for body in in_thread(hub_db, thread["root_message_id"])
        )
        (target,) = audited(hub_db, "orchestrator.answer")
        assert f"thread:{thread['id']} rule:escalate subject:@reviewer orchestrator_run:{made['run_id']}" in target
        again = in_chats(client, lead, made["run_id"], "triage/answer", {"text": "x", "reason": "y"})
        assert again.status_code == 409, again.text

    def test_the_orchestrator_gives_the_work_to_another_agent_at_its_hop_and_moves_the_wait(client, hub, hub_db):
        studio, fixer, lead, parent = led_desk(client, hub)
        made = escalated(client, studio, parent, "The retry loop is not mine to fix: who takes it?")
        lead_run(client, lead, made["run_id"])
        for handle, said in (("reviewer", "answer it instead"), ("lead", "not an agent node"), ("ghost", "no agent")):
            body = {"handle": handle, "note": "fix the loop", "reason": "r"}
            refused = in_chats(client, lead, made["run_id"], "triage/reassign", body)
            assert refused.status_code == 409 and said in refused.text, refused.text
        body = {"handle": "fixer", "note": "Fix the retry loop of the wait helper.", "reason": "fixer owns the loop"}
        done = acted(client, lead, made["run_id"], "reassign", body)
        r = tables.runs
        child = done["run_id"]
        hop, by, wait = sql(hub_db, select(r.c.hop, r.c.spawned_by_run_id, r.c.spawned_wait_id).where(r.c.id == child))[
            0
        ]
        (thread,) = threads_of(hub_db)
        assert hop == thread["hop"], "reassigned work keeps its hop"
        assert (by, wait) == (parent, made["decision_id"]), "the wait of the asker moved to the new run"
        old = sql(hub_db, select(r.c.spawned_wait_id).where(r.c.id == made["run_id"]))[0][0]
        assert old is None
        assert thread["outcome"] == "reassign"
        assert audited(hub_db, "orchestrator.reassign")
        assert any(body.startswith("Decision: ") for body in in_thread(hub_db, thread["root_message_id"]))
        assert claim(client, fixer)["id"] == child

    def test_the_orchestrator_asks_the_owner_and_the_thread_stays_open(client, hub, hub_db):
        studio, _, lead, parent = led_desk(client, hub)
        made = escalated(client, studio, parent, "Should the release wait for the fix?")
        lead_run(client, lead, made["run_id"])
        options = [{"key": "wait", "label": "Wait for it"}, {"key": "ship", "label": "Ship now"}]
        body = {"question": "Hold the release for the retry fix?", "options": options, "reason": "the owner's call"}
        done = acted(client, lead, made["run_id"], "ask-owner", body)
        d = tables.decisions
        category, run_id = sql(hub_db, select(d.c.category, d.c.run_id).where(d.c.id == done["decision_id"]))[0]
        assert (category, run_id) == ("triage", made["run_id"])
        (thread,) = threads_of(hub_db)
        assert (thread["outcome"], thread["closed_at"]) == ("ask_owner", None)
        assert audited(hub_db, "orchestrator.ask_owner")

    def test_a_failed_run_of_an_orchestrated_agent_opens_a_failed_thread_once(client, hub, hub_db):
        studio, _, lead, parent = led_desk(client, hub)
        moved(client, studio, parent, "failed", error="claude-code stopped: the model refused")
        (thread,) = threads_of(hub_db)
        assert (thread["rule"], thread["subject"], thread["subject_run_id"]) == ("failed", "reviewer", parent)
        assert thread["key"] == f"failed:run:{parent}" and thread["evidence"], "the thread names its spans"
        assert claim(client, lead)["id"] == thread["orchestrator_run_id"]
        refused = in_chats(client, studio, parent, "escalate", {"question": "still there?"})
        assert refused.status_code in (404, 409, 422), refused.text

    def test_an_agent_of_no_orchestrator_opens_no_thread(client, hub, hub_db):
        put_system(client, hub["owner"])
        studio = chat_worker(client, hub["owner"], "studio", slots=3)
        chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
        _, spec = started(client, hub, studio, chat)
        made = escalated(client, studio, spec["id"], "anyone?")
        assert made["run_id"] is None and "names no Orchestrator" in made["refusal"]
        moved(client, studio, spec["id"], "failed", error="stop here")
        assert threads_of(hub_db) == []
        assert sql(hub_db, select(tables.agent_systems.c.ops_chat_id))[0][0] is None

    def test_a_refused_budget_opens_a_budget_thread(client, hub, hub_db):
        studio, _, _, parent = led_desk(client, hub, {**LED, "limits": {"trace_runs": 2, "trace_usd": 2}})
        assert in_chats(client, studio, parent, "agent/asks", {"handle": "fixer", "body": "fix it"}).status_code == 201
        over = in_chats(client, studio, parent, "agent/asks", {"handle": "fixer", "body": "and the clock"}).json()
        assert over["run_id"] is None and system_context.BUDGET_REFUSAL in over["refusal"]
        (thread,) = [t for t in threads_of(hub_db) if t["rule"] == "budget"]
        refused = [s["id"] for s in spans_of(hub_db) if s["tool"] == "agent_ask" and s["status"] == "refused"]
        assert thread["evidence"] == refused and thread["key"].startswith("budget:trace:")

    def test_the_same_work_three_times_opens_a_loop_thread(client, hub, hub_db):
        studio, fixer, _, parent = led_desk(client, hub)
        for _ in range(3):
            response = in_chats(client, studio, parent, "agent/asks", {"handle": "fixer", "body": "fix the retry loop"})
            assert response.status_code == 201, response.text
        (thread,) = [t for t in threads_of(hub_db) if t["rule"] == "loop"]
        assert thread["subject"] == "fixer" and len(thread["evidence"]) == 3

    def test_a_quiet_trace_opens_an_idle_thread_past_its_limit(client, hub, hub_db):
        studio, _, _, parent = led_desk(client, hub)
        assert client.portal.call(orchestrator.open_idle, client.app.state.engine) == 0, "not quiet yet"
        t = tables.system_traces
        sql(hub_db, update(t).values(last_at=t.c.last_at - timedelta(minutes=31)))
        assert client.portal.call(orchestrator.open_idle, client.app.state.engine) == 1
        (thread,) = threads_of(hub_db)
        assert (thread["rule"], thread["subject_run_id"]) == ("idle", parent)
        assert client.portal.call(orchestrator.open_idle, client.app.state.engine) == 0, "once a trace"

"""Two runs that wait on each other (criterion a5 of plan hub-steward): the wait graph of agent asks, its cycles, the
run below that the hub wakes, and the reaper's pass that breaks a cycle once, with the last message of the other run
and an event saying why.

The case of 2026-11-11 is replayed with its run and wait ids: @ea-lead (#4573, resuming #4565) asked @ea-dev (#4574,
wait #1118); #4574 escalated to the Orchestrator (#4575, wait #1119), which asked the owner, went on as #4980 and gave
the work with wait #1119 to @ea-lead, whose session went on as #4982 and asked @ea-dev again (#4984 going on from
#4574, wait #1143).
"""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")  # wait_cycles reads the waits with SQLAlchemy: a core install has none

from evo_agents.hub.server import wait_cycles  # noqa: E402
from evo_agents.hub.server.wait_cycles import Edge, Node  # noqa: E402

RESUMED_BEFORE = {4565: 4573, 4575: 4980}  # when the Orchestrator #4980 reassigned
RESUMED_AFTER = {**RESUMED_BEFORE, 4573: 4982, 4574: 4984}
HOPS = {4573: Node(1, "ea-lead"), 4982: Node(1, "ea-lead"), 4574: Node(2, "ea-dev"), 4984: Node(2, "ea-dev")}


def test_reassign_of_the_real_case_would_have_given_the_wait_to_an_agent_that_waits_on_the_asker():
    edges = wait_cycles.edges_of([(1118, 4573, 4574)], RESUMED_BEFORE)
    assert wait_cycles.cycles(edges) == []
    assert wait_cycles.upstream(edges, 4574) == {4573}, "@ea-lead's session waits on #4574, which escalated"
    assert wait_cycles.upstream(edges, 4573) == set()


def test_the_cycle_of_the_real_case_breaks_at_ea_devs_run_with_its_escalation_wait():
    # the old reassign moved wait #1119 to #4982; open decisions follow the run that resumes their run
    waits = [(1118, 4982, 4574), (1119, 4984, 4982), (1143, 4982, 4984)]
    edges = wait_cycles.edges_of(waits, RESUMED_AFTER)
    assert {(e.wait_id, e.waiter, e.awaited) for e in edges} == {
        (1118, 4982, 4984),
        (1119, 4984, 4982),
        (1143, 4982, 4984),
    }, "a wait on #4574 is answered by the end of #4984, the last run of its chain"
    (cycle,) = wait_cycles.cycles(edges)
    assert {e.waiter for e in cycle} == {4982, 4984}
    edge = wait_cycles.to_answer(cycle, HOPS)
    assert (edge.wait_id, edge.waiter, edge.awaited) == (1119, 4984, 4982), "the run below, at hop 2"


def test_a_chain_of_asks_is_no_cycle_and_a_longer_cycle_is_found_once():
    chain = [Edge(1, 10, 20), Edge(2, 20, 30), Edge(3, 40, 30)]
    assert wait_cycles.cycles(chain) == []
    assert wait_cycles.upstream(chain, 30) == {10, 20, 40}
    ring = [*chain, Edge(4, 30, 10)]
    (cycle,) = wait_cycles.cycles(ring)
    assert [e.wait_id for e in cycle] == [1, 2, 4]
    hops = {10: Node(1, "a"), 20: Node(2, "b"), 30: Node(3, "c")}
    assert wait_cycles.to_answer(cycle, hops).wait_id == 4
    tied = {10: Node(2, "a"), 20: Node(2, "b"), 30: Node(1, "c")}
    assert wait_cycles.to_answer(cycle, tied).waiter == 20, "the newest run on a tie"


def test_a_chain_of_resumed_runs_is_followed_to_its_last_run():
    assert wait_cycles.last_runs({1: 2, 2: 3}, [1, 2, 3, 9]) == {1: 3, 2: 3, 3: 3, 9: 9}
    assert wait_cycles.last_runs({1: 2, 2: 1}, [1])[1] in (1, 2), "a corrupt chain still ends"


# On the hub

from tests.hub import pg  # noqa: E402

if pg.DSN:
    from sqlalchemy import select, update

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        app_key,
        claim,
        client,
        hub,
        in_chats,
        moved,
        reply,
    )
    from tests.hub.test_system_orchestrator import escalated, lead_run, led_desk

    def breaks(client) -> int:
        return client.portal.call(wait_cycles.break_cycles, client.app.state.engine)

    def test_the_reaper_breaks_a_wait_cycle_once_with_the_other_runs_last_message(client, hub, hub_db):
        studio, fixer, lead, parent = led_desk(client, hub)
        asked = in_chats(client, studio, parent, "agent/asks", {"handle": "fixer", "body": "Fix the retry loop."})
        assert asked.status_code == 201, asked.text
        child, ask_wait = asked.json()["run_id"], asked.json()["decision_id"]
        assert claim(client, fixer)["id"] == child
        moved(client, fixer, child, "running", session_id="sess-fix")
        made = escalated(client, fixer, child, "Backoff or a cap: which does the reviewer want?")
        lead_run(client, lead, made["run_id"])
        reply(client, studio, parent, "Backoff, capped at five tries.")
        moved(client, studio, parent, "waiting")
        moved(client, fixer, child, "waiting")
        assert breaks(client) == 0, "the reviewer waits on the fixer, and the fixer on the Orchestrator: no cycle"

        d, e = tables.decisions, tables.run_events
        sql(hub_db, update(d).values(awaits_run_id=parent).where(d.c.id == made["decision_id"]))  # the old reassign
        assert breaks(client) == 1
        state, answer = sql(hub_db, select(d.c.state, d.c.answer_text).where(d.c.id == made["decision_id"]))[0]
        assert state == "answered" and "wait on each other" in answer and "capped at five tries" in answer, answer
        assert sql(hub_db, select(d.c.state).where(d.c.id == ask_wait)) == [("open",)], "the run above still waits"
        for run_id in (child, parent):
            said = [row[0] for row in sql(hub_db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system"))]
            assert any(str(body.get("text", "")).startswith("Wait cycle broken:") for body in said), (run_id, said)
        assert breaks(client) == 0, "once a cycle"

    def test_the_orchestrator_may_not_reassign_to_an_agent_that_waits_on_the_asker(client, hub, hub_db):
        studio, fixer, lead, parent = led_desk(client, hub)
        asked = in_chats(client, studio, parent, "agent/asks", {"handle": "fixer", "body": "Fix the retry loop."})
        child = asked.json()["run_id"]
        assert claim(client, fixer)["id"] == child
        moved(client, fixer, child, "running", session_id="sess-fix")
        made = escalated(client, fixer, child, "Backoff or a cap?")
        lead_run(client, lead, made["run_id"])
        body = {"handle": "reviewer", "note": "Say which you want.", "reason": "the reviewer asked for it"}
        refused = in_chats(client, lead, made["run_id"], "triage/reassign", body)
        assert refused.status_code == 409 and "a cycle" in refused.text, refused.text
        d = tables.decisions
        awaited = sql(hub_db, select(d.c.awaits_run_id).where(d.c.id == made["decision_id"]))
        assert awaited == [(made["run_id"],)], "the wait still awaits the Orchestrator's run"

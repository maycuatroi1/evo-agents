"""The spread of the traces of an agent system (``system_context.trace_row`` and ``distribution``,
``evo_agents.hub.server.system_trace_stats``, GET .../systems/{slug}/metrics/traces, ``hub system metrics``): per
trace its final cost and runs with the tree they spawned, the deepest hop, the budget refusals, the overrun of
limits.trace_usd only when positive, the loop, idle and rounds threads and the off-design ratio per edge; per system
p50 and p95 over a window of N days. It reads only: the budget and the rules keep their tests
(test_system_traces.py, test_system_orchestrator.py).
"""

from __future__ import annotations

import pytest

from evo_agents.hub import system_context, triage_rules
from tests.hub import pg


def test_the_percentile_interpolates_between_the_nearest_ranks():
    assert system_context.percentile([], 0.5) is None
    assert system_context.percentile([4.0], 0.95) == 4.0
    assert system_context.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert system_context.percentile(list(range(1, 21)), 0.95) == pytest.approx(19.05)
    assert system_context.percentile([3, 1, 2], 1.0) == 3.0


def test_the_thread_rules_are_the_triage_rules():
    assert system_context.THREAD_RULES == (triage_rules.LOOP, triage_rules.IDLE, triage_rules.ROUNDS)


def row(usd: float, runs: int, **more) -> dict:
    values = {"max_hop": 1, "budget_refusals": 0, "threads": {}, "crossings": [], **more}
    return system_context.trace_row({"limits": {"trace_usd": 1.0, "trace_runs": 5}}, usd=usd, runs=runs, **values)


def test_a_trace_row_counts_the_overrun_only_when_positive_and_the_off_design_ratio_per_edge():
    under = row(0.4, 2)
    assert (under["over_usd"], under["trace_usd"], under["trace_runs"]) == (None, 1.0, 5)
    assert row(1.0, 2)["over_usd"] is None, "at the limit is no overrun"
    crossings = [("in", "review", False), ("review", "fix", False), ("review", "fix", True), ("fix", "review", True)]
    over = row(1.25, 6, threads={"loop": 2, "rounds": 1, "budget": 1}, crossings=crossings, budget_refusals=1)
    assert over["over_usd"] == 0.25
    assert (over["loop_threads"], over["idle_threads"], over["rounds_threads"]) == (2, 0, 1)
    edges = {(e["from_node"], e["to_node"]): e for e in over["edges"]}
    assert edges[("review", "fix")] == {
        "from_node": "review",
        "to_node": "fix",
        "count": 2,
        "off_design": 1,
        "off_design_ratio": 0.5,
    }
    assert edges[("fix", "review")]["off_design_ratio"] == 1.0
    assert system_context.trace_row({}, usd=3, runs=1, max_hop=None, budget_refusals=0, threads={}, crossings=[])[
        "over_usd"
    ] == pytest.approx(3 - system_context.limits_of({})[0]), "a spec with no limits has the defaults"


def test_the_distribution_of_a_system_sums_the_rows_and_none_is_empty():
    empty = system_context.distribution([])
    assert empty["traces"] == 0 and empty["usd"] == {"p50": None, "p95": None, "max": None}
    assert (empty["over_budget"], empty["max_over_usd"], empty["edges"]) == (0, None, [])
    rows = [
        row(0.5, 2, crossings=[("in", "review", False)]),
        row(1.5, 6, crossings=[("in", "review", False), ("review", "fix", True)], budget_refusals=2),
        row(2.0, 8, threads={"idle": 1}, max_hop=3, crossings=[("in", "review", True)]),
    ]
    found = system_context.distribution(rows)
    assert found["traces"] == 3
    assert found["usd"] == {"p50": 1.5, "p95": pytest.approx(1.95), "max": 2.0}
    assert found["runs"]["p50"] == 6 and found["max_hop"]["max"] == 3
    assert (found["over_budget"], found["max_over_usd"], found["budget_refusals"], found["idle_threads"]) == (
        2,
        1.0,
        2,
        1,
    )
    edges = {(e["from_node"], e["to_node"]): e for e in found["edges"]}
    assert (edges[("in", "review")]["count"], edges[("in", "review")]["off_design"]) == (3, 1)
    assert edges[("in", "review")]["off_design_ratio"] == pytest.approx(0.3333)


# On the hub

if pg.DSN:
    import argparse
    import json
    from datetime import timedelta

    from sqlalchemy import func, insert, literal, select, update

    from evo_agents.hub import system_spec, tables
    from evo_agents.hub.cli_systems import cmd_metrics
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        PROJECT,
        app_key,
        chat_worker,
        claim,
        client,
        created,
        hub,
        in_chats,
        moved,
        sent,
        started,
    )
    from tests.hub.test_system_traces import SLUG, SPEC, mentioned, put_system

    def spend(db, run_id: int, reported: float | None, estimated: float | None) -> None:
        values = {"run_id": run_id, "session": "", "model": "", "reports": 1, "ends": {}}
        sql(db, insert(tables.run_spend).values(reported_usd=reported, estimated_usd=estimated, **values))

    def spawned(db, parent: int) -> int:
        """A run spawned by run ``parent``, outside any trace: a copy of it, as a delegation leaves one."""
        r = tables.runs
        skip = {"id", "spawned_by_run_id", "session_id", "resume_of_run_id"}
        columns = [c for c in r.c if c.name not in skip and not c.unique]
        made = insert(r).from_select(
            [*[c.name for c in columns], "spawned_by_run_id"],
            select(*columns, literal(parent, r.c.id.type)).where(r.c.id == parent),
        )
        return sql(db, made.returning(r.c.id))[0][0]

    def thread(db, trace_id: int, rule: str) -> None:
        s, t = tables.agent_systems, tables.orchestrator_threads
        system_id, project_id = sql(db, select(s.c.id, s.c.project_id).where(s.c.slug == SLUG))[0]
        values = {"system_id": system_id, "project_id": project_id, "trace_id": trace_id, "subject": "reviewer"}
        sql(db, insert(t).values(rule=rule, key=f"{rule}:trace:{trace_id}", **values))

    def spread(client, headers, slug: str = SLUG, days: int = 30) -> dict:
        response = client.get(f"/v1/projects/{PROJECT}/systems/{slug}/metrics/traces?days={days}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def test_the_spread_of_the_traces_of_a_system_over_a_window(client, hub, hub_db, monkeypatch, capsys):
        put_system(client, hub["owner"], {**SPEC, "limits": {"trace_usd": 1, "trace_runs": 4}})
        studio = chat_worker(client, hub["owner"], "studio", slots=3)
        fixer = chat_worker(client, hub["owner"], "fixer-node", pool="fixer")
        chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
        _, spec = started(client, hub, studio, chat)
        parent = spec["id"]
        child = mentioned(client, studio, parent, "fixer", "fix the retry loop")["run_id"]
        assert claim(client, fixer)["id"] == child
        moved(client, studio, parent, "verifying", "done")
        moved(client, fixer, child, "running", session_id="sess-fix")
        back = mentioned(client, fixer, child, "reviewer", "review the fix again")["run_id"]  # off the edges
        below = spawned(hub_db, child)  # in the tree, through spawned_by_run_id
        refused = mentioned(client, fixer, child, "reviewer", "and once more")
        assert refused["run_id"] is None and refused["refusal"].startswith(system_context.BUDGET_REFUSAL)
        spend(hub_db, parent, 0.8, 0.1)  # the reported cost wins over the estimate
        spend(hub_db, child, None, 0.5)
        spend(hub_db, below, 0.2, None)
        t = tables.system_traces
        ((first,),) = sql(hub_db, select(t.c.id))
        for rule in ("loop", "idle", "rounds", "budget"):
            thread(hub_db, first, rule)

        quiet = created(client, hub["owner"], title="Quiet", agents=["reviewer"])
        sent(client, hub["owner"], quiet["id"], "@reviewer look at the docs")
        old = created(client, hub["owner"], title="Old", agents=["reviewer"])
        sent(client, hub["owner"], old["id"], "@reviewer an old question")
        second, third = sql(hub_db, select(t.c.id).where(t.c.id != first).order_by(t.c.id))
        second, third = second[0], third[0]
        sql(hub_db, update(t).values(started_at=func.now() - timedelta(days=40)).where(t.c.id == third))

        found = spread(client, hub["owner"])
        assert (found["system"], found["days"], found["withheld"], found["truncated"]) == (SLUG, 30, 0, False)
        traces = {trace["id"]: trace for trace in found["traces"]}
        assert list(traces) == [second, first], "the window leaves out the old trace, the newest first"
        busy = traces[first]
        assert (busy["runs"], busy["usd"], busy["over_usd"], busy["trace_usd"]) == (4, 1.5, 0.5, 1.0)
        r = tables.runs
        hops = [hop for (hop,) in sql(hub_db, select(r.c.hop).where(r.c.id.in_([parent, child, back, below])))]
        assert busy["max_hop"] == max(hops) and busy["max_hop"] >= 2
        assert busy["budget_refusals"] == 1 and busy["open"] is True
        assert (busy["loop_threads"], busy["idle_threads"], busy["rounds_threads"]) == (1, 1, 1)
        edges = {(e["from_node"], e["to_node"]): e for e in busy["edges"]}
        assert (edges[("in", "review")]["count"], edges[("in", "review")]["off_design"]) == (1, 0)
        assert edges[("review", "fix")]["off_design_ratio"] == 0
        # the turn handed off the edges, and the mention the budget refused on the same edge
        assert (edges[("fix", "review")]["count"], edges[("fix", "review")]["off_design"]) == (2, 1)
        calm = traces[second]
        assert (calm["usd"], calm["runs"], calm["over_usd"], calm["budget_refusals"]) == (0, 1, None, 0)
        summary = found["summary"]
        assert summary["traces"] == 2 and summary["over_budget"] == 1 and summary["max_over_usd"] == 0.5
        assert summary["usd"]["p50"] == pytest.approx(0.75) and summary["usd"]["p95"] == pytest.approx(1.425)
        assert summary["runs"] == {"p50": 2.5, "p95": pytest.approx(3.85), "max": 4}
        assert summary["budget_refusals"] == 1 and summary["loop_threads"] == 1

        assert [trace["id"] for trace in spread(client, hub["owner"], days=60)["traces"]] == [third, second, first]

        # a system with no trace: an empty spread
        put_system(
            client,
            hub["owner"],
            {
                "schema": system_spec.SCHEMA,
                "name": "Docs desk",
                "nodes": [{"id": "in", "kind": "entry"}, {"id": "write", "kind": "agent", "profile": "fixer"}],
                "edges": [{"from": "in", "to": "write", "kind": "interacts", "description": "the docs to write"}],
            },
            slug="docs",
        )
        empty = spread(client, hub["owner"], slug="docs")
        assert empty["traces"] == [] and empty["summary"]["traces"] == 0 and empty["summary"]["usd"]["p50"] is None
        assert (
            client.get(f"/v1/projects/{PROJECT}/systems/{SLUG}/metrics/traces?days=0", headers=hub["owner"]).status_code
            == 422
        )

        # the command, over every system of the project
        class Hub:
            def call(self, method, path, body=None):
                response = client.request(method, path, headers=hub["owner"])
                assert response.status_code == 200, response.text
                return response.json()

        monkeypatch.setattr("evo_agents.hub.cli_systems.signed_in", lambda: (Hub(), None))
        args = argparse.Namespace(project=PROJECT, slug=None, days=30, traces=True, json=True)
        assert cmd_metrics(args) == 0
        printed = json.loads(capsys.readouterr().out)
        assert sorted(each["system"] for each in printed) == ["docs", SLUG]
        args.json = False
        assert cmd_metrics(args) == 0
        out = capsys.readouterr().out
        assert f"system {SLUG}: 2 traces" in out and "system docs: 0 traces" in out
        assert "edge fix -> review: 2 crossings, 1 off design (50%)" in out
        assert "over trace_usd 1 (max 0.50 USD)" in out

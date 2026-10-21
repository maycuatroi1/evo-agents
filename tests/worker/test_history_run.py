"""A history run on the worker (``evo_agents.worker.runner.history``), without Postgres: the daemon in this process with
the fake runtime and the in-memory hub of ``tests.worker.test_plan_runs``.

The checks step 4 of the history-driven-curator plan names on the worker: each batch the hub hands read by the agent
in a session of its own in the run's own empty directory, on the run's cheap model, with what is left of the run's
caps; each facet its last message gives, and why an item got none, posted with the run's own key; the new groups of
facets named once the facets are written; the run stopping at its caps rather than failing on them, a batch a cap
stopped left for the next night; nothing pushed."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from tests.worker.test_plan_runs import (
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)

BUDGET = {"max_usd": 1.0, "max_turns": 30, "max_seconds": 1200, "spent_usd": 0.0, "spent_seconds": 0.0}


def item(n: int) -> dict:
    return {
        "item": f"session:s{n}",
        "kind": "session",
        "source": "Claude Code session",
        "fingerprint": f"f{n}",
        "lines": 40,
        "anchors": 2,
        "cut": n == 1,
        "excerpt": f"L1 user: task {n}\nL5 Bash failed: boom {n}",
    }


def facet(n: int, **changes) -> dict:
    found = {
        "item": f"session:s{n}",
        "summary": f"Session {n} ran the tests.",
        "task_type": "fix",
        "outcome": "partial",
        "friction": ["a test failed"],
        "failure_modes": ["FM-3.2"],
        "corrections": [],
    }
    return found | changes


def answer(*facets: dict) -> str:
    return "Here are the facets.\n```json\n" + json.dumps({"facets": list(facets)}) + "\n```"


def cost(usd: float) -> dict:
    return {"usage": {"total_cost_usd": usd, "input_tokens": 1000, "output_tokens": 100}}


def history_starts(machine, run_id: int) -> list[dict]:  # noqa: F811
    return [start for start in machine.starts() if start["run"] == run_id] if machine.starts_path.exists() else []


def test_history_run_reads_each_batch_in_a_session_of_its_own_then_names_the_new_groups(machine):  # noqa: F811
    names = {"names": [{"key": "fix/FM-3.2", "name": "Fixes left unchecked", "same_as": None}]}
    machine.scenarios(
        {
            "history": [cost(0.05), {"say": answer(facet(1), facet(2, task_type="poetry", failure_modes=["FM-9"]))}],
            "history/2": [cost(0.05), {"say": "I could not read it."}],
            "history/3": [cost(0.02), {"say": json.dumps(names)}],
        }
    )
    found = {}

    async def body(hub, daemon):
        hub.history_batch = 2
        hub.history_items = [item(1), item(2), item(3)]
        hub.history_groups = [{"key": "fix/FM-3.2", "label": "fix", "sessions": 3, "samples": [facet(1)]}]
        run_id = hub.queue_history_run(budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
    first, second, third = hub.history_reports
    assert [entry["item"] for entry in first["facets"]] == ["session:s1", "session:s2"]
    assert first["facets"][0]["facet"]["failure_modes"] == ["FM-3.2"] and first["facets"][0]["fingerprint"] == "f1"
    kept = first["facets"][1]["facet"]
    assert (kept["task_type"], kept["failure_modes"]) == ("other", []), "the worker keeps a facet within bounds"
    assert second["facets"] == [
        {
            "item": "session:s3",
            "fingerprint": "f3",
            "error": "the model's answer did not end with a JSON object of facets",
        }
    ]
    assert third == {
        "run_id": run_id,
        "names": [{"key": "fix/FM-3.2", "name": "Fixes left unchecked", "description": None, "same_as": None}],
    }
    assert [ask["clusters"] for ask in hub.history_asks] == [False, False, False, True]
    starts = history_starts(machine, run_id)
    assert len(starts) == 3 and len({start["session"] for start in starts}) == 3, "a session for each batch"
    assert [start["budget"]["max_usd"] for start in starts] == [1.0, 0.95, 0.9], "what is left of the run's cost"
    assert all(start["budget"]["max_turns"] == 4 for start in starts)
    assert all(start["cwd"] == starts[0]["cwd"] and start["cwd"].endswith(f"-{run_id}") for start in starts)
    assert (
        "## Item session:s1 (Claude Code session, 40 lines, cut around its errors and corrections)"
        in starts[0]["prompt"]
    )
    assert "## Item session:s3" in starts[1]["prompt"] and "## Group fix/FM-3.2" in starts[2]["prompt"]
    assert hub.judge_refused == []
    run = hub.runs[run_id]
    assert run["usage"]["total_cost_usd"] == pytest.approx(0.12), "every session's cost added up"
    assert run["summary"].startswith("Wrote 2 facet(s) of the model in 2 batch(es) and named 1 group(s); 1 item(s)")


def test_history_run_starts_no_session_its_caps_cannot_hold_and_ends_done(machine):  # noqa: F811
    machine.scenarios({"history": [cost(0.08), {"say": answer(facet(1))}], "history/2": [{"say": "never"}]})
    found = {}

    async def body(hub, daemon):
        hub.history_batch = 1
        hub.history_items = [item(1), item(2), item(3)]
        hub.history_groups = [{"key": "fix/FM-3.2", "label": "fix", "sessions": 3, "samples": []}]
        run_id = hub.queue_history_run(budget={**BUDGET, "max_usd": 0.1})
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert len(history_starts(machine, run_id)) == 1, "a second batch would pass the cost cap"
    (reported,) = hub.history_reports
    assert [entry["item"] for entry in reported["facets"]] == ["session:s1"]
    assert hub.runs[run_id]["summary"].endswith("2 item(s) wait for the next night.")


def test_a_batch_the_cost_cap_stops_waits_for_the_next_night_and_the_run_ends_done(machine):  # noqa: F811
    machine.scenarios({"history": [{"fail": "the budget ran out", "cap": "cost", **cost(1.0)}]})
    found = {}

    async def body(hub, daemon):
        hub.history_items = [item(1), item(2)]
        run_id = hub.queue_history_run(budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert hub.history_reports == [], "nothing of the batch it stopped"
    assert [ask["clusters"] for ask in hub.history_asks] == [False]
    assert "The cost cap stopped it; 2 item(s) wait for the next night." in hub.runs[run_id]["summary"]


def test_history_run_with_nothing_to_read_starts_no_agent(machine):  # noqa: F811
    machine.scenarios({"history": [{"say": "never"}]})
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_history_run(budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert history_starts(machine, run_id) == []
    assert [ask["clusters"] for ask in hub.history_asks] == [False, True]
    assert hub.runs[run_id]["summary"].endswith("Nothing waits.")


def test_history_runs_agent_command_says_it_answers_its_batch_instead(machine):  # noqa: F811
    machine.scenarios({"history": [{"cli": ["step", "3", "in_progress"]}, {"say": answer(facet(1))}]})
    found = {}

    async def body(hub, daemon):
        hub.history_items = [item(1)]
        run_id = hub.queue_history_run(budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id)

    with_daemon(machine, body)
    (command,) = [item for item in machine.commands() if item["run"] == found["run"]]
    assert command["exit"] != 0 and "is a history run, whose agent answers each batch" in command["stderr"]

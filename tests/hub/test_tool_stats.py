"""The tool figures of runs on the hub (``evo_agents.hub.server.tool_stats``): written when a run ends, kept after the
daily pruning deletes its events, read through the REST API and the MCP tool run_tool_stats.

The checks step 2 of the curator-agent plan names for criterion a3: when a run ends the hub writes, per tool, the
calls, the failed calls and the time they took; they are still there once hub.prune_run_events deleted the run's
events; the API and MCP give them per run and per tool and runtime over the last days. Around them: events that arrive
after the end, a run the reaper ends, a run that ended before schema 0013, and who may read them."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from evo_agents.hub import jobs, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.worker import queue
from tests.hub.live import bearer, sql
from tests.hub.test_runs import (
    PROJECT,
    add_worker,
    claim,
    dispatched,
    expire,
    members,
    moved,
    recover,
    set_run,
    state_of,
)

MCP_HEADERS = {"Accept": "application/json", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25"}
BASH = {"gen_ai.tool.name": "Bash", "calls": 2, "errors": 1, "duration_ms": 2000}
READ = {"gen_ai.tool.name": "Read", "calls": 1, "errors": 1, "duration_ms": 200}
EDIT = {"gen_ai.tool.name": "Edit", "calls": 1, "errors": 0, "duration_ms": 0}


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """test_runs' members of project evo-agents, a reader up to public, owner's worker mac-mini; /mcp answers
    https://hub.test."""
    config = live.hub_config(hub_db, tmp_path, github, public_url="https://hub.test")
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        headers["public"] = bearer(live.sign_in(client, github, "public-reader", 690)["token"])
        grant = {"role": "reader", "max_level": "public"}
        response = client.put(
            f"/v1/admin/projects/{PROJECT}/grants/public-reader", json=grant, headers=headers["admin"]
        )
        assert response.status_code == 200, response.text
        worker = add_worker(client, headers["owner"], "mac-mini", slots=2)
        yield SimpleNamespace(client=client, headers=headers, worker=worker, db=hub_db)


def started_run(hub, step: int = 2) -> int:
    run_id = dispatched(hub.client, hub.headers["owner"], [step])[0]["id"]
    assert claim(hub.client, hub.worker)["id"] == run_id
    moved(hub.client, hub.worker, run_id, "running")
    return run_id


def tool_events(start: datetime, first_seq: int = 1) -> list[dict]:
    """Two Bash calls (one fails), a Read that fails, an Edit that never finishes, and a message in between."""
    timeline = [
        (0, "tool_call", {"toolCallId": "c1", "title": "Bash", "kind": "execute", "status": "pending"}),
        (1.5, "tool_call_update", {"toolCallId": "c1", "status": "completed"}),
        (1.6, "agent_message_chunk", {"content": {"type": "text", "text": "Reading the queue."}}),
        (2, "tool_call", {"toolCallId": "c2", "title": "Read", "kind": "read", "status": "pending"}),
        (2.2, "tool_call_update", {"toolCallId": "c2", "status": "failed"}),
        (3, "tool_call", {"toolCallId": "c3", "title": "Bash", "kind": "execute", "status": "pending"}),
        (3.5, "tool_call_update", {"toolCallId": "c3", "status": "failed"}),
        (4, "tool_call", {"toolCallId": "c4", "title": "Edit", "kind": "edit", "status": "pending"}),
    ]
    return [
        {"seq": seq, "at": (start + timedelta(seconds=offset)).isoformat(), "kind": kind, "body": body}
        for seq, (offset, kind, body) in enumerate(timeline, start=first_seq)
    ]


def send(hub, run_id: int, events: list[dict]) -> None:
    response = hub.client.post(
        f"/v1/worker/runs/{run_id}/events", json={"events": events}, headers=hub.worker["headers"]
    )
    assert response.status_code == 200, response.text
    assert response.json()["stored"] == len(events)


def of_run(hub, run_id: int, who: str = "reader", **headers):
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/tool-stats"
    return hub.client.get(path, headers={**hub.headers[who], **headers})


def per_run(hub, run_id: int, who: str = "reader") -> dict:
    response = of_run(hub, run_id, who)
    assert response.status_code == 200, response.text
    return response.json()


def of_project(hub, who: str = "reader", **params) -> dict:
    response = hub.client.get(f"/v1/projects/{PROJECT}/tool-stats", params=params, headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


def prune(hub) -> dict:
    state = hub.client.app.state
    found = SimpleNamespace(engine=state.engine, config=state.config, sealer=state.sealer, github_app=state.github_app)
    context = SimpleNamespace(additional_context={"hub": found})
    return hub.client.portal.call(queue.tasks[jobs.PRUNE_RUN_EVENTS].func, context)


def events_left(hub, run_id: int) -> int:
    e = tables.run_events
    return sql(hub.db, select(func.count()).select_from(e).where(e.c.run_id == run_id))[0][0]


def test_tool_stats_are_written_when_a_run_ends_and_outlive_its_events(hub):
    run_id = started_run(hub)
    start = datetime.now(UTC) - timedelta(hours=1)
    send(hub, run_id, tool_events(start))
    assert per_run(hub, run_id)["tools"] == [], "nothing until the run ends"
    moved(hub.client, hub.worker, run_id, "failed", error="the tests fail")
    shown = per_run(hub, run_id)
    assert (shown["run_id"], shown["runtime"], shown["state"]) == (run_id, "claude-code", "failed")
    assert shown["tools"] == [BASH, EDIT, READ]

    # The spool's last event after the end: the Edit finished; the figures take it in.
    late = {"toolCallId": "c4", "status": "completed"}
    finished = {"seq": 9, "at": (start + timedelta(seconds=4.75)).isoformat(), "kind": "tool_call_update", "body": late}
    send(hub, run_id, [finished])
    assert per_run(hub, run_id)["tools"] == [BASH, {**EDIT, "duration_ms": 750}, READ]

    summed = of_project(hub)
    assert (summed["project"], summed["days"], summed["runs"]) == (PROJECT, 7, 1)
    assert summed["tools"][0] == {**BASH, "runtime": "claude-code", "runs": 1}
    assert [tool["gen_ai.tool.name"] for tool in summed["tools"]] == ["Bash", "Edit", "Read"]
    assert of_project(hub, runtime="codex")["tools"] == []
    assert of_project(hub, plan_id="another-plan") == {**summed, "runs": 0, "tools": []}

    set_run(hub.db, run_id, finished_at=func.now() - timedelta(days=31))
    assert prune(hub)["deleted"] == 12  # the worker's 9 and the hub's 3 state events
    assert events_left(hub, run_id) == 0
    assert per_run(hub, run_id)["tools"] == [BASH, {**EDIT, "duration_ms": 750}, READ], "kept after the pruning"
    assert of_project(hub, days=32)["tools"][0]["calls"] == 2  # 31 days ago is the 32nd day back, today included
    assert of_project(hub)["runs"] == 0  # ended 31 days ago, out of the last 7


def test_a_run_that_ended_before_0013_gets_its_tool_stats_before_its_events_are_pruned(hub):
    run_id = started_run(hub)
    send(hub, run_id, tool_events(datetime.now(UTC) - timedelta(hours=1)))
    moved(hub.client, hub.worker, run_id, "cancelled")
    s = tables.run_tool_stats
    sql(hub.db, delete(s).where(s.c.run_id == run_id))  # as a run that ended before the hub wrote them
    set_run(hub.db, run_id, finished_at=func.now() - timedelta(days=31))
    prune(hub)
    assert events_left(hub, run_id) == 0
    assert per_run(hub, run_id)["tools"] == [BASH, EDIT, READ]


def test_a_run_the_reaper_ends_gets_its_tool_stats_too(hub):
    run_id = started_run(hub)
    send(hub, run_id, tool_events(datetime.now(UTC) - timedelta(minutes=5)))
    expire(hub.db, run_id)
    recover(hub.client)
    assert state_of(hub.db, run_id) == "lost"
    assert per_run(hub, run_id)["tools"] == [BASH, EDIT, READ]


def test_reading_tool_stats_follows_the_label_of_the_run_s_plan(hub):
    run_id = started_run(hub)
    send(hub, run_id, tool_events(datetime.now(UTC) - timedelta(hours=1)))
    moved(hub.client, hub.worker, run_id, "failed", error="the tests fail")
    assert per_run(hub, run_id, "owner")["tools"] == [BASH, EDIT, READ]
    assert of_run(hub, run_id, "public").status_code == 404  # the plan is internal; the grant reaches public
    assert of_project(hub, "public") == {**of_project(hub), "runs": 0, "tools": []}
    assert of_run(hub, run_id, "reader", **{"X-Evo-Sink": "undeclared@sink"}).status_code == 404
    assert of_run(hub, run_id, "stranger").status_code == 404
    assert of_run(hub, run_id, "admin").status_code == 403  # a hub admin without a grant reads no run
    assert of_run(hub, run_id + 100).status_code == 404


def call(hub, who: str, arguments: dict) -> dict:
    message = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "run_tool_stats", "arguments": arguments},
    }
    headers = {**hub.headers[who], **MCP_HEADERS, "X-Evo-Project": PROJECT}
    response = hub.client.post("/mcp", json=message, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def test_the_mcp_tool_run_tool_stats_answers_for_a_run_and_for_the_last_days(hub):
    run_id = started_run(hub)
    send(hub, run_id, tool_events(datetime.now(UTC) - timedelta(hours=1)))
    moved(hub.client, hub.worker, run_id, "failed", error="the tests fail")

    one = call(hub, "reader", {"run_id": run_id})
    assert not one.get("isError"), one
    text = one["content"][0]["text"]
    assert f"run #{run_id} (claude-code, failed):" in text
    assert "Bash: 2 calls, 1 failed, 2.0 s" in text and "Read: 1 calls, 1 failed, 0.2 s" in text

    days = call(hub, "reader", {"days": 3, "runtime": "claude-code"})
    assert not days.get("isError"), days
    assert "1 runs of project evo-agents ended" in days["content"][0]["text"]
    assert "Bash: 2 calls, 1 failed, 2.0 s, claude-code, in 1 runs" in days["content"][0]["text"]

    both = call(hub, "reader", {"run_id": run_id, "days": 3})
    assert both["isError"] is True and "leave them out with run_id" in both["content"][0]["text"]
    hidden = call(hub, "public", {"run_id": run_id})
    assert hidden["isError"] is True and f"has no run {run_id}" in hidden["content"][0]["text"]

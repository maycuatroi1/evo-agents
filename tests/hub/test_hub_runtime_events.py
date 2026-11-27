"""The events a turn of the hub runtime writes to its run's log (``evo_agents.hub.server.hub_run_events``), as step 1 of
plan creative-agentic-workspace names them: the model's text in parts, each tool call and its result, and each round's
tokens, of the ACP kinds a worker's run writes, so the run's events and stream show the turn.

The gateway is ``Talking`` of test_hub_runtime.py, whose /v1/chat/completions answers what a test scripts.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import runs
from evo_agents.hub.server import hub_run_events
from tests.hub.test_chat_runs import created, sent
from tests.hub.test_hub_runtime import chat_runs_of, tool_call, turn
from tests.hub.test_hub_runtime import client as client  # noqa: F401  the fixtures
from tests.hub.test_hub_runtime import gateway as gateway  # noqa: F401
from tests.hub.test_hub_runtime import helper as helper  # noqa: F401
from tests.hub.test_provider_accounts import PROJECT
from tests.hub.test_provider_accounts import hub as hub  # noqa: F401


def events(client, headers, run_id: int) -> list[dict]:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/events", headers=headers)
    assert response.status_code == 200, response.text
    return [event for event in response.json()["events"] if event["kind"] in runs.ACP_EVENT_KINDS]


def test_a_hub_turn_writes_its_text_tool_calls_results_and_usage_as_acp_events(client, hub, hub_db, gateway, helper):
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], "@helper which service signs the export links?")
    (run_id,) = chat_runs_of(hub_db)
    gateway.script = [
        {
            "content": "Let me look.",
            "tool_calls": [
                tool_call("chat_post", {"body": "Looking it up."}, "call-1"),
                tool_call("no_such_tool", {}, "call-2"),
            ],
        },
        {"content": "The export service signs them."},
    ]
    assert turn(client, gateway, run_id)["state"] == "done"

    found = events(client, hub["owner"], run_id)
    kinds = [event["kind"] for event in found]
    assert kinds == [
        "agent_message_chunk",
        "tool_call",
        "tool_call",
        "usage_update",
        "tool_call_update",
        "tool_call_update",
        "agent_message_chunk",
        "usage_update",
    ]
    texts = [event["body"]["content"]["text"] for event in found if event["kind"] == "agent_message_chunk"]
    assert texts == ["Let me look.", "The export service signs them."]
    calls = [event["body"] for event in found if event["kind"] == "tool_call"]
    assert [(call["toolCallId"], call["title"], call["status"]) for call in calls] == [
        ("call-1", "chat_post", "in_progress"),
        ("call-2", "no_such_tool", "in_progress"),
    ]
    assert calls[0]["rawInput"] == {"body": "Looking it up."}
    results = [event["body"] for event in found if event["kind"] == "tool_call_update"]
    assert [(result["toolCallId"], result["status"]) for result in results] == [
        ("call-1", "completed"),
        ("call-2", "failed"),
    ]
    assert results[0]["content"][0]["content"]["text"].startswith("posted message #")
    usage = [event["body"] for event in found if event["kind"] == "usage_update"]
    assert usage[0]["usage"] == {"input_tokens": 100, "output_tokens": 20}

    stream = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/stream", headers=hub["owner"])
    assert stream.status_code == 200
    assert "agent_message_chunk" in stream.text and "event: end" in stream.text


def test_a_long_answer_goes_in_few_parts_each_within_the_event_limit(client, hub, hub_db, gateway, helper):
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], "@helper write it all down")
    (run_id,) = chat_runs_of(hub_db)
    paragraph = "The export service signs the links with the key of kb/export/keys.py. " * 40
    long = "\n\n".join([paragraph] * 60)  # some 170 KB, past the limit of one event
    gateway.script = [{"content": long}]
    turn(client, gateway, run_id)  # its events are written as the answer arrives, whatever the reply in the thread

    chunks = [event for event in events(client, hub["owner"], run_id) if event["kind"] == "agent_message_chunk"]
    assert 1 < len(chunks) <= 2 * len(long) // hub_run_events.MESSAGE_PART + 1  # each part past half of one
    assert not any(event["truncated"] for event in chunks)
    assert "".join(event["body"]["content"]["text"] for event in chunks) == long
    assert all(event["body"]["content"]["text"].endswith("\n\n") for event in chunks[:-1])  # cut at a paragraph


def test_a_run_full_of_events_takes_no_more(client, hub, hub_db, gateway, helper, monkeypatch):
    monkeypatch.setattr(runs, "MAX_RUN_EVENTS", 0)
    chat = created(client, hub["owner"], agents=["helper"])
    sent(client, hub["owner"], chat["id"], "@helper hello")
    (run_id,) = chat_runs_of(hub_db)
    assert turn(client, gateway, run_id)["state"] == "done"  # the turn goes on without its events
    assert events(client, hub["owner"], run_id) == []

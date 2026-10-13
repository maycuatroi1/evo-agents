"""A headless agent whose account hits its usage limit, on the worker (``evo_agents.worker.runner.limits``), without
Postgres: the daemon in this process with the fake runtime of ``tests.worker.fake_adapter``, which sends the
``rate_limit_event`` Claude Code passes on raw, and the in-memory hub of ``tests.worker.fake_hub``. No model is called.

The checks the claude-account-rotation plan names for its third step, on the worker's side: a rate_limit_event that is
not rejected only reports the usage; a rejected one stops the agent and reports the limit with the run's lease; when
the hub answers failover, the run takes its leases again and a new agent goes on in the same session with the new
account's value and FAILOVER_PROMPT, and a note names the move by secret id; when it answers stop, when the hub is
older than the route, or when the run holds no account of the hub, the run fails with cause rate_limit and the
earliest reset. No value reaches the run's log. Around them, the parsing of the event (the shapes seen from Claude
Code 2.1) and of a turn's error text."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub.credentials import Lease
from evo_agents.worker.adapter import AgentEvent
from evo_agents.worker.runner.limits import FAILOVER_PROMPT, rate_limit_of
from evo_agents.worker.runtimes.claude_code import OAUTH_TOKEN, limit_of_text
from tests.worker.fake_hub import FakeHub
from tests.worker.test_plan_runs import PLAN, machine, with_daemon  # noqa: F401 (machine, a fixture)

RESET = 1935316800  # 2031-04-30T12:00:00Z
RESET_ISO = "2031-04-30T12:00:00+00:00"
# What Claude Code 2.1 was seen to send (status allowed and allowed_warning); rejected has the same shape.
ALLOWED = {
    "status": "allowed",
    "resetsAt": RESET,
    "rateLimitType": "five_hour",
    "overageStatus": "rejected",
    "overageDisabledReason": "out_of_credits",
    "isUsingOverage": False,
    "unifiedWindows": {
        "five_hour": {"utilization": 0.01, "resetsAt": RESET},
        "seven_day": {"utilization": 0.63, "resetsAt": RESET + 76200},
    },
}
WARNING = {
    "status": "allowed_warning",
    "resetsAt": RESET,
    "rateLimitType": "seven_day",
    "utilization": 0.85,
    "surpassedThreshold": 0.75,
    "isUsingOverage": False,
}
REJECTED = {**ALLOWED, "status": "rejected"}


def raw(info: dict) -> AgentEvent:
    return AgentEvent("output", {"raw": {"type": "rate_limit_event", "rate_limit_info": info}})


def token() -> str:
    return "sk-ant-oat01-" + secrets.token_hex(16)


def oauth(lease_id: int, value: str, name: str) -> dict:
    return Lease(lease_id, "env", "secret", name, env_var=OAUTH_TOKEN, value=value).to_json()


# The parsing


def test_a_rate_limit_event_reads_as_the_report_the_hub_takes():
    assert rate_limit_of(raw(ALLOWED)) == {
        "status": "allowed",
        "rate_limit_type": "five_hour",
        "resets_at": RESET_ISO,
        "utilization": 0.01,
        "windows": {
            "five_hour": {"utilization": 0.01, "resets_at": RESET_ISO},
            "seven_day": {"utilization": 0.63, "resets_at": "2031-05-01T09:10:00+00:00"},
        },
    }
    assert rate_limit_of(raw(WARNING)) == {
        "status": "allowed_warning",
        "rate_limit_type": "seven_day",
        "resets_at": RESET_ISO,
        "utilization": 0.85,
    }
    assert rate_limit_of(raw(REJECTED))["status"] == "rejected"


def test_a_rejected_rate_limit_the_cli_goes_on_past_with_overage_is_a_warning():
    assert rate_limit_of(raw({**REJECTED, "isUsingOverage": True}))["status"] == "allowed_warning"
    assert rate_limit_of(raw({**REJECTED, "overageStatus": "allowed"}))["status"] == "allowed_warning"


def test_other_events_and_odd_rate_limit_events_read_as_nothing_or_drop_odd_fields():
    assert rate_limit_of(AgentEvent("output", {"raw": {"type": "result"}})) is None
    assert rate_limit_of(AgentEvent("agent_message_chunk", {"content": {"text": "rate_limit_event"}})) is None
    assert rate_limit_of(raw({"status": "exhausted"})) is None
    odd = {"status": "rejected", "rateLimitType": "five hour!", "resetsAt": "soon", "utilization": 250}
    odd["unifiedWindows"] = {"bad name": {"utilization": 0.5}, "ok": {"utilization": True}}
    assert rate_limit_of(raw(odd)) == {"status": "rejected", "windows": {"ok": {}}}


def test_a_turns_error_that_reads_as_the_usage_limit_gives_a_rejected_limit():
    assert limit_of_text(f"Claude AI usage limit reached|{RESET}") == {"status": "rejected", "resets_at": RESET_ISO}
    assert limit_of_text("5-hour limit reached ∙ resets 3pm") == {"status": "rejected"}
    assert limit_of_text("You've hit your limit · resets 9pm") == {"status": "rejected"}
    assert limit_of_text("API Error: 500 Internal server error") is None
    assert limit_of_text(None) is None


# The daemon


def first_turn(env: Path) -> list[dict]:
    return [{"env": str(env)}, {"rate_limit": ALLOWED}, {"rate_limit": REJECTED}, {"sleep": 30}]


async def until_ended(hub: FakeHub, run_id: int) -> str:
    return await hub.wait_state(run_id, "done", "failed")


def test_a_rejected_limit_moves_the_run_to_another_account_in_the_same_session(machine, tmp_path):  # noqa: F811
    first, second = token(), token()
    before, after = tmp_path / "env-1.json", tmp_path / "env-2.json"
    machine.scenarios(
        {
            "1": first_turn(before),
            "1/2": [
                {"env": str(after)},
                {"write": {"a.txt": "a\n"}},
                {"result": {"verify_commands": ["test -f a.txt"]}},
            ],
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        hub.leases[run_id] = {"leases": [oauth(1, first, "claude-a")], "missing": []}
        move = {"action": "failover", "secret_id": 12, "failovers": 1, "cooldown_until": RESET_ISO}
        hub.rate_limit_answers[run_id] = [{**move, "leases": {"leases": [oauth(2, second, "claude-b")], "missing": []}}]
        assert await until_ended(hub, run_id) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    # the usage was reported, then the limit, each with the run's lease of the first account
    reported = [(item["status"], item["lease_id"]) for item in hub.rate_limits]
    assert reported == [("allowed", 1), ("rejected", 1)], hub.rate_limits
    assert hub.rate_limits[1]["resets_at"] == RESET_ISO and hub.rate_limits[1]["rate_limit_type"] == "five_hour"
    # the second agent goes on in the session of the first, with the new account's value
    one, two = machine.starts()
    assert two["resume"] == one["session"] and two["prompt"] == FAILOVER_PROMPT
    assert json.loads(before.read_text())[OAUTH_TOKEN] == first
    assert json.loads(after.read_text())[OAUTH_TOKEN] == second
    assert hub.credential_calls[:2] == [("ask", run_id), ("ask", run_id)]
    texts = hub.texts(run_id)
    assert any("goes on with account secret 12 (failover 1) in the same session" in text for text in texts), texts
    logged = json.dumps(hub.runs[run_id]["events"])
    for secret in (first, second, "claude-a", "claude-b"):
        assert secret not in logged


def test_a_hub_that_answers_stop_fails_the_run_with_rate_limit_and_the_earliest_reset(machine, tmp_path):  # noqa: F811
    machine.scenarios({"1": first_turn(tmp_path / "env.json")})
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        hub.leases[run_id] = {"leases": [oauth(1, token(), "claude-a")], "missing": []}
        assert await until_ended(hub, run_id) == "failed", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    assert run["failure_cause"] == "rate_limit", run
    assert "no other account is ready" in run["error"] and RESET_ISO in run["error"], run["error"]
    assert len(machine.starts()) == 1, "no second agent"


def test_a_hub_older_than_the_route_fails_the_run_with_rate_limit(machine, tmp_path):  # noqa: F811
    machine.scenarios({"1": first_turn(tmp_path / "env.json")})
    found = {}

    async def body(hub: FakeHub, daemon):
        hub.rate_limit_route = False
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        hub.leases[run_id] = {"leases": [oauth(1, token(), "claude-a")], "missing": []}
        assert await until_ended(hub, run_id) == "failed", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run = found["hub"].runs[found["run"]]
    assert run["failure_cause"] == "rate_limit" and RESET_ISO in run["error"], run
    assert len(machine.starts()) == 1


def test_a_run_without_an_account_of_the_hub_reports_nothing_and_a_usage_limit_error_fails_it(machine):  # noqa: F811
    """A machine logged in by itself, as in 0.13: the events are not reported and the agent is not stopped; a turn that
    ends on the usage limit fails the run with cause rate_limit."""
    limit = {"status": "rejected", "resets_at": RESET_ISO}
    machine.scenarios({"1": [{"rate_limit": REJECTED}, {"fail": "Claude AI usage limit reached", "limit": limit}]})
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        assert await until_ended(hub, run_id) == "failed", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    assert hub.rate_limits == []
    run = hub.runs[run_id]
    assert run["failure_cause"] == "rate_limit" and "holds no account of the hub" in run["error"], run
    assert len(machine.starts()) == 1


def test_a_usage_report_alone_leaves_the_run_going(machine):  # noqa: F811
    machine.scenarios(
        {
            "1": [
                {"rate_limit": WARNING},
                {"write": {"a.txt": "a\n"}},
                {"result": {"verify_commands": ["test -f a.txt"]}},
            ]
        }
    )
    found = {}

    async def body(hub: FakeHub, daemon):
        run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
        hub.leases[run_id] = {"leases": [oauth(1, token(), "claude-a")], "missing": []}
        assert await until_ended(hub, run_id) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    hub = found["hub"]
    assert [(item["status"], item["utilization"]) for item in hub.rate_limits] == [("allowed_warning", 0.85)]
    assert len(machine.starts()) == 1 and hub.runs[found["run"]].get("failure_cause") is None

"""The core plugin pm on the hub (``evo_agents.core_plugins.pm``), as criteria a3 and a4 of plan pm-daily-tracking name
it: a project that enabled pm and tasks reads ``GET /v1/x/pm/<project>/daily`` and the tools ``pm__daily`` and
``pm__member``, which answer the same report from the hub's tables, through the label rules of the tasks routes. The
calendar is that of UTC with seven working days, so a task changed now falls in the window of tomorrow's report.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.core_plugins.pm import MANIFEST
from evo_agents.hub import plugin_calls
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.test_mcp import call
from tests.hub.test_plugin_points import rpc, tool_names
from tests.hub.test_runs import PLAN, PROJECT, dispatched
from tests.hub.test_tasks_blockers import ask
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    CORE,
    configure,
    created,
    edited,
    enable,
    hub,
    members,
    set_step,
    sprint,
)

PM = "/v1/x/pm"
BASE = f"{PM}/{PROJECT}"
POINTS = {**CORE, "pm": EntryPoint("pm", "evo_agents.core_plugins.pm:MANIFEST", ENTRY_POINT_GROUP)}
KEYS = {
    "project",
    "date",
    "previous_working_day",
    "done_use_cases",
    "done_tasks",
    "sprint",
    "late",
    "review",
    "stale",
    "blocked",
    "escalate",
    "load",
}
EVERY_DAY = {"timezone": "UTC", "working_days": [1, 2, 3, 4, 5, 6, 7], "pm": "owner"}


@pytest.fixture
def pm_core(monkeypatch):
    """The entry points of the core plugins with srs's, tasks's and pm's."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in POINTS]
    points += list(POINTS.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, pm_core):
    with TestClient(
        create_app(live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))), base_url="https://hub.test"
    ) as client:
        yield client


def tomorrow() -> str:
    return (datetime.now(UTC).date() + timedelta(days=1)).isoformat()


def daily(client, headers, expect: int = 200, **query) -> dict:
    answer = client.get(f"{BASE}/daily", params=query, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


@pytest.fixture
def pm_hub(client, hub) -> dict:
    """``hub`` with plugin pm enabled and counting in UTC every day of the week."""
    enable(client, hub["owner"], "pm", MANIFEST["permissions"])
    configure(client, hub["owner"], "pm", EVERY_DAY)
    return hub


def test_pm_daily_manifest_asks_for_notifications_and_chats_and_the_config_has_the_defaults():
    assert MANIFEST["permissions"] == ["notifications:send", "chats:write"]
    config = MANIFEST["config"]["properties"]
    assert set(config) == {
        "pm",
        "timezone",
        "working_days",
        "stale_days",
        "escalate_after_days",
        "at_risk_days",
        "wip_limit",
        "digest_chat",
    }
    assert config["timezone"]["default"] == "Asia/Ho_Chi_Minh" and config["working_days"]["default"] == [1, 2, 3, 4, 5]
    assert (config["stale_days"]["default"], config["escalate_after_days"]["default"]) == (2, 1)
    assert (config["at_risk_days"]["default"], config["wip_limit"]["default"]) == (2, 2)
    assert {tool["name"] for tool in MANIFEST["contributes"]["mcp_tools"]} == {"daily", "member"}


def test_pm_daily_access_a_project_without_pm_or_a_member_without_a_grant_gets_404(client, hub, github):
    assert daily(client, hub["alice"], 404)["message"]
    enable(client, hub["owner"], "pm", MANIFEST["permissions"])
    assert set(daily(client, hub["eve"], date=tomorrow())) >= KEYS  # a reader reads
    stranger = live.bearer(live.sign_in(client, github, "stranger", 777)["token"])
    assert client.get(f"{BASE}/daily", headers=stranger).status_code == 404


def test_pm_daily_refuses_with_a_reason_when_tasks_is_off_or_the_time_zone_is_invalid(client, members):
    enable(client, members["owner"], "pm", MANIFEST["permissions"])
    off = daily(client, members["alice"], 409)
    assert "plugin tasks is not enabled" in off["message"]
    enable(client, members["owner"], "tasks", ["plans:read"])
    assert set(daily(client, members["alice"])) >= KEYS
    configure(client, members["owner"], "pm", {"timezone": "Mars/Olympus"})
    assert "no IANA time zone" in daily(client, members["alice"], 409)["message"]
    assert daily(client, members["alice"], 422, date="not-a-date")


def test_pm_daily_reports_the_day_from_the_hubs_tables(client, pm_hub):
    alice, owner = pm_hub["alice"], pm_hub["owner"]
    yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
    sprint(client, alice, "Sprint 7", "active")
    created(client, alice, title="Cancel an order", status="in_progress", assignees=["alice"], sprint="Sprint 7")
    created(client, alice, title="Pay by card", status="todo", due_on=yesterday, assignees=["bao"])
    finished = created(client, alice, title="Show the cart", links={"use_cases": ["UC ORD-1"]})
    edited(client, alice, finished["ref"], status="done")
    created(client, alice, title="Reviewed", status="in_review", roles={"qa": ["bao"]})
    blocked = created(client, alice, title="Waits on legal", status="in_progress", assignees=["alice"])
    answer = client.post(
        f"/v1/x/tasks/{PROJECT}/tasks/{blocked['ref']}/blockers",
        json={"reason": "legal review", "waiting_text": "the bank"},
        headers=alice,
    )
    assert answer.status_code == 201, answer.text

    got = daily(client, owner, date=tomorrow())
    assert set(got) >= KEYS and got["project"] == PROJECT
    assert got["previous_working_day"] == datetime.now(UTC).date().isoformat()
    assert [t["ref"] for t in got["done_tasks"]] == ["T-3"]
    assert [c["code"] for c in got["done_use_cases"]] == ["UC ORD-1"]
    assert got["sprint"]["name"] == "Sprint 7" and got["sprint"]["scope"] == 0 and got["sprint"]["added"] == 1
    assert [(t["ref"], t["kind"]) for t in got["late"]] == [("T-2", "overdue")]
    assert [t["ref"] for t in got["review"]] == ["T-4"]
    assert [(b["ref"], b["waiting_on"]) for b in got["blocked"]] == [("T-5", "the bank")]
    assert got["escalate"] == [] and got["stale"] == []
    load = {m["login"]: m for m in got["load"]}
    assert load["alice"]["doing"] == 2 and load["alice"]["overloaded"] is False and load["bao"]["empty"] is True

    today = daily(client, owner)  # the default date is today in the project's zone
    assert today["date"] == datetime.now(UTC).date().isoformat() and today["done_tasks"] == []
    later = daily(client, owner, date=(datetime.now(UTC).date() + timedelta(days=4)).isoformat())
    assert [t["ref"] for t in later["stale"]] == ["T-1", "T-5"]  # four days idle, more than the two it allows
    assert [e["ref"] for e in later["escalate"]] == ["T-5"]
    assert later["escalate"][0]["escalate_to"] == "owner"


def test_pm_daily_reads_the_decisions_runs_and_plan_steps_that_belong_to_a_task(client, pm_hub, hub_db):
    owner, alice = pm_hub["owner"], pm_hub["alice"]
    [run] = dispatched(client, owner, [2])
    created(client, alice, title="By run", status="in_progress", links={"runs": [run["id"]]})
    created(client, alice, title="By step", status="in_progress", links={"steps": [f"{PLAN}#2"]})
    created(client, alice, title="By another step", status="in_progress", links={"steps": [f"{PLAN}#1"]})
    set_step(client, owner, 1, "done")
    decision = ask(hub_db, run["id"])
    got = daily(client, owner, date=(datetime.now(UTC).date() + timedelta(days=4)).isoformat())
    asked = [(b["ref"], b["kind"], b["waiting_on"], b["decision_id"], b["run_id"]) for b in got["blocked"]]
    assert asked == [
        ("T-1", "decision", "owner", decision, run["id"]),
        ("T-2", "decision", "owner", decision, run["id"]),
    ]
    assert [e["ref"] for e in got["escalate"]] == ["T-1", "T-2"]
    stale = {t["ref"]: t["source"] for t in got["stale"]}
    assert set(stale) == {"T-1", "T-2", "T-3"} and set(stale.values()) <= {"history", "run", "plan_step", "comment"}
    answered = client.post(f"/v1/projects/{PROJECT}/decisions/{decision}/answer", json={"option": "yes"}, headers=owner)
    assert answered.status_code == 200, answered.text
    assert daily(client, owner, date=tomorrow())["blocked"] == []


def test_pm_daily_label_hides_what_the_reader_may_not_see_and_counts_it(client, pm_hub):
    alice = pm_hub["alice"]
    created(client, alice, title="Internal secret", status="in_progress", assignees=["alice"], due_on="2020-01-01")
    seen = daily(client, pm_hub["eve"], date=tomorrow())
    assert [t["ref"] for t in seen["late"]] == ["T-1"]
    blind = daily(client, pm_hub["outsider"], date=tomorrow())  # a reader up to public: the task is internal
    assert blind["late"] == [] and blind["hidden"]["late"] == 1
    load = {m["login"]: m for m in blind["load"]}
    assert load["alice"]["doing"] == 1 and load["alice"]["doing_tasks"] == []
    assert "Internal secret" not in json.dumps(blind)
    named = {**pm_hub["eve"], "X-Evo-Sink": "claude-code@anthropic"}
    assert [t["ref"] for t in daily(client, named, date=tomorrow())["late"]] == ["T-1"]
    unknown = daily(client, {**pm_hub["eve"], "X-Evo-Sink": "no-such-sink"}, date=tomorrow())
    assert unknown["late"] == [] and unknown["hidden"]["late"] == 1  # a sink the project does not know shows nothing


def mcp(client, headers, name: str, arguments: dict | None = None) -> tuple[dict | str, bool]:
    result = rpc(client, headers, call(9, name, arguments or {}))
    text = result["content"][0]["text"]
    return (text if result["isError"] else json.loads(text)), result["isError"]


def test_pm_tools_answer_the_route_data_for_the_caller(client, pm_hub):
    alice = pm_hub["alice"]
    created(client, alice, title="Cancel an order", status="in_progress", assignees=["alice"], due_on="2020-01-01")
    got, error = mcp(client, alice, "pm__daily", {"date": tomorrow()})
    assert not error and got == daily(client, alice, date=tomorrow())
    assert "pm__daily" in tool_names(client, alice) and "pm__member" in tool_names(client, alice)

    mine, error = mcp(client, alice, "pm__member", {"login": "alice", "date": tomorrow()})
    assert not error and [t["ref"] for t in mine["doing"]] == ["T-1"] and [t["ref"] for t in mine["late"]] == ["T-1"]
    other, _ = mcp(client, alice, "pm__member", {"login": "bao"})
    assert other["doing"] == [] and other["login"] == "bao"
    blind, _ = mcp(client, pm_hub["outsider"], "pm__member", {"login": "alice", "date": tomorrow()})
    assert blind["doing"] == [] and blind["hidden"]["doing"] == 1
    text, error = mcp(client, alice, "pm__member", {"login": "nobody"})
    assert error and "no member nobody" in text
    text, error = mcp(client, alice, "pm__daily", {"date": "2026-02-30"})
    assert error and "date" in text

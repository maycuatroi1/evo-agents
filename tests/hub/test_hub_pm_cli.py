"""``evo-agents hub pm daily`` (a manifest command of the core plugin pm, ``evo_agents/core_plugins/pm/cli.json``), as criterion a4 of plan pm-daily-tracking names it: the
command and its ``--json`` keys are in the contract (seam hub-cli-v1), and a member runs it against a hub that serves
the core plugin pm: the report of a day as a table and as the JSON the route answers, a refusal as an error line.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_agents.core_plugins.pm import MANIFEST
from evo_agents.hub.contract import commands
from tests.hub import pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_hub_task_cli import PROJECT, hub, needs_pg, task  # noqa: F401 (fixtures)
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

if pg.DSN:
    import httpx


def test_hub_pm_cli_daily_is_in_the_contract():
    printed = commands()["hub pm daily"]
    assert (printed["json"]["kind"], printed["json"]["schema"]) == ("object", "PmDaily")
    options = {o["flags"][0]: o for o in printed["options"]}
    assert options["--project"]["required"] and not options["--date"]["required"] and "--json" in options
    assert {"done_use_cases", "done_tasks", "sprint", "late", "review", "stale", "blocked", "escalate", "load"} <= set(
        printed["json"]["keys"]
    )


def test_hub_pm_cli_daily_needs_a_project(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    with pytest.raises(SystemExit) as stopped:
        run_cli(monkeypatch, capsys, home, "hub", "pm", "daily")
    assert stopped.value.code == 2 and "--project" in capsys.readouterr().err


def enable_pm(hub) -> None:
    token = (hub.homes["owner"] / ".evo" / "hub" / "token").read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {token}"}
    body = {"permissions": MANIFEST["permissions"]}
    enabled = httpx.post(f"{hub.url}/v1/projects/{PROJECT}/plugins/pm/enable", json=body, headers=headers)
    assert enabled.status_code == 200, enabled.text
    values = {"timezone": "UTC", "working_days": [1, 2, 3, 4, 5, 6, 7], "pm": "owner"}
    written = httpx.post(f"{hub.url}/v1/projects/{PROJECT}/plugins/pm/config", json={"values": values}, headers=headers)
    assert written.status_code == 200, written.text


def pm(hub, monkeypatch, capsys, who: str, *args: str):
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "pm", "daily", "--project", PROJECT, *args)


@needs_pg
def test_hub_pm_cli_daily_prints_the_report_as_a_table_and_as_json(hub, monkeypatch, capsys):
    refused = pm(hub, monkeypatch, capsys, "alice")
    assert refused.code == 1 and "pm is not enabled" in refused.err
    enable_pm(hub)
    new = ("create", "--title", "Cancel an order", "--status", "in_progress", "--assignee", "alice")
    ok(task(hub, monkeypatch, capsys, "alice", *new, "--due", "2020-01-01"))
    ok(
        task(
            hub, monkeypatch, capsys, "alice", "block", "T-1", "--waiting-on", "the bank", "--text", "--reason", "legal"
        )
    )
    tomorrow = (datetime.now(UTC).date() + timedelta(days=4)).isoformat()

    report = as_json(pm(hub, monkeypatch, capsys, "bao", "--date", tomorrow, "--json"))
    assert_json_keys("hub pm daily", report)
    assert report["date"] == tomorrow and [t["ref"] for t in report["late"]] == ["T-1"]
    assert [b["ref"] for b in report["escalate"]] == ["T-1"]

    table = ok(pm(hub, monkeypatch, capsys, "bao", "--date", tomorrow)).out
    assert f"{PROJECT}  {tomorrow}" in table and "T-1" in table and "waiting on the bank: legal" in table
    assert "overdue" not in table and "working days late" in table
    assert "alice" in table and "Escalate:" in table

    today = as_json(pm(hub, monkeypatch, capsys, "alice", "--json"))
    assert today["date"] == datetime.now(UTC).date().isoformat()
    bad = pm(hub, monkeypatch, capsys, "alice", "--date", "tomorrow")
    assert bad.code == 1 and bad.err.startswith("error:")

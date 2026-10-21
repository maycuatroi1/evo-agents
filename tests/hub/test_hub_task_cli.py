"""``evo-agents hub task list|show|create|edit|import`` (``evo_agents.hub.cli_tasks``), as members run them against a
hub that serves the core plugin tasks: the commands and their ``--json`` keys are in the contract (seam hub-cli-v1),
a task is created with its people and links, shown with the status of its plan step, changed, and evo-tasks/1 JSON
imports after a dry run (criterion a3 of plan srs-tasks-plugins).

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

SAMPLE_SRS = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
SAMPLE_TASKS = Path(__file__).parent / "fixtures" / "tasks" / "demo-shop-tasks.json"
PROJECT = "evo-agents"
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_hub_task_commands_are_in_the_contract():
    printed = commands()
    names = {f"hub task {verb}" for verb in ("list", "show", "create", "edit", "import")}
    assert names <= set(printed)
    listed = printed["hub task list"]["json"]
    assert (listed["kind"], listed["schema"]) == ("array", "TasksItem")
    for verb in ("show", "create", "edit"):
        assert printed[f"hub task {verb}"]["json"]["schema"] == "TasksItemDetail"
    assert printed["hub task import"]["json"]["schema"] == "TasksImportResult"
    options = {o["flags"][0]: o for o in printed["hub task create"]["options"]}
    assert options["--title"]["required"] and options["--project"]["required"]
    assert options["--priority"]["choices"] == ["urgent", "high", "medium", "low"]
    block = {o["flags"][0]: o for o in printed["hub task block"]["options"]}
    assert block["--waiting-on"]["required"] and block["--reason"]["required"] and not block["--text"]["required"]
    assert (
        printed["hub task block"]["json"]["schema"] == printed["hub task unblock"]["json"]["schema"] == "TasksBlocker"
    )
    assert printed["hub task unblock"]["json"]["kind"] == "array"
    flags = {o["flags"][0] for o in printed["hub task list"]["options"]}
    assert "--blocked" in flags and "blocked" in printed["hub task list"]["json"]["keys"]


def test_hub_task_edit_takes_a_value_or_its_clearing_flag_not_both(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    args = ("hub", "task", "edit", "T-1", "--project", PROJECT, "--due", "2026-10-30", "--no-due")
    result = run_cli(monkeypatch, capsys, home, *args)
    assert result.code == 2 and "--no-due and the value it clears go apart" in result.err


def test_hub_task_role_takes_role_equals_login(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    args = ("hub", "task", "create", "--project", PROJECT, "--title", "x", "--role", "boss=alice")
    result = run_cli(monkeypatch, capsys, home, *args)
    assert result.code == 1 and "--role takes ROLE=LOGIN with ROLE one of ba, qa, dev" in result.err


if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import PLAN, plan_body


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, its project with plugins tasks and srs enabled, the sample SRS and the plan rollout,
    alice and bao writers, owner its admin, and a home signed in for each."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=admin).status_code == 200
        homes, headers = {}, {}
        for login, github_id, role in (("owner", 601, "admin"), ("alice", 602, "writer"), ("bao", 603, "writer")):
            token = live.sign_in(client, github, login, github_id)["token"]
            grant = {"role": role, "max_level": "internal"}
            granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
            assert granted.status_code == 200, granted.text
            homes[login], headers[login] = write_credentials(tmp_path / login, url, login, token), live.bearer(token)
        for plugin, permissions in (("tasks", ["plans:read"]), ("srs", [])):
            body = {"permissions": permissions}
            enabled = client.post(
                f"/v1/projects/{PROJECT}/plugins/{plugin}/enable", json=body, headers=headers["owner"]
            )
            assert enabled.status_code == 200, enabled.text
        body = {"markdown": SAMPLE_SRS.read_text(encoding="utf-8"), "slug": "demo-shop"}
        assert client.post(f"/v1/x/srs/{PROJECT}/import", json=body, headers=headers["alice"]).status_code == 200
        pushed = client.put(
            f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=headers["owner"]
        )
        assert pushed.status_code == 200, pushed.text
        yield SimpleNamespace(url=url, homes=homes)


def task(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "task", *args, "--project", PROJECT)


@needs_pg
def test_hub_task_create_show_edit_and_import_from_the_command_line(hub, monkeypatch, capsys):
    new = (
        "create",
        "--title",
        "Cancel an order",
        "--use-case",
        "UC ORD-1",
        "--step",
        f"{PLAN}#2",
        "--priority",
        "high",
    )
    made = as_json(task(hub, monkeypatch, capsys, "alice", *new, "--assignee", "alice", "--role", "qa=bao", "--json"))
    assert_json_keys("hub task create", made)
    assert (made["ref"], made["assignees"], made["roles"]["qa"]) == ("T-1", ["alice"], ["bao"])

    listed = as_json(task(hub, monkeypatch, capsys, "bao", "list", "--mine", "--json"))
    assert_json_keys("hub task list", listed)
    assert [t["ref"] for t in listed] == ["T-1"]
    table = ok(task(hub, monkeypatch, capsys, "bao", "list")).out
    assert "T-1" in table and "backlog" in table and "0/1" in table and "Cancel an order" in table
    shown = ok(task(hub, monkeypatch, capsys, "bao", "show", "T-1")).out
    assert "T-1 [task] Cancel an order" in shown and "UC ORD-1: Place an order (demo-shop)" in shown
    assert f"plan {PLAN} step 2: Queue, pending" in shown and "alice created T-1" in shown
    detail = as_json(task(hub, monkeypatch, capsys, "bao", "show", "1", "--json"))
    assert_json_keys("hub task show", detail)

    change = ("edit", "T-1", "--status", "in_review", "--no-assignee", "--role", "qa=", "--role", "dev=bao")
    changed = as_json(task(hub, monkeypatch, capsys, "bao", *change, "--pull", "https://x.example/pull/1", "--json"))
    assert_json_keys("hub task edit", changed)
    assert (changed["status"], changed["assignees"], changed["roles"]) == (
        "in_review",
        [],
        {"ba": [], "qa": [], "dev": ["bao"]},
    )
    assert [link["kind"] for link in changed["links"]] == ["srs_item", "plan_step", "pull"]
    refused = task(hub, monkeypatch, capsys, "bao", "edit", "T-1", "--status", "shipped")
    assert refused.code == 1 and "no status shipped in this project" in refused.err

    preview = ok(task(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE_TASKS), "--dry-run")).out
    assert preview.startswith("Would import 3 tasks, 1 sprints, 3 links.")
    assert "warning n-002: UC SHIP-1 is not linked" in preview
    done = as_json(task(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE_TASKS), "--json"))
    assert_json_keys("hub task import", done)
    assert done["created"] == ["T-2", "T-3", "T-4"]
    sprint = as_json(task(hub, monkeypatch, capsys, "bao", "list", "--sprint", "Sprint 7", "--json"))
    assert [t["ref"] for t in sprint] == ["T-2"]


@needs_pg
def test_hub_task_cli_blocks_and_unblocks_a_task_on_a_task_a_member_or_a_text(hub, monkeypatch, capsys):
    for title in ("Cancel an order", "Payment webhook"):
        ok(task(hub, monkeypatch, capsys, "alice", "create", "--title", title))
    reason = ("--reason", "needs it first")
    on_task = as_json(task(hub, monkeypatch, capsys, "alice", "block", "T-1", "--waiting-on", "t-2", *reason, "--json"))
    assert_json_keys("hub task block", on_task)
    assert (on_task["waiting_kind"], on_task["waiting_on"]) == ("task", "T-2")
    on_member = as_json(task(hub, monkeypatch, capsys, "bao", "block", "1", "--waiting-on", "bao", *reason, "--json"))
    assert (on_member["waiting_kind"], on_member["waiting_on"]) == ("member", "bao")
    on_text = as_json(
        task(hub, monkeypatch, capsys, "alice", "block", "T-1", "--waiting-on", "the bank", *reason, "--json")
    )
    assert (on_text["waiting_kind"], on_text["waiting_on"]) == ("text", "the bank")  # nobody of that login or ref
    forced = task(
        hub, monkeypatch, capsys, "alice", "block", "T-1", "--waiting-on", "alice", "--text", *reason, "--json"
    )
    assert (as_json(forced)["waiting_kind"], as_json(forced)["waiting_on"]) == ("text", "alice")
    said = ok(task(hub, monkeypatch, capsys, "alice", "block", "T-2", "--waiting-on", "T-1", *reason)).out
    assert "Blocked T-2: waiting on task T-1" in said

    listed = as_json(task(hub, monkeypatch, capsys, "bao", "list", "--blocked", "--json"))
    assert_json_keys("hub task list", listed)
    assert [t["ref"] for t in listed] == ["T-1", "T-2"] and all(t["blocked"] for t in listed)
    table = ok(task(hub, monkeypatch, capsys, "bao", "list", "--blocked")).out
    assert "backlog (blocked)" in table
    shown = ok(task(hub, monkeypatch, capsys, "bao", "show", "T-1")).out
    assert "Blocked #" in shown and "waiting on member bao" in shown and "waiting on text the bank" in shown
    detail = as_json(task(hub, monkeypatch, capsys, "bao", "show", "T-1", "--json"))
    assert_json_keys("hub task show", detail)
    assert len(detail["blockers"]) == 4

    one = as_json(task(hub, monkeypatch, capsys, "bao", "unblock", "T-1", "--blocker", str(on_member["id"]), "--json"))
    assert_json_keys("hub task unblock", one)
    assert [b["id"] for b in one] == [on_member["id"]] and one[0]["cleared_by"] == "bao"
    every = ok(task(hub, monkeypatch, capsys, "alice", "unblock", "T-1")).out
    assert every.startswith("Cleared 3 blocker(s) of T-1")
    assert "has no blocker set by hand" in ok(task(hub, monkeypatch, capsys, "alice", "unblock", "T-1")).out
    still = as_json(task(hub, monkeypatch, capsys, "bao", "list", "--blocked", "--json"))
    assert [t["ref"] for t in still] == ["T-2"]
    refused = task(hub, monkeypatch, capsys, "alice", "block", "T-1", "--waiting-on", "T-1", *reason)
    assert refused.code == 1 and "never waits on itself" in refused.err


@needs_pg
def test_hub_task_outcome_is_set_on_create_and_edit_and_read_from_stdin(hub, monkeypatch, capsys):
    made = as_json(
        task(hub, monkeypatch, capsys, "alice", "create", "--title", "Cancel", "--outcome", "Done.", "--json")
    )
    assert_json_keys("hub task create", made)
    assert made["outcome_md"] == "Done."
    stdin = io.StringIO("Orders cancel in one tap.\n\nSecond line.\n")
    piped = run_cli(
        monkeypatch,
        capsys,
        hub.homes["alice"],
        "hub",
        "task",
        "edit",
        "T-1",
        "--outcome",
        "-",
        "--project",
        PROJECT,
        "--json",
        stdin=stdin,
    )
    assert as_json(piped)["outcome_md"] == "Orders cancel in one tap.\n\nSecond line.\n"
    shown = ok(task(hub, monkeypatch, capsys, "bao", "show", "T-1")).out
    assert "  Outcome:\n  | Orders cancel in one tap.\n" in shown and "alice outcome" in shown
    cleared = as_json(task(hub, monkeypatch, capsys, "alice", "edit", "T-1", "--outcome", "", "--json"))
    assert cleared["outcome_md"] == ""

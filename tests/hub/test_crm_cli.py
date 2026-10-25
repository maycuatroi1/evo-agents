"""``evo-agents hub crm list|show|delete`` (manifest commands of the core plugin crm, ``evo_agents/core_plugins/crm/cli.json``): the commands and their ``--json`` keys are in the
contract (seam hub-cli-v1), and a member runs them against a hub that serves the core plugin crm: the contacts with
the count of each stage, one contact with its touchpoints, and a deletion that an admin makes and a writer is refused.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_agents.hub.contract import commands
from tests.hub import pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_hub_task_cli import PROJECT, hub, needs_pg  # noqa: F401 (fixtures)
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

if pg.DSN:
    import httpx
    from sqlalchemy import insert, select

    from evo_agents.core_plugins.crm import tables as crm_tables
    from evo_agents.hub import tables
    from tests.hub.live import sql

NOW = datetime.now(UTC)


def test_hub_crm_cli_commands_are_in_the_contract():
    printed = commands()
    assert {f"hub crm {verb}" for verb in ("list", "show", "delete")} <= set(printed)
    assert (printed["hub crm list"]["json"]["kind"], printed["hub crm list"]["json"]["schema"]) == (
        "object",
        "CrmContacts",
    )
    assert printed["hub crm show"]["json"]["schema"] == "CrmContactDetail"
    assert printed["hub crm delete"]["json"]["schema"] == "CrmDeleted"
    options = {o["flags"][0]: o for o in printed["hub crm list"]["options"]}
    assert options["--project"]["required"] and options["--stage"]["choices"] == [
        "lead",
        "invited",
        "activated",
        "retained",
    ]
    assert not options["--source"]["required"] and "--json" in options
    assert {"login", "profile_url", "touchpoints", "history"} <= set(printed["hub crm show"]["json"]["keys"])


def test_hub_crm_cli_delete_asks_for_yes_before_it_deletes(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    result = run_cli(monkeypatch, capsys, home, "hub", "crm", "delete", "dung", "--project", PROJECT)
    assert result.code == 2 and "--yes" in result.err


def test_hub_crm_cli_list_needs_a_project(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    with pytest.raises(SystemExit) as stopped:
        run_cli(monkeypatch, capsys, home, "hub", "crm", "list")
    assert stopped.value.code == 2 and "--project" in capsys.readouterr().err


def enable_crm(hub) -> None:
    token = (hub.homes["owner"] / ".evo" / "hub" / "token").read_text(encoding="utf-8").strip()
    enabled = httpx.post(
        f"{hub.url}/v1/projects/{PROJECT}/plugins/crm/enable",
        json={"permissions": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert enabled.status_code == 200, enabled.text


def contact(db, login: str, stage: str, source: str, ref: str, url: str) -> None:
    project_id = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    made = insert(crm_tables.contacts).values(
        project_id=project_id,
        login=login,
        profile_url=f"https://github.com/{login}",
        source=source,
        stage=stage,
        label={"level": "internal", "location": "any"},
    )
    contact_id = sql(db, made.returning(crm_tables.contacts.c.id))[0][0]
    sql(
        db,
        insert(crm_tables.touchpoints).values(contact_id=contact_id, kind=source, ref=ref, url=url, happened_at=NOW),
    )
    sql(db, insert(crm_tables.stage_history).values(contact_id=contact_id, stage="lead", reached_at=NOW))


def crm(hub, monkeypatch, capsys, who: str, *args: str):
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "crm", *args, "--project", PROJECT)


@needs_pg
def test_hub_crm_cli_lists_shows_and_deletes_contacts(hub, hub_db, monkeypatch, capsys):
    refused = crm(hub, monkeypatch, capsys, "alice", "list")
    assert refused.code == 1 and "crm is not enabled" in refused.err
    enable_crm(hub)
    contact(hub_db, "dung", "lead", "issue", "issue#1", "https://github.com/acme/widgets/issues/1")
    contact(hub_db, "giang", "invited", "invite_request", "issue#2", "https://github.com/acme/widgets/issues/2")

    found = as_json(crm(hub, monkeypatch, capsys, "bao", "list", "--json"))
    assert_json_keys("hub crm list", found)
    assert found["project"] == PROJECT and found["counts"] == {"lead": 1, "invited": 1, "activated": 0, "retained": 0}
    assert {c["login"] for c in found["contacts"]} == {"dung", "giang"}
    only = as_json(crm(hub, monkeypatch, capsys, "bao", "list", "--stage", "invited", "--json"))
    assert [c["login"] for c in only["contacts"]] == ["giang"]
    table = ok(crm(hub, monkeypatch, capsys, "bao", "list")).out
    assert "lead 1  invited 1  activated 0  retained 0" in table and "dung" in table and "invite_request" in table

    one = as_json(crm(hub, monkeypatch, capsys, "bao", "show", "dung", "--json"))
    assert_json_keys("hub crm show", one)
    assert one["profile_url"] == "https://github.com/dung" and one["touchpoints"][0]["ref"] == "issue#1"
    assert "issues/1" in ok(crm(hub, monkeypatch, capsys, "bao", "show", "dung")).out
    missing = crm(hub, monkeypatch, capsys, "bao", "show", "nobody")
    assert missing.code == 1 and missing.err.startswith("error:")

    denied = crm(hub, monkeypatch, capsys, "alice", "delete", "dung", "--yes")
    assert denied.code == 1 and "admin" in denied.err
    gone = as_json(crm(hub, monkeypatch, capsys, "owner", "delete", "dung", "--yes", "--json"))
    assert_json_keys("hub crm delete", gone)
    assert gone == {"deleted": True, "touchpoints": 1}
    assert [c["login"] for c in as_json(crm(hub, monkeypatch, capsys, "bao", "list", "--json"))["contacts"]] == [
        "giang"
    ]
    assert (
        "Deleted giang and 1 touchpoints" in ok(crm(hub, monkeypatch, capsys, "owner", "delete", "giang", "--yes")).out
    )

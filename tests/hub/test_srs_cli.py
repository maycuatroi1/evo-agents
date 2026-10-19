"""``evo-agents hub srs list|show|import|export|submit|approve`` (``evo_agents.hub.cli_srs``), as members run them
against a hub that serves the core plugin srs: the commands and their ``--json`` keys are in the contract (seam
hub-cli-v1), an import prints its preview and the problems that refuse it, an export writes the Markdown imported, and
approving follows the hub's rules (criterion a2 of plan srs-tasks-plugins).

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.core_plugins.srs import markdown
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

SAMPLE = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
PROJECT = "evo-agents"
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_the_srs_commands_are_in_the_contract():
    printed = commands()
    names = {f"hub srs {verb}" for verb in ("list", "show", "import", "export", "submit", "approve")}
    assert names <= set(printed)
    listed = printed["hub srs list"]["json"]
    assert (listed["kind"], listed["schema"]) == ("array", "SrsDocumentSummary")
    assert printed["hub srs import"]["json"]["schema"] == "SrsImportResult"
    assert printed["hub srs show"]["json"]["with"]["--item"]["schema"] == "SrsItemDetail"
    options = {o["flags"][0]: o for o in printed["hub srs approve"]["options"]}
    assert options["--role"]["choices"] == ["ba", "qa", "dev"] and options["--project"]["required"]
    assert printed["hub srs export"]["json"] is None


def test_srs_import_takes_no_slug_or_title_with_doc(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    args = ("hub", "srs", "import", str(SAMPLE), "--project", PROJECT, "--doc", "a", "--slug", "b")
    result = run_cli(monkeypatch, capsys, home, *args)
    assert result.code == 2 and "--doc imports the next version" in result.err


if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, its project with plugin srs enabled, alice and bao writers, owner its admin, and a home
    signed in for each."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=admin).status_code == 200
        homes = {}
        for login, github_id, role in (("owner", 601, "admin"), ("alice", 602, "writer"), ("bao", 603, "writer")):
            token = live.sign_in(client, github, login, github_id)["token"]
            grant = {"role": role, "max_level": "internal"}
            assert (
                client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin).status_code == 200
            )
            homes[login] = write_credentials(tmp_path / login, url, login, token)
            if login == "owner":
                enabled = client.post(
                    f"/v1/projects/{PROJECT}/plugins/srs/enable", json={"permissions": []}, headers=live.bearer(token)
                )
                assert enabled.status_code == 200, enabled.text
        yield SimpleNamespace(url=url, homes=homes)


def srs(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "srs", *args, "--project", PROJECT)


@needs_pg
def test_srs_import_export_submit_and_approve_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    preview = ok(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop", "--dry-run"))
    assert preview.out.startswith("Preview of demo-shop: ") and "3 use case" in preview.out
    done = as_json(srs(hub, monkeypatch, capsys, "alice", "import", str(SAMPLE), "--slug", "demo-shop", "--json"))
    assert_json_keys("hub srs import", done)
    assert (done["document"], done["created"], done["version"]) == ("demo-shop", True, 1)

    listed = as_json(srs(hub, monkeypatch, capsys, "bao", "list", "--json"))
    assert_json_keys("hub srs list", listed)
    assert [(d["slug"], d["latest_version"], d["latest_state"]) for d in listed] == [("demo-shop", 1, "draft")]
    shown = ok(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop"))
    assert "v1 draft by alice" in shown.out and "UC ORD-1: Place an order" in shown.out
    detail = as_json(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop", "--item", "UC ORD-1", "--json"))
    assert_json_keys("hub srs show", detail, "--item")
    assert [child["code"] for child in detail["children"]] == ["BR ORD1-1", "BR ORD1-2"]
    document = as_json(srs(hub, monkeypatch, capsys, "bao", "show", "demo-shop", "--json"))
    assert_json_keys("hub srs show", document)

    target = tmp_path / "out.md"
    ok(srs(hub, monkeypatch, capsys, "bao", "export", "demo-shop", "--no-anchors", "--output", str(target)))
    assert target.read_text(encoding="utf-8") == markdown.normalize(SAMPLE.read_text(encoding="utf-8"))
    printed = ok(srs(hub, monkeypatch, capsys, "bao", "export", "demo-shop"))
    assert "<!-- srs: SCR-1 -->" in printed.out

    again = srs(hub, monkeypatch, capsys, "bao", "import", str(target), "--doc", "demo-shop")
    assert again.code == 1 and "nothing changed since version 1" in again.err
    taken = tmp_path / "taken.md"
    taken.write_text("# 1. More\n\n### UC ORD-1: Again\n\n| Item | Content |\n|---|---|\n| Actor | Clerk |\n")
    refused = srs(hub, monkeypatch, capsys, "bao", "import", str(taken), "--slug", "more")
    assert refused.code == 1 and "error: code_taken UC ORD-1 line 3:" in refused.err

    submitted = as_json(srs(hub, monkeypatch, capsys, "alice", "submit", "demo-shop", "1", "--json"))
    assert_json_keys("hub srs submit", submitted)
    assert submitted["state"] == "in_review"
    own = srs(hub, monkeypatch, capsys, "alice", "approve", "demo-shop", "1")
    assert own.code == 1 and "another member reviews it" in own.err
    no_comment = srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--request-changes")
    assert no_comment.code == 2
    approved = as_json(srs(hub, monkeypatch, capsys, "bao", "approve", "demo-shop", "1", "--json"))
    assert_json_keys("hub srs approve", approved)
    assert approved["state"] == "approved"
    final = ok(srs(hub, monkeypatch, capsys, "owner", "list"))
    assert "demo-shop  srs   1       approved  1" in final.out

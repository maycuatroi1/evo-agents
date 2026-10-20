"""``evo-agents hub canvas list|show|create|edit|undo|evidence|export|import`` (``evo_agents.hub.cli_canvas``), as
members run them against a hub that serves the core plugin canvas: the commands and their ``--json`` keys are in the
contract (seam hub-cli-v1), a canvas is made, a claim made and changed at the rev it was read at, an edit at an old
rev is refused with what changed, a rev is undone, evidence added, and the canvas exported and imported back keeping
its codes (criterion a2 of plan living-lean-canvas). The product, Northwind Pantry, is made up.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

PROJECT = "evo-agents"
SLUG = "northwind-pantry"
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_hub_canvas_commands_are_in_the_contract():
    printed = commands()
    verbs = ("list", "show", "create", "edit", "undo", "export", "import", "evidence add", "evidence withdraw")
    assert {f"hub canvas {verb}" for verb in verbs} <= set(printed)
    assert (printed["hub canvas list"]["json"]["kind"], printed["hub canvas list"]["json"]["schema"]) == (
        "array",
        "CanvasSummary",
    )
    assert printed["hub canvas show"]["json"]["schema"] == "CanvasDetail"
    assert printed["hub canvas show"]["json"]["with"]["--claim"]["schema"] == "CanvasClaimDetail"
    for verb in ("edit", "undo"):
        assert printed[f"hub canvas {verb}"]["json"]["schema"] == "CanvasEditResult"
    assert printed["hub canvas import"]["json"]["schema"] == "CanvasImportResult"
    assert printed["hub canvas export"]["json"] is None  # it prints evo-canvas/1 JSON always, with no --json
    options = {o["flags"][0]: o for o in printed["hub canvas edit"]["options"]}
    assert options["--status"]["choices"] == ["assumption", "testing", "validated", "invalidated", "retired"]
    assert options["--project"]["required"] and not options["--base-rev"]["required"]


def test_hub_canvas_edit_takes_a_claim_with_its_base_rev_and_one_of_each_pair(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    both = run_cli(
        monkeypatch, capsys, home, "hub", "canvas", "edit", SLUG, "--project", PROJECT, "--archive", "--unarchive"
    )
    assert both.code == 2 and "--archive and --unarchive" in both.err
    args = ("hub", "canvas", "edit", SLUG, "--project", PROJECT, "--claim", "CL-1", "--text", "x")
    blind = run_cli(monkeypatch, capsys, home, *args)
    assert blind.code == 1 and "--claim takes --base-rev, the rev you read it at" in blind.err
    card = run_cli(monkeypatch, capsys, home, *args, "--base-rev", "1", "--test-card", "[1]")
    assert card.code == 1 and "--test-card takes a JSON object" in card.err


if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, its project with the plugin canvas enabled, owner its admin, alice and bao writers, and
    a home signed in for each."""
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
        body = {"permissions": []}
        enabled = client.post(f"/v1/projects/{PROJECT}/plugins/canvas/enable", json=body, headers=headers["owner"])
        assert enabled.status_code == 200, enabled.text
        yield SimpleNamespace(url=url, homes=homes)


def canvas(hub, monkeypatch, capsys, who: str, *args: str) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "canvas", *args, "--project", PROJECT)


@needs_pg
def test_hub_canvas_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    run = lambda who, *args: canvas(hub, monkeypatch, capsys, who, *args)  # noqa: E731
    made = as_json(run("alice", "create", SLUG, "--name", "Northwind Pantry", "--json"))
    assert_json_keys("hub canvas create", made)
    new = ("edit", SLUG, "--cell", "problem", "--text", "Corner shops run out of stock", "--json")
    first = as_json(run("alice", *new))
    assert_json_keys("hub canvas edit", first)
    assert [(c["code"], c["rev"]) for c in first["claims"]] == [("CL-1", 1)]
    listed = as_json(run("bao", "list", "--json"))
    assert_json_keys("hub canvas list", listed)
    assert "northwind-pantry  1" in ok(run("bao", "list")).out

    change = ("edit", SLUG, "--claim", "CL-1", "--base-rev", "1", "--status", "testing")
    card = json.dumps({"hypothesis": "Owners reorder weekly", "test": "Ask ten owners", "deadline": "2026-11-30"})
    assert "CL-1 [testing, medium, rev 2]" in ok(run("bao", *change, "--test-card", card, "--reason", "asked")).out
    stale = run("alice", "edit", SLUG, "--claim", "CL-1", "--base-rev", "1", "--text", "An older read")
    assert stale.code == 1 and "CL-1 changed since you read them" in stale.err
    assert "CL-1: at rev 2, changed by bao; you read rev 1: status: 'assumption' -> 'testing'" in stale.err

    shown = ok(run("alice", "show", SLUG)).out
    assert "Problem:\n  CL-1 [testing, medium, rev 2] Corner shops run out of stock" in shown
    proof = ("evidence", "add", SLUG, "CL-1", "--kind", "interview", "--direction", "supports")
    added = as_json(run("alice", *proof, "--summary", "Eight of ten owners reorder on Mondays", "--json"))
    assert_json_keys("hub canvas evidence add", added)
    whole = as_json(run("alice", "show", SLUG, "--claim", "CL-1", "--json"))
    assert_json_keys("hub canvas show", whole, "--claim")
    text = ok(run("alice", "show", SLUG, "--claim", "CL-1")).out
    assert f"#{added['id']} interview supports (real), by alice" in text and "rev 2" in text and "by bao" in text

    undone = as_json(run("alice", "undo", SLUG, "CL-1", "--json"))  # the current rev, read from the hub
    assert_json_keys("hub canvas undo", undone)
    assert (undone["claims"][0]["rev"], undone["claims"][0]["status"]) == (3, "assumption")

    file = tmp_path / "northwind.json"
    assert "Wrote canvas northwind-pantry, 1 claims" in ok(run("alice", "export", SLUG, "--output", str(file))).out
    data = json.loads(file.read_text(encoding="utf-8"))
    data["claims"][0]["text"] = "Shops run out of milk by Thursday"
    file.write_text(json.dumps(data), encoding="utf-8")
    preview = ok(run("bao", "import", str(file), "--dry-run")).out
    assert preview.startswith("Would import canvas northwind-pantry: 0 new, 1 changed, 0 unchanged")
    done = as_json(run("bao", "import", str(file), "--json"))
    assert_json_keys("hub canvas import", done)
    assert done["edited"] == ["CL-1"]
    assert "CL-1 [assumption, medium, rev 4]" in ok(run("alice", "show", SLUG)).out

"""``evo-agents hub signal push|list``, ``hub signal keys list|add|archive`` and ``hub signal ingest
create|list|revoke`` (``evo_agents.hub.cli_signals``), as members run them against a hub: the commands and their
``--json`` keys are in the contract (seam hub-cli-v1), an admin declares keys and makes an ingest key whose token is
printed once, a service sends a batch with it, a writer pushes one signal and a batch from a file, a reader lists them,
and an archived key and a revoked ingest key take nothing more (criterion a3 of plan living-lean-canvas). The service,
Northwind's knowledge base, is made up.

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
ZERO_RESULTS = "kb.search.zero_result_rate"
needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def test_hub_signal_commands_are_in_the_contract():
    printed = commands()
    verbs = ("push", "list", "keys list", "keys add", "keys archive", "ingest create", "ingest list", "ingest revoke")
    assert {f"hub signal {verb}" for verb in verbs} <= set(printed)
    schemas = {verb: printed[f"hub signal {verb}"]["json"]["schema"] for verb in verbs}
    assert schemas == {
        "push": "SignalBatchResult",
        "list": "Signal",
        "keys list": "SignalKey",
        "keys add": "SignalKey",
        "keys archive": "SignalKey",
        "ingest create": "SignalIngestKeyIssued",
        "ingest list": "SignalIngestKey",
        "ingest revoke": "SignalIngestKey",
    }
    options = {o["flags"][0]: o for o in printed["hub signal keys add"]["options"]}
    assert options["--kind"]["choices"] == ["number", "feedback"] and options["--project"]["required"]


def test_hub_signal_push_takes_a_file_or_a_key_with_one_value(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "alice", "evh_unused")
    base = ("hub", "signal", "push", "--project", PROJECT)
    neither = run_cli(monkeypatch, capsys, home, *base, "--key", ZERO_RESULTS)
    assert neither.code == 1 and "one of --value (a number) and --text (feedback)" in neither.err
    both = run_cli(monkeypatch, capsys, home, *base, "--key", ZERO_RESULTS, "--value", "1", "--text", "x")
    assert both.code == 1 and "one of --value" in both.err
    mixed = run_cli(monkeypatch, capsys, home, *base, "--file", "-", "--key", ZERO_RESULTS)
    assert mixed.code == 1 and "--file or --key, not both" in mixed.err


if pg.DSN:
    import httpx

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import registration
    from tests.hub.test_run_stream import serving


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, its project with owner its admin, alice a writer and eve a reader, a home signed in for
    each."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        admin = live.bearer(live.sign_in(client, github, live.ADMIN, 302)["token"])
        assert client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=admin).status_code == 200
        homes = {}
        for login, github_id, role in (("owner", 801, "admin"), ("alice", 802, "writer"), ("eve", 803, "reader")):
            token = live.sign_in(client, github, login, github_id)["token"]
            grant = {"role": role, "max_level": "internal"}
            granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
            assert granted.status_code == 200, granted.text
            homes[login] = write_credentials(tmp_path / login, url, login, token)
        yield SimpleNamespace(url=url, homes=homes, client=client)


def signal(hub, monkeypatch, capsys, who: str, *args: str, stdin=None) -> SimpleNamespace:
    return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "signal", *args, "--project", PROJECT, stdin=stdin)


@needs_pg
def test_hub_signal_from_the_command_line(hub, monkeypatch, capsys, tmp_path):
    run = lambda who, *args, **kw: signal(hub, monkeypatch, capsys, who, *args, **kw)  # noqa: E731
    added = as_json(run("owner", "keys", "add", ZERO_RESULTS, "--kind", "number", "--unit", "ratio", "--json"))
    assert_json_keys("hub signal keys add", added)
    shown = ok(run("owner", "keys", "add", "kb.support.feedback", "--kind", "feedback", "--source", "widget")).out
    assert shown.startswith("kb.support.feedback: feedback, at the project's default level")
    refused = run("alice", "keys", "add", "kb.search.latency_ms", "--kind", "number")
    assert refused.code == 1 and "needs the admin role" in refused.err
    listed = as_json(run("eve", "keys", "list", "--json"))
    assert_json_keys("hub signal keys list", listed)
    assert [k["name"] for k in listed] == [ZERO_RESULTS, "kb.support.feedback"]

    issued = as_json(run("owner", "ingest", "create", "northwind-kb", "--max-level", "internal", "--json"))
    assert_json_keys("hub signal ingest create", issued)
    printed = ok(run("owner", "ingest", "create", "lumen-search"))
    assert "Token, shown this once: evi_" in printed.out
    keys = as_json(run("owner", "ingest", "list", "--json"))
    assert_json_keys("hub signal ingest list", keys)
    assert [k["name"] for k in keys] == ["northwind-kb", "lumen-search"]
    assert "evi_" not in ok(run("owner", "ingest", "list")).out

    service = {"Authorization": f"Bearer {issued['token']}"}
    batch = {"items": [{"key": ZERO_RESULTS, "value": 0.12, "tenant": "northwind", "external_id": "z-1"}]}
    sent = hub.client.post(f"/v1/signals/{PROJECT}", json=batch, headers=service)
    assert sent.status_code == 202 and sent.json()["accepted"] == 1

    one = as_json(run("alice", "push", "--key", ZERO_RESULTS, "--value", "0.08", "--tenant", "northwind", "--json"))
    assert_json_keys("hub signal push", one)
    assert (one["accepted"], one["rejected"]) == (1, [])
    file = tmp_path / "batch.json"
    items = [
        {"key": "kb.support.feedback", "text": "Search finds nothing for 'refund'"},
        {"key": "kb.nope.x", "value": 1},
    ]
    file.write_text(json.dumps(items), encoding="utf-8")
    partly = run("alice", "push", "--file", str(file))
    assert partly.code == 1 and "1 stored, 1 refused." in partly.out
    assert "item 1 (kb.nope.x): key kb.nope.x is not in the registry" in partly.err

    read = as_json(run("eve", "list", "--key", ZERO_RESULTS, "--json"))
    assert_json_keys("hub signal list", read)
    assert [(s["value"], s["ingest_key"], s["login"]) for s in read] == [
        (0.08, None, "alice"),
        (0.12, "northwind-kb", None),
    ]
    text = ok(run("eve", "list")).out
    assert "Search finds nothing for 'refund'" in text and "northwind-kb" in text and "alice" in text

    archived = as_json(run("owner", "keys", "archive", ZERO_RESULTS, "--json"))
    assert_json_keys("hub signal keys archive", archived)
    late = run("alice", "push", "--key", ZERO_RESULTS, "--value", "0.5")
    assert late.code == 1 and "archived" in late.err
    revoked = as_json(run("owner", "ingest", "revoke", str(issued["id"]), "--json"))
    assert_json_keys("hub signal ingest revoke", revoked)
    again = hub.client.post(f"/v1/signals/{PROJECT}", json=batch, headers=service)
    assert again.status_code == 401

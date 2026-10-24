"""The evidence of an opportunity (plan opportunity-intake, criterion a2; ``evo_agents.hub.opportunities``,
``evo_agents.hub.server.opportunity_evidence`` and ``evo_agents.hub.cli_opportunities``): a line is a problem's, with the
8 signals of a problem and 7 more (wish, external_demand, window, reusable_asset, pull, intro, idle_capacity); the hub
answers 422 for a personal label and for an excerpt over 300 characters; a writer adds and confirms, a reader reads, an
outsider gets 404; ``opportunity show`` counts the lines and the independent sources; a problem's evidence keeps its 8
signals. @builder's proposals of lines are ``tests.hub.test_builder``'s and the tool opportunity_evidence
``tests.hub.test_mcp``'s.

The model runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import opportunities, problems
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
DEPOT = {
    "statement": "Sell the shelf scanner to the depot",
    "beneficiary": "the depot's night shift",
    "leverage": "the depot asked for a demo twice",
}
LINE = {
    "source_kind": "email",
    "source_uri": "email://depot/42",
    "locator": "thread 7",
    "speaker": "Ines",
    "excerpt": "Could you show the scanner to the night shift?",
    "signal": "wish",
    "label": "internal",
}

if pg.DSN:
    from tests.hub.test_autonomy_policy import GRANTS, _registered, hub  # noqa: F401 (hub is a fixture)
    from tests.hub.test_plans import PROJECT
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows


def test_opportunity_evidence_signals_are_the_eight_of_a_problem_and_seven_more():
    assert opportunities.SIGNALS[:8] == problems.SIGNALS and len(opportunities.SIGNALS) == 15
    assert set(opportunities.SIGNALS) - set(problems.SIGNALS) == {
        "wish",
        "external_demand",
        "window",
        "reusable_asset",
        "pull",
        "intro",
        "idle_capacity",
    }
    assert opportunities.MAX_PROPOSED_EVIDENCE == problems.MAX_PROPOSED_EVIDENCE


def test_opportunity_evidence_commands_are_in_the_contract():
    printed = commands()
    for name in ("list", "add", "confirm"):
        assert printed[f"hub opportunity evidence {name}"]["json"]["schema"] == "OpportunityEvidence", name
    assert {"evidence_count", "source_count"} <= set(printed["hub opportunity show"]["json"]["keys"])


@needs_pg
def test_opportunity_evidence_is_added_by_a_writer_confirmed_counted_and_read_by_members_alone(hub, hub_db):
    put = hub.client.put(f"/v1/projects/{PROJECT}/opportunities/O-1", json=DEPOT, headers=hub.headers["owner"])
    assert put.status_code == 200, put.text
    base = f"/v1/projects/{PROJECT}/opportunities/O-1/evidence"

    def post(who: str, body: dict, rest: str = ""):
        return hub.client.post(base + rest, json=body, headers=hub.headers[who])

    assert hub.client.get(base, headers=hub.headers["reader"]).json() == []
    assert post("reader", LINE).status_code == 403
    for bad in ({**LINE, "label": "personal"}, {**LINE, "excerpt": "x" * 301}, {**LINE, "signal": "rumour"}):
        assert post("owner", bad).status_code == 422, bad
    for signal in opportunities.SIGNALS:  # all fifteen are taken, each as another line
        line = {**LINE, "signal": signal, "locator": signal}
        assert post("owner", line).status_code == 201, signal
    first = post("owner", LINE)
    assert first.status_code == 201, first.text
    assert (first.json()["code"], first.json()["opportunity"], first.json()["confirmed_by"]) == ("E-16", "O-1", None)
    assert post("owner", LINE).status_code == 409  # the same line twice
    shown = hub.client.get(f"/v1/projects/{PROJECT}/opportunities/O-1", headers=hub.headers["reader"]).json()
    assert (shown["evidence_count"], shown["source_count"]) == (16, 1)
    assert_json_keys("hub opportunity show", shown)
    assert post("reader", {}, "/E-1/confirm").status_code == 403
    confirmed = post("owner", {}, "/E-1/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["confirmed_by"] is not None, confirmed.text
    assert post("owner", {}, "/E-1/confirm").status_code == 409
    assert post("owner", {}, "/E-99/confirm").status_code == 404
    listed = hub.client.get(base, headers=hub.headers["reader"]).json()
    assert [line["code"] for line in listed][:2] == ["E-1", "E-2"] and len(listed) == 16
    assert_json_keys("hub opportunity evidence list", listed)
    assert hub.client.get(base.replace("O-1", "O-7"), headers=hub.headers["reader"]).status_code == 404
    outsider = hub.client.get(base.replace(PROJECT, "elsewhere"), headers=hub.headers["reader"])
    assert outsider.status_code == 404
    rows = [row[1] for row in audit_rows(hub_db, "opportunity") if "evidence" in row[0]]
    assert len(rows) == 17 and all(LINE["excerpt"] not in row for row in rows)
    # a problem's evidence keeps its 8 signals
    problem = {"statement": "Shelves run empty", "who": "shops", "when": "Thursday", "workaround": "they drive"}
    assert (
        hub.client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=problem, headers=hub.headers["owner"]).status_code
        == 200
    )
    wish = hub.client.post(f"/v1/projects/{PROJECT}/problems/P-1/evidence", json=LINE, headers=hub.headers["owner"])
    assert wish.status_code == 422, wish.text


@needs_pg
def test_opportunity_evidence_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
    from evo_agents.hub.server.app import create_app

    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url:
        import httpx

        with httpx.Client(base_url=url, timeout=10) as client:
            headers = _registered(client, github)
        homes = {
            login: write_credentials(tmp_path / login, url, login, headers[login]["Authorization"].split()[1])
            for login, _, _ in GRANTS
        }

        def run(who: str, *args: str):
            return run_cli(monkeypatch, capsys, homes[who], "hub", "opportunity", *args, "--project", PROJECT)

        texts = ("--beneficiary", "the night shift", "--leverage", "they asked twice")
        ok(run("owner", "set", "--statement", "Sell the scanner", *texts))
        add = (
            "--source-kind", "email", "--source-uri", "email://depot/42", "--locator", "thread 7",
            "--speaker", "Ines", "--excerpt", "Show us the scanner.", "--signal", "wish",
        )  # fmt: skip
        assert ok(run("owner", "evidence", "list", "O-1")).out == (
            f"Opportunity O-1 of project {PROJECT} has no evidence.\n"
        )
        refused = run("owner", "evidence", "add", "O-1", *add, "--label", "personal")
        assert refused.code == 1  # the hub answered 422
        assert run("reader", "evidence", "add", "O-1", *add, "--label", "internal").code == 1
        made = ok(run("owner", "evidence", "add", "O-1", *add, "--label", "internal", "--json"))
        assert_json_keys("hub opportunity evidence add", as_json(made))
        assert ok(run("reader", "evidence", "list", "O-1")).out.count("E-1") == 1
        assert_json_keys("hub opportunity evidence list", as_json(run("reader", "evidence", "list", "O-1", "--json")))
        done = ok(run("owner", "evidence", "confirm", "O-1", "E-1", "--json"))
        assert_json_keys("hub opportunity evidence confirm", as_json(done))
        assert "Evidence: 1 lines from 1 independent sources" in ok(run("reader", "show", "O-1")).out

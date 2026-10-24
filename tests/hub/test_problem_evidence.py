"""The evidence of a problem (plan problem-intake, criterion a1; ``evo_agents.hub.problems``,
``evo_agents.hub.server.problem_evidence`` and ``evo_agents.hub.cli_problems``): a line has the kind and address of its
source, a place in it, a speaker, an excerpt of at most 300 characters, a signal, a label (public or internal), who
added it and who confirmed it; the hub answers 422 for a personal label and for a longer excerpt; a writer adds and
confirms, a reader reads, an outsider gets 404; ``problem show`` counts the lines and the independent sources. @builder's
proposals of lines are ``tests.hub.test_builder``'s and the tool problem_evidence ``tests.hub.test_mcp``'s.

The model runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.core_plugins import builder
from evo_agents.hub import opportunities, problems
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_problems import NIGHT, _get, _put
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
LINE = {
    "source_kind": "hub_chat",
    "source_uri": "hub://northwind/chats/12",
    "locator": "message 40",
    "speaker": "Mira",
    "excerpt": "Searches for a brand under its old name find nothing.",
    "signal": "complaint",
    "label": "internal",
}

if pg.DSN:
    from tests.hub.test_autonomy_policy import GRANTS, _registered, hub  # noqa: F401 (hub is a fixture)
    from tests.hub.test_plans import PROJECT
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows


def test_independent_sources_are_the_distinct_kind_and_address_pairs():
    lines = [
        {"source_kind": "hub_chat", "source_uri": "a"},
        {"source_kind": "hub_chat", "source_uri": "a"},
        {"source_kind": "email", "source_uri": "a"},
    ]
    assert problems.sources_of(lines) == 2
    assert problems.evidence_code(3) == "E-3"
    assert problems.MAX_EXCERPT_CHARS == 300 and "personal" not in problems.EVIDENCE_LABELS


def test_the_builder_plugin_copies_the_vocabulary_of_the_model():
    assert tuple(builder.EVIDENCE_SOURCE_KINDS) == problems.SOURCE_KINDS
    assert tuple(builder.EVIDENCE_SIGNALS) == opportunities.SIGNALS
    assert opportunities.SIGNALS[: len(problems.SIGNALS)] == problems.SIGNALS
    assert tuple(builder.EVIDENCE_LABELS) == problems.EVIDENCE_LABELS


def test_problem_evidence_commands_are_in_the_contract():
    printed = commands()
    assert printed["hub problem evidence list"]["json"]["schema"] == "ProblemEvidence"
    assert printed["hub problem evidence add"]["json"]["schema"] == "ProblemEvidence"
    assert printed["hub problem evidence confirm"]["json"]["schema"] == "ProblemEvidence"
    assert {"evidence_count", "source_count"} <= set(printed["hub problem show"]["json"]["keys"])


@needs_pg
def test_problem_evidence_is_added_by_a_writer_confirmed_counted_and_read_by_members_alone(hub, hub_db):
    assert _put(hub, "owner", "P-1", NIGHT).status_code == 200
    base = f"/v1/projects/{PROJECT}/problems/P-1/evidence"

    def post(who: str, body: dict, rest: str = ""):
        return hub.client.post(base + rest, json=body, headers=hub.headers[who])

    assert hub.client.get(base, headers=hub.headers["reader"]).json() == []
    assert post("reader", LINE).status_code == 403
    for bad in ({**LINE, "label": "personal"}, {**LINE, "excerpt": "x" * 301}, {**LINE, "signal": "rumour"}):
        assert post("owner", bad).status_code == 422, bad
    first = post("owner", LINE)
    assert first.status_code == 201, first.text
    assert (first.json()["code"], first.json()["label"], first.json()["confirmed_by"]) == ("E-1", "internal", None)
    assert post("owner", {**LINE, "excerpt": "y" * 300, "label": "public"}).status_code == 201
    assert post("owner", LINE).status_code == 409  # the same line twice
    second = {**LINE, "source_uri": "hub://northwind/chats/13"}
    assert post("owner", second).status_code == 201
    shown = _get(hub, "reader", "/P-1")
    assert (shown["evidence_count"], shown["source_count"]) == (3, 2)
    assert_json_keys("hub problem show", shown)
    assert post("reader", {}, "/E-1/confirm").status_code == 403
    confirmed = post("owner", {}, "/E-1/confirm")
    assert confirmed.status_code == 200 and confirmed.json()["confirmed_by"] is not None, confirmed.text
    assert post("owner", {}, "/E-1/confirm").status_code == 409
    assert post("owner", {}, "/E-9/confirm").status_code == 404
    listed = hub.client.get(base, headers=hub.headers["reader"]).json()
    assert [line["code"] for line in listed] == ["E-1", "E-2", "E-3"]
    assert_json_keys("hub problem evidence list", listed)
    # a member without a grant gets 404, as for the problem itself
    outsider = next((who for who in hub.headers if who not in {g[0] for g in GRANTS} and who != "admin"), None)
    if outsider:
        assert hub.client.get(base, headers=hub.headers[outsider]).status_code == 404
    assert hub.client.get(base.replace("P-1", "P-7"), headers=hub.headers["reader"]).status_code == 404
    rows = [row[1] for row in audit_rows(hub_db, "problem") if "evidence" in row[1]]
    assert len(rows) == 4 and all("excerpt" not in row and LINE["excerpt"] not in row for row in rows)


@needs_pg
def test_problem_evidence_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
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
            return run_cli(monkeypatch, capsys, homes[who], "hub", "problem", *args, "--project", PROJECT)

        texts = ("--who", NIGHT["who"], "--when", NIGHT["when"], "--workaround", NIGHT["workaround"])
        ok(run("owner", "set", "--statement", "Plans wait", *texts))
        add = (
            "--source-kind", "hub_chat", "--source-uri", "hub://northwind/chats/12", "--locator", "message 40",
            "--speaker", "Mira", "--excerpt", "Plans wait until morning.", "--signal", "complaint",
        )  # fmt: skip
        assert ok(run("owner", "evidence", "list", "P-1")).out == (
            f"Problem P-1 of project {PROJECT} has no evidence.\n"
        )
        refused = run("owner", "evidence", "add", "P-1", *add, "--label", "personal")
        assert refused.code == 1 and "does not match the API" in refused.err  # the hub answered 422
        long = run(
            "owner",
            "evidence",
            "add",
            "P-1",
            *add[:-6],
            "--excerpt",
            "x" * 301,
            "--signal",
            "blocker",
            "--label",
            "public",
        )
        assert long.code == 1
        made = ok(run("owner", "evidence", "add", "P-1", *add, "--label", "internal", "--json"))
        assert_json_keys("hub problem evidence add", as_json(made))
        assert ok(run("reader", "evidence", "list", "P-1")).out.count("E-1") == 1
        assert_json_keys("hub problem evidence list", as_json(run("reader", "evidence", "list", "P-1", "--json")))
        done = ok(run("owner", "evidence", "confirm", "P-1", "E-1", "--json"))
        assert_json_keys("hub problem evidence confirm", as_json(done))
        assert "Evidence: 1 lines from 1 independent sources" in ok(run("reader", "show", "P-1")).out

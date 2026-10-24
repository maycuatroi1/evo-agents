"""The opportunities of a project (plan opportunity-intake, criterion a1; ``evo_agents.hub.opportunities``,
``evo_agents.hub.server.opportunities``, ``evo_agents.hub.cli_opportunities``, ``evo_agents.hub.kg_opportunities``):
an opportunity has a statement, who benefits, the leverage, a window, the problems it answers, a metric, a rank, an
owner and a status (open, captured, missed, dropped, the last three with a reason); its code O-<number> is unique in the
project and never given again; a writer writes it whole as its next revision (409 on a stale ``if_revision``) and
closes it, a reader reads it, a member without a grant gets 404; an answer that names a problem of another project
needs a writer grant there, and a reader without one sees only a count. The evidence is
``tests.hub.test_opportunity_evidence``'s.

The model runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import opportunities
from evo_agents.hub.contract import commands
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_problems import NIGHT
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
DEPOT = {
    "statement": "  Sell the shelf scanner to the depot ",
    "beneficiary": "the depot's night shift",
    "leverage": "the depot asked for a demo twice",
    "window_until": "2026-12-01",
    "window_note": "the fair",
}

if pg.DSN:
    from tests.hub.test_autonomy_policy import GRANTS, _registered, hub  # noqa: F401 (hub is a fixture)
    from tests.hub.test_chats import OPEN_SINKS
    from tests.hub.test_plans import PROJECT, registration
    from tests.hub.test_run_stream import serving
    from tests.hub.test_runs import audit_rows

LUMEN = "lumen"


def test_opportunity_codes_and_answers_are_read_from_their_text():
    assert opportunities.code(4) == "O-4"
    assert opportunities.number_of("O-12") == 12 and opportunities.number_of("P-12") is None
    assert opportunities.number_of("O-0") is None
    assert opportunities.answer_of("P-3") == (None, 3)
    assert opportunities.answer_of(" lumen/P-3 ") == ("lumen", 3)
    assert opportunities.answer_of("lumen/G-3") is None and opportunities.answer_of("P-0") is None
    assert opportunities.answer_text("lumen", 3) == "lumen/P-3" and opportunities.answer_text(None, 3) == "P-3"
    assert opportunities.window_text({"window_until": None, "window_note": None}) == "no window"
    assert opportunities.window_text({"window_until": "2026-12-01", "window_note": "the fair"}) == (
        "until 2026-12-01 (the fair)"
    )
    assert opportunities.status_text({"status": "missed", "close_reason": "late"}) == "missed (late)"
    before = {"statement": "a", "rank": 1}
    assert opportunities.changed(before, {"statement": "a", "rank": 2}) == ["rank"]
    assert opportunities.MAX_ANSWERS == 20 and len(opportunities.SIGNALS) == 15


def test_opportunity_commands_are_in_the_contract():
    printed = commands()
    for name, schema in (
        ("list", "ProjectOpportunity"),
        ("show", "ProjectOpportunityShown"),
        ("set", "ProjectOpportunityWritten"),
        ("close", "ProjectOpportunityWritten"),
    ):
        assert printed[f"hub opportunity {name}"]["json"]["schema"] == schema, name


def _put(hub, who: str, ref: str, body: dict, project: str | None = None):
    return hub.client.put(f"/v1/projects/{project or PROJECT}/opportunities/{ref}", json=body, headers=hub.headers[who])


def _close(hub, who: str, ref: str, body: dict):
    return hub.client.post(f"/v1/projects/{PROJECT}/opportunities/{ref}/close", json=body, headers=hub.headers[who])


def _get(hub, who: str, rest: str = "", project: str | None = None):
    response = hub.client.get(f"/v1/projects/{project or PROJECT}/opportunities{rest}", headers=hub.headers[who])
    assert response.status_code == 200, response.text
    return response.json()


@needs_pg
def test_opportunities_are_written_by_a_writer_as_revisions_kept_closed_audited_and_read_by_members_alone(hub, hub_db):
    assert _get(hub, "reader") == []
    assert _put(hub, "reader", "O-1", DEPOT).status_code == 403
    for bad in ({**DEPOT, "statement": " "}, {k: v for k, v in DEPOT.items() if k != "leverage"}, {**DEPOT, "rank": 0}):
        assert _put(hub, "owner", "O-1", bad).status_code == 422, bad
    assert _put(hub, "owner", "O-2", DEPOT).status_code == 422  # a new opportunity takes the next code, O-1
    first = _put(
        hub,
        "owner",
        "O-1",
        DEPOT,
    ).json()
    assert (first["code"], first["revision"], first["changed"], first["status"]) == ("O-1", 1, True, "open")
    assert (first["statement"], first["owner"], first["rank"], first["answers"]) == (
        "Sell the shelf scanner to the depot",
        "owner",
        1,
        [],
    )
    assert_json_keys("hub opportunity set", first)
    same = _put(hub, "owner", "O-1", DEPOT).json()
    assert (same["revision"], same["changed"]) == (1, False)
    second = _put(
        hub,
        "alice",
        "O-1",
        {**DEPOT, "leverage": "a pilot is promised"},
    ).json()
    assert (second["revision"], second["created_by"]) == (2, "alice")
    stale = _put(hub, "owner", "O-1", {**DEPOT, "if_revision": 1})
    assert stale.status_code == 409, stale.text
    assert _put(hub, "owner", "O-1", {**DEPOT, "leverage": "a pilot is promised", "if_revision": 0}).status_code == 409
    other = _put(hub, "owner", "O-2", {**DEPOT, "statement": "Rent the scanner out", "if_revision": 0}).json()
    assert (other["code"], other["rank"]) == ("O-2", 2)
    # a closed opportunity keeps its code; it needs a reason and is not deleted
    assert _close(hub, "reader", "O-1", {"status": "captured", "reason": "signed"}).status_code == 403
    assert _close(hub, "owner", "O-1", {"status": "open", "reason": "x"}).status_code == 422
    closed = _close(hub, "owner", "O-1", {"status": "captured", "reason": "  the depot  signed ", "if_revision": 2})
    assert closed.status_code == 200, closed.text
    assert (closed.json()["status"], closed.json()["close_reason"], closed.json()["revision"]) == (
        "captured",
        "the depot signed",
        3,
    )
    assert _close(hub, "owner", "O-1", {"status": "missed", "reason": "late", "if_revision": 2}).status_code == 409
    assert [o["code"] for o in _get(hub, "reader")] == ["O-1", "O-2"]
    assert [o["code"] for o in _get(hub, "reader", "?status=open")] == ["O-2"]
    shown = _get(hub, "reader", "/O-1")
    assert (shown["evidence_count"], shown["source_count"], shown["revision"]) == (0, 0, 3)
    assert_json_keys("hub opportunity show", shown)
    assert _get(hub, "reader", "/O-1?revision=1")["leverage"] == "the depot asked for a demo twice"
    revisions = _get(hub, "reader", "/O-1/revisions")
    assert [r["revision"] for r in revisions] == [3, 2, 1] and revisions[1]["changed"] == ["leverage"]
    reopened = _put(hub, "owner", "O-1", {**DEPOT, "leverage": "a pilot is promised", "if_revision": 3}).json()
    assert (reopened["status"], reopened["close_reason"], reopened["revision"]) == ("open", None, 4)
    assert hub.client.get(f"/v1/projects/{PROJECT}/opportunities/O-9", headers=hub.headers["reader"]).status_code == 404
    # the hub admin without a grant reads nothing; a stranger gets 404 as for the project
    refused = hub.client.get(f"/v1/projects/{PROJECT}/opportunities", headers=hub.headers["admin"])
    assert refused.status_code == 403
    rows = audit_rows(hub_db, "opportunity")
    assert [row[0] for row in rows] == ["opportunity.set"] * len(rows) and len(rows) == 5
    assert all("pilot" not in row[1] for row in rows)


@needs_pg
def test_opportunity_answers_need_a_writer_grant_on_another_project_and_a_reader_without_one_sees_a_count(
    hub, hub_db, github
):
    admin = hub.headers["admin"]
    body = {**registration(OPEN_SINKS), "repos": [], "harness": {"name": LUMEN, "workspace": "~/ws", "path": LUMEN}}
    assert hub.client.put(f"/v1/projects/{LUMEN}", json=body, headers=admin).status_code == 200
    for login, role in (("owner", "writer"), ("reader", "reader")):
        grant = {"role": role, "max_level": "internal"}
        assert (
            hub.client.put(f"/v1/admin/projects/{LUMEN}/grants/{login}", json=grant, headers=admin).status_code == 200
        )
    stored = hub.client.put(f"/v1/projects/{PROJECT}/problems/P-1", json=NIGHT, headers=hub.headers["owner"])
    assert stored.status_code == 200, stored.text
    there = hub.client.put(f"/v1/projects/{LUMEN}/problems/P-1", json=NIGHT, headers=hub.headers["owner"])
    assert there.status_code == 200, there.text
    # a problem that is not there, a code that is not one, too many codes
    for answers in (["P-7"], [f"{LUMEN}/P-9"], ["lumen-x/P-1"], [f"P-{n}" for n in range(1, 22)]):
        refused = _put(hub, "owner", "O-1", {**DEPOT, "answers": answers})
        assert refused.status_code == 422, (answers, refused.text)
    assert "P-7" in _put(hub, "owner", "O-1", {**DEPOT, "answers": ["P-7"]}).json()["message"]
    # alice holds no grant on lumen: 422 naming the code
    denied = _put(hub, "alice", "O-1", {**DEPOT, "answers": ["P-1", f"{LUMEN}/P-1"]})
    assert denied.status_code == 422 and f"{LUMEN}/P-1" in denied.json()["message"], denied.text
    # the reader of lumen has a grant but not a writer one
    assert _put(hub, "reader", "O-1", {**DEPOT, "answers": [f"{LUMEN}/P-1"]}).status_code == 403
    made = _put(hub, "owner", "O-1", {**DEPOT, "answers": ["P-1", f"{LUMEN}/P-1"]})
    assert made.status_code == 200, made.text
    assert (made.json()["answers"], made.json()["hidden_answers"]) == (["P-1", f"{LUMEN}/P-1"], 0)
    # the reader of northwind holds a reader grant on lumen: sees it; alice holds none: sees a count alone
    assert _get(hub, "reader", "/O-1")["answers"] == ["P-1", f"{LUMEN}/P-1"]
    seen = _get(hub, "alice", "/O-1")
    assert (seen["answers"], seen["hidden_answers"]) == (["P-1"], 1)
    assert "lumen" not in str(_get(hub, "alice")) and _get(hub, "alice")[0]["hidden_answers"] == 1
    # a writer who cannot see the hidden answer keeps it when they write another field
    kept = _put(hub, "alice", "O-1", {**DEPOT, "answers": ["P-1"], "rank": 1, "leverage": "new"}).json()
    assert (kept["answers"], kept["hidden_answers"], kept["revision"]) == (["P-1"], 1, 2)
    assert _get(hub, "owner", "/O-1")["answers"] == ["P-1", f"{LUMEN}/P-1"]
    # a dropped problem is not answered again, but a held answer may stay
    dropped = hub.client.post(
        f"/v1/projects/{PROJECT}/problems/P-1/close",
        json={"status": "dropped", "reason": "gone"},
        headers=hub.headers["owner"],
    )
    assert dropped.status_code == 200, dropped.text
    assert _put(hub, "owner", "O-2", {**DEPOT, "answers": ["P-1"], "if_revision": 0}).status_code == 422
    assert _put(hub, "owner", "O-1", {**DEPOT, "answers": ["P-1", f"{LUMEN}/P-1"], "rank": 2}).status_code == 200
    twice = _put(hub, "owner", "O-1", {**DEPOT, "answers": [f"{LUMEN}/P-1", f"{LUMEN}/P-1"]}).json()
    assert twice["answers"] == [f"{LUMEN}/P-1"]  # the same problem twice is one answer


@needs_pg
def test_opportunity_commands_write_show_list_and_close_from_the_command_line(
    hub_db, tmp_path, github, monkeypatch, capsys
):
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

        assert ok(run("reader", "list")).out == f"Project {PROJECT} has no opportunities.\n"
        texts = ("--beneficiary", "the night shift", "--leverage", "they asked twice")
        made = ok(
            run("owner", "set", "--statement", "Sell the scanner", *texts, "--window-until", "2026-12-01", "--json")
        )
        assert_json_keys("hub opportunity set", as_json(made))
        assert run("reader", "set", "--statement", "x", *texts).code == 1
        assert run("owner", "set", "--statement", "x", *texts, "--answers", "P-4").code == 1  # no such problem
        again = ok(run("alice", "set", "O-1", "--rank", "1", "--note", "same", "--if-revision", "1", "--json"))
        assert as_json(again)["changed"] is False
        assert "O-1" in ok(run("reader", "list")).out
        assert_json_keys("hub opportunity list", as_json(run("reader", "list", "--json")))
        shown = ok(run("reader", "show", "O-1")).out
        assert "Sell the scanner" in shown and "Evidence:" not in shown  # no line yet
        assert_json_keys("hub opportunity show", as_json(run("reader", "show", "O-1", "--json")))
        done = ok(run("owner", "close", "O-1", "--status", "missed", "--reason", "the fair passed", "--json"))
        assert as_json(done)["status"] == "missed"
        assert run("owner", "close", "O-1", "--status", "missed", "--reason", "x", "--if-revision", "1").code == 1

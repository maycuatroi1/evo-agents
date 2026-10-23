"""What a claim of the core plugin canvas links, as criterion a7 of plan living-lean-canvas names it: the use cases of
the plugin srs that realize it and the keys of the project's registry of signals that measure it
(``evo_agents.core_plugins.canvas.links``); and, as criterion a3 of plan problem-oriented-hub names it, the problems of
the project it answers: one open or solved links, a code the project lacks or a dropped problem is refused, a link held
before its problem was dropped stays, an export holds them and an import leaves out those the project lacks, problem
show lists the claims that link it, and canvas__read names them.

Checked here, on the hub: an edit links a use case by its code or an alias and keeps the code the SRS names it by, and
links a declared signal key; the links are part of the claim's content, so each change is a rev with its diff, and an
undo takes it back. A link the project does not hold is refused (422) and nothing is written: a code of no item, an
item that is no use case, a use case of a project that does not enable srs, a key the registry lacks or archived; a
link a claim holds already stays though its key was archived since. An export holds the links, and an import leaves
out, with a warning, those the project does not hold. The product of the sample canvas, Northwind Pantry, is made up.
"""

from __future__ import annotations

import copy
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import plugin_calls
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.test_canvas_plugin import BASE, PROJECT, SLUG, edit, made, members, problem, shown, undo

__all__ = ["members"]  # a fixture this module uses

CORE = {
    "canvas": EntryPoint("canvas", "evo_agents.core_plugins.canvas:MANIFEST", ENTRY_POINT_GROUP),
    "srs": EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP),
}
SAMPLE_SRS = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
LEAD_DAYS = "pantry.reorder.lead_days"
STOCKOUTS = "pantry.stock.outs_per_week"


@pytest.fixture
def plugins_core(monkeypatch):
    """The entry points of the core plugins with canvas's and srs's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, plugins_core):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


def enable(client, headers, plugin: str) -> None:
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/{plugin}/enable", json={"permissions": []}, headers=headers)
    assert enabled.status_code == 200, enabled.text


def declare(client, headers, name: str) -> None:
    answer = client.post(f"/v1/projects/{PROJECT}/signal-keys", json={"name": name, "kind": "number"}, headers=headers)
    assert answer.status_code == 201, answer.text


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with srs and canvas enabled, alice's draft of the sample SRS (UC ORD-1, alias UC-ORD-001), the
    signal keys of lead days and stock-outs, and alice's canvas of Northwind Pantry."""
    for plugin in ("srs", "canvas"):
        enable(client, members["owner"], plugin)
    body = {"markdown": SAMPLE_SRS.read_text(encoding="utf-8"), "slug": "demo-shop"}
    imported = client.post(f"/v1/x/srs/{PROJECT}/import", json=body, headers=members["alice"])
    assert imported.status_code == 200, imported.text
    for name in (LEAD_DAYS, STOCKOUTS):
        declare(client, members["owner"], name)
    made(client, members["alice"], name="Northwind Pantry")
    return members


def refused(client, headers, change: dict, why: str) -> None:
    answer = client.post(f"{BASE}/canvases/{SLUG}/edit", json={"changes": [change]}, headers=headers)
    assert answer.status_code == 422 and why in answer.json()["message"], answer.text


def test_canvas_claim_links_a_use_case_by_code_or_alias_and_a_declared_signal_key_in_its_revs(client, hub):
    done = edit(client, hub["alice"], problem(use_cases=["uc-ord-001"], signal_keys=[STOCKOUTS]))
    (claim,) = done["claims"]
    assert (claim["use_cases"], claim["signal_keys"]) == (["UC ORD-1"], [STOCKOUTS])  # the SRS's code
    changed = edit(
        client,
        hub["bao"],
        {"claim": "CL-1", "base_rev": 1, "use_cases": ["UC ORD-2", "UC ORD-1"], "signal_keys": [LEAD_DAYS, STOCKOUTS]},
    )
    assert changed["claims"][0]["use_cases"] == ["UC ORD-1", "UC ORD-2"]  # sorted
    again = edit(client, hub["bao"], {"claim": "CL-1", "base_rev": 2, "use_cases": ["uc  ord-2", "UC ORD-1"]})
    assert again["written"] == []  # the same links, however written: no rev
    whole = shown(client, hub["eve"], "CL-1")
    assert whole["rev"] == 2 and whole["revs"][1]["content"]["signal_keys"] == [LEAD_DAYS, STOCKOUTS]
    assert {(c["field"], tuple(c["before"]), tuple(c["after"])) for c in whole["revs"][1]["changes"]} == {
        ("use_cases", ("UC ORD-1",), ("UC ORD-1", "UC ORD-2")),
        ("signal_keys", (STOCKOUTS,), (LEAD_DAYS, STOCKOUTS)),
    }
    diffed = client.get(f"{BASE}/canvases/{SLUG}/claims/CL-1/diff", headers=hub["eve"]).json()
    assert [c["field"] for c in diffed["changes"]] == ["use_cases", "signal_keys"]
    taken_back = undo(client, hub["bao"], ("CL-1", 2))["claims"][0]
    assert (taken_back["rev"], taken_back["use_cases"], taken_back["signal_keys"]) == (3, ["UC ORD-1"], [STOCKOUTS])
    cleared = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 3, "use_cases": [], "signal_keys": None})
    assert (cleared["claims"][0]["use_cases"], cleared["claims"][0]["signal_keys"]) == ([], [])


def test_canvas_claim_links_refuse_what_the_project_does_not_hold_and_write_nothing(client, hub):
    edit(client, hub["alice"], problem(signal_keys=[LEAD_DAYS]))
    for change, why in (
        ({"use_cases": ["UC NOPE-9"]}, "no use case UC NOPE-9 in the SRS of project"),
        ({"use_cases": ["BR ORD1-1"]}, "BR ORD1-1 is a business rule of the SRS, not a use case"),
        ({"signal_keys": ["pantry.basket.size"]}, "declares no signal key pantry.basket.size"),
        ({"signal_keys": ["Pantry Basket"]}, "signal_keys takes the keys of signals"),
    ):
        refused(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, **change}, why)
        refused(client, hub["alice"], problem(**change), why)  # a new claim too
    many = client.post(
        f"{BASE}/canvases/{SLUG}/edit",
        json={"changes": [problem(use_cases=[f"UC ORD-{n}" for n in range(21)])]},
        headers=hub["alice"],
    )
    assert many.status_code == 422, many.text
    assert shown(client, hub["eve"], "CL-1")["rev"] == 1
    assert [
        c["code"] for c in client.get(f"{BASE}/canvases/{SLUG}", headers=hub["eve"]).json()["cells"][0]["claims"]
    ] == ["CL-1"]

    archived = client.post(f"/v1/projects/{PROJECT}/signal-keys/{LEAD_DAYS}/archive", headers=hub["owner"])
    assert archived.status_code == 200, archived.text
    kept = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "text": "Shops run out by Thursday"})
    assert kept["claims"][0]["signal_keys"] == [LEAD_DAYS]  # held already: it stays
    refused(client, hub["alice"], problem(signal_keys=[LEAD_DAYS]), f"signal key {LEAD_DAYS} of project")


def test_canvas_claim_links_no_use_case_where_srs_is_off(client, hub):
    off = client.post(f"/v1/projects/{PROJECT}/plugins/srs/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    refused(client, hub["alice"], problem(use_cases=["UC ORD-1"]), "does not enable plugin srs")
    made_one = edit(client, hub["alice"], problem(signal_keys=[STOCKOUTS]))
    assert made_one["claims"][0]["signal_keys"] == [STOCKOUTS]


def test_canvas_export_holds_the_links_and_an_import_leaves_out_those_the_project_lacks(client, hub):
    edit(client, hub["alice"], problem(use_cases=["UC ORD-1"], signal_keys=[STOCKOUTS]))
    exported = client.get(f"{BASE}/canvases/{SLUG}/export", headers=hub["eve"]).json()
    (claim,) = exported["claims"]
    assert (claim["use_cases"], claim["signal_keys"]) == (["UC ORD-1"], [STOCKOUTS])

    file = copy.deepcopy(exported)
    file["canvas"] = {"slug": "lumen-notes", "name": "Lumen Notes"}
    file["claims"] = [
        {
            "cell": "solution",
            "text": "Notes sync to the class",
            "use_cases": ["UC ORD-2", "UC NOPE-9"],
            "signal_keys": [STOCKOUTS, "lumen.notes.synced"],
        }
    ]
    done = client.post(f"{BASE}/canvases/import", json={"data": file}, headers=hub["alice"])
    assert done.status_code == 200, done.text
    assert done.json()["created"] == ["CL-2"]
    warnings = [p["message"] for p in done.json()["problems"]]
    assert len(warnings) == 2 and all(p["claim"] == "CL-2" for p in done.json()["problems"])
    assert any("UC NOPE-9 left out" in w for w in warnings) and any(
        "lumen.notes.synced left out" in w for w in warnings
    )
    made_claim = shown(client, hub["eve"], "CL-2", slug="lumen-notes")
    assert (made_claim["use_cases"], made_claim["signal_keys"]) == (["UC ORD-2"], [STOCKOUTS])


# Problems (plan problem-oriented-hub, criterion a3)

STOCK_OUTS = {
    "statement": "Corner shops run out of milk between deliveries",
    "who": "the owners of corner shops",
    "when": "by Thursday of a week with one delivery",
    "workaround": "they drive to a cash and carry",
}


def problem_made(client, headers, ref: str, body: dict = STOCK_OUTS) -> dict:
    answer = client.put(f"/v1/projects/{PROJECT}/problems/{ref}", json=body, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def problem_closed(client, headers, ref: str, status: str, reason: str) -> None:
    body = {"status": status, "reason": reason}
    answer = client.post(f"/v1/projects/{PROJECT}/problems/{ref}/close", json=body, headers=headers)
    assert answer.status_code == 200, answer.text


def test_canvas_claim_links_problems_that_are_open_or_solved_and_keeps_one_dropped_since(client, hub):
    for ref in ("P-1", "P-2", "P-3"):
        problem_made(client, hub["alice"], ref, {**STOCK_OUTS, "statement": f"{STOCK_OUTS['statement']} ({ref})"})
    problem_closed(client, hub["alice"], "P-3", "solved", "a second delivery on Wednesday")
    made_one = edit(client, hub["alice"], problem(problems=["P-3", "P-1", "P-1"]))
    assert made_one["claims"][0]["problems"] == ["P-1", "P-3"]  # by number, once each; a solved one links
    changed = edit(client, hub["bao"], {"claim": "CL-1", "base_rev": 1, "problems": ["P-2", "P-1"]})
    assert changed["claims"][0]["problems"] == ["P-1", "P-2"]
    whole = shown(client, hub["eve"], "CL-1")
    assert [(c["field"], c["before"], c["after"]) for c in whole["revs"][1]["changes"]] == [
        ("problems", ["P-1", "P-3"], ["P-1", "P-2"])
    ]
    assert whole["revs"][0]["content"]["problems"] == ["P-1", "P-3"]
    for change, why in (
        ({"problems": ["P-9"]}, f"project {PROJECT} has no problem P-9"),
        ({"problems": ["p-1"]}, "problems takes the codes of problems of the project, as P-3"),
        ({"problems": [f"P-{n}" for n in range(1, 22)]}, ""),
    ):
        answer = client.post(
            f"{BASE}/canvases/{SLUG}/edit", json={"changes": [problem(**change)]}, headers=hub["alice"]
        )
        assert answer.status_code == 422 and why in answer.json()["message"], answer.text
    assert shown(client, hub["eve"], "CL-1")["rev"] == 2  # nothing was written

    problem_closed(client, hub["owner"], "P-2", "dropped", "the same as P-1")
    kept = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 2, "text": "Shops run out by Thursday"})
    assert kept["claims"][0]["problems"] == ["P-1", "P-2"]  # linked before it was dropped: it stays
    refused(client, hub["alice"], problem(problems=["P-2"]), f"problem P-2 of project {PROJECT} is dropped")
    taken_back = undo(client, hub["bao"], ("CL-1", 3))["claims"][0]
    assert taken_back["problems"] == ["P-1", "P-2"]
    cleared = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 4, "problems": []})
    assert cleared["claims"][0]["problems"] == []
    again = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 5, "problems": ["P-1"]})
    assert again["claims"][0]["problems"] == ["P-1"]


def test_problem_show_lists_the_claims_that_link_it_and_an_export_holds_them(client, hub):
    problem_made(client, hub["alice"], "P-1")
    edit(client, hub["alice"], problem(problems=["P-1"]), {"cell": "solution", "text": "Reorder in one tap"})
    edit(client, hub["alice"], {"claim": "CL-2", "base_rev": 1, "problems": ["P-1"]})
    made(client, hub["alice"], slug="lumen-notes", name="Lumen Notes")
    edit(client, hub["alice"], problem("Notes get lost", problems=["P-1"]), slug="lumen-notes")
    edit(client, hub["alice"], {"claim": "CL-3", "base_rev": 1, "archived": True}, slug="lumen-notes")
    answer = client.get(f"/v1/projects/{PROJECT}/problems/P-1", headers=hub["eve"])
    assert answer.status_code == 200, answer.text
    assert [(c["canvas"], c["code"], c["cell"], c["status"]) for c in answer.json()["claims"]] == [
        (SLUG, "CL-1", "problem", "assumption"),
        (SLUG, "CL-2", "solution", "assumption"),
    ]  # an archived claim is left out
    assert answer.json()["claims"][0]["canvas_name"] == "Northwind Pantry"
    assert answer.json()["plans"] == []

    exported = client.get(f"{BASE}/canvases/{SLUG}/export", headers=hub["eve"]).json()
    assert [claim["problems"] for claim in exported["claims"]] == [["P-1"], ["P-1"]]
    file = copy.deepcopy(exported)
    file["canvas"] = {"slug": "fabrikam-milk", "name": "Fabrikam Milk"}
    file["claims"] = [{"cell": "problem", "text": "Milk runs out", "problems": ["P-1", "P-9"]}]
    done = client.post(f"{BASE}/canvases/import", json={"data": file}, headers=hub["alice"])
    assert done.status_code == 200, done.text
    (warning,) = done.json()["problems"]
    assert "link P-9 left out" in warning["message"] and "has no problem P-9" in warning["message"]
    assert shown(client, hub["eve"], "CL-4", slug="fabrikam-milk")["problems"] == ["P-1"]


def test_canvas_read_names_the_problems_a_claim_links():
    from datetime import UTC, datetime

    from evo_agents.core_plugins.canvas import tools
    from evo_agents.core_plugins.canvas.agent import CLAIM_FIELDS, _claim_lines
    from evo_agents.core_plugins.canvas.store import ClaimRow
    from evo_agents.hub import problems as problem_model

    now = datetime.now(UTC)
    row = ClaimRow(
        id=1,
        canvas_id=1,
        number=3,
        cell="problem",
        position=0,
        text="Shops run out of milk",
        status="assumption",
        confidence="medium",
        test_card=None,
        rev=1,
        label={},
        created_by="alice",
        created_at=now,
        updated_at=now,
        archived_at=None,
        problems=["P-1", "P-4"],
    )
    assert "  problems: P-1, P-4" in _claim_lines(row, [])
    assert (
        "problems" in CLAIM_FIELDS and "problems" in tools.EDIT_SCHEMA["properties"]["changes"]["items"]["properties"]
    )
    assert tools.PROBLEM_REF == problem_model.REF

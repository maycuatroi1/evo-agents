"""What a claim of the core plugin canvas links, as criterion a7 of plan living-lean-canvas names it: the use cases of
the plugin srs that realize it and the keys of the project's registry of signals that measure it
(``evo_agents.core_plugins.canvas.links``).

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

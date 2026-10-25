"""The core plugin canvas on the hub (``evo_agents.hub.server.canvas``), as criterion a2 of plan living-lean-canvas
names it: a writer makes a canvas for a product with the nine cells of a Lean Canvas; each cell holds claims with a
status, a confidence, a test card, evidence and the provenance of each rev. Every change is a rev; an edit carries the
rev it was read at and is refused (409) when the claim changed since; an undo writes a new rev with the content of the
rev before; validated is refused without real evidence (not synthetic, of a kind other than doc). Export and import
keep the claims' codes. Readers read, writers write, admins label and archive; rows carry the project's label. The
product of the sample canvas, Northwind Pantry, is made up.
"""

from __future__ import annotations

import copy
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from evo_agents.hub import plugin_calls, tables
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_plans import registration
from tests.hub.test_runs import PROJECT

CANVAS = EntryPoint("canvas", "evo_agents.hub.server.canvas:MANIFEST", ENTRY_POINT_GROUP)
BASE = f"/v1/x/canvas/{PROJECT}"
SLUG = "northwind-pantry"
CELLS = [
    "problem",
    "customer_segments",
    "unique_value_proposition",
    "solution",
    "channels",
    "revenue_streams",
    "cost_structure",
    "key_metrics",
    "unfair_advantage",
]
MEMBERS = {  # login -> (github id, role, max level)
    "owner": (601, "admin", "internal"),
    "alice": (602, "writer", "internal"),
    "bao": (603, "writer", "internal"),
    "eve": (605, "reader", "internal"),
    "outsider": (606, "reader", "public"),
}
CARD = {"hypothesis": "Shop owners reorder weekly", "test": "Interview ten owners", "deadline": "2026-11-30"}


@pytest.fixture
def canvas_core(monkeypatch):
    """The entry points of the core plugins with canvas's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name != CANVAS.name]
    points.append(CANVAS)
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, canvas_core):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, github) -> dict:
    """The project with its members of MEMBERS and the hub admin; the headers of each."""
    headers = {"admin": bearer(live.sign_in(client, github, ADMIN, 302)["token"])}
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
    assert registered.status_code == 200, registered.text
    for login, (github_id, role, level) in MEMBERS.items():
        headers[login] = bearer(live.sign_in(client, github, login, github_id)["token"])
        grant = {"role": role, "max_level": level}
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
    return headers


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with the plugin canvas enabled and alice's canvas of Northwind Pantry."""
    enabled = client.post(
        f"/v1/projects/{PROJECT}/plugins/canvas/enable", json={"permissions": []}, headers=members["owner"]
    )
    assert enabled.status_code == 200, enabled.text
    made(client, members["alice"], name="Northwind Pantry", description="Stock reordering for corner shops")
    return members


def made(client, headers, slug: str = SLUG, expect: int = 201, **body) -> dict:
    answer = client.post(f"{BASE}/canvases", json={"slug": slug, "name": "Northwind Pantry", **body}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def edit(client, headers, *changes: dict, expect: int = 200, reason: str = "", slug: str = SLUG) -> dict:
    body = {"changes": list(changes), "reason": reason}
    answer = client.post(f"{BASE}/canvases/{slug}/edit", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def undo(client, headers, *targets: tuple[str, int], expect: int = 200, slug: str = SLUG) -> dict:
    body = {"revs": [{"claim": claim, "rev": rev} for claim, rev in targets]}
    answer = client.post(f"{BASE}/canvases/{slug}/undo", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def shown(client, headers, ref: str, slug: str = SLUG) -> dict:
    answer = client.get(f"{BASE}/canvases/{slug}/claims/{ref}", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def evidence(client, headers, ref: str, expect: int = 201, **body) -> dict:
    given = {"kind": "interview", "direction": "supports", "summary": "Eight of ten owners reorder on Mondays"}
    answer = client.post(f"{BASE}/canvases/{SLUG}/claims/{ref}/evidence", json={**given, **body}, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def problem(text: str = "Corner shops run out of stock between deliveries", **values) -> dict:
    return {"cell": "problem", "text": text, **values}


# Canvases


def test_canvas_routes_answer_404_in_a_project_that_did_not_enable_it(client, members):
    answer = client.get(f"{BASE}/canvases", headers=members["alice"])
    assert answer.status_code == 404, answer.text
    assert (
        client.post(f"{BASE}/canvases", json={"slug": SLUG, "name": "x"}, headers=members["owner"]).status_code == 404
    )


def test_canvas_a_writer_makes_a_canvas_with_nine_empty_cells_and_members_read_it(client, hub):
    canvas = client.get(f"{BASE}/canvases/{SLUG}", headers=hub["eve"]).json()
    assert (canvas["slug"], canvas["name"], canvas["created_by"]) == (SLUG, "Northwind Pantry", "alice")
    assert [cell["cell"] for cell in canvas["cells"]] == CELLS
    assert all(cell["claims"] == [] for cell in canvas["cells"]) and canvas["claims"] == 0
    assert canvas["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    again = made(client, hub["bao"], expect=409)
    assert "has a canvas northwind-pantry already" in again["message"]
    refused = made(client, hub["eve"], slug="lumen-notes", expect=403)
    assert "needs the writer role; you are reader" in refused["message"]
    listed = client.get(f"{BASE}/canvases", headers=hub["eve"]).json()
    assert [(c["slug"], c["claims"], c["statuses"]) for c in listed] == [(SLUG, 0, {})]


def test_canvas_claims_are_numbered_in_the_project_and_every_change_is_a_rev_with_its_provenance(client, hub):
    done = edit(client, hub["alice"], problem(), {"cell": "solution", "text": "Reorder from the shelf with a photo"})
    assert [(c["code"], c["cell"], c["status"], c["confidence"], c["rev"]) for c in done["claims"]] == [
        ("CL-1", "problem", "assumption", "medium", 1),
        ("CL-2", "solution", "assumption", "medium", 1),
    ]
    assert done["written"] == ["CL-1", "CL-2"]
    made(client, hub["bao"], slug="lumen-notes", name="Lumen Notes")
    other = edit(client, hub["bao"], {"cell": "channels", "text": "Teachers share notes"}, slug="lumen-notes")
    assert other["claims"][0]["code"] == "CL-3"  # numbers run across the project's canvases

    change = {"claim": "CL-1", "base_rev": 1, "text": "Shops run out of milk by Thursday", "confidence": "high"}
    after = edit(client, hub["bao"], change, reason="what the owners said")["claims"][0]
    assert (after["text"], after["confidence"], after["rev"]) == ("Shops run out of milk by Thursday", "high", 2)
    whole = shown(client, hub["eve"], "CL-1")
    assert [(r["rev"], r["change"], r["provenance"]["actor"], r["provenance"]["login"]) for r in whole["revs"]] == [
        (1, "create", "person", "alice"),
        (2, "edit", "person", "bao"),
    ]
    assert whole["revs"][1]["reason"] == "what the owners said"
    assert {c["field"] for c in whole["revs"][1]["changes"]} == {"text", "confidence"}
    same = edit(client, hub["bao"], {"claim": "CL-1", "base_rev": 2, "confidence": "high"})
    assert same["written"] == [] and same["claims"][0]["rev"] == 2  # nothing changed, no rev

    moved = edit(client, hub["alice"], {"claim": "cl-2", "base_rev": 1, "cell": "unique_value_proposition"})
    assert moved["claims"][0]["cell"] == "unique_value_proposition"
    diffed = client.get(f"{BASE}/canvases/{SLUG}/claims/CL-1/diff", params={"base": 1}, headers=hub["eve"]).json()
    assert (diffed["base"], diffed["head"]) == (1, 2)
    assert [c["field"] for c in diffed["changes"]] == ["text", "confidence"]
    wrong = edit(client, hub["alice"], {"cell": "moon", "text": "x"}, expect=422)
    assert wrong["error"] == "invalid_request"
    lacking = edit(client, hub["alice"], {"cell": "channels"}, expect=422)
    assert lacking["message"] == "a new claim takes a cell and a text"


def test_canvas_an_edit_at_an_old_rev_is_a_409_with_both_contents_and_nothing_is_written(client, hub):
    edit(client, hub["alice"], problem(), problem("Owners count stock by hand"))
    edit(client, hub["bao"], {"claim": "CL-1", "base_rev": 1, "text": "Shops run out of milk by Thursday"})
    both = (
        {"claim": "CL-2", "base_rev": 1, "confidence": "low"},
        {"claim": "CL-1", "base_rev": 1, "text": "Alice's older read"},
    )
    refused = edit(client, hub["alice"], *both, expect=409)
    assert "CL-1 changed since you read them: nothing was written" in refused["message"]
    [conflict] = refused["detail"]
    assert (conflict["claim"], conflict["base_rev"], conflict["rev"], conflict["changed_by"]) == ("CL-1", 1, 2, "bao")
    assert conflict["base"]["text"] == "Corner shops run out of stock between deliveries"
    assert conflict["current"]["text"] == "Shops run out of milk by Thursday"
    assert conflict["changes"] == [
        {
            "field": "text",
            "before": "Corner shops run out of stock between deliveries",
            "after": "Shops run out of milk by Thursday",
        }
    ]
    assert shown(client, hub["eve"], "CL-2")["confidence"] == "medium"  # the whole edit was refused
    missing = edit(client, hub["alice"], {"claim": "CL-2", "confidence": "low"}, expect=422)
    assert "names the rev it was read at (base_rev)" in missing["message"]
    twice = edit(client, hub["alice"], {"claim": "CL-2", "base_rev": 1}, {"claim": "2", "base_rev": 1}, expect=422)
    assert "CL-2 is named twice" in twice["message"]
    nowhere = edit(client, hub["alice"], {"claim": "CL-9", "base_rev": 1, "text": "x"}, expect=404)
    assert nowhere["message"] == f"no claim CL-9 in canvas {SLUG} of project {PROJECT}"


def test_canvas_undo_writes_the_rev_before_as_a_new_rev_and_undoing_a_first_rev_archives(client, hub):
    edit(client, hub["alice"], problem(), problem("Owners count stock by hand"))
    edit(client, hub["bao"], {"claim": "CL-1", "base_rev": 1, "text": "Shops run out of milk", "status": "invalidated"})
    undone = undo(client, hub["alice"], ("CL-1", 2))["claims"][0]
    assert (undone["rev"], undone["text"], undone["status"]) == (
        3,
        "Corner shops run out of stock between deliveries",
        "assumption",
    )
    last = shown(client, hub["eve"], "CL-1")["revs"][-1]
    assert (last["change"], last["undo_of"], last["provenance"]["login"]) == ("undo", 2, "alice")
    stale = undo(client, hub["alice"], ("CL-1", 2), expect=409)
    assert stale["detail"][0]["rev"] == 3
    redone = undo(client, hub["alice"], ("CL-1", 3))["claims"][0]
    assert (redone["rev"], redone["text"]) == (4, "Shops run out of milk")  # undoing an undo brings the edit back

    gone = undo(client, hub["bao"], ("CL-2", 1))["claims"][0]
    assert (gone["rev"], gone["archived"]) == (2, True)
    canvas = client.get(f"{BASE}/canvases/{SLUG}", headers=hub["eve"]).json()
    assert [c["code"] for cell in canvas["cells"] for c in cell["claims"]] == ["CL-1"]
    with_archived = client.get(f"{BASE}/canvases/{SLUG}", params={"archived": True}, headers=hub["eve"]).json()
    assert [c["code"] for cell in with_archived["cells"] for c in cell["claims"]] == ["CL-1", "CL-2"]
    back = undo(client, hub["bao"], ("CL-2", 2))["claims"][0]
    assert (back["rev"], back["archived"]) == (3, False)


def test_canvas_testing_needs_a_test_card_with_a_hypothesis_a_test_and_a_deadline(client, hub):
    edit(client, hub["alice"], problem())
    partial = {"hypothesis": "Shop owners reorder weekly"}
    refused = edit(
        client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": partial}, expect=422
    )
    assert refused["message"].endswith("it lacks test, deadline")
    card = {**CARD, "metric": "pantry.reorder.weekly_share", "aggregate": "mean", "comparator": ">=", "threshold": 0.6}
    testing = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "status": "testing", "test_card": card})
    held = testing["claims"][0]["test_card"]
    assert (held["deadline"], held["threshold"], held["metric"]) == ("2026-11-30", 0.6, "pantry.reorder.weekly_share")
    dropped = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 2, "test_card": None}, expect=422)
    assert "goes to testing only with a test card" in dropped["message"]
    bad = edit(
        client, hub["alice"], {"claim": "CL-1", "base_rev": 2, "test_card": {"metric": "Weekly Share"}}, expect=422
    )
    assert "test_card.metric is a signal's key" in bad["message"]
    retired = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 2, "status": "retired"})
    assert retired["claims"][0]["status"] == "retired"  # leaving a hypothesis takes nothing


def test_canvas_validated_needs_real_evidence_that_supports_it(client, hub):
    edit(client, hub["alice"], problem())
    to_validated = {"claim": "CL-1", "base_rev": 1, "status": "validated"}
    none = edit(client, hub["alice"], to_validated, expect=422)
    assert none["message"].endswith("no evidence supports it")
    doc = evidence(client, hub["alice"], "CL-1", kind="doc", summary="The master plan says so")
    simulated = evidence(client, hub["alice"], "CL-1", kind="simulation", summary="Twenty simulated owners agreed")
    synthetic = evidence(client, hub["alice"], "CL-1", synthetic=True, summary="An interview with a persona")
    against = evidence(client, hub["bao"], "CL-1", direction="contradicts", summary="Two owners reorder daily")
    assert simulated["synthetic"] and not simulated["real"]  # a simulation is always synthetic
    assert against["real"] and against["not_real_because"] is None
    refused = edit(client, hub["alice"], to_validated, expect=422)
    assert f"evidence {doc['id']}: it is of kind doc" in refused["message"]
    assert f"evidence {simulated['id']}: it is synthetic" in refused["message"]
    assert f"evidence {synthetic['id']}: it is synthetic" in refused["message"]
    hub_kind = evidence(client, hub["alice"], "CL-1", kind="signal", expect=422)
    assert hub_kind["error"] == "invalid_request"

    real = evidence(client, hub["bao"], "CL-1", url="https://example.invalid/notes/1", observed_on="2026-10-20")
    assert real["real"] and real["provenance"] == {
        "actor": "person",
        "login": "bao",
        "run_id": None,
        "runtime": None,
        "model": None,
        "chat_message_id": None,
    }
    assert shown(client, hub["eve"], "CL-1")["rev"] == 1  # evidence is no part of a rev
    path = f"{BASE}/canvases/{SLUG}/claims/CL-1/evidence/{real['id']}/withdraw"
    withdrawn = client.post(path, json={"reason": "The notes were of another shop"}, headers=hub["alice"])
    assert withdrawn.status_code == 200 and withdrawn.json()["not_real_because"] == "it was withdrawn"
    assert client.post(path, json={"reason": "again"}, headers=hub["alice"]).status_code == 409
    assert edit(client, hub["alice"], to_validated, expect=422)["message"].endswith("it was withdrawn")
    evidence(client, hub["bao"], "CL-1", summary="Nine of ten owners reorder on Mondays")
    validated = edit(client, hub["alice"], to_validated)["claims"][0]
    assert (validated["status"], validated["real_support_count"], validated["evidence_count"]) == ("validated", 1, 6)


def test_canvas_evidence_an_agent_added_counts_once_a_writer_confirms_it(client, hub, hub_db):
    edit(client, hub["alice"], problem())
    [(claim_id,)] = sql(hub_db, select(tables.canvas.claims.c.id).where(tables.canvas.claims.c.number == 1))
    [(alice,)] = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == "alice"))
    e = tables.canvas.evidence
    added = insert(e).values(
        claim_id=claim_id,
        kind="interview",
        direction="supports",
        summary="Notes of an interview the agent typed up",
        label={"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]},
        actor="agent",
        user_id=alice,
        model="claude-test",
    )
    [(evidence_id,)] = sql(hub_db, added.returning(e.c.id))
    item = shown(client, hub["eve"], "CL-1")["evidence"][0]
    assert item["not_real_because"] == "an agent added it and no writer confirmed it"
    assert (item["provenance"]["actor"], item["provenance"]["model"]) == ("agent", "claude-test")
    refused = edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "status": "validated"}, expect=422)
    assert "no writer confirmed it" in refused["message"]
    path = f"{BASE}/canvases/{SLUG}/claims/CL-1/evidence/{evidence_id}/confirm"
    assert client.post(path, headers=hub["eve"]).status_code == 403
    confirmed = client.post(path, headers=hub["bao"])
    assert confirmed.status_code == 200 and confirmed.json()["confirmed_by"] == "bao" and confirmed.json()["real"]
    assert client.post(path, headers=hub["bao"]).status_code == 409
    person = evidence(client, hub["alice"], "CL-1")
    not_agents = client.post(f"{BASE}/canvases/{SLUG}/claims/CL-1/evidence/{person['id']}/confirm", headers=hub["bao"])
    assert not_agents.status_code == 409 and "needs no confirming" in not_agents.json()["message"]
    assert edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "status": "validated"})["claims"][0]["rev"] == 2


def test_canvas_undo_back_to_validated_checks_the_evidence_again(client, hub):
    edit(client, hub["alice"], problem())
    proof = evidence(client, hub["bao"], "CL-1")
    edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 1, "status": "validated"})
    edit(client, hub["alice"], {"claim": "CL-1", "base_rev": 2, "status": "invalidated"})
    path = f"{BASE}/canvases/{SLUG}/claims/CL-1/evidence/{proof['id']}/withdraw"
    assert client.post(path, json={"reason": "Wrong shop"}, headers=hub["bao"]).status_code == 200
    refused = undo(client, hub["alice"], ("CL-1", 3), expect=422)
    assert "CL-1 goes to validated only with real evidence" in refused["message"]
    assert shown(client, hub["eve"], "CL-1")["rev"] == 3


def test_canvas_export_and_import_keep_the_codes_and_an_import_checks_the_rev(client, hub):
    edit(client, hub["alice"], problem(), {"cell": "key_metrics", "text": "Share of shops reordering weekly"})
    evidence(client, hub["bao"], "CL-1")
    exported = client.get(f"{BASE}/canvases/{SLUG}/export", headers=hub["eve"]).json()
    assert exported["format"] == "evo-canvas/1" and exported["canvas"]["slug"] == SLUG
    assert [(c["code"], c["rev"], len(c["evidence"])) for c in exported["claims"]] == [("CL-1", 1, 1), ("CL-2", 1, 0)]

    file = copy.deepcopy(exported)
    file["claims"][0]["text"] = "Shops run out of milk by Thursday"
    file["claims"][0]["evidence"].append({"kind": "market", "direction": "supports", "summary": "A trade survey"})
    file["claims"].append({"code": "CL-40", "cell": "channels", "text": "Wholesalers' reps", "status": "assumption"})
    file["claims"].append({"cell": "channels", "text": "Shop owners' group chats"})
    body = {"data": file, "dry_run": True}
    preview = client.post(f"{BASE}/canvases/import", json=body, headers=hub["alice"])
    assert preview.status_code == 200, preview.text
    assert (preview.json()["created"], preview.json()["edited"], preview.json()["unchanged"]) == (
        ["CL-40", "CL-41"],
        ["CL-1"],
        ["CL-2"],
    )
    assert shown(client, hub["eve"], "CL-1")["rev"] == 1  # a dry run writes nothing
    done = client.post(f"{BASE}/canvases/import", json={"data": file}, headers=hub["alice"]).json()
    assert (done["dry_run"], done["created"], done["edited"], done["evidence"]) == (
        False,
        ["CL-40", "CL-41"],
        ["CL-1"],
        1,
    )
    claim = shown(client, hub["eve"], "CL-1")
    assert (claim["rev"], claim["text"], claim["revs"][-1]["change"]) == (
        2,
        "Shops run out of milk by Thursday",
        "import",
    )
    assert [item["kind"] for item in claim["evidence"]] == ["interview", "market"]

    stale = client.post(f"{BASE}/canvases/import", json={"data": exported}, headers=hub["alice"])
    assert stale.status_code == 409 and [c["claim"] for c in stale.json()["detail"]] == ["CL-1"]
    again = client.post(f"{BASE}/canvases/import", json={"data": file}, headers=hub["alice"])
    assert again.status_code == 422 and again.json()["detail"][0]["claim"] == "CL-40"  # it holds CL-40 now: which rev?
    elsewhere = copy.deepcopy(exported)
    elsewhere["canvas"] = {"slug": "lumen-notes", "name": "Lumen Notes"}
    refused = client.post(f"{BASE}/canvases/import", json={"data": elsewhere}, headers=hub["alice"])
    assert refused.status_code == 422
    assert {p["message"] for p in refused.json()["detail"]} == {"another canvas of the project holds this code"}
    assert client.get(f"{BASE}/canvases/lumen-notes", headers=hub["eve"]).status_code == 404  # nothing written
    fresh = {"format": "evo-canvas/1", "canvas": {"slug": "lumen-notes", "name": "Lumen Notes"}, "claims": []}
    fresh["claims"] = [{"code": "CL-77", "cell": "problem", "text": "Students lose notes", "archived": True}]
    made_new = client.post(f"{BASE}/canvases/import", json={"data": fresh}, headers=hub["bao"]).json()
    assert (made_new["canvas_created"], made_new["created"]) == (True, ["CL-77"])
    assert shown(client, hub["eve"], "CL-77", slug="lumen-notes")["archived"]
    garbage = client.post(f"{BASE}/canvases/import", json={"data": {"format": "evo-canvas/9"}}, headers=hub["bao"])
    assert garbage.status_code == 422 and "is no evo-canvas/1 canvas" in garbage.json()["message"]
    reader = client.post(f"{BASE}/canvases/import", json={"data": fresh}, headers=hub["eve"])
    assert reader.status_code == 403


def test_canvas_readers_read_and_admins_label_and_archive(client, hub):
    edit(client, hub["alice"], problem())
    assert edit(client, hub["eve"], problem(), expect=403)["message"].endswith("needs the writer role; you are reader")
    assert client.post(f"{BASE}/canvases/{SLUG}/archive", headers=hub["alice"]).status_code == 403
    archived = client.post(f"{BASE}/canvases/{SLUG}/archive", headers=hub["owner"])
    assert archived.status_code == 200 and archived.json()["archived_at"] is not None
    assert "is archived and takes no change" in edit(client, hub["alice"], problem(), expect=409)["message"]
    assert client.get(f"{BASE}/canvases", headers=hub["eve"]).json() == []
    assert [
        c["slug"] for c in client.get(f"{BASE}/canvases", params={"archived": True}, headers=hub["eve"]).json()
    ] == [SLUG]
    assert client.post(f"{BASE}/canvases/{SLUG}/unarchive", headers=hub["owner"]).status_code == 200
    assert edit(client, hub["alice"], problem("Owners count stock by hand"))["written"] == ["CL-2"]

    label = {"level": "internal", "location": "any"}
    refused = made(client, hub["alice"], slug="lumen-notes", label=label, expect=403)
    assert "takes an admin" in refused["message"]
    assert made(client, hub["owner"], slug="lumen-notes", label=label)["label"]["level"] == "internal"


def test_canvas_a_label_above_the_readers_grant_hides_the_canvas(client, hub):
    edit(client, hub["alice"], problem())
    assert client.get(f"{BASE}/canvases", headers=hub["outsider"]).json() == []
    assert client.get(f"{BASE}/canvases/{SLUG}", headers=hub["outsider"]).status_code == 404
    assert client.get(f"{BASE}/canvases/{SLUG}/claims/CL-1", headers=hub["outsider"]).status_code == 404
    assert client.get(f"{BASE}/canvases/{SLUG}/export", headers=hub["outsider"]).status_code == 404


def test_canvas_tables_refuse_rows_their_checks_forbid(client, hub, hub_db):
    edit(client, hub["alice"], problem())
    [(claim_id,)] = sql(hub_db, select(tables.canvas.claims.c.id).where(tables.canvas.claims.c.number == 1))
    [(alice,)] = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == "alice"))
    e, r = tables.canvas.evidence, tables.canvas.revs
    piece = {"claim_id": claim_id, "direction": "supports", "summary": "x", "label": {}, "actor": "person"}
    with pytest.raises(psycopg.errors.CheckViolation):  # a simulation is always synthetic
        sql(hub_db, insert(e).values(**piece, kind="simulation", user_id=alice))
    with pytest.raises(psycopg.errors.CheckViolation):  # a person's evidence names the person
        sql(hub_db, insert(e).values(**piece, kind="interview"))
    rev = {"claim_id": claim_id, "rev": 2, "content": {}, "actor": "person", "user_id": alice}
    with pytest.raises(psycopg.errors.CheckViolation):  # an undo names the rev it took back
        sql(hub_db, insert(r).values(**rev, change="undo"))
    with pytest.raises(psycopg.errors.UniqueViolation):  # one row per rev
        sql(hub_db, insert(r).values(**{**rev, "rev": 1}, change="edit"))

"""The core plugin srs on the hub (``evo_agents.core_plugins.srs``), as criterion a2 of plan srs-tasks-plugins names
it: a project that enabled it keeps BA documents. A writer imports the sample SRS (tests/hub/fixtures/srs, no client's
content) after a preview; every save is a version in one straight line; codes are unique in the project, aliases
included, stay when an item is renamed, and are never given again; a version goes from draft to review to approved,
approved by a writer who wrote none of the versions since the one approved last, as the config's policy asks; its
author or an admin withdraws a version in review for good (criterion a2 of plan srs-document-lifecycle); two versions
compare item by item; a version exports as the Markdown it came from. Readers read, writers write, admins
drop coded items; a document carries the project's label and reads follow it. A project that did not enable the
plugin answers 404.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import asyncio

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.core_plugins.srs import markdown, reviews
from evo_agents.core_plugins.srs.doc import SrsError
from evo_agents.hub import plugin_calls, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import Principal, RunScope
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_plans import registration
from tests.hub.test_runs import PROJECT

SRS = EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP)
SAMPLE = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
BASE = f"/v1/x/srs/{PROJECT}"
DOC = f"{BASE}/documents/demo-shop"
MEMBERS = {  # login -> (github id, role, max level)
    "owner": (601, "admin", "internal"),
    "alice": (602, "writer", "internal"),
    "bao": (603, "writer", "internal"),
    "chi": (604, "writer", "internal"),
    "eve": (605, "reader", "internal"),
    "outsider": (606, "reader", "public"),
}


def sample() -> str:
    return SAMPLE.read_text(encoding="utf-8")


@pytest.fixture
def srs_core(monkeypatch):
    """The entry points of the core plugins with srs's, as the package installed declares it."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name != "srs"] + [SRS]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, srs_core):
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


def enable(client, headers) -> None:
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/srs/enable", json={"permissions": []}, headers=headers)
    assert enabled.status_code == 200, enabled.text


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with plugin srs enabled in the project."""
    enable(client, members["owner"])
    return members


def imported(client, headers, text: str | None = None, **body) -> dict:
    answer = client.post(
        f"{BASE}/import", json={"markdown": sample() if text is None else text, **body}, headers=headers
    )
    assert answer.status_code == 200, answer.text
    return answer.json()


def item(client, headers, ref: str, path: str = DOC) -> dict:
    found = client.get(f"{path}/items/{ref}", headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def saved(client, headers, base: int, *changes: dict, path: str = DOC, status: int = 201):
    answer = client.post(f"{path}/versions", json={"base_version": base, "changes": list(changes)}, headers=headers)
    assert answer.status_code == status, answer.text
    return answer.json()


def edit(key: str, **values) -> dict:
    return {"key": key, **values}


def review(client, headers, number: int, decision: str = "approve", **body):
    return client.post(f"{DOC}/versions/{number}/reviews", json={"decision": decision, **body}, headers=headers)


def submitted(client, headers, number: int) -> dict:
    answer = client.post(f"{DOC}/versions/{number}/submit", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def withdraw(client, headers, number: int, **body):
    return client.post(f"{DOC}/versions/{number}/withdraw", json=body or None, headers=headers)


def states(client, headers) -> dict[int, str]:
    return {v["number"]: v["state"] for v in client.get(f"{DOC}/versions", headers=headers).json()}


# Enabling


def test_srs_routes_answer_404_where_the_project_has_not_enabled_the_plugin(client, members):
    assert client.get(f"{BASE}/documents", headers=members["alice"]).status_code == 404
    enable(client, members["owner"])
    assert client.get(f"{BASE}/documents", headers=members["alice"]).json() == []
    disabled = client.post(f"/v1/projects/{PROJECT}/plugins/srs/disable", headers=members["owner"])
    assert disabled.status_code == 200, disabled.text
    answer = client.post(f"{BASE}/import", json={"markdown": sample()}, headers=members["alice"])
    assert answer.status_code == 404 and "not enabled" in answer.json()["message"]


# Importing and exporting


def test_srs_import_shows_a_preview_then_keeps_the_document_as_its_first_draft(client, hub):
    preview = imported(client, hub["alice"], dry_run=True)
    assert preview["dry_run"] and preview["version"] is None and preview["document"] == "demo-shop-order-service"
    counts = preview["preview"]["counts"]
    assert (counts["use_case"], counts["business_rule"], counts["screen"]) == (3, 4, 2)
    assert (counts["nfr"], counts["cbr"]) == (2, 2)
    assert preview["preview"]["problems"] == []
    assert client.get(f"{BASE}/documents", headers=hub["alice"]).json() == []

    done = imported(client, hub["alice"], slug="demo-shop", summary="first import")
    assert (done["document"], done["created"], done["version"]) == ("demo-shop", True, 1)
    [listed] = client.get(f"{BASE}/documents", headers=hub["eve"]).json()
    assert listed["title"] == "Demo Shop: Order Service" and listed["kind"] == "srs"
    assert (listed["latest_version"], listed["latest_state"], listed["approved_version"]) == (1, "draft", None)
    assert listed["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    shown = client.get(DOC, headers=hub["eve"]).json()
    assert [version["author"] for version in shown["versions"]] == ["alice"]
    screens = [entry["code"] for entry in shown["shown"]["items"] if entry["kind"] == "screen"]
    assert screens == ["SCR-1", "SCR-2"]
    order = item(client, hub["eve"], "UC ORD-1")
    assert order["aliases"] == ["UC-ORD-001", "ORD-01"] and order["fields"]["actor"] == "Customer"
    assert [child["code"] for child in order["children"]] == ["BR ORD1-1", "BR ORD1-2"]
    assert item(client, hub["eve"], "ord-01")["key"] == order["key"]


def test_srs_export_gives_the_imported_markdown_back_with_the_same_codes(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    plain = client.get(f"{DOC}/export", params={"anchors": "false"}, headers=hub["eve"]).json()
    assert plain["version"] == 1 and plain["markdown"] == markdown.normalize(sample())
    anchored = client.get(f"{DOC}/export", headers=hub["eve"]).json()["markdown"]
    assert "<!-- srs: SCR-1 -->" in anchored and "<!-- srs: SCR-2 -->" in anchored
    again = client.post(f"{BASE}/import", json={"markdown": anchored, "document": "demo-shop"}, headers=hub["bao"])
    assert again.status_code == 409 and "nothing changed since version 1" in again.json()["message"]
    renamed = anchored.replace("### Screen: Cart", "### Screen: Basket")
    done = imported(client, hub["bao"], renamed, document="demo-shop", dry_run=True)
    assert done["preview"]["changed"] == ["SCR-1"] and done["preview"]["added"] == []


def test_srs_a_new_version_imported_keeps_every_key_and_says_what_it_adds_changes_and_drops(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    keys = {entry["code"]: entry["key"] for entry in client.get(DOC, headers=hub["eve"]).json()["shown"]["items"]}
    text = (
        sample()
        .replace("50 lines", "60 lines")
        .replace("| CBR 2 | Times are shown in the customer's time zone |\n", "")
    )
    text = text.replace(
        "| NFR-PERF-01 |", "| NFR-PERF-02 | Performance | Search answers within a second |\n| NFR-PERF-01 |"
    )
    preview = imported(client, hub["owner"], text, document="demo-shop", dry_run=True)["preview"]
    assert (preview["added"], preview["changed"], preview["removed"]) == (["NFR-PERF-02"], ["BR ORD1-1"], ["CBR 2"])
    refused = client.post(f"{BASE}/import", json={"markdown": text, "document": "demo-shop"}, headers=hub["bao"])
    assert refused.status_code == 422 and refused.json()["detail"][0]["check"] == "coded_item_dropped"
    done = imported(client, hub["owner"], text, document="demo-shop", summary="perf")
    assert (done["created"], done["version"]) == (False, 2)
    after = {entry["code"]: entry["key"] for entry in client.get(DOC, headers=hub["eve"]).json()["shown"]["items"]}
    assert all(after[code] == key for code, key in keys.items() if code not in (None, "CBR 2"))
    assert "CBR 2" not in after


# Codes


def test_srs_codes_are_unique_in_the_project_stay_on_rename_and_are_never_given_again(client, hub, hub_db):
    imported(client, hub["alice"], slug="demo-shop")
    other = "# 1. Payments\n\n### UC ORD-1: Refund\n\n| Item | Content |\n|---|---|\n| Actor | Clerk |\n"
    refused = client.post(f"{BASE}/import", json={"markdown": other, "slug": "refunds"}, headers=hub["bao"])
    assert refused.status_code == 422
    assert [(p["check"], p["code"]) for p in refused.json()["detail"]] == [("code_taken", "UC ORD-1")]
    alias = other.replace("UC ORD-1", "UC PAY-1").replace("| Actor |", "| ID | UC-PAY-001 (ord-01) |\n| Actor |")
    refused = client.post(f"{BASE}/import", json={"markdown": alias, "slug": "refunds"}, headers=hub["bao"])
    assert refused.status_code == 422 and "another document" in refused.json()["detail"][0]["message"]

    order = item(client, hub["alice"], "UC ORD-1")
    renamed = saved(client, hub["bao"], 1, edit(order["key"], title="Place and confirm an order"))
    assert renamed["number"] == 2
    again = item(client, hub["alice"], "UC ORD-1")
    assert (again["key"], again["title"], again["code"]) == (order["key"], "Place and confirm an order", "UC ORD-1")

    ret = item(client, hub["alice"], "UC RET-1")
    saved(client, hub["owner"], 2, edit(ret["key"], retire=True))
    reuse = "# 1. Returns\n\n### UC RET-1: Return again\n\n| Item | Content |\n|---|---|\n| Actor | Customer |\n"
    refused = client.post(f"{BASE}/import", json={"markdown": reuse, "slug": "returns"}, headers=hub["owner"])
    assert refused.status_code == 422 and "never given again" in refused.json()["detail"][0]["message"]
    i = tables.srs.items
    retired = sql(hub_db, select(i.c.code, i.c.retired_in).where(i.c.code.in_(["UC RET-1", "BR RET1-1"])))
    assert sorted(retired) == [("BR RET1-1", 3), ("UC RET-1", 3)]


# Saving


def test_srs_saves_merge_edits_of_different_items_and_refuse_a_conflict(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    cart, stock = item(client, hub["alice"], "BR ORD1-1"), item(client, hub["alice"], "BR ORD1-2")
    first = saved(client, hub["alice"], 1, edit(cart["key"], body=cart["body"].replace("50 lines", "40 lines")))
    second = saved(client, hub["bao"], 1, edit(stock["key"], body=stock["body"].replace("15 minutes", "20 minutes")))
    assert (first["number"], second["number"], second["parent_number"]) == (2, 3, 2)
    bodies = {entry["code"]: entry["body"] for entry in second["items"]}
    assert "40 lines" in bodies["BR ORD1-1"] and "20 minutes for 15" not in bodies["BR ORD1-2"]
    assert "holds its stock for 20 minutes" in bodies["BR ORD1-2"]
    conflict = saved(client, hub["chi"], 1, edit(cart["key"], body="- A cart holds anything."), status=409)
    assert conflict["error"] == "conflict" and "changed since version 1" in conflict["message"]
    [both] = conflict["detail"]
    assert "50 lines" in both["base"] and "40 lines" in both["latest"] and both["code"] == "BR ORD1-1"
    nothing = saved(client, hub["chi"], 3, edit(cart["key"], body=bodies["BR ORD1-1"]), status=409)
    assert "nothing changed" in nothing["message"]


def test_srs_saving_adds_items_links_screens_and_drops_coded_items_only_for_an_admin(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    order, nfr = item(client, hub["alice"], "UC ORD-1"), item(client, hub["alice"], "NFR-SEC-01")
    new_rule = {"new": {"kind": "business_rule", "code": "BR ORD1-3", "title": "Gift cards", "parent": order["key"]}}
    new_rule["new"] |= {"body": "- A gift card pays part of an order.", "steps": ["3"]}
    row = {"new": {"kind": "nfr", "title": "x", "code": "NFR-SEC-02", "parent": nfr["parent"]}}
    row["new"]["body"] = "| NFR-SEC-02 | Security | Sessions end after 30 minutes idle |"
    link = edit(order["key"], refs=[{"rel": "uses_screen", "to": "SCR-1"}])
    detail = saved(client, hub["alice"], 1, new_rule, row, link)
    by_code = {entry["code"]: entry for entry in detail["items"]}
    assert by_code["BR ORD1-3"]["parent"] == order["key"] and by_code["BR ORD1-3"]["fields"]["steps"] == ["3"]
    assert by_code["NFR-SEC-02"]["title"] == "Sessions end after 30 minutes idle"
    assert by_code["UC ORD-1"]["refs"] == [{"rel": "uses_screen", "to": by_code["SCR-1"]["key"], "to_code": "SCR-1"}]
    exported = client.get(f"{DOC}/export", params={"anchors": "false"}, headers=hub["eve"]).json()["markdown"]
    assert "##### Step (3): BR ORD1-3 – Gift cards" in exported
    assert "| NFR-PERF-01 | Performance | Placing an order answers within 2 seconds |\n| NFR-SEC-02 |" in exported

    dropped = saved(client, hub["bao"], 2, edit(by_code["BR ORD1-3"]["key"], retire=True), status=422)
    assert dropped["detail"][0]["check"] == "coded_item_dropped"
    taken = {"new": {"kind": "use_case", "code": "UC ORD-1", "title": "Again"}}
    refused = saved(client, hub["bao"], 2, taken, status=422)["detail"]
    assert {problem["check"] for problem in refused} == {"code_duplicate", "code_taken"}
    bad = {"new": {"kind": "use_case", "code": "UC 1", "title": "Bad"}}
    assert saved(client, hub["bao"], 2, bad, status=422)["detail"][0]["check"] == "code_pattern"
    gone = saved(client, hub["owner"], 2, edit(by_code["BR ORD1-3"]["key"], retire=True))
    assert "BR ORD1-3" not in {entry["code"] for entry in gone["items"]}


# Reviews


def test_srs_a_version_is_approved_by_a_writer_who_wrote_none_since_the_last_approved(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    assert review(client, hub["bao"], 1).status_code == 409  # a draft is not reviewed
    assert submitted(client, hub["alice"], 1)["state"] == "in_review"
    author = review(client, hub["alice"], 1)
    assert author.status_code == 403 and "another member reviews it" in author.json()["message"]
    assert review(client, hub["eve"], 1).status_code == 403  # a reader
    returned = review(client, hub["bao"], 1, "request_changes", body="Name the gateway")
    assert returned.status_code == 200 and returned.json()["state"] == "draft"
    assert submitted(client, hub["alice"], 1)["round"] == 2
    approved = review(client, hub["bao"], 1)
    assert approved.status_code == 200 and approved.json()["state"] == "approved"
    assert client.get(DOC, headers=hub["eve"]).json()["approved_version"] == 1

    cart = item(client, hub["alice"], "BR ORD1-1")
    saved(client, hub["bao"], 1, edit(cart["key"], body="- A cart holds at most 40 lines."))
    stock = item(client, hub["alice"], "BR ORD1-2")
    saved(client, hub["owner"], 2, edit(stock["key"], body="- Stock is held for 20 minutes."))
    submitted(client, hub["owner"], 3)
    for writer in ("bao", "owner"):  # each wrote a version since version 1
        assert review(client, hub[writer], 3).status_code == 403, writer
    assert review(client, hub["alice"], 3).json()["state"] == "approved"
    states = {v["number"]: v["state"] for v in client.get(f"{DOC}/versions", headers=hub["eve"]).json()}
    assert states == {1: "superseded", 2: "superseded", 3: "approved"}
    assert review(client, hub["chi"], 3).status_code == 409  # approved already


def test_srs_the_approval_policy_asks_each_required_role(client, hub):
    config = {"roles": {"ba": ["bao"], "qa": ["chi"], "dev": ["owner"]}}
    config["approval_policy"] = {"srs": {"required": ["ba", "qa"], "optional": ["dev"]}}
    written = client.post(f"/v1/projects/{PROJECT}/plugins/srs/config", json={"values": config}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    imported(client, hub["alice"], slug="demo-shop")
    submitted(client, hub["alice"], 1)
    not_held = review(client, hub["bao"], 1, role="qa")
    assert not_held.status_code == 403 and "does not hold the role qa" in not_held.json()["message"]
    assert review(client, hub["owner"], 1).json()["state"] == "in_review"  # dev is optional
    assert review(client, hub["bao"], 1).json()["state"] == "in_review"  # ba, still no qa
    done = review(client, hub["chi"], 1).json()
    assert done["state"] == "approved"
    assert [(r["reviewer"], r["role"]) for r in done["reviews"]] == [("owner", "dev"), ("bao", "ba"), ("chi", "qa")]


def test_srs_the_author_withdraws_a_version_in_review_for_good_with_the_reason_as_a_comment(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    early = withdraw(client, hub["alice"], 1)
    assert early.status_code == 409 and "only a version in review is withdrawn" in early.json()["message"]
    submitted(client, hub["alice"], 1)
    for other in ("bao", "eve"):  # another writer, a reader
        refused = withdraw(client, hub[other], 1)
        assert refused.status_code == 403, other
    assert "its author or an admin" in withdraw(client, hub["bao"], 1).json()["message"]
    assert states(client, hub["eve"]) == {1: "in_review"}
    done = withdraw(client, hub["alice"], 1, comment="The gateway is not chosen yet")
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "withdrawn" and done.json()["decided_at"] is not None
    listed = client.get(f"{DOC}/comments", headers=hub["eve"]).json()
    assert [(c["version"], c["author"], c["body"]) for c in listed] == [(1, "alice", "The gateway is not chosen yet")]
    assert client.get(f"{BASE}/documents", headers=hub["eve"]).json()[0]["latest_state"] == "withdrawn"
    # a last state: never withdrawn, submitted, reviewed or approved again
    assert withdraw(client, hub["owner"], 1).status_code == 409
    resubmitted = client.post(f"{DOC}/versions/1/submit", headers=hub["alice"])
    assert resubmitted.status_code == 409 and "is withdrawn: only a draft is submitted" in resubmitted.json()["message"]
    assert review(client, hub["bao"], 1).status_code == 409
    assert review(client, hub["bao"], 1, "request_changes", body="Back to draft").status_code == 409
    assert states(client, hub["eve"]) == {1: "withdrawn"}
    assert len(client.get(f"{DOC}/comments", headers=hub["eve"]).json()) == 1


def test_srs_an_admin_withdraws_a_writers_version_and_approving_a_later_one_leaves_it_withdrawn(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    submitted(client, hub["alice"], 1)
    assert review(client, hub["bao"], 1).json()["state"] == "approved"
    cart = item(client, hub["alice"], "BR ORD1-1")
    saved(client, hub["alice"], 1, edit(cart["key"], body="- A cart holds at most 40 lines."))
    submitted(client, hub["alice"], 2)
    by_admin = withdraw(client, hub["owner"], 2)  # owner wrote no version of it
    assert by_admin.status_code == 200 and by_admin.json()["state"] == "withdrawn", by_admin.text
    assert client.get(f"{DOC}/comments", headers=hub["eve"]).json() == []  # no reason, no comment
    saved(client, hub["alice"], 2, edit(cart["key"], body="- A cart holds at most 30 lines."))
    submitted(client, hub["alice"], 3)
    assert review(client, hub["bao"], 3).json()["state"] == "approved"
    assert states(client, hub["eve"]) == {1: "superseded", 2: "withdrawn", 3: "approved"}
    assert withdraw(client, hub["owner"], 3).status_code == 409  # an approved version stays approved


def test_srs_the_agent_of_a_run_never_withdraws_a_version():
    """The routes reach the agent of a run only through /mcp, which has no tool that withdraws; were one to call
    ``withdraw``, the run's scope refuses it before anything is read."""
    scope = RunScope(run_id=7, project=PROJECT, role="writer", max_level="internal")
    agent = Principal(602, "alice", False, 9, "worker", "a" * 64, scope=scope)
    with pytest.raises(SrsError) as refused:
        asyncio.run(reviews.withdraw(None, None, agent, None, 1))
    assert refused.value.status == 403 and "the agent of a run never withdraws" in refused.value.message


def test_srs_a_version_with_an_error_is_not_submitted(client, hub):
    text = sample().replace(
        "| Actor | Customer |\n| Trigger | The customer presses", "| Trigger | The customer presses"
    )
    imported(client, hub["alice"], text, slug="demo-shop")
    refused = client.post(f"{DOC}/versions/1/submit", headers=hub["alice"])
    assert refused.status_code == 422 and refused.json()["detail"][0]["check"] == "uc_no_actor"
    shown = client.get(f"{DOC}/versions/1", headers=hub["eve"]).json()
    assert [(p["level"], p["check"]) for p in shown["problems"]] == [("error", "uc_no_actor")]


# Comparing, commenting, reading


def test_srs_two_versions_compare_item_by_item(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    cart = item(client, hub["alice"], "BR ORD1-1")
    saved(client, hub["bao"], 1, edit(cart["key"], body=cart["body"].replace("50 lines", "40 lines")))
    found = client.get(f"{DOC}/diff", params={"from": 1, "to": 2}, headers=hub["eve"]).json()
    assert (found["from_version"], found["to_version"], found["preamble"]) == (1, 2, [])
    [change] = found["items"]
    assert (change["code"], change["change"]) == ("BR ORD1-1", "changed")
    assert "-- A cart holds at most 50 lines and 99 of one SKU." in change["lines"]
    assert "+- A cart holds at most 40 lines and 99 of one SKU." in change["lines"]


def test_srs_writers_comment_and_close_threads_readers_read_them(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    made = client.post(
        f"{DOC}/comments", json={"body": "Is 50 right?", "version": 1, "item": "BR ORD1-1"}, headers=hub["bao"]
    )
    assert made.status_code == 201, made.text
    answer = {"body": "Yes, from the shop.", "parent_id": made.json()["id"]}
    assert client.post(f"{DOC}/comments", json=answer, headers=hub["alice"]).status_code == 201
    assert client.post(f"{DOC}/comments", json={"body": "me too"}, headers=hub["eve"]).status_code == 403
    closed = client.post(f"{DOC}/comments/{made.json()['id']}/resolve", headers=hub["alice"])
    assert closed.status_code == 200 and closed.json()["resolved_by"] == "alice"
    listed = client.get(f"{DOC}/comments", headers=hub["eve"]).json()
    assert [(c["author"], c["item_code"], c["version"]) for c in listed] == [
        ("bao", "BR ORD1-1", 1),
        ("alice", None, None),
    ]


def test_srs_readers_read_and_write_nothing_and_a_label_above_their_grant_hides_the_document(client, hub):
    imported(client, hub["alice"], slug="demo-shop")
    eve = hub["eve"]
    assert client.get(f"{DOC}/export", headers=eve).status_code == 200
    assert client.post(f"{BASE}/import", json={"markdown": sample(), "slug": "x"}, headers=eve).status_code == 403
    order = item(client, eve, "UC ORD-1")
    change = {"base_version": 1, "changes": [edit(order["key"], title="Mine")]}
    assert client.post(f"{DOC}/versions", json=change, headers=eve).status_code == 403
    assert client.post(f"{DOC}/versions/1/submit", headers=eve).status_code == 403
    assert client.get(f"{BASE}/documents", headers=hub["outsider"]).json() == []
    assert client.get(DOC, headers=hub["outsider"]).status_code == 404
    label = {"level": "internal"}
    body = {"markdown": "# 1. Notes\n\nnothing\n", "slug": "notes", "label": label}
    assert client.post(f"{BASE}/import", json=body, headers=hub["alice"]).status_code == 403
    assert client.post(f"{BASE}/import", json=body, headers=hub["owner"]).status_code == 200


# The schema


@pytest.mark.parametrize(
    "change",
    [
        lambda s: update(s.versions).values(state="approved", decided_at=None),
        lambda s: update(s.versions).values(state="in_review", submitted_at=None),
        lambda s: update(s.versions).values(state="withdrawn", decided_at=None),
        lambda s: update(s.versions).values(state="retracted"),
        lambda s: update(s.items).values(retired_in=1),
        lambda s: update(s.documents).values(slug="Not A Slug"),
        lambda s: update(s.documents).values(kind="memo"),
        lambda s: update(s.item_versions).values(heading_level=7),
    ],
    ids=[
        "approved-undecided",
        "in-review-unsubmitted",
        "withdrawn-undecided",
        "state",
        "retired-before-made",
        "slug",
        "kind",
        "level",
    ],
)
def test_srs_tables_refuse_rows_their_checks_forbid(client, hub, hub_db, change):
    imported(client, hub["alice"], slug="demo-shop")
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, change(tables.srs))

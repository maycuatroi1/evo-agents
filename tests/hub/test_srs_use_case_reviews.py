"""The review of single use cases of a version in review, on the hub (``evo_agents.core_plugins.srs.use_case_reviews``,
``reviews.review_use_case``), as criteria a1 to a4 of the plan srs-use-case-review name them. The sample is
``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes version 1, whose three use cases are all to review."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_ba_agent import DOC, client, hub, imported, item, members, saved, srs_core, submitted, version  # noqa: F401
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_srs_events import stored  # noqa: F401

CASES = ["UC ORD-1", "UC ORD-2", "UC RET-1"]


def one(client, headers, number: int, ref: str, decision: str = "approve", **body):
    return client.post(
        f"{DOC}/versions/{number}/items/{ref}/reviews", json={"decision": decision, **body}, headers=headers
    )


def states(client, headers, number: int) -> dict:
    return {c["code"]: c["state"] for c in version(client, headers, number)["use_cases"]}


def test_use_case_reviews_every_use_case_approved_approves_the_version_and_submit_never_does(client, hub, hub_db):
    imported(client, hub["chi"])
    assert version(client, hub["chi"], 1)["use_cases"][0]["state"] == "pending"
    assert submitted(client, hub["chi"], 1)["state"] == "in_review"
    assert states(client, hub["reader"], 1) == dict.fromkeys(CASES, "pending")
    for ref in CASES[:2]:
        done = one(client, hub["other"], 1, ref)
        assert done.status_code == 200, done.text
        assert done.json()["state"] == "in_review"
    assert stored(hub_db) == []
    last = one(client, hub["other"], 1, "UC RET-1", body="Fine")
    assert last.status_code == 200, last.text
    assert last.json()["state"] == "approved"
    assert {c["code"]: c["state"] for c in last.json()["use_cases"]} == dict.fromkeys(CASES, "approved")
    assert states(client, hub["reader"], 1) == {}, "a version decided lists no use case to review"
    (event,) = stored(hub_db)
    assert (event["version"], event["document"]) == (1, "demo-shop")
    first = last.json()["use_cases"][2]
    assert (first["code"], first["change"]) == ("UC RET-1", "added")
    assert [(r["reviewer"], r["decision"], r["version"]) for r in first["reviews"]] == [("someone-else", "approve", 1)]


def test_use_case_reviews_the_conditions_of_a_version_review_hold(client, hub):
    imported(client, hub["chi"])
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 409  # a draft
    submitted(client, hub["chi"], 1)
    assert one(client, hub["chi"], 1, "UC ORD-1").status_code == 403  # its author
    assert one(client, hub["reader"], 1, "UC ORD-1").status_code == 403
    missing = one(client, hub["other"], 1, "UC ORD-9")
    assert missing.status_code in (404, 422)
    blank = one(client, hub["other"], 1, "UC ORD-1", "request_changes", body="  ")
    assert blank.status_code == 422 and "note" in blank.json()["message"]
    bad = client.post(f"{DOC}/versions/1/items/UC ORD-1/reviews", json={"decision": "maybe"}, headers=hub["other"])
    assert bad.status_code == 422


def test_use_case_reviews_a_request_for_changes_blocks_every_approval_and_a_later_review_replaces_it(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    asked = one(client, hub["other"], 1, "UC ORD-2", "request_changes", body="Name the gateway")
    assert asked.status_code == 200, asked.text
    assert asked.json()["state"] == "in_review"
    assert states(client, hub["chi"], 1)["UC ORD-2"] == "changes_requested"
    for ref in ("UC ORD-1", "UC RET-1"):
        assert one(client, hub["owner"], 1, ref).json()["state"] == "in_review"
    # a version-wide approval is recorded and still blocked by the request
    wide = whole(client, hub["owner"], 1)
    assert wide.status_code == 200 and wide.json()["state"] == "in_review"
    assert states(client, hub["chi"], 1)["UC ORD-2"] == "changes_requested"
    # the same person approves later: the request no longer counts, the history stays
    final = one(client, hub["other"], 1, "UC ORD-2")
    assert final.json()["state"] == "approved"
    (case,) = [c for c in final.json()["use_cases"] if c["code"] == "UC ORD-2"]
    assert [r["decision"] for r in case["reviews"]] == ["approve"]


def test_use_case_reviews_a_version_wide_request_for_changes_returns_the_version_to_draft(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    one(client, hub["other"], 1, "UC ORD-1")
    returned = whole(client, hub["other"], 1, "request_changes", body="Too vague")
    assert returned.status_code == 200 and returned.json()["state"] == "draft"


def test_use_case_reviews_a_later_version_needs_only_what_it_changes(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"  # version-wide: counts for every use case
    cart = item(client, hub["chi"], "BR ORD1-1")
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."})
    submitted(client, hub["chi"], 2)
    assert states(client, hub["reader"], 2) == {"UC ORD-1": "pending"}
    refused = one(client, hub["other"], 2, "UC ORD-2")
    assert refused.status_code == 422 and "UC ORD-1" in refused.json()["message"]
    done = one(client, hub["other"], 2, "UC ORD-1")
    assert done.status_code == 200 and done.json()["state"] == "approved"
    assert len(stored(hub_db)) == 2

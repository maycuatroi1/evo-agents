"""The review of single use cases of a version in review, on the hub (``evo_agents.hub.server.srs.use_case_reviews``,
``reviews.review_use_case``), as criteria a1 to a4 of the plan srs-use-case-review name them. The sample is
``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes version 1, whose three use cases are all to review."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_ba_agent import (  # noqa: F401
    CHI,
    DOC,
    OTHER,
    OWNER,
    PROJECT,
    client,
    hub,
    imported,
    item,
    members,
    saved,
    srs_core,
    submitted,
    version,
)
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_runs import moved
from tests.hub.test_srs_events import stored  # noqa: F401
from tests.hub.test_srs_findings import ba_in_chat, billing, graph, recorded, reviewing  # noqa: F401
from tests.hub.test_srs_plugin import configured  # noqa: F401

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
    assert stored(hub_db, "srs.version.approved") == []
    last = one(client, hub["other"], 1, "UC RET-1", body="Fine")
    assert last.status_code == 200, last.text
    assert last.json()["state"] == "approved"
    assert {c["code"]: c["state"] for c in last.json()["use_cases"]} == dict.fromkeys(CASES, "approved")
    assert states(client, hub["reader"], 1) == dict.fromkeys(CASES, "approved"), "the version approved last keeps them"
    (event,) = stored(hub_db, "srs.version.approved")
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
    assert len(stored(hub_db, "srs.version.approved")) == 2


def test_use_case_reviews_a_policy_of_two_roles_is_filled_per_use_case_or_by_a_version_wide_approval(client, hub):
    policy = {"srs": {"required": ["ba", "qa"]}}
    configured(client, hub["owner"], {"roles": {"ba": [OTHER], "qa": [OWNER]}, "approval_policy": policy})
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert one(client, hub["reader"], 1, "UC ORD-1", role="qa").status_code == 403
    assert one(client, hub["other"], 1, "UC ORD-1", role="qa").status_code == 403, "the role is not theirs"
    first = one(client, hub["other"], 1, "UC ORD-1")
    assert [(r["reviewer"], r["role"]) for r in first.json()["use_cases"][0]["reviews"]] == [(OTHER, "ba")]
    assert states(client, hub["chi"], 1)["UC ORD-1"] == "pending", "ba alone does not fill the policy"
    both = one(client, hub["owner"], 1, "UC ORD-1")
    assert both.json()["state"] == "in_review" and states(client, hub["chi"], 1)["UC ORD-1"] == "approved"
    assert [r["role"] for r in both.json()["use_cases"][0]["reviews"]] == ["ba", "qa"]
    for ref in ("UC ORD-2", "UC RET-1"):
        assert one(client, hub["other"], 1, ref).json()["state"] == "in_review"
    # qa approves the version as a whole: it counts for the use cases still short of it
    wide = whole(client, hub["owner"], 1)
    assert wide.status_code == 200 and wide.json()["state"] == "approved", wide.text


def test_use_case_reviews_without_a_policy_one_approval_fills_a_use_case_and_roles_may_be_unset(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    done = one(client, hub["other"], 1, "UC ORD-1")
    assert states(client, hub["chi"], 1) == {"UC ORD-1": "approved", "UC ORD-2": "pending", "UC RET-1": "pending"}
    assert [r["role"] for r in done.json()["use_cases"][0]["reviews"]] == [None]


def test_use_case_reviews_the_author_reviews_a_use_case_only_when_self_approval_is_on(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert one(client, hub["chi"], 1, "UC ORD-1").status_code == 403
    configured(client, hub["owner"], {"self_approval": True})
    for ref in CASES:
        assert one(client, hub["chi"], 1, ref).status_code == 200
    assert version(client, hub["chi"], 1)["state"] == "approved"


def test_use_case_reviews_a_request_for_changes_needs_a_note_and_only_a_use_case_the_version_needs(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert one(client, hub["other"], 1, "UC ORD-1", "request_changes").status_code == 422
    assert one(client, hub["other"], 1, "UC ORD-1", "request_changes", body="").status_code == 422
    rule = one(client, hub["other"], 1, "BR ORD1-1")
    assert rule.status_code == 422 and "is not a use case" in rule.json()["message"], rule.text
    assert one(client, hub["other"], 1, "NFR-SEC-01").status_code == 422
    assert states(client, hub["chi"], 1) == dict.fromkeys(CASES, "pending"), "a refusal records nothing"


def test_use_case_reviews_a_version_with_no_use_case_to_review_is_approved_by_version_wide_reviews_alone(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    nfr = item(client, hub["chi"], "NFR-PERF-01")
    saved(
        client,
        hub["chi"],
        1,
        {"key": nfr["key"], "body": "| NFR-PERF-01 | Performance | Placing an order answers within 1 second |"},
    )
    submitted(client, hub["chi"], 2)
    assert version(client, hub["reader"], 2)["use_cases"] == []
    refused = one(client, hub["other"], 2, "UC ORD-1")
    assert refused.status_code == 422 and "these are: none" in refused.json()["message"], refused.text
    assert whole(client, hub["other"], 2).json()["state"] == "approved"


def test_use_case_reviews_an_open_decide_finding_holds_an_approval_but_not_a_request_for_changes(client, hub, graph):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    held = one(client, hub["other"], 1, "UC ORD-1")
    assert held.status_code == 409 and f"(run #{run_id}) is running" in held.json()["message"], held.text
    decide = recorded(client, agent, item="UC ORD-2")
    moved(client, studio, run_id, "verifying", "done", summary="One finding.")
    gated = one(client, hub["other"], 1, "UC ORD-1")
    assert gated.status_code == 409 and f"#{decide} (contradiction, UC ORD-2)" in gated.json()["message"], gated.text
    assert states(client, hub["chi"], 1)["UC ORD-1"] == "pending", "a held approval records nothing"
    asked = one(client, hub["other"], 1, "UC ORD-1", "request_changes", body="Settle the retries first")
    assert asked.status_code == 200 and asked.json()["state"] == "in_review", asked.text
    assert states(client, hub["chi"], 1)["UC ORD-1"] == "changes_requested"


def test_use_case_reviews_each_use_case_approved_emits_its_event_once_before_the_versions(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    kind = "srs.use_case.approved"
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 200
    (first,) = stored(hub_db, kind)
    assert (first["document"], first["version"], first["code"], first["approver"]) == (
        "demo-shop",
        1,
        "UC ORD-1",
        "someone-else",
    )
    assert first["parent"] and first["claim"] is None and first["approver_id"] and first["version_id"]
    assert one(client, hub["owner"], 1, "UC ORD-1", "request_changes", body="Again").status_code == 200
    assert states(client, hub["chi"], 1)["UC ORD-1"] == "changes_requested"
    assert one(client, hub["owner"], 1, "UC ORD-1").status_code == 200  # approved again: no second event
    assert one(client, hub["other"], 1, "UC ORD-2").status_code == 200
    assert one(client, hub["other"], 1, "UC RET-1").json()["state"] == "approved"
    assert [e["code"] for e in stored(hub_db, kind)] == CASES, "each once, in the order of the reviews"
    assert len(stored(hub_db, "srs.version.approved")) == 1


def test_use_case_reviews_a_version_wide_approval_emits_the_use_cases_it_approves(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    assert [e["code"] for e in stored(hub_db, "srs.use_case.approved")] == CASES
    assert len(stored(hub_db, "srs.version.approved")) == 1


def test_use_case_reviews_the_event_carries_the_claim_field_and_the_parent_heading(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    cart = item(client, hub["chi"], "UC ORD-1")
    table = "| Item | Content |\n|---|---|\n"
    assert table in cart["body"]
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": cart["body"].replace(table, table + "| Claim | G-3 |\n")})
    submitted(client, hub["chi"], 2)
    assert one(client, hub["other"], 2, "UC ORD-1").status_code == 200
    first, second = [e for e in stored(hub_db, "srs.use_case.approved") if e["code"] == "UC ORD-1"]
    assert (first["version"], first["claim"]) == (1, None)
    assert (second["version"], second["claim"], second["parent"]) == (2, "G-3", first["parent"])

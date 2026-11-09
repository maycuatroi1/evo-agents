"""The owners of groups of use case codes and the switch that lets an approval go without @ba, on the hub (the config's
``owners`` and ``wait_for_ba``; ``evo_agents.hub.server.srs.use_case_reviews.may_decide``, ``reviews``, ``notices``).
The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes version 1, whose use cases UC ORD-1, UC ORD-2 and
UC RET-1 are all to review; owner (admin) and someone-else are the other writers, reader only reads."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub.server.srs import notices
from evo_agents.hub.server.srs.store import owners_of_config
from evo_agents.hub.server.srs.use_case_reviews import code_group, may_decide
from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_ba_agent import (  # noqa: F401
    CHI,
    OTHER,
    OWNER,
    client,
    hub,
    imported,
    members,
    srs_core,
    submitted,
    version,
)
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_runs import moved
from tests.hub.test_srs_findings import billing, graph, recorded, reviewing  # noqa: F401
from tests.hub.test_srs_notices import kind_of, queue, told
from tests.hub.test_srs_plugin import configured
from tests.hub.test_srs_use_case_reviews import CASES, one, states


def test_srs_owners_a_code_group_is_the_letters_before_its_number_and_the_config_reads_upper_case():
    assert [code_group(code) for code in ("UC ORD-3", "uc ret-12", "UC ORD3", None, "")] == [
        "ORD",
        "RET",
        None,
        None,
        None,
    ]
    assert owners_of_config({"ord": ["alice", "alice", " "], "PAY": [], "RET": "bao", 4: ["chi"]}) == {
        "ORD": ("alice",),
        "4": ("chi",),
    }
    assert owners_of_config(["alice"]) == {}
    assert may_decide((), "anyone", False) and may_decide(("Alice",), "alice", False)
    assert may_decide(("alice",), "bao", True) and not may_decide(("alice",), "bao", False)


def test_srs_owners_review_their_group_with_the_admins_and_every_other_group_stays_open(client, hub):
    configured(client, hub["owner"], {"owners": {"ord": [CHI]}, "self_approval": True})
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    refused = one(client, hub["other"], 1, "UC ORD-1")
    assert refused.status_code == 403 and f"which {CHI} own" in refused.json()["message"], refused.text
    assert states(client, hub["chi"], 1) == dict.fromkeys(CASES, "pending"), "a refusal records nothing"
    listed = {c["code"]: (c["owners"], c["may_review"]) for c in queue(client, hub["other"])[0]["use_cases"]}
    assert listed == {"UC ORD-1": ([CHI], False), "UC ORD-2": ([CHI], False), "UC RET-1": ([], True)}
    mine = {c["code"]: c["may_review"] for c in queue(client, hub["chi"])[0]["use_cases"]}
    assert mine == dict.fromkeys(CASES, True), "the owner, author too, reviews with self_approval on"
    # someone-else approves the version as a whole: it counts for UC RET-1 alone, which no owner holds
    wide = whole(client, hub["other"], 1)
    assert wide.status_code == 200 and wide.json()["state"] == "in_review", wide.text
    assert states(client, hub["chi"], 1) == {"UC ORD-1": "pending", "UC ORD-2": "pending", "UC RET-1": "approved"}
    assert one(client, hub["chi"], 1, "UC ORD-1").status_code == 200  # the owner
    last = one(client, hub["owner"], 1, "UC ORD-2")  # an admin
    assert last.status_code == 200 and last.json()["state"] == "approved", last.text
    (case,) = [c for c in last.json()["use_cases"] if c["code"] == "UC ORD-2"]
    assert [(r["reviewer"], r["decision"]) for r in case["reviews"]] == [(OWNER, "approve")], "who approved it stays"


def test_srs_owners_a_member_who_owns_none_of_the_use_cases_does_not_review_the_version(client, hub):
    configured(client, hub["owner"], {"owners": {"ORD": [CHI], "RET": [CHI]}, "self_approval": True})
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    refused = whole(client, hub["other"], 1)
    assert refused.status_code == 403 and "owns none of the use cases" in refused.json()["message"], refused.text
    assert whole(client, hub["owner"], 1).json()["state"] == "approved", "an admin approves every group"


def test_srs_owners_are_told_their_own_use_cases_and_the_admins_when_no_owner_may(client, hub):
    configured(client, hub["owner"], {"owners": {"ORD": [OTHER]}, "reviewers": [OWNER]})
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    (notice,) = kind_of(client, hub["other"], notices.REVIEW)
    assert "UC ORD-1 r1" in notice["body"] and "UC ORD-2 r1" in notice["body"] and "UC RET-1" not in notice["body"]
    (admin,) = kind_of(client, hub["owner"], notices.REVIEW)
    assert "UC RET-1 r1" in admin["body"] and "UC ORD" not in admin["body"], "reviewers names owner for no group"
    assert admin["link"].endswith("/cases/" + version(client, hub["chi"], 1)["use_cases"][2]["key"])
    assert told(client, hub["chi"]) == []


def test_srs_owners_who_may_not_review_leave_their_use_cases_to_the_admins(client, hub):
    configured(client, hub["owner"], {"owners": {"ORD": [CHI], "RET": [CHI]}})  # chi wrote them, no self_approval
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert told(client, hub["chi"]) == told(client, hub["other"]) == []
    (admin,) = kind_of(client, hub["owner"], notices.REVIEW)
    assert all(f"{code} r1" in admin["body"] for code in CASES), admin["body"]


def test_srs_wait_for_ba_off_tells_the_reviewers_at_submit_and_approves_while_ba_reviews_or_a_finding_is_open(
    client, hub, graph
):
    configured(client, hub["owner"], {"wait_for_ba": False})
    billing(client, hub["chi"])  # the source of the finding
    studio, run_id, agent = reviewing(client, hub)
    (notice,) = kind_of(client, hub["other"], notices.REVIEW)
    assert f"@ba is still reviewing it (run #{run_id})" in notice["body"], notice["body"]
    assert [(v["held_by"], v["deciding"]) for v in queue(client, hub["other"])] == [(None, 0)]
    early = one(client, hub["other"], 1, "UC ORD-1")
    assert early.status_code == 200 and early.json()["state"] == "in_review", early.text
    recorded(client, agent, item="BR ORD2-1")  # a finding to decide about UC ORD-2
    moved(client, studio, run_id, "verifying", "done", summary="One finding.")
    assert [(v["held_by"], v["deciding"]) for v in queue(client, hub["other"])] == [(None, 1)]
    assert one(client, hub["other"], 1, "UC ORD-2").status_code == 200
    last = one(client, hub["other"], 1, "UC RET-1")
    assert last.status_code == 200 and last.json()["state"] == "approved", last.text
    assert version(client, hub["chi"], 1)["state"] == "approved"


def test_srs_wait_for_ba_on_by_default_still_holds_an_approval_while_ba_reviews(client, hub, graph):
    studio, run_id, _ = reviewing(client, hub)
    held = one(client, hub["other"], 1, "UC ORD-1")
    assert held.status_code == 409 and f"(run #{run_id}) is running" in held.json()["message"], held.text

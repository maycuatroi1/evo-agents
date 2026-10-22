"""What a version needs reviewed and which reviews carry to a later version (``evo_agents.core_plugins.srs.
use_case_reviews``), on ``tests/hub/fixtures/srs/demo-shop-srs.md``: the use cases a version adds, changes (itself or a
business rule under it) or drops since the version approved last, and the reviews that stay while a use case reads the
same."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.core_plugins.srs.doc import Document, Item
from evo_agents.core_plugins.srs.use_case_reviews import needed
from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_ba_agent import client, hub, imported, item, members, saved, srs_core, submitted, version  # noqa: F401
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_srs_use_case_reviews import CASES, one, states


def edit(client, headers, ref: str, **change) -> dict:
    return {"key": item(client, headers, ref)["key"], **change}


def new_rule(client, headers, under: str) -> dict:
    parent = item(client, headers, under)["key"]
    rule = {"kind": "business_rule", "code": "BR RET1-2", "title": "Refund", "parent": parent}
    return {"new": rule | {"body": "- A refund is paid within 5 days.", "steps": ["1"]}}


def new_case(client, headers) -> dict:
    section = item(client, headers, "UC ORD-1")["parent"]
    case = {"kind": "use_case", "code": "UC ORD-3", "title": "Cancel an order", "parent": section}
    return {"new": case | {"body": "| Item | Content |\n|---|---|\n| Actor | Customer |"}}


def approved_first(client, headers) -> None:
    imported(client, headers["chi"])
    submitted(client, headers["chi"], 1)
    assert whole(client, headers["other"], 1).json()["state"] == "approved"


def kinds(client, headers, number: int) -> dict:
    return {c["code"]: c["change"] for c in version(client, headers, number)["use_cases"]}


@pytest.mark.parametrize(
    ("what", "make", "expected"),
    [
        ("a business rule added", lambda c, h: new_rule(c, h, "UC RET-1"), {"UC RET-1": "changed"}),
        (
            "a business rule changed",
            lambda c, h: edit(c, h, "BR ORD1-2", body="- The hold lasts 20 minutes."),
            {"UC ORD-1": "changed"},
        ),
        ("a field of a rule changed", lambda c, h: edit(c, h, "BR ORD2-1", steps=["2"]), {"UC ORD-2": "changed"}),
        ("a business rule removed", lambda c, h: edit(c, h, "BR RET1-1", retire=True), {"UC RET-1": "changed"}),
        ("the use case changed", lambda c, h: edit(c, h, "UC ORD-2", title="Pay now"), {"UC ORD-2": "changed"}),
        ("a use case added", lambda c, h: new_case(c, h), {"UC ORD-3": "added"}),
        ("a use case removed", lambda c, h: edit(c, h, "UC RET-1", retire=True), {"UC RET-1": "removed"}),
    ],
)
def test_use_case_reviews_a_version_needs_the_use_cases_it_adds_changes_or_drops(client, hub, what, make, expected):
    approved_first(client, hub)
    saved(client, hub["owner"], 1, make(client, hub["owner"]))  # an admin drops coded items
    assert kinds(client, hub["reader"], 2) == expected, what


def test_use_case_reviews_a_removed_use_case_is_reviewed_by_its_code_and_ends_the_version(client, hub):
    approved_first(client, hub)
    saved(client, hub["owner"], 1, edit(client, hub["owner"], "UC RET-1", retire=True))
    submitted(client, hub["owner"], 2)
    done = one(client, hub["other"], 2, "UC RET-1")
    assert done.status_code == 200, done.text
    assert (done.json()["state"], done.json()["use_cases"][0]["change"]) == ("approved", "removed")


def test_use_case_reviews_a_use_case_that_only_moved_needs_no_review():
    def case(key: str, parent: int | None) -> Item:
        return Item("use_case", f"Case {key}", code=f"UC {key}", key=key, parent=parent)

    group = Item("section", "Orders", key="g1")
    old = Document(items=[group, case("a", 0), case("b", 0), Item("section", "Returns", key="g2")])
    moved = Document(items=[Item("section", "Returns", key="g2"), group, case("b", 1), case("a", 1)])
    assert needed(old, moved) == [], "a use case moved to another place reads the same"
    again = Document(items=[Item("section", "Returns", key="g2"), case("b", 0), case("a", 0), group])
    assert needed(old, again) == []


def test_use_case_reviews_a_review_carries_to_a_later_version_until_the_use_case_changes(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 200
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD2-1", steps=["2"]))
    carried = version(client, hub["reader"], 2)["use_cases"]
    assert {c["code"]: c["state"] for c in carried} == {
        "UC ORD-1": "approved",
        "UC ORD-2": "pending",
        "UC RET-1": "pending",
    }
    assert [r["version"] for r in carried[0]["reviews"]] == [1], "the review reads as recorded on version 1"
    saved(client, hub["chi"], 2, edit(client, hub["chi"], "UC ORD-1", title="Place an order now"))
    assert states(client, hub["reader"], 3) == dict.fromkeys(CASES, "pending"), "the use case changed: no review stays"
    assert version(client, hub["reader"], 3)["use_cases"][0]["reviews"] == []
    submitted(client, hub["chi"], 3)
    assert one(client, hub["other"], 3, "UC ORD-1").json()["use_cases"][0]["reviews"][0]["version"] == 3


def test_use_case_reviews_a_rule_changed_under_a_use_case_drops_its_carried_review(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    for ref in ("UC ORD-1", "UC ORD-2"):
        one(client, hub["other"], 1, ref)
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    assert states(client, hub["reader"], 2) == {"UC ORD-1": "pending", "UC ORD-2": "approved", "UC RET-1": "pending"}


def test_use_case_reviews_a_later_review_of_a_reviewer_replaces_their_earlier_one_and_the_history_stays(
    client, hub, hub_db
):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    one(client, hub["other"], 1, "UC ORD-2", "request_changes", body="Name the gateway")
    assert states(client, hub["reader"], 1)["UC ORD-2"] == "changes_requested"
    one(client, hub["other"], 1, "UC ORD-2")
    assert states(client, hub["reader"], 1)["UC ORD-2"] == "approved"
    asked = one(client, hub["other"], 1, "UC ORD-2", "request_changes", body="After all, no")
    (case,) = [c for c in asked.json()["use_cases"] if c["code"] == "UC ORD-2"]
    assert [(c["decision"], c["body"]) for c in case["reviews"]] == [("request_changes", "After all, no")]
    (count,) = sql(hub_db, select(func.count()).select_from(tables.srs.use_case_reviews))
    assert count[0] == 3, "every review stays in the table"
    wide = whole(client, hub["owner"], 1)
    assert wide.json()["state"] == "in_review", "the request for changes still blocks a version-wide approval"
    assert [r["decision"] for r in wide.json()["reviews"]] == ["approve"], "and the approval is recorded"

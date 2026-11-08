"""What the plugin srs tells members (``evo_agents.hub.server.srs.notices``) and the queue of versions in review
(GET /v1/x/srs/<project>/reviews). The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes version 1,
whose three use cases are all to review; owner (admin) and someone-else are the other writers, reader only reads."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import tables
from evo_agents.hub.server.srs import notices, store
from tests.hub.test_ba_agent import (  # noqa: F401
    CHI,
    DOC,
    OTHER,
    OWNER,
    PROJECT,
    SRS,
    client,
    delivered,
    hub,
    imported,
    item,
    members,
    saved,
    srs_core,
    submitted,
)
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_runs import moved
from tests.hub.test_srs_findings import billing, graph, recorded, reviewing  # noqa: F401
from tests.hub.test_srs_plugin import configured
from tests.hub.test_srs_use_case_reviews import CASES, one

WAITS = f"v1 of demo-shop in {PROJECT} waits for your review"
LINK = f"/p/{PROJECT}/x/srs/documents/demo-shop/versions/1"


def kind_of(client, headers, notice_kind: str) -> list[dict]:
    """The member's notices of ``notice_kind``, the newest first."""
    found = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=headers)
    assert found.status_code == 200, found.text
    return [row for row in found.json()["notifications"] if row["notice_kind"] == notice_kind]


def told(client, headers) -> list[tuple]:
    return [
        (row["title"], row["details"]["version"], row["details"]["round"], row["link"])
        for row in kind_of(client, headers, notices.REVIEW)
    ]


def queue(client, headers) -> list[dict]:
    found = client.get(f"{SRS}/reviews", headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def tell_again(client, number: int) -> list[int]:
    """``notices.tell_reviewers`` once more for version ``number`` of demo-shop, as a second delivery would call it."""

    async def call():
        d, p = tables.srs.documents, tables.projects
        async with client.app.state.engine.begin() as conn:
            query = select(d.c.id, p.c.id).join_from(d, p, p.c.id == d.c.project_id).where(d.c.slug == "demo-shop")
            document_id, project_id = (await conn.execute(query)).one()
            doc = await store.document_by_id(conn, project_id, document_id)
            return await notices.tell_reviewers(conn, project_id, doc, await store.version(conn, doc, number))

    return client.portal.call(call)


def test_srs_notices_without_ba_the_reviewers_learn_at_submit_once_per_round_and_never_the_author(client, hub):
    imported(client, hub["chi"])
    assert told(client, hub["other"]) == [], "a draft waits for nobody"
    submitted(client, hub["chi"], 1)
    assert told(client, hub["other"]) == told(client, hub["owner"]) == [(WAITS, 1, 1, LINK)]
    assert told(client, hub["chi"]) == told(client, hub["reader"]) == []
    (notice,) = kind_of(client, hub["other"], notices.REVIEW)
    assert "Use cases to review: UC ORD-1 (added), UC ORD-2 (added), UC RET-1 (added)." in notice["body"]
    assert "@ba did not review this round." in notice["body"]
    assert notice["details"]["use_cases"] == CASES and notice["details"]["event"] == notices.READY
    assert tell_again(client, 1) == [], "each member is told once per round"
    returned = whole(client, hub["other"], 1, "request_changes", body="Say why 40")
    assert returned.json()["state"] == "draft", returned.text
    (back,) = kind_of(client, hub["chi"], notices.REVIEWED)
    assert (back["title"], back["body"], back["link"]) == (
        f"{OTHER} returned v1 of demo-shop in {PROJECT} to draft",
        "Say why 40",
        LINK,
    )
    submitted(client, hub["chi"], 1)
    assert told(client, hub["other"]) == [(WAITS, 1, 2, LINK), (WAITS, 1, 1, LINK)], "a new round asks again"


def test_srs_notices_go_to_the_reviewers_the_config_names_or_every_writer_who_may_when_none_of_them_may(client, hub):
    configured(client, hub["owner"], {"reviewers": [OWNER.upper(), "nobody-here"]})
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert told(client, hub["owner"]) == [(WAITS, 1, 1, LINK)]
    assert told(client, hub["other"]) == []
    stock = item(client, hub["chi"], "BR ORD1-2")
    saved(client, hub["owner"], 1, {"key": stock["key"], "body": "- Placing the order holds its stock for 20 minutes."})
    submitted(client, hub["owner"], 2)  # owner wrote version 2, so owner may not review it
    assert [row[:3] for row in told(client, hub["other"])] == [
        (f"v2 of demo-shop in {PROJECT} waits for your review", 2, 1)
    ]
    assert [row[1] for row in told(client, hub["owner"])] == [1]


def test_srs_notices_wait_for_bas_review_then_for_the_last_finding_to_decide(client, hub, graph):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    assert told(client, hub["other"]) == [], "@ba's review of the round runs first"
    assert [(v["held_by"], v["deciding"]) for v in queue(client, hub["other"])] == [("ba_review", 0)]
    first = recorded(client, agent, item="UC ORD-2")
    second = recorded(client, agent, item="UC ORD-1", summary="Holds differ")
    moved(client, studio, run_id, "verifying", "done", summary="Two findings.")
    delivered(client)
    assert told(client, hub["other"]) == [], "two findings to decide hold it"
    assert [(v["held_by"], v["deciding"]) for v in queue(client, hub["other"])] == [("findings", 2)]
    for finding, reason in ((first, "Retries agreed"), (second, "Holds agreed")):
        waived = client.post(f"{DOC}/findings/{finding}/waive", json={"reason": reason}, headers=hub["owner"])
        assert waived.status_code == 200, waived.text
        if finding == first:
            assert told(client, hub["other"]) == [], "one finding to decide still holds it"
    assert told(client, hub["other"]) == told(client, hub["owner"]) == [(WAITS, 1, 1, LINK)]
    (notice,) = kind_of(client, hub["other"], notices.REVIEW)
    assert (
        f"@ba reviewed it (run #{run_id})." in notice["body"]
        and "Every finding to decide is settled." in notice["body"]
    )
    assert notice["details"]["review_run"] == run_id
    assert [(v["held_by"], v["deciding"]) for v in queue(client, hub["other"])] == [(None, 0)]


def test_srs_notices_reach_the_reviewers_when_bas_review_ends_without_a_finding_to_decide(client, hub):
    studio, run_id, _ = reviewing(client, hub)
    moved(client, studio, run_id, "verifying", "done", summary="Nothing found.")
    assert told(client, hub["other"]) == [], "the run's end is told by the event the hub delivers"
    delivered(client)
    assert told(client, hub["other"]) == [(WAITS, 1, 1, LINK)]
    assert tell_again(client, 1) == []


def test_srs_notices_tell_the_author_of_changes_asked_on_a_use_case_and_of_the_approval(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    asked = one(client, hub["other"], 1, "UC ORD-1", "request_changes", body="Name the hold")
    assert asked.status_code == 200, asked.text
    (back,) = kind_of(client, hub["chi"], notices.REVIEWED)
    assert back["title"] == f"{OTHER} asked for changes on UC ORD-1 of v1 of demo-shop in {PROJECT}"
    assert (back["body"], back["details"]["use_case"], back["details"]["event"]) == (
        "Name the hold",
        "UC ORD-1",
        "changes_requested",
    )
    for ref in CASES:
        assert one(client, hub["other"], 1, ref).status_code == 200
    latest = kind_of(client, hub["chi"], notices.REVIEWED)[0]
    assert (latest["title"], latest["details"]["event"]) == (f"v1 of demo-shop in {PROJECT} is approved", "approved")
    assert kind_of(client, hub["other"], notices.REVIEWED) == [], "the reviewer is not told of their own review"


def test_srs_review_queue_lists_versions_in_review_with_their_use_cases_and_who_may_review(client, hub):
    imported(client, hub["chi"])
    assert queue(client, hub["other"]) == [], "a draft is not in the queue"
    submitted(client, hub["chi"], 1)
    (entry,) = queue(client, hub["other"])
    title = client.get(f"{SRS}/documents", headers=hub["reader"]).json()[0]["title"]
    shown = (entry["document"], entry["document_title"], entry["number"], entry["state"], entry["author"])
    assert shown == ("demo-shop", title, 1, "in_review", CHI)
    assert (entry["held_by"], entry["deciding"], entry["review_run"]) == (None, 0, None)
    assert [(case["code"], case["change"], case["state"]) for case in entry["use_cases"]] == [
        (code, "added", "pending") for code in CASES
    ]
    may = {who: queue(client, hub[who])[0]["may_review"] for who in ("other", "owner", "chi", "reader")}
    assert may == {"other": True, "owner": True, "chi": False, "reader": False}
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 200
    (entry,) = queue(client, hub["chi"])
    assert [case["state"] for case in entry["use_cases"]] == ["approved", "pending", "pending"]
    assert [review["reviewer"] for review in entry["use_cases"][0]["reviews"]] == [OTHER]

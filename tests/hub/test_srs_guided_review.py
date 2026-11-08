"""The review of a use case as a first-time reviewer meets it: the choices a finding of level decide offers, settling it
by picking one (POST .../findings/{id}/settle), what the queue says of each use case, and where a notice leads. The
sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes it, someone-else and owner review it."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from tests.hub.test_ba_agent import (  # noqa: F401
    DOC,
    OTHER,
    PROJECT,
    client,
    delivered,
    hub,
    imported,
    item,
    members,
    saved,
    srs_core,
    submitted,
    tool,
)
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_runs import moved
from tests.hub.test_srs_findings import billing, graph, recorded, reviewing  # noqa: F401
from tests.hub.test_srs_notices import kind_of, queue
from tests.hub.test_srs_use_case_reviews import one

CHOICES = ["Keep 3 retries in 15 minutes", "Take the 5 retries of billing"]


def settle(client, headers, finding: int, **body):
    return client.post(f"{DOC}/findings/{finding}/settle", json=body, headers=headers)


def open_findings(client, headers) -> list[dict]:
    found = client.get(f"{DOC}/findings", params={"status": "open"}, headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def test_srs_guided_review_ba_offers_choices_and_a_reviewer_settles_by_picking_one(client, hub, graph):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    for wrong in (CHOICES[:1], [*CHOICES, "c", "d", "e"], [CHOICES[0], CHOICES[0]]):
        text, error = tool(
            client, agent, "srs__finding", action="record", document="demo-shop", kind="contradiction",
            level="decide", item="BR ORD2-1", summary="Retries differ", options=wrong,
            sources=[{"kind": "code", "ref": "BR BIL2-1", "note": "5 retries in 30 minutes"}],
        )  # fmt: skip
        assert error and ("options" in text or "choices" in text), text  # the schema, then the hub
    decide = recorded(client, agent, options=CHOICES)
    advice = recorded(client, agent, level="advisory", item="UC ORD-1", summary="Holds differ")
    moved(client, studio, run_id, "verifying", "done", summary="Two findings.")
    delivered(client)
    shown = {row["id"]: row["options"] for row in open_findings(client, hub["reader"])}
    assert shown == {decide: CHOICES, advice: []}
    (entry,) = queue(client, hub["other"])
    cases = {case["code"]: (case["deciding"], case["noted"], bool(case["intent"])) for case in entry["use_cases"]}
    assert cases == {"UC ORD-1": (0, 1, True), "UC ORD-2": (1, 0, True), "UC RET-1": (0, 0, True)}
    assert kind_of(client, hub["other"], "srs_review") == [], "a finding to decide holds it"
    assert settle(client, hub["reader"], decide, option=1).status_code == 403, "a reader settles nothing"
    assert settle(client, hub["other"], decide).status_code == 422, "a finding with choices takes one"
    assert settle(client, hub["other"], decide, option=2).status_code == 422, "it offers A and B only"
    done = settle(client, hub["other"], decide, option=1, note="Billing is the newer rule")
    assert done.status_code == 200, done.text
    row = done.json()
    assert (row["status"], row["settled_by"]) == ("resolved", OTHER)
    assert row["resolution"] == "B. Take the 5 retries of billing\n\nBilling is the newer rule"
    assert settle(client, hub["other"], decide, option=0).status_code == 409, "settled once"
    (notice,) = kind_of(client, hub["other"], "srs_review")
    assert notice["link"] == f"/p/{PROJECT}/x/srs/reviews", "three use cases to read: the list of what waits"
    assert "Every finding to decide is settled." in notice["body"]
    assert one(client, hub["other"], 1, "UC ORD-2").status_code == 200, "nothing holds approval any more"
    assert settle(client, hub["other"], advice).status_code == 422, "a finding with no choice is settled by a note"
    assert settle(client, hub["other"], advice, note="Fine as is").json()["resolution"] == "Fine as is"


def test_srs_guided_review_a_notice_of_one_use_case_leads_to_its_review(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    for code in ("UC ORD-1", "UC ORD-2", "UC RET-1"):
        assert one(client, hub["other"], 1, code).status_code == 200
    stock = item(client, hub["chi"], "BR ORD1-2")
    order = item(client, hub["chi"], "UC ORD-1")
    saved(client, hub["chi"], 1, {"key": stock["key"], "body": "- Placing the order holds its stock for 20 minutes."})
    submitted(client, hub["chi"], 2)
    case = f"/p/{PROJECT}/x/srs/documents/demo-shop/versions/2/cases/{order['key']}"
    latest = kind_of(client, hub["other"], "srs_review")[0]
    assert (latest["details"]["version"], latest["link"]) == (2, case)
    asked = one(client, hub["other"], 2, "UC ORD-1", "request_changes", body="Say why 20")
    assert asked.status_code == 200, asked.text
    back = kind_of(client, hub["chi"], "srs_reviewed")[0]
    assert (back["details"]["use_case"], back["link"]) == ("UC ORD-1", case)

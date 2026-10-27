"""The goal that aims at an SRS document, its ledger of what is delivered and its backlog, as criterion a2 of plan
srs-backlog-loop names it (``evo_agents.hub.srs_backlog``, ``evo_agents.hub.server.loop.backlog`` and ``.delivery``,
``evo-agents hub goal set --srs-document``).

A goal names a document of the project and may limit itself to a heading of it; its backlog is the document's approved
use cases (those of the approved version, and those a review approved in a version still in review) minus those the
ledger says delivered, delivering or unknown. It is in groups: by the claim field of the use case, else by its direct
parent heading, else one each; a group of more than 15 splits by heading. The metric ``srs.backlog.open`` is the size of
the backlog, computed by the hub from the goal: no key of the registry of signals, no signal pushed. A goal with no
SRS document is as before. The pure rules and the command line run without Postgres; the rest needs EVO_HUB_TEST_DSN.
The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from evo_agents.hub import cli_goals, srs_backlog
from evo_agents.hub.contract import commands
from evo_agents.hub.srs_backlog import DELIVERED, DELIVERING, UNDELIVERED, UNKNOWN, UseCase, backlog, groups
from tests.hub import pg

METRIC = {"key": srs_backlog.BACKLOG_METRIC, "aggregate": "last", "comparator": "<=", "target": 0}


def case(ref: str, claim: str | None = None, *headings: str) -> UseCase:
    return UseCase(ref, ref.title(), claim, headings[-1] if headings else None, headings)


# The rules


def test_the_backlog_leaves_out_what_the_ledger_delivered_delivers_or_does_not_know():
    approved = [case("UC A-1"), case("UC A-2"), case("UC A-3"), case("UC A-4"), case("UC A-1")]
    ledger = {"uc a-1": DELIVERED, "uc a-2": UNKNOWN, "uc a-3": UNDELIVERED}
    assert [c.ref for c in backlog(approved, ledger)] == ["UC A-3", "UC A-4"]
    assert [c.ref for c in backlog(approved, {srs_backlog.ref_key("UC  A-4"): DELIVERING})] == [
        "UC A-1",
        "UC A-2",
        "UC A-3",
    ], "by ref_key"
    assert backlog([], ledger) == []


def test_groups_go_by_claim_else_direct_heading_else_one_each_and_a_large_group_splits_by_heading():
    cases = [
        case("UC A-1", "G-3", "Orders"),
        case("UC A-2", None, "Orders"),
        case("UC A-3", " G-3 ", "Returns"),
        case("UC A-4", None, "Orders"),
        case("UC A-5"),
    ]
    found = groups(cases)
    assert [(g.key, [c.ref for c in g.use_cases]) for g in found] == [
        ("claim:g-3", ["UC A-1", "UC A-3"]),
        ("heading:orders", ["UC A-2", "UC A-4"]),
        ("use-case:uc a-5", ["UC A-5"]),
    ]
    assert [g.title for g in found] == ["G-3", "Orders", "UC A-5"]
    many = [case(f"UC B-{n}", "G-9", "Orders" if n % 2 else "Returns") for n in range(1, 18)]
    split = groups(many)
    assert [g.key for g in split] == ["claim:g-9/heading:orders", "claim:g-9/heading:returns"]
    assert sum(len(g.use_cases) for g in split) == 17
    assert len(groups(many[:15])) == 1, "15 stay one group"
    same = [case(f"UC C-{n}", None, "Orders") for n in range(1, 18)]
    assert [len(g.use_cases) for g in groups(same)] == [17], "a group that cannot split by heading stays whole"
    assert groups([]) == []


def test_under_keeps_the_use_cases_below_a_heading_by_code_or_title():
    cases = [case("UC A-1", None, "1. Intro", "2.1. Orders"), case("UC A-2", None, "2.2. Returns")]
    assert [c.ref for c in srs_backlog.under(cases, " 2.1. orders")] == ["UC A-1"]
    assert [c.ref for c in srs_backlog.under(cases, "1. INTRO")] == ["UC A-1"]
    assert srs_backlog.under(cases, None) == cases and srs_backlog.under(cases, "Nowhere") == []


# The command line


def test_goal_set_takes_the_srs_document_and_its_heading_and_clears_them_together():
    def args(**given) -> SimpleNamespace:
        names = [*cli_goals.GIVEN_FIELDS, *cli_goals.METRIC_OPTIONS, "if_revision", "note", "status"]
        base = {name: None for name in names} | {f"no_{name}": False for name in (*cli_goals.CLEARED_FIELDS, "metric")}
        return SimpleNamespace(**{**base, **given})

    body, why = cli_goals._body(
        args(
            statement="Ship the SRS",
            srs_document="demo-shop",
            srs_heading="2.1. Orders",
            metric="srs.backlog.open",
            aggregate="last",
            comparator="<=",
            target=0.0,
        ),
        {},
    )
    assert why is None and (body["srs_document"], body["srs_heading"]) == ("demo-shop", "2.1. Orders")
    held = {"statement": "Ship", "srs_document": "demo-shop", "srs_heading": "2.1. Orders", "status": "active"}
    kept, _ = cli_goals._body(args(rank=2), held)
    assert (kept["srs_document"], kept["srs_heading"]) == ("demo-shop", "2.1. Orders"), "left out: kept"
    cleared, _ = cli_goals._body(args(no_srs_document=True), held)
    assert "srs_document" not in cleared and "srs_heading" not in cleared
    assert cli_goals._conflict(args(srs_document="x", no_srs_document=True)) is not None
    printed = commands()["hub goal set"]["options"]
    flags = {flag for option in printed for flag in option["flags"]}
    assert {"--srs-document", "--no-srs-document", "--srs-heading", "--no-srs-heading"} <= flags


# On the hub

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.loop import backlog as hub_backlog
    from evo_agents.hub.server.loop import delivery, view
    from tests.hub.live import sql
    from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
        PROJECT,
        client,
        hub,
        imported,
        members,
        srs_core,
        submitted,
    )
    from tests.hub.test_ba_agent import review as whole
    from tests.hub.test_credentials_api import app_key  # noqa: F401
    from tests.hub.test_srs_use_case_reviews import one

    GOALS = f"/v1/projects/{PROJECT}/goals"

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
CASES = ["UC ORD-1", "UC ORD-2", "UC RET-1"]  # of the sample, in the order of the document


def run(client, fn, *args, **kwargs):
    """``fn(conn, *args, **kwargs)`` on the hub's engine, in a transaction."""

    async def go():
        async with client.app.state.engine.begin() as conn:
            return await fn(conn, *args, **kwargs)

    return client.portal.call(go)


def goal(client, hub, **fields):
    body = {"statement": "Deliver the order service", "srs_document": "demo-shop", "metric": METRIC} | fields
    return client.put(f"{GOALS}/G-1", json=body, headers=hub["owner"])


def ids(db) -> tuple[int, int, int]:
    """(project, document, goal) ids."""
    project = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    document = sql(db, select(tables.srs.documents.c.id).where(tables.srs.documents.c.slug == "demo-shop"))[0][0]
    return project, document, sql(db, select(tables.goals.c.id))[0][0]


def refs(client, goal_id: int) -> list[str]:
    return [c.ref for c in run(client, hub_backlog.backlog_of, goal_id)]


@needs_pg
def test_a_goal_names_a_document_of_the_project_and_its_metric_needs_no_signal_key(client, hub, hub_db):
    imported(client, hub["chi"])
    wrong = goal(client, hub, srs_document="no-such-doc")
    assert wrong.status_code == 422 and f"project {PROJECT} has no SRS document no-such-doc" in wrong.text
    wrong = goal(client, hub, srs_document=None, srs_heading="2.1. Orders")
    assert wrong.status_code == 422 and "srs_heading: a heading belongs to the SRS document" in wrong.text
    wrong = goal(client, hub, srs_document=None)
    assert (
        wrong.status_code == 422 and "metric.key: srs.backlog.open counts the backlog of an SRS document" in wrong.text
    )
    wrong = goal(client, hub, metric=METRIC | {"key": "search.zero_rate"})
    assert wrong.status_code == 422 and "the registry of project" in wrong.text, "other keys are as before"
    made = goal(client, hub, srs_heading="2.1. Orders")
    assert made.status_code == 200, made.text
    shown = made.json()
    assert (shown["revision"], shown["srs_document"], shown["srs_heading"], shown["metric"]["key"]) == (
        1,
        "demo-shop",
        "2.1. Orders",
        "srs.backlog.open",
    )
    again = goal(client, hub, srs_heading="2.1. Orders")
    assert again.json()["changed"] is False
    listed = client.get(GOALS, headers=hub["chi"]).json()
    assert [(g["code"], g["srs_document"]) for g in listed] == [("G-1", "demo-shop")]
    moved = goal(client, hub, srs_heading=None)
    assert (moved.json()["revision"], moved.json()["srs_heading"]) == (2, None)
    revisions = client.get(f"{GOALS}/G-1/revisions", headers=hub["chi"]).json()
    assert revisions[0]["changed"] == ["srs_heading"]


@needs_pg
def test_the_backlog_is_the_approved_use_cases_not_delivered_and_the_metric_counts_it(client, hub, hub_db):
    imported(client, hub["chi"])
    assert goal(client, hub).status_code == 200
    project, document, goal_id = ids(hub_db)
    metric = METRIC
    now = datetime.now(UTC)

    async def measure(conn):
        return await view.measure(conn, project, metric, now, goal_id=goal_id)

    assert refs(client, goal_id) == [] and run(client, measure) == 0.0, "nothing approved: an empty backlog"
    submitted(client, hub["chi"], 1)
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 200
    assert refs(client, goal_id) == ["UC ORD-1"], "a use case approved in a version in review is in the backlog"
    assert run(client, measure) == 1.0
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    assert refs(client, goal_id) == CASES
    states = {"UC ORD-1": (DELIVERED, "kg"), "UC ORD-2": (UNKNOWN, "kg"), "UC RET-1": (DELIVERING, "deliver")}
    for code, (state, source) in states.items():
        changed = run(client, delivery.record, project, document, code, state, source, {"plan": "p"}, goal_id=goal_id)
        assert changed is True
    assert refs(client, goal_id) == [] and run(client, measure) == 0.0
    again = run(client, delivery.record, project, document, "uc  ord-2", UNKNOWN, "kg", {"plan": "p"}, goal_id=goal_id)
    assert again is False, "the same row again writes nothing"
    assert run(client, delivery.record, project, document, "UC ORD-2", UNDELIVERED, "admin", {"by": "owner"}) is True
    assert refs(client, goal_id) == ["UC ORD-2"] and run(client, measure) == 1.0
    ledger = run(client, delivery.read, document)
    assert {key: (row.state, row.source) for key, row in ledger.items()} == {
        "uc ord-1": (DELIVERED, "kg"),
        "uc ord-2": (UNDELIVERED, "admin"),
        "uc ret-1": (DELIVERING, "deliver"),
    }
    assert ledger["uc ord-1"].evidence == {"plan": "p"} and ledger["uc ord-1"].goal_id == goal_id
    with pytest.raises(ValueError, match="state"):
        run(client, delivery.record, project, document, "UC ORD-2", "done", "admin")


@needs_pg
def test_the_backlog_of_a_goal_limited_to_a_heading_and_in_groups(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    assert goal(client, hub).status_code == 200
    _, _, goal_id = ids(hub_db)
    found = run(client, hub_backlog.groups_of, goal_id)
    assert sorted(c.ref for g in found for c in g.use_cases) == sorted(CASES)
    assert all(g.key.startswith(("heading:", "use-case:")) for g in found), "no claim field in the sample"
    heading = run(client, hub_backlog.backlog_of, goal_id)[0].heading
    assert heading
    assert goal(client, hub, srs_heading=heading).status_code == 200
    narrowed = refs(client, goal_id)
    assert narrowed and set(narrowed) <= set(CASES)
    assert goal(client, hub, srs_heading="No such heading").status_code == 200
    assert refs(client, goal_id) == []


@needs_pg
def test_a_goal_with_no_srs_document_has_no_backlog_and_is_measured_as_before(client, hub, hub_db):
    imported(client, hub["chi"])
    plain = client.put(f"{GOALS}/G-1", json={"statement": "Corner shops reorder"}, headers=hub["owner"])
    assert plain.status_code == 200 and plain.json()["srs_document"] is None
    _, _, goal_id = ids(hub_db)
    assert run(client, hub_backlog.backlog_of, goal_id) is None
    assert run(client, hub_backlog.groups_of, goal_id) == []
    assert run(client, hub_backlog.open_of, goal_id) is None

    async def measure(conn):
        return await view.measure(conn, ids(hub_db)[0], METRIC, datetime.now(UTC)), await view.measure(
            conn, ids(hub_db)[0], METRIC, datetime.now(UTC), goal_id=goal_id
        )

    assert run(client, measure) == (None, None), "the hub's metric has no value for a goal that aims at no document"
    figures = client.get(f"/v1/projects/{PROJECT}/loop/goals", headers=hub["chi"])
    assert figures.status_code in (200, 403, 404)

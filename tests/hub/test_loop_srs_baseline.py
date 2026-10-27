"""The baseline of a goal that aims at an SRS document: the hub proposes delivered, undelivered or unknown for each approved
use case with the evidence of the knowledge graph, and an admin fixes it by code, by heading and for the unknown ones, and
confirms it once; before that the goal is unconfirmed and a use case left unknown never enters its backlog (plan
srs-backlog-loop, criterion a5). The rules and the command line run without Postgres; the rest needs EVO_HUB_TEST_DSN.
The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from evo_agents.hub import cli_loop, kg_graph
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import commands
from evo_agents.hub.srs_backlog import DELIVERED, UNDELIVERED, UNKNOWN, UseCase
from evo_agents.hub.srs_baseline import CODE_COMMENT, COMMIT, PLAN_COMPLETED, STEP_DONE, Evidence, fixes, propose
from tests.hub import pg

PLAN = Evidence(PLAN_COMPLETED, "plan:coding-activity", "Coding activity")


def case(ref: str, *headings: str) -> UseCase:
    return UseCase(ref, ref.title(), None, headings[-1] if headings else None, headings)


# The rules


def test_a_use_case_with_evidence_is_delivered_a_new_one_without_is_undelivered_any_other_is_unknown():
    assert propose([PLAN], newer=False).state == DELIVERED
    assert propose([PLAN], newer=True).state == DELIVERED
    assert propose([], newer=True).state == UNDELIVERED
    assert propose([], newer=False).state == UNKNOWN
    shown = propose([Evidence(COMMIT, f"c{n}") for n in range(9)], newer=False)
    assert len(shown.evidence) == 5 and "commit_merged" in shown.reason


def test_fixes_go_unknown_first_then_headings_then_codes():
    cases = [case("UC A-1", "Orders"), case("UC A-2", "Orders"), case("UC A-3", "Returns"), case("UC A-4")]
    now = {"uc a-1": UNKNOWN, "uc a-2": DELIVERED, "uc a-3": UNKNOWN, "uc a-4": UNKNOWN}
    assert fixes(now, cases, unknown=UNDELIVERED) == {
        "uc a-1": UNDELIVERED,
        "uc a-3": UNDELIVERED,
        "uc a-4": UNDELIVERED,
    }
    assert fixes(now, cases, headings={"orders": DELIVERED}) == {"uc a-1": DELIVERED, "uc a-2": DELIVERED}
    narrowed = fixes(now, cases, unknown=DELIVERED, headings={"Returns": UNDELIVERED}, codes={"uc  a-3": DELIVERED})
    assert narrowed == {"uc a-1": DELIVERED, "uc a-3": DELIVERED, "uc a-4": DELIVERED}
    with pytest.raises(KeyError):
        fixes(now, cases, codes={"UC Z-9": DELIVERED})


def test_the_graph_gives_evidence_for_a_completed_plan_a_done_step_a_commit_a_merged_pull_request_and_code_only():
    evidence = kg_graph._evidence
    node = {"id": "x", "name": "n"}
    assert evidence({**node, "kind": "Plan", "props": {"steps": 2, "done": 2}}).kind == PLAN_COMPLETED
    assert evidence({**node, "kind": "Plan", "props": {"steps": 2, "done": 1}}) is None
    assert evidence({**node, "kind": "Plan", "props": {"steps": 0, "done": 0}}) is None
    assert evidence({**node, "kind": "PlanStep", "props": {"status": "done"}}).kind == STEP_DONE
    assert evidence({**node, "kind": "PlanStep", "props": {"status": "pending"}}) is None
    assert evidence({**node, "kind": "Commit", "props": {}}).kind == COMMIT
    assert evidence({**node, "kind": "PullRequest", "props": {"status": "merged"}}).kind == "pr_merged"
    assert evidence({**node, "kind": "PullRequest", "props": {"status": "open"}}) is None
    assert evidence({**node, "kind": "File", "props": {}}).kind == CODE_COMMENT
    assert evidence({**node, "kind": "Document", "props": {}}) is None


# The command line


def test_baseline_options_are_name_state_pairs_and_the_command_is_in_the_contract():
    assert cli_loop._fixes(["UC A-1=delivered", "UC B-2=undelivered"], "--set") == {
        "UC A-1": "delivered",
        "UC B-2": "undelivered",
    }
    for bad in ("UC A-1", "UC A-1=unknown", "=delivered"):
        with pytest.raises(HubError):
            cli_loop._fixes([bad], "--set")
    printed = commands()["hub loop baseline"]["options"]
    flags = {flag for option in printed for flag in option["flags"]}
    assert {"--goal", "--confirm", "--set", "--heading", "--unknown", "--project"} <= flags


def test_the_preview_prints_each_state_with_its_evidence_and_says_what_confirming_needs():
    found = {
        "goal": "G-1",
        "document": "demo-shop",
        "heading": None,
        "confirmed": False,
        "confirmed_at": None,
        "confirmed_by": None,
        "graph": True,
        "why": None,
        "counts": {"delivered": 1, "unknown": 1},
        "items": [
            {
                "code": "UC A-1",
                "title": "Order",
                "state": "delivered",
                "source": "proposal",
                "reason": "the graph holds 1: commit_merged",
                "evidence": [{"kind": "commit_merged", "ref": "commit:abc", "title": "repo@abc"}],
            },
            {"code": "UC A-2", "title": "Ship", "state": "unknown", "source": "proposal", "reason": "", "evidence": []},
        ],
    }
    text = "\n".join(cli_loop._baseline_lines(found))
    assert "delivered (1):" in text and "unknown (1):" in text and "commit_merged: repo@abc" in text
    assert "Not confirmed" in text and "--confirm" in text


# On the hub

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.server.loop import backlog as hub_backlog
    from evo_agents.hub.server.loop import baseline, delivery
    from evo_agents.hub.server.loop import routes as loop_routes
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
    BASELINE = f"{GOALS}/G-1/baseline"
    BOARD = f"/v1/projects/{PROJECT}/decision-board"

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
METRIC = {"key": "srs.backlog.open", "aggregate": "last", "comparator": "<=", "target": 0}
COMMIT_OF_ORD_2 = Evidence(COMMIT, "commit:r@abc", "r@abc")


@pytest.fixture
def graph(monkeypatch):
    """The knowledge graph stood in for: what it shows of each use case, or None when it cannot be read."""
    fake = SimpleNamespace(evidence={"UC ORD-2": [COMMIT_OF_ORD_2]}, why=None, asked=[])

    async def answer(state, user, project, codes):
        fake.asked.append(list(codes))
        if fake.evidence is None:
            return None, fake.why or "no graph yet"
        return {code: fake.evidence.get(code, []) for code in codes}, None

    monkeypatch.setattr(loop_routes, "delivery_evidence", answer)
    return fake


def baseline_set(client, hub):
    """A document with one use case approved before the goal (ORD-1), and two after it (ORD-2 and RET-1)."""
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert one(client, hub["other"], 1, "UC ORD-1").status_code == 200
    body = {"statement": "Deliver the order service", "srs_document": "demo-shop", "metric": METRIC}
    assert client.put(f"{GOALS}/G-1", json=body, headers=hub["owner"]).status_code == 200
    assert whole(client, hub["other"], 1).json()["state"] == "approved"


def states(shown: dict) -> dict[str, tuple[str, str]]:
    return {item["code"]: (item["state"], item["source"]) for item in shown["items"]}


def ids(db) -> tuple[int, int, int]:
    project = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    document = sql(db, select(tables.srs.documents.c.id).where(tables.srs.documents.c.slug == "demo-shop"))[0][0]
    return project, document, sql(db, select(tables.goals.c.id))[0][0]


def run(client, fn, *args, **kwargs):
    async def go():
        async with client.app.state.engine.begin() as conn:
            return await fn(conn, *args, **kwargs)

    return client.portal.call(go)


@needs_pg
def test_the_baseline_proposes_a_state_with_evidence_for_each_approved_use_case(client, hub, graph, hub_db):
    baseline_set(client, hub)
    shown = client.get(BASELINE, headers=hub["chi"])
    assert shown.status_code == 200, shown.text
    found = shown.json()
    assert (found["goal"], found["document"], found["confirmed"], found["graph"]) == ("G-1", "demo-shop", False, True)
    assert states(found) == {
        "UC ORD-1": (UNKNOWN, "proposal"),  # approved before the goal, nothing in the graph: a person says
        "UC ORD-2": (DELIVERED, "proposal"),
        "UC RET-1": (UNDELIVERED, "proposal"),  # approved after the goal, nothing in the graph
    }
    ord_2 = next(item for item in found["items"] if item["code"] == "UC ORD-2")
    assert ord_2["evidence"] == [{"kind": "commit_merged", "ref": "commit:r@abc", "title": "r@abc"}]
    assert found["counts"] == {"unknown": 1, "delivered": 1, "undelivered": 1}
    assert sorted(graph.asked[-1]) == ["UC ORD-1", "UC ORD-2", "UC RET-1"]
    _, document, _ = ids(hub_db)
    assert run(client, delivery.read, document) == {}, "a preview writes nothing"


@needs_pg
def test_before_confirmation_the_goal_waits_on_the_board_and_an_unknown_use_case_is_in_no_backlog(
    client, hub, graph, hub_db
):
    baseline_set(client, hub)
    _, document, goal_id = ids(hub_db)
    assert run(client, baseline.confirmed, goal_id) is False
    items = [i for i in client.get(BOARD, headers=hub["owner"]).json()["items"] if i["kind"] == "srs_baseline"]
    assert len(items) == 1 and items[0]["group"] == "policy" and items[0]["decided_by"] == "admin"
    assert items[0]["refs"] == {"goal": "G-1", "document": "demo-shop"} and "G-1" in items[0]["title"]
    assert items[0]["deciders"], "an admin decides it"
    # unknown is out of the backlog whether or not it is confirmed
    run(client, delivery.record, *ids(hub_db)[:2], "UC ORD-1", UNKNOWN, "kg")
    assert "UC ORD-1" not in [c.ref for c in run(client, hub_backlog.backlog_of, goal_id)]


@needs_pg
def test_an_admin_fixes_by_code_by_heading_and_the_unknown_group_and_a_writer_cannot(client, hub, graph, hub_db):
    baseline_set(client, hub)
    refused = client.post(f"{BASELINE}/fix", json={"unknown": "delivered"}, headers=hub["chi"])
    assert refused.status_code == 403
    by_code = client.post(f"{BASELINE}/fix", json={"codes": {"uc  ret-1": "delivered"}}, headers=hub["owner"])
    assert by_code.status_code == 200, by_code.text
    assert states(by_code.json())["UC RET-1"] == (DELIVERED, "admin")
    assert states(by_code.json())["UC ORD-1"] == (UNKNOWN, "proposal")
    wrong = client.post(f"{BASELINE}/fix", json={"codes": {"UC NOPE-1": "delivered"}}, headers=hub["owner"])
    assert wrong.status_code == 422 and "UC NOPE-1" in wrong.text
    invalid = client.post(f"{BASELINE}/fix", json={"unknown": "unknown"}, headers=hub["owner"])
    assert invalid.status_code == 422
    group = client.post(f"{BASELINE}/fix", json={"unknown": "undelivered"}, headers=hub["owner"])
    assert states(group.json())["UC ORD-1"] == (UNDELIVERED, "admin")
    assert states(group.json())["UC RET-1"] == (DELIVERED, "admin"), "a fix of a code stays"
    heading = next(i for i in group.json()["items"] if i["code"] == "UC ORD-2")["heading"]
    assert heading
    by_heading = client.post(f"{BASELINE}/fix", json={"headings": {heading: "undelivered"}}, headers=hub["owner"])
    assert by_heading.status_code == 200
    moved = [i["code"] for i in by_heading.json()["items"] if i["heading"] == heading]
    assert moved and all(states(by_heading.json())[code] == (UNDELIVERED, "admin") for code in moved)
    _, document, _ = ids(hub_db)
    ledger = run(client, delivery.read, document)
    assert ledger["uc ret-1"].evidence["by"] and ledger["uc ret-1"].evidence["was"] == UNDELIVERED


@needs_pg
def test_confirming_writes_the_ledger_once_and_the_loop_reads_the_backlog_from_it(client, hub, graph, hub_db):
    baseline_set(client, hub)
    _, document, goal_id = ids(hub_db)
    assert client.post(f"{BASELINE}/confirm", headers=hub["chi"]).status_code == 403
    assert client.post(f"{BASELINE}/confirm", headers=hub["reader"]).status_code == 403
    assert (
        client.post(f"{BASELINE}/fix", json={"codes": {"UC RET-1": "undelivered"}}, headers=hub["owner"]).status_code
        == 200
    )
    done = client.post(f"{BASELINE}/confirm", headers=hub["owner"])
    assert done.status_code == 200, done.text
    found = done.json()
    assert found["confirmed"] is True and found["confirmed_by"] and found["confirmed_at"]
    assert states(found) == {
        "UC ORD-1": (UNKNOWN, "kg"),
        "UC ORD-2": (DELIVERED, "kg"),
        "UC RET-1": (UNDELIVERED, "admin"),
    }
    ledger = run(client, delivery.read, document)
    assert ledger["uc ord-2"].evidence["evidence"][0]["ref"] == "commit:r@abc"
    assert ledger["uc ord-2"].goal_id == goal_id
    assert run(client, baseline.confirmed, goal_id) is True
    assert [c.ref for c in run(client, hub_backlog.backlog_of, goal_id)] == ["UC RET-1"], "unknown stays out"
    again = client.post(f"{BASELINE}/confirm", headers=hub["owner"])
    assert again.status_code == 409 and "confirmed already" in again.text
    items = [i for i in client.get(BOARD, headers=hub["owner"]).json()["items"] if i["kind"] == "srs_baseline"]
    assert items == []
    # the admin may still move a use case after, and the Loop's own rows stay
    run(client, delivery.record, *ids(hub_db)[:2], "UC RET-1", "delivering", "deliver", {}, goal_id=goal_id)
    fixed = client.post(f"{BASELINE}/fix", json={"unknown": "delivered"}, headers=hub["owner"])
    assert states(fixed.json())["UC ORD-1"] == (DELIVERED, "admin")
    assert states(fixed.json())["UC RET-1"][0] == "delivering"
    skipped = client.post(f"{BASELINE}/fix", json={"codes": {"UC RET-1": "delivered"}}, headers=hub["owner"])
    assert states(skipped.json())["UC RET-1"] == ("delivering", "deliver")


@needs_pg
def test_without_a_readable_graph_everything_is_unknown_and_confirming_is_refused(client, hub, graph, hub_db):
    baseline_set(client, hub)
    graph.evidence, graph.why = None, "project demo has no knowledge graph on the hub yet"
    found = client.get(BASELINE, headers=hub["chi"]).json()
    assert found["graph"] is False and "no knowledge graph" in found["why"]
    assert {state for state, _ in states(found).values()} == {UNKNOWN}, "never undelivered without the graph"
    refused = client.post(f"{BASELINE}/confirm", headers=hub["owner"])
    assert refused.status_code == 409 and "could not be read" in refused.text
    fixed = client.post(f"{BASELINE}/fix", json={"unknown": "delivered"}, headers=hub["owner"])
    assert fixed.status_code == 200
    assert client.post(f"{BASELINE}/confirm", headers=hub["owner"]).status_code == 200, "every use case has a row"


@needs_pg
def test_a_goal_with_no_srs_document_has_no_baseline_and_a_goal_re_aimed_is_unconfirmed(client, hub, graph, hub_db):
    imported(client, hub["chi"])
    plain = client.put(f"{GOALS}/G-1", json={"statement": "Corner shops reorder"}, headers=hub["owner"])
    assert plain.status_code == 200
    assert client.get(BASELINE, headers=hub["chi"]).status_code == 409
    assert client.get(f"{GOALS}/G-9/baseline", headers=hub["chi"]).status_code == 404
    _, _, goal_id = ids(hub_db)
    assert run(client, baseline.confirmed, goal_id) is False
    board = client.get(BOARD, headers=hub["owner"]).json()
    assert [i for i in board["items"] if i["kind"] == "srs_baseline"] == []

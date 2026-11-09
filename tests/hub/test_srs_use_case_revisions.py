"""The revisions of each use case and its approved baseline (``evo_agents.hub.server.srs.revisions``, migration 0226):
a use case's own revision number (r1 and up), the link of its review on every answer, the author rule and the findings
gate narrowed to one use case, a use case sent to review on its own and released into the baseline the knowledge graph
reads. The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes version 1, someone-else and owner review."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, select

from evo_agents.hub import tables
from evo_agents.hub.server.srs import kg as srs_kg
from evo_agents.hub.server.srs import notices
from evo_agents.hub.server.srs.doc import Document, Item
from tests.hub.live import engine as live_engine
from tests.hub.live import sql
from tests.hub.test_ba_agent import (  # noqa: F401 (fixtures)
    CHI,
    DOC,
    OTHER,
    PROJECT,
    client,
    delivered,
    enable,
    enable_ba,
    hub,
    imported,
    item,
    members,
    owners_worker,
    running,
    saved,
    srs_core,
    submitted,
    version,
)
from tests.hub.test_ba_agent import review as whole
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_runs import moved
from tests.hub.test_srs_events import stored  # noqa: F401
from tests.hub.test_srs_notices import kind_of
from tests.hub.test_srs_use_case_reviews import CASES, one, states

MIGRATION = Path(__file__).parents[2] / "evo_agents/hub/migrations/versions/0226_srs_use_case_revisions.py"


def revs(client, headers, number: int) -> dict:
    return {c["code"]: c["rev"] for c in version(client, headers, number)["use_cases"]}


def sent(client, headers, ref: str):
    return client.post(f"{DOC}/use-cases/{ref}/submit", headers=headers)


def edit(client, headers, ref: str, **change) -> dict:
    return {"key": item(client, headers, ref)["key"], **change}


def revision_rows(hub_db) -> list[tuple]:
    r, i, v = tables.srs.use_case_revisions, tables.srs.items, tables.srs.versions
    query = (
        select(i.c.code, r.c.rev, v.c.number, r.c.fingerprint.is_(None))
        .join_from(r, i, i.c.id == r.c.item_id)
        .join(v, v.c.id == r.c.version_id)
        .order_by(i.c.code, r.c.rev)
    )
    return sql(hub_db, query)


def graph_nodes(client, hub_db) -> dict:
    """The UseCase nodes the plugin's knowledge source declares, by code: (approval, rev)."""
    p = tables.projects
    ((project_id,),) = sql(hub_db, select(p.c.id).where(p.c.name == PROJECT))

    async def work():
        async with client.app.state.engine.begin() as conn:
            return await srs_kg.read(conn, "plugin.srs", project_id, PROJECT)

    found = client.portal.call(work)
    (doc,) = [entry for entry in found["items"] if entry["props"]["document"] == "demo-shop"]
    nodes = doc["props"]["graph"]["nodes"]
    return {
        node["props"]["code"]: (node["props"]["approval"], node["props"].get("rev"))
        for node in nodes
        if node["kind"] == "UseCase"
    }, doc


# (1) Revisions


def test_srs_revisions_go_up_only_for_the_use_case_a_version_changes_and_a_drop_is_the_last(client, hub, hub_db):
    imported(client, hub["chi"])
    assert revs(client, hub["reader"], 1) == dict.fromkeys(CASES, 1)
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    assert revs(client, hub["reader"], 2) == {"UC ORD-1": 2, "UC ORD-2": 1, "UC RET-1": 1}
    detail = item(client, hub["reader"], "UC ORD-1")
    assert (detail["rev"], detail["released"]) == (2, None)
    shown = client.get(f"{DOC}/versions/2", headers=hub["reader"]).json()
    by_code = {entry["code"]: entry["rev"] for entry in shown["items"] if entry["code"]}
    assert (by_code["UC ORD-1"], by_code["UC RET-1"], by_code["BR ORD1-2"]) == (2, 1, None)
    retired = client.post(f"{DOC}/retire", json={"codes": ["UC RET-1"]}, headers=hub["owner"])
    assert retired.status_code == 200, retired.text
    assert revision_rows(hub_db) == [
        ("UC ORD-1", 1, 1, False),
        ("UC ORD-1", 2, 2, False),
        ("UC ORD-2", 1, 1, False),
        ("UC RET-1", 1, 1, False),
        ("UC RET-1", 2, 3, True),
    ]


def test_srs_revisions_backfill_of_the_migration_matches_what_saving_writes(client, hub, hub_db):
    imported(client, hub["chi"])
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    saved(client, hub["chi"], 2, edit(client, hub["chi"], "UC RET-1", title="Return a book in 30 days"))
    submitted(client, hub["chi"], 3)
    assert whole(client, hub["other"], 3).json()["state"] == "approved"
    r, e = tables.srs.use_case_revisions, tables.srs.use_case_releases
    columns = (r.c.item_id, r.c.rev, r.c.version_id, r.c.fingerprint, r.c.author_id)
    written = sql(hub_db, select(*columns).order_by(r.c.item_id, r.c.rev))
    released = sql(hub_db, select(e.c.item_id, e.c.rev, e.c.version_id).order_by(e.c.item_id))
    assert len(written) == 5 and len(released) == 3
    spec = importlib.util.spec_from_file_location("migration_0226", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with live_engine(hub_db).begin() as conn:
        conn.execute(delete(e))
        conn.execute(delete(r))
        assert migration.backfill_revisions(conn) == 5
        assert migration.backfill_releases(conn) == 3
    assert sql(hub_db, select(*columns).order_by(r.c.item_id, r.c.rev)) == written
    assert sql(hub_db, select(e.c.item_id, e.c.rev, e.c.version_id).order_by(e.c.item_id)) == released


# (2) Links, (3) the author rule and the gate of one use case


def test_srs_every_use_case_answered_links_its_review_and_the_author_rule_is_the_use_cases(client, hub):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    for code in ("UC ORD-2", "UC RET-1"):
        approved = one(client, hub["other"], 1, code)
        assert approved.status_code == 200, approved.text
    (case,) = [c for c in approved.json()["use_cases"] if c["code"] == "UC RET-1"]
    assert case["link"] == f"/p/{PROJECT}/x/srs/documents/demo-shop/versions/1/cases/{case['key']}"
    assert (case["rev"], case["released"], case["state"]) == (1, 1, "approved")
    # owner writes UC ORD-1 r2 in version 2: owner may not review UC ORD-1, yet may review the others
    saved(client, hub["owner"], 1, edit(client, hub["owner"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    queued = submitted(client, hub["owner"], 2)
    assert [(c["code"], c["rev"], c["state"]) for c in queued["use_cases"]] == [
        ("UC ORD-1", 2, "pending"),
        ("UC ORD-2", 1, "approved"),
        ("UC RET-1", 1, "approved"),
    ]
    assert all(c["link"].endswith(f"/versions/2/cases/{c['key']}") for c in queued["use_cases"])
    (ord1,) = [c for c in queued["use_cases"] if c["code"] == "UC ORD-1"]
    assert set(ord1["authors"]) == {CHI, "owner"}
    barred = one(client, hub["owner"], 2, "UC ORD-1")
    assert barred.status_code == 403 and "wrote a revision of UC ORD-1" in barred.json()["message"], barred.text
    assert one(client, hub["chi"], 2, "UC ORD-1").status_code == 403, "chi wrote r1, never approved"
    queue = {
        entry["number"]: entry for entry in client.get(f"/v1/x/srs/{PROJECT}/reviews", headers=hub["owner"]).json()
    }
    assert {c["code"]: c["may_review"] for c in queue[2]["use_cases"]} == dict.fromkeys(CASES, False)
    assert queue[2]["may_review"] is False
    theirs = client.get(f"/v1/x/srs/{PROJECT}/reviews", headers=hub["other"]).json()
    assert {c["code"]: c["may_review"] for c in theirs[-1]["use_cases"]}["UC ORD-1"] is True
    done = one(client, hub["other"], 2, "UC ORD-1")
    assert done.status_code == 200 and done.json()["state"] == "approved", "every use case of version 2 approved"
    assert client.get(f"/v1/x/srs/{PROJECT}/reviews", headers=hub["owner"]).json() == [], "version 1 superseded"


# (4) A use case sent to review on its own, and the approved baseline


def test_srs_a_use_case_sent_alone_is_reviewed_released_and_read_by_the_graph_without_its_version(client, hub, hub_db):
    imported(client, hub["chi"])
    answer = sent(client, hub["chi"], "UC ORD-1")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["number"], body["state"], body["use_case"]["code"], body["use_case"]["rev"]) == (
        1,
        "draft",
        "UC ORD-1",
        1,
    )
    assert body["use_case"]["submitted"] and body["use_case"]["link"].endswith(f"/cases/{body['use_case']['key']}")
    again = sent(client, hub["chi"], "UC ORD-1")
    assert again.status_code == 409 and "in review already" in again.json()["message"], again.text
    # the version is a draft: only the use case sent alone is reviewed
    other = one(client, hub["other"], 1, "UC ORD-2")
    assert other.status_code == 409 and "was not sent to review on its own" in other.json()["message"], other.text
    approved = one(client, hub["other"], 1, "UC ORD-1")
    assert approved.status_code == 200 and approved.json()["state"] == "draft", approved.text
    assert states(client, hub["reader"], 1)["UC ORD-1"] == "approved"
    (event,) = stored(hub_db, "srs.use_case.approved")
    assert (event["code"], event["rev"], event["release"], event["version"]) == ("UC ORD-1", 1, "added", 1)
    assert stored(hub_db, "srs.version.approved") == []
    nodes, doc = graph_nodes(client, hub_db)
    assert nodes == {"UC ORD-1": ("approved", 1), "UC ORD-2": ("draft", 1), "UC RET-1": ("draft", 1)}
    assert [fragment["title"] for fragment in doc["fragments"]][0].startswith("UC ORD-1"), "the baseline alone"
    # r2 of it: sent, approved, and the baseline holds it in place of r1
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 25 minutes."))
    assert sent(client, hub["chi"], "UC ORD-1").json()["use_case"]["rev"] == 2
    assert one(client, hub["other"], 2, "UC ORD-1").status_code == 200
    nodes, doc = graph_nodes(client, hub_db)
    assert nodes["UC ORD-1"] == ("approved", 2)
    assert any("25 minutes" in fragment["text"] for fragment in doc["fragments"])
    released = sent(client, hub["chi"], "UC ORD-1")
    assert released.status_code == 409 and "approved already" in released.json()["message"]


def test_srs_a_request_for_changes_takes_a_use_case_sent_alone_back_and_a_version_in_review_takes_none(client, hub):
    imported(client, hub["chi"])
    assert sent(client, hub["chi"], "UC ORD-2").status_code == 200
    asked = one(client, hub["other"], 1, "UC ORD-2", "request_changes", body="Name the gateway")
    assert asked.status_code == 200, asked.text
    (case,) = [c for c in asked.json()["use_cases"] if c["code"] == "UC ORD-2"]
    assert (case["state"], case["submitted"]) == ("changes_requested", False)
    assert sent(client, hub["chi"], "UC ORD-2").status_code == 200, "sent again after the request"
    submitted(client, hub["chi"], 1)
    refused = sent(client, hub["chi"], "UC RET-1")
    assert refused.status_code == 409 and "is in review" in refused.json()["message"], refused.text


def test_srs_a_version_with_a_use_case_superseded_is_approved_by_its_other_use_cases(client, hub, hub_db):
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    assert sent(client, hub["chi"], "UC ORD-1").json()["use_case"]["rev"] == 2
    assert one(client, hub["other"], 2, "UC ORD-1").status_code == 200
    assert states(client, hub["reader"], 1)["UC ORD-1"] == "superseded"
    older = one(client, hub["other"], 1, "UC ORD-1")
    assert older.status_code == 409 and "superseded" in older.json()["message"], older.text
    assert one(client, hub["other"], 1, "UC ORD-2").json()["state"] == "in_review"
    assert one(client, hub["other"], 1, "UC RET-1").json()["state"] == "approved"
    nodes, _ = graph_nodes(client, hub_db)
    assert nodes["UC ORD-1"] == ("approved", 2), "approving the older version keeps the newer revision"


def test_srs_a_use_case_with_a_frame_task_already_gets_a_comment_when_approved_again(client, hub, hub_db):
    enable(client, hub["owner"], "tasks", ["plans:read"])
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    assert whole(client, hub["other"], 1).json()["state"] == "approved"
    delivered(client)
    tasks = client.get(f"/v1/x/tasks/{PROJECT}/tasks", headers=hub["reader"]).json()
    assert [t["title"] for t in tasks] == [
        "UC ORD-1: Place an order",
        "UC ORD-2: Pay for an order",
        "UC RET-1: Return a book",
    ]
    # its approvals taken back (as a hub that resets them does), UC ORD-1 is approved again: no second frame task
    e, i = tables.srs.use_case_releases, tables.srs.items
    ((item_id,),) = sql(hub_db, select(i.c.id).where(i.c.code == "UC ORD-1"))
    with live_engine(hub_db).begin() as conn:
        conn.execute(delete(e).where(e.c.item_id == item_id))
    saved(client, hub["chi"], 1, edit(client, hub["chi"], "BR ORD1-2", body="- The hold lasts 20 minutes."))
    assert sent(client, hub["chi"], "UC ORD-1").status_code == 200
    assert one(client, hub["other"], 2, "UC ORD-1").status_code == 200
    delivered(client)
    tasks = client.get(f"/v1/x/tasks/{PROJECT}/tasks", headers=hub["reader"]).json()
    assert len(tasks) == 3
    frame = client.get(f"/v1/x/tasks/{PROJECT}/tasks/T-1", headers=hub["reader"]).json()
    (comment,) = frame["comments"]
    assert comment["body"].startswith(f"Use case UC ORD-1 was approved again: {OTHER} approved UC ORD-1 r2"), comment


# The baseline in memory


def test_srs_compose_puts_each_released_use_case_in_place_adds_new_ones_under_their_heading_and_drops_removed():
    def doc(*items: tuple) -> Document:
        made = Document()
        keys: dict[str, int] = {}
        for key, kind, title, parent in items:
            keys[key] = len(made.items)
            made.items.append(Item(kind=kind, title=title, key=key, level=2, parent=keys.get(parent)))
        return made

    approved = doc(("s", "section", "Use cases", None), ("a", "use_case", "A v1", "s"), ("b", "use_case", "B", "s"))
    later = doc(
        ("s", "section", "Use cases", None),
        ("a", "use_case", "A v2", "s"),
        ("ra", "business_rule", "Rule of A", "a"),
        ("c", "use_case", "C", "s"),
    )
    found = srs_kg.compose(approved, {"a": (later, "s"), "b": (None, None), "c": (later, "s")})
    assert [
        (item.key, item.title, found.items[item.parent].key if item.parent is not None else None)
        for item in found.items
    ] == [
        ("s", "Use cases", None),
        ("a", "A v2", "s"),
        ("ra", "Rule of A", "a"),
        ("c", "C", "s"),
    ]
    alone = srs_kg.compose(None, {"c": (later, "s")})
    assert [(item.key, item.parent) for item in alone.items] == [("c", None)], "no approved version: at the top"
    assert srs_kg.compose(approved, {}) is approved


# Who learns that a use case sent alone waits, and @ba's review of it


def test_srs_a_use_case_sent_alone_tells_its_reviewers_once_with_its_link(client, hub):
    imported(client, hub["chi"])
    answer = sent(client, hub["chi"], "UC RET-1")
    assert answer.status_code == 200, answer.text
    link = answer.json()["use_case"]["link"]
    (notice,) = kind_of(client, hub["other"], notices.REVIEW)
    assert notice["title"] == f"UC RET-1 r1 of demo-shop in {PROJECT} waits for your review"
    assert (notice["link"], notice["details"]["rev"], notice["details"]["case_key"]) == (
        link,
        1,
        answer.json()["use_case"]["key"],
    )
    assert link in notice["body"]
    assert kind_of(client, hub["chi"], notices.REVIEW) == [], "its author may not review it"


def test_srs_ba_reviews_a_use_case_sent_alone_and_its_approval_waits_for_that_run(client, hub):
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    answer = sent(client, hub["chi"], "UC ORD-2")
    run = answer.json()["use_case_review_run"]
    assert run is not None and run["state"] == "queued", answer.text
    spec = running(client, studio, "task")
    assert spec["id"] == run["id"] and "Review use case UC ORD-2 r1" in spec["prompt"], spec["prompt"]
    held = one(client, hub["other"], 1, "UC ORD-2")
    assert held.status_code == 409 and f"(run #{run['id']}) is running" in held.json()["message"], held.text
    moved(client, studio, run["id"], "verifying", "done", summary="Nothing found.")
    delivered(client)
    assert [n["title"] for n in kind_of(client, hub["other"], notices.REVIEW)] == [
        f"UC ORD-2 r1 of demo-shop in {PROJECT} waits for your review"
    ]
    assert one(client, hub["other"], 1, "UC ORD-2").status_code == 200

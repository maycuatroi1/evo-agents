"""The review queue routes of the knowledge graph (``evo_agents.hub.server.kg_review``) and ``evo-agents hub kg
review``.

Checked here: the queue lists the items of the latest build a member can see, by rule then most connected first, with
counts per rule; a member whose grant stops at internal never receives the customer contract's item, its id or a count
that includes it, and a public reader sees none; ``rule``, ``cluster`` and ``state`` narrow the list; a writer
dismisses an item with a reason, kept by fingerprint so it stays dismissed after the next build; a reader may not
dismiss, an item the writer cannot see or that does not exist answers 404, a blank reason 422; dismissing writes the
audit log; the command line prints the hub's answer and dismisses.

One hub serves every test of the module, built as in ``tests.hub.test_kg_clusters``; xdist may give a worker any of
the tests, so each one reads the queue as it finds it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import get_args

import pytest

from tests.hub import live, pg
from tests.hub.kg_fixture import CONTRACT, Harness, registration

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import select

from evo_agents.hub import kg_review_cli, tables
from evo_agents.hub.server import audit
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.kg_review import Rule
from evo_agents.kg import clusters as kg_clusters
from evo_agents.kg.review import RULES
from tests.hub.s3 import fake_s3
from tests.hub.test_kg import InProcessHub, make_config, run_jobs

PROJECT = "kgreview"
EMPTY = "kgreview-empty"  # registered, never pushed: no graph
MEMBERS = {
    "wendy": ("writer", "customer"),  # pushed the fixture
    "ivan": ("reader", "internal"),
    "pat": ("reader", "public"),
}

pytestmark = pytest.mark.skipif(not kg_clusters.available(), reason=f"the review queue needs {kg_clusters.INSTALL}")


@pytest.fixture(scope="module")
def hub(hub_template, tmp_path_factory):
    base = tmp_path_factory.mktemp("kg-review")
    db = pg.clone_database(hub_template)  # a copy of the migrated template, as tests.hub.test_kg_clusters says why
    try:
        with fake_s3() as s3:
            app = create_app(make_config(db, base / "api", s3))
            with TestClient(app) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                for name in (PROJECT, EMPTY):
                    response = client.put(f"/v1/projects/{name}", json=registration(name), headers=admin)
                    assert response.status_code == 200, response.text
                members = {}
                for login, (role, level) in MEMBERS.items():
                    for name in (PROJECT, EMPTY):
                        response = client.put(
                            f"/v1/admin/projects/{name}/grants/{login}",
                            json={"role": role, "max_level": level},
                            headers=admin,
                        )
                        assert response.status_code in (200, 201), response.text
                    members[login] = live.bearer(live.insert_token(db, login))
                harness = Harness(base / "machine", PROJECT)
                harness.sync()
                harness.push(InProcessHub(client, members["wendy"]))
                run_jobs(db, s3, base / "worker")

                def rebuild(extra: str) -> dict:
                    harness.sync(extra)
                    harness.push(InProcessHub(client, members["wendy"]))
                    run_jobs(db, s3, base / "worker")
                    return client.get(f"/v1/kg/{PROJECT}/builds", headers=members["wendy"]).json()["builds"][0]

                builds = client.get(f"/v1/kg/{PROJECT}/builds", headers=members["wendy"]).json()["builds"]
                assert builds[0]["status"] == "succeeded", builds[0]
                yield SimpleNamespace(client=client, db=db, build=builds[0], rebuild=rebuild, **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def get(hub, member: str, path: str, **params):
    return hub.client.get(path, params=params, headers=getattr(hub, member))


def queue(hub, member: str, **params) -> dict:
    response = get(hub, member, f"/v1/kg/{PROJECT}/review", **params)
    assert response.status_code == 200, response.text
    return response.json()


def dismiss(hub, member: str, fingerprint: str, reason: str):
    return hub.client.post(
        f"/v1/kg/{PROJECT}/review/{fingerprint}/dismiss", json={"reason": reason}, headers=getattr(hub, member)
    )


def contract_item(found: dict) -> dict:
    return next(i for i in found["items"] if CONTRACT in {n["id"] for n in i["nodes"]})


def test_the_routes_rules_are_the_rules_of_the_queue():
    assert get_args(Rule) == RULES


def test_the_queue_lists_the_items_a_writer_sees_by_rule_and_weight_with_counts(hub):
    found = queue(hub, "wendy", state="all")
    assert found["graph"]["build_id"] >= hub.build["id"] and found["computed"] is True and found["reason"] is None
    assert found["can_dismiss"] is True
    assert [c["rule"] for c in found["counts"]] == list(RULES)
    items = found["items"]
    assert items and len({i["fingerprint"] for i in items}) == len(items)
    rank = {rule: n for n, rule in enumerate(RULES)}
    order = [(rank[i["rule"]], -i["weight"]) for i in items]
    assert order == sorted(order)
    for c in found["counts"]:
        assert c["open"] + c["dismissed"] == sum(1 for i in items if i["rule"] == c["rule"])
    for item in items:
        assert item["fingerprint"].startswith("r:") and item["nodes"] and item["action"]
        assert "required" not in item["detail"] and "least" not in item["detail"]
        if item["cluster"]:
            assert item["cluster_label"]
    contract = contract_item(found)
    assert contract["rule"] == "unmentioned_document" and contract["action"] == "link_or_deprecate"


def test_an_internal_reader_never_sees_the_customer_contracts_item_and_a_public_one_sees_none(hub):
    wendy, ivan = queue(hub, "wendy", state="all"), queue(hub, "ivan", state="all")
    assert ivan["can_dismiss"] is False
    text = json.dumps(ivan).lower()
    assert CONTRACT not in text and "acme" not in text
    wendy_items = {i["fingerprint"] for i in wendy["items"]}
    ivan_items = {i["fingerprint"] for i in ivan["items"]}
    assert ivan_items < wendy_items and wendy_items - ivan_items == {contract_item(wendy)["fingerprint"]}
    totals = {c["rule"]: c["open"] + c["dismissed"] for c in ivan["counts"]}
    assert totals["unmentioned_document"] == sum(1 for i in ivan["items"] if i["rule"] == "unmentioned_document")
    pat = queue(hub, "pat", state="all")
    assert pat["items"] == [] and all(c["open"] == c["dismissed"] == 0 for c in pat["counts"])


def test_rule_cluster_and_state_narrow_the_list(hub):
    every = queue(hub, "ivan", state="all")
    rule = every["items"][0]["rule"]
    assert {i["rule"] for i in queue(hub, "ivan", state="all", rule=rule)["items"]} == {rule}
    other = next(r for r in RULES if r != rule)
    narrowed = queue(hub, "ivan", state="all", rule=other)
    assert [i["fingerprint"] for i in narrowed["items"]] == [
        i["fingerprint"] for i in every["items"] if i["rule"] == other
    ]
    assert narrowed["counts"] == every["counts"]  # the counts are of the whole queue
    cluster = next(i["cluster"] for i in every["items"] if i["cluster"])
    in_cluster = queue(hub, "ivan", state="all", cluster=cluster)["items"]
    assert in_cluster and {i["cluster"] for i in in_cluster} == {cluster}
    assert queue(hub, "ivan", state="all", cluster="no-such-cluster")["items"] == []
    opened = {i["fingerprint"] for i in queue(hub, "ivan")["items"]}
    dismissed = {i["fingerprint"] for i in queue(hub, "ivan", state="dismissed")["items"]}
    assert not opened & dismissed and opened | dismissed == {i["fingerprint"] for i in every["items"]}
    assert all(i["dismissal"] is None for i in queue(hub, "ivan")["items"])
    assert get(hub, "ivan", f"/v1/kg/{PROJECT}/review", rule="no_such_rule").status_code == 422
    assert get(hub, "ivan", f"/v1/kg/{PROJECT}/review", state="closed").status_code == 422


def test_a_project_without_a_build_has_an_empty_queue_with_a_reason(hub):
    response = hub.client.get(f"/v1/kg/{EMPTY}/review", headers=hub.ivan)
    assert response.status_code == 200, response.text
    found = response.json()
    assert found["graph"] is None and found["computed"] is False and found["reason"] and found["items"] == []
    assert [c["rule"] for c in found["counts"]] == list(RULES)


def test_a_writer_dismisses_an_item_it_stays_dismissed_after_the_next_build_and_readers_may_not(hub):
    target = next(i for i in queue(hub, "ivan")["items"] if CONTRACT not in json.dumps(i))
    fp = target["fingerprint"]

    assert dismiss(hub, "ivan", fp, "nothing to do").status_code == 403
    assert dismiss(hub, "wendy", fp, "   ").status_code == 422
    assert dismiss(hub, "wendy", "r:0000000000000000", "nothing to do").status_code == 404
    assert dismiss(hub, "wendy", "not-a-fingerprint", "nothing to do").status_code == 422

    made = dismiss(hub, "wendy", fp, "  an old note, kept on purpose  ")
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["reason"] == "an old note, kept on purpose" and body["by"] == "wendy" and body["build_id"]
    a = tables.audit
    logged = live.sql(hub.db, select(a.c.target).where(a.c.action == audit.KG_REVIEW, a.c.target.endswith(fp)))
    assert logged == [(f"{PROJECT} item={fp}",)]

    dismissed = queue(hub, "ivan", state="dismissed")["items"]
    assert fp in {i["fingerprint"] for i in dismissed}
    assert next(i for i in dismissed if i["fingerprint"] == fp)["dismissal"]["reason"] == body["reason"]
    assert fp not in {i["fingerprint"] for i in queue(hub, "ivan")["items"]}

    again = dismiss(hub, "wendy", fp, "kept on purpose, said twice")
    assert again.status_code == 200 and again.json()["reason"] == "kept on purpose, said twice"

    before = queue(hub, "wendy", state="all")["graph"]["build_id"]
    newer = hub.rebuild("later-note")
    assert newer["status"] == "succeeded" and newer["id"] != before
    after = queue(hub, "wendy", state="all")
    assert after["graph"]["build_id"] == newer["id"]
    still = next(i for i in after["items"] if i["fingerprint"] == fp)
    assert still["dismissal"]["reason"] == "kept on purpose, said twice"
    assert still["dismissal"]["build_id"] == body["build_id"]  # the build the writer saw it in


def test_the_command_line_prints_the_queue_and_dismisses(hub, capsys):
    client = InProcessHub(hub.client, hub.wendy)
    args = SimpleNamespace(
        project=PROJECT, action=None, fingerprint=None, rule=None, cluster=None, state="all", reason=None, json=True
    )
    assert kg_review_cli._list(client, args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == set(kg_review_cli.REVIEW_QUEUE) and printed["items"]

    args.json = False
    assert kg_review_cli._list(client, args) == 0
    text = capsys.readouterr().out
    assert "RULE" in text and printed["items"][0]["fingerprint"] in text and "open" in text

    fp = printed["items"][-1]["fingerprint"]
    done = SimpleNamespace(project=PROJECT, action="dismiss", fingerprint=fp, reason="checked by hand", json=True)
    assert kg_review_cli._dismiss(client, done) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == set(kg_review_cli.DISMISSAL) and shown["reason"] == "checked by hand"


def test_a_writer_takes_back_a_dismissal_a_core_plugin_made_and_the_item_is_open_again(hub, capsys):
    from sqlalchemy import insert

    target = next(i for i in queue(hub, "wendy")["items"] if CONTRACT not in json.dumps(i))
    fp = target["fingerprint"]
    project_id = live.sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    live.sql(
        hub.db,
        insert(tables.kg_review_dismissals).values(
            project_id=project_id,
            fingerprint=fp,
            rule=target["rule"],
            reason="a plugin's reason",
            dismissed_by=None,
            dismissed_by_plugin="jev@typesafe",
        ),
    )
    shown = next(i for i in queue(hub, "wendy", state="dismissed")["items"] if i["fingerprint"] == fp)
    assert shown["dismissal"]["by"] == "plugin:jev@typesafe" and shown["dismissal"]["reason"] == "a plugin's reason"
    path = f"/v1/kg/{PROJECT}/review/{fp}/dismiss"
    assert hub.client.delete(path, headers=hub.ivan).status_code == 403  # a reader may not
    undone = hub.client.delete(path, headers=hub.wendy)
    assert undone.status_code == 200, undone.text
    assert undone.json() == {"fingerprint": fp, "was_by": "plugin:jev@typesafe"}
    assert fp in {i["fingerprint"] for i in queue(hub, "wendy")["items"]}
    assert hub.client.delete(path, headers=hub.wendy).status_code == 404  # nothing is dismissed now
    a = tables.audit
    logged = live.sql(hub.db, select(a.c.target).where(a.c.action == audit.KG_REVIEW, a.c.target.contains(fp)))
    assert (f"{PROJECT} item={fp} reopened dismissed_by=plugin:jev@typesafe",) in logged

    assert dismiss(hub, "wendy", fp, "dismissed again by a member").status_code == 200
    client = InProcessHub(hub.client, hub.wendy)
    args = SimpleNamespace(project=PROJECT, action="undismiss", fingerprint=fp, json=True)
    assert kg_review_cli._undismiss(client, args) == 0
    assert json.loads(capsys.readouterr().out) == {"fingerprint": fp, "was_by": "wendy"}

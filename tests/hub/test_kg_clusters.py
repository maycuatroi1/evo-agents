"""The cluster map routes of the knowledge graph (``evo_agents.hub.server.kg_map``) and ``evo-agents hub kg clusters``.

Checked here: the map lists the level-1 clusters a member can see with sizes and kinds counted over their visible
members, links between listed clusters only, and the members with their edges as indexes; a member whose grant stops
at internal never receives the customer contract, its id, a word of its name in a label, or a count that includes it,
from the map, a cluster or the whole graph; a cluster with no visible member answers 404 like one that does not exist;
one cluster lists its subclusters, members and neighbours; the whole graph is every visible node and edge, compact,
capped by ``limit``; a project without a build, a build without clusters and a reader who sees none get an empty map
with a reason; the command line prints the hub's answer.

One hub serves every test of the module (they only read), built as in ``tests.hub.test_kg_api``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.hub.kg_fixture import CONTRACT, CONTRACT_SECTION, Harness, registration

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import kg_clusters_cli, kg_map
from evo_agents.hub.server.app import create_app
from evo_agents.kg import clusters as kg_clusters
from tests.hub.s3 import fake_s3
from tests.hub.test_kg import InProcessHub, make_config, run_jobs

PROJECT = "kgmap"
EMPTY = "kgmap-empty"  # registered, never pushed: no graph
MEMBERS = {
    "wendy": ("writer", "customer"),  # pushed the fixture
    "ivan": ("reader", "internal"),
    "pat": ("reader", "public"),
}

pytestmark = pytest.mark.skipif(not kg_clusters.available(), reason=f"clusters need {kg_clusters.INSTALL}")


@pytest.fixture(scope="module")
def hub(hub_template, tmp_path_factory):
    base = tmp_path_factory.mktemp("kg-map")
    # a copy of the migrated template: xdist spreads the module over its workers, and each one migrating an empty
    # database in one transaction at once runs the server out of locks
    db = pg.clone_database(hub_template)
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
                builds = client.get(f"/v1/kg/{PROJECT}/builds", headers=members["wendy"]).json()["builds"]
                assert builds[0]["status"] == "succeeded", builds[0]
                yield SimpleNamespace(client=client, build=builds[0], **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def get(hub, member: str, path: str, **params):
    return hub.client.get(path, params=params, headers=getattr(hub, member))


def ok(hub, member: str, path: str, **params) -> dict:
    response = get(hub, member, path, **params)
    assert response.status_code == 200, response.text
    return response.json()


def cluster_map(hub, member: str) -> dict:
    return ok(hub, member, f"/v1/kg/{PROJECT}/clusters")


def check_map(found: dict) -> None:
    """Sizes and kinds are counts of the listed members; links and edges join listed clusters and members."""
    ids = {c["id"] for c in found["clusters"]}
    by_cluster: dict[str, list[dict]] = {}
    for node in found["nodes"]:
        by_cluster.setdefault(node["cluster"], []).append(node)
    assert set(by_cluster) == ids
    for cluster in found["clusters"]:
        members = by_cluster[cluster["id"]]
        assert cluster["level"] == 1 and cluster["parent"] is None and cluster["label"]
        assert cluster["size"] == len(members) == sum(cluster["kinds"].values())
    sizes = [c["size"] for c in found["clusters"]]
    assert sizes == sorted(sizes, reverse=True)
    for link in found["links"]:
        assert {link["a"], link["b"]} <= ids and link["a"] < link["b"] and link["weight"] > 0
    for a, z, weight in found["edges"]:
        assert 0 <= a < len(found["nodes"]) and 0 <= z < len(found["nodes"]) and a != z and weight > 0


def contract_cluster(found: dict) -> str:
    return next(n["cluster"] for n in found["nodes"] if n["id"] == CONTRACT)


def test_the_map_lists_the_clusters_a_writer_sees_with_their_members(hub):
    found = cluster_map(hub, "wendy")
    assert found["graph"]["build_id"] == hub.build["id"]
    assert found["computed"] is True and found["reason"] is None
    check_map(found)
    assert CONTRACT in {n["id"] for n in found["nodes"]}
    assert not {n["kind"] for n in found["nodes"]} & {"Symbol", "Section", "Source", "Repo"}


def test_an_internal_reader_never_sees_the_customer_contract_in_the_map(hub):
    wendy, ivan = cluster_map(hub, "wendy"), cluster_map(hub, "ivan")
    check_map(ivan)
    text = json.dumps(ivan).lower()
    assert CONTRACT not in text and "acme" not in text
    wendy_ids, ivan_ids = {n["id"] for n in wendy["nodes"]}, {n["id"] for n in ivan["nodes"]}
    assert ivan_ids < wendy_ids and CONTRACT in wendy_ids - ivan_ids
    assert sum(c["size"] for c in ivan["clusters"]) == len(ivan_ids)
    hidden = contract_cluster(wendy)
    sizes = {c["id"]: c["size"] for c in ivan["clusters"]}
    seen = sum(1 for n in wendy["nodes"] if n["cluster"] == hidden and n["id"] in ivan_ids)
    assert sizes.get(hidden, 0) == seen  # the contract is not counted

    pat = cluster_map(hub, "pat")
    assert {n["id"] for n in pat["nodes"]} <= ivan_ids
    if not pat["clusters"]:
        assert pat["computed"] is False and pat["reason"] == kg_map.NO_VISIBLE


def test_the_map_without_nodes_lists_the_clusters_alone(hub):
    found = ok(hub, "wendy", f"/v1/kg/{PROJECT}/clusters", nodes="false")
    assert found["nodes"] == [] and found["edges"] == []
    assert found["clusters"] == cluster_map(hub, "wendy")["clusters"]


def test_one_cluster_lists_its_members_subclusters_and_neighbours(hub):
    wendy = cluster_map(hub, "wendy")
    cid = contract_cluster(wendy)
    found = ok(hub, "wendy", f"/v1/kg/{PROJECT}/cluster", id=cid)
    assert found["cluster"]["id"] == cid
    assert {m["id"] for m in found["members"]} == {n["id"] for n in wendy["nodes"] if n["cluster"] == cid}
    assert found["cluster"]["size"] == len(found["members"])
    for sub in found["subclusters"]:
        assert sub["level"] == 2 and sub["parent"] == cid
        detail = ok(hub, "wendy", f"/v1/kg/{PROJECT}/cluster", id=sub["id"])
        assert {m["sub"] for m in detail["members"]} == {sub["id"]}
    listed = {c["id"]: c for c in wendy["clusters"]}
    for neighbour in found["neighbours"]:
        assert neighbour["id"] in listed and neighbour["id"] != cid and neighbour["weight"] > 0
        assert any(
            cid in (link["a"], link["b"]) and neighbour["id"] in (link["a"], link["b"]) for link in wendy["links"]
        )
    for a, z, _w in found["edges"]:
        assert 0 <= a < len(found["members"]) and 0 <= z < len(found["members"])


def test_a_cluster_shows_an_internal_reader_only_what_they_may_read(hub):
    cid = contract_cluster(cluster_map(hub, "wendy"))
    response = get(hub, "ivan", f"/v1/kg/{PROJECT}/cluster", id=cid)
    if response.status_code == 404:  # the contract was its only member
        assert cid not in response.text
    else:
        assert response.status_code == 200, response.text
        text = response.text.lower()
        assert CONTRACT not in text and "acme" not in text
    unknown = get(hub, "ivan", f"/v1/kg/{PROJECT}/cluster", id="c1-does-not-exist")
    assert unknown.status_code == 404
    assert get(hub, "wendy", f"/v1/kg/{EMPTY}/cluster", id=cid).status_code == 404


def test_a_project_without_a_build_has_an_empty_map_with_a_reason(hub):
    found = ok(hub, "wendy", f"/v1/kg/{EMPTY}/clusters")
    assert found["graph"] is None and found["computed"] is False and found["reason"]
    assert found["clusters"] == found["links"] == found["nodes"] == []
    whole = ok(hub, "wendy", f"/v1/kg/{EMPTY}/whole")
    assert whole["graph"] is None and whole["nodes"] == []


def test_a_build_without_clusters_says_why():
    class NoClusters:
        def clusters(self, b):
            return None

        def build_row(self, b):
            return {"coverage": {"clusters": {"computed": False, "reason": "graspologic-native is not installed"}}}

    found = kg_map.cluster_map(kg_map.view(NoClusters(), 1, lambda label: True), True)
    assert found == {
        "computed": False,
        "reason": "graspologic-native is not installed",
        "clusters": [],
        "links": [],
        "nodes": [],
        "edges": [],
    }


def test_the_whole_graph_is_every_visible_node_and_edge(hub):
    wendy = ok(hub, "wendy", f"/v1/kg/{PROJECT}/whole")
    assert wendy["graph"]["build_id"] == hub.build["id"] and wendy["truncated"] is False
    ids = [n["id"] for n in wendy["nodes"]]
    assert ids == sorted(ids) and CONTRACT in ids and CONTRACT_SECTION in ids
    for a, z, rel in wendy["edges"]:
        assert 0 <= a < len(ids) and 0 <= z < len(ids) and 0 <= rel < len(wendy["rels"])
    clusters = {n["id"]: n["cluster"] for n in cluster_map(hub, "wendy")["nodes"]}
    by_id = {n["id"]: n for n in wendy["nodes"]}
    assert by_id[CONTRACT]["cluster"] == clusters[CONTRACT]
    assert by_id[CONTRACT_SECTION]["cluster"] == clusters[CONTRACT]  # a section counts in its document's cluster

    ivan = ok(hub, "ivan", f"/v1/kg/{PROJECT}/whole")
    ivan_ids = {n["id"] for n in ivan["nodes"]}
    assert ivan_ids < set(ids) and CONTRACT not in ivan_ids and CONTRACT_SECTION not in ivan_ids
    assert CONTRACT not in json.dumps(ivan)
    assert len(ivan["edges"]) < len(wendy["edges"])

    capped = ok(hub, "wendy", f"/v1/kg/{PROJECT}/whole", limit=5)
    assert capped["truncated"] is True and [n["id"] for n in capped["nodes"]] == ids[:5]


def test_the_command_line_prints_the_clusters_and_one_cluster(hub, capsys):
    client = InProcessHub(hub.client, hub.wendy)
    args = SimpleNamespace(project=PROJECT, action=None, id=None, json=True)
    assert kg_clusters_cli._list(client, args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert isinstance(printed, list) and printed == cluster_map(hub, "wendy")["clusters"]
    assert all({"id", "label", "size"} <= set(c) for c in printed)

    cid = printed[0]["id"]
    assert kg_clusters_cli._show(client, SimpleNamespace(project=PROJECT, action="show", id=cid, json=True)) == 0
    shown = json.loads(capsys.readouterr().out)
    assert set(shown) == set(kg_clusters_cli.CLUSTER_DETAIL) and shown["cluster"]["id"] == cid

    assert kg_clusters_cli._list(client, SimpleNamespace(project=PROJECT, action=None, id=None, json=False)) == 0
    text = capsys.readouterr().out
    assert cid in text and "CLUSTER" in text

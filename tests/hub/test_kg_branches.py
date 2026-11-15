"""Draft branches of a project's graph (plan kg-centralized-on-hub, criterion a5): ``evo_agents.kg.branch_ops``,
``evo_agents.hub.kg_branches``, the routes of ``evo_agents.hub.server.kg_branches`` and ``evo-agents hub kg branch``.

Checked without a database: an operation is checked and normalized, applying ones leaves the rows of main as they
were, the edge id is the store's, the diff counts what changed, the CLI reads its operations from a file. With a test
database (``EVO_HUB_TEST_DSN``): a writer opens a branch, applies operations, reads it with the kg tools and the web
routes while main does not change, sees its diff and the ontology rules it breaks; a merge is refused with 409 when
main changed what it changes or it adds a hard violation, merges otherwise and outlives the next build; a branch is
dropped; a reader cannot write; the build a tool names is the hub's."""

from __future__ import annotations

import io
import json

import pytest

pytest.importorskip("sqlalchemy", reason="the hub tables need the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub import kg_branches_cli
from evo_agents.kg import branch_ops, store
from evo_agents.kg.policy import Label
from evo_agents.kg.snapshot import StoreRows, node_dict, node_row
from tests.hub import pg

TOP = ((1, 0), frozenset())


def _node(node_id: str, kind: str = "Document", name: str | None = None) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "name": name or node_id,
        "props": {},
        "status": "parsed",
        "conf": 1.0,
        "label": Label(1, 0, "T", frozenset(), frozenset()),
        "aliases": [],
    }


def rows_of(*node_ids: str) -> StoreRows:
    return StoreRows("sha256:main", tuple(node_row(n, _node(n)) for n in node_ids), (), (), {}, TOP)


# Without a database


def test_the_edge_id_of_a_branch_is_the_store_s():
    assert branch_ops.edge_id("a", "mentions", "b") == store.edge_id("a", "mentions", "b")


@pytest.mark.parametrize(
    "op",
    [
        {"op": "node.drop", "id": "x"},
        {"op": "node.put", "id": "x"},
        {"op": "edge.put", "src": "a", "rel": "", "dst": "b"},
        {"op": "decision", "kind": "rename", "src": "a"},
        {"op": "decision", "kind": "deprecate", "src": "a", "rel": "r"},
        {"op": "node.put", "id": "x", "kind": "K", "label": [1]},
        "node.put",
    ],
)
def test_an_operation_that_is_not_one_is_refused(op):
    with pytest.raises(branch_ops.OpError):
        branch_ops.normalize(op)


def test_applying_operations_leaves_main_as_it_was_and_the_diff_counts_them():
    main = rows_of("a", "b", "c")
    ops = [
        branch_ops.normalize(op)
        for op in (
            {"op": "node.put", "id": "d", "kind": "Concept", "name": "Delta kiwi"},
            {"op": "edge.put", "src": "a", "rel": "mentions", "dst": "d"},
            {"op": "node.delete", "id": "c"},
            {"op": "decision", "kind": "deprecate", "src": "b"},
        )
    ]
    branch = branch_ops.apply(main, ops)
    assert [r[0] for r in main.nodes] == ["a", "b", "c"]
    assert branch.content_hash != main.content_hash
    found = branch_ops.diff(main, branch)
    assert found["counts"] == {
        "nodes_added": 1,
        "nodes_changed": 1,
        "nodes_removed": 1,
        "edges_added": 1,
        "edges_changed": 0,
        "edges_removed": 0,
    }
    (deprecated,) = [node_dict(r) for r in branch.nodes if r[0] == "b"]
    assert deprecated["props"].get("deprecated")


def test_an_edge_to_a_node_the_branch_lacks_is_refused():
    with pytest.raises(branch_ops.OpError):
        branch_ops.apply(rows_of("a"), [branch_ops.normalize({"op": "edge.put", "src": "a", "rel": "r", "dst": "z"})])


def test_the_cli_reads_operations_from_a_file_or_stdin(tmp_path, monkeypatch):
    ops = [{"op": "node.delete", "id": "a"}]
    path = tmp_path / "ops.json"
    path.write_text(json.dumps({"ops": ops}), encoding="utf-8")
    assert kg_branches_cli.read_ops(str(path)) == ops
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ops)))
    assert kg_branches_cli.read_ops("-") == ops
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        kg_branches_cli.read_ops(str(path))


# With a database

needs_db = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    from sqlalchemy import update

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_kg import PROJECT, Machine, call_tool, doc, hub, push, tool_ok  # noqa: F401 (hub: a fixture)
    from tests.hub.test_kg_postgres_build import build, project_id, read_back


def _base(name: str | None = None) -> str:
    return f"/v1/kg/{PROJECT}/branches" + ("" if name is None else f"/{name}")


def _post(hub, path: str, body: dict | None = None, member: str = "alice"):
    return hub.client.post(path, json=body, headers=getattr(hub, member))


def _built(hub, tmp_path, caplog, *docs):
    laptop = Machine(tmp_path, "laptop")
    laptop.sync("docs", *docs)
    assert push(hub, laptop).ok
    found, _ = build(hub, caplog)
    return laptop, found


def _guide(hub) -> str:
    rows = read_back(hub.db, project_id(hub))
    return next(r[0] for r in rows.nodes if r[1] == "Document" and "guide" in r[0])


def _ids(result: dict) -> str:
    return json.dumps(result.get("structuredContent", {}), ensure_ascii=False)


@needs_db
def test_a_branch_is_read_apart_from_main_then_merged_and_outlives_the_next_build(hub, tmp_path, caplog):
    laptop, first = _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    created = _post(hub, _base(), {"name": "kiwi-draft"})
    assert created.status_code == 201, created.text
    assert created.json()["base_build_id"] == first["id"] and created.json()["status"] == "open"
    assert _post(hub, _base(), {"name": "kiwi-draft"}).status_code == 409
    assert _post(hub, _base(), {"name": "main"}).status_code == 422
    assert _post(hub, _base(), {"name": "other"}, member="carol").status_code == 403

    ops = [
        {"op": "node.put", "id": "concept:mango", "kind": "Concept", "name": "Mango pipeline"},
        {"op": "edge.put", "src": guide, "rel": "mentions", "dst": "concept:mango"},
    ]
    applied = _post(hub, f"{_base('kiwi-draft')}/ops", {"ops": ops})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] == 2
    bad = _post(
        hub, f"{_base('kiwi-draft')}/ops", {"ops": [{"op": "edge.put", "src": guide, "rel": "r", "dst": "x:none"}]}
    )
    assert bad.status_code == 422, bad.text

    on_branch = tool_ok(hub, "kg_search", {"query": "Mango pipeline", "branch": "kiwi-draft"})
    assert "concept:mango" in _ids(on_branch)
    assert "concept:mango" not in _ids(tool_ok(hub, "kg_search", {"query": "Mango pipeline"}))
    assert "concept:mango" not in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}
    missing = call_tool(hub, "kg_search", {"query": "kiwi", "branch": "nope"}).json()
    assert missing["isError"] and "nope" in missing["content"][0]["text"]
    node = f"/v1/kg/{PROJECT}/node?id=concept:mango"
    assert hub.client.get(f"{node}&branch=kiwi-draft", headers=hub.alice).status_code == 200
    assert hub.client.get(node, headers=hub.alice).status_code == 404
    assert hub.client.get(f"{node}&branch=nope", headers=hub.alice).status_code == 404

    diff = hub.client.get(f"{_base('kiwi-draft')}/diff", headers=hub.alice).json()
    assert diff["counts"]["nodes_added"] == 1 and diff["counts"]["edges_added"] == 1
    check = hub.client.get(f"{_base('kiwi-draft')}/check", headers=hub.carol).json()
    assert check["mergeable"] and check["conflicts"] == []

    merged = _post(hub, f"{_base('kiwi-draft')}/merge")
    assert merged.status_code == 200, merged.text
    assert merged.json()["nodes_added"] == 1
    assert "concept:mango" in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}
    assert hub.client.delete(_base("kiwi-draft"), headers=hub.alice).status_code == 409  # merged: a layer of main

    laptop.sync("docs", doc("guide", "How the kiwi app works, again.", rev="2"))
    assert push(hub, laptop).ok
    build(hub, caplog)
    assert "concept:mango" in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}  # the merge outlived it
    listed = hub.client.get(f"{_base()}?status=merged", headers=hub.carol).json()["branches"]
    assert [b["name"] for b in listed] == ["kiwi-draft"]


@needs_db
def test_a_merge_is_refused_when_main_changed_what_the_branch_changes(hub, tmp_path, caplog):
    laptop, _ = _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    assert _post(hub, _base(), {"name": "stale"}).status_code == 201
    op = {"op": "decision", "kind": "deprecate", "src": guide}
    assert _post(hub, f"{_base('stale')}/ops", {"ops": [op]}).status_code == 200
    laptop.sync("docs", doc("guide", "The kiwi app works another way now.", rev="2"))
    assert push(hub, laptop).ok
    build(hub, caplog)
    if guide not in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}:
        pytest.skip("the rebuilt guide has another id")
    check = hub.client.get(f"{_base('stale')}/check", headers=hub.alice).json()
    assert not check["mergeable"] and check["conflicts"]
    refused = _post(hub, f"{_base('stale')}/merge")
    assert refused.status_code == 409, refused.text
    assert "main changed" in refused.text
    dropped = hub.client.delete(_base("stale"), headers=hub.alice)
    assert dropped.status_code == 200 and dropped.json()["ops"] == 1
    assert hub.client.get(_base("stale"), headers=hub.alice).status_code == 404


@needs_db
def test_a_merge_is_refused_for_a_new_violation_of_a_hard_rule(hub, tmp_path, caplog):
    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    ontology = {"relations": [{"name": "owned_by", "domain": ["Team"], "range": ["Team"]}]}
    configs = tables.kg_configs
    sql(hub.db, update(configs).where(configs.c.project_id == project_id(hub)).values(ontology=ontology))
    assert _post(hub, _base(), {"name": "owners"}).status_code == 201
    ops = [
        {"op": "node.put", "id": "team:lumen-core", "kind": "Team", "name": "Lumen core"},
        {"op": "edge.put", "src": guide, "rel": "owned_by", "dst": "team:lumen-core"},
    ]
    assert _post(hub, f"{_base('owners')}/ops", {"ops": ops}).status_code == 200
    check = hub.client.get(f"{_base('owners')}/check", headers=hub.alice).json()
    assert check["rules"] and [v["rule"] for v in check["hard"]] == ["wrong_endpoint"]
    assert not check["mergeable"]
    refused = _post(hub, f"{_base('owners')}/merge")
    assert refused.status_code == 409 and "hard" in refused.text
    assert "team:lumen-core" not in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}


@needs_db
def test_kg_search_names_the_hub_build_and_kg_status_runs_for_a_run(hub, tmp_path, caplog):
    laptop, _ = _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    laptop.sync("docs", doc("guide", "How the kiwi app works, again.", rev="2"))
    assert push(hub, laptop).ok
    second, _ = build(hub, caplog)
    assert second["id"] != 1  # a build's own store numbers it 1; the tools name the hub's id
    text = tool_ok(hub, "kg_search", {"query": "kiwi"})["content"][0]["text"]
    assert f"(build {second['id']})" in text, text
    assert "(build 1)" not in text
    branch = _post(hub, _base(), {"name": "label"})
    assert branch.status_code == 201
    on_branch = tool_ok(hub, "kg_search", {"query": "kiwi", "branch": "label"})["content"][0]["text"]
    assert f"(build {second['id']})" in on_branch

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

from evo_agents.hub.cli.kg import branches as kg_branches_cli
from evo_agents.kg import branch_ops, graph_model
from evo_agents.kg.policy import Label
from evo_agents.kg.snapshot import StoreRows, edge_dict, edge_row, node_dict, node_row
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
    assert branch_ops.edge_id("a", "mentions", "b") == graph_model.edge_id("a", "mentions", "b")


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


def test_a_merged_change_to_a_node_that_left_the_sources_is_left_out_not_made_again():
    from evo_agents.hub import kg_branches as hub_branches

    main = rows_of("doc:guide")
    payload = {"op": "node.put", "id": "doc:guide", "kind": "Document", "props": {"owner": "kiwi"}}
    before = {"doc:guide": list(main.nodes[0])}
    replayed = hub_branches._replayed({**branch_ops.normalize(payload), "before": before})
    skipped: list[str] = []
    rebuilt = branch_ops.apply(rows_of("doc:other"), [replayed], skipped)
    assert len(skipped) == 1 and "doc:guide" not in {r[0] for r in rebuilt.nodes}
    kept = branch_ops.apply(main, [replayed], [])
    assert node_dict(next(r for r in kept.nodes if r[0] == "doc:guide"))["props"] == {"owner": "kiwi"}
    added = hub_branches._replayed({**branch_ops.normalize(payload), "before": {"doc:guide": None}})
    assert "doc:guide" in {r[0] for r in branch_ops.apply(rows_of("doc:other"), [added], []).nodes}


def test_a_diff_shows_only_what_the_reader_reads():
    secret = {**_node("doc:secret"), "label": Label(3, 0, "T", frozenset(), frozenset())}
    main = StoreRows(
        "sha256:main", (node_row("doc:a", _node("doc:a")), node_row("doc:secret", secret)), (), (), {}, TOP
    )
    ops = [
        branch_ops.normalize(op)
        for op in (
            {"op": "node.put", "id": "doc:secret", "kind": "Document", "props": {"x": 1}},
            {"op": "node.put", "id": "doc:a", "kind": "Document", "props": {"y": 1}},
        )
    ]
    found = branch_ops.diff(main, branch_ops.apply(main, ops))
    seen = branch_ops.visible(main, frozenset(), (1, 0))
    assert seen == {"doc:a"} and branch_ops.visible(main, frozenset(), None) == set()
    shown = branch_ops.shown_diff(found, seen)
    assert [c["after"]["id"] for c in shown["nodes"]["changed"]] == ["doc:a"]
    assert shown["counts"]["nodes_changed"] == 1 and "secret" not in json.dumps(shown)


def test_a_writer_may_not_change_what_it_does_not_read_nor_lower_a_label():
    secret = {**_node("doc:secret"), "props": {"salary": 1}, "label": Label(3, 0, "T", frozenset(), frozenset())}
    main = StoreRows(
        "sha256:main", (node_row("doc:a", _node("doc:a")), node_row("doc:secret", secret)), (), (), {}, TOP
    )

    def refusal(op: dict, ceiling: tuple[int, int] | None = (1, 0)) -> str | None:
        fresh = branch_ops.normalize(op)
        rows = branch_ops.apply(main, [fresh])
        return branch_ops.write_refusal(main, rows, [*branch_ops.touched(fresh)[0]], frozenset(), ceiling)

    lowered = {"op": "node.put", "id": "doc:secret", "kind": "Document", "label": [0, 0]}
    assert "above your grant" in refusal(lowered)  # a level-1 writer may not touch a level-3 node
    assert "below" in refusal(lowered, (3, 0))  # nor may a level-3 writer lower it
    raised = {"op": "node.put", "id": "doc:a", "kind": "Document", "label": [1, 0]}
    assert refusal(raised) is None
    assert "above your grant" in refusal({**raised, "id": "doc:new", "label": [3, 0]})
    assert refusal({"op": "node.put", "id": "doc:new", "kind": "Document", "label": [1, 0]}) is None
    assert "above your grant" in refusal(raised, None)


def test_a_merge_or_a_delete_may_not_move_or_take_away_an_edge_above_the_writer_s_grant():
    high = Label(3, 0, "T", frozenset(), frozenset())
    nodes = (
        node_row("k", _node("k")),
        node_row("g", _node("g")),
        node_row("x", {**_node("x"), "label": high}),
    )
    pays = {
        "id": branch_ops.edge_id("g", "pays", "x"),
        "src": "g",
        "rel": "pays",
        "dst": "x",
        "props": {"amount": 9},
        "status": "parsed",
        "conf": 1.0,
        "label": high,
    }
    main = StoreRows("sha256:main", nodes, (edge_row(pays["id"], pays),), (), {}, TOP)
    for op in ({"op": "decision", "kind": "merge", "src": "k", "dst": "g"}, {"op": "node.delete", "id": "g"}):
        fresh = branch_ops.normalize(op)
        rows = branch_ops.apply(main, [fresh])
        ids = {*branch_ops.touched(fresh)[0], *branch_ops.changed_ids(main, rows)}
        assert pays["id"] in ids
        assert "above your grant" in branch_ops.write_refusal(main, rows, ids, frozenset(), (1, 0))
        assert branch_ops.write_refusal(main, rows, ids, frozenset(), (3, 0)) is None
    merged = branch_ops.apply(main, [branch_ops.normalize({"op": "decision", "kind": "merge", "src": "k", "dst": "g"})])
    (moved,) = merged.edges
    assert edge_dict(moved)["label"][:2] == (3, 0)  # the moved edge keeps the label of what it holds


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
    from sqlalchemy import select, update

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
    main = read_back(hub.db, project_id(hub))
    assert "concept:mango" in {r[0] for r in main.nodes}  # the merge outlived it
    built, head = _row_and_head(hub)
    assert built == head, "the build's row names main's head"
    listed = hub.client.get(f"{_base()}?status=merged", headers=hub.carol).json()["branches"]
    assert [b["name"] for b in listed] == ["kiwi-draft"]


@needs_db
def test_a_build_of_nothing_new_after_merges_keeps_main_and_its_hash(hub, tmp_path, caplog):
    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    for name, node in (("mango", "concept:mango"), ("papaya", "concept:papaya")):  # two merges: their hashes chain
        assert _post(hub, _base(), {"name": name}).status_code == 201
        ops = [
            {"op": "node.put", "id": node, "kind": "Concept", "name": f"{name.title()} pipeline"},
            {"op": "edge.put", "src": guide, "rel": "mentions", "dst": node},
        ]
        assert _post(hub, f"{_base(name)}/ops", {"ops": ops}).status_code == 200
        assert _post(hub, f"{_base(name)}/merge").status_code == 200
    _, merged = _row_and_head(hub)
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    build(hub, caplog)
    built, head = _row_and_head(hub)
    assert head == merged, "a build of nothing new leaves main and its hash as the merges wrote them"
    assert built == head, "the build's row names main's head"


@needs_db
def test_a_branch_merged_while_a_build_of_nothing_new_runs_stays_on_main(hub, tmp_path, caplog, monkeypatch):
    from evo_agents.hub import kg_build
    from tests.hub.test_kg import latest, run_jobs

    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    assert _post(hub, _base(), {"name": "late"}).status_code == 201
    ops = [
        {"op": "node.put", "id": "concept:mango", "kind": "Concept", "name": "Mango pipeline"},
        {"op": "edge.put", "src": guide, "rel": "mentions", "dst": "concept:mango"},
    ]
    assert _post(hub, f"{_base('late')}/ops", {"ops": ops}).status_code == 200
    original, merged = kg_build.build_in_cache, []

    def merging(*args, **kwargs):
        built = original(*args, **kwargs)
        merged.append(_post(hub, f"{_base('late')}/merge"))  # after the build read main, before it keeps its graph
        return built

    monkeypatch.setattr(kg_build, "build_in_cache", merging)
    assert hub.client.post(f"/v1/kg/{PROJECT}/builds", headers=hub.alice).status_code == 202
    caplog.clear()
    with caplog.at_level("INFO", logger="evo_agents.hub.kg_build"):
        run_jobs(hub.db, hub.s3, hub.worker_dir)
    row = latest(hub)
    assert row["status"] == "succeeded"
    assert [m.status_code for m in merged] == [200], [m.text for m in merged]
    main = read_back(hub.db, project_id(hub))
    assert "concept:mango" in {r[0] for r in main.nodes}, "the merge stays on main"
    assert "kg graph left as a merge wrote it" in caplog.messages
    built, head = _row_and_head(hub)
    assert built == head, "the build's row names main's head"


def _row_and_head(hub) -> tuple[tuple, tuple]:
    """(content hash, nodes, edges) of the latest build's row, then of main's head."""
    from tests.hub.test_kg import latest

    row = latest(hub)
    heads = tables.kg_graph_heads
    head = sql(
        hub.db, select(heads.c.content_hash).where(heads.c.project_id == project_id(hub), heads.c.branch_id == 0)
    )
    main = read_back(hub.db, project_id(hub))
    return (row["content_hash"], row["nodes"], row["edges"]), (head[0][0], len(main.nodes), len(main.edges))


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


@needs_db
def test_an_apply_waits_for_a_merge_under_way_and_is_then_refused(hub, tmp_path, caplog):
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from evo_agents.hub import kg_branches as hub_branches

    laptop, _ = _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    assert _post(hub, _base(), {"name": "race"}).status_code == 201
    first = [{"op": "node.put", "id": "concept:early", "kind": "Concept", "name": "Early"}]
    assert _post(hub, f"{_base('race')}/ops", {"ops": first}).status_code == 200
    pid = project_id(hub)
    late = [{"op": "node.put", "id": "concept:late", "kind": "Concept", "name": "Late"}]

    async def race():
        engine = create_async_engine(hub.db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.connect() as merging, engine.connect() as applying:
                await merging.begin()
                await hub_branches.merge(merging, pid, "race", None)
                await applying.begin()
                waiting = asyncio.create_task(
                    hub_branches.apply(applying, pid, "race", late, None, (frozenset(), (9, 9)))
                )
                await asyncio.sleep(0.5)
                assert not waiting.done()  # the project's lock holds it until the merge ends
                await merging.commit()
                with pytest.raises(hub_branches.BranchError) as refused:
                    await waiting
                await applying.rollback()
                return refused.value
        finally:
            await engine.dispose()

    refused = asyncio.run(race())
    assert refused.status == 409 and "merged" in str(refused)
    laptop.sync("docs", doc("guide", "How the kiwi app works, again.", rev="2"))
    assert push(hub, laptop).ok
    build(hub, caplog)
    nodes = {r[0] for r in read_back(hub.db, project_id(hub)).nodes}
    assert "concept:early" in nodes and "concept:late" not in nodes


@needs_db
def test_kg_status_and_kg_more_answer_a_run_whose_system_has_a_default_graph(hub, tmp_path, caplog, monkeypatch):
    from evo_agents.hub.server import security
    from evo_agents.hub.server.security import RUN, Principal
    from evo_agents.hub.server.systems.service import traces as system_traces

    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))

    async def default_graph(conn, run_id, project_id):
        return "docs", frozenset({"docs"})

    monkeypatch.setattr(system_traces, "default_graph", default_graph)
    (user,) = sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == "alice"))[0]
    hub.client.app.dependency_overrides[security.principal] = lambda: Principal(
        user, "alice", False, 0, RUN, "", run_id=41
    )
    try:
        for tool in ("kg_status", "kg_more"):
            answer = call_tool(hub, tool, {}).json()
            text = answer["content"][0]["text"]
            assert "no argument 'graph'" not in text, text
        assert not call_tool(hub, "kg_status", {}).json().get("isError")
    finally:
        hub.client.app.dependency_overrides.pop(security.principal, None)


@needs_db
def test_a_branch_refuses_with_403_an_operation_that_lowers_a_label(hub, tmp_path, caplog):
    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    assert _post(hub, _base(), {"name": "lower"}).status_code == 201
    op = {"op": "node.put", "id": guide, "kind": "Document", "label": [0, 0], "audience": []}
    refused = _post(hub, f"{_base('lower')}/ops", {"ops": [op]})
    assert refused.status_code == 403, refused.text
    assert hub.client.get(_base("lower"), headers=hub.alice).json()["ops"] == 0


def _ontology(hub, ontology: dict) -> None:
    configs = tables.kg_configs
    sql(hub.db, update(configs).where(configs.c.project_id == project_id(hub)).values(ontology=ontology))


def _merged(hub, name: str, ops: list[dict]) -> None:
    assert _post(hub, _base(), {"name": name}).status_code == 201
    applied = _post(hub, f"{_base(name)}/ops", {"ops": ops})
    assert applied.status_code == 200, applied.text
    merged = _post(hub, f"{_base(name)}/merge")
    assert merged.status_code == 200, merged.text


@needs_db
def test_apply_diff_check_and_merge_read_only_the_branch_s_reach_not_main_whole(hub, tmp_path, caplog, monkeypatch):
    from evo_agents.hub import kg_graph_rows

    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."), doc("notes", "Notes on the lime app."))
    guide = _guide(hub)
    before = {r[0]: r for r in read_back(hub.db, project_id(hub)).nodes}

    async def whole(*args, **kwargs):
        raise AssertionError("a branch's apply, diff, check or merge read a table of main whole")

    for reader in ("read_table_rows", "_read"):  # what read_graph and write_graph read every row with
        monkeypatch.setattr(kg_graph_rows, reader, whole)
    assert _post(hub, _base(), {"name": "reach"}).status_code == 201
    ops = [
        {"op": "node.put", "id": "concept:mango", "kind": "Concept", "name": "Mango pipeline"},
        {"op": "edge.put", "src": guide, "rel": "mentions", "dst": "concept:mango"},
    ]
    applied = _post(hub, f"{_base('reach')}/ops", {"ops": ops})
    assert applied.status_code == 200, applied.text
    assert applied.json()["nodes"] == len(before) + 1, "the counts are main's whole, the branch's change added"
    diff = hub.client.get(f"{_base('reach')}/diff", headers=hub.alice).json()
    assert diff["counts"]["nodes_added"] == 1 and diff["counts"]["edges_added"] == 1
    assert hub.client.get(f"{_base('reach')}/check", headers=hub.alice).json()["mergeable"]
    merged = _post(hub, f"{_base('reach')}/merge")
    assert merged.status_code == 200, merged.text
    assert merged.json()["changed"]["kg_nodes"] == {"new": 1, "changed": 0, "deleted": 0}
    monkeypatch.undo()
    after = {r[0]: r for r in read_back(hub.db, project_id(hub)).nodes}
    assert after.keys() == before.keys() | {"concept:mango"}
    assert all(after[k] == row for k, row in before.items()), "a merge leaves the rows it does not change"


@needs_db
def test_a_cycle_closed_through_nodes_the_operations_do_not_name_is_found(hub, tmp_path, caplog):
    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    _ontology(hub, {"relations": [{"name": "part_of", "acyclic": True}]})
    nodes = [{"op": "node.put", "id": f"concept:{n}", "kind": "Concept", "name": n.title()} for n in ("a", "b", "c")]
    chain = [
        {"op": "edge.put", "src": "concept:a", "rel": "part_of", "dst": "concept:b"},
        {"op": "edge.put", "src": "concept:b", "rel": "part_of", "dst": "concept:c"},
    ]
    _merged(hub, "chain", [*nodes, *chain])
    assert _post(hub, _base(), {"name": "loop"}).status_code == 201
    close = [{"op": "edge.put", "src": "concept:c", "rel": "part_of", "dst": "concept:a"}]
    assert _post(hub, f"{_base('loop')}/ops", {"ops": close}).status_code == 200
    check = hub.client.get(f"{_base('loop')}/check", headers=hub.alice).json()
    assert [(v["rule"], v["nodes"]) for v in check["hard"]] == [("cycle", ["concept:a", "concept:b", "concept:c"])], (
        "concept:b is in reach though no operation names it"
    )
    assert not check["mergeable"]
    assert _post(hub, f"{_base('loop')}/merge").status_code == 409


@needs_db
def test_a_key_a_branch_shares_with_a_node_it_does_not_name_is_a_duplicate(hub, tmp_path, caplog):
    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    _ontology(hub, {"node_kinds": [{"name": "PullRequest", "keys": [["repo", "number"]]}]})
    first = {
        "op": "node.put",
        "id": "pr:1",
        "kind": "PullRequest",
        "name": "One",
        "props": {"repo": "kiwi", "number": 7},
    }
    _merged(hub, "first", [first])
    assert _post(hub, _base(), {"name": "again"}).status_code == 201
    again = {**first, "id": "pr:2", "name": "Two"}
    assert _post(hub, f"{_base('again')}/ops", {"ops": [again]}).status_code == 200
    check = hub.client.get(f"{_base('again')}/check", headers=hub.alice).json()
    assert [(v["rule"], v["nodes"]) for v in check["hard"]] == [("duplicate_key", ["pr:1", "pr:2"])]


@needs_db
def test_a_merged_delete_takes_away_the_node_s_full_text_rows(hub, tmp_path, caplog):
    from sqlalchemy import func

    from evo_agents.kg.search import PASSAGE_MARK

    _built(hub, tmp_path, caplog, doc("guide", "How the kiwi app works."))
    guide = _guide(hub)
    text = tables.kg_text_rows
    owned = select(text.c.row_key).where(
        text.c.project_id == project_id(hub), func.split_part(text.c.node_id, PASSAGE_MARK, 1) == guide
    )
    assert sql(hub.db, owned), "the guide has full-text rows on main"
    _merged(hub, "gone", [{"op": "node.delete", "id": guide}])
    assert sql(hub.db, owned) == []
    assert guide not in {r[0] for r in read_back(hub.db, project_id(hub)).nodes}

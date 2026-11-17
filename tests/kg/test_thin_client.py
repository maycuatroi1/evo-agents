"""The thin client of the knowledge graph. Its sync half: ``kg sync`` and its --build, --accept-removals and --due work
from what the hub holds of each source (GET .../sources/{s}/state) and keep no corpus on the machine
(``evo_agents.hub.kg_hub_sync``); ``kg build`` and ``kg init`` ask the hub, which alone builds. Its read half:
``kg query``, ``kg serve``, ``kg status``, ``kg profile``, ``kg settings`` and ``kg semantic`` ask the hub, and a corpus
left on the machine answers none of them."""

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.hub.kg_hub_sync import HubSync, sync_due
from evo_agents.kg.project import load_project_at
from tests.kg.conftest import FakeSource, doc

PROJECT = "thin"


def harness(tmp_path: Path, level: str = "internal", **source) -> Path:
    root = tmp_path / "thin-harness"
    root.mkdir()
    (root / "harness.yaml").write_text(f"name: {PROJECT}\nrepos: []\nknowledge_file: knowledge.yaml\n")
    spec = {
        "id": "docs",
        "connector": "python:tests.kg.fakes:run",
        "config": {"file": str(tmp_path / "fake-docs.json")},
        "label": {"level": level, "integrity": "U"},
        **source,
    }
    knowledge = {
        "version": 1,
        "project": PROJECT,
        "policy": {
            "levels": ["public", "internal", "customer"],
            "sinks": [
                {"id": "session", "kind": "agent-session", "clearance": {"level": "internal"}},
                {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
            ],
        },
        "sources": [spec],
    }
    (root / "knowledge.yaml").write_text(yaml.safe_dump(knowledge))
    return root


def sync(root: Path, capsys, *extra: str) -> tuple[int, dict]:
    code = main(["kg", "sync", "--project", str(root), "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def alive(hub) -> list[str]:
    return sorted(record.item_id for record in hub.corpus.items("docs"))


def test_a_sync_pushes_its_run_to_the_hub_and_keeps_no_corpus(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a"), doc("b")], cursor="c1")
    root = harness(tmp_path)

    code, body = sync(root, capsys)
    (run,) = body["runs"]
    assert code == 0 and run["status"] == "ok" and run["items"] == 2 and run["log"] is None
    assert list(hub.ingested) == [run["run_id"]] and alive(hub) == ["docs:doc:a", "docs:doc:b"]
    assert not (kg_env / PROJECT / "state.json").exists() and not (kg_env / PROJECT / "logs").exists()
    assert not list((kg_env / PROJECT).glob("spool-*"))  # the run's spool goes once it is pushed


def test_cursor_epoch_and_removals_come_from_the_hub_not_from_this_machine(tmp_path, kg_env, hub_for):
    hub = hub_for(PROJECT).hubs[PROJECT]
    fake = FakeSource(tmp_path / "fake-docs.json")
    fake.set(items=[doc("a"), doc("b"), doc("c")], cursor="c1")
    root = harness(tmp_path, deletion={"max_removal_ratio": 0.5})
    first = HubSync(hub, load_project_at(root, kg_env)).sync_source("docs")

    # Another machine, with a knowledge home of its own, takes up from what the hub holds.
    fake.set(items=[doc("a"), doc("b")])
    other = HubSync(hub, load_project_at(root, tmp_path / "other-home"))
    second = other.sync_source("docs")
    assert first.ok and second.ok and second.removals == 1 and second.tombstones == 0
    assert other.cursors["docs"] == "c1"  # the cursor the hub holds, as the connector got it
    assert alive(hub) == ["docs:doc:a", "docs:doc:b"]
    runs = [hub.ingested[first.run_id], hub.ingested[second.run_id]]
    assert runs[0]["lineage"] == runs[1]["lineage"] and runs[0]["epoch"] == runs[1]["epoch"]


def test_held_removals_are_held_on_the_hub_until_accept_removals(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    fake = FakeSource(tmp_path / "fake-docs.json")
    fake.set(items=[doc(key) for key in "abcd"])
    root = harness(tmp_path, deletion={"max_removal_ratio": 0.25, "min_scope": 1})
    assert sync(root, capsys)[0] == 0

    fake.set(items=[doc("a")])
    code, body = sync(root, capsys)
    (run,) = body["runs"]
    assert code == 1 and run["removals"] == 0 and run["held"]  # held: the sync fails until someone accepts them
    assert alive(hub) == [f"docs:doc:{key}" for key in "abcd"]
    (held,) = hub.state("docs")["held"]
    assert held["scope"] == "docs:" and sorted(held["ids"]) == ["docs:doc:b", "docs:doc:c", "docs:doc:d"]

    fake.set(items=[doc("a"), doc("b")])  # b is back by the time the owner accepts
    code, body = sync(root, capsys, "--accept-removals")
    assert code == 0 and body["runs"][0]["status"] == "ok"
    assert alive(hub) == ["docs:doc:a", "docs:doc:b"] and hub.state("docs")["held"] == []


def test_due_reads_the_last_ok_run_the_hub_holds_without_its_items(tmp_path, kg_env, hub_for):
    hub = hub_for(PROJECT).hubs[PROJECT]
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a")])
    syncer = HubSync(hub, load_project_at(harness(tmp_path, refresh="1h"), kg_env))

    first = sync_due(syncer, build=True)
    assert first.ok and first.due == ["docs"] and first.build.ok and hub.builds == 1
    hub.calls.clear()
    assert sync_due(syncer, build=True).due == []
    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)
    assert sync_due(syncer, now=later).due == ["docs"]
    assert hub.builds == 1
    asked = [path for _, path in hub.calls if path.startswith(f"/v1/kg/{PROJECT}/sources/docs/state")]
    assert asked[0].endswith("?items=false")  # only the run's dates: no item list to work out what is due


def test_build_asks_the_hub_for_a_build_and_waits_on_it(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a")])
    root = harness(tmp_path)
    code, body = sync(root, capsys, "--build")
    assert code == 0 and body["build"]["ok"] and body["build"]["build"]["status"] == "succeeded"
    assert hub.builds == 1 and ("GET", f"/v1/kg/{PROJECT}/builds/1") in hub.calls

    assert main(["kg", "sync", "--project", str(root), "--build"]) == 0
    assert "build 2 on the hub: succeeded" in capsys.readouterr().out


def test_kg_build_asks_the_hub_for_a_build_and_builds_nothing_here(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    root = harness(tmp_path)
    assert main(["kg", "build", "--project", str(root), "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["project"] == PROJECT and body["ok"] and body["build"]["status"] == "succeeded"
    assert hub.builds == 1 and ("GET", f"/v1/kg/{PROJECT}/builds/1") in hub.calls
    assert main(["kg", "build", "--project", str(root)]) == 0
    assert capsys.readouterr().out == "build 2 on the hub: succeeded\n"
    assert not list(kg_env.rglob("*.sqlite"))  # no graph, memo or corpus on this machine
    with pytest.raises(SystemExit) as refused:  # the local build's flags are gone with it
        main(["kg", "build", "--project", str(root), "--verify"])
    assert refused.value.code == 2


def test_kg_build_and_init_without_a_hub_login_say_so(tmp_path, kg_env, capsys):
    root = harness(tmp_path)
    assert main(["kg", "build", "--project", str(root)]) == 1
    assert "error: the hub: not signed in to a hub" in capsys.readouterr().err
    assert main(["kg", "init", "--project", str(root)]) == 1
    assert "error: the hub: not signed in to a hub" in capsys.readouterr().err


def test_kg_init_asks_the_hub_for_a_kg_init_run(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    root = harness(tmp_path)
    assert main(["kg", "init", "--project", str(root)]) == 0
    assert (
        capsys.readouterr().out == f"kg init run of project {PROJECT}: #1 cli queued: the Curator reads the profile\n"
    )
    assert main(["kg", "init", "--project", str(root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == 2
    assert hub.inits == 2 and hub.builds == 0 and not (root / "kg-init.json").exists()


def test_a_source_the_hub_sink_does_not_clear_is_not_synced(tmp_path, kg_env, hub_for, capsys):
    hub = hub_for(PROJECT).hubs[PROJECT]
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a")])
    root = harness(tmp_path, level="customer")
    code, body = sync(root, capsys)  # every source: it is left out, and said so
    assert code == 0 and body["runs"] == [] and body["left_out"] == ["docs"]
    assert main(["kg", "sync", "--project", str(root)]) == 0
    assert "SKIP docs: the hub sink does not clear its label" in capsys.readouterr().out

    code, body = sync(root, capsys, "--source", "docs")  # named: it fails
    (run,) = body["runs"]
    assert code == 1 and run["status"] == "failed" and "does not clear source docs" in run["issues"][0]
    assert hub.pushes() == 0 and hub.ingested == {}


def test_without_a_hub_login_kg_sync_says_so(tmp_path, kg_env, capsys):
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a")])
    assert main(["kg", "sync", "--project", str(harness(tmp_path))]) == 1
    assert "error: the hub: not signed in to a hub" in capsys.readouterr().err


def test_runs_no_build_read_yet_get_a_build_first_from_a_member_and_none_from_the_worker(tmp_path, kg_env, hub_for):
    from evo_agents.hub.kg_hub_sync import UnreadRuns
    from evo_agents.hub.kg_push import worker_routes

    hub = hub_for(PROJECT).hubs[PROJECT]
    FakeSource(tmp_path / "fake-docs.json").set(items=[doc("a")])
    project = load_project_at(harness(tmp_path), kg_env)
    hub.unread = 3
    worker = HubSync(hub, project, routes=worker_routes(PROJECT), send_config=False, builds=False)
    with pytest.raises(UnreadRuns, match="3 run"):
        worker.sync_source("docs")
    assert hub.builds == 0 and not hub.ingested  # the worker may not build, and reads nothing meanwhile

    result = HubSync(hub, project).sync_source("docs")
    assert result.ok and hub.builds == 1 and hub.unread == 0 and alive(hub) == ["docs:doc:a"]
    assert worker.sync_source("docs").ok  # once a build read them


# -- the read half: every read of the kg CLI asks the hub --------------------------------------------------------------


def hub_graph(tmp_path, kg_env, monkeypatch):
    """The demo project with its graph on the hub (synced under a home of the hub's own and built in memory), and
    this machine's home empty."""
    from tests.kg.built import sync_build
    from tests.kg.hub_graph import sign_in
    from tests.kg.projects import make_project

    on_hub = make_project(tmp_path, tmp_path / "hub-home")
    sync_build(on_hub)
    return str(on_hub.harness.root), sign_in(monkeypatch, on_hub)


def machine_files(kg_env: Path) -> list[str]:
    return sorted(p.name for p in kg_env.rglob("*.sqlite")) if kg_env.exists() else []


def test_the_reads_of_the_kg_cli_ask_the_hub_and_open_nothing_here(tmp_path, kg_env, monkeypatch, capsys):
    root, hub = hub_graph(tmp_path, kg_env, monkeypatch)

    assert main(["kg", "query", "kg_search", "rank_results", "--project", root, "--sink", "agent"]) == 0
    assert "app:app/search.py::rank_results" in capsys.readouterr().out
    assert main(["kg", "status", "--project", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["graph"]["ready"] is True
    assert main(["kg", "status", "--project", root, "--brief"]) == 0
    assert capsys.readouterr().out.startswith("evo-kg: project demo, snapshot")
    assert main(["kg", "profile", "--project", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["profile"]["items"] > 0
    assert main(["kg", "settings", "--project", root, "--set", "semantic_weight=1.5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["overrides"] == {"semantic_weight": 1.5}
    assert main(["kg", "semantic", "--project", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "demo"

    hello = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "kg_status", "arguments": {}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(hello) + "\n"))
    assert main(["kg", "serve", "--project", root, "--sink", "agent"]) == 0
    answer = json.loads(capsys.readouterr().out)["result"]
    assert not answer.get("isError") and "structuredContent" not in answer  # the model reads the text alone

    assert hub.asked == [
        ("MCP", "kg_search"),
        ("POST", "/v1/kg/demo/tools/kg_status"),
        ("POST", "/v1/kg/demo/tools/kg_status"),
        ("POST", "/v1/kg/demo/tools/kg_status"),
        ("PUT", "/v1/kg/demo/settings"),
        ("POST", "/v1/kg/demo/tools/kg_status"),
        ("MCP", "kg_status"),
    ]
    assert machine_files(kg_env) == []  # no corpus, no graph, no vectors on this machine


def test_a_corpus_left_on_this_machine_answers_no_read(tmp_path, kg_env, monkeypatch, capsys):
    from evo_agents.kg.sync import sync_project
    from tests.kg.projects import make_project

    left = make_project(tmp_path, kg_env)  # a corpus synced here from before graphs were built on the hub only
    assert all(r.ok for r in sync_project(left))
    root = str(left.harness.root)
    monkeypatch.setattr("evo_agents.hub.mcp_proxy.session_credentials", lambda: (_ for _ in ()).throw(not_signed_in()))
    monkeypatch.setattr("evo_agents.hub.client.load_credentials", lambda: (_ for _ in ()).throw(not_signed_in()))
    monkeypatch.setattr("evo_agents.hub.cli_client.load_credentials", lambda: (_ for _ in ()).throw(not_signed_in()))
    for backend in ("auto", "hub"):
        assert main(["kg", "query", "kg_search", "rank_results", "--project", root, "--backend", backend]) == 1
        assert capsys.readouterr().out == "error: the hub: not signed in\n"
    for command in (["status"], ["profile"], ["settings"], ["semantic"]):
        assert main(["kg", *command, "--project", root]) != 0
        out, err = capsys.readouterr()
        assert out == "" and "not signed in" in err


def not_signed_in():
    from evo_agents.hub.client import HubError

    return HubError("not signed in")

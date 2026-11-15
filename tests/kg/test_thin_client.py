"""The thin client of the knowledge graph, its sync half: ``kg sync`` and its --build, --accept-removals and --due work
from what the hub holds of each source (GET .../sources/{s}/state) and keep no corpus on the machine
(``evo_agents.hub.kg_hub_sync``)."""

import datetime as dt
import json
from pathlib import Path

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
    assert not (kg_env / PROJECT / "corpus.sqlite").exists() and not (kg_env / PROJECT / "logs").exists()
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

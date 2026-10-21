"""The checks of knowledge.yaml a judge run runs on the worker (``evo_agents.worker.runner.judge_knowledge``), step 8 of
plan history-driven-curator: for a change of kind kg_source, ``evo-agents harness validate`` in the worktree of the
commit judged, then a trial sync of each source the change edits, from that commit's knowledge.yaml into a corpus of
the run's own; a git source the worker's sync of the graph would skip fails its check, and nothing is pushed. The daemon
runs in this process with the fake runtime, the in-memory hub and the checkouts alpha and beta of
``tests.worker.test_plan_runs``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import knowledge_diff
from tests.worker.test_curator_runs import change_branch, curator, inputs
from tests.worker.test_plan_runs import PROJECT, machine, with_daemon  # noqa: F401 (machine is a fixture)

HARNESS = {
    "name": PROJECT,
    "knowledge_file": "knowledge.yaml",
    "repos": [{"name": "alpha", "path": "~/nowhere/alpha"}, {"name": "beta", "path": "~/nowhere/beta"}],
}
KNOWLEDGE = {
    "version": 1,
    "project": PROJECT,
    "sources": [
        {"id": "notes", "connector": "harness", "include": ["*.md"]},
        {"id": "beta-code", "connector": "git", "repo": "beta", "ref": "origin/main", "fetch": True},
        {"id": "beta-local", "connector": "git", "repo": "beta", "ref": "main"},
        {"id": "wiki", "connector": "exec", "command": ["sh", "-c", "touch {marker}"]},
    ],
}


def judged(machine, sources: list[str], knowledge: dict | None = None) -> tuple[dict, list[str], object]:  # noqa: F811
    """A judge run of a change of alpha that writes harness.yaml and knowledge.yaml, whose inputs name ``sources``;
    (the verdict it posted, the checks' names, the hub)."""
    data = json.loads(json.dumps(knowledge or KNOWLEDGE).replace("{marker}", str(machine.tmp / "exec-ran")))
    files = {"harness.yaml": yaml.safe_dump(HARNESS), "knowledge.yaml": yaml.safe_dump(data), "notes.md": "# Notes\n"}
    head = change_branch(machine, files)
    machine.env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{machine.env.get('PATH', '')}"  # evo-agents
    machine.scenarios({"judge": [{"say": json.dumps({"verdict": "pass", "reasons": "The checks passed."})}]})
    checks = knowledge_diff.judge_checks(sources)
    asked = {
        **inputs(machine, head),
        "verify": [],
        "hidden_checks": [],
        "knowledge": {"files": ["knowledge.yaml"], "sources": sources, "checks": checks},
    }
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_judge_run("alpha", curator(head), asked)
        assert await hub.wait_state(run_id, "done", "failed", timeout=120) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    (sent,) = found["hub"].verdicts
    return sent, checks, found


def test_judge_run_of_a_kg_source_change_validates_the_harness_and_syncs_each_source_on_trial(machine):  # noqa: F811
    beta = machine.origin_rev("beta", "refs/heads/main")
    sent, checks, found = judged(machine, ["notes", "beta-code", "beta-local", "wiki"])
    hub, run_id = found["hub"], found["run"]
    assert [(item["command"], item["exit_code"]) for item in sent["verify"]] == [
        (checks[0], 0),  # harness validate
        (checks[1], 0),  # the harness source, over the worktree
        (checks[2], 0),  # a git source as the worker's sync reads it
        (checks[3], 1),  # a ref the worker's sync would skip
        (checks[4], 1),  # an exec command never runs
    ]
    events = [event["body"] for event in hub.runs[run_id]["events"] if event["kind"] == "system"]
    texts = "\n".join(str(body.get("output") or "") for body in events if body.get("command") in checks)
    assert "the worker's sync of the graph would skip source beta-local" in texts
    assert "a Judge syncs git and harness sources alone" in texts
    assert not (machine.tmp / "exec-ran").exists()
    assert "source beta-code synced on trial" in texts and "source notes synced on trial" in texts
    assert machine.origin_rev("beta", "refs/heads/main") == beta  # nothing pushed
    trial = machine.directory(run_id) / "kg-trial"
    assert (trial / "harness" / "knowledge.yaml").read_bytes() == yaml.safe_dump(
        json.loads(json.dumps(KNOWLEDGE).replace("{marker}", str(machine.tmp / "exec-ran")))
    ).encode()  # byte for byte from the commit judged
    view = yaml.safe_load((trial / "harness" / "harness.yaml").read_text(encoding="utf-8"))
    assert {repo["name"]: repo["path"] for repo in view["repos"]} == {
        name: str(path) for name, path in machine.checkouts.items()
    }  # each repo's path is the worker's checkout of it


def test_judge_run_whose_branch_has_an_invalid_knowledge_yaml_fails_its_checks(machine):  # noqa: F811
    broken = {**KNOWLEDGE, "sources": "not a list"}
    sent, checks, _ = judged(machine, ["beta-code"], knowledge=broken)
    codes = {item["command"]: item["exit_code"] for item in sent["verify"]}
    assert codes[checks[0]] != 0 and codes[checks[1]] == 1

"""The evolve routes of the Curator on the hub (plan pareto-curator-evolution, step 2, criterion a2): an owner starts
an evolution of a harness file of a repo, its run's claim hands the daemon a key of its own, whose SHA-256 alone the
hub keeps, and the worker reads its inputs and posts its baseline arms, its candidates and its end with that key
alone. Its inputs hold light tasks of the select split and active probes, never a holdout task; a baseline arm's
result is cached for the next evolution; each candidate recomputes the Pareto front; a git evolution that ends done
moves its proposal's planned change on as a Builder's would; the MCP tool curator_evolve shows neither holdout nor
verify; the charter's evolve block goes out and back whole. Every name here is made up.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select

from evo_agents.hub import evolve, replay, tables
from evo_agents.hub.mcp_tools import RUN_TOOLS, TOOLS
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import NIGHT, PROJECT, charter_path, fire, put_charter, shown, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    accepted,
    curator_charter,
    one_change,
    path,
    world,
)
from tests.hub.test_review_runs import MCP_HEADERS
from tests.hub.test_runs import claim, moved

HEAD = "6" * 40
EVOLVE = {
    "enabled": True,
    "evolvable_paths": ["evo-agents:AGENTS.md", "evo-agents:**/skills/**"],
    "budget": {"iterations": 3, "max_candidates": 6, "max_cost_usd": 0.3},
}
SELECT_VERIFY = "python -m pytest -q tests/test_select_{n}.py"
HOLDOUT_VERIFY = "python -m pytest -q tests/test_kept_back.py"
PROBE_VERIFY = "python -m pytest -q tests/test_probe_wait.py"


def enabled(world, **changes) -> None:
    written(world.client, world.headers["owner"], curator_charter(evolve={**EVOLVE, **changes}))


def project_row(world):
    p = tables.projects
    return sql(world.db, select(p.c.id, p.c.default_label).where(p.c.name == PROJECT))[0]


def suite(world, proposal_id: int | None = None) -> dict:
    """Two replay tasks of the select split, one of the holdout split, and an active probe of ``proposal_id``; their
    refs by name."""
    project_id, label = project_row(world)
    t, found = tables.replay_tasks, {}
    for n, split in ((1, "select"), (2, "select"), (3, "holdout")):
        verify = HOLDOUT_VERIFY if split == "holdout" else SELECT_VERIFY.format(n=n)
        row = {"project_id": project_id, "plan_id": "plan-past", "step_key": str(n), "repo": "evo-agents"}
        row |= {"title": f"Step {n}", "what": f"Do step {n} again.", "verify": verify, "commits": [f"{n}" * 40]}
        row |= {"source": "evidence", "split": split, "label": label or {}}
        task_id = sql(world.db, insert(t).values(row).returning(t.c.id))[0][0]
        found[split if split == "holdout" else f"select{n}"] = evolve.task_ref("replay", task_id)
    if proposal_id is not None:
        p = tables.curator_probes
        row = {"project_id": project_id, "proposal_id": proposal_id, "repo": "evo-agents", "source": "admin"}
        row |= {"task": "Wait for the fake build to finish.", "verify": PROBE_VERIFY, "label": label or {}}
        probe_id = sql(world.db, insert(p).values(row).returning(p.c.id))[0][0]
        found["probe"] = evolve.task_ref("probe", probe_id)
    return found


def started(world, **body):
    sent = {"target": "git", "repo": "evo-agents", "paths": ["AGENTS.md"], **body}
    return world.client.post(path("evolve"), json=sent, headers=world.headers["owner"])


def evolve_run(world) -> dict:
    """The evolve run, claimed and running; its spec."""
    spec = claim(world.client, world.worker)
    assert spec is not None and spec["kind"] == "evolve", spec
    moved(world.client, world.worker, spec["id"], "running")
    return spec


def keyed(world, key: str | None) -> dict:
    return {**world.worker["headers"], **({evolve.EVOLVE_KEY_HEADER: key} if key is not None else {})}


def inputs(world, spec: dict) -> dict:
    found = world.client.get(
        f"/v1/worker/runs/{spec['id']}/evolve", headers=keyed(world, spec["curator"]["evolve_key"])
    )
    assert found.status_code == 200, found.text
    return found.json()


def posted(world, spec: dict, what: str, body: dict, *, status: int = 200) -> dict:
    url = f"/v1/worker/runs/{spec['id']}/evolve/{what}"
    found = world.client.post(url, json=body, headers=keyed(world, spec["curator"]["evolve_key"]))
    assert found.status_code == status, found.text
    return found.json()


def candidate(scores: dict, text: str, *, origin: str = "mutate", parents=(), iteration: int = 1) -> dict:
    return {
        "parents": list(parents),
        "origin": origin,
        "iteration": iteration,
        "components": {"AGENTS.md": text},
        "scores": scores,
        "cost_usd": 0.02,
        "turns": 3,
        "traces": {},
    }


def evolution(world, evolve_id: int) -> SimpleNamespace:
    e = tables.curator_evolve_runs
    (row,) = sql(world.db, select(e).where(e.c.id == evolve_id))
    return SimpleNamespace(**dict(zip(e.c.keys(), row, strict=True)))


def test_evolve_the_owner_starts_an_evolution_and_its_claim_hands_a_key_the_hub_keeps_hashed(world):
    enabled(world)
    made = started(world)
    assert made.status_code == 201, made.text
    found = made.json()
    assert (found["target"], found["repo"], found["paths"], found["state"]) == (
        "git",
        "evo-agents",
        ["AGENTS.md"],
        "running",
    ), found["reason"]
    assert found["run_id"] and found["budget"]["iterations"] == 3
    spec = evolve_run(world)
    assert spec["id"] == found["run_id"] and spec["plan_id"] == ""
    sent = spec["curator"]
    assert sent["role"] == "evolve" and sent["evolve"] == {"evolve_id": found["id"], "target": "git"}
    key = sent["evolve_key"]
    assert key and evolution(world, found["id"]).evolve_key == hashlib.sha256(key.encode()).hexdigest() != key
    assert spec["budget"]["max_usd"] <= 0.3  # the charter's evolve.budget.max_cost_usd caps the run
    assert started(world).status_code == 201  # a second waits: one evolve run of a project is active at a time
    listed = world.client.get(path("evolve"), headers=world.headers["owner"]).json()["evolutions"]
    assert [item["state"] for item in listed] == ["queued", "running"], listed
    waiting = listed[0]
    assert "another evolve run of the project is active" in waiting["reason"]


def test_evolve_a_path_the_charter_does_not_name_and_a_charter_without_evolve_start_nothing(world):
    refused = started(world)
    assert refused.status_code == 409 and "does not enable evolve" in refused.text
    enabled(world)
    for paths in (["src/app.py"], ["docs/AGENTS.md"]):  # no harness file, a harness file no glob names
        refused = started(world, paths=paths)
        assert refused.status_code == 422 and "evolvable_paths" in refused.text, refused.text
    assert started(world, repo="no-such-repo").status_code == 422
    assert started(world, system_id=1).status_code == 422  # target git takes repo and paths alone


def test_evolve_a_wrong_or_missing_key_is_refused(world):
    enabled(world)
    started(world)
    spec = evolve_run(world)
    url = f"/v1/worker/runs/{spec['id']}/evolve"
    for key in (None, "not-the-key"):
        refused = world.client.get(url, headers=keyed(world, key))
        assert refused.status_code == 403 and evolve.EVOLVE_KEY_HEADER in refused.text
        refused = world.client.post(url + "/candidate", json=candidate({}, "x"), headers=keyed(world, key))
        assert refused.status_code == 403
    assert world.client.get(url, headers=keyed(world, spec["curator"]["evolve_key"])).status_code == 200


def test_evolve_inputs_hold_select_tasks_and_probes_with_their_verify_and_never_a_holdout_task(world):
    proposal = accepted(world)
    refs = suite(world, proposal["id"])
    enabled(world)
    started(world)
    found = inputs(world, evolve_run(world))
    assert {task["ref"] for task in found["tasks"]} == {refs["select1"], refs["select2"], refs["probe"]}
    assert all(task["kind"] in evolve.TASK_KINDS and task["verify"] for task in found["tasks"])
    verify = {task["ref"]: task["verify"] for task in found["tasks"]}
    assert verify[refs["select1"]] == [SELECT_VERIFY.format(n=1)] and verify[refs["probe"]] == [PROBE_VERIFY]
    assert HOLDOUT_VERIFY not in str(found) and refs["holdout"] not in str(found)
    assert found["baseline"] == {} and found["branch"].startswith("curator/evolve-")
    assert found["budget"] == EVOLVE["budget"] and found["evolvable_paths"] == EVOLVE["evolvable_paths"]
    assert found["runtime"] and found["cached"] == {} and found["paths"] == ["AGENTS.md"]


def test_evolve_a_baseline_arm_is_cached_and_the_next_evolution_reads_it(world):
    refs = suite(world)
    enabled(world)
    first = started(world).json()
    spec = evolve_run(world)
    task = next(item for item in inputs(world, spec)["tasks"] if item["ref"] == refs["select1"])
    digest = evolve.baseline_digest({"AGENTS.md": "Be brief."})
    body = {"task_kind": "replay", "task_id": task["id"], "base_sha": None, "baseline_digest": digest}
    body |= {"runtime": spec["runtime"], "model": spec["model"]}
    body |= {"result": {"score": 1.0, "cost_usd": 0.01, "turns": 2, "trace": {}}}
    made = posted(world, spec, "arm", body)
    key = evolve.arm_key("replay", task["id"], None, digest, spec["runtime"], spec["model"])
    assert made == {"key": key, "cached": False}
    assert posted(world, spec, "arm", {**body, "result": {"score": 0.0}}) == {"key": key, "cached": True}
    holdout_id = int(refs["holdout"].split(":")[1])
    posted(world, spec, "arm", {**body, "task_id": holdout_id}, status=422)  # no task of the evolution
    posted(world, spec, "report", {"state": "failed", "reason": "the test ends it"})
    assert evolution(world, first["id"]).state == "failed"
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    started(world)
    found = inputs(world, evolve_run(world))
    assert found["cached"] == {key: body["result"]}  # the first result stays


def test_evolve_each_candidate_recomputes_the_pareto_front(world):
    refs = suite(world)
    one, two = refs["select1"], refs["select2"]
    enabled(world)
    evolve_id = started(world).json()["id"]
    spec = evolve_run(world)
    seed = posted(world, spec, "candidate", candidate({one: 0.5, two: 0.5}, "seed", origin="seed", iteration=0))
    assert seed["frontier"] == [{"candidate_id": seed["candidate_id"], "freq": 2}]
    again = posted(world, spec, "candidate", candidate({one: 0.5, two: 0.5}, "seed", origin="seed", iteration=0))
    assert again["candidate_id"] == seed["candidate_id"]  # the same components are the same candidate
    left = posted(world, spec, "candidate", candidate({one: 1.0, two: 0.0}, "left", parents=[seed["candidate_id"]]))
    assert {item["candidate_id"] for item in left["frontier"]} == {seed["candidate_id"], left["candidate_id"]}
    best = posted(world, spec, "candidate", candidate({one: 1.0, two: 1.0}, "both", iteration=2))
    assert best["frontier"] == [{"candidate_id": best["candidate_id"], "freq": 2}]
    ec = tables.curator_evolve_candidates
    fronts = dict(sql(world.db, select(ec.c.id, ec.c.on_front).where(ec.c.evolve_id == evolve_id)))
    assert fronts == {seed["candidate_id"]: False, left["candidate_id"]: False, best["candidate_id"]: True}
    row = evolution(world, evolve_id)
    assert row.iterations == 2 and row.frontier == best["frontier"]
    posted(world, spec, "candidate", candidate({refs["holdout"]: 1.0}, "peek"), status=422)  # no holdout score
    posted(world, spec, "candidate", candidate({}, "orphan", parents=[10**9]), status=422)


def test_evolve_each_candidate_keeps_the_asi_of_each_task_bounded_and_show_returns_it(world):
    refs = suite(world)
    one = refs["select1"]
    enabled(world)
    evolve_id = started(world).json()["id"]
    spec = evolve_run(world)
    tail = "x" * 9000 + "\nfatal: cannot change to '/home/lumen/github/evo-agents'\n"
    asi = {
        "score": 0.0,
        "checks": [{"exit_code": 128, "output_tail": tail}],
        "failed_tools": [f"Bash: no such file {n}" for n in range(40)],
        "agent_error": None,
        "summary": "I could not find the repo.",
    }
    body = candidate({one: 0.0}, "seed", origin="seed", iteration=0)
    body["traces"] = {one: asi, refs["holdout"]: {"score": 1.0}, "replay:0": "not an object"}
    posted(world, spec, "candidate", body)
    shown_one = world.client.get(path("evolve", str(evolve_id)), headers=world.headers["owner"])
    assert shown_one.status_code == 200, shown_one.text
    (item,) = shown_one.json()["candidates"]
    assert list(item["asi"]) == [one]  # a task the candidate was not measured on is left out
    kept = item["asi"][one]
    assert kept["checks"][0]["exit_code"] == 128 and kept["summary"] == "I could not find the repo."
    assert len(kept["checks"][0]["output_tail"]) == evolve.ASI_TEXT
    assert kept["checks"][0]["output_tail"].endswith("fatal: cannot change to '/home/lumen/github/evo-agents'\n")
    assert kept["failed_tools"] == asi["failed_tools"][-evolve.ASI_ITEMS :]
    assert item["components"] == {"AGENTS.md": "seed"}  # the candidate itself is never cut


def test_evolve_a_git_evolution_done_moves_its_proposals_planned_change_on(world):
    proposal = accepted(world)
    change = one_change(world)
    assert change["state"] == "planned"
    refs = suite(world)
    enabled(world)
    evolve_id = started(world, proposal_id=proposal["id"]).json()["id"]
    spec = evolve_run(world)
    found = inputs(world, spec)
    assert found["branch"] == change["branch"]
    best = posted(world, spec, "candidate", candidate({refs["select1"]: 1.0}, "better"))
    sent = {"state": "done", "best_candidate_id": best["candidate_id"], "head_sha": HEAD, "branch": change["branch"]}
    ended = posted(world, spec, "report", sent)
    assert ended["state"] == "done" and ended["change_id"] == change["id"] and ended["best_candidate_id"]
    c = tables.curator_changes
    ((state, builder_run_id, head_sha),) = sql(
        world.db, select(c.c.state, c.c.builder_run_id, c.c.head_sha).where(c.c.id == change["id"])
    )
    assert (state, builder_run_id, head_sha) == ("pr_pending", spec["id"], HEAD)
    row = evolution(world, evolve_id)
    assert row.change_id == change["id"] and row.evolve_key is None and "the hub opens its pull request" in row.reason
    posted(world, spec, "report", sent, status=409)  # an evolution ends once


def test_evolve_a_git_evolution_without_a_proposal_says_why_its_branch_waits(world):
    refs = suite(world)
    enabled(world)
    evolve_id = started(world).json()["id"]
    spec = evolve_run(world)
    best = posted(world, spec, "candidate", candidate({refs["select1"]: 1.0}, "better"))
    sent = {"state": "done", "best_candidate_id": best["candidate_id"], "head_sha": HEAD, "branch": "curator/x"}
    posted(world, spec, "report", sent)
    row = evolution(world, evolve_id)
    assert row.state == "done" and row.change_id is None and "names no accepted proposal" in row.reason


def test_evolve_the_mcp_tool_shows_select_tasks_candidates_and_front_never_holdout_nor_verify(world):
    proposal = accepted(world)
    refs = suite(world, proposal["id"])
    enabled(world)
    evolve_id = started(world).json()["id"]
    spec = evolve_run(world)
    posted(world, spec, "candidate", candidate({refs["select1"]: 1.0, refs["probe"]: 0.0}, "Be brief."))

    def tool(arguments: dict) -> dict:
        message = {"jsonrpc": "2.0", "id": 9, "method": "tools/call"}
        message["params"] = {"name": "curator_evolve", "arguments": arguments}
        headers = {**world.headers["owner"], "X-Evo-Project": PROJECT, **MCP_HEADERS}
        response = world.client.post("/mcp", json=message, headers=headers)
        assert response.status_code == 200, response.text
        found = response.json()["result"]
        assert not found.get("isError"), found
        return found

    shown_one = tool({"evolve_id": evolve_id})
    text = shown_one["content"][0]["text"]
    assert refs["select1"] in text and refs["probe"] in text and "Front: #" in text
    everything = str(shown_one)
    for hidden in (refs["holdout"], HOLDOUT_VERIFY, PROBE_VERIFY, SELECT_VERIFY.format(n=1), "Be brief."):
        assert hidden not in everything
    listed = tool({})
    assert f"#{evolve_id} git of evo-agents AGENTS.md (running)" in listed["content"][0]["text"]
    assert RUN_TOOLS["evolve"] == frozenset() and "curator_evolve" in [item["name"] for item in TOOLS]


def test_evolve_the_charter_block_goes_out_and_back_whole_and_is_checked(world):
    owner = world.headers["owner"]
    assert shown(world.client, owner)["evolve"] == {
        "enabled": False,
        "evolvable_paths": [],
        "budget": {"iterations": 6, "max_candidates": 12, "max_cost_usd": None},
        "tasks": 8,
        "probes": 4,
        "exposure_limit": None,
    }
    enabled(world, tasks=5, probes=2, exposure_limit=30)
    found = shown(world.client, owner)["evolve"]
    assert found == {**EVOLVE, "tasks": 5, "probes": 2, "exposure_limit": 30}
    assert (
        written(world.client, owner, shown(world.client, owner))["revision"] == shown(world.client, owner)["revision"]
    )
    for wrong in ({"tasks": replay.MAX_TASKS + 1}, {"exposure_limit": -1}, {"budget": {"iterations": 0}}, {"other": 1}):
        refused = put_charter(world.client, owner, curator_charter(evolve={**EVOLVE, **wrong}))
        assert refused.status_code == 422, (wrong, refused.text)


def test_evolve_an_evolution_waits_while_the_night_shift_is_paused_and_the_night_queues_its_run(world):
    enabled(world)
    owner = world.headers["owner"]
    assert world.client.post(charter_path("pause"), headers=owner).status_code == 200
    found = started(world).json()
    assert found["state"] == "queued" and found["run_id"] is None and "paused" in found["reason"]
    assert world.client.post(charter_path("resume"), headers=owner).status_code == 200
    words = []
    for minutes in range(6):  # the night's other runs go first: each is cancelled for the next turn
        words.append(next(iter(fire(world.client, NIGHT + timedelta(minutes=minutes))), None))
        if words[-1] == "evolve":
            break
        r = tables.runs
        for (run_id,) in sql(world.db, select(r.c.id).where(r.c.state == "queued")):
            assert world.client.post(f"/v1/projects/{PROJECT}/runs/{run_id}/cancel", headers=owner).status_code == 200
    assert words[-1] == "evolve", words
    row = evolution(world, found["id"])
    assert row.state == "running" and row.run_id is not None and row.reason is None

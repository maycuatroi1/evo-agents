"""The Curator's replays on the hub (``evo_agents.hub.replay_suite``, ``evo_agents.hub.server.replays``) and schema
0053, in the world of ``tests.hub.test_curator_changes``: the project evo-agents on the fake GitHub with both Apps,
its owner an admin with a charter whose worker on duty runs every kind of run.

The checks step 10 of the history-driven-curator plan names on the hub: a9 (the suite made from the plans' steps done
with a verify and commits, its split fixed and the suite only growing; the night shift's replay runs of a change of a
skill or an AGENTS.md on holdout tasks, with the Builder's runtime and model, within the charter's replay budget and
tasks per change; each arm's result written with the run's own key alone, kept with the change and read through the
API, the command line and MCP; a run that ends without both arms replayed again, then failed; a task refused as
unusable never replayed again; the Judge reading none of it) and a13 (the commands and keys added only)."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, select, update

from evo_agents.hub import replay, tables
from evo_agents.hub.mcp_tools import JUDGE_TOOLS, REPLAY_TOOLS, TOOLS, offered
from evo_agents.hub.replay_suite import build_suites
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_credentials_api import app_key  # noqa: F401 (a fixture)
from tests.hub.test_curator import NIGHT, PROJECT, fire, put_charter, written
from tests.hub.test_curator_changes import (  # noqa: F401 (world is a fixture)
    accepted,
    builder,
    curator_charter,
    judge_run,
    moved_on,
    one_change,
    review_run,
    world,
)
from tests.hub.test_migrate import move_to
from tests.hub.test_review_runs import MCP_HEADERS
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import claim, moved

HISTORY = "history"
REPLAY = {"tasks": 2, "budget_usd": 1.0}
AGENTS = [{"repo": "evo-agents", "path": "AGENTS.md"}]
BASE = "a" * 40


def sha7(key: int) -> str:
    return f"{key:03d}abcdef"[:9]


def history_plan(steps: int = 12, whats: dict | None = None) -> dict:
    return {
        "id": HISTORY,
        "title": "What the project did",
        "goal": "Work the replays are made of.",
        "repos": [{"repo": "evo-agents", "branch": "main"}],
        "steps": [
            {
                "id": key,
                "title": f"Step {key}",
                "what": (whats or {}).get(key, f"Do step {key} of the history."),
                "status": "done",
                "verify": f"python -m pytest -q tests/test_{key}.py",
                "evidence": f"Commits: {sha7(key)}",
            }
            for key in range(1, steps + 1)
        ]
        + [{"id": 99, "title": "Not done", "what": "Later.", "status": "pending", "verify": "true"}],
    }


def put_history(world, body: dict, revision: int | None = None) -> None:
    sent = {"body": body, **({"if_revision": revision} if revision else {})}
    response = world.client.put(f"/v1/projects/{PROJECT}/plans/{HISTORY}", json=sent, headers=owner(world))
    assert response.status_code == 200, response.text


def owner(world) -> dict:
    return world.headers["owner"]


def built(world) -> dict:
    return world.client.portal.call(partial(build_suites, world.app.state.engine))


def holdout(steps: int = 12) -> list[str]:
    return [str(key) for key in range(1, steps + 1) if replay.split_of(HISTORY, str(key)) == "holdout"]


def named(world, statement) -> list[SimpleNamespace]:
    """The rows of ``statement``, each with its columns by name."""
    keys = [column.name for column in statement.selected_columns]
    return [SimpleNamespace(**dict(zip(keys, row, strict=True))) for row in sql(world.db, statement)]


def suite(world) -> list:
    t = tables.replay_tasks
    return named(world, select(t.c.id, t.c.step_key, t.c.split, t.c.what, t.c.base_sha, t.c.unusable).order_by(t.c.id))


def path(*parts: str) -> str:
    return "/".join((f"/v1/projects/{PROJECT}/curator", *parts))


def replays(world) -> list:
    rp = tables.curator_replays
    return named(world, select(rp.c.id, rp.c.task_id, rp.c.state, rp.c.attempts, rp.c.run_id).order_by(rp.c.id))


def report(world, run_id: int, key: str | None, **body):
    headers = {**world.worker["headers"], **({replay.REPLAY_KEY_HEADER: key} if key else {})}
    return world.client.post(f"/v1/worker/runs/{run_id}/replay", json=body, headers=headers)


def arm(passed: bool, **extra) -> dict:
    return {
        "passed": passed,
        "harness_sha": "b" * 40,
        "harness_paths": ["AGENTS.md"],
        "verify": [{"exit_code": 0 if passed else 1, "duration_ms": 1200}],
        "cost_usd": 0.12,
        "turns": 7,
        "tool_calls": 9,
        "tool_errors": 1,
        "peak_context_tokens": 40000,
        "duration_ms": 61000,
        "session_id": "s-1",
        **extra,
    }


def replay_world(world, *, charter_changes=None, steps: int = 12) -> dict:
    """The history plan's suite built, a charter that replays, and a change of AGENTS.md built; the change."""
    written(world.client, owner(world), curator_charter(replay=REPLAY, **(charter_changes or {})))
    put_history(world, history_plan(steps))
    assert built(world)["added"] == steps
    accepted(world, kind="docs", paths=AGENTS)
    builder(world)
    return one_change(world)


def replay_run(world) -> dict:
    """The replay run the night shift queues, claimed; its spec."""
    assert fire(world.client, NIGHT).get("replay") == 1
    spec = claim(world.client, world.worker)
    assert spec["kind"] == "replay"
    return spec


# The suite


def test_replay_suite_adds_each_done_step_once_with_its_fixed_split_and_only_grows(world):
    written(world.client, owner(world), curator_charter())
    put_history(world, history_plan(6))
    assert built(world) == {"projects": 1, "added": 6, "failed": 0}
    first = suite(world)
    assert [row.step_key for row in first] == ["1", "2", "3", "4", "5", "6"]  # the pending step gives none
    assert [row.split for row in first] == [replay.split_of(HISTORY, row.step_key) for row in first]
    assert built(world)["added"] == 0
    put_history(world, history_plan(8, {2: "The step written again."}), revision=1)
    assert built(world)["added"] == 2
    again = suite(world)
    assert again[:6] == first  # a task once in the suite stays as it was
    assert [row.step_key for row in again[6:]] == ["7", "8"]
    t = tables.replay_tasks
    (task,) = named(world, select(t.c.commits, t.c.source, t.c.repo, t.c.verify, t.c.label).where(t.c.step_key == "1"))
    assert (task.commits, task.source, task.repo) == ([sha7(1)], "evidence", "evo-agents")
    assert task.verify == "python -m pytest -q tests/test_1.py" and isinstance(task.label, dict)


def test_replay_suite_is_read_through_the_api_and_the_command_line(world, tmp_path, monkeypatch, capsys):
    written(world.client, owner(world), curator_charter())
    put_history(world, history_plan(9))
    built(world)
    listed = world.client.get(path("replay", "tasks"), headers=owner(world))
    assert listed.status_code == 200, listed.text
    found = listed.json()
    held = holdout(9)
    assert found["total"] == 9 and found["counts"] == {"select": 9 - len(held), "holdout": len(held)}
    only = world.client.get(path("replay", "tasks"), params={"split": "holdout"}, headers=owner(world)).json()
    assert [task["step_key"] for task in only["tasks"]] == held
    assert world.client.get(path("replay", "tasks"), headers=world.worker["headers"]).status_code == 403
    with serving(create_app(world.app.state.config)) as url:
        token = owner(world)["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)
        shown = as_json(
            cli(monkeypatch, capsys, home, "hub", "curator", "replay", "tasks", "--json", "--project", PROJECT)
        )
        assert_json_keys("hub curator replay tasks", shown)
        out = ok(cli(monkeypatch, capsys, home, "hub", "curator", "replay", "tasks", "--project", PROJECT)).out
        assert f"9 of 9 replay task(s) of {PROJECT}" in out and "history#1" in out


# The night shift's replay runs


def test_replay_the_night_shift_replays_a_change_of_agents_md_on_holdout_tasks_as_the_builder(world):
    change = replay_world(world, charter_changes={"builder": {"runtime": "claude-code", "model": "opus"}})
    assert change["state"] == "pr_pending"
    spec = replay_run(world)
    held = holdout()
    task = spec["curator"]["replay"]["task"]
    assert spec["curator"]["role"] == "replay" and spec["curator"]["replay"]["repo"] == "evo-agents"
    assert task["step_key"] == held[0] and task["verify"] == [f"python -m pytest -q tests/test_{held[0]}.py"]
    assert task["commits"] == [sha7(int(held[0]))] and task["base_sha"] is None
    assert spec["curator"]["replay_key"] and spec["curator"]["branch"] == change["branch"]
    assert (spec["runtime"], spec["model"], spec["plan_id"]) == ("claude-code", "opus", change["plan_id"])
    assert spec["budget"]["max_usd"] == 0.5  # the charter's run_budget_usd, under replay.budget_usd
    assert f"Do step {held[0]} of the history." in spec["prompt"]
    (target,) = sql(world.db, select(tables.audit.c.target).where(tables.audit.c.action == "curator.replay"))
    assert target[0].startswith(f"{PROJECT}/{change['plan_id']} run:{spec['id']} change:{change['id']} task:")
    assert fire(world.client, NIGHT).get("replay") is None  # one run of the schedule at a time


def test_replay_results_take_the_runs_own_key_and_are_kept_with_the_change(world, tmp_path, monkeypatch, capsys):
    change = replay_world(world)
    spec = replay_run(world)
    run_id, key = spec["id"], spec["curator"]["replay_key"]
    moved(world.client, world.worker, run_id, "running")
    body = {"arm": "a", "base_sha": BASE, "result": arm(False)}
    assert report(world, run_id, None, **body).status_code == 403  # the worker token alone writes no result
    assert report(world, run_id, "not-the-key", **body).status_code == 403
    first = report(world, run_id, key, **body)
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "running" and first.json()["a"]["passed"] is False
    assert report(world, run_id, key, **body).status_code == 409  # one result an arm
    done = report(world, run_id, key, arm="b", base_sha=BASE, result=arm(True, cost_usd=0.08))
    assert done.status_code == 200 and done.json()["state"] == "done"
    moved(world.client, world.worker, run_id, "verifying", "done")
    assert [row.base_sha for row in suite(world) if row.step_key == holdout()[0]] == [BASE]
    shown = world.client.get(path("changes", str(change["id"]), "replay"), headers=owner(world))
    assert shown.status_code == 200, shown.text
    found = shown.json()
    assert found["tasks"] == 2 and [item["state"] for item in found["replays"]] == ["done"]
    assert (found["arms"]["a"]["passed"], found["arms"]["b"]["passed"], found["arms"]["both"]) == (0, 1, 1)
    assert found["arms"]["b"]["cost_usd"] == 0.08
    second = replay_run(world)  # the next holdout task
    assert second["curator"]["replay"]["task"]["step_key"] == holdout()[1]
    with serving(create_app(world.app.state.config)) as url:
        token = owner(world)["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)
        args = ("hub", "curator", "replay", "show", str(change["id"]), "--project", PROJECT)
        listed = as_json(cli(monkeypatch, capsys, home, *args, "--json"))
        assert_json_keys("hub curator replay show", listed)
        out = ok(cli(monkeypatch, capsys, home, *args)).out
        assert "arm a (the default branch's harness): 0 of 1 passed" in out
        assert "arm b (the change's harness): 1 of 1 passed, $0.08" in out


def test_replay_run_that_ends_without_both_arms_is_replayed_again_then_fails(world):
    replay_world(world, charter_changes={"circuit_breaker": {"max_failed_in_a_row": 5}})
    first = replay_run(world)
    moved(world.client, world.worker, first["id"], "running")
    assert report(
        world, first["id"], first["curator"]["replay_key"], arm="a", base_sha=BASE, result=arm(True)
    ).is_success
    moved(world.client, world.worker, first["id"], "failed", error="the worker stopped")
    (row,) = replays(world)
    assert (row.state, row.attempts) == ("queued", 1)
    again = replay_run(world)
    assert again["curator"]["replay"]["task"]["id"] == row.task_id  # the same task, from both arms again
    rp = tables.curator_replays
    assert sql(world.db, select(rp.c.result_a).where(rp.c.id == row.id)) == [(None,)]
    moved(world.client, world.worker, again["id"], "running", "failed", error="no answer")
    (row,) = replays(world)
    assert (row.state, row.attempts) == ("failed", 2)
    reason = sql(world.db, select(rp.c.reason).where(rp.c.id == row.id))[0][0]
    assert reason.startswith("2 replay runs ended without both arms, the last failed")
    third = replay_run(world)
    assert third["curator"]["replay"]["task"]["id"] != row.task_id


def test_replay_a_task_refused_as_unusable_is_never_replayed_again(world):
    replay_world(world)
    spec = replay_run(world)
    moved(world.client, world.worker, spec["id"], "running")
    refused = report(
        world, spec["id"], spec["curator"]["replay_key"], refused="no commit on origin", task_unusable=True
    )
    assert refused.status_code == 200 and refused.json()["state"] == "failed"
    assert report(world, spec["id"], spec["curator"]["replay_key"], arm="a").status_code == 422
    moved(world.client, world.worker, spec["id"], "verifying", "done")
    unusable = [row for row in suite(world) if row.unusable]
    assert [(row.step_key, row.unusable) for row in unusable] == [(holdout()[0], "no commit on origin")]
    after = replay_run(world)
    assert after["curator"]["replay"]["task"]["step_key"] == holdout()[1]


def test_replay_nothing_without_a_replay_budget_and_for_a_change_of_code(world):
    written(world.client, owner(world), curator_charter())  # no replay.budget_usd
    put_history(world, history_plan())
    built(world)
    accepted(world, kind="docs", paths=AGENTS)
    builder(world)
    assert fire(world.client, NIGHT).get("replay") is None
    written(world.client, owner(world), curator_charter(replay=REPLAY))
    c = tables.curator_changes
    sql(world.db, update(c).values(state="pr_pending"))
    pr = tables.proposals
    sql(world.db, update(pr).values(paths=[{"repo": "evo-agents", "path": "evo_agents/hub/replay.py"}]))
    assert fire(world.client, NIGHT).get("replay") is None  # a change of code is not replayed
    sql(world.db, update(pr).values(kind="skill"))
    assert fire(world.client, NIGHT).get("replay") == 1  # a proposal of kind skill is, whatever its paths


def test_replay_charter_takes_the_replay_settings_and_checks_them(world):
    for changes, status in (
        ({"replay": {"tasks": 2, "budget_usd": 9.0}}, 422),  # over night_budget_usd 5.0
        ({"replay": {"tasks": replay.MAX_TASKS + 1, "budget_usd": 1.0}}, 422),
        ({"replay": {"tasks": 2, "budget": 1.0}}, 422),
    ):
        assert put_charter(world.client, owner(world), curator_charter(**changes)).status_code == status, changes
    shown = written(world.client, owner(world), curator_charter(replay=REPLAY))
    assert shown["replay"] == REPLAY
    assert written(world.client, owner(world), curator_charter())["replay"] == {"tasks": 3, "budget_usd": None}


# Who reads replays


def test_replay_mcp_tool_answers_a_change_and_the_suite_and_no_run_agent_of_a_replay_or_judge_gets_it(world):
    change = replay_world(world)
    spec = replay_run(world)

    def tool(arguments: dict, headers: dict, method: str = "tools/call") -> dict:
        message = {"jsonrpc": "2.0", "id": 9, "method": method}
        if method == "tools/call":
            message["params"] = {"name": "curator_replay", "arguments": arguments}
        response = world.client.post("/mcp", json=message, headers={**headers, **MCP_HEADERS})
        assert response.status_code == 200, response.text
        return response.json()["result"]

    member = {**owner(world), "X-Evo-Project": PROJECT}
    found = tool({"change_id": change["id"]}, member)
    assert (
        not found.get("isError")
        and f"Change #{change['id']} of project {PROJECT}: 1 of 2 replays" in (found["content"][0]["text"])
    )
    listed = tool({"split": "holdout"}, member)
    assert (
        f"The replay suite of project {PROJECT}: {12 - len(holdout())} select, {len(holdout())} holdout"
        in (listed["content"][0]["text"])
    )
    agent = {"Authorization": world.worker["headers"]["Authorization"], "X-Evo-Run": str(spec["id"])}
    moved(world.client, world.worker, spec["id"], "running")
    refused = tool({"change_id": change["id"]}, agent)
    assert refused.get("isError") and "is not for a replay run" in refused["content"][0]["text"]
    assert tool({}, agent, method="tools/list")["tools"] == []  # a replay run's agent gets none of the hub's tools
    assert REPLAY_TOOLS == frozenset() and "curator_replay" not in JUDGE_TOOLS
    assert not offered("curator_replay", "judge") and offered("curator_replay", "review")
    assert "curator_replay" in [item["name"] for item in TOOLS]


def test_replay_the_agent_of_a_review_run_lists_the_select_split_alone(world):
    written(world.client, owner(world), curator_charter(replay=REPLAY))
    put_history(world, history_plan())
    built(world)
    run_id = review_run(world)
    agent = {"Authorization": world.worker["headers"]["Authorization"], "X-Evo-Run": str(run_id), **MCP_HEADERS}

    def listed(arguments: dict) -> dict:
        message = {"jsonrpc": "2.0", "id": 3, "method": "tools/call"}
        message["params"] = {"name": "curator_replay", "arguments": arguments}
        response = world.client.post("/mcp", json=message, headers=agent)
        assert response.status_code == 200, response.text
        return response.json()["result"]

    found = listed({})
    assert not found.get("isError")
    shown = {task["step_key"] for task in found["structuredContent"]["tasks"]} if "structuredContent" in found else None
    text = found["content"][0]["text"]
    assert all(f"history#{key} " not in text for key in holdout()) and "select history#" in text
    assert shown is None or shown.isdisjoint(holdout())
    refused = listed({"split": "holdout"})
    assert refused.get("isError") and "the select split of the replay suite alone" in refused["content"][0]["text"]


def test_replay_the_judge_reads_no_replay(world):
    replay_world(world)
    spec = replay_run(world)
    moved(world.client, world.worker, spec["id"], "running")
    report(world, spec["id"], spec["curator"]["replay_key"], arm="a", base_sha=BASE, result=arm(True))
    assert moved_on(world) == {"opened": 1}
    moved(world.client, world.worker, spec["id"], "failed", error="stopped")
    judge = judge_run(world)
    assert "replay" not in judge["prompt"].lower() and "arm a" not in judge["prompt"]


def test_replay_schema_0053_goes_down_to_0052_and_up_again(world):
    replay_world(world)
    spec = replay_run(world)
    db = world.db
    rp = tables.curator_replays
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(rp).values(state="done"))  # done needs both arms
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(tables.replay_tasks).values(split="other"))
    r = tables.runs
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(db, update(r).values(schedule_id=None).where(r.c.kind == "replay"))
    assert sql(db, select(func.count()).select_from(r).where(r.c.id == spec["id"])) == [(1,)]
    move_to(db, "0052", down=True)
    assert sql(db, select(func.count()).select_from(r).where(r.c.kind == "replay")) == [(0,)]
    move_to(db, "0053")
    assert sql(db, select(func.count()).select_from(tables.replay_tasks)) == [(0,)]

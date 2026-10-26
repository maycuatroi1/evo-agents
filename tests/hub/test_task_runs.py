"""Agent types of plugins and the task runs a plugin dispatches, on the hub (``evo_agents.hub.task_runs``,
``evo_agents.hub.server.agent_types`` and ``task_dispatch``), as criterion a9 of the plan hub-agent-plugins names it.

A plugin contributes agent types; an admin of a project that enabled the plugin enables one there, which creates its
agent profile from the type's template, and names the member its runs go under, that member's workers they may run on
and the cap of what they may cost a day. The plugin, granted runs:dispatch, hands the type a task with its token: a run
of kind task under that member's name, on the branch task/<run>, with what is left of the cap as its cost cap, claimed
only by a worker the type names; a task past the cap is refused (429). The run's push gets a draft pull request the hub
opens and never merges, and the plugin gets the event task.result when the task ends. The worker's side is
``tests.worker.test_task_run``."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from evo_agents.hub import autonomy_card, runs, tables, task_runs
from evo_agents.hub.server import plugin_tokens
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chat_runs import chat_worker, in_chats
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_plugin_runtimes import install, message
from tests.hub.test_runs import (
    PROJECT,
    audit_rows,
    claim,
    control,
    expire,
    moved,
    of_run,
    recover,
    set_run,
    state_of,
    steady,
)

app_key = credentials_api.app_key  # a fixture
MINE = credentials_api.MINE
OWNER, OTHER, READER = credentials_api.OWNER, credentials_api.OTHER, "reader"
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
READ_ONLY = {"role": "reader", "max_level": "internal"}
ONE_REPO = {f"{PROJECT}/evo-agents": {"path": "/src/evo-agents", "branch": "main"}}
WORKERS_APP = {"contents": "write", "metadata": "read", "pull_requests": "write"}
PLUGIN, DISPATCH = "triage", task_runs.DISPATCH_PERMISSION
TRIAGER = {
    "name": "triager",
    "title": "Issue triager",
    "description": "Fixes the issues the plugin hands it",
    "profile": {
        "handle": "triager",
        "name": "Triager",
        "description": "Fixes flaky tests",
        "instructions": "Keep each change small.",
    },
    "events": ["results"],
}
CONTRIBUTES = {"events": [{"name": "results", "on": ["task.result"]}], "agent_types": [TRIAGER]}
TASK = "Fix the flaky wait in tests/test_wait.py."
REPOS = ["evo-agents-harness", "evo-agents", "lumen-identity", "lumen-kb-docs", "notes"]  # its harness, then its repos


@pytest.fixture
def client(hub_db, tmp_path, github, app_key):  # noqa: F811
    config = credentials_api.hub_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """The project of ``test_credentials_api`` (evo-agents on GitHub and three more repos), owner its admin,
    someone-else a writer and reader a reader, stranger signed in without a grant; plugin triage installed with its
    agent type triager and enabled there with runs:dispatch granted. The headers of each, and the plugin's id."""
    return set_up(client, github, hub_db)


def set_up(client, github, hub_db, contributes: dict = CONTRIBUTES) -> dict:
    """What ``hub`` sets up, triage contributing ``contributes``."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
        "reader": bearer(live.sign_in(client, github, READER, 504)["token"]),
        "stranger": bearer(live.sign_in(client, github, "stranger", 503)["token"]),
    }
    body = {**credentials_api.registration(credentials_api.DISPATCHED), "sinks": OPEN_SINKS}
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, ADMIN_GRANT), (OTHER, credentials_api.WRITER), (READER, READ_ONLY)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    plugin_id = install(hub_db, name=PLUGIN, contributes=contributes, permissions=[DISPATCH])
    enable_plugin(client, headers["owner"], PLUGIN, [DISPATCH])
    return {**headers, "plugin_id": plugin_id}


def enable_plugin(client, headers, name: str, permissions: list[str]) -> None:
    body = {"permissions": permissions}
    response = client.post(f"/v1/projects/{PROJECT}/plugins/{name}/enable", json=body, headers=headers)
    assert response.status_code == 200, response.text


def put_type(client, headers, name: str = "triager", plugin: str = PLUGIN, **body):
    body = {"run_as": OTHER, "daily_usd": 5} | body
    return client.put(f"/v1/projects/{PROJECT}/agent-types/{plugin}/{name}", json=body, headers=headers)


def enabled(client, headers, **body) -> dict:
    response = put_type(client, headers, **body)
    assert response.status_code == 200, response.text
    return response.json()


def listed(client, headers) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/agent-types", headers=headers)
    assert response.status_code == 200, response.text
    return {(item["plugin"], item["name"]): item for item in response.json()}


def plugin_key(client, plugin_id: int) -> dict:
    """The headers of a fresh token of the plugin's backend, as the plugin host hands it."""

    async def issue() -> str:
        async with client.app.state.engine.begin() as conn:
            return await plugin_tokens.issue_token(conn, plugin_id)

    return {"Authorization": f"Bearer {client.portal.call(issue)}"}


def hand(client, key: dict, task: str = TASK, name: str = "triager", project: str = PROJECT, **body):
    """The plugin hands agent type ``name`` a task."""
    path = f"/v1/plugin-api/projects/{project}/agent-types/{name}/tasks"
    return client.post(path, json={"task": task, **body}, headers=key)


def handed(client, key: dict, **body) -> dict:
    response = hand(client, key, **body)
    assert response.status_code == 201, response.text
    return response.json()


def task_of(client, key: dict, task_id: int):
    return client.get(f"/v1/plugin-api/projects/{PROJECT}/tasks/{task_id}", headers=key)


def deliveries(db, kind: str = task_runs.RESULT_EVENT) -> list[tuple]:
    d = tables.plugin_deliveries
    query = select(d.c.plugin_id, d.c.contribution, d.c.payload).where(d.c.kind == kind).order_by(d.c.id)
    return sql(db, query)


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def without_task() -> list[str]:
    return [kind for kind in runs.RUN_KINDS if kind != "task"]


# Enabling an agent type


def test_agent_type_enable_creates_its_profile_once_and_an_admin_changes_or_turns_it_off(client, hub, hub_db):
    chat_worker(client, hub["other"], "Laptop", checkouts=ONE_REPO)
    before = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (before["enabled"], before["handle"], before["run_as"], before["daily_usd"]) == (
        False,
        "triager",
        None,
        None,
    )
    view = enabled(client, hub["owner"], workers=["laptop", "LAPTOP"], daily_usd=5)
    assert (view["enabled"], view["run_as"], view["workers"], view["daily_usd"], view["spent_usd"]) == (
        True,
        OTHER,
        ["laptop"],
        5,
        0,
    )
    assert (view["enabled_by"], view["title"], view["environment"], view["packs"]) == (
        OWNER,
        "Issue triager",
        None,
        [],
    )
    profile = client.get(f"/v1/projects/{PROJECT}/agents/triager", headers=hub["reader"]).json()
    assert (profile["name"], profile["description"], profile["instructions"], profile["version"]) == (
        "Triager",
        "Fixes flaky tests",
        "Keep each change small.",
        1,
    )
    again = enabled(client, hub["owner"], daily_usd=12.5)  # the same profile: its admins edit it as any other
    assert (again["workers"], again["daily_usd"], again["handle"]) == ([], 12.5, "triager")
    ap = tables.agent_profiles
    assert sql(hub_db, select(func.count()).select_from(ap).where(ap.c.handle == "triager")) == [(1,)]
    off = client.delete(f"/v1/projects/{PROJECT}/agent-types/{PLUGIN}/triager", headers=hub["owner"])
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["disabled_at"] is not None
    assert "is not enabled" in message(
        client.delete(f"/v1/projects/{PROJECT}/agent-types/{PLUGIN}/triager", headers=hub["owner"]), 409
    )
    assert enabled(client, hub["owner"])["enabled"] is True
    target = f"{PROJECT}/plugin:{PLUGIN} agent-type:triager"
    assert audit_rows(hub_db, "agent-type") == [
        ("agent-type.enable", f"{target} @triager run-as={OTHER} workers=laptop daily-usd=5.0", OWNER, PROJECT),
        ("agent-type.enable", f"{target} @triager run-as={OTHER} workers=- daily-usd=12.5", OWNER, PROJECT),
        ("agent-type.disable", target, OWNER, PROJECT),
        ("agent-type.enable", f"{target} @triager run-as={OTHER} workers=- daily-usd=5.0", OWNER, PROJECT),
    ]
    assert [row[1] for row in audit_rows(hub_db, "agent")] == [f"{PROJECT}/@triager v1"]


def test_agent_type_enable_is_refused_saying_why_and_changes_nothing(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio", checkouts=ONE_REPO)
    assert "needs the admin role" in message(put_type(client, hub["other"]), 403)
    unseen = client.get(f"/v1/projects/{PROJECT}/agent-types", headers=hub["stranger"])
    assert "no project evo-agents that you can see" in message(unseen, 404)
    assert "contributes no agent type fixer" in message(put_type(client, hub["owner"], name="fixer"), 404)
    install(hub_db, name="helpers", contributes={"agent_types": [{**TRIAGER, "events": []}]}, permissions=[DISPATCH])
    assert "is not enabled in project" in message(put_type(client, hub["owner"], plugin="helpers"), 409)
    for body, said in (
        ({"run_as": READER}, f"and {READER} holds reader there"),
        ({"run_as": "nobody"}, "and nobody is no member of the hub"),
        ({"workers": ["studio"]}, f"{OTHER} has no worker studio that is not revoked"),
    ):
        assert said in message(put_type(client, hub["owner"], **body), 422)
    for body in ({"daily_usd": 0}, {"daily_usd": 1000.5}, {"workers": ["w"] * 17}, {"model": "large"}):
        assert put_type(client, hub["owner"], **body).status_code == 422, body
    # a handle a project or another agent holds: the one the admin names, or both the template's and
    # <template's>-<project>
    clash = {**TRIAGER, "name": "clash", "events": [], "profile": {"handle": PROJECT}}
    taken = {**TRIAGER, "name": "taken", "events": [], "profile": {"handle": "reviewer"}}
    install(hub_db, name="more", contributes={"agent_types": [clash, taken]}, permissions=[DISPATCH])
    enable_plugin(client, hub["owner"], "more", [DISPATCH])
    named = put_type(client, hub["owner"], name="clash", plugin="more", handle=PROJECT)
    assert f"@{PROJECT}, the handle agent type clash" in message(named, 409)
    for handle in ("reviewer", f"reviewer-{PROJECT}"):
        made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": "R"}, headers=hub["owner"])
        assert made.status_code == 201, made.text
    both = f"@reviewer and @reviewer-{PROJECT}, the handles agent type taken"
    assert both in message(put_type(client, hub["owner"], name="taken", plugin="more"), 409)
    assert sql(hub_db, select(func.count()).select_from(tables.project_agent_types)) == [(0,)]
    assert audit_rows(hub_db, "agent-type") == []
    # a deleted profile is never made again
    enabled(client, hub["owner"])
    assert client.delete(f"/v1/projects/{PROJECT}/agents/triager", headers=hub["owner"]).status_code == 200
    assert "was deleted, and a handle is never taken again" in message(put_type(client, hub["owner"]), 409)
    assert listed(client, hub["owner"])[(PLUGIN, "triager")]["enabled"] is False


# Dispatching a task


def test_task_dispatch_queues_a_task_run_under_run_as_with_what_is_left_of_the_cap(client, hub, hub_db):
    enabled(client, hub["owner"], daily_usd=5)
    key = plugin_key(client, hub["plugin_id"])
    task = handed(client, key, title="Fix the wait")
    run_id = task["run_id"]
    branch = task_runs.branch_of(run_id)
    assert (task["task_id"], task["attempt"], task["state"], task["branch"], task["pulls"]) == (
        run_id,
        1,
        "queued",
        branch,
        [],
    )
    assert (task["project"], task["agent_type"], task["daily_usd"], task["spent_usd"]) == (PROJECT, "triager", 5, 0)
    assert task["budget"] == {"max_usd": 5, "max_seconds": task_runs.TIME_CAP_SECONDS}
    ap = tables.agent_profiles
    profile_id = sql(hub_db, select(ap.c.id).where(ap.c.handle == "triager"))[0][0]
    names = ("kind", "dispatched_by", "dispatched_via", "profile_id", "request", "title", "plan_id", "mode", "budget")
    assert of_run(hub_db, run_id, *names) == [
        (
            "task",
            user_id(hub_db, OTHER),
            "machine",
            profile_id,
            TASK,
            "Fix the wait",
            None,
            "headless",
            task_runs.run_budget(5),
        )
    ]
    assert of_run(hub_db, run_id, "repos")[0][0] == [{"repo": name, "branch": branch} for name in REPOS]
    assert audit_rows(hub_db, "task") == [
        ("task.dispatch", f"{PROJECT}/plugin:{PLUGIN} agent-type:triager run:{run_id}", OTHER, PROJECT)
    ]
    shown = task_of(client, key, run_id).json()
    assert (shown["run_id"], shown["state"], shown["branch"]) == (run_id, "queued", branch)
    assert "budget" not in shown and "request" not in shown


def test_task_dispatch_goes_only_to_a_worker_of_run_as_the_agent_type_names_with_its_prompt(client, hub):
    studio = chat_worker(client, hub["owner"], "studio", checkouts=ONE_REPO)
    desk = chat_worker(client, hub["other"], "desk", checkouts=ONE_REPO)
    old = chat_worker(client, hub["other"], "old", checkouts=ONE_REPO, kinds=without_task())
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=ONE_REPO)
    enabled(client, hub["owner"], workers=["laptop", "old"])
    run_id = handed(client, plugin_key(client, hub["plugin_id"]))["run_id"]
    for worker in (studio, desk, old):  # not run_as's, not named, or a daemon that runs no task run
        assert claim(client, worker) is None, worker["name"]
    spec = claim(client, laptop)
    assert (spec["id"], spec["kind"], spec["branch"], spec["mode"]) == (run_id, "task", f"task/{run_id}", "headless")
    assert spec["budget"]["max_usd"] == 5 and spec["budget"]["max_seconds"] == task_runs.TIME_CAP_SECONDS
    prompt = autonomy_card.without_card(spec["prompt"])  # the card opens it (autonomy-levels)
    assert prompt.startswith(f"You are @triager (Triager), agent type triager of plugin {PLUGIN} in project {PROJECT}")
    assert f"Plugin {PLUGIN} hands you the task below for {OTHER}" in prompt
    assert f"each a worktree on the branch task/{run_id}" in prompt and prompt.endswith(f"# The task\n{TASK}\n")
    # an agent type naming no worker: any worker of run_as takes the next one
    enabled(client, hub["owner"])
    second = handed(client, plugin_key(client, hub["plugin_id"]))["run_id"]
    assert claim(client, studio) is None and claim(client, desk)["id"] == second


def test_task_dispatch_needs_a_live_plugin_token_and_runs_dispatch_and_an_enabled_agent_type(client, hub, hub_db):
    key = plugin_key(client, hub["plugin_id"])
    for headers in ({}, {"Authorization": "Bearer evp_nope"}, hub["owner"], {"Authorization": "Basic x"}):
        refused = hand(client, headers)
        assert refused.status_code == 401 and "a live plugin token is required" in refused.json()["message"]
    assert "agent type triager of plugin triage is not enabled" in message(hand(client, key), 404)
    enabled(client, hub["owner"])
    assert hand(client, key, name="fixer").status_code == 404
    assert "with runs:dispatch granted" in message(hand(client, key, project="lumen-other"), 403)
    assert client.post(f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/disable", headers=hub["owner"]).status_code == 200
    assert "with runs:dispatch granted" in message(hand(client, key), 403)
    enable_plugin(client, hub["owner"], PLUGIN, [DISPATCH])
    quiet_id = install(hub_db, name="quiet", contributes={"agent_types": [{**TRIAGER, "events": []}]})
    enable_plugin(client, hub["owner"], "quiet", [])  # it asks for nothing: no runs:dispatch to grant
    assert "with runs:dispatch granted" in message(hand(client, plugin_key(client, quiet_id)), 403)
    # another plugin's token reaches neither its agent types nor its tasks
    other_id = install(hub_db, name="helpers", contributes={"events": []}, permissions=[DISPATCH])
    enable_plugin(client, hub["owner"], "helpers", [DISPATCH])
    theirs = plugin_key(client, other_id)
    assert hand(client, theirs).status_code == 404
    run_id = handed(client, key)["run_id"]
    assert task_of(client, theirs, run_id).status_code == 404 and task_of(client, key, run_id).status_code == 200
    old = key
    key = plugin_key(client, hub["plugin_id"])  # a new token revokes the old one
    assert hand(client, old).status_code == 401 and hand(client, key).status_code == 201
    for body in ({"task": ""}, {"task": "é" * (task_runs.MAX_TASK_BYTES // 2 + 1)}, {"task": TASK, "x": 1}):
        path = f"/v1/plugin-api/projects/{PROJECT}/agent-types/triager/tasks"
        assert client.post(path, json=body, headers=key).status_code == 422
    # run_as no longer a writer; then the type turned off
    grant = {"role": "reader", "max_level": "internal"}
    client.put(f"/v1/admin/projects/{PROJECT}/grants/{OTHER}", json=grant, headers=hub["admin"])
    assert f"runs as {OTHER}, who holds reader" in message(hand(client, key), 409)
    client.put(f"/v1/admin/projects/{PROJECT}/grants/{OTHER}", json=credentials_api.WRITER, headers=hub["admin"])
    assert (
        client.delete(f"/v1/projects/{PROJECT}/agent-types/{PLUGIN}/triager", headers=hub["owner"]).status_code == 200
    )
    assert hand(client, key).status_code == 404
    assert [row[0] for row in audit_rows(hub_db, "task")] == ["task.dispatch", "task.dispatch"]


def test_task_budget_refuses_a_dispatch_once_the_runs_of_a_day_cost_the_cap(client, hub, hub_db):
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=ONE_REPO)
    enabled(client, hub["owner"], daily_usd=5)
    key = plugin_key(client, hub["plugin_id"])
    first = handed(client, key)["run_id"]
    assert claim(client, laptop)["id"] == first
    moved(client, laptop, first, "running", "verifying", "done", usage={"total_cost_usd": 3.25})
    second = handed(client, key)
    assert (second["spent_usd"], second["budget"]["max_usd"]) == (3.25, 1.75)
    assert listed(client, hub["owner"])[(PLUGIN, "triager")]["spent_usd"] == 3.25
    set_run(hub_db, second["run_id"], usage={"total_cost_usd": 1.75})
    refused = hand(client, key)
    said = message(refused, 429)
    assert "cost $5.00 in the last 24 hours, which reached its cap of $5.00" in said
    count = select(func.count()).select_from(tables.runs).where(tables.runs.c.kind == "task")
    assert sql(hub_db, count) == [(2,)]
    enabled(client, hub["owner"], daily_usd=6)  # the cap raised
    assert handed(client, key)["budget"]["max_usd"] == 1
    for run_id in (first, second["run_id"]):  # runs older than the window leave it
        set_run(hub_db, run_id, queued_at=func.now() - func.make_interval(0, 0, 0, 0, task_runs.BUDGET_HOURS + 1))
    assert handed(client, key)["spent_usd"] == 0


# The branch, the result and the next attempt


def test_task_dispatch_push_gets_a_draft_pull_request_and_the_end_reaches_the_plugin(client, hub, hub_db, github):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=ONE_REPO)
    enabled(client, hub["owner"])
    key = plugin_key(client, hub["plugin_id"])
    run_id = handed(client, key, title="Fix the wait")["run_id"]
    branch = task_runs.branch_of(run_id)
    assert claim(client, laptop)["id"] == run_id
    moved(client, laptop, run_id, "running", session_id="sess-1")
    github.push(MINE, "evo-agents", branch, "1" * 40)
    body = {"repo": "evo-agents", "branch": branch, "head": "1" * 40}
    opened = in_chats(client, laptop, run_id, "task/pulls", body)
    assert opened.status_code == 201, opened.text
    pull = opened.json()
    url = f"https://github.com/{MINE}/evo-agents/pull/1"
    assert (pull["url"], pull["number"], pull["opened"], pull["problem"]) == (url, 1, True, None)
    made = github.pull_of(MINE, "evo-agents", branch)
    assert (made.base, made.draft, made.merged, made.state) == ("main", True, False, "open")
    again = in_chats(client, laptop, run_id, "task/pulls", {**body, "head": "2" * 40}).json()
    assert (again["number"], again["opened"]) == (1, False)
    elsewhere = in_chats(client, laptop, run_id, "task/pulls", {**body, "repo": "lumen-kb-docs"}).json()
    assert (elsewhere["url"], elsewhere["number"]) == (None, None)
    for wrong, status in (({"branch": "main"}, 422), ({"repo": "other"}, 422)):
        assert in_chats(client, laptop, run_id, "task/pulls", {**body, **wrong}).status_code == status
    stranger = chat_worker(client, hub["owner"], "studio", checkouts=ONE_REPO)
    assert in_chats(client, stranger, run_id, "task/pulls", body).status_code == 404
    kept = [{"repo": "evo-agents", "url": url, "number": 1}]
    assert of_run(hub_db, run_id, "pulls") == [(kept,)]
    assert deliveries(hub_db) == []
    moved(client, laptop, run_id, "verifying", "done", summary="Replaced the sleep.")
    payload = {
        "task_id": run_id,
        "run_id": run_id,
        "attempt": 1,
        "project": PROJECT,
        "agent_type": "triager",
        "state": "done",
        "branch": branch,
        "pulls": kept,
        "failure_cause": None,
    }
    assert deliveries(hub_db) == [(hub["plugin_id"], "results", payload)]
    shown = task_of(client, key, run_id).json()
    assert (shown["state"], shown["pulls"], shown["finished_at"] is not None) == ("done", kept, True)
    assert "is a task run: its plugin hands the task to its agent type again" in message(
        control(client, hub["other"], run_id, "rerun"), 409
    )


def test_task_dispatch_lost_run_goes_on_in_the_next_attempt_with_its_agent_type_and_branch(client, hub, hub_db):
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=ONE_REPO)
    enabled(client, hub["owner"])
    key = plugin_key(client, hub["plugin_id"])
    first = handed(client, key)["run_id"]
    assert claim(client, laptop)["id"] == first
    moved(client, laptop, first, "running")
    set_run(hub_db, first, pulls=[{"repo": "evo-agents", "url": "https://example.org/pull/7", "number": 7}])
    expire(hub_db, first)
    assert recover(client)["lost"] == 1 and state_of(hub_db, first) == "lost"
    r = tables.runs
    ((second,),) = sql(hub_db, select(r.c.id).where(r.c.parent_run_id == first))
    names = ("kind", "agent_type_id", "branch", "pulls", "state", "attempt", "request")
    (kept,) = of_run(hub_db, second, *names)
    assert kept[:4] == of_run(hub_db, first, *names[:4])[0] and kept[4:] == ("queued", 2, TASK)
    assert deliveries(hub_db) == []  # a lost run is no end of the task
    shown = task_of(client, key, first).json()
    assert (shown["task_id"], shown["run_id"], shown["attempt"], shown["state"]) == (first, second, 2, "queued")
    steady(hub_db, laptop)  # a worker that lost a run takes the next once its heartbeats are steady again
    assert claim(client, laptop)["branch"] == task_runs.branch_of(first)
    assert control(client, hub["other"], second, "cancel").status_code == 200
    moved(client, laptop, second, "cancelled")
    (result,) = deliveries(hub_db)
    assert (result[2]["task_id"], result[2]["run_id"], result[2]["state"]) == (first, second, "cancelled")


# A branch the task names, and its agent time


def test_task_dispatch_on_a_named_branch_of_one_repo_with_more_agent_time_pushes_back_to_it(
    client, hub, hub_db, github
):
    credentials_api.install(github, "evo-agents", permissions=WORKERS_APP)
    github.add_repo(MINE, "evo-agents")
    laptop = chat_worker(client, hub["other"], "laptop", checkouts=ONE_REPO)
    enabled(client, hub["owner"], daily_usd=5)
    key = plugin_key(client, hub["plugin_id"])
    named = {"repo": "evo-agents", "branch": "post/northwind-launch", "minutes": task_runs.MAX_MINUTES}
    task = handed(client, key, **named)
    run_id = task["run_id"]
    own = task_runs.branch_of(run_id)
    assert task["branch"] == own and task["task_id"] == run_id  # the task's identity stays task/<run>
    assert task["budget"] == {"max_usd": 5, "max_seconds": task_runs.MAX_MINUTES * 60}
    assert of_run(hub_db, run_id, "timeout_s", "budget") == [
        (task_runs.run_timeout(task_runs.MAX_MINUTES), task_runs.run_budget(5, task_runs.MAX_MINUTES))
    ]
    assert task_runs.run_timeout(task_runs.MAX_MINUTES) > task_runs.MAX_MINUTES * 60
    expected = [{"repo": name, "branch": named["branch"] if name == "evo-agents" else own} for name in REPOS]
    assert of_run(hub_db, run_id, "repos")[0][0] == expected
    # another task on that branch waits for this one to end
    busy = hand(client, key, repo="evo-agents", branch=named["branch"])
    assert busy.status_code == 409 and f"task run #{run_id} works on" in busy.text
    spec = claim(client, laptop)
    assert spec["id"] == run_id and spec["budget"]["max_seconds"] == task_runs.MAX_MINUTES * 60
    assert {"repo": "evo-agents", "branch": named["branch"]} in spec["repos"]
    prompt = autonomy_card.without_card(spec["prompt"])
    assert f"- evo-agents: evo-agents/ (on the branch {named['branch']}, which the task names" in prompt
    moved(client, laptop, run_id, "running", session_id="sess-1")
    github.push(MINE, "evo-agents", named["branch"], "1" * 40)
    body = {"repo": "evo-agents", "branch": named["branch"], "head": "1" * 40}
    opened = in_chats(client, laptop, run_id, "task/pulls", body)
    assert opened.status_code == 201, opened.text
    assert github.pull_of(MINE, "evo-agents", named["branch"]).draft is True
    for wrong in ({"branch": own}, {"branch": "main"}, {"repo": "lumen-kb-docs"}):
        assert in_chats(client, laptop, run_id, "task/pulls", {**body, **wrong}).status_code == 422, wrong
    other = in_chats(client, laptop, run_id, "task/pulls", {**body, "repo": "lumen-kb-docs", "branch": own})
    assert other.status_code == 201, other.text  # the other repos stay on task/<run>


def test_task_dispatch_refuses_a_default_branch_or_one_of_another_run_and_more_than_its_minutes(client, hub, hub_db):
    enabled(client, hub["owner"])
    key = plugin_key(client, hub["plugin_id"])
    before = sql(hub_db, select(func.count()).select_from(tables.runs))
    for body, status, said in (
        ({"repo": "evo-agents", "branch": "main"}, 422, "is a default branch"),
        ({"repo": "evo-agents", "branch": "HEAD"}, 422, "is a default branch"),
        ({"repo": "evo-agents", "branch": "task/12"}, 422, "is a branch of another run"),
        ({"repo": "evo-agents", "branch": "chat/3"}, 422, "is a branch of another run"),
        ({"repo": "evo-agents", "branch": "post/../x"}, 422, "not a branch name git takes"),
        ({"repo": "nowhere", "branch": "post/x"}, 422, "the project has no repo nowhere"),
        ({"branch": "post/x"}, 422, "names a repo and its branch together"),
        ({"repo": "evo-agents"}, 422, "names a repo and its branch together"),
        ({"minutes": task_runs.MAX_MINUTES + 1}, 422, "less than or equal to"),
        ({"minutes": 0}, 422, "greater than or equal to"),
    ):
        response = hand(client, key, **body)
        assert response.status_code == status and said in response.text, (body, response.text)
    assert sql(hub_db, select(func.count()).select_from(tables.runs)) == before


def test_task_branch_problem_takes_a_branch_of_its_own_and_no_default_branch():
    assert task_runs.branch_problem("post/northwind-launch") is None
    assert task_runs.branch_problem("draft-7", default_branch="trunk") is None
    assert "default branch" in task_runs.branch_problem("trunk", default_branch="trunk")
    for name in ("", "-x", "a b", "a..b", "a/", "a.lock", "x@{1}", "a//b", "a/.b", "release/1.0", "evo-run/3/x"):
        assert task_runs.branch_problem(name) is not None, name
    assert task_runs.run_budget(2.5) == {"max_usd": 2.5, "max_seconds": task_runs.TIME_CAP_SECONDS}
    assert task_runs.run_timeout() == task_runs.TIMEOUT_MIN * 60 and task_runs.run_timeout(10) == 3600

"""The environment axis of an agent profile on the hub, as criterion a7 of the plan hub-agent-plugins names it: the
eighth axis is all (the worker's host, as 0.16.1 ran every run) or an allowlist, in order of preference, of host and
the environments of plugins (<plugin>/<name>, a container image a plugin declares); editing it raises the profile's
version; a dispatch under a profile that allows no environment its project registers is 422; a worker claims a run
whose profile allows only plugins' environments when its owner accepted the plugin and its daemon says it runs
environments, else the run waits, queued, never failed; the claim picks the first environment of the allowlist the
worker takes, records it in runs.environment and hands its declaration; a run whose plugin was disabled before its
claim fails there, saying why; a dispatch pinned to a worker that could never take the run is 409. Whether the worker
has a container engine is its preflight's (``tests.worker.test_environments``)."""

from __future__ import annotations

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import profiles, runs, tables
from tests.hub.test_plans import registration
from tests.hub.test_plugin_runtimes import (
    ADMIN_GRANT,
    beat,
    client,  # noqa: F401 (a fixture)
    enable,
    install,
    message,
    set_plugins,
    state_of,
)
from tests.hub.test_runs import OWNER, PROJECT, add_worker, claim, dispatch, dispatched, members, of_run

DIGEST = "sha256:" + "0123456789abcdef" * 4
BOX = {
    "name": "box",
    "title": "Agent box",
    "image": f"ghcr.io/acme/agent-box@{DIGEST}",
    "network": "outbound",
    "mounts": [{"type": "tmpfs", "target": "/scratch", "size_mb": 64}],
}
ENVIRONMENT = "sandbox/box"
DECLARATION = {
    "environment": ENVIRONMENT,
    "plugin": "sandbox",
    "version": "1.0.0",
    "title": "Agent box",
    "provider": "container",
    "image": f"ghcr.io/acme/agent-box@{DIGEST}",
    "network": "outbound",
    "mounts": [{"type": "tmpfs", "target": "/scratch", "name": None, "read_only": False, "size_mb": 64}],
}
FEATURES = list(runs.WORKER_FEATURES)
OLD_FEATURES = [feature for feature in FEATURES if feature != runs.ENVIRONMENTS]


@pytest.fixture
def hub(client, github, hub_db) -> dict:  # noqa: F811 (client is the fixture imported above)
    """The members of ``tests.hub.test_runs``, owner an admin of the project, and plugin sandbox installed with its
    environment box, not enabled yet."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    install(hub_db, contributes={"environments": [BOX]})
    return headers


def agent(client, headers, handle: str, environments: list[str] | None, **permissions):
    """POST of profile ``handle`` whose environment axis allows ``environments`` (all when None)."""
    rules = {axis: {"mode": "allowlist", "allow": allow} for axis, allow in permissions.items()}
    if environments is not None:
        rules["environment"] = {"mode": "allowlist", "allow": environments}
    body = {"handle": handle, "name": handle, "permissions": rules}
    return client.post(f"/v1/projects/{PROJECT}/agents", json=body, headers=headers)


def made(client, headers, handle: str, environments: list[str] | None) -> dict:
    response = agent(client, headers, handle, environments)
    assert response.status_code == 201, response.text
    return response.json()


def accepting(client, headers, name: str, *, features=FEATURES) -> dict:
    """A worker whose owner accepted plugin sandbox and whose daemon says ``features``."""
    worker = add_worker(client, headers, name)
    assert set_plugins(client, headers, worker, ["sandbox"]).status_code == 200
    beat(client, worker, features=features)
    return worker


def shown(client, headers, run_id: int) -> dict:
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# The axis


def test_profile_environment_axis_names_host_and_plugins_environments_and_raises_the_version(client, hub, hub_db):
    created = made(client, hub["owner"], "boxed", [ENVIRONMENT, "host"])
    assert created["permissions"]["environment"] == {"mode": "allowlist", "allow": [ENVIRONMENT, "host"]}
    assert created["restricted"] == ["environment"] and created["version"] == 1
    assert made(client, hub["owner"], "anywhere", None)["permissions"]["environment"] == {"mode": "all", "allow": []}
    bad = agent(client, hub["owner"], "docker", ["docker"])
    assert bad.status_code == 422
    assert "environment 'docker' is neither host nor a plugin's <plugin>/<name>" in bad.text
    edited = client.patch(
        f"/v1/projects/{PROJECT}/agents/anywhere",
        json={"permissions": {"environment": {"mode": "allowlist", "allow": ["host"]}}},
        headers=hub["owner"],
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2 and edited.json()["restricted"] == ["environment"]
    versions = client.get(f"/v1/projects/{PROJECT}/agents/anywhere/versions", headers=hub["reader"]).json()
    assert versions[0]["body"]["permissions"]["environment"] == {"mode": "allowlist", "allow": ["host"]}
    default = client.patch(
        f"/v1/projects/{PROJECT}/agents/{PROJECT}",
        json={"permissions": {"environment": {"mode": "allowlist", "allow": ["host"]}}},
        headers=hub["owner"],
    )
    assert default.status_code == 422, "the project's default agent keeps every permission"
    listed = client.get(f"/v1/projects/{PROJECT}/agents/{PROJECT}", headers=hub["reader"]).json()
    assert listed["permissions"] == {axis: {"mode": "all", "allow": []} for axis in profiles.AXES}


# Dispatching


def test_profile_environment_a_dispatch_under_a_profile_its_project_cannot_place_is_422(client, hub, hub_db):
    made(client, hub["owner"], "boxed", [ENVIRONMENT])
    made(client, hub["owner"], "nowhere", [])
    refused = message(dispatch(client, hub["owner"], [2], profile="boxed"), 422)
    assert refused == (
        f"agent @boxed allows only the environments {ENVIRONMENT}, none of which project {PROJECT} registers: enable "
        "the plugin that declares one, or allow host; it registers host; nothing was dispatched"
    )
    nowhere = message(dispatch(client, hub["owner"], [2], profile="nowhere"), 422)
    assert nowhere == "agent @nowhere allows no environment, so its runs run nowhere; nothing was dispatched"
    plan_run = client.post(
        f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": "rollout", "profile": "boxed"}, headers=hub["owner"]
    )
    assert "none of which project" in message(plan_run, 422)
    assert not live.sql(hub_db, select(tables.runs.c.id)), "nothing was dispatched"
    enable(client, hub["owner"])
    (run,) = dispatched(client, hub["owner"], [2], profile="boxed")
    assert (run["state"], run["environment"]) == ("queued", None), "the claim picks the environment"


def test_profile_environment_a_dispatch_pinned_to_a_worker_that_cannot_take_it_is_409_saying_why(client, hub, hub_db):
    enable(client, hub["owner"])
    made(client, hub["owner"], "boxed", [ENVIRONMENT])
    worker = add_worker(client, hub["owner"], "mac-mini")

    def why() -> str:
        return message(dispatch(client, hub["owner"], [2], profile="boxed", worker_id=worker["id"]), 409)

    beat(client, worker, features=OLD_FEATURES)
    assert f"does not say it runs environments, and the run's agent runs only in {ENVIRONMENT}" in why()
    beat(client, worker)
    assert "its owner has not let it take the environments of plugin sandbox: add one to the worker's plugins" in why()
    assert set_plugins(client, hub["owner"], worker, ["sandbox"]).status_code == 200
    (run,) = dispatched(client, hub["owner"], [2], profile="boxed", worker_id=worker["id"])
    spec = claim(client, worker)
    assert (spec["id"], spec["environment"]) == (run["id"], DECLARATION)
    made(client, hub["owner"], "either", [ENVIRONMENT, "host"])
    old = add_worker(client, hub["owner"], "old-daemon")
    beat(client, old, features=OLD_FEATURES)
    (other,) = dispatched(client, hub["owner"], [4], profile="either", worker_id=old["id"])
    spec = claim(client, old)
    assert (spec["id"], spec["environment"]) == (other["id"], None), "host is allowed: an old daemon takes it there"


# Claiming


def test_profile_environment_only_a_worker_that_accepted_the_plugin_and_runs_environments_claims_the_run(
    client, hub, hub_db
):
    enable(client, hub["owner"])
    made(client, hub["owner"], "boxed", [ENVIRONMENT])
    (run,) = dispatched(client, hub["owner"], [2], profile="boxed")
    old = accepting(client, hub["owner"], "old-daemon", features=OLD_FEATURES)
    assert claim(client, old) is None, "a daemon that does not say environments takes no such run"
    new = add_worker(client, hub["owner"], "new-daemon")
    beat(client, new)
    assert claim(client, new) is None, "its owner did not accept the plugin"
    assert state_of(hub_db, run["id"]) == "queued", "the run waits; it does not fail"
    worker_features = live.sql(hub_db, select(tables.workers.c.features).where(tables.workers.c.id == new["id"]))
    assert runs.ENVIRONMENTS in worker_features[0][0]

    assert set_plugins(client, hub["owner"], new, ["sandbox"]).status_code == 200
    spec = claim(client, new)
    assert (spec["id"], spec["environment"], spec["runtime"]) == (run["id"], DECLARATION, "claude-code")
    assert spec["profile"]["permissions"]["environment"] == {"mode": "allowlist", "allow": [ENVIRONMENT]}
    assert of_run(hub_db, run["id"], "worker_id", "environment") == [(new["id"], ENVIRONMENT)]
    assert shown(client, hub["owner"], run["id"])["environment"] == ENVIRONMENT


def test_profile_environment_all_and_the_default_profile_run_on_the_host_as_before(client, hub, hub_db):
    enable(client, hub["owner"])
    made(client, hub["owner"], "anywhere", None)
    (default_run,) = dispatched(client, hub["owner"], [2])
    (anywhere_run,) = dispatched(client, hub["owner"], [4], profile="anywhere")
    old = add_worker(client, hub["owner"], "old-daemon")
    beat(client, old, features=OLD_FEATURES)
    worker = accepting(client, hub["owner"], "mac-mini")
    claimed = [claim(client, old), claim(client, worker)]
    assert {spec["id"] for spec in claimed} == {default_run["id"], anywhere_run["id"]}
    assert [spec["environment"] for spec in claimed] == [None, None]
    rows = live.sql(hub_db, select(tables.runs.c.environment).where(tables.runs.c.id.in_([s["id"] for s in claimed])))
    assert rows == [(None,), (None,)]


def test_profile_environment_the_claim_picks_the_first_of_the_allowlist_the_worker_takes(client, hub, hub_db):
    enable(client, hub["owner"])
    made(client, hub["owner"], "box-first", [ENVIRONMENT, "host"])
    made(client, hub["owner"], "host-first", ["host", ENVIRONMENT])
    (box_first,) = dispatched(client, hub["owner"], [2], profile="box-first")
    plain = add_worker(client, hub["owner"], "plain")
    beat(client, plain)
    spec = claim(client, plain)
    assert (spec["id"], spec["environment"]) == (box_first["id"], None), "it takes no box: the host is next"
    (again,) = dispatched(client, hub["owner"], [4], profile="box-first")
    worker = accepting(client, hub["owner"], "mac-mini")
    assert claim(client, worker)["environment"] == DECLARATION
    (host_first,) = dispatched(client, hub["owner"], [5], profile="host-first")
    studio = accepting(client, hub["owner"], "mac-studio")
    spec = claim(client, studio)
    assert (spec["id"], spec["environment"]) == (host_first["id"], None)
    assert of_run(hub_db, again["id"], "environment") == [(ENVIRONMENT,)]


def test_profile_environment_a_version_from_before_the_axis_runs_on_the_host(client, hub, hub_db):
    made(client, hub["owner"], "older", None)
    v = tables.agent_profile_versions
    with live.engine(hub_db).begin() as conn:  # as 0030 to 0039 wrote it: seven axes
        conn.execute(update(v).values(body=v.c.body.delete_path(["permissions", "environment"])))
    (run,) = dispatched(client, hub["owner"], [2], profile="older")
    worker = accepting(client, hub["owner"], "mac-mini")
    spec = claim(client, worker)
    assert (spec["id"], spec["environment"]) == (run["id"], None)
    assert "environment" not in spec["profile"]["permissions"]


def test_profile_environment_a_run_whose_plugin_was_disabled_before_its_claim_fails_there(client, hub, hub_db):
    enable(client, hub["owner"])
    made(client, hub["owner"], "boxed", [ENVIRONMENT])
    (run,) = dispatched(client, hub["owner"], [2], profile="boxed")
    worker = accepting(client, hub["owner"], "mac-mini")
    off = client.post(f"/v1/projects/{PROJECT}/plugins/sandbox/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    assert claim(client, worker) is None, "no project it serves enables sandbox now, so it takes no box"
    assert state_of(hub_db, run["id"]) == "queued", "the run waits for a worker that takes it"

    # Another project the worker serves still enables sandbox: the worker takes the box, which the run's project no
    # longer registers.
    registered = client.put("/v1/projects/second", json=registration(), headers=hub["admin"])
    assert registered.status_code == 200, registered.text
    granted = client.put("/v1/admin/projects/second/grants/" + OWNER, json=ADMIN_GRANT, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    on = client.post("/v1/projects/second/plugins/sandbox/enable", json={"permissions": []}, headers=hub["owner"])
    assert on.status_code == 200, on.text
    p, wp = tables.projects, tables.worker_projects
    with live.engine(hub_db).begin() as conn:
        second = conn.execute(select(p.c.id).where(p.c.name == "second")).scalar_one()
        conn.execute(insert(wp).values(worker_id=worker["id"], project_id=second))
    assert claim(client, worker) is None
    ((state, error),) = of_run(hub_db, run["id"], "state", "error")
    assert state == "failed"
    assert error.startswith(
        f"no environment the run's agent profile allows ({ENVIRONMENT}) is registered in project {PROJECT} now: the "
        "plugin was disabled there"
    )

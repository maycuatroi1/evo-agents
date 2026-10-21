"""Runtimes of plugins on the hub, as criterion a6 of the plan hub-agent-plugins names it: the hub checks a dispatch's
runtime against the runtimes its project registers (the core's, and <plugin>/<name> of the plugins it enabled that
declare one with acp), 422 otherwise; a worker claims a run of a plugin's runtime only when its owner accepted the
plugin (workers.plugins, PATCH /v1/workers/{id}), its daemon says it runs plugins' runtimes and its heartbeat reports
the runtime available, which it learns from the declarations the answer to its heartbeat carries; a worker that has
not accepted the plugin leaves the run queued, never failed; a run asking for any never takes a plugin's runtime; the
claim hands the declaration; the runtime axis of an agent profile names plugins' runtimes too; a run whose plugin was
disabled before its claim fails there, saying why. Runs of the core's runtimes are claimed as before
(``tests.hub.test_runs``)."""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    RUNTIMES,
    add_worker,
    audit_rows,
    claim,
    control,
    dispatch,
    dispatched,
    members,
    of_run,
    report,
)
from tests.plugins.samples import manifest

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
RUNTIME = "sandbox/fake"
FAKE = {
    "name": "fake",
    "title": "Fake ACP agent",
    "acp": {"command": ["fake-acp", "--stdio"]},
    "models": ["small", "large"],
    "secrets": ["FAKE_API_KEY"],
}
DECLARATION = {
    "runtime": RUNTIME,
    "plugin": "sandbox",
    "version": "1.0.0",
    "title": "Fake ACP agent",
    "command": ["fake-acp", "--stdio"],
    "models": ["small", "large"],
    "secrets": ["FAKE_API_KEY"],
}
FEATURES = list(runs.WORKER_FEATURES)
OLD_FEATURES = [feature for feature in FEATURES if feature != runs.PLUGIN_RUNTIMES]
FOUND = {"available": True, "version": None, "reason": None, "models": ["small", "large"]}


def install(
    db, *, runtimes=(FAKE,), name: str = "sandbox", version: str = "1.0.0", contributes=None, permissions=None
) -> int:
    """Plugin ``name`` installed at ``version``, declaring ``runtimes`` (or ``contributes`` whole) and asking for
    ``permissions``, as the store would hold it."""
    p, v, users = tables.plugins, tables.plugin_versions, tables.users
    contributes = {"runtimes": list(runtimes)} if contributes is None else contributes
    data = manifest(id=name, version=version, permissions=permissions, config=None, contributes=contributes)
    with live.engine(db).begin() as conn:
        admin_id = conn.execute(select(users.c.id).where(users.c.login == live.ADMIN)).scalar_one()
        plugin_id = conn.execute(insert(p).values(name=name, publisher="acme").returning(p.c.id)).scalar_one()
        # the bundle's digest as the store holds it: the hub reads a version's manifest once per plugin and digest
        sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        row = {"plugin_id": plugin_id, "version": version, "sha256": sha, "size": 10, "r2_key": f"blobs/sha256/{sha}"}
        row |= {"publisher": "acme", "tier": "isolated", "manifest": data, "signature": {"kind": "ed25519"}}
        row |= {"signature_kind": "ed25519", "signed_by": "ed25519:0123", "published_by": admin_id}
        conn.execute(insert(v).values(row))
        installed = {"installed_version": version, "installed_by": admin_id, "installed_at": func.now()}
        conn.execute(update(p).where(p.c.id == plugin_id).values(installed))
    return plugin_id


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """The members of ``tests.hub.test_runs``, owner an admin of the project, and plugin sandbox installed with its
    runtime fake, not enabled yet."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    install(hub_db)
    return headers


def enable(client, headers, name: str = "sandbox") -> None:
    response = client.post(f"/v1/projects/{PROJECT}/plugins/{name}/enable", json={"permissions": []}, headers=headers)
    assert response.status_code == 200, response.text


def beat(client, worker: dict, *, features=FEATURES, runs_held=()) -> dict:
    """A heartbeat of a daemon that says ``features``, reporting the worker's runtimes."""
    body = {
        "runtimes": worker["runtimes"],
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": list(runs_held),
        "features": list(features),
        "run_kinds": list(runs.RUN_KINDS),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def set_plugins(client, headers, worker: dict, plugins: list[str]):
    return client.patch(f"/v1/workers/{worker['id']}", json={"plugins": plugins}, headers=headers)


def message(response, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["message"]


def state_of(db, run_id: int) -> str:
    return of_run(db, run_id, "state")[0][0]


# The registry


def test_a_dispatch_of_a_runtime_the_project_does_not_register_is_422_and_one_it_registers_is_queued(
    client, hub, hub_db
):
    refused = message(dispatch(client, hub["owner"], [2], runtime=RUNTIME), 422)
    assert refused == (
        f"runtime {RUNTIME} is not registered in project {PROJECT}: plugin sandbox is not enabled in project "
        f"{PROJECT}, or declares no runtime fake; it takes claude-code, opencode, codex; nothing was dispatched"
    )
    for name in ("gemini", "Bad Name", "sandbox/fake/x"):  # neither of the core nor <plugin>/<name>
        assert dispatch(client, hub["owner"], [2], runtime=name).status_code == 422, name
    assert not live.sql(hub_db, select(tables.runs.c.id)), "nothing was dispatched"
    enable(client, hub["owner"])
    other = message(dispatch(client, hub["owner"], [2], runtime="sandbox/other"), 422)
    assert f"it takes claude-code, opencode, codex, {RUNTIME}" in other
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME)
    assert (run["requested_runtime"], run["runtime"], run["state"]) == (RUNTIME, RUNTIME, "queued")
    (core,) = dispatched(client, hub["owner"], [4], runtime="codex")
    assert core["runtime"] == "codex"


def test_a_plan_run_and_a_rerun_check_the_runtime_against_the_registry_too(client, hub, hub_db):
    body = {"plan_id": "rollout", "runtime": RUNTIME}
    refused = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=hub["owner"])
    assert "is not registered in project" in message(refused, 422)
    enable(client, hub["owner"])
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME)
    assert control(client, hub["owner"], run["id"], "cancel").status_code == 200
    off = client.post(f"/v1/projects/{PROJECT}/plugins/sandbox/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    assert "is not registered in project" in message(control(client, hub["owner"], run["id"], "rerun"), 422)


# Which worker claims a run of a plugin's runtime


def test_only_a_worker_whose_owner_accepted_the_plugin_and_whose_daemon_runs_it_claims_the_run(client, hub, hub_db):
    enable(client, hub["owner"])
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME)
    old = add_worker(client, hub["owner"], "old-daemon", runtimes={**RUNTIMES, RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], old, ["sandbox"]).status_code == 200
    answer = beat(client, old, features=OLD_FEATURES)
    assert answer["settings"]["plugins"] == ["sandbox"] and answer["plugin_runtimes"] == []
    assert claim(client, old) is None, "a daemon that does not say plugin_runtimes takes no such run"

    new = add_worker(client, hub["owner"], "new-daemon")
    answer = beat(client, new)
    assert answer["settings"]["plugins"] == [] and answer["plugin_runtimes"] == []
    new["runtimes"] = {**RUNTIMES, RUNTIME: FOUND}  # as if it found the program anyway
    beat(client, new)
    assert claim(client, new) is None, "its owner did not accept the plugin"
    assert state_of(hub_db, run["id"]) == "queued", "the run waits; it does not fail"

    changed = set_plugins(client, hub["owner"], new, ["sandbox", "sandbox"])
    assert changed.status_code == 200 and changed.json()["plugins"] == ["sandbox"]
    new["runtimes"] = RUNTIMES
    answer = beat(client, new)
    assert answer["settings"]["plugins"] == ["sandbox"] and answer["plugin_runtimes"] == [DECLARATION]
    assert claim(client, new) is None, "its heartbeat does not report the runtime yet"
    new["runtimes"] = {**RUNTIMES, RUNTIME: FOUND}
    beat(client, new)
    spec = claim(client, new)
    assert (spec["id"], spec["runtime"], spec["plugin_runtime"]) == (run["id"], RUNTIME, DECLARATION)
    assert of_run(hub_db, run["id"], "worker_id", "runtime") == [(new["id"], RUNTIME)]
    rp, p = tables.run_plugins, tables.plugins
    recorded = live.sql(
        hub_db,
        select(p.c.name, rp.c.version).join_from(rp, p, p.c.id == rp.c.plugin_id).where(rp.c.run_id == run["id"]),
    )
    assert recorded == [("sandbox", "1.0.0")]
    assert report(client, new, run["id"], "running").status_code == 200


def test_a_run_asking_for_any_takes_a_runtime_of_the_core_never_a_plugins(client, hub, hub_db):
    enable(client, hub["owner"])
    plugin_only = add_worker(client, hub["owner"], "plugin-only", runtimes={RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], plugin_only, ["sandbox"]).status_code == 200
    beat(client, plugin_only)
    (run,) = dispatched(client, hub["owner"], [2])
    assert claim(client, plugin_only) is None
    both = add_worker(client, hub["owner"], "both", runtimes={**RUNTIMES, RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], both, ["sandbox"]).status_code == 200
    beat(client, both)
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"], spec["plugin_runtime"]) == (run["id"], "claude-code", None)


def test_a_dispatch_pinned_to_a_worker_that_cannot_take_the_plugins_runtime_is_409_saying_why(client, hub, hub_db):
    enable(client, hub["owner"])
    worker = add_worker(client, hub["owner"], "mac-mini")

    def why() -> str:
        return message(dispatch(client, hub["owner"], [2], runtime=RUNTIME, worker_id=worker["id"]), 409)

    beat(client, worker, features=OLD_FEATURES)
    assert "does not say it runs the runtimes of plugins: upgrade it and restart its daemon" in why()
    beat(client, worker)
    assert "its owner has not let it take the runtimes of plugin sandbox: add sandbox to the worker's plugins" in why()
    assert set_plugins(client, hub["owner"], worker, ["sandbox"]).status_code == 200
    worker["runtimes"] = {**RUNTIMES, RUNTIME: {"available": False, "reason": "fake-acp is not on PATH"}}
    beat(client, worker)
    assert f"it does not report {RUNTIME} available (fake-acp is not on PATH)" in why()
    worker["runtimes"] = {**RUNTIMES, RUNTIME: FOUND}
    beat(client, worker)
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME, worker_id=worker["id"])
    assert claim(client, worker)["id"] == run["id"]


def test_a_run_whose_plugin_was_disabled_before_its_claim_fails_there_saying_why(client, hub, hub_db):
    enable(client, hub["owner"])
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME)
    worker = add_worker(client, hub["owner"], "mac-mini", runtimes={**RUNTIMES, RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], worker, ["sandbox"]).status_code == 200
    beat(client, worker)
    off = client.post(f"/v1/projects/{PROJECT}/plugins/sandbox/disable", headers=hub["owner"])
    assert off.status_code == 200, off.text
    assert beat(client, worker)["plugin_runtimes"] == [], "no project it serves enables sandbox now"
    assert claim(client, worker) is None
    ((state, error),) = of_run(hub_db, run["id"], "state", "error")
    assert state == "failed"
    assert error.startswith(f"runtime {RUNTIME} is no longer registered in project {PROJECT}: plugin sandbox was ")


# The runtime axis of an agent profile


def profile(client, headers, handle: str, runtimes: list[str]):
    body = {"handle": handle, "name": handle, "permissions": {"runtime": {"mode": "allowlist", "allow": runtimes}}}
    return client.post(f"/v1/projects/{PROJECT}/agents", json=body, headers=headers)


def test_the_runtime_axis_of_an_agent_profile_names_and_limits_plugins_runtimes(client, hub, hub_db):
    enable(client, hub["owner"])
    assert profile(client, hub["owner"], "core-only", ["claude-code"]).status_code == 201
    assert profile(client, hub["owner"], "fake-only", [RUNTIME]).status_code == 201
    bad = profile(client, hub["owner"], "gemini", ["gemini"])
    assert bad.status_code == 422
    assert "runtime 'gemini' is none of claude-code, opencode, codex, nor a plugin's <plugin>/<name>" in bad.text
    refused = message(dispatch(client, hub["owner"], [2], runtime=RUNTIME, profile="core-only"), 422)
    assert (
        refused == f"agent @core-only does not allow runtime {RUNTIME}: it allows claude-code; nothing was dispatched"
    )
    any_run = message(dispatch(client, hub["owner"], [2], profile="fake-only"), 422)
    assert any_run == (
        f"agent @fake-only allows only {RUNTIME}, and a run asking for any takes a runtime of the core: name one; "
        "nothing was dispatched"
    )
    (run,) = dispatched(client, hub["owner"], [2], runtime=RUNTIME, profile="fake-only")
    (core,) = dispatched(client, hub["owner"], [4], profile="core-only")
    worker = add_worker(client, hub["owner"], "mac-mini", runtimes={**RUNTIMES, RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], worker, ["sandbox"]).status_code == 200
    beat(client, worker)
    claimed = {claim(client, worker)["id"]}
    worker2 = add_worker(client, hub["owner"], "mac-studio", runtimes={**RUNTIMES, RUNTIME: FOUND})
    assert set_plugins(client, hub["owner"], worker2, ["sandbox"]).status_code == 200
    beat(client, worker2)
    claimed.add(claim(client, worker2)["id"])
    assert claimed == {run["id"], core["id"]}
    runtimes_of = dict(
        live.sql(hub_db, select(tables.runs.c.id, tables.runs.c.runtime).where(tables.runs.c.id.in_(list(claimed))))
    )
    assert runtimes_of == {run["id"]: RUNTIME, core["id"]: "claude-code"}


# The worker's plugins


def test_the_plugins_of_a_worker_are_its_owners_bounded_and_audited(client, hub, hub_db):
    worker = add_worker(client, hub["owner"], "mac-mini")
    shown = client.get(f"/v1/workers/{worker['id']}", headers=hub["owner"]).json()
    assert shown["plugins"] == []
    changed = set_plugins(client, hub["owner"], worker, ["sandbox", "srs-docs"])
    assert changed.status_code == 200 and changed.json()["plugins"] == ["sandbox", "srs-docs"]
    assert set_plugins(client, hub["owner"], worker, ["Sandbox"]).status_code == 422
    assert set_plugins(client, hub["owner"], worker, [f"p{i}" for i in range(17)]).status_code == 422
    assert set_plugins(client, hub["other"], worker, ["sandbox"]).status_code == 404
    assert set_plugins(client, hub["admin"], worker, ["sandbox"]).status_code == 403
    assert set_plugins(client, hub["owner"], worker, ["sandbox", "srs-docs"]).status_code == 200  # nothing changes
    assert set_plugins(client, hub["owner"], worker, []).json()["plugins"] == []
    rows = [row for row in audit_rows(hub_db, "worker") if row[0] == "worker.update"]
    assert [row[1].rsplit(" ", 1)[1] for row in rows] == [
        "plugins=[]->[sandbox,srs-docs]",
        "plugins=[sandbox,srs-docs]->[]",
    ]

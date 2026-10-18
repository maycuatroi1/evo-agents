"""Agent packs of plugins on agent profiles, on the hub, as criterion a8 of the plan hub-agent-plugins names it: a
profile attaches packs of the plugins its project enabled, each pinned at a version of its plugin the hub holds as a
bundle that declares the pack; editing them raises the profile's version, and the pin holds when the plugin moves on;
the default profile attaches none; a worker claims a run whose profile attaches packs only when its daemon says it
builds them and its owner accepted each plugin, else the run waits, queued; the claim hands each pack with a presigned
GET of its bundle; a run whose pack the hub can no longer hand fails at the claim, saying why; a dispatch pinned to a
worker that could never take the run is 409. What the worker builds from a pack is ``tests.worker.test_agent_packs``'s.
"""

from __future__ import annotations

import argparse
import hashlib
import json

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from evo_agents.hub import profiles, runs, tables
from evo_agents.hub.cli_agents import packs_from
from evo_agents.hub.server.app import create_app
from tests.hub.test_plugin_runtimes import ADMIN_GRANT, beat, enable, install, message, set_plugins, state_of
from tests.hub.test_runs import OWNER, PROJECT, add_worker, claim, dispatch, dispatched, members, of_run
from tests.plugins.samples import manifest

REVIEW = {"name": "review", "title": "Code review", "description": "Skills and MCP servers that review a diff"}
PACK = "sandbox/review"
FEATURES = list(runs.WORKER_FEATURES)
OLD_FEATURES = [feature for feature in FEATURES if feature != runs.AGENT_PACKS]


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """The members of ``tests.hub.test_runs``, owner an admin of the project, and plugin sandbox 1.0.0 installed with
    its pack review, not enabled yet."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    headers["plugin_id"] = install(hub_db, contributes={"agent_packs": [REVIEW]})
    return headers


def add_version(db, plugin_id: int, version: str, *, contributes=None, image: bool = False, **changes) -> str:
    """Version ``version`` of plugin sandbox held by the hub, as a bundle (or, with ``image``, as a core plugin that
    comes with the hub's image), declaring ``contributes`` (pack review by default); its digest."""
    v, users = tables.plugin_versions, tables.users
    contributes = {"agent_packs": [REVIEW]} if contributes is None else contributes
    data = manifest(id="sandbox", version=version, permissions=None, config=None, contributes=contributes, **changes)
    sha = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    with live.engine(db).begin() as conn:
        admin_id = conn.execute(select(users.c.id).where(users.c.login == live.ADMIN)).scalar_one()
        row = {"plugin_id": plugin_id, "version": version, "sha256": sha, "size": 20, "manifest": data}
        row |= {"r2_key": f"image:{sha}" if image else f"blobs/sha256/{sha}", "publisher": "acme", "tier": "isolated"}
        if image:  # as plugin_store._image_version writes a core plugin of the image
            row |= {"signature": {}, "signature_kind": "image", "signed_by": "image:x", "published_by": None}
        else:
            row |= {"signature": {"kind": "ed25519"}, "signature_kind": "ed25519", "signed_by": "ed25519:0123"}
            row |= {"published_by": admin_id}
        conn.execute(insert(v).values(row))
    return sha


def digest_of(db, plugin_id: int, version: str) -> str:
    v = tables.plugin_versions
    return live.sql(db, select(v.c.sha256).where(v.c.plugin_id == plugin_id, v.c.version == version))[0][0]


def agent(client, headers, handle: str, packs: list[dict] | None = None, **body):
    """POST of profile ``handle`` that attaches ``packs``."""
    body = {"handle": handle, "name": handle, **body, **({} if packs is None else {"packs": packs})}
    return client.post(f"/v1/projects/{PROJECT}/agents", json=body, headers=headers)


def made(client, headers, handle: str, packs: list[dict]) -> dict:
    response = agent(client, headers, handle, packs)
    assert response.status_code == 201, response.text
    return response.json()


def edited(client, headers, handle: str, **body):
    return client.patch(f"/v1/projects/{PROJECT}/agents/{handle}", json=body, headers=headers)


def pinned(version: str = "1.0.0", name: str = "review") -> dict:
    return {"pack": f"sandbox/{name}", "plugin": "sandbox", "name": name, "version": version}


def accepting(client, headers, name: str, *, features=FEATURES) -> dict:
    """A worker whose owner accepted plugin sandbox and whose daemon says ``features``."""
    worker = add_worker(client, headers, name)
    assert set_plugins(client, headers, worker, ["sandbox"]).status_code == 200
    beat(client, worker, features=features)
    return worker


# Attaching


def test_profile_pack_is_attached_at_a_pinned_version_and_editing_the_packs_raises_the_version(client, hub, hub_db):
    enable(client, hub["owner"])
    add_version(hub_db, hub["plugin_id"], "1.1.0")
    created = made(client, hub["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
    assert (created["packs"], created["version"]) == ([pinned()], 1), "a pin without a version takes the installed one"
    plain = made(client, hub["owner"], "plain", [])
    assert plain["packs"] == [] and agent(client, hub["owner"], "bare").json()["packs"] == []

    moved = edited(
        client, hub["owner"], "reviewer", packs=[{"plugin": "sandbox", "name": "review", "version": "1.1.0"}]
    )
    assert moved.status_code == 200, moved.text
    assert (moved.json()["packs"], moved.json()["version"]) == ([pinned("1.1.0")], 2)
    same = edited(client, hub["owner"], "reviewer", packs=[{"plugin": "sandbox", "name": "review", "version": "1.1.0"}])
    assert same.json()["version"] == 2, "the same packs change nothing"
    renamed = edited(client, hub["owner"], "reviewer", name="Reviewer")
    assert (renamed.json()["version"], renamed.json()["packs"]) == (3, [pinned("1.1.0")]), "an edit leaves the packs"
    dropped = edited(client, hub["owner"], "reviewer", packs=[])
    assert (dropped.json()["version"], dropped.json()["packs"]) == (4, [])
    versions = client.get(f"/v1/projects/{PROJECT}/agents/reviewer/versions", headers=hub["reader"]).json()
    bodies = {item["version"]: item["body"]["packs"] for item in versions}
    assert bodies == {
        4: [],
        3: [{"plugin": "sandbox", "name": "review", "version": "1.1.0"}],
        2: [{"plugin": "sandbox", "name": "review", "version": "1.1.0"}],
        1: [{"plugin": "sandbox", "name": "review", "version": "1.0.0"}],
    }

    # The plugin moves on: the pin holds until an edit moves it.
    with live.engine(hub_db).begin() as conn:
        conn.execute(update(tables.plugins).values(installed_version="1.1.0").where(tables.plugins.c.name == "sandbox"))
    kept = client.get(f"/v1/projects/{PROJECT}/agents/reviewer/versions", headers=hub["reader"]).json()
    assert kept[-1]["body"]["packs"] == [{"plugin": "sandbox", "name": "review", "version": "1.0.0"}]
    newer = made(client, hub["owner"], "newer", [{"plugin": "sandbox", "name": "review"}])
    assert newer["packs"] == [pinned("1.1.0")], "a pin without a version takes the version installed now"


def test_profile_pack_attach_refuses_what_the_hub_cannot_hand_a_worker_with_422(client, hub, hub_db):
    def refused(packs: list[dict], handle: str = "refused") -> str:
        return message(agent(client, hub["owner"], handle, packs), 422)

    review = {"plugin": "sandbox", "name": "review"}
    assert refused([review]) == (
        f"agent pack {PACK}: plugin sandbox is not enabled in project {PROJECT}, or it is turned off; see GET "
        f"/v1/projects/{PROJECT}/agent-packs; nothing changed"
    )
    enable(client, hub["owner"])
    add_version(hub_db, hub["plugin_id"], "0.9.0", contributes={"routes": [{"name": "hello"}]})
    add_version(hub_db, hub["plugin_id"], "0.8.0", image=True)
    add_version(hub_db, hub["plugin_id"], "0.7.0", engines={"evo-agents": ">=99"})
    assert refused([{**review, "version": "2.0.0"}]) == (
        f"agent pack {PACK}: the hub holds no version 2.0.0 of plugin sandbox; nothing changed"
    )
    assert refused([{**review, "version": "0.9.0"}]) == (
        f"agent pack {PACK}: version 0.9.0 of plugin sandbox declares no agent pack review; nothing changed"
    )
    assert refused([{**review, "version": "0.8.0"}]).startswith(
        f"agent pack {PACK}: version 0.8.0 of plugin sandbox comes with the hub's image, with no bundle a worker"
    )
    assert refused([{**review, "version": "0.7.0"}]).startswith(
        f"agent pack {PACK}: version 0.7.0 of plugin sandbox does not run on this hub: "
    )
    assert refused([{"plugin": "sandbox", "name": "lint"}]) == (
        "agent pack sandbox/lint: version 1.0.0 of plugin sandbox declares no agent pack lint; nothing changed"
    )
    assert refused([review, {**review, "version": "1.0.0"}]) == (
        f"agent pack {PACK} is attached twice: attach each pack once; nothing changed"
    )
    too_many = agent(client, hub["owner"], "many", [{"plugin": "sandbox", "name": f"p{n}"} for n in range(17)])
    assert too_many.status_code == 422
    unknown = agent(client, hub["owner"], "odd", [{**review, "extra": 1}])
    assert unknown.status_code == 422
    assert not live.sql(hub_db, select(tables.agent_profiles.c.id).where(tables.agent_profiles.c.handle == "refused"))

    made(client, hub["owner"], "reviewer", [])
    assert message(edited(client, hub["owner"], "reviewer", packs=[{**review, "version": "0.9.0"}]), 422).endswith(
        "declares no agent pack review; nothing changed"
    )
    default = message(edited(client, hub["owner"], PROJECT, packs=[review]), 422)
    assert default == (
        f"@{PROJECT} is the default agent of project {PROJECT}: it has no instructions and attaches no agent pack, so "
        "a run that names no agent runs as before; create another agent to give it some"
    )
    assert edited(client, hub["owner"], PROJECT, packs=[]).status_code == 200, "no pack is what it has"
    reader = edited(client, hub["reader"], "reviewer", packs=[review])
    assert reader.status_code == 403


def test_profile_pack_list_names_the_packs_of_the_projects_plugins_at_the_versions_a_profile_may_pin(
    client, hub, hub_db
):
    path = f"/v1/projects/{PROJECT}/agent-packs"
    assert client.get(path, headers=hub["reader"]).json() == [], "sandbox is not enabled yet"
    enable(client, hub["owner"])
    add_version(hub_db, hub["plugin_id"], "1.2.0")
    add_version(hub_db, hub["plugin_id"], "1.10.0")
    add_version(hub_db, hub["plugin_id"], "0.9.0", contributes={"routes": [{"name": "hello"}]})
    add_version(hub_db, hub["plugin_id"], "0.8.0", image=True)
    listed = client.get(path, headers=hub["reader"])
    assert listed.status_code == 200, listed.text
    assert listed.json() == [
        {
            "pack": PACK,
            "plugin": "sandbox",
            "name": "review",
            "title": "Code review",
            "description": "Skills and MCP servers that review a diff",
            "installed_version": "1.0.0",
            "versions": ["1.10.0", "1.2.0", "1.0.0"],
        }
    ]
    assert client.get(path, headers=hub["admin"]).status_code == 403, "the hub's admin holds no grant on the project"
    assert client.get(path, headers=hub["stranger"]).status_code == 404


# Claiming


def test_profile_pack_only_a_worker_that_builds_packs_and_accepted_the_plugin_claims_the_run(client, hub, hub_db, s3):
    enable(client, hub["owner"])
    made(client, hub["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
    (run,) = dispatched(client, hub["owner"], [2], profile="reviewer")
    old = accepting(client, hub["owner"], "old-daemon", features=OLD_FEATURES)
    assert claim(client, old) is None, "a daemon that does not say it builds agent packs takes no such run"
    new = add_worker(client, hub["owner"], "new-daemon")
    beat(client, new)
    assert claim(client, new) is None, "its owner did not accept the plugin"
    assert state_of(hub_db, run["id"]) == "queued", "the run waits; it does not fail"
    (plain,) = dispatched(client, hub["owner"], [4])
    assert claim(client, old)["id"] == plain["id"], "a run that attaches no pack goes to any worker, as before"

    assert set_plugins(client, hub["owner"], new, ["sandbox"]).status_code == 200
    spec = claim(client, new)
    assert spec["id"] == run["id"]
    (ticket,) = spec["packs"]
    sha = digest_of(hub_db, hub["plugin_id"], "1.0.0")
    assert {key: ticket[key] for key in ("pack", "plugin", "name", "version", "sha256", "size")} == {
        **pinned(),
        "sha256": sha,
        "size": 10,
    }
    assert f"blobs/sha256/{sha}" in ticket["url"] and s3.bucket in ticket["url"]
    assert of_run(hub_db, run["id"], "worker_id") == [(new["id"],)]


def test_profile_pack_a_run_keeps_the_packs_of_the_version_it_was_dispatched_under(client, hub, hub_db):
    enable(client, hub["owner"])
    add_version(hub_db, hub["plugin_id"], "1.1.0")
    made(client, hub["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
    (run,) = dispatched(client, hub["owner"], [2], profile="reviewer")
    moved = edited(
        client, hub["owner"], "reviewer", packs=[{"plugin": "sandbox", "name": "review", "version": "1.1.0"}]
    )
    assert moved.json()["version"] == 2
    worker = accepting(client, hub["owner"], "mac-mini")
    spec = claim(client, worker)
    assert (spec["id"], [ticket["version"] for ticket in spec["packs"]]) == (run["id"], ["1.0.0"])
    (later,) = dispatched(client, hub["owner"], [4], profile="reviewer")
    spec = claim(client, accepting(client, hub["owner"], "mac-studio"))
    assert (spec["id"], [ticket["version"] for ticket in spec["packs"]]) == (later["id"], ["1.1.0"])


def test_profile_pack_a_run_whose_pack_the_hub_cannot_hand_fails_at_its_claim_saying_why(client, hub, hub_db):
    enable(client, hub["owner"])
    add_version(hub_db, hub["plugin_id"], "1.1.0")
    made(client, hub["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
    made(client, hub["owner"], "newer", [{"plugin": "sandbox", "name": "review", "version": "1.1.0"}])
    worker = accepting(client, hub["owner"], "mac-mini")

    (gone,) = dispatched(client, hub["owner"], [2], profile="newer")
    v = tables.plugin_versions
    with live.engine(hub_db).begin() as conn:
        conn.execute(v.delete().where(v.c.version == "1.1.0"))
    assert claim(client, worker) is None
    ((state, error),) = of_run(hub_db, gone["id"], "state", "error")
    assert state == "failed"
    assert error.startswith(
        "the hub no longer holds version 1.1.0 of plugin sandbox with agent pack review, which the run's agent profile "
        "pins"
    )

    (off,) = dispatched(client, hub["owner"], [4], profile="reviewer")
    disabled = client.post(f"/v1/projects/{PROJECT}/plugins/sandbox/disable", headers=hub["owner"])
    assert disabled.status_code == 200, disabled.text
    assert claim(client, worker) is None
    ((state, error),) = of_run(hub_db, off["id"], "state", "error")
    assert state == "failed"
    assert error.startswith(f"plugin sandbox of agent pack {PACK} is no longer enabled in project {PROJECT}")


def test_profile_pack_a_hub_without_a_blob_store_fails_the_run_at_its_claim(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        headers = members(client, github)
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        install(hub_db, contributes={"agent_packs": [REVIEW]})
        enable(client, headers["owner"])
        made(client, headers["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
        (run,) = dispatched(client, headers["owner"], [2], profile="reviewer")
        worker = accepting(client, headers["owner"], "mac-mini")
        assert claim(client, worker) is None
    ((state, error),) = of_run(hub_db, run["id"], "state", "error")
    assert state == "failed" and error.startswith("the hub has no blob store, so it cannot hand the worker the bundles")


# Dispatching


def test_profile_pack_a_dispatch_pinned_to_a_worker_that_cannot_build_the_packs_is_409_saying_why(client, hub, hub_db):
    enable(client, hub["owner"])
    made(client, hub["owner"], "reviewer", [{"plugin": "sandbox", "name": "review"}])
    worker = add_worker(client, hub["owner"], "mac-mini")

    def why() -> str:
        return message(dispatch(client, hub["owner"], [2], profile="reviewer", worker_id=worker["id"]), 409)

    beat(client, worker, features=OLD_FEATURES)
    assert "does not say it builds agent packs, and the run's agent attaches some" in why()
    beat(client, worker)
    assert "its owner has not let it take the agent packs of plugin sandbox: add it to the worker's plugins" in why()
    assert set_plugins(client, hub["owner"], worker, ["sandbox"]).status_code == 200
    (run,) = dispatched(client, hub["owner"], [2], profile="reviewer", worker_id=worker["id"])
    assert claim(client, worker)["id"] == run["id"]


# Without a hub


def test_profile_pack_unfit_says_why_a_worker_cannot_build_a_profiles_packs():
    packs = [
        {"plugin": "sandbox", "name": "review", "version": "1.0.0"},
        {"plugin": "docs", "name": "x", "version": "2"},
    ]
    unfit = {"worker_id": 7, "agent_version": "0.16.1", "features": FEATURES, "plugins": ["sandbox"]}
    assert profiles.packs_unfit([], **unfit) is None and profiles.packs_unfit(None, **unfit) is None
    assert profiles.packs_unfit(packs, **unfit) == (
        "its owner has not let it take the agent packs of plugin docs: add it to the worker's plugins, on its page or "
        "with PATCH /v1/workers/7"
    )
    assert profiles.packs_unfit(packs, **{**unfit, "plugins": ["docs", "sandbox"]}) is None
    old = profiles.packs_unfit(packs, **{**unfit, "features": OLD_FEATURES, "agent_version": None})
    assert old == (
        "its daemon (evo-agents of an unknown version) does not say it builds agent packs, and the run's agent attaches "
        "some: upgrade it and restart its daemon"
    )
    assert profiles.pack_plugins(packs) == ["docs", "sandbox"]


@pytest.mark.parametrize(
    ("given", "no_packs", "expected"),
    [
        ([], False, None),
        ([], True, []),
        (["sandbox/review"], False, [{"plugin": "sandbox", "name": "review"}]),
        (
            ["sandbox/review@1.2.0", "docs/x"],
            False,
            [{"plugin": "sandbox", "name": "review", "version": "1.2.0"}] + [{"plugin": "docs", "name": "x"}],
        ),
    ],
)
def test_profile_pack_options_of_the_command_line(given, no_packs, expected):
    assert packs_from(argparse.Namespace(pack=given, no_packs=no_packs)) == expected


@pytest.mark.parametrize("given", ["sandbox", "sandbox/", "/review", "sandbox/review/x", "sandbox/review@"])
def test_profile_pack_options_refuse_what_does_not_read(given):
    with pytest.raises(ValueError, match="is not PLUGIN/NAME or PLUGIN/NAME@VERSION"):
        packs_from(argparse.Namespace(pack=[given], no_packs=False))

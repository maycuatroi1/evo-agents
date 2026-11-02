"""The core plugin guard on the hub (``evo_agents.core_plugins.guard``, ``docs/guard.md``): an admin of a project that
enables it keeps the project's private names and published repos, a writer reads them and changes nothing, the policy
every worker and member gets holds the terms hashed and never in clear, a worker's heartbeat carries its digest, and a
pull request the hub opens on a guarded repo loses the names. The names are fictional (northwind, lumen).
"""

from __future__ import annotations

import dataclasses
from importlib.metadata import EntryPoint

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub import guard, plugin_calls
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from evo_agents.private_names import Policy, entries
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import ADMIN, bearer
from tests.hub.test_runs import PROJECT, audit_rows, beat

app_key = credentials_api.app_key  # a fixture
OWNER, OTHER = credentials_api.OWNER, credentials_api.OTHER
GUARD = EntryPoint("guard", "evo_agents.core_plugins.guard:MANIFEST", ENTRY_POINT_GROUP)
LISTS = f"/v1/x/guard/{PROJECT}"


@pytest.fixture
def guard_core(monkeypatch):
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name != "guard"] + [GUARD]
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, app_key, guard_core):  # noqa: F811
    config = credentials_api.hub_config(hub_db, tmp_path, github, app_key)
    with TestClient(create_app(dataclasses.replace(config)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project of ``test_credentials_api`` with plugin guard enabled, owner its admin and someone-else a writer."""
    headers = {
        "admin": bearer(live.sign_in(client, github, ADMIN, credentials_api.ADMIN_ID)["token"]),
        "owner": bearer(live.sign_in(client, github, OWNER, credentials_api.OWNER_ID)["token"]),
        "other": bearer(live.sign_in(client, github, OTHER, credentials_api.OTHER_ID)["token"]),
    }
    body = credentials_api.registration(credentials_api.DISPATCHED)
    assert client.put(f"/v1/projects/{PROJECT}", json=body, headers=headers["admin"]).status_code == 200
    for login, grant in ((OWNER, {"role": "admin", "max_level": "internal"}), (OTHER, credentials_api.WRITER)):
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert response.status_code == 200, response.text
    enabled = client.post(
        f"/v1/projects/{PROJECT}/plugins/guard/enable", json={"permissions": []}, headers=headers["owner"]
    )
    assert enabled.status_code == 200, enabled.text
    return headers


def test_an_admin_keeps_the_lists_a_writer_reads_them_and_each_change_is_audited_without_the_term(client, hub, hub_db):
    made = client.post(f"{LISTS}/terms", json={"term": "Northwind-Ops", "note": "a client"}, headers=hub["owner"])
    assert made.status_code == 201, made.text
    assert made.json()["words"] == "northwind ops"
    again = client.post(f"{LISTS}/terms", json={"term": "northwind ops"}, headers=hub["owner"])
    assert again.status_code == 409
    assert client.post(f"{LISTS}/terms", json={"term": "--"}, headers=hub["owner"]).status_code == 422
    refused = client.post(f"{LISTS}/terms", json={"term": "lumen"}, headers=hub["other"])
    assert refused.status_code == 403 and "admin role" in refused.json()["message"]
    target = client.post(f"{LISTS}/targets", json={"pattern": "git@github.com:Acme/Public.git"}, headers=hub["owner"])
    assert target.status_code == 201 and target.json()["pattern"] == "github.com/acme/public"
    wild = client.post(f"{LISTS}/targets", json={"pattern": "github.com/acme/open-*"}, headers=hub["owner"])
    assert wild.status_code == 201, wild.text
    assert client.post(f"{LISTS}/targets", json={"pattern": "nothing"}, headers=hub["owner"]).status_code == 422
    listed = client.get(f"{LISTS}/terms", headers=hub["other"]).json()
    assert [t["term"] for t in listed] == ["Northwind-Ops"]
    assert [t["pattern"] for t in client.get(f"{LISTS}/targets", headers=hub["other"]).json()] == [
        "github.com/acme/open-*",
        "github.com/acme/public",
    ]
    term_id = made.json()["id"]
    assert client.delete(f"{LISTS}/terms/{term_id}", headers=hub["other"]).status_code == 403
    assert client.delete(f"{LISTS}/terms/{term_id}", headers=hub["owner"]).json()["term"] == "Northwind-Ops"
    assert client.delete(f"{LISTS}/terms/{term_id}", headers=hub["owner"]).status_code == 404
    rows = audit_rows(hub_db, "guard-term") + audit_rows(hub_db, "guard-target")
    assert {action for action, *_ in rows} == {"guard-term.add", "guard-term.remove", "guard-target.add"}
    assert all("northwind" not in target.lower() and login == OWNER for _, target, login, _ in rows)


def test_the_policy_holds_the_terms_hashed_and_a_heartbeat_carries_its_digest(client, hub):
    worker = credentials_api.worker_of(client, hub["owner"], "box")
    assert beat(client, worker)["settings"]["guard"] == ""  # no term nor target: nothing guarded
    client.post(f"{LISTS}/terms", json={"term": "Northwind"}, headers=hub["owner"])
    assert beat(client, worker)["settings"]["guard"] == ""  # a term and no target still guards nothing
    client.post(f"{LISTS}/targets", json={"pattern": "acme/public"}, headers=hub["owner"])
    answered = client.get("/v1/worker/guard/policy", headers=worker["headers"])
    assert answered.status_code == 200, answered.text
    data = answered.json()
    assert "northwind" not in answered.text.lower()
    assert data["digest"] and beat(client, worker)["settings"]["guard"] == data["digest"]
    assert data["terms"] == sorted(entries("Northwind"), key=lambda t: t["h"])
    assert data["targets"] == ["github.com/acme/public"]
    etag = answered.headers["etag"]
    assert client.get("/v1/guard/policy", headers={**hub["other"], "If-None-Match": etag}).status_code == 304
    policy = Policy.from_json(data)
    assert [h.found for h in policy.matcher().find("ships NorthwindCLI to Northwind")] == ["Northwind", "Northwind"]
    checked = client.post(f"{LISTS}/check", json={"text": "for northwind", "repo": "acme/other"}, headers=hub["other"])
    assert checked.json()["guarded"] is False and checked.json()["hits"][0]["found"] == "northwind"


def test_turning_the_plugin_off_in_the_project_drops_its_names_from_the_policy(client, hub):
    client.post(f"{LISTS}/terms", json={"term": "Northwind"}, headers=hub["owner"])
    client.post(f"{LISTS}/targets", json={"pattern": "acme/public"}, headers=hub["owner"])
    assert client.get("/v1/guard/policy", headers=hub["other"]).json()["digest"]
    off = client.post(f"/v1/projects/{PROJECT}/plugins/guard/disable", headers=hub["owner"])
    assert off.status_code in (200, 204), off.text
    assert client.get("/v1/guard/policy", headers=hub["other"]).json() == {
        "version": 1,
        "digest": "",
        "terms": [],
        "targets": [],
    }


def test_the_hub_takes_a_name_out_of_the_text_of_a_pull_request_on_a_guarded_repo_only():
    policy = Policy(terms=entries("Northwind") + entries("lumen ops"), targets=["github.com/acme/*"])
    title, body = guard.scrub(policy, "github.com/acme/public", "@bot in northwind: fix", "Lumen-Ops and NorthwindCLI")
    assert title == "@bot in [private]: fix"
    assert body == "[private] and [private]CLI"
    kept = guard.scrub(policy, "github.com/other/repo", "northwind")
    assert kept == ["northwind"]
    assert guard.scrub(None, "github.com/acme/public", "northwind") == ["northwind"]

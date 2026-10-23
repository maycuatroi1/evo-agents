"""Browser identities of an agent profile and the login on the worker's own machine (plan agent-browser, step 9): an
admin makes them for a profile, the first is its default, a run under the profile gets the default and its worker, the
profile's delete retires them, and a worker's owner signs one in without a run."""

import base64
import secrets

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_browser_identity import PATH, audit_rows, created, stored  # noqa: F401 (fixtures)
from tests.hub.test_browser_profile import HIDDEN, held_by, profile_archive, send, set_identity
from tests.hub.test_chats import agent
from tests.hub.test_runs import PROJECT, claim, dispatch, dispatched, of_run

LOGIN = "/v1/worker/browser-identities/login"


def agent_identity(client, headers, name: str, handle: str = "scout", **body):
    return client.post(
        PATH,
        json={
            "name": name,
            "domains": ["northwind.example"],
            "worker": "box",
            "owner": "agent",
            "agent": handle,
            **body,
        },
        headers=headers,
    )


def test_browser_identity_of_an_agent_is_an_admins_and_the_first_is_the_default(stored):
    client, headers = stored.client, stored.headers
    agent(client, headers["owner"], "scout")
    first = agent_identity(client, headers["owner"], "northwind")
    assert first.status_code == 201, first.text
    assert (first.json()["owner"], first.json()["agent"], first.json()["agent_default"]) == ("agent", "scout", True)
    assert not HIDDEN & set(first.json())
    second = agent_identity(client, headers["owner"], "northwind-2")
    assert second.json()["agent_default"] is False
    assert agent_identity(client, headers["owner"], "northwind").status_code == 409  # a name per profile
    assert agent_identity(client, headers["owner"], "x", "nobody").status_code == 404
    assert agent_identity(client, headers["other"], "y").status_code == 403  # not an admin
    mixed = client.post(
        PATH, json={"name": "z", "domains": ["a.example"], "worker": "box", "owner": "agent"}, headers=headers["owner"]
    )
    assert mixed.status_code == 422  # agent owner with no agent

    shown = client.get(PATH, headers=headers["other"]).json()
    assert all(i["owner"] != "agent" for i in shown)  # only admins see them
    moved = client.post(f"{PATH}/{second.json()['id']}/default", headers=headers["owner"])
    assert moved.status_code == 200 and moved.json()["agent_default"] is True
    listing = {
        i["name"]: i["agent_default"]
        for i in client.get(PATH, headers=headers["owner"]).json()
        if i["owner"] == "agent"
    }
    assert listing == {"northwind": False, "northwind-2": True}
    assert "browser_identity.default" in [row[0] for row in audit_rows(stored.db)]


def test_browser_dispatch_under_a_profile_gets_its_default_identity_and_its_worker(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    agent(client, headers["owner"], "scout")
    identity = agent_identity(client, headers["owner"], "northwind").json()
    (run,) = dispatched(client, headers["owner"], [4], profile="scout")
    assert (run["browser"], run["browser_identities"]) == (True, [identity["id"]])
    assert of_run(db, run["id"], "pinned_worker_id") == [(stored.box["id"],)]
    named = dispatch(client, headers["owner"], [5], identities=["northwind"])
    assert named.status_code == 422  # a dispatcher cannot name an agent's identity
    plain = dispatched(client, headers["owner"], [5])
    assert plain[0]["browser_identities"] == []


def test_browser_identity_of_an_agent_goes_with_the_profile_and_waits_for_the_run_holding_it(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    agent(client, headers["owner"], "scout")
    identity = agent_identity(client, headers["owner"], "northwind").json()
    (run,) = dispatched(client, headers["owner"], [4], profile="scout")
    assert claim(client, stored.box)["id"] == run["id"]
    assert held_by(db, identity["id"]) == run["id"]
    refused = client.delete(f"/v1/projects/{PROJECT}/agents/scout", headers=headers["owner"])
    assert refused.status_code == 409
    set_identity(db, identity["id"], held_by_run=None)
    assert client.delete(f"/v1/projects/{PROJECT}/agents/scout", headers=headers["owner"]).status_code == 200
    b = tables.browser_identities
    assert sql(db, select(b.c.deleted_at).where(b.c.id == identity["id"]))[0][0] is not None


def local(client, worker: dict, **body):
    return client.post(LOGIN, json={"project": PROJECT, **body}, headers=worker["headers"])


def test_browser_identity_local_login_makes_the_identity_on_the_worker_and_saves_without_a_run(stored, tmp_path):
    client, db, box = stored.client, stored.db, stored.box
    opened = local(client, box, name="northwind", domains=["northwind.example"])
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert (body["created"], body["version"], body["profile"], body["domains"]) == (
        True,
        0,
        None,
        ["northwind.example"],
    )
    assert "local" in audit_rows(db)[-1][1]
    archive = profile_archive(tmp_path, "session=" + secrets.token_hex(8))
    path = f"/v1/worker/browser-identities/{body['identity_id']}/profile"
    headers = {**box["headers"], "Content-Type": "application/octet-stream"}
    saved = client.put(
        path, params={"base_version": 0, "chromium_version": "153.0.8010.12"}, content=archive, headers=headers
    )
    assert saved.status_code == 200 and saved.json()["version"] == 1
    again = local(client, box, name="northwind")
    assert (again.json()["created"], again.json()["version"]) == (False, 1)
    assert base64.b64decode(again.json()["profile"]) == archive
    stale = client.put(path, params={"base_version": 0}, content=archive, headers=headers)
    assert stale.status_code == 409
    set_identity(db, body["identity_id"], held_by_run=None)
    assert local(client, box, name="northwind", owner="agent", agent="scout").status_code in (403, 404)
    assert local(client, box, name="fresh").status_code == 422  # a new one needs domains


def test_browser_identity_local_login_is_refused_for_an_identity_a_run_holds(stored, tmp_path):
    client, box = stored.client, stored.box
    identity = created(client, stored.headers["owner"], "northwind", domains=("northwind.example",))
    (run,) = dispatched(client, stored.headers["owner"], [4], identities=["northwind"])
    assert claim(client, box)["id"] == run["id"]
    assert local(client, box, name="northwind").status_code == 409
    path = f"/v1/worker/browser-identities/{identity['id']}/profile"
    headers = {**box["headers"], "Content-Type": "application/octet-stream"}
    saved = client.put(path, params={"base_version": 0}, content=profile_archive(tmp_path, "x"), headers=headers)
    assert saved.status_code == 409
    assert send  # the run's own route stays the one for a run

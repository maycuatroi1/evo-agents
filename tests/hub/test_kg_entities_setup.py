"""The one click of the Entities view's empty state (``evo_agents.hub.server.kg_entities_setup``): a reader sees why
the graph has no entities, a writer turns the step on and queues a build, and a sink the knowledge config lacks stops
the click before anything changes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.hub.kg_fixture import Harness, registration

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient

from evo_agents.hub.server.app import create_app
from evo_agents.kg import strategy
from tests.hub.s3 import fake_s3
from tests.hub.test_kg import InProcessHub, make_config

PROJECT = "kgsetup"
PATH = f"/v1/kg/{PROJECT}/entities/setup"
MEMBERS = {"wendy": "writer", "ivan": "reader"}
LLM_SINK = "    - {id: llm, kind: llm-backend, clearance: {level: internal}}\n"


@pytest.fixture(scope="module")
def hub(pg_server, tmp_path_factory):
    base = tmp_path_factory.mktemp("kg-setup")
    db = pg.create_database()
    try:
        with fake_s3() as s3:
            app = create_app(make_config(db, base / "api", s3))
            with TestClient(app) as client:
                admin = live.bearer(live.insert_token(db, live.ADMIN))
                response = client.put(f"/v1/projects/{PROJECT}", json=registration(PROJECT), headers=admin)
                assert response.status_code == 200, response.text
                members = {}
                for login, role in MEMBERS.items():
                    response = client.put(
                        f"/v1/admin/projects/{PROJECT}/grants/{login}",
                        json={"role": role, "max_level": "customer"},
                        headers=admin,
                    )
                    assert response.status_code in (200, 201), response.text
                    members[login] = live.bearer(live.insert_token(db, login))
                harness = Harness(base / "machine", PROJECT)
                harness.sync()
                harness.push(InProcessHub(client, members["wendy"]))
                yield SimpleNamespace(client=client, harness=harness, **members)
        assert pg.wait_no_backends(db.name) == 0
    finally:
        pg.drop_database(db)


def settings(hub) -> dict:
    response = hub.client.get(f"/v1/kg/{PROJECT}/settings", headers=hub.wendy)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_click_turns_the_step_on_once_the_sink_is_declared(hub):
    seen = hub.client.get(PATH, headers=hub.ivan)
    assert seen.status_code == 200, seen.text
    state = seen.json()
    assert state["step_on"] is False and state["turns_on"] == "entity-graph-api" and state["can_change"] is False
    assert state["sink"] == "llm" and "not declared" in state["sink_problem"]

    assert hub.client.post(PATH, json={}, headers=hub.ivan).status_code == 403, "a reader cannot click"

    refused = hub.client.post(PATH, json={}, headers=hub.wendy)
    assert refused.status_code == 409 and "knowledge.yaml" in refused.json()["message"]
    assert settings(hub)["values"]["strategy"] is None, "a refused click changes nothing"

    knowledge = hub.harness.harness / "knowledge.yaml"
    text = knowledge.read_text(encoding="utf-8")
    knowledge.write_text(text.replace("  sinks:\n", "  sinks:\n" + LLM_SINK, 1), encoding="utf-8")
    hub.harness.push(InProcessHub(hub.client, hub.wendy))

    clicked = hub.client.post(PATH, json={}, headers=hub.wendy)
    assert clicked.status_code == 202, clicked.text
    state = clicked.json()
    assert state["step_on"] is True and state["strategy"] == "entity-graph-api" and state["sink_problem"] is None
    assert state["building"]["status"] == "queued"
    after = settings(hub)
    carried = strategy.builtins()["entity-graph-api"].settings["entities"]
    assert after["values"]["entities"]["model"] == carried["model"]
    assert after["changes"][0]["reason"] == "entity graph view" and after["changes"][0]["changed_by"] == "wendy"

    again = hub.client.post(PATH, json={}, headers=hub.wendy)
    assert again.status_code == 202 and again.json()["queued"] is False, "the waiting build counts"
    assert again.json()["building"]["id"] == state["building"]["id"]
    assert len(settings(hub)["changes"]) == len(after["changes"]), "a step already on is kept"

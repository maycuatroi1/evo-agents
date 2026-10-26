"""Turning the features of the hub on and off, as criterion a11 of plan hub-agent-plugins names it
(``evo_agents.hub.features``, ``evo_agents.hub.server.features``, ``evo_agents.hub.server.hub_settings``): core
declares chats, curator, kg and monitor; with nothing set every feature is on and no route is turned away, as on
0.16.1; a hub admin turns a feature off for the whole hub or for one project, a project's layer over the hub's, and the
routes and /mcp tools of a feature that is off answer 404 for that project (the 404 of an unknown project to someone
who cannot see it); the Curator's night shift leaves out a project its Curator is off for; each change is a version
that rolls back, with its audit row; and GET /v1/features tells the web what to leave out of its menus."""

from types import SimpleNamespace

import pytest

from evo_agents.hub import features
from tests.hub import live, pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

SECOND = "second-project"


# The catalogue, without a hub


def test_the_features_core_declares_and_the_routes_and_tools_each_owns():
    assert features.IDS == ("chats", "curator", "kg", "monitor", "market", "seller", "canvas", "srs", "tasks", "docs")
    assert {f.id: f.layers for f in features.FEATURES} == {
        "chats": ("instance",),
        "curator": ("instance", "project"),
        "kg": ("instance", "project"),
        "monitor": ("instance",),
        "market": ("instance",),
        "seller": ("instance",),
        "canvas": ("instance", "project"),
        "srs": ("instance", "project"),
        "tasks": ("instance", "project"),
        "docs": ("instance", "project"),
    }
    # the seller is off until a hub admin turns it on; canvas, srs, tasks (plugins once) and docs until a project does
    assert [f.id for f in features.FEATURES if not f.default] == ["seller", "canvas", "srs", "tasks", "docs"]
    owners = {
        "/v1/chats": ("chats", None),
        "/v1/chats/12/messages": ("chats", None),
        "/v1/projects/demo/curator": ("curator", "demo"),
        "/v1/projects/demo/curator/proposals/3/plan": ("curator", "demo"),
        "/v1/kg/demo": ("kg", "demo"),
        "/v1/kg/demo/tools/kg_search": ("kg", "demo"),
        "/v1/projects/demo/runs": None,
        "/v1/projects/demo/curatorship": None,
        "/v1/chatsroom": None,
        "/v1/projects/Demo/curator": None,  # not a project's name: the route refuses it on its own
        "/v1/worker/chats": None,
        "/v1/x/canvas/demo": ("canvas", "demo"),
        "/v1/x/srs/demo/documents/4/versions": ("srs", "demo"),
        "/v1/x/tasks/demo/tasks": ("tasks", "demo"),
        "/v1/x/tasksboard/demo": None,
        "/v1/x/docs/demo/documents/guide/images/ab": ("docs", "demo"),
        "/v1/market": ("market", None),
        "/v1/market/3/lumen-notes": ("market", None),
        "/v1/marketplace": None,
        "/v1/seller/orders": ("seller", None),
        "/v1/seller-api/orders": None,
        "/v1/plugins/install-github": None,  # installing from a release stays with the market off
        "/v1/x/crm/demo/contacts": None,  # a plugin of the store, which its own gate turns away
    }
    for path, owner in owners.items():
        found = features.route_owner(path)
        assert (None if found is None else (found[0].id, found[1])) == owner, path
    assert features.tool_owner("kg_search").id == "kg" and features.tool_owner("kg_more").id == "kg"
    assert features.tool_owner("curator_figures").id == "curator"
    assert features.tool_owner("chat_message").id == features.tool_owner("chat_thread").id == "chats"
    assert features.tool_owner("memory_get") is None
    assert features.tool_owner("canvas__read").id == "canvas" and features.tool_owner("srs__propose").id == "srs"
    assert features.tool_owner("tasks__create").id == "tasks" and features.tool_owner("crm__invite") is None
    assert features.tool_owner("docs__read").id == "docs"


def test_a_projects_layer_wins_over_the_hubs_and_a_feature_neither_sets_is_on():
    kg, chats = features.BY_ID["kg"], features.BY_ID["chats"]
    assert features.enabled(kg, {}) and features.enabled(kg, {}, {})
    assert not features.enabled(kg, {"kg": False}) and not features.enabled(kg, {}, {"kg": False})
    assert features.enabled(kg, {"kg": False}, {"kg": True})
    assert not features.enabled(kg, {"kg": True}, {"kg": False})
    assert not features.enabled(chats, {"chats": False}, {"chats": True})  # the hub's alone: a project's is ignored
    assert features.problems({"kg": False, "curator": True}, "project") == []
    assert features.problems({"wiki": False}, "instance") == [
        "wiki: not a feature of this hub, which has chats, curator, kg, monitor, market, seller, canvas, srs, tasks, "
        "docs"
    ]
    srs = features.BY_ID["srs"]
    assert not features.enabled(srs, {}) and features.enabled(srs, {}, {"srs": True})
    assert features.enabled(srs, {"srs": True}) and not features.enabled(srs, {"srs": True}, {"srs": False})
    assert features.problems({"kg": "off"}, "instance") == ["kg: true turns it on and false off, nothing else"]
    assert features.problems({"chats": False}, "project") == [
        "chats: it is turned on or off for the whole hub only, not for one project"
    ]


# The routes

if pg.DSN:
    from fastapi.testclient import TestClient
    from sqlalchemy import literal, select

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.hub_settings import feature_on
    from tests.hub.test_curator import NIGHT, charter_body, fire, night, written  # noqa: F401 (night: a fixture)
    from tests.hub.test_mcp import failed, hub, ok  # noqa: F401 (hub: a fixture)
    from tests.hub.test_plans import registration
    from tests.hub.test_runs import (  # noqa: F401 (web_client: a fixture)
        OWNER,
        PROJECT,
        audit_rows,
        members,
        web_client,
    )


@pytest.fixture
def toggles(hub_db, tmp_path, github):
    """test_runs' members on project evo-agents, and second-project, which owner reads."""
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        headers = members(client, github)
        second = client.put(f"/v1/projects/{SECOND}", json=registration(), headers=headers["admin"])
        assert second.status_code == 200, second.text
        grant = {"role": "reader", "max_level": "internal"}
        granted = client.put(f"/v1/admin/projects/{SECOND}/grants/{OWNER}", json=grant, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
        yield SimpleNamespace(client=client, headers=headers, db=hub_db)


def set_layer(hub, path: str, status: int = 200, **body) -> dict:
    response = hub.client.post(path, json=body, headers=hub.headers["admin"])
    assert response.status_code == status, response.text
    return response.json()


# PROJECT comes from test_runs, imported only with a database: the helpers read it when called, not at import, so
# the tests of the catalogue above run without one (macOS and Windows in the full ci).
def project_layer(project: str | None = None) -> str:
    return f"/v1/admin/projects/{project or PROJECT}/features"


def get(hub, path: str, who: str = "owner"):
    return hub.client.get(path, headers=hub.headers[who] if who else {})


def turned_away(response, message: str) -> None:
    assert response.status_code == 404, response.text
    assert response.json()["error"] == "not_found" and response.json()["message"] == message
    assert response.json()["request_id"]


def off(feature: str, project: str | None = None) -> str:
    where = f" for project {project}" if project else ""
    return f"{feature} is turned off{where} on this hub; a hub admin turns it on in the administration, Features"


def routes() -> tuple[str, ...]:
    return (f"/v1/kg/{PROJECT}/builds", f"/v1/projects/{PROJECT}/curator", "/v1/chats")


@needs_pg
def test_with_nothing_set_every_feature_is_on_and_no_route_is_turned_away(toggles):
    listed = get(toggles, "/v1/features", "reader").json()
    assert listed == {
        "features": [
            {"id": "chats", "layers": ["instance"], "enabled": True},
            {"id": "curator", "layers": ["instance", "project"], "enabled": True},
            {"id": "kg", "layers": ["instance", "project"], "enabled": True},
            {"id": "monitor", "layers": ["instance"], "enabled": True},
            {"id": "market", "layers": ["instance"], "enabled": True},
            {"id": "seller", "layers": ["instance"], "enabled": False},
            {"id": "canvas", "layers": ["instance", "project"], "enabled": False},
            {"id": "srs", "layers": ["instance", "project"], "enabled": False},
            {"id": "tasks", "layers": ["instance", "project"], "enabled": False},
            {"id": "docs", "layers": ["instance", "project"], "enabled": False},
        ],
        "projects": [],
    }
    for path in routes():
        assert get(toggles, path).status_code == 200, path
    layer = get(toggles, "/v1/admin/features", "admin").json()
    assert (layer["kind"], layer["project"], layer["version"], layer["values"]) == ("features", None, 0, {})
    assert get(toggles, project_layer(), "admin").json()["version"] == 0
    assert get(toggles, "/v1/features", None).status_code == 401


@needs_pg
def test_a_feature_turned_off_for_a_project_answers_404_there_and_nowhere_else(toggles):
    layer = set_layer(toggles, project_layer(), values={"kg": False}, if_version=0)
    assert (layer["project"], layer["version"], layer["values"]) == (PROJECT, 1, {"kg": False})

    turned_away(get(toggles, f"/v1/kg/{PROJECT}/builds"), off("kg", PROJECT))
    turned_away(get(toggles, f"/v1/kg/{PROJECT}/graph", "reader"), off("kg", PROJECT))
    post = toggles.client.post(f"/v1/kg/{PROJECT}/builds", headers=toggles.headers["owner"])
    turned_away(post, off("kg", PROJECT))
    # someone who cannot see the project learns nothing of it: the 404 of an unknown project
    stranger = get(toggles, f"/v1/kg/{PROJECT}/builds", "stranger")
    assert stranger.status_code == 404 and stranger.json()["message"].startswith(f"no project {PROJECT} that you can")
    assert get(toggles, f"/v1/kg/{PROJECT}/builds", None).status_code == 401  # a credential first
    assert get(toggles, f"/v1/kg/{SECOND}/builds").status_code == 200
    assert get(toggles, f"/v1/projects/{PROJECT}/curator").status_code == 200

    mine = get(toggles, "/v1/features").json()
    assert mine["projects"] == [{"project": PROJECT, "values": {"kg": False}}]
    assert all(
        feature["enabled"] == (feature["id"] not in ("seller", "canvas", "srs", "tasks", "docs"))
        for feature in mine["features"]
    )
    assert get(toggles, "/v1/features", "stranger").json()["projects"] == []  # no grant on it


@needs_pg
def test_the_hubs_layer_turns_a_feature_off_everywhere_and_a_projects_layer_turns_it_back_on(toggles):
    set_layer(toggles, "/v1/admin/features", values={"kg": False, "chats": False, "monitor": False})
    states = {f["id"]: f["enabled"] for f in get(toggles, "/v1/features", "reader").json()["features"]}
    assert states == {
        "chats": False,
        "curator": True,
        "kg": False,
        "monitor": False,
        "market": True,
        "seller": False,
        "canvas": False,
        "srs": False,
        "tasks": False,
        "docs": False,
    }
    turned_away(get(toggles, "/v1/chats"), off("chats"))
    turned_away(get(toggles, f"/v1/kg/{SECOND}/builds"), off("kg", SECOND))
    turned_away(get(toggles, f"/v1/kg/{PROJECT}/builds"), off("kg", PROJECT))

    set_layer(toggles, project_layer(), values={"kg": True})
    assert get(toggles, f"/v1/kg/{PROJECT}/builds").status_code == 200
    turned_away(get(toggles, f"/v1/kg/{SECOND}/builds"), off("kg", SECOND))
    set_layer(toggles, project_layer(), unset=["kg"])
    turned_away(get(toggles, f"/v1/kg/{PROJECT}/builds"), off("kg", PROJECT))
    set_layer(toggles, "/v1/admin/features", unset=["kg", "chats", "monitor"])
    for path in routes():
        assert get(toggles, path).status_code == 200, path


@needs_pg
def test_a_layer_takes_only_the_features_core_declares_and_only_hub_admins_write_it(toggles):
    for values, words in (
        ({"chats": False}, "chats: it is turned on or off for the whole hub only"),
        ({"kg": "off"}, "kg: true turns it on and false off"),
        ({"wiki": False}, "wiki: not a feature of this hub"),
    ):
        refused = set_layer(toggles, project_layer(), 422, values=values)
        assert refused["message"].startswith(words) and refused["message"].endswith("nothing changed"), refused
    assert set_layer(toggles, "/v1/admin/features", 422, values={"kg": 0})["message"].startswith("kg: true turns")
    assert set_layer(toggles, project_layer("nowhere"), 404, values={"kg": False})["message"].startswith("no project")
    for who in ("owner", "reader", "stranger"):
        headers = toggles.headers[who]
        assert toggles.client.get(project_layer(), headers=headers).status_code == 403
        refused = toggles.client.post("/v1/admin/features", json={"values": {"kg": False}}, headers=headers)
        assert refused.status_code == 403
    assert get(toggles, "/v1/admin/features/versions", "admin").json() == []


@needs_pg
def test_feature_toggles_are_versions_that_roll_back_each_with_its_audit_row(toggles):
    set_layer(toggles, project_layer(), values={"kg": False, "curator": False})
    set_layer(toggles, project_layer(), unset=["kg"])
    assert get(toggles, f"/v1/kg/{PROJECT}/builds").status_code == 200
    turned_away(get(toggles, f"/v1/projects/{PROJECT}/curator"), off("curator", PROJECT))
    back = set_layer(toggles, project_layer() + "/rollback", version=1, if_version=2)
    assert (back["version"], back["rolled_back_to"], back["values"]) == (3, 1, {"kg": False, "curator": False})
    turned_away(get(toggles, f"/v1/kg/{PROJECT}/builds"), off("kg", PROJECT))
    versions = get(toggles, project_layer() + "/versions", "admin").json()
    assert [(v["version"], v["values"]) for v in versions] == [
        (3, {"kg": False, "curator": False}),
        (2, {"curator": False}),
        (1, {"kg": False, "curator": False}),
    ]
    assert set_layer(toggles, project_layer() + "/rollback", 409, version=3)["message"].startswith(
        f"the layer of features of project {PROJECT} is at version 3"
    )
    set_layer(toggles, "/v1/admin/features", values={"chats": False})
    assert audit_rows(toggles.db, "settings") == [
        ("settings.features", f"{PROJECT}/features v1 fields=curator,kg", live.ADMIN, PROJECT),
        ("settings.features", f"{PROJECT}/features v2 fields=kg", live.ADMIN, PROJECT),
        ("settings.features", f"{PROJECT}/features v3 rollback-to=v1", live.ADMIN, PROJECT),
        ("settings.features", "features v1 fields=chats", live.ADMIN, None),
    ]


@needs_pg
def test_feature_on_is_the_same_rule_in_sql(toggles):
    p = tables.projects

    def on(feature: str, project: str = PROJECT) -> bool:
        project_id = select(p.c.id).where(p.c.name == project).scalar_subquery()
        return live.sql(toggles.db, select(feature_on(feature, project_id)))[0][0]

    assert on("curator") and on("kg")
    set_layer(toggles, "/v1/admin/features", values={"curator": False})
    assert not on("curator") and not on("curator", SECOND) and on("kg")
    set_layer(toggles, project_layer(), values={"curator": True, "kg": False})
    assert on("curator") and not on("curator", SECOND) and not on("kg") and on("kg", SECOND)
    assert live.sql(toggles.db, select(feature_on("chats", literal(0))))[0][0] is True
    assert not on("srs") and not on("srs", SECOND)  # off by default, as the plugin was until a project enabled it
    set_layer(toggles, project_layer(), values={"srs": True})
    assert on("srs") and not on("srs", SECOND)


@needs_pg
def test_the_night_shift_queues_nothing_for_a_project_whose_curator_is_off(night):
    client, headers = night.client, night.headers
    written(client, headers["owner"], charter_body())
    turned = client.post(project_layer(), json={"values": {"curator": False}}, headers=headers["admin"])
    assert turned.status_code == 200, turned.text
    assert fire(client, NIGHT) == {}
    client.post(project_layer(), json={"unset": ["curator"]}, headers=headers["admin"])
    assert fire(client, NIGHT) == {"queued": 1}


@needs_pg
def test_a_tool_of_a_feature_turned_off_is_refused_as_its_route_is(hub):
    project = "alpha"
    ok(hub, "alice", "kg_status")
    turned = hub.client.post(
        f"/v1/admin/projects/{project}/features", json={"values": {"kg": False}}, headers=hub.admin
    )
    assert turned.status_code == 200, turned.text
    assert failed(hub, "alice", "kg_status") == "error: " + off("kg", project)
    assert failed(hub, "alice", "kg_search", {"query": "acme"}) == "error: " + off("kg", project)
    ok(hub, "alice", "memory_search", {"query": "acme"})  # a tool of no feature
    turned = hub.client.post("/v1/admin/features", json={"values": {"curator": False}}, headers=hub.admin)
    assert turned.status_code == 200, turned.text
    assert failed(hub, "alice", "curator_figures") == "error: " + off("curator", project)


@needs_pg
def test_canvas_srs_tasks_and_docs_are_features_the_store_leaves_out_and_enabling_one_turns_it_on(toggles):
    admin = toggles.headers["admin"]
    grant = {"role": "admin", "max_level": "internal"}
    made = toggles.client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=grant, headers=admin)
    assert made.status_code == 200, made.text
    owner = toggles.headers["owner"]  # an admin of the project, not of the hub
    store = {plugin["name"] for plugin in get(toggles, "/v1/plugins", "admin").json()}
    assert not {"canvas", "srs", "tasks", "docs"} & store
    for name in ("canvas", "srs", "tasks", "docs"):
        assert get(toggles, f"/v1/plugins/{name}", "admin").status_code == 404
        refused = toggles.client.post(f"/v1/plugins/{name}/remove", json={}, headers=admin)
        assert refused.status_code == 404, refused.text
    listed = {plugin["name"]: plugin for plugin in get(toggles, f"/v1/projects/{PROJECT}/plugins").json()}
    assert {name for name, plugin in listed.items() if plugin["builtin"]} == {"canvas", "srs", "tasks", "docs"}
    assert not listed["srs"]["enabled"] and listed["srs"]["granted"] == []
    message = (
        f"srs is not enabled in project {PROJECT}: an admin of the project enables it on the project's Plugins page "
        "(POST .../plugins/srs/enable), or a hub admin in the administration, Features"
    )
    turned_away(get(toggles, f"/v1/x/srs/{PROJECT}/documents"), message)

    asked = [item["name"] for item in listed["tasks"]["permissions"]]
    enabled = toggles.client.post(
        f"/v1/projects/{PROJECT}/plugins/tasks/enable", json={"permissions": asked}, headers=owner
    )
    assert enabled.status_code == 200, enabled.text
    layer = get(toggles, project_layer(), "admin").json()
    assert (layer["version"], layer["values"]) == (1, {"tasks": True})
    shown = get(toggles, f"/v1/projects/{PROJECT}/plugins/tasks").json()
    assert shown["enabled"] and shown["builtin"] and shown["granted"] == asked and shown["pending"] == []
    assert get(toggles, f"/v1/x/tasks/{PROJECT}/settings").status_code == 200
    assert get(toggles, f"/v1/x/tasks/{SECOND}/settings").status_code == 404
    again = toggles.client.post(
        f"/v1/projects/{PROJECT}/plugins/tasks/enable", json={"permissions": asked}, headers=owner
    )
    assert again.status_code == 200 and get(toggles, project_layer(), "admin").json()["version"] == 1

    disabled = toggles.client.post(f"/v1/projects/{PROJECT}/plugins/tasks/disable", headers=owner)
    assert disabled.status_code == 200, disabled.text
    assert get(toggles, project_layer(), "admin").json()["values"] == {"tasks": False}
    assert get(toggles, f"/v1/x/tasks/{PROJECT}/settings").status_code == 404
    set_layer(toggles, "/v1/admin/features", values={"canvas": True})  # the hub's layer turns it on everywhere
    assert get(toggles, f"/v1/x/canvas/{SECOND}/canvases").status_code == 200

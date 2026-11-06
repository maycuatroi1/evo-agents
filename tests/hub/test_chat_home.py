"""The agents a person may chat with (``evo_agents.hub.server.chat_home``) and the starters of a profile
(``agent_profiles``), as criteria b1 and b2 of the plan chat-first-home name them.

b1: GET /v1/me/chat-agents lists the agents of every project the caller holds a grant on that the caller may give work
to by the rules a mention follows (``agent_tasking``: writer on the project, admin for an agent that takes work from
the admins alone), each with what the chat home shows of it and when the caller last wrote to it; an agent deleted, or
without a pool of its own, is left out.
b2: a profile has at most four starters of at most 120 characters each, which a project admin sets through the profile
API, the default profile's included, and which the list carries.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select, update

from evo_agents.hub import tables
from tests.hub import test_chats as group_chats
from tests.hub.live import sql
from tests.hub.test_plans import registration
from tests.hub.test_runs import OTHER, OWNER, PROJECT

client, hub = group_chats.client, group_chats.hub  # fixtures: project evo-agents, owner its admin, the agent @reviewer
agent, sent = group_chats.agent, group_chats.sent

KB = "lumen-kb"
STARTERS = ["What changed in the release?", "Which runs failed today?"]


def listed(client, headers) -> list[dict]:
    response = client.get("/v1/me/chat-agents", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def handles(client, headers) -> list[str]:
    return [item["handle"] for item in listed(client, headers)]


def edit(client, headers, handle: str, body: dict, project: str = PROJECT):
    return client.patch(f"/v1/projects/{project}/agents/{handle}", json=body, headers=headers)


def kb_project(client, hub) -> None:
    """Project lumen-kb with the agent kb-bot: owner is its admin, someone-else a reader."""
    assert client.put(f"/v1/projects/{KB}", json=registration(), headers=hub["admin"]).status_code == 200
    for login, role in ((OWNER, "admin"), (OTHER, "reader")):
        grant = {"role": role, "max_level": "internal"}
        response = client.put(f"/v1/admin/projects/{KB}/grants/{login}", json=grant, headers=hub["admin"])
        assert response.status_code == 200, response.text
    agent(client, hub["owner"], "kb-bot", project=KB)


def admins_only(db, handle: str) -> None:
    """Make agent ``handle`` of evo-agents the agent of a plugin's agent type that takes work from admins alone."""
    ap, u = tables.agent_profiles, tables.users
    ((profile_id, project_id),) = sql(db, select(ap.c.id, ap.c.project_id).where(ap.c.handle == handle))
    ((owner_id,),) = sql(db, select(u.c.id).where(u.c.login == OWNER))
    (plugin_id,) = sql(
        db, insert(tables.plugins).values(name="gate", publisher="northwind").returning(tables.plugins.c.id)
    )[0]
    manifest = {"contributes": {"agent_types": [{"name": "gate", "mentioned_by": "admin"}]}}
    version = {
        "plugin_id": plugin_id,
        "version": "1.0.0",
        "sha256": "0" * 64,
        "size": 1,
        "r2_key": "plugins/gate/1.0.0",
        "publisher": "northwind",
        "tier": "core",
        "manifest": manifest,
        "signature": {},
        "signature_kind": "image",  # a core plugin of the image, which nobody published
        "signed_by": "northwind",
    }
    sql(db, insert(tables.plugin_versions).values(**version))
    agent_type = {
        "project_id": project_id,
        "plugin_id": plugin_id,
        "name": "gate",
        "profile_id": profile_id,
        "run_as": owner_id,
        "daily_usd": 1.0,
        "enabled_by": owner_id,
    }
    sql(db, insert(tables.project_agent_types).values(**agent_type))


def test_chat_agents_lists_agents_the_viewer_may_task(client, hub, hub_db):
    kb_project(client, hub)
    agent(client, hub["owner"], "gatekeeper")
    admins_only(hub_db, "gatekeeper")
    assert edit(client, hub["owner"], "reviewer", {"starters": STARTERS}).status_code == 200
    opened = client.put("/v1/chats/direct/reviewer", headers=hub["owner"])
    assert opened.status_code == 201, opened.text
    sent(client, hub["owner"], opened.json()["id"], "What is left of the release?")

    # the admin of both projects: every agent of both, the one written to last first, then by project, default first
    mine = listed(client, hub["owner"])
    assert [item["handle"] for item in mine] == ["reviewer", PROJECT, "gatekeeper", KB, "kb-bot"]
    reviewer = mine[0]
    assert {key: reviewer[key] for key in ("project", "name", "color", "icon_url", "starters", "is_default")} == {
        "project": PROJECT,
        "name": "Reviewer",
        "color": reviewer["color"],
        "icon_url": None,
        "starters": STARTERS,
        "is_default": False,
    }
    assert reviewer["chat_id"] == opened.json()["id"] and reviewer["last_chatted_at"] is not None
    assert reviewer["description"] == "" and reviewer["color"].startswith("#")
    assert all(item["chat_id"] is None and item["last_chatted_at"] is None for item in mine[1:])
    assert mine[1]["is_default"] and mine[3]["is_default"]

    # a writer of evo-agents and a reader of lumen-kb: not the agent of admins alone, nothing of lumen-kb
    assert handles(client, hub["other"]) == [PROJECT, "reviewer"]
    # a reader, someone without a grant and a hub admin without one: none
    for who in ("reader", "stranger", "admin"):
        assert listed(client, hub[who]) == [], who
    assert client.get("/v1/me/chat-agents").status_code == 401


def test_chat_agents_leaves_out_agents_without_pool(client, hub, hub_db):
    agent(client, hub["owner"], "drafter")
    agent(client, hub["owner"], "retired")
    assert handles(client, hub["owner"]) == [PROJECT, "drafter", "retired", "reviewer"]
    wp, ap = tables.worker_pools, tables.agent_profiles
    ((drafter_id,),) = sql(hub_db, select(ap.c.id).where(ap.c.handle == "drafter"))
    sql(hub_db, update(wp).where(wp.c.agent_id == drafter_id).values(agent_id=None))
    assert client.delete(f"/v1/projects/{PROJECT}/agents/retired", headers=hub["owner"]).status_code == 200
    assert handles(client, hub["owner"]) == [PROJECT, "reviewer"]
    assert handles(client, hub["other"]) == [PROJECT, "reviewer"]


def test_profile_starters(client, hub, hub_db):
    path = f"/v1/projects/{PROJECT}/agents"
    made = client.post(path, json={"handle": "helper", "name": "Helper", "starters": STARTERS}, headers=hub["owner"])
    assert made.status_code == 201, made.text
    assert (made.json()["starters"], made.json()["version"]) == (STARTERS, 1)
    assert client.get(f"{path}/reviewer", headers=hub["reader"]).json()["starters"] == []

    four = ["  Summarise the plan  ", "x" * 120, "Which step is next?", "Who owns the deploy?"]
    changed = edit(client, hub["owner"], "helper", {"starters": four})
    assert changed.status_code == 200, changed.text
    assert changed.json()["starters"] == ["Summarise the plan", "x" * 120, *four[2:]]
    assert changed.json()["version"] == 2
    v = tables.agent_profile_versions
    bodies = sql(
        hub_db,
        select(v.c.version, v.c.body["starters"])
        .join_from(v, tables.agent_profiles)
        .where(tables.agent_profiles.c.handle == "helper")
        .order_by(v.c.version),
    )
    assert bodies == [(1, STARTERS), (2, changed.json()["starters"])]
    unchanged = edit(client, hub["owner"], "helper", {"starters": changed.json()["starters"]})
    assert unchanged.json()["version"] == 2

    for refused in ([*four, "One more"], ["x" * 121], ["two\nlines"], ["   "], [""]):
        response = edit(client, hub["owner"], "helper", {"starters": refused})
        assert response.status_code == 422, refused
    assert edit(client, hub["other"], "helper", {"starters": ["Mine"]}).status_code == 403
    assert edit(client, hub["owner"], "helper", {"starters": []}).json()["starters"] == []

    # the default profile has starters too, and the chat home lists them
    default = edit(client, hub["owner"], PROJECT, {"starters": ["What is this project?"]})
    assert default.status_code == 200, default.text
    assert default.json()["starters"] == ["What is this project?"]
    shown = {item["handle"]: item["starters"] for item in listed(client, hub["other"])}
    assert shown[PROJECT] == ["What is this project?"] and shown["helper"] == []

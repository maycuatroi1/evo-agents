"""The records an isolated plugin keeps on the hub (``evo_agents.hub.plugin_records``, ``plugin_records_api``,
``plugin_delegation``; schema 0169), on the hub with Postgres.

Criterion a1: a plugin writes, reads, updates, lists and deletes records through the plugin API with revisions, and they
sit in Postgres. Criterion a2: a record has a level and may name its writer as its audience; a call whose grant, sink or
person does not reach it gets what it would for a record that does not exist, through the route, the tool, the bridge
and the run agent. Criterion a3: a backend touches records only for the caller of the call it serves, or, with no caller,
up to the level the admin granted at enable time; the hostile plugin below tries what it can."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from types import SimpleNamespace

from sqlalchemy import select, update

from evo_agents.hub import plugin_records, tables
from evo_agents.hub.access import ProjectRules
from tests.hub import live
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_credentials_api import DISPATCHED
from tests.hub.test_credentials_api import registration as project_registration
from tests.hub.test_mcp import MCP_HEADERS, call
from tests.hub.test_plugin_runtimes import install
from tests.hub.test_runs import PROJECT
from tests.hub.test_task_runs import (  # noqa: F401 (fixtures)
    OTHER,
    OWNER,
    READER,
    app_key,
    client,
    hub,
    plugin_key,
    user_id,
)

RECORDS = "records:use"
PLUGIN = "vault"
LOW = {"id": "low", "kind": "agent-session", "clearance": {"level": "public"}}
ROUTES = [{"name": "notes", "methods": ["GET", "POST"]}]
TOOLS = [{"name": "peek", "description": "Peek", "input_schema": {"type": "object"}}]
BASE = f"/v1/plugin-api/projects/{PROJECT}/records"
DELEGATION = "X-Evo-On-Behalf-Of"
LADDER = ["public", "internal", "customer", "secret"]


class Host:
    """A plugin host that records what the hub hands the backend and answers every call with success."""

    def __init__(self):
        self.calls = []

    async def call(self, plugin, method, params, timeout=30.0):
        self.calls.append(SimpleNamespace(plugin=plugin, method=method, params=params))
        if method == "route":
            return {"status": 200, "headers": {}, "body": ""}
        return {"content": [{"type": "text", "text": "ok"}]}

    async def aclose(self):
        pass


def grant(client, hub, login: str, role: str, level: str) -> None:
    response = client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{login}", json={"role": role, "max_level": level}, headers=hub["admin"]
    )
    assert response.status_code == 200, response.text


def enable(client, hub, name: str, **body) -> None:
    sent = {"permissions": [RECORDS], **body}
    response = client.post(f"/v1/projects/{PROJECT}/plugins/{name}/enable", json=sent, headers=hub["owner"])
    assert response.status_code == 200, response.text


@pytest.fixture
def vault(client, hub, hub_db) -> dict:  # noqa: F811
    """Plugin vault with a route and a tool, enabled with records:use (no records_level); the project's sinks and
    ladder (public, internal, customer, secret); someone-else reaches customer, reader public. Its key and the host."""
    registered = {**project_registration(DISPATCHED), "sinks": [*OPEN_SINKS, LOW]}
    assert client.put(f"/v1/projects/{PROJECT}", json=registered, headers=hub["admin"]).status_code == 200
    grant(client, hub, OTHER, "writer", "customer")
    grant(client, hub, READER, "reader", "public")
    plugin_id = install(hub_db, name=PLUGIN, contributes={"routes": ROUTES, "mcp_tools": TOOLS}, permissions=[RECORDS])
    enable(client, hub, PLUGIN)
    host = Host()
    client.app.state.plugin_host = host
    return {**hub, "key": plugin_key(client, plugin_id), "id": plugin_id, "host": host}


def delegation(client, vault, member: str = "owner", cookies=None) -> dict:
    """The headers of a backend serving a call of ``member``: the hub's route hands the backend its delegation."""
    headers = {} if cookies else vault[member]
    served = client.get(f"/v1/x/{PLUGIN}/{PROJECT}/notes", headers=headers, cookies=cookies)
    assert served.status_code == 200, served.text
    token = vault["host"].calls[-1].params["caller"]["token"]
    return {**vault["key"], DELEGATION: token}


def tool_delegation(client, vault, member: str, sink: str | None = None) -> dict:
    """The same, for a call of the plugin's tool by ``member``'s agent through ``sink``."""
    headers = {**vault[member], **MCP_HEADERS, "X-Evo-Project": PROJECT, **({"X-Evo-Sink": sink} if sink else {})}
    answered = client.post("/mcp", json=call(1, f"{PLUGIN}__peek"), headers=headers)
    assert answered.status_code == 200, answered.text
    return {**vault["key"], DELEGATION: vault["host"].calls[-1].params["caller"]["token"]}


def put(client, headers, key: str, collection: str = "notes", **body):
    return client.put(f"{BASE}/{collection}/{key}", json={"body": {"text": key}, **body}, headers=headers)


def keys(client, headers, collection: str = "notes") -> list[str]:
    listed = client.get(f"{BASE}/{collection}", headers=headers)
    assert listed.status_code == 200, listed.text
    return [record["key"] for record in listed.json()["records"]]


def absent(client, headers, key: str):
    return client.get(f"{BASE}/notes/{key}", headers=headers)


def stored(db) -> list:
    r = tables.plugin_records.c
    return live.sql(db, select(r.collection, r.key, r.revision, r.level, r.audience_user_id).order_by(r.key))


# a1: the store


def test_records_are_written_read_updated_listed_and_deleted_with_revisions_and_survive_in_postgres(
    client, vault, hub_db
):
    caller = delegation(client, vault)
    made = put(client, caller, "plan-1", body={"text": "first"})
    assert made.status_code == 201, made.text
    record = made.json()
    assert (record["revision"], record["level"], record["audience"]) == (1, "internal", "everyone")
    assert (record["created_by"], record["updated_by"]) == (OWNER, OWNER)
    updated = put(client, caller, "plan-1", body={"text": "second"}, revision=1)
    assert (updated.status_code, updated.json()["revision"], updated.json()["body"]) == (200, 2, {"text": "second"})
    assert put(client, caller, "plan-1", revision=1).status_code == 409, "the record moved on"
    assert put(client, caller, "plan-1", revision=0).status_code == 409, "a new record asks for a key that is taken"
    assert put(client, caller, "other", revision=3).status_code == 404
    assert put(client, caller, "plan-2").status_code == 201
    assert client.get(f"{BASE}/notes/plan-1", headers=caller).json()["body"] == {"text": "second"}
    first = client.get(f"{BASE}/notes", params={"limit": 1}, headers=caller).json()
    assert [r["key"] for r in first["records"]] == ["plan-1"] and first["next_after"] == "plan-1"
    after = client.get(f"{BASE}/notes", params={"after": first["next_after"]}, headers=caller).json()
    assert [r["key"] for r in after["records"]] == ["plan-2"] and after["next_after"] is None
    assert keys(client, caller, "elsewhere") == [], "collections are separate"
    # the records are rows of Postgres, by project, plugin and collection
    assert stored(hub_db) == [("notes", "plan-1", 2, "internal", None), ("notes", "plan-2", 1, "internal", None)]
    assert client.delete(f"{BASE}/notes/plan-1", params={"revision": 1}, headers=caller).status_code == 409
    assert client.delete(f"{BASE}/notes/plan-1", params={"revision": 2}, headers=caller).status_code == 204
    assert absent(client, caller, "plan-1").status_code == 404
    assert client.delete(f"{BASE}/notes/plan-1", headers=caller).status_code == 404
    assert keys(client, caller) == ["plan-2"]


def test_records_limits_and_names(client, vault):
    caller = delegation(client, vault)
    assert put(client, caller, "k", body={"text": "x" * (plugin_records.MAX_BODY_BYTES + 1)}).status_code == 413
    assert put(client, caller, "k" * 201).status_code == 422
    assert client.put(f"{BASE}/Bad/k", json={"body": {}}, headers=caller).status_code == 422
    assert client.put(f"{BASE}/notes/k", json={"body": [1]}, headers=caller).status_code == 422
    assert put(client, caller, "k", level="nowhere").status_code == 422
    assert put(client, caller, "k", audience="nobody").status_code == 422


def test_a_member_and_a_page_keep_the_records_of_the_plugin_through_their_own_grant(client, vault):
    own = f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/records/notes"
    written = client.put(f"{own}/mine", json={"body": {"a": 1}}, headers=vault["other"])
    assert written.status_code == 201 and written.json()["created_by"] == OTHER
    assert client.get(own, headers=vault["owner"]).json()["records"][0]["key"] == "mine"
    assert client.delete(f"{own}/mine", headers=vault["reader"]).status_code == 403, "a reader reads"
    assert client.get(f"{own}/mine", headers=vault["stranger"]).status_code == 404


# a2: levels, audiences, sinks


def test_a_record_above_the_level_of_the_callers_grant_is_a_record_that_does_not_exist(client, vault):
    owner, other = delegation(client, vault, "owner"), delegation(client, vault, "other")
    assert put(client, owner, "customer-note", level="customer").status_code == 403, "owner reaches internal"
    # someone-else reaches customer on the hub, but a plugin call is an agent's: the sink claude-code clears internal
    assert put(client, other, "customer-note", level="customer").status_code == 403
    assert put(client, owner, "pub", level="public").status_code == 201
    assert put(client, owner, "int").status_code == 201
    reader = delegation(client, vault, "reader")
    assert keys(client, reader) == ["pub"], "a reader whose grant reaches public sees public"
    hidden = absent(client, reader, "int")
    nothing = absent(client, reader, "never-written")
    assert hidden.status_code == nothing.status_code == 404
    assert hidden.json()["message"].replace("int", "X") == nothing.json()["message"].replace("never-written", "X")
    assert put(client, reader, "int").status_code == 403, "a reader writes nothing"
    assert client.delete(f"{BASE}/notes/int", headers=reader).status_code == 403


def test_records_follow_the_sink_the_call_reads_through_by_tool_and_by_the_web_by_the_grant_alone(
    client, vault, hub_db
):
    owner = delegation(client, vault, "owner")
    assert put(client, owner, "pub", level="public").status_code == 201
    assert put(client, owner, "int").status_code == 201
    # a tool call of someone-else's agent through the low sink (clears public) sees public; through the default, internal
    low = tool_delegation(client, vault, "other", sink="low")
    assert keys(client, low) == ["pub"]
    assert absent(client, low, "int").status_code == 404
    assert keys(client, tool_delegation(client, vault, "other")) == ["int", "pub"]
    # a record at customer, written by a delegation of someone-else's web session (no sink, grant customer)
    token, _ = live.web_session(hub_db, OTHER)
    cookies = {"evo_hub_session": token}
    web = delegation(client, vault, "other", cookies=cookies)
    assert put(client, web, "deep", level="customer").status_code == 201
    assert keys(client, tool_delegation(client, vault, "other")) == ["int", "pub"], "an agent's sink stops at internal"
    assert "deep" in keys(client, delegation(client, vault, "other", cookies=cookies)), "the web reads by the grant"


def test_a_record_that_names_its_writer_as_its_audience_is_seen_by_the_writer_alone(client, vault):
    owner, other = delegation(client, vault, "owner"), delegation(client, vault, "other")
    mine = put(client, owner, "diary", audience="writer")
    assert mine.status_code == 201 and mine.json()["audience"] == "writer"
    assert put(client, owner, "open").status_code == 201
    assert keys(client, owner) == ["diary", "open"]
    assert keys(client, other) == ["open"]
    assert absent(client, other, "diary").status_code == 404
    assert put(client, other, "diary", revision=1).status_code == 404, "a stranger's update finds nothing"
    assert put(client, other, "diary").status_code == 409, "a create over a key that is taken"
    assert client.delete(f"{BASE}/notes/diary", headers=other).status_code == 404
    assert absent(client, owner, "diary").status_code == 200
    # the writer opens it to everyone again
    assert put(client, owner, "diary", audience="everyone").json()["audience"] == "everyone"
    assert keys(client, other) == ["diary", "open"]


def test_reach_filters_by_level_audience_and_the_people_a_chat_run_is_cleared_for():
    rules = ProjectRules(
        "p", LADDER, ["any"], [{"id": "s", "kind": "agent-session", "clearance": {"level": "internal"}}]
    )
    row = SimpleNamespace(level="internal", audience_user_id=7)
    base = {"project_id": 1, "plugin_id": 1, "rules": rules, "max_level": "customer"}
    both = plugin_records.Reach(**base, user_id=7, sink="s", people=frozenset({7, 8}))
    alone = plugin_records.Reach(**base, user_id=7, sink="s", people=frozenset({7}))
    assert alone.sees(row) and not both.sees(row), "a run cleared for two people sees what both may"
    assert not plugin_records.Reach(**base, user_id=7, sink="s").sees(
        SimpleNamespace(level="customer", audience_user_id=None)
    )
    assert plugin_records.Reach(**base, user_id=7).sees(SimpleNamespace(level="customer", audience_user_id=None))
    assert plugin_records.Reach(**base).sees(row), "a call with no caller has no audience to match"
    assert plugin_records.Reach(**{**base, "max_level": None}).levels() == []


def test_the_bridge_reaches_the_records_of_its_own_plugin_only_while_the_project_grants_it(client, vault, hub_db):
    page = {"X-Evo-Plugin": PLUGIN}
    own = f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/records/notes"
    assert client.put(f"{own}/k", json={"body": {}}, headers={**vault["owner"], **page}).status_code == 201
    assert client.get(own, headers={**vault["owner"], **page}).json()["records"][0]["key"] == "k"
    # a page of another plugin reaches none of vault's records, and vault's page none of another's
    other = install(hub_db, name="spy", contributes={"events": []}, permissions=[RECORDS])
    enable(client, vault, "spy")
    spy = {"X-Evo-Plugin": "spy"}
    assert client.get(own, headers={**vault["owner"], **spy}).status_code == 403
    assert client.get(own.replace(PLUGIN, "spy"), headers={**vault["owner"], **page}).status_code == 403
    assert client.get(f"/v1/projects/{PROJECT}/plugins", headers={**vault["owner"], **page}).status_code == 403
    assert other
    # the project takes the permission away: the open page gets nothing more
    grants = tables.project_plugins
    live.sql(hub_db, update(grants).where(grants.c.plugin_id == vault["id"]).values(permissions=[]))
    assert client.get(own, headers={**vault["owner"], **page}).status_code == 403


# a3: a backend touches records for the caller of its call, or up to records_level


def test_a_call_with_no_caller_reaches_the_levels_the_admin_granted_at_enable_time_and_no_audience(client, vault):
    own = vault["key"]
    refused = client.get(f"{BASE}/notes", headers=own)
    assert refused.status_code == 403 and "records_level" in refused.json()["message"]
    assert enable_level(client, vault, "public")["records_level"] == "public"
    assert put(client, own, "job-note", level="public").status_code == 201
    assert put(client, own, "job-note", level="internal").status_code == 403, "above what the admin granted"
    assert put(client, own, "x", audience="writer").status_code == 422, "no writer to name"
    member = delegation(client, vault, "owner")
    assert put(client, member, "int-note").status_code == 201
    assert keys(client, own) == ["job-note"], "the job does not see internal records"
    assert put(client, own, "int-note", revision=1).status_code == 404
    assert put(client, own, "int-note").status_code == 409
    assert client.get(f"{BASE}/notes/job-note", headers=member).json()["created_by"] is None
    bad = client.post(
        f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable",
        json={"permissions": [RECORDS], "records_level": "x"},
        headers=vault["owner"],
    )
    assert bad.status_code == 422


def enable_level(client, vault, level: str) -> dict:
    response = client.post(
        f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable",
        json={"permissions": [RECORDS], "records_level": level},
        headers=vault["owner"],
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_hostile_plugin_cannot_use_a_delegation_that_is_not_its_own_or_not_live_or_make_one(client, vault, hub_db):
    owner = delegation(client, vault, "owner")
    assert put(client, owner, "secret-plan").status_code == 201
    # another plugin with records:use of its own
    spy_id = install(hub_db, name="spy", contributes={"events": []}, permissions=[RECORDS])
    enable(client, vault, "spy")
    spy_key = plugin_key(client, spy_id)
    stolen = {**spy_key, DELEGATION: owner[DELEGATION]}
    assert client.get(f"{BASE}/notes", headers=stolen).status_code == 403, "another plugin's delegation"
    assert keys(client, {**spy_key, DELEGATION: delegation_of(client, hub_db, "spy")}) == [], "records are the plugin's"
    # a forged, a malformed and a plugin token as a delegation; and a delegation without the plugin token
    for given in ("evd_" + "A" * 43, "garbage", vault["key"]["Authorization"].split()[1]):
        assert client.get(f"{BASE}/notes", headers={**vault["key"], DELEGATION: given}).status_code == 403
    assert client.get(f"{BASE}/notes", headers={DELEGATION: owner[DELEGATION]}).status_code == 401
    # the backend has no way to mint one
    for method in ("post", "put"):
        made = getattr(client, method)(
            f"/v1/plugin-api/projects/{PROJECT}/records/leases", json={}, headers=vault["key"]
        )
        assert made.status_code in (404, 405, 422)
    leases = tables.plugin_record_leases
    # an expired delegation opens nothing; a lease holds only the hash of its token
    assert client.get(f"{BASE}/notes", headers=owner).status_code == 200
    stored_hashes = [row[0] for row in live.sql(hub_db, select(leases.c.token_hash))]
    assert owner[DELEGATION] not in stored_hashes and all(len(h) == 64 for h in stored_hashes)
    live.sql(hub_db, update(leases).values(expires_at=tables.plugin_record_leases.c.created_at))
    assert client.get(f"{BASE}/notes", headers=owner).status_code == 403
    assert client.get(f"{BASE}/notes", headers=vault["key"]).status_code == 403, "and no fall back to no caller"


def delegation_of(client, db, plugin: str) -> str:
    """A delegation of ``plugin``'s own for a call of the owner (the spy has no route: mint through the hub's helper)."""
    from evo_agents.hub.server import plugin_delegation as delegations
    from evo_agents.hub.server.projects import access_of

    owner = user_id(db, OWNER)

    async def mint() -> str:
        async with client.app.state.engine.begin() as conn:
            access = await access_of(conn, owner, PROJECT)
            found = select(tables.plugins.c.id).where(tables.plugins.c.name == plugin)
            principal = SimpleNamespace(user_id=owner, kind="machine", scope=None)
            return await delegations.mint(conn, (await conn.execute(found)).scalar_one(), access, principal, None, 30)

    return client.portal.call(mint)


def test_a_delegation_never_outlives_the_grant_of_the_person_it_serves(client, vault):
    owner = delegation(client, vault, "owner")
    assert put(client, owner, "n").status_code == 201
    grant(client, vault, OWNER, "admin", "public")
    assert keys(client, owner) == [], "the grant was lowered after the lease: the lower one holds"
    assert absent(client, owner, "n").status_code == 404
    grant(client, vault, OWNER, "admin", "internal")
    assert keys(client, owner) == ["n"]
    revoked = client.delete(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", headers=vault["admin"])
    assert revoked.status_code in (200, 204), revoked.text
    assert client.get(f"{BASE}/notes", headers=owner).status_code == 403


def test_a_plugin_without_records_use_gets_no_delegation_and_no_records(client, vault, hub_db):
    plain_id = install(hub_db, name="plain", contributes={"routes": ROUTES}, permissions=[])
    response = client.post(
        f"/v1/projects/{PROJECT}/plugins/plain/enable", json={"permissions": []}, headers=vault["owner"]
    )
    assert response.status_code == 200
    client.get(f"/v1/x/plain/{PROJECT}/notes", headers=vault["owner"])
    assert "token" not in vault["host"].calls[-1].params["caller"]
    assert client.get(f"{BASE}/notes", headers=plugin_key(client, plain_id)).status_code == 403
    own = f"/v1/projects/{PROJECT}/plugins/plain/records/notes"
    assert client.get(own, headers=vault["owner"]).status_code == 404


def test_the_plugin_api_needs_a_live_plugin_token(client, vault):
    for headers in ({}, {"Authorization": "Bearer evp_nope"}, vault["owner"]):
        assert client.get(f"{BASE}/notes", headers=headers).status_code == 401


def test_no_plugin_reaches_the_records_of_another_project(client, vault):
    second = {
        **project_registration({"solo": "https://git.example.org/solo/solo.git"}),
        "sinks": OPEN_SINKS,
        "harness": {"name": "second", "workspace": "~/ws", "path": "solo-harness"},
    }
    assert client.put("/v1/projects/second", json=second, headers=vault["admin"]).status_code == 200
    grant_to = {"role": "admin", "max_level": "internal"}
    assert (
        client.put(f"/v1/admin/projects/second/grants/{OWNER}", json=grant_to, headers=vault["admin"]).status_code
        == 200
    )
    enabled = client.post(
        "/v1/projects/second/plugins/vault/enable",
        json={"permissions": [RECORDS], "records_level": "public"},
        headers=vault["owner"],
    )
    assert enabled.status_code == 200, enabled.text
    owner = delegation(client, vault, "owner")
    assert put(client, owner, "here").status_code == 201
    there = "/v1/plugin-api/projects/second/records/notes"
    assert client.get(there, headers=owner).status_code == 403, "a delegation of one project in another"
    assert client.get(there, headers=vault["key"]).json()["records"] == [], "and the job's reach is that project's"
    assert client.put(f"{there}/here", json={"body": {}, "level": "public"}, headers=vault["key"]).status_code == 201
    assert keys(client, owner) == ["here"] and client.get(f"{BASE}/notes/here", headers=owner).json()["body"] == {
        "text": "here"
    }

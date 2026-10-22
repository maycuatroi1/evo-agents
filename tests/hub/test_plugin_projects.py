"""Plugins in projects, as criterion a3 of the plan hub-agent-plugins names them: an admin of a project reads what an
installed plugin asks for, each permission with what it lets it do, and enables it by granting exactly that; writers
and readers only read; a plugin's config is checked against its schema, a secret field sealed and never shown again;
each write makes a new version of its layer and a rollback copies an older one; the effective config merges the
plugin's defaults, the hub's layer, the project's, an agent profile's and a run's; each run records the version and
the effective config digest of every plugin of its project when a worker claims it; each enable, disable, grant and
config change is audited. Then ``evo-agents hub plugin list --project|enable|disable|config|rollback`` against the hub
under uvicorn.

The plugin is written straight into the store's tables (``install``): publishing and installing a signed bundle is
``tests.hub.test_plugin_store``'s."""

from __future__ import annotations

import asyncio
import io
import json
import secrets

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.runs import AGENT_HOME
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.sealing import KEY_BYTES
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import write_credentials
from tests.hub.test_runs import OTHER, OWNER, PROJECT, add_worker, audit_rows, claim, dispatch, dispatched, members
from tests.hub.test_secrets_cli import run_cli
from tests.plugins.samples import manifest

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
CONFIG = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string", "default": "hello", "maxLength": 40},
        "retries": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "channel": {"enum": ["general", "ops"]},
        "token": {"type": "string", "x-secret": True, "minLength": 8},
    },
    "required": ["greeting", "token"],
}
TOKEN = "tok-" + secrets.token_hex(12)
ASKED = ["runs:read", "chats:write"]


def install(db, version: str = "1.0.0", *, permissions=ASKED, name: str = "sandbox") -> int:
    """Plugin ``name`` installed at ``version``, asking for ``permissions``, as the store would hold it."""
    p, v, users = tables.plugins, tables.plugin_versions, tables.users
    data = manifest(id=name, version=version, permissions=list(permissions), config=CONFIG)
    with live.engine(db).begin() as conn:
        admin_id = conn.execute(select(users.c.id).where(users.c.login == live.ADMIN)).scalar_one()
        plugin_id = conn.execute(select(p.c.id).where(p.c.name == name)).scalar()
        if plugin_id is None:
            plugin_id = conn.execute(insert(p).values(name=name, publisher="acme").returning(p.c.id)).scalar_one()
        row = {"plugin_id": plugin_id, "version": version, "sha256": "b" * 64, "size": 10, "r2_key": "blobs/sha256/b"}
        row |= {"publisher": "acme", "tier": "isolated", "manifest": data, "signature": {"kind": "ed25519"}}
        row |= {"signature_kind": "ed25519", "signed_by": "ed25519:0123", "published_by": admin_id}
        conn.execute(insert(v).values(row))
        installed = {"installed_version": version, "installed_by": admin_id, "installed_at": func.now()}
        conn.execute(update(p).where(p.c.id == plugin_id).values(installed))
    return plugin_id


def turn_off(db, reason: str | None, name: str = "sandbox") -> None:
    p = tables.plugins
    at = None if reason is None else func.now()
    live.sql(db, update(p).where(p.c.name == name).values(disabled_reason=reason, disabled_at=at))


def grants_of(db, name: str = "sandbox") -> dict:
    """``plugin_grants.project_grants`` of plugin ``name``, as step 4 reads it for a plugin's token."""
    import psycopg
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from evo_agents.hub.server.plugin_grants import project_grants

    async def read() -> dict:
        engine = create_async_engine(
            "postgresql+psycopg://", async_creator=lambda: psycopg.AsyncConnection.connect(db.dsn), poolclass=NullPool
        )
        try:
            async with engine.connect() as conn:
                return await project_grants(conn, name)
        finally:
            await engine.dispose()

    return asyncio.run(read())


@pytest.fixture
def client(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES))
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


def prepare(client, github, db) -> dict:
    """The members of ``tests.hub.test_runs``, owner an admin of the project, and plugin sandbox installed."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    install(db)
    return headers


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    return prepare(client, github, hub_db)


def path(name: str | None = None, action: str | None = None) -> str:
    base = f"/v1/projects/{PROJECT}/plugins"
    base = base if name is None else f"{base}/{name}"
    return base if action is None else f"{base}/{action}"


def enable(client, headers, permissions=ASKED, name: str = "sandbox"):
    return client.post(path(name, "enable"), json={"permissions": permissions}, headers=headers)


def enabled(client, headers, permissions=ASKED) -> dict:
    response = enable(client, headers, permissions)
    assert response.status_code == 200, response.text
    return response.json()


def write(client, headers, body: dict, agent: str | None = None):
    return client.post(
        path("sandbox", "config"), json=body, params={"agent": agent} if agent else None, headers=headers
    )


def written(client, headers, body: dict, agent: str | None = None) -> dict:
    response = write(client, headers, body, agent)
    assert response.status_code == 200, response.text
    return response.json()


def effective(client, headers, agent: str | None = None) -> dict:
    params = {"agent": agent} if agent else None
    response = client.get(path("sandbox", "config/effective"), params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def message(response, status: int) -> str:
    assert response.status_code == status, response.text
    return response.json()["message"]


def add_agent(client, headers, handle: str = "helper") -> None:
    made = client.post(f"/v1/projects/{PROJECT}/agents", json={"handle": handle, "name": "Helper"}, headers=headers)
    assert made.status_code == 201, made.text


def plugin_audit(db) -> list[tuple]:
    return audit_rows(db, "plugin")


# Enabling, and what a plugin may do


def test_plugin_consent_shows_what_it_asks_for_and_an_admin_enables_it_by_granting_exactly_that(client, hub, hub_db):
    for who in ("owner", "other", "reader"):
        listed = client.get(path(), headers=hub[who])
        assert listed.status_code == 200, listed.text
        by_name = {item["name"]: item for item in listed.json()}
        assert sorted(by_name) == sorted(["sandbox", *live.image_plugins()]), "with the core plugins of the image"
        plugin = by_name["sandbox"]
        assert (plugin["name"], plugin["version"], plugin["state"], plugin["enabled"]) == (
            "sandbox",
            "1.0.0",
            "installed",
            False,
        )
        assert plugin["permissions"] == [
            {"name": "runs:read", "text": "read the project's runs, their events and their outcomes", "granted": False},
            {"name": "chats:write", "text": "post messages in the project's group chats", "granted": False},
        ]
        assert (plugin["granted"], plugin["pending"], plugin["config_schema"]) == ([], [], CONFIG)
    assert client.get(path(), headers=hub["stranger"]).status_code == 404
    assert client.get(path(), headers=hub["admin"]).status_code == 403  # a hub admin without a grant
    assert client.get(path("nowhere"), headers=hub["owner"]).status_code == 404

    # writers and readers only read
    for who in ("other", "reader"):
        assert "needs the admin role on project evo-agents" in message(enable(client, hub[who]), 403)
    # the grant is exactly what the plugin asks for, read before it is given
    for wrong in (["runs:read"], [*ASKED, "plans:read"], ["runs:read", "runs:read", "chats:write"], []):
        refused = message(enable(client, hub["owner"], wrong), 422)
        assert "plugin sandbox 1.0.0 asks for runs:read, chats:write" in refused and "nothing changed" in refused
    assert grants_of(hub_db) == {}

    plugin = enabled(client, hub["owner"], ["chats:write", "runs:read"])
    assert (plugin["enabled"], plugin["granted"], plugin["pending"], plugin["enabled_by"]) == (True, ASKED, [], OWNER)
    assert all(item["granted"] for item in plugin["permissions"])
    assert client.get(path("sandbox"), headers=hub["reader"]).json()["enabled"] is True
    assert grants_of(hub_db) == {PROJECT: ("runs:read", "chats:write")}
    assert enabled(client, hub["owner"])["granted"] == ASKED  # the same grant again changes nothing
    target = f"{PROJECT}/plugin:sandbox@1.0.0"
    assert plugin_audit(hub_db) == [
        ("plugin.project-enable", target, OWNER, PROJECT),
        ("plugin.grant", f"{target} permissions=runs:read,chats:write", OWNER, PROJECT),
    ]


def test_plugin_enable_again_grants_what_an_upgrade_asks_for_and_a_turned_off_plugin_may_do_nothing(
    client, hub, hub_db
):
    enabled(client, hub["owner"])
    install(hub_db, "1.1.0", permissions=["runs:read", "plans:read"])
    plugin = client.get(path("sandbox"), headers=hub["other"]).json()
    # what it may do is what was granted that 1.1.0 still asks for: chats:write stays in the table, not in the grant
    assert (plugin["version"], plugin["granted"], plugin["pending"]) == ("1.1.0", ["runs:read"], ["plans:read"])
    assert [(item["name"], item["granted"]) for item in plugin["permissions"]] == [
        ("runs:read", True),
        ("plans:read", False),
    ]
    assert grants_of(hub_db) == {PROJECT: ("runs:read",)}  # granted and still asked for

    regranted = enabled(client, hub["owner"], ["runs:read", "plans:read"])
    assert (regranted["granted"], regranted["pending"]) == (["runs:read", "plans:read"], [])
    assert grants_of(hub_db) == {PROJECT: ("runs:read", "plans:read")}
    assert plugin_audit(hub_db)[-1] == (
        "plugin.grant",
        f"{PROJECT}/plugin:sandbox@1.1.0 permissions=runs:read,plans:read",
        OWNER,
        PROJECT,
    )

    turn_off(hub_db, "plugin sandbox 1.1.0 does not run on evo-agents 9.0.0")
    assert client.get(path("sandbox"), headers=hub["reader"]).json()["state"] == "disabled"
    assert "is turned off on this hub" in message(enable(client, hub["owner"], ["runs:read", "plans:read"]), 409)
    assert grants_of(hub_db) == {}
    turn_off(hub_db, None)

    for who in ("other", "reader"):
        assert client.post(path("sandbox", "disable"), headers=hub[who]).status_code == 403
    off = client.post(path("sandbox", "disable"), headers=hub["owner"])
    assert off.status_code == 200, off.text
    assert (off.json()["enabled"], off.json()["granted"]) == (False, [])
    assert grants_of(hub_db) == {}
    assert "is not enabled for project" in message(client.post(path("sandbox", "disable"), headers=hub["owner"]), 409)
    assert plugin_audit(hub_db)[-1] == ("plugin.project-disable", f"{PROJECT}/plugin:sandbox@1.1.0", OWNER, PROJECT)


# Config layers


def test_plugin_config_is_checked_against_its_schema_versioned_rolled_back_and_never_shows_a_secret(
    client, hub, hub_db
):
    assert "is not enabled for project" in message(client.get(path("sandbox", "config"), headers=hub["owner"]), 409)
    enabled(client, hub["owner"])
    empty = client.get(path("sandbox", "config"), headers=hub["reader"]).json()
    assert (empty["layer"], empty["version"], empty["values"], empty["secrets"]) == ("project", 0, {}, [])

    assert "needs the admin role" in message(write(client, hub["other"], {"values": {"greeting": "hi"}}), 403)
    refused = message(write(client, hub["owner"], {"values": {"retries": 9, "colour": "red", "token": "short"}}), 422)
    assert "retries: must be <= 5" in refused and "colour: not a field of the plugin's config" in refused
    assert "token: the value does not fit the field's schema" in refused and "short" not in refused

    first = written(client, hub["owner"], {"values": {"greeting": "hi", "token": TOKEN}})
    assert (first["version"], first["values"], first["secrets"], first["changed_by"]) == (
        1,
        {"greeting": "hi"},
        ["token"],
        OWNER,
    )
    second = written(client, hub["owner"], {"values": {"retries": 2}})
    assert (second["version"], second["values"], second["secrets"]) == (2, {"greeting": "hi", "retries": 2}, ["token"])
    assert written(client, hub["owner"], {"values": {"retries": 2}})["version"] == 2  # nothing changed, no version
    assert "is at version 2, not 1" in message(
        write(client, hub["owner"], {"unset": ["retries"], "if_version": 1}), 409
    )
    assert "nothing to unset" in message(write(client, hub["owner"], {"unset": ["channel"]}), 422)
    assert "both set and unset" in message(
        write(client, hub["owner"], {"values": {"retries": 1}, "unset": ["retries"]}), 422
    )
    third = written(client, hub["owner"], {"unset": ["retries"], "if_version": 2})
    assert (third["version"], third["values"]) == (3, {"greeting": "hi"})

    versions = client.get(path("sandbox", "config/versions"), headers=hub["reader"]).json()
    assert [(v["version"], v["values"], v["secrets"]) for v in versions] == [
        (3, {"greeting": "hi"}, ["token"]),
        (2, {"greeting": "hi", "retries": 2}, ["token"]),
        (1, {"greeting": "hi"}, ["token"]),
    ]
    rollback = path("sandbox", "config/rollback")
    assert client.post(rollback, json={"version": 2}, headers=hub["other"]).status_code == 403
    back = client.post(rollback, json={"version": 2}, headers=hub["owner"])
    assert back.status_code == 200, back.text
    assert (back.json()["version"], back.json()["values"], back.json()["rolled_back_to"]) == (4, second["values"], 2)
    assert "roll back to an older one" in message(client.post(rollback, json={"version": 4}, headers=hub["owner"]), 409)
    stale = client.post(rollback, json={"version": 1, "if_version": 3}, headers=hub["owner"])
    assert "is at version 4, not 3" in message(stale, 409)

    # an agent profile's layer
    add_agent(client, hub["owner"])
    agent = written(client, hub["owner"], {"values": {"channel": "ops"}}, agent="helper")
    assert (agent["layer"], agent["agent"], agent["version"], agent["values"]) == (
        "agent",
        "helper",
        1,
        {"channel": "ops"},
    )
    assert "has no agent @nobody" in message(write(client, hub["owner"], {"values": {}}, agent="nobody"), 404)

    # the secret is sealed and never comes back, in an answer, the table or the audit
    configs = tables.plugin_configs
    stored = live.sql(hub_db, select(configs.c.secrets).where(configs.c.version == 1, configs.c.layer == "project"))
    assert set(stored[0][0]["token"]) == {"ciphertext", "nonce", "key_id"}
    dump = json.dumps([row[0] for row in live.sql(hub_db, select(configs.c.secrets, configs.c["values"]))])
    assert TOKEN not in dump and TOKEN not in json.dumps(versions)
    project = f"{PROJECT}/plugin:sandbox config project"
    assert [row[1] for row in plugin_audit(hub_db) if row[0] == "plugin.config"] == [
        f"{project} v1 fields=greeting,token",
        f"{project} v2 fields=retries",
        f"{project} v3 fields=retries",
        f"{project} v4 rollback-to=v2",
        f"{PROJECT}/@helper/plugin:sandbox config agent v1 fields=channel",
    ]
    assert TOKEN not in str(plugin_audit(hub_db))

    # without the hub's key no secret is written, while plain values still are
    client.app.state.sealer, sealer = None, client.app.state.sealer
    try:
        assert "EVO_HUB_SECRETS_KEY" in message(write(client, hub["owner"], {"values": {"token": TOKEN}}), 503)
        assert written(client, hub["owner"], {"values": {"greeting": "plain"}})["version"] == 5
    finally:
        client.app.state.sealer = sealer


def test_plugin_config_instance_layer_is_the_hub_admins_and_effective_config_merges_every_layer(client, hub, hub_db):
    instance = "/v1/plugins/sandbox/config"
    assert client.post(instance, json={"values": {"retries": 3}}, headers=hub["owner"]).status_code == 403
    hub_layer = client.post(instance, json={"values": {"greeting": "from the hub", "retries": 3}}, headers=hub["admin"])
    assert hub_layer.status_code == 200, hub_layer.text
    assert (hub_layer.json()["layer"], hub_layer.json()["version"], hub_layer.json()["project"]) == (
        "instance",
        1,
        None,
    )
    assert client.get(instance, headers=hub["admin"]).json()["values"] == {"greeting": "from the hub", "retries": 3}
    assert len(client.get(f"{instance}/versions", headers=hub["admin"]).json()) == 1
    assert client.post("/v1/plugins/nowhere/config", json={"values": {}}, headers=hub["admin"]).status_code == 404

    enabled(client, hub["owner"])
    bare = effective(client, hub["reader"])
    assert bare["agent"] == PROJECT  # the default profile: what a run that names no agent gets
    assert {f["name"]: (f["value"], f["layer"], f["version"]) for f in bare["fields"]} == {
        "greeting": ("from the hub", "instance", 1),
        "retries": (3, "instance", 1),
    }
    assert bare["problems"] == ["token: required, and no layer sets it"]

    written(client, hub["owner"], {"values": {"greeting": "from the project", "token": TOKEN}})
    add_agent(client, hub["owner"])
    written(client, hub["owner"], {"values": {"retries": 4}}, agent="helper")
    project = effective(client, hub["reader"])
    assert {f["name"]: (f["value"], f["secret"], f["layer"], f["version"]) for f in project["fields"]} == {
        "greeting": ("from the project", False, "project", 1),
        "retries": (3, False, "instance", 1),
        "token": (None, True, "project", 1),
    }
    assert project["problems"] == [] and project["digest"].startswith("sha256:") and TOKEN not in json.dumps(project)
    helper = effective(client, hub["reader"], agent="helper")
    assert {f["name"]: (f["value"], f["layer"]) for f in helper["fields"]}["retries"] == (4, "agent")
    assert helper["digest"] != project["digest"]
    # a rollback that copies the secret's sealed value gives the digest back
    written(client, hub["owner"], {"values": {"token": "tok-another-one"}})
    assert effective(client, hub["reader"])["digest"] != project["digest"]
    assert client.post(path("sandbox", "config/rollback"), json={"version": 1}, headers=hub["owner"]).status_code == 200
    assert effective(client, hub["reader"])["digest"] == project["digest"]


# Runs


def test_plugin_config_of_a_run_is_its_own_layer_and_its_claim_records_version_and_digest(client, hub, hub_db):
    enabled(client, hub["owner"])
    written(client, hub["owner"], {"values": {"greeting": "from the project", "token": TOKEN}})
    add_agent(client, hub["owner"])
    for bad, why in (
        ({"sandbox": {"token": "tok-12345678"}}, "token: a secret, which a run's layer may not set"),
        ({"sandbox": {"retries": 7}}, "sandbox: retries: must be <= 5"),
        ({"elsewhere": {"greeting": "hi"}}, "elsewhere: not a plugin enabled on project evo-agents"),
    ):
        refused = message(dispatch(client, hub["owner"], [2], plugin_config=bad), 422)
        assert why in refused and "nothing was dispatched" in refused
    assert dispatch(client, hub["owner"], [2], plugin_config={"Bad Name": {}}).status_code == 422

    worker = add_worker(client, hub["owner"], "mac-mini", slots=2)
    home = {"runtimes": worker["runtimes"], "checkouts": worker["checkouts"], "free_slots": 2, "runs": []}
    home["features"] = [AGENT_HOME]  # a run under the profile helper needs it (tests.hub.test_claim_agent_home)
    assert client.post("/v1/worker/heartbeat", json=home, headers=worker["headers"]).status_code == 200
    own = dispatched(client, hub["owner"], [2], profile="helper", plugin_config={"sandbox": {"channel": "ops"}})[0]
    plain = dispatched(client, hub["owner"], [4])[0]
    reader = hub["reader"]
    assert client.get(f"/v1/projects/{PROJECT}/runs/{own['id']}/plugins", headers=reader).json() == []
    claimed = {claim(client, worker)["id"], claim(client, worker)["id"]}
    assert claimed == {own["id"], plain["id"]}

    def recorded(run_id: int) -> list[dict]:
        response = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/plugins", headers=reader)
        assert response.status_code == 200, response.text
        return response.json()

    (record,) = recorded(plain["id"])
    assert (record["name"], record["version"]) == ("sandbox", "1.0.0")
    assert record["config_digest"] == effective(client, reader)["digest"]  # no layer of its own: the project's
    (with_own,) = recorded(own["id"])
    helper = effective(client, reader, agent="helper")
    assert with_own["config_digest"] not in (helper["digest"], record["config_digest"])  # its channel counts
    runs = tables.runs
    assert live.sql(hub_db, select(runs.c.plugin_config).where(runs.c.id == own["id"])) == [
        ({"sandbox": {"channel": "ops"}},)
    ]
    # what a run ran with stays as its claim found it
    written(client, hub["owner"], {"values": {"greeting": "changed later"}})
    assert recorded(plain["id"])[0]["config_digest"] == record["config_digest"]
    assert client.get(f"/v1/projects/{PROJECT}/runs/{plain['id']}/plugins", headers=hub["stranger"]).status_code == 404


# The command line


def test_hub_plugin_enable_and_config_cli_grant_write_roll_back_and_print_the_effective_config(
    hub_db, tmp_path, github, monkeypatch, capsys
):
    from tests.hub.test_run_stream import serving

    config = live.hub_config(hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES))
    with serving(create_app(config)) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = prepare(client, github, hub_db)
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", OTHER), ("admin", live.ADMIN))
        }

        def plugin_cli(who: str, *args: str, stdin=None):
            return run_cli(monkeypatch, capsys, homes[who], "hub", "plugin", *args, stdin=stdin)

        def ok(result):
            assert result.code == 0, result.out + result.err
            return result

        listed = ok(plugin_cli("other", "list", "--project", PROJECT)).out
        assert "sandbox" in listed and "not enabled" in listed and "runs:read, chats:write" in listed
        assert_json_keys(
            "hub plugin list",
            json.loads(ok(plugin_cli("other", "list", "--project", PROJECT, "--json")).out),
            "--project",
        )
        unasked = plugin_cli("owner", "enable", "sandbox", "--project", PROJECT, stdin=io.StringIO(""))
        assert unasked.code == 1 and "pass --yes to grant it" in unasked.err
        assert "runs:read: read the project's runs, their events and their outcomes" in unasked.out
        assert "pass --yes" in plugin_cli("owner", "enable", "sandbox", "--project", PROJECT, "--json").err
        refused = plugin_cli("other", "enable", "sandbox", "--project", PROJECT, "--yes")
        assert refused.code == 1 and "needs the admin role" in refused.err
        out = ok(plugin_cli("owner", "enable", "sandbox", "--project", PROJECT, "--yes")).out
        assert out.endswith(f"Enabled plugin sandbox 1.0.0 for project {PROJECT}.\n")
        again = json.loads(ok(plugin_cli("owner", "enable", "sandbox", "--project", PROJECT, "--yes", "--json")).out)
        assert_json_keys("hub plugin enable", again)

        # the project's layer: --set as JSON or text, a secret from stdin, never on the command line or printed
        base = ("config", "sandbox", "--project", PROJECT)
        set_ = ok(plugin_cli("owner", *base, "--set", "greeting=hi there", "--set", "retries=2", "--json"))
        layer = json.loads(set_.out)
        assert_json_keys("hub plugin config", layer)
        assert (layer["version"], layer["values"]) == (1, {"greeting": "hi there", "retries": 2})
        secret = ok(plugin_cli("owner", *base, "--secret", "token", stdin=io.StringIO(TOKEN + "\n")))
        assert "token = (secret, set)" in secret.out and TOKEN not in secret.out + secret.err
        bad = plugin_cli("owner", *base, "--set", "retries=9")
        assert bad.code == 1 and "retries: must be <= 5" in bad.err
        assert "needs the admin role" in plugin_cli("other", *base, "--set", "retries=1").err
        shown = ok(plugin_cli("other", *base)).out
        assert 'greeting = "hi there"' in shown and "v2" in shown and TOKEN not in shown
        versions = json.loads(ok(plugin_cli("other", *base, "--versions", "--json")).out)
        assert_json_keys("hub plugin config", versions, "--versions")
        assert [v["version"] for v in versions] == [2, 1]
        rolled = ok(plugin_cli("owner", "rollback", "sandbox", "--project", PROJECT, "--to", "1")).out
        assert rolled == f"Rolled the layer of project {PROJECT} of plugin sandbox back to v1, as v3.\n"

        # the effective config of every plugin the project enabled
        printed = ok(plugin_cli("other", "config", "--project", PROJECT, "--effective")).out
        assert f"Plugin sandbox 1.0.0 in project {PROJECT}, for @{PROJECT}:" in printed
        assert '  greeting = "hi there"  [project v3]' in printed and "  problem: token: required" in printed
        found = json.loads(ok(plugin_cli("other", "config", "--project", PROJECT, "--effective", "--json")).out)
        assert_json_keys("hub plugin config", found, "--effective")
        assert found[0]["digest"] in printed
        assert "name the layer" in plugin_cli("owner", "config", "sandbox").err
        assert (
            "--instance is the hub's own layer"
            in plugin_cli("owner", "config", "sandbox", "--instance", "--effective").err
        )

        # the hub's own layer, for hub admins
        hub_layer = ok(plugin_cli("admin", "config", "sandbox", "--instance", "--set", "channel=ops")).out
        assert hub_layer.startswith("Plugin sandbox, the hub's own layer, v1, by octo-admin")
        assert "needs a hub admin" in plugin_cli("owner", "config", "sandbox", "--instance", "--set", "channel=ops").err
        assert (
            'channel = "ops"  [instance v1]'
            in ok(plugin_cli("other", "config", "sandbox", "--project", PROJECT, "--effective")).out
        )

        missing = plugin_cli("other", "config", "--project", PROJECT, "--run", "999")
        assert missing.code == 1 and "has no run 999" in missing.err
        off = ok(plugin_cli("owner", "disable", "sandbox", "--project", PROJECT)).out
        assert off == f"Disabled plugin sandbox for project {PROJECT}; the versions of its config stay.\n"

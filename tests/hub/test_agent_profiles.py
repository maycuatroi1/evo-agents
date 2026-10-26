"""The agent profiles of a project (``evo_agents.hub.server.agent_profiles``), as criterion a2 of the plan
agent-profiles-group-chat names them: an admin of the project creates, edits and soft-deletes a profile (a handle unique
on the hub and never a project's name, a name, an avatar in the blob store or none, a colour, a description,
instructions, seven axes each all or an allowlist, all by default, the version raised by every edit); every project
has a default profile with every permission, made at its registration or by migration 0030 for a project before it;
writers and readers read profiles, and a writer who changes one gets 403. ``hub agent`` does the same from the command
line. The rules the hub applies to runs are in ``tests.hub.test_profile_runs``."""

import base64
import hashlib
import json
import secrets

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from evo_agents.hub import profiles, tables
from evo_agents.hub.contract import commands
from evo_agents.hub.server.app import create_app
from tests.hub import test_secrets_cli as secrets_cli
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_migrate import move_to, seed
from tests.hub.test_plans import registration
from tests.hub.test_run_cli import as_json, ok
from tests.hub.test_runs import OWNER, PROJECT, audit_rows, members
from tests.hub.test_secrets_cli import run_cli

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + secrets.token_bytes(64)
LIMITED = {
    "credential": {"mode": "allowlist", "allow": ["claude-b", "github-app"]},
    "tool": {"mode": "allowlist", "allow": ["read", "edit", "mcp:github"]},
    "skill": {"mode": "allowlist", "allow": ["create-exec-plan"]},
    "worker": {"mode": "allowlist", "allow": ["Mac-Mini", "label:GPU"]},
    "model": {"mode": "allowlist", "allow": ["claude-sonnet-4-5"]},
    "runtime": {"mode": "allowlist", "allow": ["claude-code"]},
}


@pytest.fixture
def client(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """The project and members of ``tests.hub.test_runs``, the owner made an admin of the project: the headers of
    admin (the hub's, without a grant), owner (project admin), other (writer), reader and stranger (no grant)."""
    headers = members(client, github)
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    return headers


def agents_path(handle: str | None = None, project: str = PROJECT) -> str:
    path = f"/v1/projects/{project}/agents"
    return path if handle is None else f"{path}/{handle}"


def create(client, headers, handle: str, **body):
    return client.post(agents_path(), json={"handle": handle, "name": handle.title(), **body}, headers=headers)


def created(client, headers, handle: str, **body) -> dict:
    response = create(client, headers, handle, **body)
    assert response.status_code == 201, response.text
    return response.json()


def edit(client, headers, handle: str, **body):
    return client.patch(agents_path(handle), json=body, headers=headers)


def every_axis(**limited) -> dict:
    """Permissions as the API shows them: every axis all, with an empty allow list, but those of ``limited``."""
    return {axis: limited.get(axis, {"mode": "all", "allow": []}) for axis in profiles.AXES}


# The default profile


def test_a_registered_project_has_a_default_profile_with_every_permission_that_members_read(client, hub, hub_db):
    for who in ("owner", "other", "reader"):
        listed = client.get(agents_path(), headers=hub[who])
        assert listed.status_code == 200, listed.text
        (default,) = listed.json()
        assert (default["handle"], default["name"], default["is_default"], default["version"]) == (
            PROJECT,
            PROJECT,
            True,
            1,
        )
        assert default["permissions"] == every_axis() and default["restricted"] == []
        assert (default["instructions"], default["icon_url"], default["color"]) == ("", None, profiles.DEFAULT_COLOR)
    versions = client.get(agents_path(PROJECT) + "/versions", headers=hub["reader"]).json()
    assert [(v["version"], v["body"]["handle"], v["body"]["permissions"]) for v in versions] == [
        (1, PROJECT, profiles.all_access())
    ]
    # someone without a grant learns nothing; a hub admin without one manages the project but reads no agent
    assert client.get(agents_path(), headers=hub["stranger"]).status_code == 404
    assert client.get(agents_path(), headers=hub["admin"]).status_code == 403


def test_the_default_profile_keeps_every_permission_and_no_instructions_and_is_never_deleted(client, hub, hub_db):
    limited = edit(client, hub["owner"], PROJECT, permissions={"tool": {"mode": "allowlist", "allow": ["read"]}})
    assert limited.status_code == 422 and "its permissions stay all" in limited.json()["message"]
    instructed = edit(client, hub["owner"], PROJECT, instructions="Be terse.")
    assert instructed.status_code == 422 and "it has no instructions" in instructed.json()["message"]
    deleted = client.delete(agents_path(PROJECT), headers=hub["owner"])
    assert deleted.status_code == 409 and "never deleted" in deleted.json()["message"]
    renamed = edit(client, hub["owner"], PROJECT, name="Evo agents", description="Everything.", color="#0f766e")
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["version"], renamed.json()["is_default"]) == ("Evo agents", 2, True)


@pytest.mark.empty_db
def test_migration_0030_gives_every_project_before_it_a_default_profile_with_its_version_row(hub_db):
    move_to(hub_db, "0029")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        t = tables
        other = insert(t.projects).values(
            name="second",
            levels=["public"],
            locations=["any"],
            default_label={"level": "public"},
            created_by=ids["user"],
        )
        conn.execute(other)
    move_to(hub_db, "0030")
    ap, v = tables.agent_profiles, tables.agent_profile_versions
    rows = sql(
        hub_db, select(ap.c.handle, ap.c.name, ap.c.is_default, ap.c.permissions, ap.c.version).order_by(ap.c.id)
    )
    seven = {axis: {"mode": "all"} for axis in profiles.AXES if axis != "environment"}  # 0040 adds the eighth
    assert rows == [("demo", "demo", True, seven, 1), ("second", "second", True, seven, 1)]
    bodies = sql(hub_db, select(v.c.version, v.c.body).order_by(v.c.profile_id))
    assert [(version, body["handle"], body["is_default"]) for version, body in bodies] == [
        (1, "demo", True),
        (1, "second", True),
    ]
    move_to(hub_db, "0029", down=True)
    move_to(hub_db, "0030")
    assert len(sql(hub_db, select(ap.c.id))) == 2


# Creating, editing, deleting


def test_an_admin_creates_edits_and_deletes_a_profile_and_each_edit_raises_its_version(client, hub, hub_db):
    made = created(
        client,
        hub["owner"],
        "reviewer",
        description="Reviews pull requests.",
        instructions="Review, never push.",
        permissions=LIMITED,
    )
    assert (made["handle"], made["project"], made["version"], made["is_default"]) == ("reviewer", PROJECT, 1, False)
    assert made["created_by"] == OWNER and made["color"] == profiles.default_color("reviewer")
    # worker names and labels are kept lowered, as the hub compares them; every axis not named allows all
    assert made["permissions"]["worker"] == {"mode": "allowlist", "allow": ["mac-mini", "label:gpu"]}
    assert made["permissions"]["project"] == {"mode": "all", "allow": []}
    assert made["restricted"] == ["credential", "tool", "skill", "worker", "model", "runtime"]

    renamed = edit(client, hub["owner"], "reviewer", name="Reviewer bot")
    assert renamed.json()["version"] == 2, renamed.text
    same = edit(client, hub["owner"], "reviewer", name="Reviewer bot")
    assert same.json()["version"] == 2, "a value it has already changes nothing"
    opened = edit(client, hub["owner"], "reviewer", permissions={"tool": {"mode": "all"}}, if_version=2)
    assert opened.status_code == 200, opened.text
    assert opened.json()["version"] == 3 and opened.json()["restricted"] == []
    stale = edit(client, hub["owner"], "reviewer", name="Late", if_version=2)
    assert stale.status_code == 409 and "is at version 3, not 2" in stale.json()["message"]

    versions = client.get(agents_path("reviewer") + "/versions", headers=hub["other"]).json()
    assert [(v["version"], v["body"]["name"], v["changed_by"]) for v in versions] == [
        (3, "Reviewer bot", OWNER),
        (2, "Reviewer bot", OWNER),
        (1, "Reviewer", OWNER),
    ]
    assert versions[2]["body"]["permissions"]["credential"] == LIMITED["credential"]

    deleted = client.delete(agents_path("reviewer"), headers=hub["owner"])
    assert deleted.status_code == 200 and deleted.json()["deleted_at"] is not None
    assert [a["handle"] for a in client.get(agents_path(), headers=hub["reader"]).json()] == [PROJECT]
    with_deleted = client.get(agents_path() + "?deleted=true", headers=hub["reader"]).json()
    assert [a["handle"] for a in with_deleted] == [PROJECT, "reviewer"]
    assert client.get(agents_path("reviewer"), headers=hub["reader"]).json()["name"] == "Reviewer bot"
    again = edit(client, hub["owner"], "reviewer", name="Back")
    assert again.status_code == 409 and "changes no more" in again.json()["message"]

    assert audit_rows(hub_db, "agent") == [
        ("agent.create", f"{PROJECT}/@reviewer v1", OWNER, PROJECT),
        ("agent.update", f"{PROJECT}/@reviewer v2 fields=name", OWNER, PROJECT),
        ("agent.update", f"{PROJECT}/@reviewer v3 fields=permissions", OWNER, PROJECT),
        ("agent.delete", f"{PROJECT}/@reviewer v3", OWNER, PROJECT),
    ]
    assert "Review, never push." not in json.dumps(audit_rows(hub_db, "agent"))


def test_a_writer_or_a_reader_who_changes_a_profile_gets_403(client, hub, hub_db):
    created(client, hub["owner"], "helper")
    for who in ("other", "reader"):
        refused = create(client, hub[who], f"made-by-{who}")
        assert refused.status_code == 403 and "needs the admin role" in refused.json()["message"]
        assert edit(client, hub[who], "helper", name="Mine").status_code == 403
        assert client.delete(agents_path("helper"), headers=hub[who]).status_code == 403
        assert client.put(agents_path("helper") + "/avatar", json=avatar_body(PNG), headers=hub[who]).status_code == 403
        assert client.get(agents_path("helper"), headers=hub[who]).status_code == 200
    assert client.get(agents_path("helper"), headers=hub["owner"]).json()["version"] == 1


def test_a_profile_handle_is_unique_on_the_hub_and_never_a_project_name(client, hub, hub_db):
    taken = create(client, hub["owner"], PROJECT)
    assert taken.status_code == 409 and f"is the name of project {PROJECT}" in taken.json()["message"]
    created(client, hub["owner"], "builder")
    assert create(client, hub["owner"], "builder").status_code == 409
    assert client.delete(agents_path("builder"), headers=hub["owner"]).status_code == 200
    reused = create(client, hub["owner"], "builder")
    assert reused.status_code == 409 and "deleted ones included" in reused.json()["message"]
    # a project of that name would make @builder name two agents
    clash = client.put("/v1/projects/builder", json=registration(), headers=hub["admin"])
    assert clash.status_code == 409 and f"handle of an agent of project {PROJECT}" in clash.json()["message"]
    assert create(client, hub["owner"], "Not_A_Handle").status_code == 422
    # a new project gets its own default profile at once
    assert client.put("/v1/projects/second", json=registration(), headers=hub["admin"]).status_code == 200
    ap = tables.agent_profiles
    assert sql(hub_db, select(ap.c.handle, ap.c.is_default).where(ap.c.handle == "second")) == [("second", True)]


@pytest.mark.parametrize(
    ("permissions", "problem"),
    [
        ({"tool": {"mode": "allowlist", "allow": ["shell"]}}, "tool 'shell' is none of read, edit, bash, web, agent"),
        ({"runtime": {"mode": "allowlist", "allow": ["gemini"]}}, "runtime 'gemini' is none of"),
        ({"credential": {"mode": "all", "allow": ["claude-a"]}}, "an axis of mode all takes no allow list"),
        ({"worker": {"mode": "allowlist", "allow": ["label:"]}}, "is not label:<a worker label>"),
        ({"worker": {"mode": "allowlist", "allow": ["pool:"]}}, "is not pool:<a worker pool's name>"),
        ({"model": {"mode": "allowlist", "allow": ["two\nlines"]}}, "is not a model's name"),
    ],
)
def test_a_profile_permission_outside_its_axis_vocabulary_is_422(client, hub, hub_db, permissions, problem):
    refused = create(client, hub["owner"], "picky", permissions=permissions)
    assert refused.status_code == 422, refused.text
    assert any(problem in item["msg"] for item in refused.json()["detail"]), refused.json()


# The avatar


def avatar_body(data: bytes) -> dict:
    return {"data": base64.b64encode(data).decode()}


def test_a_profile_avatar_goes_to_the_blob_store_and_comes_back_from_its_icon_url(client, hub, hub_db, s3):
    created(client, hub["owner"], "painter")
    put = client.put(agents_path("painter") + "/avatar", json=avatar_body(PNG), headers=hub["owner"])
    assert put.status_code == 200, put.text
    agent = put.json()
    sha = hashlib.sha256(PNG).hexdigest()
    assert (agent["avatar_sha256"], agent["version"]) == (sha, 2)
    assert agent["icon_url"] == f"/v1/projects/{PROJECT}/agents/painter/avatar?v={sha[:16]}"
    served = client.get(agent["icon_url"], headers=hub["reader"])
    assert served.status_code == 200 and served.content == PNG
    assert served.headers["content-type"] == "image/png" and served.headers["x-content-type-options"] == "nosniff"
    assert s3.get(f"blobs/sha256/{sha}") == PNG
    b = tables.blobs
    assert sql(hub_db, select(b.c.kind, b.c.size).where(b.c.sha256 == sha)) == [("agent-avatar", len(PNG))]

    text = client.put(agents_path("painter") + "/avatar", json=avatar_body(b"<svg/>"), headers=hub["owner"])
    assert text.status_code == 422 and "PNG, JPEG, WebP" in text.json()["message"]
    big = b"\x89PNG\r\n\x1a\n" + bytes(512 * 1024)
    too_big = client.put(agents_path("painter") + "/avatar", json=avatar_body(big), headers=hub["owner"])
    assert too_big.status_code in (413, 422), too_big.text
    dropped = client.delete(agents_path("painter") + "/avatar", headers=hub["owner"])
    assert dropped.status_code == 200 and dropped.json()["icon_url"] is None and dropped.json()["version"] == 3
    assert client.get(agents_path("painter") + "/avatar", headers=hub["reader"]).status_code == 404


# The command line


def test_the_profile_commands_are_in_the_contract():
    printed = commands()
    names = {"hub agent list", "hub agent show", "hub agent create", "hub agent edit", "hub agent delete"}
    assert names <= set(printed)
    assert printed["hub agent list"]["json"] == {
        "kind": "array",
        "keys": printed["hub agent show"]["json"]["keys"],
        "schema": "Agent",
    }
    for command in ("hub run dispatch", "hub run plan", "hub run author"):
        assert "--agent" in {o["flags"][0] for o in printed[command]["options"]}, command
    assert "--agent" not in {o["flags"][0] for o in printed["hub run rerun"]["options"]}


if pg.DSN:
    app_key, cli_hub = secrets_cli.app_key, secrets_cli.hub


def agent_cli(monkeypatch, capsys, home, *args: str):
    return run_cli(monkeypatch, capsys, home, "hub", "agent", *args, "--project", PROJECT)


def test_hub_agent_creates_lists_shows_edits_and_deletes_a_profile(cli_hub, monkeypatch, capsys, tmp_path):
    grant = cli_hub.client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=cli_hub.headers["admin"]
    )
    assert grant.status_code == 200, grant.text
    owner, other = cli_hub.homes["owner"], cli_hub.homes["other"]
    instructions = tmp_path / "instructions.md"
    instructions.write_text("Read and report; change nothing.\n", encoding="utf-8")
    made = agent_cli(
        monkeypatch,
        capsys,
        owner,
        "create",
        "@auditor",
        "--name",
        "Auditor",
        "--allow",
        "tool:read",
        "--allow",
        "credential:claude-b",
        "--none",
        "skill",
        "--instructions-file",
        str(instructions),
        "--json",
    )
    agent = as_json(made)
    assert_json_keys("hub agent create", agent)
    assert (agent["handle"], agent["version"], agent["avatar_sha256"]) == ("auditor", 1, None)
    assert agent["permissions"]["skill"] == {"mode": "allowlist", "allow": []}
    assert agent["instructions"] == "Read and report; change nothing.\n"

    listed = as_json(agent_cli(monkeypatch, capsys, other, "list", "--json"))
    assert_json_keys("hub agent list", listed)
    assert [a["handle"] for a in listed] == [PROJECT, "auditor"]
    table = ok(agent_cli(monkeypatch, capsys, other, "list")).out
    assert "@auditor" in table and "credential, tool, skill" in table
    shown = ok(agent_cli(monkeypatch, capsys, other, "show", "auditor")).out
    assert shown.startswith(f"@auditor (Auditor) of project {PROJECT}, version 1\n")
    assert "  tool:       read\n" in shown and "  skill:      none\n" in shown and "  model:      all\n" in shown

    # edit changes only the axes it names, from the version it read
    changed = as_json(agent_cli(monkeypatch, capsys, owner, "edit", "auditor", "--all", "skill", "--json"))
    assert changed["version"] == 2 and changed["permissions"]["tool"] == {"mode": "allowlist", "allow": ["read"]}
    assert changed["permissions"]["skill"] == {"mode": "all", "allow": []}
    refused = agent_cli(monkeypatch, capsys, other, "edit", "auditor", "--name", "Mine")
    assert refused.code == 1 and "needs the admin role" in refused.err
    nothing = agent_cli(monkeypatch, capsys, owner, "edit", "auditor")
    assert nothing.code == 2 and "give something to change" in nothing.err
    wrong = agent_cli(monkeypatch, capsys, owner, "edit", "auditor", "--allow", "tools:read")
    assert wrong.code == 2 and "--allow takes AXIS:ITEM" in wrong.err

    gone = ok(agent_cli(monkeypatch, capsys, owner, "delete", "auditor"))
    assert gone.out == f"Deleted @auditor of project {PROJECT}, version 2.\n"
    assert [a["handle"] for a in as_json(agent_cli(monkeypatch, capsys, other, "list", "--json"))] == [PROJECT]

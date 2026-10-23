"""Who reads a global skill (``evo_agents.hub.server.skills``, plan autonomous-oss-growth, criterion a3): everyone signed
in, or hub admins alone. A member who is not a hub admin never sees an admins skill through the API (list, detail,
bundle), the skill_list tool, ``hub skills list`` and ``hub skills sync``, nor through the SessionStart check of the
evo-hub plugin; the agent of a run sees what the run's owner sees. A hub admin sets the audience when publishing and
changes it through the API and ``hub skills audience``, each change one audit row; a project skill stays everyone's.
Migration 0111 leaves every skill that exists everyone's.

The checks of the client run without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub import hooks
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import commands
from evo_agents.hub.skill_sync import list_skills, publish, set_audience, sync
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_skills import hub, own_home, sign_in, skills_cli, write_skill  # noqa: F401 (hub, own_home: fixtures)
from tests.hub.test_skills import ok as cli_ok

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

AUDIENCE = "/v1/skills/global/{name}/audience"


def audit_rows(db) -> list[tuple]:
    """The target and project of the audit rows of audience changes, oldest first."""
    from sqlalchemy import select

    from evo_agents.hub import tables

    audit = tables.audit
    query = select(audit.c.target, audit.c.project_id).where(audit.c.action == "skill.audience")
    return live.sql(db, query.order_by(audit.c.id))


def names(found: list[dict]) -> list[tuple[str, str]]:
    return [(s["name"], s["audience"]) for s in found]


# Without the hub


class Recording:
    """A hub that records what it is asked and answers nothing."""

    url = "https://hub.test"

    def __init__(self):
        self.calls: list[tuple] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path, body))
        raise AssertionError("nothing should reach the hub")


def test_the_audience_command_and_option_are_in_the_contract():
    contract = commands()
    audience = contract["hub skills audience"]
    assert [p["name"] for p in audience["positionals"]] == ["name", "audience"]
    assert audience["positionals"][1]["choices"] == ["everyone", "admins"]
    assert audience["json"] == {"kind": "object", "keys": ["name", "audience", "changed"], "schema": "SkillAudience"}
    (option,) = [o for o in contract["hub skills publish"]["options"] if o["flags"] == ["--audience"]]
    assert option["choices"] == ["everyone", "admins"] and not option["required"]
    assert "audience" in contract["hub skills list"]["json"]["keys"]
    assert "audience" in contract["hub skills publish"]["json"]["keys"]


def test_an_audience_the_hub_would_refuse_is_refused_before_anything_is_sent(tmp_path):
    recording = Recording()
    skill = write_skill(tmp_path, "house-style")
    for audience, project, said in (
        ("nobody", None, "--audience takes everyone or admins"),
        ("admins", "demo", "--audience admins is a global skill's"),
    ):
        with pytest.raises(HubError, match=said):
            publish(recording, skill, project, audience=audience)
    with pytest.raises(HubError, match="--audience takes everyone or admins"):
        set_audience(recording, "house-style", "members")
    with pytest.raises(HubError):
        set_audience(recording, "../escape", "admins")
    assert recording.calls == []


# The API


@needs_pg
def test_a_member_never_sees_lists_or_downloads_a_skill_for_admins(hub, tmp_path):
    admin, alice = hub.as_(live.ADMIN), hub.as_("alice")
    source = tmp_path / "source"
    everyone = publish(admin, write_skill(source, "house-style"))
    admins = publish(admin, write_skill(source, "release-keys"), audience="admins")
    assert (everyone["audience"], admins["audience"]) == ("everyone", "admins")
    team = publish(alice, write_skill(source, "team-notes"), "demo", audience="everyone")
    assert team["audience"] == "everyone"

    assert names(list_skills(admin)) == [("house-style", "everyone"), ("release-keys", "admins")]
    for login in ("alice", "carol", "stranger"):
        member = hub.as_(login)
        assert ("release-keys", "admins") not in names(list_skills(member))
        assert names(list_skills(member, "global")) == [("house-style", "everyone")]
    assert names(list_skills(alice)) == [("house-style", "everyone"), ("team-notes", "everyone")]

    def get(login: str, path: str):
        return hub.client.get(path, headers=hub.who[login])

    hidden, missing = get("alice", "/v1/skills/global/release-keys"), get("alice", "/v1/skills/global/no-such")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["message"] == "no global skill release-keys: see `evo-agents hub skills list`"
    assert missing.json()["message"] == "no global skill no-such: see `evo-agents hub skills list`"
    for path in ("/v1/skills/global/release-keys/bundle", "/v1/skills/global/RELEASE-KEYS/bundle?version=1"):
        refused = get("carol", path)
        assert refused.status_code == 404 and "url" not in refused.json(), refused.text
    shown = get(live.ADMIN, "/v1/skills/global/release-keys")
    assert shown.status_code == 200 and shown.json()["audience"] == "admins"
    assert get(live.ADMIN, "/v1/skills/global/release-keys/bundle").status_code == 200
    assert get("alice", "/v1/skills/global/house-style").json()["audience"] == "everyone"
    assert get("alice", "/v1/skills/projects/demo/team-notes").json()["audience"] == "everyone"


@needs_pg
def test_a_hub_admin_changes_the_audience_and_each_change_is_one_audit_row(hub, tmp_path):
    admin = hub.as_(live.ADMIN)
    skill = write_skill(tmp_path / "source", "house-style")
    publish(admin, skill)

    def put(login: str, name: str, body: dict):
        return hub.client.put(AUDIENCE.format(name=name), json=body, headers=hub.who[login])

    refused = put("alice", "house-style", {"audience": "admins"})
    assert refused.status_code == 403 and "hub admin" in refused.json()["message"]
    assert put(live.ADMIN, "no-such", {"audience": "admins"}).status_code == 404
    assert put(live.ADMIN, "house-style", {"audience": "members"}).status_code == 422
    assert audit_rows(hub.db) == []

    changed = put(live.ADMIN, "House-Style", {"audience": "admins"})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"name": "house-style", "audience": "admins", "changed": True}
    assert put(live.ADMIN, "house-style", {"audience": "admins"}).json()["changed"] is False
    assert audit_rows(hub.db) == [("skill:global/house-style audience=everyone->admins", None)]
    assert list_skills(hub.as_("alice"), "global") == []

    # Publishing the same bundle again keeps the audience without one, and changes it with one.
    again = publish(admin, skill)
    assert (again["created"], again["audience"]) == (False, "admins")
    opened = publish(admin, skill, audience="everyone")
    assert (opened["created"], opened["audience"]) == (False, "everyone")
    (skill / "notes.md").write_text("a second version\n", encoding="utf-8")
    closed = publish(admin, skill, audience="admins")
    assert (closed["created"], closed["latest"]["version"], closed["audience"]) == (True, 2, "admins")
    assert [target for target, _ in audit_rows(hub.db)] == [
        "skill:global/house-style audience=everyone->admins",
        "skill:global/house-style audience=admins->everyone",
        "skill:global/house-style audience=everyone->admins",
    ]

    # A project skill is its members': admins is refused before any version, and there is no route to change it.
    alice, bundle = hub.as_("alice"), {"sha256": "a" * 64, "size": 1}
    with pytest.raises(HubError, match="audience admins is a global skill's"):
        alice.call("POST", "/v1/skills/projects/demo/team-notes/versions", {**bundle, "audience": "admins"})
    path = "/v1/skills/projects/demo/team-notes/audience"
    response = hub.client.put(path, json={"audience": "admins"}, headers=hub.who[live.ADMIN])
    assert response.status_code in (404, 405)


# Sync and the SessionStart check


@needs_pg
def test_sync_and_the_session_start_check_follow_the_audience(hub, tmp_path, own_home):
    admin, alice = hub.as_(live.ADMIN), hub.as_("alice")
    source = tmp_path / "source"
    for name in ("house-style", "release-keys"):
        publish(admin, write_skill(source, name))
    claude = own_home / ".claude" / "skills"
    claude.mkdir(parents=True)
    assert sync(alice).counts() == {"install": 2}
    assert sorted(p.name for p in claude.iterdir()) == ["house-style", "release-keys"]

    assert hub.client.put(
        AUDIENCE.format(name="release-keys"), json={"audience": "admins"}, headers=hub.who[live.ADMIN]
    ).json()["changed"]
    line = hooks.Line()
    hooks._check_skills(alice, line)
    assert line.parts == ["1 skill to sync (`evo-agents hub skills sync`)"]
    report = sync(alice)
    assert report.counts() == {"remove": 1, "current": 1} and report.errors == []
    assert sorted(p.name for p in claude.iterdir()) == ["house-style"]
    assert (Path(report.backup) / "global" / "claude" / "release-keys" / "SKILL.md").is_file()
    line = hooks.Line()
    hooks._check_skills(alice, line)
    assert line.parts == ["skills up to date"]

    # A hub admin's machine writes it.
    admin_home = tmp_path / "admin-home"
    (admin_home / ".claude").mkdir(parents=True)
    assert sync(admin, home=admin_home).counts() == {"install": 2}


# The command line against `hub serve`


@needs_pg
def test_the_cli_publishes_with_an_audience_and_changes_it_against_hub_serve(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin_home, member_home = tmp_path / "admin", tmp_path / "member"
        sign_in(admin_home, served.url, live.ADMIN, live.insert_token(hub_db, live.ADMIN))
        sign_in(member_home, served.url, "member", live.insert_token(hub_db, "member"))
        skill = write_skill(tmp_path / "agent-skills" / "skills", "release-keys")

        published = cli_ok(skills_cli(["publish", str(skill), "--audience", "admins"], admin_home))
        assert published.stdout.startswith("Published global skill release-keys version 1 (")
        assert "Hub admins alone see it" in published.stdout
        bad = skills_cli(["publish", str(skill), "--audience", "nobody"], admin_home)
        assert bad.returncode == 2 and "invalid choice" in bad.stderr

        assert json.loads(cli_ok(skills_cli(["list", "--json"], member_home)).stdout) == []
        listed = json.loads(cli_ok(skills_cli(["list", "--json"], admin_home)).stdout)
        assert_json_keys("hub skills list", listed)
        assert names(listed) == [("release-keys", "admins")]
        assert "AUDIENCE" in cli_ok(skills_cli(["list"], admin_home)).stdout

        refused = skills_cli(["audience", "release-keys", "everyone"], member_home)
        assert refused.returncode == 1 and refused.stdout == ""
        assert refused.stderr.startswith("error: this needs a hub admin")
        changed = json.loads(cli_ok(skills_cli(["audience", "release-keys", "everyone", "--json"], admin_home)).stdout)
        assert_json_keys("hub skills audience", changed)
        assert changed == {"name": "release-keys", "audience": "everyone", "changed": True}
        again = cli_ok(skills_cli(["audience", "release-keys", "everyone"], admin_home))
        assert again.stdout == "Global skill release-keys is for every signed-in member already; nothing changed.\n"
        assert names(json.loads(cli_ok(skills_cli(["list", "--json"], member_home)).stdout)) == [
            ("release-keys", "everyone")
        ]
    messages = [line["msg"] for line in pg.log_lines(served.log())]
    assert messages.count("skill audience changed") == 1


# The skill_list tool, for a member and for the agent of a run


@pytest.fixture
def mcp_hub(hub_db, tmp_path):
    """The app with project alpha of ``tests.hub.test_mcp``, alice a writer and the hub admin an admin of it, and two
    global skills, house-style for everyone and release-keys for hub admins, written straight into the database."""
    from fastapi.testclient import TestClient
    from sqlalchemy import insert, select

    from evo_agents.hub import tables
    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app
    from tests.hub.test_mcp import PROJECT, REGISTRATION

    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=3.0,
        admins=frozenset({live.ADMIN}),
        public_url="https://hub.test",
    )
    app = create_app(config)
    with TestClient(app, base_url="https://hub.test") as client:
        admin = live.bearer(live.insert_token(hub_db, live.ADMIN))
        assert client.put(f"/v1/projects/{PROJECT}", json=REGISTRATION, headers=admin).status_code == 200
        members = SimpleNamespace(client=client, db=hub_db, app=app, admin=admin)
        members.alice = live.bearer(live.insert_token(hub_db, "alice"))
        for login, role in (("alice", "writer"), (live.ADMIN, "admin")):
            grant = {"role": role, "max_level": "internal"}
            response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
            assert response.status_code == 200, response.text
        skills, versions = tables.skills, tables.skill_versions
        with live.engine(hub_db).begin() as conn:
            by = conn.execute(select(tables.users.c.id).where(tables.users.c.login == live.ADMIN)).scalar_one()
            for number, (name, audience) in enumerate((("house-style", "everyone"), ("release-keys", "admins")), 1):
                values = {"scope": "global", "name": name, "created_by": by, "audience": audience}
                skill_id = conn.execute(insert(skills).values(**values).returning(skills.c.id)).scalar_one()
                digest = f"{number:064x}"
                version = {"skill_id": skill_id, "version": 1, "name": name, "sha256": digest, "size": 1}
                conn.execute(insert(versions).values(**version, r2_key=f"blobs/sha256/{digest}", published_by=by))
        yield members


@needs_pg
def test_skill_list_shows_the_agent_of_a_run_the_audience_its_owner_reads(mcp_hub):
    from tests.hub.test_mcp import agent_tool, ok, push_plan, run_on, worker_of

    def listed(result: dict) -> list[str]:
        return [s["name"] for s in result["structuredContent"]["skills"]]

    member = ok(mcp_hub, "alice", "skill_list")
    assert listed(member) == ["house-style"]
    shown = ok(mcp_hub, "admin", "skill_list")
    assert listed(shown) == ["house-style", "release-keys"]
    assert shown["content"][0]["text"].splitlines()[1].startswith("release-keys (global, version 1, hub admins only): ")
    assert ok(mcp_hub, "admin", "skill_list", {"scope": "project"})["structuredContent"]["skills"] == []

    push_plan(mcp_hub)
    admins_worker = worker_of(mcp_hub, "admin", "admin-box")
    admins_run = run_on(mcp_hub, "admin", admins_worker, 1)
    alices_worker = worker_of(mcp_hub, "alice", "alice-box")
    alices_run = run_on(mcp_hub, "alice", alices_worker, 2)
    assert listed(agent_tool(mcp_hub, admins_worker, admins_run, "skill_list")) == ["house-style", "release-keys"]
    assert listed(agent_tool(mcp_hub, alices_worker, alices_run, "skill_list")) == ["house-style"]


# Migration 0111


@needs_pg
@pytest.mark.empty_db
def test_migration_0111_leaves_every_skill_everyones_and_takes_the_audience_back(hub_db):
    import psycopg
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub.live import add_project, sql
    from tests.hub.test_migrate import ALL, columns, migrate, move_to

    move_to(hub_db, "0110")
    users, skills = tables.users, tables.skills
    (user_id,) = sql(hub_db, users.insert().values(login="octo").returning(users.c.id))[0]
    project_id = add_project(hub_db, "demo")
    held = {"created_by": user_id}
    sql(hub_db, skills.insert().values(scope="global", name="house-style", **held))
    sql(hub_db, skills.insert().values(scope="project", project_id=project_id, name="team-notes", **held))

    move_to(hub_db, "0111")
    assert sql(hub_db, select(skills.c.name, skills.c.audience).order_by(skills.c.name)) == [
        ("house-style", "everyone"),
        ("team-notes", "everyone"),
    ]
    sql(hub_db, skills.insert().values(scope="global", name="release-keys", audience="admins", **held))
    for bad in (
        {"scope": "global", "name": "a", "audience": "nobody"},
        {"scope": "project", "project_id": project_id, "name": "b", "audience": "admins"},
    ):
        with pytest.raises(psycopg.errors.CheckViolation, match="skills_audience_check"):
            sql(hub_db, skills.insert().values(**bad, **held))

    move_to(hub_db, "0110", down=True)
    assert "audience" not in columns(hub_db, "skills")
    assert migrate(hub_db.dsn).applied == tuple(ALL[ALL.index("0111") :])
    assert "audience" in columns(hub_db, "skills")
